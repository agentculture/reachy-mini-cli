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
import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from reachy.behavior.sense import DOA_TIMEOUT, DoaPoller, read_doa
from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import CliError
from reachy.cli._export import add_export_args, build_export_hook
from reachy.cli._logging import add_log_level_arg, install_logging
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.daemon import state_dir
from reachy.motion.expression import ExpressionProducer
from reachy.motion.queue import MotionQueue
from reachy.motion.server import run as run_motion
from reachy.robot import add_robot_args, get_transport
from reachy.speech import cognition_signal, supervisor
from reachy.speech.cognition import DEFAULT_SYSTEM_PROMPT, DEFAULT_TURN_INTERVAL, CognitionEngine
from reachy.speech.distinctness import find_too_similar as _find_too_similar
from reachy.speech.events import EventBuffer
from reachy.speech.expressions import NEUTRAL_KEY, Catalog
from reachy.speech.harmonic import synthesize as _harmonic_synthesize
from reachy.speech.llm import stream_sentences as _stream_sentences
from reachy.speech.markers import MarkerEvent, SpeechEvent
from reachy.speech.markers import parse as _parse_marker_script
from reachy.speech.playback import play_audio as _play_audio
from reachy.speech.tts import synthesize as _synthesize
from reachy.speech.voice import VOICE_ENGINE_ENV, VoiceEngine, resolve_voice_engine

_JSON_HELP = "Emit structured JSON."

# Rolling sense-event window size (matches EventBuffer's own default).
_DEFAULT_MAXLEN = 256

# Self-mute window (seconds) after the robot finishes speaking. Playback and
# capture share the one Reachy Mini USB audio device, so the mic hears the robot's
# own voice — without this guard think reacts to itself in a runaway feedback loop.
# v1 has no AEC/barge-in (intentional boundary); this is the minimal guard. 0 disables.
_DEFAULT_MUTE_AFTER_SPEAK = 2.5

# Sidecar file (next to the supervisor's own think.pid/think.log, under the shared
# state dir) recording the active think run's voice engine name — the mechanism
# `think status --json` reads to report which engine a *running* loop uses. Written
# on run entry, removed on every exit path (mirrors cognition_signal.cognition_active()'s
# enter/exit symmetry) so a stale name never survives a crash. Deliberately NOT added
# to reachy.speech.supervisor (this feature stays inside think.py).
_VOICE_SIDECAR_NAME = "think.voice"

# The --voice-engine flag's argparse choices; kept in sync with
# reachy.speech.voice's registered engine names ("tts", "harmonic").
_VOICE_ENGINE_CHOICES = ("tts", "harmonic")

_VERBS = [
    "think run — run the cognition loop in the foreground",
    "think start — start the loop in the background (tracked process)",
    "think stop — stop the loop this CLI started",
    "think restart — restart the background loop (re-reads flags + code)",
    "think status — loop process state",
    "think expressions — list the expression catalog (and 'expressions check')",
    "think demo — run a scripted expression stream for hardware verification",
    "think overview — this summary",
]


# --- expression vocabulary (catalog → prompt + listing) -------------------


def _expression_emojis(catalog: Catalog | None = None) -> list[str]:
    """The catalog's expression emojis (every key except the neutral fallback)."""
    cat = catalog if catalog is not None else Catalog()
    return [key for key in cat.keys() if key != NEUTRAL_KEY]


def _pose_descriptor(catalog: Catalog, emoji: str) -> str:
    """A short, generated descriptor of an emoji's pose (its non-zero axes).

    The catalog is pose values only (the TOML's prose lives in comments, which
    ``tomllib`` drops), so we summarise the pose itself — the non-zero axes and
    their signed magnitudes — giving an agent a machine-stable, catalog-derived
    descriptor without duplicating the TOML comments in code.
    """
    pose = catalog.get(emoji)
    axes = [
        ("head_x", pose.head_x),
        ("head_y", pose.head_y),
        ("head_z", pose.head_z),
        ("head_roll", pose.head_roll),
        ("head_pitch", pose.head_pitch),
        ("head_yaw", pose.head_yaw),
        ("antenna_right", pose.antenna_right),
        ("antenna_left", pose.antenna_left),
        ("body_yaw", pose.body_yaw),
    ]
    moved = [f"{name}{value:+g}" for name, value in axes if value]
    return ", ".join(moved) if moved else "neutral (no offset)"


