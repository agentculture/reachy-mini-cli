"""``reachy-mini-cli agent`` — attach an external AI agent over the runtime seams.

This noun is the *external agent client* of the symbolic runtime. It realizes two
decisions from the ``symbolic-runtime-70`` spec:

* **c11 — the loop is AI-agnostic; an agent attaches externally.** The
  deterministic 50 Hz runtime (``behavior engine run``) ticks, evaluates its
  rules, and sustains intents entirely on its own. This client attaches from a
  *separate process*: it never edits a systemd unit, never restarts the loop, and
  never opens the robot's SDK. It reads the runtime's published event feed and
  acts by writing atomic commands into the intents spool the running engine
  drains — the same seam ``behavior``/``listen`` already use.
* **c27 — the agent publishes its OWN cognition feed.** The runtime feed carries
  only perception/decision events (``sense``/``rule``/``intent``/``motion`` — see
  :mod:`reachy.export.runtime`); it never carries a cognition block. The agent's
  *thinking/message/emotion* is a wholly separate feed, published here through the
  **same** exporter ``think run --export -`` / ``listen run --live --export -``
  use (:func:`reachy.cli._export.build_export_hook`), so the wire contract matches
  ``docs/export-schema.md``'s cognition feed exactly.

Three composition seams (all injectable so tests need no live LLM, robot, or
network):

* **INPUT** — ``--feed <path|->``: runtime-event JSONL lines to read (a
  stream/FIFO/file to tail, or ``-`` for stdin). This client does **not** spawn
  the runtime; it only reads the feed the runtime writes. Each runtime event is
  mapped to zero or more short first-person perception cues
  (:func:`_cues_for_runtime_event`) and accumulated in a
  :class:`_RuntimeCueBuffer` — a minimal ``snapshot()``-only buffer the tool-use
  engine consumes exactly as it consumes the folded ``listen --live`` sense
  buffer.
* **COGNITION** — an :class:`~reachy.speech.agent_turn.AgentTurnEngine` wired with
  a :class:`~reachy.speech.tools.ToolRegistry` carrying the four **intent tools**
  (:func:`reachy.speech.intent_tools.register_intent_tools`) so its actions are
  atomic spool writes. The built-in ``speak`` / ``harmonics`` / ``apply_pose``
  tools are present too, but wired **publish-only** (inert seams) — they exist so
  the agent can emit ``message`` / ``emotion`` blocks to its cognition feed
  *without* the external client ever touching the robot (the single-SDK-owner
  model: the runtime loop owns the robot; this client owns cognition + intents).
* **OUTPUT** — ``--export -`` / ``--export-blocks``: the agent's own
  ``thinking`` / ``message`` / ``emotion`` JSONL feed, built by the shared
  :func:`reachy.cli._export.build_export_hook` so it cannot drift from the other
  cognition feeds.

Like ``daemon`` / ``service``, ``agent`` does **not** use a ``--transport`` — it
talks to feeds + the intent spool, not the robot — so its ``overview`` is
hand-built and never calls ``_robot.get_transport`` / ``noun_overview``.

Runtime-event → cue mapping (honest + documented)
-------------------------------------------------
Every runtime event is edge-triggered (published only when it changes), so the
feed is naturally sparse — no per-tick flood. Each event maps to a concise
perception cue reusing the same vocabulary the folded ``listen --live`` cognition
path uses (:mod:`reachy.speech.events`):

* ``sense``  → ``"speech from the <dir>"`` / ``"loud sound <dir>"`` /
  ``"felt a <intensity> <touch> on the head"`` / ``"saw <name>"``.
* ``rule``   → ``"a behavior rule fired (<rule>): now doing <behavior>"`` (etc.).
* ``intent`` → ``"a standing intent was set: <name>"`` (the agent perceiving its
  own — or a peer's — declared goal taking effect).
* ``motion`` → ``"started moving: <behavior>"`` / ``"stopped moving: …"`` (a
  low-level ``goto`` is not surfaced, to keep turns focused).
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import sys
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import TextIO

from reachy import senselog
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.cli._export import add_export_args, build_export_hook
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.speech.events import SenseCue

_JSON_HELP = "Emit structured JSON."

# Sound-direction band + loudness threshold — mirror reachy.speech.events'
# DoA convention (0 = left, pi/2 = front, pi = right; ~15° "ahead" band) and its
# loud-sound floor, so the agent's perception vocabulary matches the folded
# listen --live cognition path exactly.
_AHEAD_BAND_RAD: float = 0.26
_LOUD_RMS_THRESHOLD: float = 0.02

# Touch phrasing — keys match the strings reachy.motion.pat.PatDetector emits and
# reachy.speech.events uses for the folded pat cue.
_PAT_KIND_PHRASE: dict[str, str] = {"scratch": "scratch", "side_pat": "sideways nudge"}
_PAT_LEVEL_INTENSITY: dict[str, str] = {"level1": "gentle", "level2": "firm"}

# Inert voice sample rates for the publish-only speak/harmonics tools (a no-op
# playback ignores them — they only satisfy the VoiceEngine shape).
_TTS_RATE = 24000
_HARMONIC_RATE = 16000


# ---------------------------------------------------------------------------
# Runtime-event → perception-cue mapping
# ---------------------------------------------------------------------------


def _direction_word(doa: object) -> str | None:
    """Map a DoA angle (radians) to ``"left"`` / ``"ahead"`` / ``"right"``, or ``None``.

    Convention (``reachy.behavior.sense`` / ``reachy.speech.events``): ``0`` = left,
    ``pi/2`` = front, ``pi`` = right. A ``None``/unparseable angle yields ``None``.
    """
    if doa is None:
        return None
    try:
        angle = float(doa)
    except (TypeError, ValueError):
        return None
    front = math.pi / 2.0
    if angle < front - _AHEAD_BAND_RAD:
        return "left"
    if angle > front + _AHEAD_BAND_RAD:
        return "right"
    return "ahead"


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sense_cues(event: dict) -> list[str]:
    """Cues for a ``sense`` runtime event (perception snapshot)."""
    cues: list[str] = []
    direction = _direction_word(event.get("doa"))
    rms = event.get("rms")
    if event.get("speech"):
        cues.append(f"speech from the {direction}" if direction else "speech nearby")
    elif _is_number(rms) and rms >= _LOUD_RMS_THRESHOLD:
        cues.append(f"loud sound {direction}" if direction else "loud sound nearby")

    pat = event.get("pat")
    if isinstance(pat, (list, tuple)) and len(pat) == 2:
        phrase = _PAT_KIND_PHRASE.get(pat[0])
        intensity = _PAT_LEVEL_INTENSITY.get(pat[1])
        if phrase and intensity:
            cues.append(f"felt a {intensity} {phrase} on the head")

    face = event.get("face")
    if isinstance(face, str) and face.strip():
        cues.append(f"saw {face.strip()}")
    return cues


def _rule_cues(event: dict) -> list[str]:
    """Cues for a ``rule`` runtime event (a rule fire/suppress decision)."""
    rule = str(event.get("rule") or "a rule")
    action = event.get("action")
    if action == "fire":
        behavior = event.get("behavior")
        disable = event.get("disable") or []
        if behavior:
            return [f"a behavior rule fired ({rule}): now doing {behavior}"]
        if disable:
            joined = ", ".join(str(d) for d in disable)
            return [f"a behavior rule fired ({rule}): stopping {joined}"]
        return [f"a behavior rule fired ({rule})"]
    if action == "suppress":
        return [f"a behavior rule held off ({rule})"]
    return []


def _intent_cues(event: dict) -> list[str]:
    """Cues for an ``intent`` runtime event (a standing goal declared/updated/cleared)."""
    action = event.get("action")
    name = str(event.get("name") or "").strip()
    if action == "clear":
        return ["a standing intent was cleared"]
    if action in ("declare", "update"):
        verb = "set" if action == "declare" else "updated"
        return [
            f"a standing intent was {verb}: {name}" if name else f"a standing intent was {verb}"
        ]
    return []


def _motion_cues(event: dict) -> list[str]:
    """Cues for a ``motion`` runtime event (a behavior admission/eviction)."""
    action = event.get("action")
    label = str(event.get("behavior") or "a body behavior")
    if action == "admit":
        return [f"started moving: {label}"]
    if action == "evict":
        return [f"stopped moving: {label}"]
    return []  # a low-level goto keyframe is not surfaced as a cognition cue


_CUE_MAPPERS: dict[str, Callable[[dict], list[str]]] = {
    "sense": _sense_cues,
    "rule": _rule_cues,
    "intent": _intent_cues,
    "motion": _motion_cues,
}


def _cues_for_runtime_event(event: object) -> list[str]:
    """Map one runtime-feed event dict to zero or more perception-cue strings.

    Dispatches on the event's ``t`` discriminator against the runtime feed's four
    block types (:data:`reachy.export.runtime.RUNTIME_BLOCKS`). An unrecognised or
    malformed event yields no cue — never raises, so one bad feed line can never
    break the attach loop.
    """
    if not isinstance(event, dict):
        return []
    mapper = _CUE_MAPPERS.get(event.get("t"))
    return mapper(event) if mapper is not None else []


def _parse_runtime_line(line: str) -> dict | None:
    """Parse one JSONL feed line into an event dict, or ``None`` for junk/blank."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _event_ts(event: dict) -> float:
    try:
        return float(event.get("ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# The cue buffer the agent engine consumes
# ---------------------------------------------------------------------------


class _RuntimeCueBuffer:
    """Thread-safe buffer mapping runtime events → :class:`SenseCue` perceptions.

    Satisfies the ``_BufferLike`` protocol the
    :class:`~reachy.speech.agent_turn.AgentTurnEngine` consumes (a single
    ``snapshot() -> list[SenseCue]``), so the tool-use engine drives it exactly as
    it drives the folded ``listen --live`` sense buffer. Runtime events are pushed
    in via :meth:`feed_event`; :meth:`snapshot` atomically drains.
    """

    def __init__(self, maxlen: int = 256) -> None:
        self._lock = threading.Lock()
        self._buf: deque[SenseCue] = deque(maxlen=maxlen)
        self._maxlen = maxlen

    def feed_event(self, event: dict) -> int:
        """Map *event* to cues, append them, and return how many were added.

        A return of ``0`` means the event produced no cue (an unrecognised type, a
        below-threshold reading, …) so the caller can skip running a turn for it.
        """
        cues = _cues_for_runtime_event(event)
        if not cues:
            return 0
        ts = _event_ts(event)
        with self._lock:
            for text in cues:
                self._buf.append(SenseCue(text=text, timestamp=ts))
        for text in cues:
            senselog.stage("cue", "runtime", uuid.uuid4().hex[:8], text)
        return len(cues)

    def snapshot(self) -> list[SenseCue]:
        """Return all buffered cues (oldest first) and atomically clear the buffer."""
        with self._lock:
            old = self._buf
            self._buf = deque(maxlen=self._maxlen)
        return list(old)


# ---------------------------------------------------------------------------
# The default cognition engine — a real AgentTurnEngine acting via the spool
# ---------------------------------------------------------------------------


def _build_default_engine(
    buffer: _RuntimeCueBuffer,
    export: object,
    *,
    spool_dir: Path | None,
    await_timeout: float,
    modes: Iterable[str] = (),
    turn_fn: object | None = None,
) -> object:
    """Build the real :class:`AgentTurnEngine` whose actions are intent-spool writes.

    The registry carries:

    * the four **intent tools** (:func:`register_intent_tools`) — the agent's real
      actuators: each is an atomic write into the ``intents`` namespaced spool the
      running engine drains, so an agent turn moves the robot *through the runtime*
      rather than around it (decision c11);
    * the built-in ``speak`` / ``harmonics`` / ``apply_pose`` tools wired
      **publish-only** — inert ``synthesize`` / ``play`` / ``express`` seams — so a
      tool call still emits its ``message`` / ``emotion`` export block (the export
      block is emitted ahead of dispatch by the engine) but the *external* client
      never opens the robot's SDK. The runtime loop owns the robot; this client
      owns cognition + intents.

    ``turn_fn`` is the LLM turn function; ``None`` (the default) lets
    :class:`AgentTurnEngine` use :func:`reachy.speech.llm.stream_turn` over the
    ``REACHY_OPENAI_*`` config. Tests build this same engine with a fake ``turn_fn``
    so no network is ever hit. Lazy-imported so registering the noun stays cheap.
    """
    from reachy.speech.agent_turn import AgentTurnEngine
    from reachy.speech.intent_tools import register_intent_tools
    from reachy.speech.tools import ToolRegistry
    from reachy.speech.voice import VoiceEngine

    def _silent_synth(_text: str) -> bytes:
        return b""

    def _no_play(_pcm: object, **_kw: object) -> None:
        return None

    def _no_express(_emoji: str) -> None:
        return None

    registry = ToolRegistry(
        express=_no_express,
        speak_engine=VoiceEngine(name="tts", synthesize=_silent_synth, samplerate=_TTS_RATE),
        harmonic_engine=VoiceEngine(
            name="harmonic", synthesize=_silent_synth, samplerate=_HARMONIC_RATE
        ),
        play=_no_play,
    )
    register_intent_tools(
        registry, spool_dir=spool_dir, await_timeout=await_timeout, modes=tuple(modes)
    )

    kwargs: dict[str, object] = {}
    if turn_fn is not None:
        kwargs["turn_fn"] = turn_fn
    return AgentTurnEngine(
        buffer=buffer,
        registry=registry,
        export=export,
        audio_optional=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The attach loop
# ---------------------------------------------------------------------------


def _run_attach_loop(
    lines: Iterable[str],
    buffer: _RuntimeCueBuffer,
    engine: object,
    *,
    max_turns: int | None,
    max_events: int | None,
) -> dict:
    """Read runtime lines, feed cues, run one agent turn per cue-bearing event.

    Each consumed runtime event that maps to at least one cue triggers exactly one
    serialized :meth:`AgentTurnEngine.run_turn`, which snapshots the accumulated
    cues and acts through the tool loop. Returns ``{"events": …, "turns": …}``.
    """
    turns = 0
    events = 0
    for line in lines:
        if max_events is not None and events >= max_events:
            break
        event = _parse_runtime_line(line)
        if event is None:
            continue
        events += 1
        if buffer.feed_event(event) == 0:
            continue
        if engine.run_turn():
            turns += 1
            if max_turns is not None and turns >= max_turns:
                break
    return {"events": events, "turns": turns}


def _open_feed(feed: str, *, stdin: TextIO | None = None) -> Iterator[str]:
    """Yield runtime-event JSONL lines from *feed* (``-`` = stdin, else a path).

    A FIFO / pipe streams line-by-line as data arrives (the intended "tail a live
    runtime feed" use); a regular file is read once to EOF. This client never
    spawns the runtime — it only reads the feed the runtime writes.
    """
    if feed == "-":
        source = stdin if stdin is not None else sys.stdin
        yield from source
        return
    try:
        handle = Path(feed).open("r", encoding="utf-8")
    except OSError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot open runtime feed {feed!r}: {exc}",
            remediation="pass --feed - to read from stdin, or a readable path/FIFO the "
            "runtime writes (behavior engine run --export -)",
        ) from exc
    with handle:
        yield from handle


def _resolve_spool_dir(args: argparse.Namespace) -> Path | None:
    raw = getattr(args, "spool_dir", None)
    return Path(raw) if raw else None


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------


def cmd_agent_attach(
    args: argparse.Namespace,
    *,
    lines: Iterable[str] | None = None,
    engine_factory: Callable[[_RuntimeCueBuffer, object], object] | None = None,
    stream: TextIO | None = None,
) -> int:
    """Attach to a running runtime: read its feed, act via intents, publish cognition.

    Composition seams (each injectable so tests need no live LLM/robot/network):

    * ``lines`` — the runtime-event line source (default: :func:`_open_feed` over
      ``args.feed``);
    * ``engine_factory`` — ``(buffer, export_hook) -> engine`` (default:
      :func:`_build_default_engine` over the intents spool); tests inject a fake
      engine or the same builder with a fake ``turn_fn``;
    * ``stream`` — the export sink stream for the cognition feed (default stdout).
    """
    json_mode = bool(getattr(args, "json", False))

    # OUTPUT seam: the agent's OWN cognition feed (thinking/message/emotion), the
    # SAME exporter think/listen use — the wire contract matches the schema doc.
    export_hook = build_export_hook(args, stream=stream)

    # INPUT seam: runtime-event JSONL lines (a stream/FIFO/file, or '-' for stdin).
    feed_lines = lines if lines is not None else _open_feed(getattr(args, "feed", "-"))

    # COGNITION seam: the tool-use engine whose actions are intents-spool writes.
    buffer = _RuntimeCueBuffer()
    if engine_factory is None:
        engine_factory = functools.partial(
            _build_default_engine,
            spool_dir=_resolve_spool_dir(args),
            await_timeout=float(getattr(args, "await_timeout", 1.0)),
        )
    engine = engine_factory(buffer, export_hook)

    # A start banner is always safe on stderr (never pollutes the --export stdout feed).
    emit_diagnostic("[agent] attached: runtime feed -> intent spool; publishing own cognition feed")

    stats = _run_attach_loop(
        feed_lines,
        buffer,
        engine,
        max_turns=getattr(args, "max_turns", None),
        max_events=getattr(args, "max_events", None),
    )

    # Under --export, stdout is reserved for the pure JSONL cognition feed, so the
    # summary goes to stderr; otherwise a --json summary is a stdout result.
    if json_mode and export_hook is None:
        emit_result({"status": "ok", **stats}, json_mode=True)
    else:
        emit_diagnostic(
            f"[agent] detached: {stats['turns']} turn(s) over {stats['events']} event(s)"
        )
    return 0


# ---------------------------------------------------------------------------
# overview (rubric-required; hand-built — no transport line)
# ---------------------------------------------------------------------------

_VERBS = [
    "agent attach — read the runtime event feed, act via the intent spool, "
    "publish the agent's own cognition feed",
    "agent overview — describe the agent noun",
]


def cmd_agent_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "Attach an EXTERNAL AI agent over the runtime's seams — the "
                "deterministic loop is AI-agnostic (decision c11); the agent attaches "
                "from a separate process, no unit edit and no loop restart.",
                "INPUT: read the runtime's own event feed (sense/rule/intent/motion "
                "JSONL) from --feed <path|-> — this client never spawns the runtime.",
                "COGNITION: a tool-use engine whose actions are ATOMIC INTENT-SPOOL "
                "writes (run_behavior / declare_goal / set_mode / set_inhibition) the "
                "running engine drains each tick.",
                "OUTPUT: the agent publishes its OWN thinking/message/emotion feed via "
                "--export - (decision c27: the runtime feed carries no cognition block).",
                "Detaching changes nothing about the loop — it keeps ticking and its "
                "rules keep running, agent attached or not.",
            ],
        },
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "Conventions",
            "items": [
                "no --transport: this noun talks to feeds + the intent spool, not the robot",
                "every command supports --json",
                "results to stdout, diagnostics to stderr (never mixed); under --export "
                "stdout is the pure JSONL cognition feed and summaries go to stderr",
                "exit codes: 0 ok, 1 user error, 2 environment error",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli agent",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_agent_overview(args)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    """Register the ``agent`` noun group (``attach`` + ``overview``) into *sub*."""
    p = sub.add_parser(
        "agent",
        help="Attach an external AI agent over the runtime seams "
        "(see 'reachy-mini-cli agent overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    # parser_class propagates so nested parse errors keep the structured contract.
    noun_sub = p.add_subparsers(dest="agent_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the agent noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_agent_overview)

    attach = noun_sub.add_parser(
        "attach",
        help="Read the runtime event feed, act via the intent spool, publish the "
        "agent's own cognition feed.",
    )
    attach.add_argument(
        "--feed",
        default="-",
        metavar="PATH",
        help="Runtime-event JSONL source: a path (stream/FIFO/file) or '-' for stdin "
        "(default). This client never spawns the runtime; it reads the feed the "
        "runtime writes (behavior engine run --export -).",
    )
    attach.add_argument(
        "--spool-dir",
        default=None,
        dest="spool_dir",
        metavar="DIR",
        help="Override the intents-spool root (default: the shared state dir, so the "
        "client writes into the SAME intents spool the running engine drains).",
    )
    attach.add_argument(
        "--await-timeout",
        type=float,
        default=1.0,
        dest="await_timeout",
        help="Seconds an intent tool waits for the engine to confirm a spool command "
        "before returning 'submitted, unconfirmed' (default: 1.0).",
    )
    attach.add_argument(
        "--max-turns",
        type=int,
        default=None,
        dest="max_turns",
        help="Stop after this many agent turns that ran (default: unbounded).",
    )
    attach.add_argument(
        "--max-events",
        type=int,
        default=None,
        dest="max_events",
        help="Stop after consuming this many runtime events (default: unbounded).",
    )
    add_export_args(attach)
    attach.add_argument("--json", action="store_true", help=_JSON_HELP)
    attach.set_defaults(func=cmd_agent_attach)
