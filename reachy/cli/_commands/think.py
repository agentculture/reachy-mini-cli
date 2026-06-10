"""``reachy-mini-cli think`` — think out loud about what the robot perceives.

A continuous cognition mode (the same shape as the ``listen`` noun): a foreground
loop reads the robot's live senses, accumulates them into a sense-event buffer,
and on each turn hands the buffer to the :class:`~reachy.speech.cognition.CognitionEngine`,
which streams a short spoken thought back through TTS + playback. Sentences are
*streamed* — the first sentence reaches the speaker before the LLM finishes the
turn (see :mod:`reachy.speech.cognition`).

Three faces, like the ``listen`` / ``daemon`` nouns:

* **run** — the foreground loop (what ``start`` / the process launch run);
* **start** / **stop** / **restart** — manage it as a tracked background process
  (PID + log under the state dir, via :mod:`reachy.speech.supervisor`);
* **status** — loop process state.

The sense feed mirrors ``listen``: the ``sdk`` transport (default) opens a
``ReachyMini`` media session and reads real DoA + mic RMS per turn; the ``http``
transport polls the daemon's DoA route instead. Each turn's fresh reading is
pumped into the :class:`~reachy.speech.events.EventBuffer` via the engine's
``before_turn`` hook, then consumed by that turn's snapshot.

Errors degrade under the structured contract: an unreachable LLM or TTS endpoint
raises a :class:`~reachy.cli._errors.CliError` (exit 2) from inside the engine's
collaborators — never a Python traceback.
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Callable

import numpy as np

from reachy.behavior.sense import DOA_TIMEOUT, DoaPoller, read_doa
from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.robot import add_robot_args, get_transport
from reachy.speech import supervisor
from reachy.speech.cognition import DEFAULT_TURN_INTERVAL, CognitionEngine
from reachy.speech.events import EventBuffer
from reachy.speech.llm import stream_sentences as _stream_sentences
from reachy.speech.playback import play_audio as _play_audio
from reachy.speech.tts import synthesize as _synthesize

_JSON_HELP = "Emit structured JSON."

# Rolling sense-event window size (matches EventBuffer's own default).
_DEFAULT_MAXLEN = 256

# Self-mute window (seconds) after the robot finishes speaking. Playback and
# capture share the one Reachy Mini USB audio device, so the mic hears the robot's
# own voice — without this guard think reacts to itself in a runaway feedback loop.
# v1 has no AEC/barge-in (intentional boundary); this is the minimal guard. 0 disables.
_DEFAULT_MUTE_AFTER_SPEAK = 2.5

_VERBS = [
    "think run — run the cognition loop in the foreground",
    "think start — start the loop in the background (tracked process)",
    "think stop — stop the loop this CLI started",
    "think restart — restart the background loop (re-reads flags + code)",
    "think status — loop process state",
    "think overview — this summary",
]


# --- overview -------------------------------------------------------------


def cmd_think_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "A continuous cognition loop: live senses → sense-event buffer → "
                "streamed spoken thought.",
                "Each turn snapshots what the robot just perceived (DoA / mic loudness / "
                "speech, and — when wired — camera motion + light) and asks the LLM for "
                "one or two short first-person sentences.",
                "Sentence-streamed: the first sentence is synthesized and played while "
                "later sentences are still being generated (think↔speak overlap).",
                "SDK-first by default: real DoA + mic loudness in-process; use "
                "--transport http to poll the daemon's DoA route instead.",
                "Graceful: an empty buffer is a no-op turn (no LLM call, no audio).",
            ],
        },
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "State",
            "items": [
                f"pid file: {supervisor.pid_file()}",
                f"log file: {supervisor.log_file()}",
            ],
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "LLM endpoint: --llm-base-url / --llm-model (env REACHY_LLM_BASE_URL / "
                "REACHY_LLM_MODEL)",
                "TTS endpoint: --tts-url / --voice (env REACHY_TTS_URL / REACHY_TTS_VOICE)",
                "pacing: --turn-interval (seconds between turns)",
                "bound a run for testing/ops with --max-turns / --max-ticks",
                "exit codes: 0 ok, 1 user error, 2 environment (LLM/TTS/daemon unreachable)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli think",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


# --- sense feed (DoA/RMS/speech → EventBuffer) ----------------------------


def _feed_doa(buffer: EventBuffer, sense, rms: float) -> None:
    """Translate one DoA reading + loudness into a cue and append it."""
    buffer.feed_doa(
        angle_rad=sense.doa_angle,
        rms=rms,
        is_speech=bool(getattr(sense, "speech_detected", False)),
    )


def _make_sdk_feed(transport: object, buffer: EventBuffer) -> Callable[[], None]:
    """A ``before_turn`` hook over an open SDK media session (real DoA + mic RMS).

    Opens the media session once (held for the loop's lifetime) and, on each
    turn, reads the latest DoA snapshot and the mic RMS, feeding both into the
    buffer. The session is entered eagerly so a missing SDK / dead daemon raises
    its clean CliError before the loop starts.
    """
    session = transport.media_session()  # type: ignore[attr-defined]
    # Support both context-manager sessions (real SDK) and plain objects (fakes).
    # When it is a context manager, enter it now (so a missing SDK / dead daemon
    # raises its clean CliError before the loop starts) and remember it so the
    # caller can close it — skipping __exit__ would leave the mic recorder running
    # for the rest of the process (the SDK finalizes recording in __exit__).
    cm = session if (hasattr(session, "__enter__") and hasattr(session, "__exit__")) else None
    if cm is not None:
        session = cm.__enter__()
    poller = DoaPoller(read=lambda: read_doa(session, timeout=DOA_TIMEOUT))

    def _feed() -> None:
        sense = poller()
        sample = session.get_audio_sample()
        rms = float(np.sqrt(np.mean(sample**2))) if sample is not None else 0.0
        _feed_doa(buffer, sense, rms)
        # Vision cues (camera motion + light) are not fed here yet — tracked in
        # issue #32. The engine already consumes any cues the buffer holds, so
        # wiring buffer.feed_vision() later is additive (see reachy.vision.*).

    def _close() -> None:
        if cm is not None:
            cm.__exit__(None, None, None)

    _feed.close = _close  # type: ignore[attr-defined]
    return _feed


def _make_http_feed(transport: object, buffer: EventBuffer) -> Callable[[], None]:
    """A ``before_turn`` hook over the HTTP transport's DoA route (no mic RMS)."""
    poller = DoaPoller(read=lambda: read_doa(transport, timeout=DOA_TIMEOUT))

    def _feed() -> None:
        sense = poller()
        # The HTTP DoA route carries no loudness; treat speech-detected as the
        # only "notable" signal (non-speech non-loud readings emit no cue).
        _feed_doa(buffer, sense, rms=0.0)

    return _feed


def _make_sense_feed(args: argparse.Namespace, buffer: EventBuffer) -> Callable[[], None]:
    """Build the ``before_turn`` sense feed for the selected transport.

    The SDK profile streams real DoA + mic loudness through a media session; the
    HTTP/remote profile polls the daemon's DoA route with no audio source.
    """
    transport = get_transport(args)
    if hasattr(transport, "media_session"):
        return _make_sdk_feed(transport, buffer)
    return _make_http_feed(transport, buffer)


# --- run (foreground loop) ------------------------------------------------


def _llm_kwargs(args: argparse.Namespace) -> dict:
    kw: dict = {}
    if getattr(args, "llm_base_url", None) is not None:
        kw["base_url"] = args.llm_base_url
    if getattr(args, "llm_model", None) is not None:
        kw["model"] = args.llm_model
    return kw


def _tts_kwargs(args: argparse.Namespace) -> dict:
    kw: dict = {}
    if getattr(args, "tts_url", None) is not None:
        kw["tts_url"] = args.tts_url
    if getattr(args, "voice", None) is not None:
        kw["voice"] = args.voice
    return kw


def _playback_kwargs(args: argparse.Namespace) -> dict:
    """Playback transport + daemon URL (mirrors the ``say`` noun)."""
    return {
        "transport": getattr(args, "transport", None),
        "base_url": getattr(args, "base_url", "http://localhost:8000"),
    }


def cmd_think_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    buffer = EventBuffer(maxlen=_DEFAULT_MAXLEN)
    feed = _make_sense_feed(args, buffer)
    turn_interval = _resolve_turn_interval(args)
    mute_after = _resolve_mute_after(args)

    # Self-mute guard against the audio feedback loop: while the robot is speaking
    # and for `mute_after` seconds after, suppress sense cues so think never reacts
    # to its own voice (mic + speaker share one device). `play_audio` stamps the
    # window forward on every clip; the feed drops anything captured inside it.
    mute = {"until": 0.0}

    def _guarded_play(pcm: bytes, **kwargs: object) -> None:
        _play_audio(pcm, **kwargs)
        if mute_after > 0:
            mute["until"] = time.monotonic() + mute_after

    def _guarded_feed() -> None:
        if time.monotonic() < mute["until"]:
            buffer.snapshot()  # discard the robot's own speech the mic just caught
            return
        feed()

    _guarded_feed.close = getattr(feed, "close", None)  # type: ignore[attr-defined]

    engine = CognitionEngine(
        buffer=buffer,
        stream_sentences=_stream_sentences,
        synthesize=_synthesize,
        play_audio=_guarded_play,
        llm_kwargs=_llm_kwargs(args),
        tts_kwargs=_tts_kwargs(args),
        playback_kwargs=_playback_kwargs(args),
        turn_interval=turn_interval,
    )

    if not json_mode:
        emit_diagnostic(
            f"[think] thinking out loud via {getattr(args, 'transport', 'sdk')}; "
            f"turn-interval={turn_interval:g}s mute-after-speak={mute_after:g}s; Ctrl-C to stop"
        )

    # --max-ticks bounds the loop by *iterations* (idle turns included); --max-turns
    # bounds it by *spoken* turns. Either (or both) makes a run terminate for tests/ops.
    max_turns = getattr(args, "max_turns", None)
    stop = _tick_stop(getattr(args, "max_ticks", None))

    try:
        turns = engine.run(max_turns=max_turns, stop=stop, before_turn=_guarded_feed)
    finally:
        # Close the SDK media session (stops the mic recorder) on every exit path
        # — normal stop, max-ticks/turns, Ctrl-C, or error. Fakes/http feeds carry
        # no closer.
        close = getattr(_guarded_feed, "close", None)
        if close is not None:
            close()

    if json_mode:
        emit_result({"status": "ok", "turns": turns}, json_mode=True)
    else:
        emit_diagnostic(f"[think] stopped after {turns} spoken turn(s)")
    return 0


def _resolve_mute_after(args: argparse.Namespace) -> float:
    """Self-mute window after speaking: the flag value, or the built-in default."""
    value = getattr(args, "mute_after_speak", None)
    return _DEFAULT_MUTE_AFTER_SPEAK if value is None else max(0.0, float(value))


def _resolve_turn_interval(args: argparse.Namespace) -> float:
    """The pacing gap between turns: the flag value, or the engine default."""
    value = getattr(args, "turn_interval", None)
    return DEFAULT_TURN_INTERVAL if value is None else float(value)


def _tick_stop(max_ticks: int | None) -> Callable[[], bool] | None:
    """A ``stop`` predicate that fires after ``max_ticks`` iterations, or None."""
    if max_ticks is None:
        return None
    state = {"n": 0}

    def _stop() -> bool:
        if state["n"] >= max_ticks:
            return True
        state["n"] += 1
        return False

    return _stop


# --- start / stop / restart / status --------------------------------------


def cmd_think_start(args: argparse.Namespace) -> int:
    data = supervisor.start(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        llm_base_url=getattr(args, "llm_base_url", None),
        llm_model=getattr(args, "llm_model", None),
        tts_url=getattr(args, "tts_url", None),
        voice=getattr(args, "voice", None),
        turn_interval=getattr(args, "turn_interval", None),
        mute_after_speak=getattr(args, "mute_after_speak", None),
        max_turns=getattr(args, "max_turns", None),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_think_stop(args: argparse.Namespace) -> int:
    data = supervisor.stop(timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_think_restart(args: argparse.Namespace) -> int:
    data = supervisor.restart(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        llm_base_url=getattr(args, "llm_base_url", None),
        llm_model=getattr(args, "llm_model", None),
        tts_url=getattr(args, "tts_url", None),
        voice=getattr(args, "voice", None),
        turn_interval=getattr(args, "turn_interval", None),
        mute_after_speak=getattr(args, "mute_after_speak", None),
        max_turns=getattr(args, "max_turns", None),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_think_status(args: argparse.Namespace) -> int:
    data = supervisor.status()
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_think_overview(args)


# --- shared cognition args ------------------------------------------------


def _add_cognition_args(parser: argparse.ArgumentParser) -> None:
    """LLM / TTS endpoint + pacing knobs shared by run / start / restart."""
    parser.add_argument(
        "--llm-base-url",
        default=None,
        dest="llm_base_url",
        help="LLM base URL (overrides REACHY_LLM_BASE_URL).",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        dest="llm_model",
        help="LLM model name (overrides REACHY_LLM_MODEL).",
    )
    parser.add_argument(
        "--tts-url",
        default=None,
        dest="tts_url",
        help="TTS base URL (overrides REACHY_TTS_URL).",
    )
    parser.add_argument(
        "--voice",
        default=None,
        help="TTS voice identifier (overrides REACHY_TTS_VOICE).",
    )
    parser.add_argument(
        "--turn-interval",
        type=float,
        default=None,
        dest="turn_interval",
        help=f"Seconds between cognition turns (default {DEFAULT_TURN_INTERVAL:g}).",
    )
    parser.add_argument(
        "--mute-after-speak",
        type=float,
        default=None,
        dest="mute_after_speak",
        help="Seconds to ignore the mic after speaking, to avoid hearing itself "
        f"(default {_DEFAULT_MUTE_AFTER_SPEAK:g}; 0 disables).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        dest="max_turns",
        help="Stop after this many *spoken* turns (default: run until signalled).",
    )


def _register_run(noun_sub: argparse._SubParsersAction) -> None:
    run = noun_sub.add_parser("run", help="Run the cognition loop in the foreground.")
    add_robot_args(run)
    run.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_cognition_args(run)
    run.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        dest="max_ticks",
        help="Stop after this many loop iterations (idle turns included).",
    )
    run.set_defaults(func=cmd_think_run)


def _register_process_verbs(noun_sub: argparse._SubParsersAction) -> None:
    start = noun_sub.add_parser("start", help="Start the cognition loop in the background.")
    add_robot_args(start)
    start.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_cognition_args(start)
    start.set_defaults(func=cmd_think_start)

    restart = noun_sub.add_parser("restart", help="Restart the background loop (re-reads flags).")
    add_robot_args(restart)
    restart.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_cognition_args(restart)
    restart.set_defaults(func=cmd_think_restart)

    stop = noun_sub.add_parser("stop", help="Stop the loop this CLI started.")
    stop.add_argument("--json", action="store_true", help=_JSON_HELP)
    stop.add_argument(
        "--timeout",
        type=float,
        default=supervisor.DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait after SIGTERM before SIGKILL "
        f"(default: {supervisor.DEFAULT_STOP_TIMEOUT:g}).",
    )
    stop.set_defaults(func=cmd_think_stop)

    st = noun_sub.add_parser("status", help="Report think loop process state.")
    st.add_argument("--json", action="store_true", help=_JSON_HELP)
    st.set_defaults(func=cmd_think_status)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "think",
        help="Think out loud about what the robot perceives "
        "(see 'reachy-mini-cli think overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="think_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the think noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_think_overview)

    _register_run(noun_sub)
    _register_process_verbs(noun_sub)