def _build_system_prompt(*, emojis: list[str], base: str = DEFAULT_SYSTEM_PROMPT) -> str:
    """Append the available emoji vocabulary + the marker convention to *base*.

    The vocabulary is pulled from the live catalog (never hardcoded), so editing
    ``expressions.toml`` re-shapes what the LLM is told it may express. The
    convention line teaches the ``*emoji*`` (expression) / ``"quoted"`` (speech)
    output contract the cognition loop parses.
    """
    vocab = " ".join(emojis)
    convention = (
        " Output format: write NOTHING except asterisk-wrapped emojis and "
        "double-quoted speech. Express body language by emitting one of the "
        "available emojis wrapped in asterisks (e.g. *<emoji>*); put every word "
        'you want spoken aloud inside double quotes (e.g. "like this"). Begin your '
        "reply with a quote or an emoji marker. Any text outside quotes and emoji "
        "markers is discarded, not spoken — so never write unquoted narration or a "
        "lead-in. Only quoted text is spoken; only an asterisk-wrapped emoji moves "
        f"the body. Available expressions: {vocab}."
    )
    return base + convention


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
                "LLM endpoint: --llm-base-url / --llm-model (env REACHY_OPENAI_URL_BASE / "
                "REACHY_OPENAI_MODEL_ID)",
                "TTS endpoint: --tts-url / --voice (env REACHY_TTS_URL / REACHY_TTS_VOICE)",
                "voice engine: --voice-engine {tts,harmonic} (env REACHY_VOICE_ENGINE; "
                "default tts); 'status --json' reports the running loop's voice_engine",
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


# --- expressions sub-noun (catalog tooling) -------------------------------

_EXPR_VERBS = [
    "expressions list — list the expression catalog (emoji + pose descriptor)",
    "expressions check — flag catalog poses too similar to be distinct",
    "expressions overview — this summary",
]


def cmd_expressions_list(args: argparse.Namespace) -> int:
    """List the expression catalog: each emoji + a short pose descriptor."""
    catalog = Catalog()
    rows = [
        {"emoji": emoji, "descriptor": _pose_descriptor(catalog, emoji)}
        for emoji in _expression_emojis(catalog)
    ]
    if bool(getattr(args, "json", False)):
        emit_result({"expressions": rows}, json_mode=True)
    else:
        lines = [f"{row['emoji']}  {row['descriptor']}" for row in rows]
        emit_result("\n".join(lines), json_mode=False)
    return 0


def cmd_expressions_check(args: argparse.Namespace) -> int:
    """Run the distinctness check; report flagged pairs (clean check exits 0).

    A flagged pair is a *warning*, not an error — the catalog still works — so the
    exit code stays 0; the ``--json`` ``ok`` field is the machine-readable signal.
    """
    catalog = Catalog()
    flagged = _find_too_similar(catalog)
    ok = not flagged
    if bool(getattr(args, "json", False)):
        emit_result(
            {"ok": ok, "flagged": [[a, b, score] for a, b, score in flagged]},
            json_mode=True,
        )
    else:
        if ok:
            emit_result("clean — all expressions are sufficiently distinct", json_mode=False)
        else:
            lines = [f"{a} ~ {b} (distance {score:.3f})" for a, b, score in flagged]
            emit_result(
                "too similar (" + str(len(flagged)) + " pair(s)):\n" + "\n".join(lines),
                json_mode=False,
            )
    return 0


def cmd_expressions_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "The emoji-keyed expression catalog think uses to gesture while "
                "thinking (loaded from expressions.toml).",
                "list — every catalog emoji + a generated pose descriptor.",
                "check — flags catalog poses too similar to be meaningfully distinct.",
            ],
        },
        {"title": "Verbs", "items": list(_EXPR_VERBS)},
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "results to stdout, diagnostics to stderr (never mixed)",
                "a flagged 'check' is a warning, not an error — exit stays 0",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli think expressions",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _expressions_no_verb(args: argparse.Namespace) -> int:
    # Bare `think expressions` lists the catalog.
    return cmd_expressions_list(args)


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
        # Vision cues are fed by the folded VisionHook under `listen --live`
        # (buffer.feed_vision, issue #32) — standalone `think run` remains
        # audio-only by design; the engine consumes any cues the buffer holds.

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


