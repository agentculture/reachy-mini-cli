"""``reachy-mini-cli listen`` — orient the head toward the direction of sound.

A sound-reactive loop: it reads the mic array's Direction of Arrival (DoA) from
the daemon and turns the head toward a *sustained, off-axis* sound, then holds
there before reconsidering, easing back to center after silence. Unlike the
behavior engine (which streams immediate ``set_target`` poses at 50 Hz), this
loop drives the robot with the daemon's smooth minjerk ``goto`` planner and runs
moves strictly one at a time through a serial motion queue — so reorienting turns
are soft and never conflict.

Three faces, like the ``daemon`` / ``demo-mode`` nouns:

* **run** — the foreground loop (what ``start`` / the process launch run);
* **start** / **stop** / **restart** — manage it as a tracked background process
  (PID + log under the state dir);
* **status** — loop + daemon reachability.

It degrades gracefully: no mic / no daemon DoA ⇒ no reaction, no crash. The loop
drives the robot through the shared transport, so it needs a running daemon —
bring one up with ``reachy daemon start``.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
from typing import Callable

import numpy as np

from reachy.behavior.sense import DOA_TIMEOUT, DoaPoller, Sense, read_doa
from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.cli._export import add_export_args, build_export_hook
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.motion import supervisor
from reachy.motion.listen import ListenParams, ListenProducer, SampleHolder
from reachy.motion.listen_hooks import HookChain
from reachy.motion.listen_pat import PatHook
from reachy.motion.listen_sleep import SleepHook
from reachy.motion.listen_think import ThinkHook
from reachy.motion.listen_transcribe import TranscribeHook
from reachy.motion.listen_vision import VisionHook
from reachy.motion.pat import PatDetector
from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SenseSample
from reachy.motion.server import LoopHooks
from reachy.motion.server import run as run_loop
from reachy.motion.snap import SnapDetector
from reachy.robot import add_robot_args, get_transport
from reachy.speech.voice import VoiceEngine, resolve_voice_engine

logger = logging.getLogger(__name__)

_JSON_HELP = "Emit structured JSON."
_CENTER = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}

# Self-mute window (seconds) after a spoken clip during which the TranscribeHook
# discards captured audio, so the robot never transcribes its own TTS through the
# shared USB audio device. Re-declares ``think``'s documented default
# (``think._DEFAULT_MUTE_AFTER_SPEAK``) rather than importing it, keeping this
# module free of a cross-command import that would pull the cognition stack at
# import time; the two are intended to agree.
_DEFAULT_MUTE_AFTER_SPEAK = 2.5

# --cognition: which folded-live cognition engine drives thinking. ``marker`` is
# the established ``*emoji*``/``"speech"`` marker path (CognitionEngine); ``agent``
# swaps in the tool-use AgentTurnEngine (speak/harmonics/apply_pose as LLM tool
# calls) behind the SAME folded ThinkHook seam. Mirrors REACHY_VOICE_ENGINE.
COGNITION_ENV = "REACHY_COGNITION"
DEFAULT_COGNITION = "marker"
_COGNITION_CHOICES = ("marker", "agent")

_VERBS = [
    "listen run — run the sound-orienting loop in the foreground",
    "listen start — start the loop in the background (tracked process)",
    "listen stop — stop the loop this CLI started (eases robot to center)",
    "listen restart — restart the background loop (re-reads tuning + code)",
    "listen status — loop process state + daemon reachability",
    "listen overview — this summary",
]


# --- shared args ----------------------------------------------------------


def _add_tuning_args(parser: argparse.ArgumentParser) -> None:
    """Listen feel knobs (degrees / seconds / deg-per-second); unset ⇒ built-in default."""
    d = ListenParams()
    parser.add_argument("--gain", type=float, default=None, help="head-yaw gain per DoA angle.")
    parser.add_argument(
        "--max-yaw",
        type=float,
        default=None,
        dest="max_yaw",
        help=f"max head yaw toward sound (deg, default {d.max_yaw:g}).",
    )
    parser.add_argument(
        "--deadband",
        type=float,
        default=None,
        help=f"ignore sound within this of the current heading (deg, default {d.deadband:g}).",
    )
    parser.add_argument(
        "--dwell",
        type=float,
        default=None,
        help=f"a direction must persist this long before turning (s, default {d.dwell:g}).",
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=None,
        help=f"after turning, stay this long before reconsidering (s, default {d.hold:g}).",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help=f"turn/return slew speed (deg/s, default {d.alert_speed:g}).",
    )
    parser.add_argument(
        "--recenter-after",
        type=float,
        default=None,
        dest="recenter_after",
        help="silence grace before the head/body start drifting home "
        f"(s, default {d.recenter_after:g}).",
    )
    parser.add_argument(
        "--idle-energy",
        type=float,
        default=None,
        dest="idle_energy",
        help="liveliness of the always-alive idle motion; 0 holds still between sounds "
        f"(default {d.idle_energy:g}).",
    )
    parser.add_argument(
        "--drift-speed",
        type=float,
        default=None,
        dest="drift_speed",
        help="speed the head/body drift home after silence (deg/s, " f"default {d.drift_speed:g}).",
    )
    parser.add_argument(
        "--speech-only",
        action="store_true",
        dest="speech_only",
        help="react only to detected speech (default: any sound).",
    )
    parser.add_argument(
        "--antenna-gain",
        type=float,
        default=None,
        dest="antenna_gain",
        help=f"scales Tier-1 antenna lean magnitude (default {d.antenna_gain:g}).",
    )
    parser.add_argument(
        "--antenna-max",
        type=float,
        default=None,
        dest="antenna_max",
        help=f"maximum near-side antenna deflection (deg, default {d.antenna_max:g}).",
    )
    parser.add_argument(
        "--body-yaw-max",
        type=float,
        default=None,
        dest="body_yaw_max",
        help=f"max body yaw for Tier-2 head/body escalation (deg, default {d.body_yaw_max:g}).",
    )
    parser.add_argument(
        "--body-speed",
        type=float,
        default=None,
        dest="body_speed",
        help=f"body turn slew speed for Tier-2 escalation (deg/s, default {d.body_speed:g}).",
    )
    parser.add_argument(
        "--head-only-band",
        type=float,
        default=None,
        dest="head_only_band",
        help=f"|desired| <= this uses head-only; beyond triggers body escalation "
        f"(deg, default {d.head_only_band:g}).",
    )
    parser.add_argument(
        "--snap-ratio",
        type=float,
        default=None,
        dest="snap_ratio",
        help="RMS snap detector: loudness ratio over rolling average to fire (default 5.0).",
    )
    parser.add_argument(
        "--snap-floor",
        type=float,
        default=None,
        dest="snap_floor",
        help="RMS snap detector: absolute RMS floor below which chunks are ignored (default 0.02).",
    )


def _add_pat_args(parser: argparse.ArgumentParser) -> None:
    """Head-pat detection toggle + tuning (SDK transport only; on by default).

    ``--pat`` / ``--no-pat`` fold proprioceptive head-pat detection into the SDK
    loop (the loop owns the single SDK client, so its head-pose read-backs are
    fast enough to detect a pat). The tuning knobs mirror the standalone ``pat``
    noun; unset ⇒ the detector's built-in default.
    """
    parser.add_argument(
        "--pat",
        action="store_true",
        dest="pat",
        default=True,
        help="detect head pats inside the sdk loop and lean into them (default: on).",
    )
    parser.add_argument(
        "--no-pat",
        action="store_false",
        dest="pat",
        help="do not detect head pats (sound-orienting only).",
    )
    parser.add_argument(
        "--press-threshold",
        type=float,
        default=None,
        dest="press_threshold",
        help="pat: pitch deviation (deg) past which a head-press counts (default 1.2).",
    )
    parser.add_argument(
        "--min-presses",
        type=int,
        default=None,
        dest="min_presses",
        help="pat: presses within the window needed to trigger a pat (default 2).",
    )


def _add_live_arg(parser: argparse.ArgumentParser) -> None:
    """The ``--live`` opt-in: fold ALL the senses into the one listen loop.

    Off by default — bare ``listen run`` is exactly as today (sound-orient + the
    single head-pat hook). With ``--live`` the loop additionally composes the
    ``think`` cognition trigger, ``vision`` motion/light detection, and the
    ``sleep`` decay→wake state machine — all four sense hooks ride the ONE SDK
    media session and the ONE motion queue, arbitrated by the idle-interrupt
    priority ``sleep > pat > think``. This is the "live mode" the boot service
    runs. SDK transport only (the http profile has no audio/camera/pose).
    """
    parser.add_argument(
        "--live",
        action="store_true",
        dest="live",
        default=False,
        help="fold think + vision + sleep into the loop alongside sound-orient + pat "
        "(sdk only; the mode the boot service runs).",
    )
    parser.add_argument(
        "--transcribe",
        action="store_true",
        dest="transcribe",
        default=False,
        help="transcribe nearby speech (STT) and feed the WORDS into live cognition "
        "(requires --live + sdk; off by default; the robot never transcribes its own "
        "voice — a self-mute window after each spoken clip drops its own audio).",
    )


def _add_voice_engine_arg(parser: argparse.ArgumentParser) -> None:
    """The ``--voice-engine`` opt-in: pick the folded live cognition's speech backend.

    Selects between ``"tts"`` (default — the external Chatterbox HTTP speech engine,
    :mod:`reachy.speech.tts`) and ``"harmonic"`` (in-process, fully offline melodic
    gesture voice, :mod:`reachy.speech.harmonic`). The choice only matters inside the
    folded live cognition loop, so — mirroring ``--export``/``--transcribe`` — it is
    honoured ONLY with ``--live``; a bare ``--voice-engine`` is a clean exit-1 error
    (see :func:`_resolve_voice_engine`). Left unset (``None``), it defers to
    :func:`reachy.speech.voice.resolve_voice_engine`'s own fallback (the
    ``REACHY_VOICE_ENGINE`` env var, then ``"tts"``), so a bare ``listen run --live``
    with no flag stays behaviourally identical to before this feature.
    """
    parser.add_argument(
        "--voice-engine",
        choices=("tts", "harmonic"),
        default=None,
        dest="voice_engine",
        help="folded live cognition speech backend: 'tts' (default; Chatterbox HTTP) "
        "or 'harmonic' (in-process melodic gesture, fully offline); overrides "
        "REACHY_VOICE_ENGINE (requires --live). In --cognition agent mode BOTH voices "
        "are always available as tools, so this flag only affects --cognition marker.",
    )


def _add_cognition_arg(parser: argparse.ArgumentParser) -> None:
    """The ``--cognition`` opt-in: pick the folded live cognition engine.

    Selects between ``"marker"`` (default — the established ``*emoji*`` / ``"speech"``
    marker path, :class:`~reachy.speech.cognition.CognitionEngine`) and ``"agent"``
    (the tool-use engine, :class:`~reachy.speech.agent_turn.AgentTurnEngine`, which
    acts through ``speak`` / ``harmonics`` / ``apply_pose`` LLM tool calls). The two
    engines share the SAME folded :class:`~reachy.motion.listen_think.ThinkHook` seam,
    the SAME shared :class:`~reachy.speech.events.EventBuffer`, and the SAME export
    feed — so ``agent`` is a drop-in behind the seam with no new process and no second
    media session. Mirroring ``--voice-engine`` / ``--transcribe`` / ``--export``, it
    is honoured ONLY with ``--live``; a bare ``--cognition`` is a clean exit-1 error
    (see :func:`_resolve_cognition`). Left unset (``None``), it defers to
    ``REACHY_COGNITION`` then ``"marker"``.

    Interplay with ``--voice-engine``: in ``agent`` mode the tool registry always
    exposes BOTH the ``tts`` and ``harmonic`` voices as separate tools, so the agent
    picks per utterance — ``--voice-engine`` there is inert (it keeps controlling only
    the ``marker`` engine's single speech backend).
    """
    parser.add_argument(
        "--cognition",
        choices=_COGNITION_CHOICES,
        default=None,
        dest="cognition",
        help="folded live cognition engine: 'marker' (default; the *emoji*/\"speech\" "
        "marker path) or 'agent' (the tool-use engine — speak/harmonics/apply_pose as "
        "LLM tool calls); overrides REACHY_COGNITION (requires --live). In 'agent' mode "
        "BOTH voices are always available as tools, so --voice-engine affects only "
        "'marker' mode.",
    )


# 1:1 ``(arg attr, ListenParams attr)`` flags: an unset CLI flag (``None``) keeps
# the param's default. The genuinely special cases (--speed sets two fields,
# --speech-only is a bool flag, --pat is a default-True toggle) are handled apart.
_SIMPLE_PARAM_MAP: tuple[tuple[str, str], ...] = (
    ("gain", "gain"),
    ("max_yaw", "max_yaw"),
    ("deadband", "deadband"),
    ("dwell", "dwell"),
    ("hold", "hold"),
    ("recenter_after", "recenter_after"),
    ("idle_energy", "idle_energy"),
    ("drift_speed", "drift_speed"),
    ("antenna_gain", "antenna_gain"),
    ("antenna_max", "antenna_max"),
    ("body_yaw_max", "body_yaw_max"),
    ("body_speed", "body_speed"),
    ("head_only_band", "head_only_band"),
)


def _params_from_args(args: argparse.Namespace) -> ListenParams:
    """A :class:`ListenParams` from CLI flags (each unset flag keeps its default)."""
    p = ListenParams()
    for arg_name, attr in _SIMPLE_PARAM_MAP:
        value = getattr(args, arg_name, None)
        if value is not None:
            setattr(p, attr, value)
    # Special cases: --speed drives both slew speeds; --speech-only / --no-pat are
    # bool toggles, not value flags.
    if getattr(args, "speed", None) is not None:
        p.alert_speed = p.relax_speed = args.speed
    if getattr(args, "speech_only", False):
        p.speech_only = True
    if getattr(args, "pat", True) is False:
        p.pat = False
    return p


# --- overview -------------------------------------------------------------


def cmd_listen_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "A two-tier sound-reactive loop using real mic-array DoA + RMS loudness "
                "(SDK-first by default).",
                "Tier-1 (near-side antenna lean): on any live sound that does not trigger a "
                "head turn, the antenna facing the source deflects gently toward it — "
                "a subtle 'I hear you' cue.",
                "Tier-2 (head→body 'turn to see'): on detected speech OR a loud RMS snap "
                "transient, the head turns toward the source; when the angle exceeds "
                "head-only-band the body rotates too (head re-centres on the residual) "
                "so the whole robot faces the sound.",
                "Always-alive idle: between sounds the robot keeps gently moving "
                "(breathing, slow gaze wander, antenna sway) around its current heading — "
                "if it turned toward a sound it stays rotated and keeps moving there, "
                "then drifts slowly back to front after silence (never frozen, never a "
                "hard snap). Tune with --idle-energy / --drift-speed (--idle-energy 0 "
                "restores hold-still).",
                "Head pats too (sdk only): the loop reads the head pose back in-process "
                "each tick, so a downward press or sideways nudge is detected as a pat and "
                "the robot leans into it (lean→nuzzle→settle) while still reacting to sound. "
                "On by default; --no-pat turns it off.",
                "Smooth by construction — drives the daemon's minjerk 'goto' planner, "
                "one move at a time through a serial motion queue (no jerky streaming).",
                "Graceful: no mic / no daemon DoA ⇒ no reaction, no crash.",
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
                "SDK-first by default: real DoA + mic loudness in-process; "
                "use --transport http for the remote/daemon profile",
                "Tier-1 knobs: --antenna-gain / --antenna-max",
                "Tier-2 knobs: --head-only-band / --body-yaw-max / --body-speed",
                "idle knobs: --idle-energy / --drift-speed / --recenter-after",
                "feel knobs: --dwell / --hold / --speed / --deadband / --gain",
                "head-pat (sdk only): --pat / --no-pat (default on), "
                "--press-threshold / --min-presses",
                "snap detector: --snap-ratio / --snap-floor (SDK profile only)",
                "exit codes: 0 ok, 1 user error, 2 environment (daemon unreachable)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli listen",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


# --- run (foreground loop) ------------------------------------------------


def _build_pat_hook(args: argparse.Namespace, transport: object, queue) -> PatHook | None:
    """A :class:`PatHook` bound to the loop's queue, or ``None`` when pat is off.

    Pat detection is only meaningful on the SDK transport (``head_pose`` is an
    SDK-only read-back) and is on by default; ``--no-pat`` (``args.pat`` False)
    suppresses it, as does a transport that cannot read the head pose back. The
    hook reads the head pose back each tick *inside* the loop that owns the single
    SDK client, so the read-backs are fast enough to detect a pat — a separate
    ``pat`` process would be throttled by SDK contention.
    """
    if not getattr(args, "pat", True):
        return None
    if not hasattr(transport, "head_pose"):
        return None
    kw: dict[str, float] = {}
    if getattr(args, "press_threshold", None) is not None:
        kw["press_threshold"] = args.press_threshold
    if getattr(args, "min_presses", None) is not None:
        kw["min_presses"] = args.min_presses
    detector = PatDetector(**kw) if kw else None
    return PatHook(queue, detector=detector)


def _build_think_hook(
    provider: Callable[[], SenseSample | None],
    *,
    export: object | None = None,
    buffer: object | None = None,
    play_audio: object | None = None,
    feed_doa_cues: bool = True,
    voice_engine: VoiceEngine | None = None,
) -> ThinkHook | None:
    """A :class:`ThinkHook` driving cognition from the shared sample, or ``None``.

    Builds a real :class:`~reachy.speech.cognition.CognitionEngine` over a shared
    :class:`~reachy.speech.events.EventBuffer` and wires that *same* buffer into the
    hook (the engine stores its buffer privately and does not expose it, so the
    composition layer must pass it explicitly — see ``listen_think.ThinkHook``).
    Construction is wrapped: if the cognition stack can't be assembled (e.g. the LLM
    env isn't configured), we log once and return ``None`` so ``--live`` still runs
    the other three senses — the loop must never die because cognition is absent.

    The folded-live engine is built ``audio_optional=True``: a TTS/playback outage
    degrades to "no speech" instead of killing the cognition worker (the bug where a
    wedged TTS endpoint silently took down ``listen --live``'s thinking). When
    ``export`` is an :class:`~reachy.export.exporter.ExportHook`, the engine also
    emits the ``thinking`` / ``message`` / ``emotion`` JSONL feed — so the live loop
    streams what the robot is thinking to any subscriber (a reTerminal panel, a log,
    an audio renderer) over the one documented wire contract.

    ``buffer`` lets the composition layer pass the *shared* cognition event buffer
    so the optional :class:`~reachy.motion.listen_transcribe.TranscribeHook` can feed
    transcribed words into the *same* buffer the engine consumes (``--transcribe``);
    when ``None`` a fresh buffer is created internally. ``play_audio`` lets the layer
    inject a self-mute-stamping playback wrapper so the engine and the transcribe
    hook's mute window agree; when ``None`` the engine's default playback is used.

    ``voice_engine`` is the resolved :class:`~reachy.speech.voice.VoiceEngine`
    (``--voice-engine`` / ``REACHY_VOICE_ENGINE``, see :func:`_resolve_voice_engine`).
    When given, its ``synthesize`` callable and an empty ``tts_kwargs`` are passed to
    the engine explicitly — for the default ``"tts"`` engine this is the exact same
    function object :class:`~reachy.speech.cognition.CognitionEngine` already defaults
    to, so passing it is behaviourally byte-identical; for ``"harmonic"`` it swaps in
    :func:`reachy.speech.harmonic.synthesize` with zero other engine changes. ``None``
    (the default; only reachable from a direct test call, never from the CLI, which
    always resolves an engine) skips the override entirely.
    """
    try:
        # Imported lazily so a bare (no-LLM) live run, or a box without the speech
        # deps configured, doesn't pull the cognition stack at module import time.
        from reachy.speech.cognition import CognitionEngine
        from reachy.speech.events import EventBuffer

        buf = buffer if buffer is not None else EventBuffer()
        engine_kwargs: dict[str, object] = {
            "buffer": buf,
            "export": export,
            "audio_optional": True,
        }
        if play_audio is not None:
            engine_kwargs["play_audio"] = play_audio
        if voice_engine is not None:
            engine_kwargs["synthesize"] = voice_engine.synthesize
            engine_kwargs["tts_kwargs"] = {}
        engine = CognitionEngine(**engine_kwargs)
        return ThinkHook(provider, engine=engine, buffer=buf, feed_doa_cues=feed_doa_cues)
    except Exception:  # noqa: BLE001
        logger.warning(
            "listen --live: cognition engine unavailable; think fold-in disabled", exc_info=True
        )
        return None


def _build_agent_think_hook(
    provider: Callable[[], SenseSample | None],
    queue: MotionQueue,
    *,
    export: object | None = None,
    buffer: object | None = None,
    play_audio: object | None = None,
    feed_doa_cues: bool = True,
) -> ThinkHook | None:
    """A :class:`ThinkHook` driving the tool-use agent engine, or ``None``.

    The ``--cognition agent`` counterpart of :func:`_build_think_hook`: it builds a
    :class:`~reachy.speech.agent_turn.AgentTurnEngine` (which exposes the *same*
    ``.buffer`` / ``run(stop=...)`` surface as :class:`CognitionEngine`, so the
    folded :class:`~reachy.motion.listen_think.ThinkHook` drives it unchanged) over a
    :class:`~reachy.speech.tools.ToolRegistry` wired with the loop's REAL seams:

    * ``express`` -> an :class:`~reachy.motion.expression.ExpressionProducer` bound to
      the loop's ONE ``queue`` — so ``apply_pose`` tool calls enqueue on the SAME
      serial :class:`~reachy.motion.queue.MotionQueue` every other sense hook uses (no
      new motion channel);
    * ``speak_engine`` / ``harmonic_engine`` -> ``resolve_voice_engine("tts")`` /
      ``("harmonic")`` — BOTH voices are always available as tools regardless of
      ``--voice-engine`` (that flag only affects the marker engine);
    * ``play`` -> the injected ``play_audio`` seam, which is the SAME self-mute
      wrapper ``--transcribe`` uses today (stamps the mute window the TranscribeHook
      reads), so the robot never transcribes its own *tool*-spoken voice.

    Like the marker engine it is built ``audio_optional=True`` (a wedged TTS degrades
    to "no speech" instead of killing live thinking) and threads the SAME ``export``
    hook (shared :func:`reachy.cli._export.build_export_hook` builder). ``buffer`` is
    the shared cognition :class:`~reachy.speech.events.EventBuffer` (so a folded
    TranscribeHook feeds the SAME buffer the engine consumes); ``feed_doa_cues`` is
    threaded to the ThinkHook exactly as the marker path does (``False`` under
    ``--transcribe`` — words-only cognition). Construction is guarded: if the agent
    stack can't be assembled (e.g. no LLM env) we log once and return ``None`` so
    ``--live`` still runs the other senses.
    """
    try:
        # Imported lazily (like _build_think_hook) so a bare live run doesn't pull the
        # agent stack at module import time.
        from reachy.motion.expression import ExpressionProducer
        from reachy.speech.agent_turn import AgentTurnEngine
        from reachy.speech.events import EventBuffer
        from reachy.speech.tools import ToolRegistry
        from reachy.speech.voice import resolve_voice_engine

        buf = buffer if buffer is not None else EventBuffer()
        registry_kwargs: dict[str, object] = {
            # apply_pose enqueues on the loop's ONE MotionQueue (same serial queue the
            # other sense hooks drain), byte-for-byte the *emoji* marker path's action.
            "express": ExpressionProducer(queue=queue).express,
            # Both voices are tools side by side — --voice-engine does not gate them.
            "speak_engine": resolve_voice_engine("tts"),
            "harmonic_engine": resolve_voice_engine("harmonic"),
        }
        if play_audio is not None:
            # The SAME self-mute-stamping wrapper the marker engine + TranscribeHook
            # share, so a tool-spoken clip mutes the hook exactly like marker speech.
            registry_kwargs["play"] = play_audio
        registry = ToolRegistry(**registry_kwargs)
        engine = AgentTurnEngine(
            buffer=buf,
            registry=registry,
            export=export,
            audio_optional=True,
        )
        return ThinkHook(provider, engine=engine, buffer=buf, feed_doa_cues=feed_doa_cues)
    except Exception:  # noqa: BLE001
        logger.warning(
            "listen --live --cognition agent: agent engine unavailable; think fold-in disabled",
            exc_info=True,
        )
        return None


class _SessionBoundTransport:
    """Route the loop's per-tick pose / move / frame reads through the ONE open
    media session instead of opening a fresh ``ReachyMini`` per call.

    ``SdkTransport.head_pose`` / ``move_goto`` / ``get_frame`` each open a new SDK
    client per call, and the SDK's ``GStreamerAudio`` teardown leaks file
    descriptors — so at the loop's tick/move/frame rate they exhaust the process
    fd limit in minutes and crash-loop the service (issue #51). The loop already
    holds one open client via ``media_session()``; this proxy serves those three
    reads from it and delegates everything else to the base transport untouched.
    """

    def __init__(self, base: object, session: object) -> None:
        self._base = base
        self._session = session

    def _via(self, name: str):  # type: ignore[no-untyped-def]
        """Prefer the open session for *name*; fall back to the base transport.

        The production :class:`~reachy.robot.sdk_transport.MediaSession` serves
        all of ``head_pose``/``move_goto``/``get_frame``, so the real loop always
        rides the one open client (the issue-#51 fix). The fallback only matters
        for the HTTP profile / minimal fakes whose session does not expose them.
        """
        fn = getattr(self._session, name, None)
        return fn if callable(fn) else getattr(self._base, name)

    def head_pose(self) -> tuple[float, float]:
        return self._via("head_pose")()

    def move_goto(self, **kwargs: object) -> object:
        return self._via("move_goto")(**kwargs)

    def get_frame(self) -> object:
        return self._via("get_frame")()

    def __getattr__(self, name: str) -> object:
        return getattr(self._base, name)


def _build_engagement_classifier() -> object | None:
    """Build the LLM engagement classifier for the ``--transcribe`` gate, or ``None``.

    The classifier judges *addressed-to-the-robot* vs *ambient* speech (issue #55).
    It is constructed with the SAME LLM endpoint config the folded
    :class:`~reachy.speech.cognition.CognitionEngine` uses: both leave
    ``base_url`` / ``model`` / ``api_key`` unset so the underlying
    :func:`reachy.speech.llm.complete` resolves the one ``REACHY_OPENAI_*`` endpoint
    (the cognition engine resolves the *same* env via :func:`reachy.speech.llm`),
    so the gate and cognition always hit the same backend — no separate config and
    no new remote API surface. Construction does **no** network I/O.

    Imported lazily (like :func:`_build_think_hook`) so a bare live run without the
    speech stack configured doesn't pull the cognition modules at import time, and a
    construction fault degrades to ``None`` (the gate then stays on the pure
    :meth:`~reachy.motion.listen_transcribe.TranscribeHook._should_engage` heuristic).

    When the ``REACHY_ENGAGE_HEURISTIC`` escape hatch is truthy the hook ignores any
    injected classifier, so we skip building one entirely — saving the import and
    keeping the path identical to the un-injected heuristic.
    """
    from reachy.motion.listen_transcribe import _env_truthy  # local: stdlib-only helper

    if _env_truthy(os.environ.get("REACHY_ENGAGE_HEURISTIC")):
        return None
    try:
        from reachy.speech.engagement import EngagementClassifier

        # No base_url/model/api_key overrides → llm.complete resolves the same
        # REACHY_OPENAI_* env the CognitionEngine's LLM client resolves.
        return EngagementClassifier()
    except Exception:  # noqa: BLE001 — a build fault must not disable hearing
        logger.warning(
            "listen --live --transcribe: engagement classifier unavailable; "
            "gate stays on the heuristic",
            exc_info=True,
        )
        return None


def _build_transcribe_hook(
    provider: Callable[[], SenseSample | None],
    *,
    buffer: object,
    mute_until: Callable[[], float],
    sample_rate: int | None = None,
    classifier: object | None = None,
    on_engage: Callable[[], None] | None = None,
) -> TranscribeHook:
    """A :class:`TranscribeHook` feeding STT words into the shared cognition buffer.

    Wired to the *same* :class:`~reachy.speech.events.EventBuffer` the
    :class:`~reachy.speech.cognition.CognitionEngine` consumes (so transcribed words
    become cognition cues) and a real :class:`~reachy.speech.stt.Transcriber` (no
    network I/O at construction). ``mute_until`` reads the shared self-mute window
    the playback wrapper stamps, so the robot never transcribes its own TTS.
    ``sample_rate`` is the REAL mic rate from the SDK session (``session.samplerate``)
    so the WAV sent to STT is labelled correctly — a wrong rate makes STT return
    nothing (the gap live-testing exposed); ``None`` falls back to the 16 kHz default.

    ``classifier`` is the optional :class:`~reachy.speech.engagement.EngagementClassifier`
    that runs the addressed-vs-ambient LLM gate (``None`` keeps the pure heuristic).
    ``on_engage`` is the motion-ladder signal fired exactly when the gate ENGAGES —
    the composition layer wires it to ``ListenProducer.set_engaged`` so an addressed
    utterance latches one deliberate turn toward the speaker.
    """
    return TranscribeHook(
        provider,
        buffer=buffer,
        mute_until=mute_until,
        sample_rate=sample_rate,
        classifier=classifier,
        on_engage=on_engage,
    )


def _build_live_hooks(
    transport: object,
    queue: MotionQueue,
    provider: Callable[[], SenseSample | None],
    pat_hook: PatHook | None,
    *,
    export: object | None = None,
    transcribe: bool = False,
    sample_rate: int | None = None,
    clock: Callable[[], float] | None = None,
    producer: object | None = None,
    voice_engine: VoiceEngine | None = None,
    cognition: str = DEFAULT_COGNITION,
) -> list[object]:
    """Build the ``--live`` sense hooks in ``sleep > pat > think`` priority order.

    The flag-arbitrated three lead in descending idle-interrupt priority (sleep
    yields the head entirely, pat pauses the idle wander, think drops to a focused
    breathe), then vision rides last (it competes for nothing the flags arbitrate).
    All four share the loop's one ``queue`` and the one shared-sample ``provider``;
    none opens its own audio/camera/pose — they ride the single SDK client the loop
    owns. A hook whose optional stack is unavailable is simply omitted. The list is
    handed to a :class:`~reachy.motion.listen_hooks.HookChain` as the loop's single
    ``on_tick``. ``export`` (an :class:`~reachy.export.exporter.ExportHook` or
    ``None``) is threaded into the think hook's engine to stream the cognition feed.

    ``transcribe`` (the ``--transcribe`` opt-in) additionally composes a
    :class:`~reachy.motion.listen_transcribe.TranscribeHook` that transcribes the
    loop's shared per-tick audio and feeds the recognised **words** into the SAME
    :class:`~reachy.speech.events.EventBuffer` the cognition engine consumes — so the
    composition layer creates one buffer here and wires it into both the engine (via
    :func:`_build_think_hook`) and the transcribe hook. It also creates the shared
    self-mute window: a ``play_audio`` wrapper stamps ``mute["until"]`` after every
    spoken clip and the transcribe hook's ``mute_until`` reads it, so the robot never
    transcribes its own voice. If cognition is unavailable (no LLM env →
    :func:`_build_think_hook` returns ``None``) there is no buffer to feed, so the
    transcribe hook is skipped (logged once) and the loop still runs the rest.

    Under ``transcribe`` the transcribe hook is also given (a) an
    :class:`~reachy.speech.engagement.EngagementClassifier` (the LLM addressed-vs-ambient
    gate, built with the SAME ``REACHY_OPENAI_*`` endpoint config as cognition — see
    :func:`_build_engagement_classifier`) and (b) ``on_engage=producer.set_engaged``,
    the motion-ladder signal. The result: an addressed/named utterance latches exactly
    one deliberate turn toward the speaker's DoA (via the producer's one-shot engaged
    latch) while ambient/dropped speech latches no turn at all (no barge-in). ``producer``
    is the loop's :class:`~reachy.motion.listen.ListenProducer`; when it is ``None`` (or
    has no ``set_engaged``) the engaged signal is simply not wired and the gate still
    runs (words flow, no engaged turn). WITHOUT ``transcribe`` no classifier is built and
    no engaged signal is wired — the off path is byte-identical to today.

    ``voice_engine`` is the resolved :class:`~reachy.speech.voice.VoiceEngine`
    (see :func:`_resolve_voice_engine`) selecting the folded cognition's speech
    backend. It is threaded to BOTH the ``play_audio`` wrapper (so the self-mute
    window is stamped from the clip's REAL duration at the engine's sample rate —
    a fixed TTS-rate assumption would under/over-stamp a harmonic-rate clip) and
    into :func:`_build_think_hook` (so the engine's ``synthesize`` matches). For
    the default ``"tts"`` engine the wrapper's ``samplerate`` override is skipped
    entirely, so playback + mute-stamping stay byte-identical to before this
    feature; only ``"harmonic"`` threads a real override.

    ``cognition`` (the ``--cognition`` choice — ``"marker"`` default or ``"agent"``)
    selects which engine is built *behind the ThinkHook seam*: ``"marker"`` builds the
    established :class:`~reachy.speech.cognition.CognitionEngine` (via
    :func:`_build_think_hook`, byte-identical to before), ``"agent"`` builds the
    tool-use :class:`~reachy.speech.agent_turn.AgentTurnEngine` (via
    :func:`_build_agent_think_hook`) with the loop's ``queue`` wired into its
    ``apply_pose`` tool. Everything else — the shared buffer, the self-mute play
    wrapper, the TranscribeHook composition + engagement gate, the ordering — is
    identical for both; only the engine object behind the seam differs.
    """
    if clock is None:
        import time as _time

        clock = _time.monotonic

    sleep_hook = SleepHook(provider)
    vision_hook = VisionHook(queue=queue, transport=transport)

    # The shared cognition buffer + self-mute window live here, at composition level,
    # so the optional TranscribeHook feeds the SAME buffer the engine consumes and
    # reads the SAME mute window the playback wrapper stamps.
    think_buffer: object | None = None
    mute = {"until": 0.0}
    # Only the non-default ("harmonic") engine gets an explicit samplerate override —
    # the default "tts" engine's rate already matches _make_self_mute_play_audio's own
    # hardcoded TTS-rate fallback, so leaving it unset keeps both the real playback
    # call and the mute-duration math byte-identical to before this feature.
    voice_samplerate = (
        voice_engine.samplerate if voice_engine is not None and voice_engine.name != "tts" else None
    )
    # Always route --live cognition playback over HTTP to the daemon: speech plays
    # through the daemon's mixer (proven to coexist with the loop's one SDK session)
    # rather than opening a second ReachyMini client (single-SDK-owner). The wrapper
    # also stamps the self-mute window the TranscribeHook reads after each spoken clip.
    think_play_audio: object | None = _make_self_mute_play_audio(
        mute, clock, playback_transport="http", samplerate=voice_samplerate
    )

    if transcribe:
        think_buffer = _make_transcribe_buffer()

    # Under --transcribe, cognition is driven by transcribed WORDS only: the ThinkHook
    # stops pushing raw DoA/RMS sound cues, so the robot doesn't react to its own TTS
    # (a feedback loop) and stays quiet until someone actually speaks. Without
    # --transcribe there are no transcripts, so DoA cues remain the cognition input.
    # ``cognition`` selects the engine BEHIND the seam; the ``feed_doa_cues`` /
    # play_audio / buffer / export wiring is identical for both engines.
    if cognition == "agent":
        think_hook: object | None = _build_agent_think_hook(
            provider,
            queue,
            export=export,
            buffer=think_buffer,
            play_audio=think_play_audio,
            feed_doa_cues=not transcribe,
        )
    else:
        think_hook = _build_think_hook(
            provider,
            export=export,
            buffer=think_buffer,
            play_audio=think_play_audio,
            feed_doa_cues=not transcribe,
            voice_engine=voice_engine,
        )

    ordered: list[object] = [sleep_hook]
    if pat_hook is not None:
        ordered.append(pat_hook)
    if think_hook is not None:
        ordered.append(think_hook)

    if transcribe:
        transcribe_hook = _compose_transcribe_hook(
            provider,
            think_hook=think_hook,
            mute=mute,
            sample_rate=sample_rate,
            producer=producer,
        )
        if transcribe_hook is not None:
            ordered.append(transcribe_hook)

    ordered.append(vision_hook)
    return ordered


def _make_transcribe_buffer() -> object | None:
    """The shared ``--transcribe`` cognition buffer, or ``None`` if unavailable.

    Built up front, at composition level, so it can be wired into both the
    engine (via :func:`_build_think_hook`) and the TranscribeHook.
    """
    try:
        from reachy.speech.events import EventBuffer

        return EventBuffer()
    except Exception:  # noqa: BLE001
        logger.warning("listen --live --transcribe: EventBuffer unavailable", exc_info=True)
        return None


def _compose_transcribe_hook(
    provider: Callable[[], SenseSample | None],
    *,
    think_hook: object | None,
    mute: dict[str, float],
    sample_rate: int | None,
    producer: object | None,
) -> object | None:
    """Compose the ``--transcribe`` hook against the ThinkHook's REAL buffer.

    The transcribe hook feeds the buffer the ThinkHook's engine consumes — the
    buffer the hook was actually built with (it may differ if the shared buffer
    was ``None`` and the hook built its own). If cognition is unavailable
    entirely (no ThinkHook) there is no buffer to feed, so transcription is
    skipped gracefully (logged once) and ``None`` is returned.

    The engagement gate (addressed-vs-ambient LLM classifier) + the
    motion-ladder engaged signal are built ONLY here, under ``--transcribe``.
    The classifier shares cognition's ``REACHY_OPENAI_*`` endpoint; ``on_engage``
    is the producer's one-shot turn latch — fired only when the gate ENGAGES,
    so ambient speech never latches a barge-in turn.
    """
    feed_buffer = getattr(think_hook, "_buffer", None) if think_hook is not None else None
    if feed_buffer is None:
        logger.warning(
            "listen --live --transcribe: cognition unavailable; no buffer to feed "
            "transcribed words into — transcription disabled this run"
        )
        return None
    classifier = _build_engagement_classifier()
    on_engage = getattr(producer, "set_engaged", None) if producer is not None else None
    return _build_transcribe_hook(
        provider,
        buffer=feed_buffer,
        mute_until=lambda: mute["until"],
        sample_rate=sample_rate,
        classifier=classifier,
        on_engage=on_engage,
    )


def _make_self_mute_play_audio(
    mute: dict[str, float],
    clock: Callable[[], float],
    *,
    mute_after: float | None = None,
    playback_transport: str | None = None,
    samplerate: int | None = None,
) -> Callable[..., None]:
    """Wrap the real playback so each clip stamps the shared self-mute window.

    The returned callable plays the PCM (via :func:`reachy.speech.playback.play_audio`)
    and then stamps ``mute["until"] = clock() + mute_after`` so the TranscribeHook
    (reading ``mute_until=lambda: mute["until"]``) drops any audio captured while —
    and just after — the robot speaks. Mirrors ``think``'s ``_guarded_play``; the
    default ``mute_after`` is the documented ``_DEFAULT_MUTE_AFTER_SPEAK`` (2.5 s).

    ``playback_transport`` (e.g. ``"http"``) is injected as ``transport=`` into the
    playback call unless the caller already set one. ``--live`` passes ``"http"`` so
    cognition speech plays through the daemon's mixer — which coexists with the loop's
    one open SDK session — instead of opening a *second* ``ReachyMini`` client (the
    single-SDK-owner model; see ``CLAUDE.md``).

    ``samplerate`` (the active :class:`~reachy.speech.voice.VoiceEngine`'s rate, e.g.
    16000 for the harmonic voice) is injected as ``samplerate=`` into the playback
    call the SAME way — unless the caller already set one — and is ALSO the rate the
    mute-duration math below converts the clip's byte length with, so the mute window
    reflects the clip's REAL duration regardless of which engine rendered it. ``None``
    (the default) keeps today's behaviour exactly: no ``samplerate`` kwarg is injected
    into the playback call, and the duration math falls back to the hardcoded TTS
    default (:data:`reachy.speech.tts.DEFAULT_SAMPLE_RATE`) — so the default ``"tts"``
    voice engine's playback + mute stamping stay byte-identical to before this feature.
    """
    after = _DEFAULT_MUTE_AFTER_SPEAK if mute_after is None else max(0.0, float(mute_after))

    def _guarded_play(pcm: bytes, **kwargs: object) -> None:
        from reachy.speech.playback import play_audio as _play

        _default_kwarg(kwargs, "transport", playback_transport)
        _default_kwarg(kwargs, "samplerate", samplerate)
        _play(pcm, **kwargs)
        if after > 0:
            _stamp_mute_window(mute, clock, pcm, samplerate=samplerate, after=after)

    return _guarded_play


def _default_kwarg(kwargs: dict[str, object], key: str, value: object | None) -> None:
    """Inject ``key=value`` into a playback call's kwargs unless the caller set one."""
    if value is not None and key not in kwargs:
        kwargs[key] = value


def _stamp_mute_window(
    mute: dict[str, float],
    clock: Callable[[], float],
    pcm: bytes,
    *,
    samplerate: int | None,
    after: float,
) -> None:
    """Advance ``mute["until"]`` past this clip's REAL duration plus the margin.

    Playback may be async (HTTP play_sound returns before the audio finishes),
    so a fixed pad alone would expire mid-utterance and let the robot
    transcribe its own (long) voice — a slower feedback loop. Base the window
    on the audio length so the whole utterance is covered; ``samplerate=None``
    falls back to the hardcoded TTS default rate.
    """
    from reachy.speech.tts import DEFAULT_SAMPLE_RATE

    rate = float(samplerate if samplerate is not None else (DEFAULT_SAMPLE_RATE or 24000))
    clip_seconds = len(pcm) / (2.0 * rate) if rate > 0 else 0.0
    mute["until"] = clock() + clip_seconds + after


def _build_sample_tap(
    holder: SampleHolder,
    poller: DoaPoller,
    audio: Callable[[float], tuple[bool, bool | None]],
    audio_rms: dict[str, object],
    *,
    transcribe: bool = False,
) -> tuple[Callable[[float], Sense], Callable[[float], tuple[bool, bool | None]]]:
    """Wrap the loop's sense/audio taps so each tick publishes a shared SenseSample.

    The loop reads ONE mic chunk per tick — inside ``audio(t)`` (the loop's
    ``_audio``), which computes snap/sound_present AND stashes that chunk's loudness
    into ``audio_rms`` (and, under ``--transcribe``, the raw float32 chunk itself,
    next to the rms). We reuse those exact values here rather than re-reading the
    session (a second ``get_audio_sample()`` would consume a *different* chunk,
    desyncing the stored RMS from the snap decision and dropping half the audio).
    ``server.run`` calls ``audio(t)`` then ``sense(t)`` each tick, so the audio
    wrapper records this tick's snap and the sense wrapper (running second)
    assembles the full :class:`SenseSample` from the same chunk and publishes it.

    ``transcribe`` gates whether the raw chunk rides on :attr:`SenseSample.audio`:
    only when ``--transcribe`` is on does the sense tap copy the chunk ``_audio``
    already pulled (``audio_rms["audio"]``) onto the sample, so the STT hook can
    transcribe it. When off, :attr:`SenseSample.audio` stays ``None`` — there is no
    second read and the off path is byte-identical to today.
    """
    last: dict[str, bool | None] = {"snap": False, "sound_present": None}

    def _audio_tap(t: float) -> tuple[bool, bool | None]:
        snap, sound_present = audio(t)  # reads the chunk ONCE; stashes rms in audio_rms
        last["snap"] = bool(snap)
        last["sound_present"] = sound_present
        return snap, sound_present

    def _sense_tap(t: float) -> Sense:
        sense = poller(t)
        doa_deg = None if sense.doa_angle is None else math.degrees(sense.doa_angle)
        # Only ride the raw chunk onto the sample when transcription is on; the
        # chunk was already read by _audio this tick (no second get_audio_sample()).
        raw_audio = audio_rms.get("audio") if transcribe else None
        holder.update(
            SenseSample(
                rms=float(audio_rms["rms"]),  # type: ignore[arg-type]
                doa=doa_deg,
                speech=bool(sense.speech_detected) or bool(last["snap"]),
                ts=t,
                audio=raw_audio,  # type: ignore[arg-type]
            )
        )
        return sense

    return _sense_tap, _audio_tap


def _run_sdk_loop(
    transport: object,
    producer: ListenProducer,
    args: argparse.Namespace,
    on_action: Callable[[object], None],
    *,
    export: object | None = None,
    transcribe: bool = False,
    voice_engine: VoiceEngine | None = None,
    cognition: str = DEFAULT_COGNITION,
) -> int:
    """Drive the loop over an open SDK media session (real DoA + mic-audio loudness).

    The loop folds in proprioceptive head-pat detection (unless ``--no-pat``): a
    :class:`~reachy.motion.listen_pat.PatHook` runs once per tick via the executor's
    ``on_tick`` seam, reading the head pose back through the *same* SDK client the
    loop owns. On a detected pat it enqueues a lean→nuzzle→settle gesture and raises
    the ``pat_active`` flag (so the idle wander yields).

    Under ``--live`` it composes ALL four sense hooks into ONE
    :class:`~reachy.motion.listen_hooks.HookChain` (the loop's single ``on_tick``):
    ``sleep > pat > think`` by idle-interrupt priority, plus vision. The loop opens
    ONE media session and every hook rides it via the shared-sample provider — no
    hook opens a second single-consumer session (see the single-SDK-owner model in
    ``CLAUDE.md``). ``voice_engine`` (see :func:`_resolve_voice_engine`) is threaded
    into the live composition only — it selects the folded cognition's speech
    backend and is ignored entirely outside ``--live``. ``cognition`` (see
    :func:`_resolve_cognition`) selects the folded engine behind the ThinkHook seam
    (``"marker"`` default, ``"agent"`` for the tool-use engine); it is likewise a
    live-only choice.
    """
    snap_kwargs: dict[str, float] = {}
    if getattr(args, "snap_ratio", None) is not None:
        snap_kwargs["ratio"] = args.snap_ratio
    if getattr(args, "snap_floor", None) is not None:
        snap_kwargs["min_rms"] = args.snap_floor
    queue = MotionQueue()
    pat_hook = _build_pat_hook(args, transport, queue)
    holder = SampleHolder()
    with transport.media_session() as session:  # type: ignore[attr-defined]
        # Per-tick pose / move / frame reads ride the ONE open client (issue #51).
        loop_transport = _SessionBoundTransport(transport, session)
        poller = DoaPoller(read=lambda: read_doa(session, timeout=DOA_TIMEOUT))
        detector = SnapDetector(**snap_kwargs)
        # The ONE mic chunk read per tick; _audio stashes its loudness here so the
        # --live sample tap reuses it instead of reading a second (different) chunk.
        # Under --transcribe the SAME read also retains the raw chunk (audio_rms[
        # "audio"]) so the STT hook transcribes the exact chunk — still ONE read.
        audio_rms: dict[str, object] = {"rms": 0.0, "audio": None}

        def _audio(_t: float) -> tuple[bool, bool | None]:
            sample = session.get_audio_sample()
            if sample is None:
                audio_rms["rms"] = 0.0
                audio_rms["audio"] = None
                return (False, None)
            rms = float(np.sqrt(np.mean(sample**2)))
            audio_rms["rms"] = rms
            # Retain the raw chunk for the transcribe hook only when transcribing —
            # the same single read; when off this stays None (no STT input, byte-
            # identical off path).
            audio_rms["audio"] = sample if transcribe else None
            return (detector.feed(sample), rms > detector.min_rms)

        # --live composes all four sense hooks into one HookChain *and* taps the
        # loop's per-tick reading into the shared-sample holder the audio hooks read.
        # The default keeps the established single-PatHook on_tick and the bare
        # sense/audio taps byte-for-byte (no chain, no holder tap, no extra read).
        if getattr(args, "live", False):
            sense_tap, audio_tap = _build_sample_tap(
                holder, poller, _audio, audio_rms, transcribe=transcribe
            )
            hooks_list = _build_live_hooks(
                loop_transport,
                queue,
                holder.provider,
                pat_hook,
                export=export,
                transcribe=transcribe,
                sample_rate=getattr(session, "samplerate", None),
                producer=producer,
                voice_engine=voice_engine,
                cognition=cognition,
            )
            on_tick: object = HookChain(hooks_list)
        else:
            sense_tap, audio_tap = poller, _audio
            on_tick = pat_hook

        try:
            return run_loop(
                loop_transport,
                producer,
                hooks=LoopHooks(
                    sense=sense_tap, audio=audio_tap, on_action=on_action, on_tick=on_tick
                ),
                queue=queue,
                max_ticks=args.max_ticks,
            )
        finally:
            close = getattr(on_tick, "close", None)
            if close is not None:
                close()


def _run_http_loop(
    transport: object,
    producer: ListenProducer,
    args: argparse.Namespace,
    on_action: Callable[[object], None],
) -> int:
    """Drive the loop over the HTTP transport's DoA (no audio source / loudness)."""
    poller = DoaPoller(read=lambda: read_doa(transport, timeout=DOA_TIMEOUT))
    return run_loop(
        transport,
        producer,
        hooks=LoopHooks(sense=poller, on_action=on_action),
        max_ticks=args.max_ticks,
    )


def _resolve_export_hook(args: argparse.Namespace) -> object | None:
    """Build the ``--export`` hook (or ``None``), requiring ``--live`` for it.

    A bare ``--export`` (no ``--live``) is a clean exit-1 user error — the feed
    carries cognition blocks only the folded live loop produces. This runs *before*
    ``get_transport`` so the combo error fires regardless of whether the sdk extra
    is installed (the tests rely on this ordering).
    """
    export_hook = build_export_hook(args)
    if export_hook is not None and not getattr(args, "live", False):
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--export needs --live",
            remediation="the export feed carries cognition blocks, which only the "
            "folded live loop produces; add --live (it runs on the sdk transport)",
        )
    return export_hook


def _require_export_transport(export_hook: object | None, transport: object) -> None:
    """The cognition feed needs the sdk media session; the http profile can't fold it."""
    if export_hook is not None and not hasattr(transport, "media_session"):
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--export/--live require the sdk transport",
            remediation="run with --transport sdk (the default); the http profile has "
            "no media session to fold cognition into",
        )


def _resolve_transcribe(args: argparse.Namespace) -> bool:
    """Whether to fold STT transcription in, requiring ``--live`` for it.

    A bare ``--transcribe`` (no ``--live``) is a clean exit-1 user error — the
    transcribed words are only useful when there is a folded cognition buffer to
    feed, which only the live loop builds. This mirrors ``_resolve_export_hook``
    and runs *before* ``get_transport`` so the combo error fires regardless of
    whether the sdk extra is installed (the tests rely on this ordering).
    """
    transcribe = bool(getattr(args, "transcribe", False))
    if transcribe and not getattr(args, "live", False):
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--transcribe needs --live",
            remediation="transcription feeds words into the folded live cognition "
            "buffer, which only --live builds; add --live (it runs on the sdk transport)",
        )
    return transcribe


def _require_transcribe_transport(transcribe: bool, transport: object) -> None:
    """STT transcription needs the sdk media session; the http profile has no mic."""
    if transcribe and not hasattr(transport, "media_session"):
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--transcribe requires the sdk transport",
            remediation="run with --transport sdk (the default); the http profile has "
            "no mic audio to transcribe",
        )