# --- voice engine selection (tts / harmonic) -------------------------------


def _voice_sidecar_path() -> Path:
    """Path to the ``think.voice`` sidecar — see :data:`_VOICE_SIDECAR_NAME`."""
    return state_dir() / _VOICE_SIDECAR_NAME


def _write_voice_sidecar(name: str) -> None:
    """Record the active voice engine name for ``status`` to read (best effort).

    A failed write only degrades the ``status --json`` ``voice_engine`` field —
    it must never abort a think run.
    """
    try:
        _voice_sidecar_path().write_text(name, encoding="utf-8")
    except OSError:
        pass


def _clear_voice_sidecar() -> None:
    """Remove the sidecar on any run exit (clean stop, signal, or crash)."""
    try:
        _voice_sidecar_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _read_voice_sidecar() -> str | None:
    """The recorded voice engine name, or ``None`` if absent/unreadable."""
    try:
        text = _voice_sidecar_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _resolve_engine(args: argparse.Namespace) -> VoiceEngine:
    """Resolve think's active voice engine: --voice-engine > REACHY_VOICE_ENGINE > tts."""
    return resolve_voice_engine(getattr(args, "voice_engine", None))


def _synthesize_for(engine: VoiceEngine) -> Callable[..., bytes]:
    """The synthesize callable for *engine*.

    Routed through the same module-level aliases the collaborators were always
    wired from (``_synthesize`` for tts, ``_harmonic_synthesize`` for harmonic) —
    not ``engine.synthesize`` directly — so existing tests that monkeypatch
    ``think_mod._synthesize`` keep intercepting the default (tts) engine
    unchanged, and a harmonic-engine test can monkeypatch
    ``think_mod._harmonic_synthesize`` the same way.
    """
    return _harmonic_synthesize if engine.name == "harmonic" else _synthesize


def _tts_kwargs_for(engine: VoiceEngine, args: argparse.Namespace) -> dict:
    """tts_kwargs for *engine*: harmonic's ``synthesize()`` accepts no tts kwargs."""
    if engine.name == "harmonic":
        return {}
    return _tts_kwargs(args)


def _playback_kwargs_for(engine: VoiceEngine, args: argparse.Namespace) -> dict:
    """playback_kwargs for *engine*.

    Every non-tts engine pins ``samplerate`` to its own native rate (harmonic
    renders at 16 kHz, not Chatterbox's 24 kHz) while keeping the other playback
    kwargs (``transport`` / ``base_url``) unchanged. The tts engine keeps its
    pre-existing kwargs unset (no ``samplerate`` key), preserving byte-identical
    behaviour for bare ``think run`` / ``think demo``.
    """
    kwargs = _playback_kwargs(args)
    if engine.name != "tts":
        kwargs["samplerate"] = engine.samplerate
    return kwargs


def _mute_window(clip_pcm: bytes, samplerate: int, mute_after: float, *, now: float) -> float:
    """The self-mute ``until`` timestamp for a just-played clip.

    Covers the clip's own play duration (derived from *samplerate* — the active
    engine's rate, not a hardcoded TTS assumption) PLUS the ``mute_after`` margin,
    so a longer/slower-rendered clip (e.g. harmonic's 16 kHz vs tts's 24 kHz) is
    never unmuted mid-utterance. Callers only invoke this when ``mute_after > 0``
    (the guard-disabled path never calls it).
    """
    clip_seconds = len(clip_pcm) / (2.0 * samplerate) if samplerate > 0 else 0.0
    return now + clip_seconds + mute_after