def _resolve_voice_engine(args: argparse.Namespace) -> VoiceEngine:
    """Resolve the ``--voice-engine`` choice, requiring ``--live`` for the explicit flag.

    A bare ``--voice-engine`` (no ``--live``) is a clean exit-1 user error — the
    engine only selects the folded live cognition's speech backend, which only
    ``--live`` builds. This mirrors ``_resolve_export_hook`` / ``_resolve_transcribe``
    and runs *before* ``get_transport`` so the combo error fires regardless of
    whether the sdk extra is installed (the tests rely on this ordering).

    Unlike ``export``/``transcribe`` (which are booleans, off by default), a voice
    engine is ALWAYS resolved — :func:`reachy.speech.voice.resolve_voice_engine`
    falls back through the ``REACHY_VOICE_ENGINE`` env var to ``"tts"`` when no
    explicit choice is given, so the return value is never ``None``. The resolved
    engine is simply unused downstream when ``--live`` is not set.
    """
    explicit = getattr(args, "voice_engine", None)
    if explicit is not None and not getattr(args, "live", False):
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--voice-engine needs --live",
            remediation="the voice engine selects the folded live cognition's speech "
            "backend, which only --live builds; add --live (it runs on the sdk "
            "transport)",
        )
    return resolve_voice_engine(explicit)