@contextlib.contextmanager
def _voice_engine_env(name: str | None):
    """Temporarily set ``REACHY_VOICE_ENGINE`` for a spawned think-run subprocess.

    ``think start`` / ``think restart`` re-exec this CLI via
    :func:`reachy.speech.supervisor.start` / ``restart``, which spawn a *new*
    process that inherits the current environment (``subprocess.Popen`` there is
    called with no explicit ``env=``). An explicit ``--voice-engine`` flag on
    ``start``/``restart`` is not itself forwarded as a CLI arg to the spawned
    ``think run`` — that would require extending the supervisor's
    ``build_run_command`` (deliberately out of scope: this feature stays inside
    think.py) — so it is forwarded via the inherited environment instead:
    temporarily set ``REACHY_VOICE_ENGINE`` for the duration of the spawn call,
    restoring whatever value (or absence) was there before. A ``None`` *name* (no
    explicit flag) is a no-op — the spawned loop then resolves its engine from
    whatever ``REACHY_VOICE_ENGINE`` the operator already has set (or the "tts"
    default), unchanged from before this feature existed.
    """
    if name is None:
        yield
        return
    prior = os.environ.get(VOICE_ENGINE_ENV)
    os.environ[VOICE_ENGINE_ENV] = name
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(VOICE_ENGINE_ENV, None)
        else:
            os.environ[VOICE_ENGINE_ENV] = prior


class _NullProducer:
    """A producer that never originates a move — the queue is filled externally.

    ``think``'s expression moves are submitted onto the :class:`MotionQueue` from
    the cognition thread via :meth:`ExpressionProducer.express`. The motion
    executor (:func:`reachy.motion.server.run`) still owns *draining* that queue
    to the robot one move at a time, but it should originate nothing itself — so
    we hand it this no-op producer whose :meth:`update` always returns ``None``.
    The executor's serial-drain guarantee (never two moves at once) is what keeps
    the rare expression gestures soft and non-overlapping.
    """

    def update(self, *_a: object, **_kw: object) -> None:
        return None


class _MotionExecutor:
    """Background thread draining an expression queue to the robot, degrade-safe.

    Wraps :func:`reachy.motion.server.run` on its own thread, draining the shared
    :class:`MotionQueue` (which :attr:`producer` fills) to ``transport.move_goto``.
    A :class:`~reachy.cli._errors.CliError` inside the executor (e.g. the daemon
    went away mid-run) is captured, **not** raised on the cognition thread — motion
    degrades to silent while the cognition loop keeps thinking/speaking. The clean
    exit-2 for a missing ``[sdk]``/``[daemon]`` extra is raised *eagerly* at
    :meth:`start` (before the loop), so a missing extra is still a tidy CliError,
    not a traceback.
    """

    def __init__(self, transport: object) -> None:
        self.transport = transport
        self.queue = MotionQueue()
        self.producer = ExpressionProducer(queue=self.queue)
        self._stop = {"flag": False}
        self._thread: threading.Thread | None = None
        self._error: list[BaseException] = []

    def express(self, emoji: str) -> None:
        """Enqueue one calm gesture for *emoji* (drained by the executor thread)."""
        self.producer.express(emoji)

    def _drive(self) -> None:
        try:
            # No own stop handlers: the cognition loop owns SIGINT/SIGTERM. We
            # tolerate transient transport errors and only stop on the flag.
            run_motion(
                self.transport,
                _NullProducer(),
                queue=self.queue,
                stop=self._stop,
                max_errors=10**9,
            )
        # Degrade, never crash cognition: capture any transport error from this
        # background thread; the cognition loop owns SIGINT/SIGTERM, so letting
        # KeyboardInterrupt/SystemExit propagate (by catching Exception, not
        # BaseException) is correct here.
        except Exception as exc:  # noqa: BLE001
            self._error.append(exc)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._drive, name="reachy-think-motion", daemon=True)
        self._thread.start()

    def drain(self) -> None:
        """Flush any pending moves the executor hasn't issued yet (best effort).

        On a bounded/clean stop the executor thread may not have serviced the last
        enqueued gesture before the stop flag flips, so we issue whatever remains
        synchronously here. A transport error is swallowed — draining is best
        effort, and motion is degrade-safe by contract.
        """
        try:
            while True:
                action = self.queue.pop()
                if action is None:
                    return
                self.transport.move_goto(  # type: ignore[attr-defined]
                    head=action.head,
                    antennas=action.antennas,
                    body_yaw=action.body_yaw,
                    duration=action.duration,
                    interpolation=action.interpolation,
                )
        except CliError:
            return

    def stop(self) -> None:
        self._stop["flag"] = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.drain()