def _resolve_cognition(args: argparse.Namespace) -> str:
    """Resolve the ``--cognition`` choice, requiring ``--live`` for the explicit flag.

    A bare ``--cognition`` (no ``--live``) is a clean exit-1 user error — the engine
    only selects the folded live loop's thinking backend, which only ``--live``
    builds. This mirrors ``_resolve_voice_engine`` / ``_resolve_transcribe`` and runs
    *before* ``get_transport`` so the combo error fires regardless of whether the sdk
    extra is installed (the tests rely on this ordering).

    Resolution order mirrors :func:`reachy.speech.voice.resolve_voice_engine`: the
    explicit flag > the ``REACHY_COGNITION`` env var > the ``"marker"`` default. An
    unknown value (only reachable through the env var — argparse's ``choices=`` guards
    the flag) is a clean exit-1 error, like an unknown voice engine.
    """
    explicit = getattr(args, "cognition", None)
    if explicit is not None and not getattr(args, "live", False):
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--cognition needs --live",
            remediation="the cognition engine selects the folded live loop's thinking "
            "backend, which only --live builds; add --live (it runs on the sdk "
            "transport)",
        )
    resolved = explicit or os.environ.get(COGNITION_ENV) or DEFAULT_COGNITION
    if resolved not in _COGNITION_CHOICES:
        valid = ", ".join(_COGNITION_CHOICES)
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown cognition engine: {resolved!r}",
            remediation=f"choose one of: {valid}",
        )
    return resolved