def _make_motion_executor(args: argparse.Namespace) -> _MotionExecutor:
    """Build the expression motion executor for the selected transport.

    Reuses :func:`get_transport` (the same transport flavor the sense feed uses):
    a missing ``[sdk]``/``[daemon]`` extra raises its clean exit-2 CliError here,
    before any loop work, mirroring the rest of ``think``.
    """
    transport = get_transport(args)
    return _MotionExecutor(transport)


def _emit_run_summary(turns: int, *, exporting: bool, json_mode: bool) -> None:
    """Emit the end-of-run summary, honoring stdout purity when exporting.

    When exporting to stdout the summary must go to **stderr** so the JSONL feed
    on stdout stays uncontaminated by non-event text — even under ``--json``.
    """
    if exporting:
        emit_diagnostic(f"[think] stopped after {turns} spoken turn(s) (export: stdout)")
    elif json_mode:
        emit_result({"status": "ok", "turns": turns}, json_mode=True)
    else:
        emit_diagnostic(f"[think] stopped after {turns} spoken turn(s)")


def cmd_think_run(args: argparse.Namespace) -> int:
    install_logging(getattr(args, "log_level", None))
    json_mode = bool(getattr(args, "json", False))
    buffer = EventBuffer(maxlen=_DEFAULT_MAXLEN)
    feed = _make_sense_feed(args, buffer)
    motion = _make_motion_executor(args)
    turn_interval = _resolve_turn_interval(args)
    mute_after = _resolve_mute_after(args)
    engine_choice = _resolve_engine(args)
    synthesize_fn = _synthesize_for(engine_choice)
    tts_kwargs = _tts_kwargs_for(engine_choice, args)
    playback_kwargs = _playback_kwargs_for(engine_choice, args)

    # Self-mute guard against the audio feedback loop: while the robot is speaking
    # and for `mute_after` seconds after, suppress sense cues so think never reacts
    # to its own voice (mic + speaker share one device). `play_audio` stamps the
    # window forward on every clip; the feed drops anything captured inside it. The
    # window covers the clip's own play duration (see _mute_window) computed at the
    # ACTIVE engine's samplerate — not a hardcoded TTS assumption — so a harmonic
    # clip's (16 kHz) duration is never mis-measured against tts's 24 kHz rate.
    mute = {"until": 0.0}

    def _guarded_play(pcm: bytes, **kwargs: object) -> None:
        _play_audio(pcm, **kwargs)
        if mute_after > 0:
            mute["until"] = _mute_window(
                pcm, engine_choice.samplerate, mute_after, now=time.monotonic()
            )

    def _guarded_feed() -> None:
        if time.monotonic() < mute["until"]:
            buffer.snapshot()  # discard the robot's own speech the mic just caught
            return
        feed()

    _guarded_feed.close = getattr(feed, "close", None)  # type: ignore[attr-defined]

    system_prompt = _build_system_prompt(emojis=_expression_emojis())

    # Export sink (None unless --export -); see reachy.cli._export.build_export_hook.
    export_hook = build_export_hook(args)

    engine = CognitionEngine(
        buffer=buffer,
        stream_sentences=_stream_sentences,
        synthesize=synthesize_fn,
        play_audio=_guarded_play,
        express=motion.express,
        export=export_hook,
        system_prompt=system_prompt,
        llm_kwargs=_llm_kwargs(args),
        tts_kwargs=tts_kwargs,
        playback_kwargs=playback_kwargs,
        turn_interval=turn_interval,
    )

    if not json_mode:
        emit_diagnostic(
            f"[think] thinking out loud via {getattr(args, 'transport', 'sdk')}; "
            f"voice engine: {engine_choice.name}; turn-interval={turn_interval:g}s "
            f"mute-after-speak={mute_after:g}s; Ctrl-C to stop"
        )

    # --max-ticks bounds the loop by *iterations* (idle turns included); --max-turns
    # bounds it by *spoken* turns. Either (or both) makes a run terminate for tests/ops.
    max_turns = getattr(args, "max_turns", None)
    stop = _tick_stop(getattr(args, "max_ticks", None))

    # Sidecar write/clear brackets the whole run so `think status --json` can report
    # which engine a running loop uses (null once the run has exited, for any reason).
    _write_voice_sidecar(engine_choice.name)
    try:
        # cognition_active() publishes the file flag on enter and clears it on exit —
        # on a clean stop, max-ticks/turns, Ctrl-C, OR an exception (its finally runs).
        # The motion executor drains the expression queue to the robot in parallel.
        with cognition_signal.cognition_active():
            motion.start()
            try:
                turns = engine.run(max_turns=max_turns, stop=stop, before_turn=_guarded_feed)
            finally:
                # Stop the motion executor thread, then close the SDK media session
                # (stops the mic recorder) on every exit path. Fakes/http feeds carry
                # no closer.
                motion.stop()
                close = getattr(_guarded_feed, "close", None)
                if close is not None:
                    close()
    finally:
        _clear_voice_sidecar()

    _emit_run_summary(turns, exporting=export_hook is not None, json_mode=json_mode)
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
    # An explicit --voice-engine is forwarded to the spawned `think run` via the
    # inherited environment (see _voice_engine_env's docstring) — supervisor.start's
    # build_run_command has no voice_engine parameter and stays untouched.
    with _voice_engine_env(getattr(args, "voice_engine", None)):
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
    # See cmd_think_start: the flag is forwarded via the environment, not build_run_command.
    with _voice_engine_env(getattr(args, "voice_engine", None)):
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
    data = dict(supervisor.status())
    # voice_engine is only meaningful while a tracked loop is actually alive — a
    # stale/absent sidecar (or a stopped loop) always reports None, never a leftover
    # name from a previous run.
    data["voice_engine"] = _read_voice_sidecar() if data.get("process") == "running" else None
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_think_overview(args)


# --- demo (scripted expression stream) ------------------------------------

#: The scripted stream used by ``think demo`` — a short *emoji* / "speech" sequence
#: that exercises the full marker→ExpressionProducer + TTS path so a human can
#: observe the wiring on a live robot without an LLM.
DEMO_SCRIPT: str = (
    '*🤔* "I wonder what that sound was." '
    '*👂* "There it is again, to my left." '
    '*🙂* "Ah — it\'s just the fan."'
)


def _demo_speak(args: argparse.Namespace, text: str) -> None:
    """Synthesize ``text`` and play it, honoring the demo's voice-engine/TTS/playback flags.

    Mirrors ``cmd_think_run``'s per-engine wiring: the tts engine keeps its existing
    ``--tts-url``/``--voice`` flags and unset playback samplerate (byte-identical to
    before this feature existed); the harmonic engine takes no tts kwargs and pins
    playback to its native 16 kHz samplerate.
    """
    engine_choice = _resolve_engine(args)
    synthesize_fn = _synthesize_for(engine_choice)
    pcm = synthesize_fn(text, **_tts_kwargs_for(engine_choice, args))
    if pcm:
        _play_audio(pcm, **_playback_kwargs_for(engine_choice, args))


def cmd_think_demo(args: argparse.Namespace) -> int:
    """Run a scripted ``*emoji* "speech"`` stream through the real pipeline.

    Drives :data:`DEMO_SCRIPT` (or ``--script TEXT``) through
    :class:`~reachy.speech.markers.MarkerParser` →
    :class:`~reachy.motion.expression.ExpressionProducer` (enqueue moves) +
    TTS (speak quoted text), with the cognition signal active, so a co-running
    ``listen`` idles while the demo plays.  Exits when the scripted stream is
    exhausted.

    Use this to verify ``think``'s body-expression wiring on a live robot
    without a running LLM.  See
    ``docs/verification/think-body-expression.md`` for the manual checklist.
    """
    json_mode = bool(getattr(args, "json", False))
    script = getattr(args, "script", None) or DEMO_SCRIPT

    events = _parse_marker_script(script)
    motion = _make_motion_executor(args)

    expressed: list[str] = []
    spoken: list[str] = []

    with cognition_signal.cognition_active():
        motion.start()
        try:
            for event in events:
                if isinstance(event, MarkerEvent):
                    motion.express(event.emoji)
                    expressed.append(event.emoji)
                elif isinstance(event, SpeechEvent):
                    _demo_speak(args, event.text)
                    spoken.append(event.text)
        finally:
            motion.stop()

    if json_mode:
        emit_result(
            {"status": "ok", "expressed": expressed, "spoken": spoken},
            json_mode=True,
        )
    else:
        emit_diagnostic(
            f"[think demo] done — expressed {len(expressed)} gesture(s), "
            f"spoke {len(spoken)} phrase(s)"
        )
    return 0