def _orienting_banner(
    transport: object,
    params: object,
    *,
    live: bool,
    exporting: bool,
    voice_engine: VoiceEngine | None = None,
    cognition: str = DEFAULT_COGNITION,
) -> str:
    """The one-line '[listen] orienting…' preflight banner (stderr)."""
    voice_note = ""
    if live and voice_engine is not None:
        voice_note = f" (voice: {voice_engine.name})"
    # Only the non-default agent engine gets a banner note, so the marker (default)
    # live banner is unchanged.
    cognition_note = " (cognition: agent)" if live and cognition == "agent" else ""
    return (
        f"[listen] orienting to sound via {transport.name}: dwell={params.dwell:g}s "
        f"hold={params.hold:g}s speed={params.alert_speed:g}deg/s"
        f"{' (speech only)' if params.speech_only else ''}"
        f"{' (live: think/vision/sleep folded in)' if live else ''}"
        f"{cognition_note}"
        f"{voice_note}"
        f"{' [export: stdout]' if exporting else ''}; Ctrl-C to stop"
    )


def cmd_listen_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    # Export sink (None unless `--export -`) and the --transcribe opt-in. Both are
    # validated *before* get_transport so a bad combo (e.g. --transcribe without
    # --live) is a clean exit-1 error regardless of whether the sdk extra is
    # installed (the tests rely on this ordering).
    export_hook = _resolve_export_hook(args)
    transcribe = _resolve_transcribe(args)
    voice_engine = _resolve_voice_engine(args)
    cognition = _resolve_cognition(args)
    transport = get_transport(args)
    _require_export_transport(export_hook, transport)
    _require_transcribe_transport(transcribe, transport)
    params = _params_from_args(args)
    if transcribe:
        # In the words-only live mode the head must NOT swing toward every sound
        # (it should turn only on its name) — and suppressing the large Tier-2
        # escalate-turns also sidesteps the SDK goto fault they can trip. Tier-1
        # antenna lean still reacts to sound.
        params.turn_enabled = False
    producer = ListenProducer(params)
    # When exporting, stdout is reserved for the pure JSONL feed: every banner,
    # action line, and summary goes to stderr regardless of --json.
    exporting = export_hook is not None
    text_diagnostics = (not json_mode) or exporting

    # Preflight: ease to center. Validates the transport (a dead daemon raises a
    # clean CliError → tidy exit) and gives the loop a known starting pose.
    transport.move_goto(head=dict(_CENTER), duration=0.8, interpolation="minjerk")
    if text_diagnostics:
        emit_diagnostic(
            _orienting_banner(
                transport,
                params,
                live=getattr(args, "live", False),
                exporting=exporting,
                voice_engine=voice_engine,
                cognition=cognition,
            )
        )

    def _on_action(action) -> None:
        yaw = action.head.get("yaw") if action.head else None
        if json_mode and not exporting:
            emit_result(
                {"action": action.label, "yaw": yaw, "duration": round(action.duration, 3)},
                json_mode=True,
            )
        else:
            emit_diagnostic(f"[listen] {action.label} ({action.duration:.1f}s)")

    # SDK profile streams real DoA + mic loudness through a media session; the HTTP/remote
    # profile polls transport.doa() with no audio source.
    if hasattr(transport, "media_session"):
        ticks = _run_sdk_loop(
            transport,
            producer,
            args,
            _on_action,
            export=export_hook,
            transcribe=transcribe,
            voice_engine=voice_engine,
            cognition=cognition,
        )
    else:
        ticks = _run_http_loop(transport, producer, args, _on_action)

    # Settle: ease back to center (best effort — a dead daemon can't be settled).
    try:
        transport.move_goto(head=dict(_CENTER), duration=0.8, interpolation="minjerk")
    except CliError:
        pass
    if text_diagnostics:
        emit_diagnostic(f"[listen] stopped after {ticks} tick(s)")
    return 0