def _register_demo(noun_sub: argparse._SubParsersAction) -> None:
    demo = noun_sub.add_parser(
        "demo",
        help="Run a scripted expression stream on the robot (hardware verification).",
    )
    # add_robot_args provides --json, --transport, --base-url, --timeout.
    add_robot_args(demo)
    # Override the transport default to sdk (think's default transport).
    demo.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    demo.add_argument(
        "--script",
        default=None,
        help=(
            "Override the built-in scripted stream with custom text "
            '(same *emoji* / "speech" format).  Default: built-in 3-marker sequence.'
        ),
    )
    demo.add_argument(
        "--tts-url",
        default=None,
        dest="tts_url",
        help="TTS base URL (overrides REACHY_TTS_URL).",
    )
    demo.add_argument(
        "--voice",
        default=None,
        help="TTS voice identifier (overrides REACHY_TTS_VOICE).",
    )
    demo.add_argument(
        "--voice-engine",
        choices=_VOICE_ENGINE_CHOICES,
        default=None,
        dest="voice_engine",
        help="Voice engine to speak through (overrides REACHY_VOICE_ENGINE; default: tts).",
    )
    demo.set_defaults(func=cmd_think_demo)


# --- shared cognition args ------------------------------------------------


def _add_cognition_args(parser: argparse.ArgumentParser) -> None:
    """LLM / TTS endpoint + pacing knobs shared by run / start / restart."""
    parser.add_argument(
        "--llm-base-url",
        default=None,
        dest="llm_base_url",
        help="LLM base URL (overrides REACHY_OPENAI_URL_BASE).",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        dest="llm_model",
        help="LLM model name (overrides REACHY_OPENAI_MODEL_ID).",
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
        "--voice-engine",
        choices=_VOICE_ENGINE_CHOICES,
        default=None,
        dest="voice_engine",
        help="Voice engine to speak through (overrides REACHY_VOICE_ENGINE; default: tts). "
        "On 'start'/'restart' this is forwarded to the spawned loop via the environment.",
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
    add_export_args(run)
    add_log_level_arg(run)
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


def _register_expressions(noun_sub: argparse._SubParsersAction) -> None:
    """The ``think expressions`` sub-noun: list + check the expression catalog.

    A noun with action-verbs must also expose ``overview`` (rubric requirement),
    so ``expressions`` carries one alongside ``list`` / ``check``. ``parser_class``
    propagates so nested parse errors keep the structured error contract.
    """
    ex = noun_sub.add_parser(
        "expressions",
        help="List/check the expression catalog (see 'think expressions overview').",
    )
    ex.add_argument("--json", action="store_true", help=_JSON_HELP)
    ex.set_defaults(func=_expressions_no_verb, json=False)
    ex_sub = ex.add_subparsers(dest="expressions_command", parser_class=type(ex))

    ov = ex_sub.add_parser("overview", help="Describe the expressions sub-noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_expressions_overview)

    ls = ex_sub.add_parser("list", help="List the expression catalog.")
    ls.add_argument("--json", action="store_true", help=_JSON_HELP)
    ls.set_defaults(func=cmd_expressions_list)

    ck = ex_sub.add_parser("check", help="Flag catalog poses too similar to be distinct.")
    ck.add_argument("--json", action="store_true", help=_JSON_HELP)
    ck.set_defaults(func=cmd_expressions_check)


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
    _register_expressions(noun_sub)
    _register_demo(noun_sub)