# --- start / stop / restart / status --------------------------------------


def cmd_listen_start(args: argparse.Namespace) -> int:
    data = supervisor.start(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        params=_params_from_args(args),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_listen_stop(args: argparse.Namespace) -> int:
    data = supervisor.stop(timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_listen_restart(args: argparse.Namespace) -> int:
    data = supervisor.restart(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        params=_params_from_args(args),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_listen_status(args: argparse.Namespace) -> int:
    data = supervisor.status(base_url=args.base_url, timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_listen_overview(args)


def _register_run(noun_sub: argparse._SubParsersAction) -> None:
    run = noun_sub.add_parser("run", help="Run the sound-orienting loop in the foreground.")
    add_robot_args(run)
    run.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_tuning_args(run)
    _add_pat_args(run)
    _add_live_arg(run)
    _add_voice_engine_arg(run)
    _add_cognition_arg(run)
    add_export_args(run)
    run.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        dest="max_ticks",
        help="Stop after this many loop ticks (default: run until signalled).",
    )
    run.set_defaults(func=cmd_listen_run)


def _register_process_verbs(noun_sub: argparse._SubParsersAction) -> None:
    start = noun_sub.add_parser("start", help="Start the sound-orienting loop in the background.")
    add_robot_args(start)
    start.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_tuning_args(start)
    _add_pat_args(start)
    start.set_defaults(func=cmd_listen_start)

    restart = noun_sub.add_parser("restart", help="Restart the background loop (re-reads tuning).")
    add_robot_args(restart)
    restart.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_tuning_args(restart)
    _add_pat_args(restart)
    restart.set_defaults(func=cmd_listen_restart)

    stop = noun_sub.add_parser("stop", help="Stop the loop this CLI started.")
    stop.add_argument("--json", action="store_true", help=_JSON_HELP)
    stop.add_argument(
        "--timeout",
        type=float,
        default=supervisor.DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait after SIGTERM before SIGKILL "
        f"(default: {supervisor.DEFAULT_STOP_TIMEOUT:g}).",
    )
    stop.set_defaults(func=cmd_listen_stop)

    st = noun_sub.add_parser("status", help="Report listen process + daemon state.")
    add_robot_args(st)
    st.set_defaults(func=cmd_listen_status)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "listen",
        help="Orient the head toward sound (see 'reachy-mini-cli listen overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="listen_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the listen noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_listen_overview)

    _register_run(noun_sub)
    _register_process_verbs(noun_sub)
