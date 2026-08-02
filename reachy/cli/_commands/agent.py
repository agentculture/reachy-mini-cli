"""``reachy-mini-cli agent`` — attach an external AI agent over the runtime seams.

Two verbs, two minds, one noun. ``attach`` is the turn-based, text-cue-driven
client described below. ``embody`` is the **embodiment layer**'s composition
root (``docs/specs/2026-08-01-embodiment-layer.md``, decision: "the layer lives
under the agent noun — a new verb beside attach"): a detachable realtime
harness that hears and speaks out loud over ONE lobes ``/v1/realtime`` duplex
session while it thinks over the streaming HTTP lane. Its own section lives at
the bottom of this module; everything down to that point is ``attach``.

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
  **same** shared exporter builder
  (:func:`reachy.cli._export.build_export_hook`), so the wire contract matches
  ``docs/export-schema.md``'s cognition feed exactly.

Three composition seams (all injectable so tests need no live LLM, robot, or
network):

* **INPUT** — ``--feed <path|->``: runtime-event JSONL lines to read (a
  stream/FIFO/file to tail, or ``-`` for stdin). This client does **not** spawn
  the runtime; it only reads the feed the runtime writes. Each runtime event is
  mapped to zero or more short first-person perception cues
  (:func:`_cues_for_runtime_event`) and accumulated in a
  :class:`_RuntimeCueBuffer` — a minimal ``snapshot()``-only buffer the tool-use
  engine consumes exactly as it consumed the retired folded ``listen --live``
  sense buffer.
* **COGNITION** — an :class:`~reachy.speech.agent_turn.AgentTurnEngine` wired with
  a :class:`~reachy.speech.tools.ToolRegistry` carrying the four **intent tools**
  (:func:`reachy.speech.intent_tools.register_intent_tools`) so its actions are
  atomic spool writes. The built-in ``speak`` / ``harmonics`` / ``apply_pose``
  tools are present too, but wired **publish-only** (inert seams) — they exist so
  the agent can emit ``message`` / ``emotion`` blocks to its cognition feed
  *without* the external client ever touching the robot (the single-SDK-owner
  model: the runtime loop owns the robot; this client owns cognition + intents).
  The registry also carries the ``forge`` **self-extension** tool
  (:mod:`reachy.forge`, composed by :func:`_activate_forge`): a turn can hand a
  natural-language goal to a coder model and, once the generated code clears the
  fail-closed AST validator, gain a new callable tool on the *next* turn with no
  restart. Its dispatch seam and register/announce callbacks are plain **injected**
  callables — :mod:`reachy.speech.tools` and :mod:`reachy.speech.agent_turn` never
  import :mod:`reachy.forge` — and a missing/broken forge stack disables only that
  one tool.
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
perception cue reusing the same vocabulary :mod:`reachy.speech.events` defines:

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
import logging
import math
import os
import sys
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from reachy import senselog
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.cli._export import add_export_args, build_export_hook
from reachy.cli._logging import add_log_level_arg, install_logging
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.export.events import ThinkingEvent

if TYPE_CHECKING:  # annotations only — never imported at runtime
    from reachy.speech.events import SenseCue


def _sense_cue():
    """The :class:`~reachy.speech.events.SenseCue` type, imported on first use.

    Deliberately NOT a module-scope import. ``_build_parser()`` registers this
    noun, so a top-level ``from reachy.speech.events import SenseCue`` put the
    cognition event bus in the import path of EVERY ``reachy`` invocation --
    ``say run``, ``daemon status``, even ``--help``. ``say`` is specified as a
    dumb TTS pipe and its boundary test forbids exactly that. Found by t24's
    import-boundary suite.
    """
    from reachy.speech.events import SenseCue

    return SenseCue


logger = logging.getLogger(__name__)

_JSON_HELP = "Emit structured JSON."

# Sound-direction band + loudness threshold — mirror reachy.speech.events'
# DoA convention (0 = left, pi/2 = front, pi = right; ~15° "ahead" band) and its
# loud-sound floor, so the agent's perception vocabulary matches the folded
# retired listen --live cognition path exactly.
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
    it drove the retired folded ``listen --live`` sense buffer. Runtime events are pushed
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
                self._buf.append(_sense_cue()(text=text, timestamp=ts))
        for text in cues:
            senselog.stage("cue", "runtime", uuid.uuid4().hex[:8], text)
        return len(cues)

    def feed_forge(self, text: str) -> None:
        """Append one forge self-extension lifecycle cue (the ``announce`` seam).

        Mirrors :meth:`reachy.speech.events.EventBuffer.feed_forge` — the method the
        retired folded ``listen --live`` cognition path wired as
        :class:`~reachy.forge.activate.ForgeActivator`'s ``announce`` — so a skill the
        agent forged announces itself back as a perception ("learned a new skill:
        <name>") and the agent discovers on its next snapshot that it gained a tool.
        The text is already a complete human-readable sentence, so (unlike a runtime
        event) it is passed through verbatim. Empty/whitespace/``None`` yields no cue;
        never raises, so a fault here can never break activation.
        """
        if not text or not str(text).strip():
            return
        cue = str(text).strip()
        with self._lock:
            self._buf.append(_sense_cue()(text=cue, timestamp=0.0))
        senselog.stage("cue", "forge", uuid.uuid4().hex[:8], cue)

    def snapshot(self) -> list[SenseCue]:
        """Return all buffered cues (oldest first) and atomically clear the buffer."""
        with self._lock:
            old = self._buf
            self._buf = deque(maxlen=self._maxlen)
        return list(old)


# ---------------------------------------------------------------------------
# Forge composition — runtime self-extension, wired as INJECTED callables
# ---------------------------------------------------------------------------
#
# ``reachy.forge`` lets an agent turn hand a natural-language goal to a coder model and,
# if the generated code passes the fail-closed AST validator, gain a new callable tool
# with no restart. ``agent attach`` is its composition site: the sanctioned external
# cognition surface.
#
# IMPORT BOUNDARY (asserted by tests/test_speech_tools.py, tests/test_agent_turn.py and
# tests/test_agent_forge.py): :mod:`reachy.speech.tools` and
# :mod:`reachy.speech.agent_turn` must NEVER import :mod:`reachy.forge`. The dispatch
# seam and the register/announce callbacks are plain callables INJECTED here, and even
# here every ``reachy.forge`` import is function-local — so a missing or broken forge
# stack disables only the forge tool and can never break importing this noun.


def _forge_stack_available() -> bool:
    """Whether the forge self-extension stack imports (advertise the tool only if so)."""
    try:
        import reachy.forge  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _default_forge_client_factory(publish: Callable[[str, dict], None]) -> object:
    """Build the production :class:`~reachy.forge.client.ForgeClient` over *publish*.

    Threaded the DEFAULT sanctioned ``ctx`` surface so the client's validator gates
    generated code against exactly the attributes :class:`ForgedSkillContext` exposes.
    Tests inject a substitute factory (see ``forge_client_factory``) so no coder-model
    endpoint is ever contacted.
    """
    from reachy.forge import ForgeClient
    from reachy.forge.validator import DEFAULT_ALLOWED_CTX_ATTRS

    return ForgeClient(publish=publish, allowed_ctx_attrs=set(DEFAULT_ALLOWED_CTX_ATTRS))


def _activate_forge(
    registry: object,
    buffer: object,
    holder: list,
    *,
    express: Callable[[str], object],
    speak_engine: object,
    harmonic_engine: object,
    play: Callable[..., None],
    run_behavior: Callable[..., str],
    client_factory: Callable[[Callable[[str, dict], None]], object] | None = None,
) -> None:
    """Wire the forge auto-activation subsystem for the agent registry (best-effort).

    Builds the restricted :class:`~reachy.forge.ForgedSkillContext` over the SAME
    publish-only seams this noun's built-in ``speak`` / ``harmonics`` / ``apply_pose``
    tools use (the external client never opens the robot's SDK — the runtime loop owns
    the robot), a register callback that HOT-registers a forged skill into the LIVE
    ``registry``, a :class:`~reachy.forge.ForgeActivator` (validator-gated AUTO-activation
    on ``forge/staged`` + boot reload of ``active/``), and a
    :class:`~reachy.forge.ForgeClient` whose ``publish`` IS the activator. Finally arms
    the late-bound dispatch seam by appending the client to ``holder``.

    ``run_behavior`` is the ctx's one non-inert seam. Because this client is
    publish-only by design, a forged skill's ``ctx.speak`` / ``ctx.harmonics`` /
    ``ctx.express`` render nothing — so without an actuator the forge's own premise
    ("the robot gains a new callable tool") would be only half true here. The seam is
    :func:`reachy.speech.intent_tools.make_run_behavior_effector`'s callable: the forged
    skill reaches the robot exactly as this noun's own tools do, by submitting a bounded
    intent to the spool the running engine drains — never by touching the SDK.

    The ``announce`` seam is the cue buffer's :meth:`_RuntimeCueBuffer.feed_forge` — kept
    a plain callable, so the forge modules never import the event bus. A failure disables
    only the forge tool; cognition keeps running.
    """
    try:
        from reachy.forge import ForgeActivator, build_ctx_seams
        from reachy.speech.tools import function_tool

        def _register(name: str, description: str, parameters: dict, handler: object) -> None:
            registry.register(
                function_tool(
                    name=name, description=description, parameters=parameters, handler=handler
                )
            )

        ctx = build_ctx_seams(
            speak_engine=speak_engine,
            harmonic_engine=harmonic_engine,
            play=play,
            express=express,
            run_behavior=run_behavior,
        )
        announce = getattr(buffer, "feed_forge", None)
        activator = ForgeActivator(register=_register, ctx=ctx, announce=announce)
        # Boot reload: any active/<name> forged skill re-registers now (idempotent), so a
        # skill forged before a restart is callable again on the very first turn.
        activator.reload_active()
        factory = client_factory if client_factory is not None else _default_forge_client_factory
        holder.append(factory(activator.publish))
    except Exception:  # noqa: BLE001
        logger.warning(
            "agent attach: forge subsystem unavailable; self-extension disabled",
            exc_info=True,
        )


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
    forge_client_factory: Callable[[Callable[[str, dict], None]], object] | None = None,
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
    * the ``forge`` **self-extension** tool — advertised only when
      :func:`_forge_stack_available`, wired as a late-bound dispatch seam (see
      :func:`_activate_forge`) so the registry can list ``forge`` *before* the
      :class:`~reachy.forge.client.ForgeClient` (which needs the already-built registry,
      via the activator's register callback) exists.

    ``turn_fn`` is the LLM turn function; ``None`` (the default) lets
    :class:`AgentTurnEngine` use :func:`reachy.speech.llm.stream_turn` over the
    ``REACHY_OPENAI_*`` config. ``forge_client_factory`` is the matching seam for the
    coder-model client (``None`` → :func:`_default_forge_client_factory`). Tests build
    this same engine with a fake ``turn_fn`` and a fake forge client so no network is
    ever hit. Lazy-imported so registering the noun stays cheap.
    """
    from reachy.speech.agent_turn import AgentTurnEngine
    from reachy.speech.intent_tools import make_run_behavior_effector, register_intent_tools
    from reachy.speech.tools import ToolRegistry
    from reachy.speech.voice import VoiceEngine

    def _silent_synth(_text: str) -> bytes:
        return b""

    def _no_play(_pcm: object, **_kw: object) -> None:
        return None

    def _no_express(_emoji: str) -> None:
        return None

    # Build the publish-only seams ONCE and share them between the built-in tools and
    # the forged-skill ctx, so a forged skill's ctx.speak / ctx.express render through
    # exactly the same (inert, no-SDK) seams the speak / apply_pose tools use.
    speak_engine = VoiceEngine(name="tts", synthesize=_silent_synth, samplerate=_TTS_RATE)
    harmonic_engine = VoiceEngine(
        name="harmonic", synthesize=_silent_synth, samplerate=_HARMONIC_RATE
    )

    registry_kwargs: dict[str, object] = {
        "express": _no_express,
        "speak_engine": speak_engine,
        "harmonic_engine": harmonic_engine,
        "play": _no_play,
    }

    # The forge self-extension tool: a late-bound dispatch seam that dereferences the
    # holder only at CALL time, so the registry can be constructed with `forge` listed
    # before the ForgeClient (which needs this very registry) exists. Only advertised
    # when the forge stack imports; an unarmed holder degrades the tool to an inert
    # no-op rather than raising into a turn.
    forge_holder: list = []
    if _forge_stack_available():

        def _forge_seam(goal: str, improve: str | None = None) -> object:
            if not forge_holder:
                return None
            return forge_holder[0].dispatch(goal, improve=improve)

        registry_kwargs["forge"] = _forge_seam

    registry = ToolRegistry(**registry_kwargs)
    register_intent_tools(
        registry, spool_dir=spool_dir, await_timeout=await_timeout, modes=tuple(modes)
    )

    if "forge" in registry_kwargs:
        # After the registry exists: wire the activation subsystem + ForgeClient and arm
        # the seam. Best-effort by construction — and belt-and-braces here too, so even a
        # hard failure in composition leaves cognition (intents + publish-only tools)
        # fully running with the forge tool merely inert.
        try:
            _activate_forge(
                registry,
                buffer,
                forge_holder,
                express=_no_express,
                speak_engine=speak_engine,
                harmonic_engine=harmonic_engine,
                play=_no_play,
                # The forged skill's ONE actuator: the same bounded, validated
                # run_behavior admission the registry's own intent tool submits, over
                # the same spool — never a second path to the robot.
                run_behavior=make_run_behavior_effector(
                    spool_dir=spool_dir, await_timeout=await_timeout
                ),
                client_factory=forge_client_factory,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "agent attach: forge composition failed; self-extension disabled",
                exc_info=True,
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
    # The shared exporter builder — the wire contract matches the schema doc.
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


# ===========================================================================
# embody — the embodiment layer's composition root (task t11)
# ===========================================================================
#
# Everything below composes pieces that already exist and are already tested.
# The value this section adds is the JOINS, and the arc has already paid once
# for getting a join wrong quietly: t4 wrote the audio tee's wire with a JSON
# header and float32 samples while t6 independently read it as headerless
# int16. Nothing raised — the reader simply heard noise — because no test
# connected the two ends. So the wirings here are stated, not implied:
#
#   media.source.read   -> duplex read_audio      (the layer's EARS)
#   media.sink.play     -> duplex play            (the layer's MOUTH)
#   media.sink.play     -> the speak/harmonics tools' render leg
#   duplex on_utterance -> engine.submit_utterance   (a TRIGGER)
#   duplex on_response  -> engine.note_spoken        (CONTEXT, never a trigger)
#   runtime feed line   -> cues_for_line -> engine.submit_cues
#   engine + every layer failure -> the shared --export cognition feed
#
# IMPORT BOUNDARY (h15). Every import of the layer, the realtime client and the
# speech stack in this section is FUNCTION-LOCAL, for the same reason ``attach``
# defers ``SenseCue``: ``_build_parser()`` imports every command module, so one
# module-scope import here would put an LLM client and a WebSocket client on the
# import path of ``say run``, ``daemon status`` and ``--help``.
# ``tests/test_agent_embody.py`` pins both the syntactic form and the
# fresh-interpreter behaviour, and the runtime's own closure is pinned to gain
# no edge to the layer at all.
#
# NO ``reachy_mini`` (h14). Nothing here — or anywhere it can reach at run
# time — constructs a ``ReachyMini``. The layer hears through the runtime's tee
# socket and speaks through the daemon's HTTP media route; the single-SDK-owner
# model gives the SDK itself to the runtime process alone.

#: ``[SENSE stage=embody source=<...> event=<id>]`` — the composition root's own
#: journal identity, distinct from the engine's ``turn``, the session's
#: ``duplex`` and the tools' ``action`` so one journal can be split by layer.
EMBODY_STAGE = "embody"

EMBODY_SOURCE_VOICE = "voice"
EMBODY_SOURCE_SESSION = "session"
EMBODY_SOURCE_CUES = "cue-reader"
EMBODY_SOURCE_SHUTDOWN = "shutdown"

#: A voice engine would not resolve at composition, so that tool refuses for the
#: life of the process rather than the tool set changing shape per box.
REASON_VOICE_UNAVAILABLE = "voice-unavailable"
#: Synthesis or playback raised — a wedged TTS, a dead speaker, a refused
#: daemon route. The model is told (a named refusal) and the feed shows it.
REASON_SPEAK_FAILED = "speak-failed"
#: The duplex session refused to start; the layer is deaf and mute but alive.
REASON_SESSION_START_FAILED = "session-start-failed"
#: The runtime line source raised mid-stream (the feed went away, the bus died).
REASON_CUE_SOURCE_FAILED = "cue-source-failed"
#: Closing a held resource raised. Named, never propagated — a fault in teardown
#: must not mask the reason the layer was stopping.
REASON_SHUTDOWN_FAILED = "shutdown-failed"

#: Every failure this composition root can name, in one place so the journal,
#: the export feed, the operator docs and the tests share ONE vocabulary — the
#: same discipline :mod:`reachy.embody.tools` and :mod:`reachy.embody.engine`
#: each keep for their own layer.
EMBODY_REASONS: frozenset[str] = frozenset(
    {
        REASON_VOICE_UNAVAILABLE,
        REASON_SPEAK_FAILED,
        REASON_SESSION_START_FAILED,
        REASON_CUE_SOURCE_FAILED,
        REASON_SHUTDOWN_FAILED,
    }
)

#: The two voices the layer composes, by :mod:`reachy.speech.voice` engine name.
_EMBODY_VOICES = ("tts", "harmonic")

EMBODY_READER_THREAD_NAME = "embody-cue-reader"

#: Default gap between turns when nothing is pending (seconds).
DEFAULT_TURN_INTERVAL = 0.5


def _embody_drop(export: object, source: str, reason: str, detail: str = "") -> None:
    """Name one layer failure on the journal AND on the export feed (h22).

    The house rule is "no silent no-op anywhere"; the layer's observability
    requirement (spec c27) adds "and a conversational robot with an invisible
    mind is undebuggable". So a failure is reported TWICE, deliberately: once as
    the grep-able ``[SENSE stage=embody …]`` line every other subsystem emits,
    and once as a ``thinking`` block on the operator's ``--export`` feed, which
    is the only surface a remote consumer (the reTerminal panel, a log tail)
    ever sees.

    ``export`` is optional — a run without ``--export`` still journals — and a
    broken export consumer is already handled inside
    :class:`~reachy.export.exporter.JsonlExporter`, which latches itself off
    rather than raising. This function therefore never raises.
    """
    detail = detail.strip()
    senselog.drop(
        EMBODY_STAGE,
        source,
        uuid.uuid4().hex[:8],
        f"{reason} ({detail})" if detail else reason,
    )
    if export is None:
        return
    try:
        export.emit(
            ThinkingEvent(
                cues=[f"[drop] {source}: {reason}"],
                text=f"[drop reason={reason} source={source}" + (f" {detail}]" if detail else "]"),
                ts=export.time_fn(),
            )
        )
    except Exception:  # noqa: BLE001 — observability must never break the layer
        logger.warning("[embody] export sink raised while naming %s", reason, exc_info=True)


# ---------------------------------------------------------------------------
# The voice seams — the layer's mouth for its own DELIBERATE utterances
# ---------------------------------------------------------------------------
#
# Distinct from the duplex session's mouth: that one plays the SERVER's spoken
# reply, this one renders a ``speak`` / ``harmonics`` tool call. Both end at the
# same :class:`~reachy.embody.media.EmbodySink`, which is what keeps the
# profile decision (daemon-http on the robot, monitor speakers on the bench) in
# exactly one place.


def _voice_seam(
    name: str,
    synthesize: Callable[[str], bytes],
    samplerate: int,
    sink: object,
    *,
    export: object,
) -> Callable[[str], str]:
    """Build ``seam(text) -> str``: synthesize, play through *sink*, or name it.

    A failure is named with the layer's own precise reason AND re-raised as the
    tool layer's :class:`~reachy.embody.tools.Refusal`, so the model is told in
    the same turn that its mouth did not work. A failure the model cannot see is
    not a failure — it is a robot that believes it spoke.
    """

    def _speak(text: str) -> str:
        from reachy.embody.tools import REFUSAL_TOOL_ERROR, Refusal

        try:
            pcm = synthesize(text)
            sink.play(pcm, samplerate=samplerate)
        except Exception as err:  # noqa: BLE001 — every voice fault is NAMED, never raw
            _embody_drop(
                export,
                EMBODY_SOURCE_VOICE,
                REASON_SPEAK_FAILED,
                f"{name}: {type(err).__name__}: {err}",
            )
            raise Refusal(REFUSAL_TOOL_ERROR, f"the {name} voice failed: {err}") from err
        return f"{len(pcm)} bytes at {samplerate} Hz"

    return _speak


def _build_voice_seams(
    sink: object,
    *,
    export: object,
    synthesize: Mapping[str, Callable[[str], bytes]] | None = None,
) -> tuple[Callable[[str], str] | None, Callable[[str], str] | None]:
    """The ``(speak, harmonics)`` pair for :class:`EmbodyToolRegistry`.

    Both legs reuse :func:`reachy.speech.voice.resolve_voice_engine` — the same
    registry ``say`` and the runtime's ``SpeechActuator`` resolve through — so
    the layer inherits their synthesis and their sample rates rather than
    inventing a third pair. ``synthesize`` overrides the callable per engine
    name (tests only); the rate always comes from the resolved engine.

    A voice that will not resolve yields ``None``, which leaves the tool
    ADVERTISED but refusing by name — the layer's action set must not change
    shape with the box's audio configuration, or the model learns a different
    robot on every start (``reachy/embody/tools.py``'s own reasoning).
    """
    from reachy.speech.voice import resolve_voice_engine

    overrides = dict(synthesize or {})
    seams: dict[str, Callable[[str], str] | None] = {}
    for name in _EMBODY_VOICES:
        try:
            engine = resolve_voice_engine(name)
        except Exception as err:  # noqa: BLE001 — a missing voice is not a dead layer
            _embody_drop(export, EMBODY_SOURCE_VOICE, REASON_VOICE_UNAVAILABLE, f"{name}: {err}")
            seams[name] = None
            continue
        seams[name] = _voice_seam(
            name,
            overrides.get(name, engine.synthesize),
            engine.samplerate,
            sink,
            export=export,
        )
    return seams["tts"], seams["harmonic"]


# ---------------------------------------------------------------------------
# The cue reader — the runtime's own reflex life, on a background thread
# ---------------------------------------------------------------------------


class _CueReader:
    """Drain :func:`reachy.embody.cues.open_runtime_lines` into the turn engine.

    A THREAD, because the intake is blocking by construction: the bus route
    blocks on a queue and the feed-tail route blocks on a FIFO/stdin read. The
    ears (the duplex session) must keep producing utterances while the feed is
    quiet, so pumping lines on the turn loop's own thread would make a silent
    runtime a deaf layer. Daemon, so a blocked read can never hold up
    interpreter exit.

    Everything it touches is O(1) and non-raising:
    :meth:`~reachy.embody.engine.EmbodyTurnEngine.submit_cues` appends to a
    bounded deque and names its own overflow.
    """

    def __init__(
        self,
        lines: Iterable[str],
        engine: object,
        *,
        export: object = None,
        max_events: int | None = None,
    ) -> None:
        self._lines = lines
        self._engine = engine
        self._export = export
        self._max_events = max_events
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.events = 0
        self.cues = 0
        #: Set LAST, after every cue of the final line has been submitted, so a
        #: reader that is ``done`` with an empty engine really has nothing more
        #: to give — which is exactly what :meth:`_EmbodyLayer.should_stop`
        #: keys on to end a bounded run.
        self.done = False

    def start(self) -> None:
        """Start the reader thread. Idempotent."""
        if self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self._run, name=EMBODY_READER_THREAD_NAME, daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        """Ask the reader to stop after the line it is on. Never blocks."""
        self._stop.set()

    def _run(self) -> None:
        from reachy.embody.cues import cues_for_line

        try:
            for line in self._lines:
                if self._stop.is_set():
                    break
                self.events += 1
                self.cues += self._engine.submit_cues(cues_for_line(line))
                if self._max_events is not None and self.events >= self._max_events:
                    break
        except Exception as err:  # noqa: BLE001 — a dead feed must not kill the conversation
            _embody_drop(
                self._export,
                EMBODY_SOURCE_CUES,
                REASON_CUE_SOURCE_FAILED,
                f"{type(err).__name__}: {err}",
            )
        finally:
            self.done = True


# ---------------------------------------------------------------------------
# The composed layer
# ---------------------------------------------------------------------------


class _EmbodyLayer:
    """Everything one ``agent embody`` process holds, and how it shuts down."""

    def __init__(
        self,
        *,
        profile: str,
        media: object,
        session: object,
        registry: object,
        engine: object,
        reader: _CueReader,
        export: object = None,
    ) -> None:
        self.profile = profile
        self.media = media
        self.session = session
        self.registry = registry
        self.engine = engine
        self.reader = reader
        self._export = export
        self._stop = threading.Event()
        self._closed = False

    def start(self) -> None:
        """Open the ears/mouth and start reading the runtime's reflex life.

        A session that will not start is a NAMED drop, not a raise: a gateway
        that is not up yet is the ordinary resting state of a boot-persistent
        box, and a layer that can still think about its own reflexes is worth
        more than one that exits.
        """
        try:
            self.session.start()
        except Exception as err:  # noqa: BLE001 — a dead gateway is deaf, not fatal
            _embody_drop(
                self._export,
                EMBODY_SOURCE_SESSION,
                REASON_SESSION_START_FAILED,
                f"{type(err).__name__}: {err}",
            )
        self.reader.start()

    def request_stop(self) -> None:
        """Ask the run loop to finish after the current turn. Safe from any thread."""
        self._stop.set()

    def should_stop(self) -> bool:
        """The run loop's ``stop`` predicate.

        Two ways a run ends. An explicit stop (``Ctrl-C``, :meth:`close`) always
        wins. Otherwise the run ends when the LINE SOURCE has run dry and the
        engine has nothing left to think about — which is what makes ``--feed
        <file>`` a bounded run, and what makes ``--feed -`` end when the
        runtime writing it goes away. In production the feed is a FIFO or stdin
        held open by a live runtime, so this is false for the whole life of the
        robot.
        """
        if self._stop.is_set():
            return True
        return self.reader.done and self.engine.pending == 0

    def close(self) -> None:
        """Release everything, in order, naming any fault. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self.reader.stop()
        for label, closer in (("session", self.session.close), ("media", self.media.close)):
            try:
                closer()
            except Exception as err:  # noqa: BLE001 — teardown names faults, never raises
                _embody_drop(
                    self._export,
                    EMBODY_SOURCE_SHUTDOWN,
                    REASON_SHUTDOWN_FAILED,
                    f"{label}: {type(err).__name__}: {err}",
                )


def _utterance_tap(engine: object) -> Callable[[object], None]:
    """duplex ``on_utterance`` -> a TRIGGER. Ungated: the layer hears everyone."""

    def _heard(utterance: object) -> None:
        engine.submit_utterance(getattr(utterance, "text", "") or "")

    return _heard


def _response_tap(engine: object) -> Callable[[object], None]:
    """duplex ``on_response`` -> already-said CONTEXT, never a trigger.

    The duplex session answers speech on its own, server-side, so without this
    the thinking mind would not know its own mouth had already replied and
    would cheerfully call ``speak`` to say it again (the wiring t10's docstring
    singles out as the one this composition could miss).

    An INTERRUPTED reply is deliberately excluded. ``_finish_response``
    publishes it like any other — the record carries the audio and says why —
    but never PLAYS it, because a barge-in means the human started talking
    again. Recording it as spoken would leave the mind believing it had
    answered when the room heard nothing.
    """

    def _spoke(response: object) -> None:
        if getattr(response, "interrupted", False):
            return
        engine.note_spoken(getattr(response, "text", "") or "")

    return _spoke


def _require_readable_feed(feed: str) -> None:
    """Pre-flight the fallback intake route: a typo'd feed is an exit-2, not a drop.

    ``os.access`` rather than an ``open``: opening a FIFO for reading BLOCKS
    until a writer appears, and pre-flighting must not hang on the very thing
    it is checking.

    This checks the feed-tail route because that is the only intake route that
    exists today — events-cli is publish-only (upstream events-cli#14), so
    :func:`reachy.embody.cues.open_runtime_lines` always falls back to it after
    one named drop. If a bus subscriber ever lands, this pre-flight moves
    behind the same decision.
    """
    if feed == "-" or os.access(feed, os.R_OK):
        return
    raise CliError(
        code=EXIT_ENV_ERROR,
        message=f"cannot read runtime feed {feed!r}",
        remediation="pass --feed - to read from stdin, or a readable path/FIFO the "
        "runtime writes (behavior engine run --export -)",
    )


def _compose_embody_seam(
    args: argparse.Namespace,
    *,
    export: object = None,
    media: object = None,
    session_factory: Callable[..., object] | None = None,
    registry_factory: Callable[..., object] | None = None,
    engine_factory: Callable[..., object] | None = None,
    turn_fn: object | None = None,
    synthesize: Mapping[str, Callable[[str], bytes]] | None = None,
    lines: Iterable[str] | None = None,
    stdin: TextIO | None = None,
) -> _EmbodyLayer:
    """Build the whole layer — the ONE place the wave-1/2/3 seams meet.

    Every collaborator is injectable so the composition is exercised with no
    gateway, no broker, no robot, no tee socket and no audio device; the
    defaults are the real thing.

    Order matters in one place only: the media profile is built FIRST, because
    its sink is shared by three consumers (the duplex mouth, and both voice
    tools) and its source is the duplex ears. Building it twice would give the
    layer two mouths and, on the robot profile, two readers contending for one
    tee socket.
    """
    from reachy.embody.cues import open_runtime_lines
    from reachy.embody.engine import EmbodyTurnEngine
    from reachy.embody.media import build_media
    from reachy.embody.tools import EmbodyToolRegistry
    from reachy.speech.realtime_duplex import RealtimeDuplexSession

    resolved_media = (
        media if media is not None else build_media(getattr(args, "media_profile", None))
    )

    speak_seam, harmonic_seam = _build_voice_seams(
        resolved_media.sink, export=export, synthesize=synthesize
    )

    build_registry = registry_factory if registry_factory is not None else EmbodyToolRegistry
    registry = build_registry(
        speak=speak_seam,
        harmonics=harmonic_seam,
        spool_root=_resolve_spool_dir(args),
        await_timeout=float(getattr(args, "await_timeout", 1.0)),
    )

    build_engine = engine_factory if engine_factory is not None else EmbodyTurnEngine
    engine_kwargs: dict[str, object] = {
        "registry": registry,
        "export": export,
        "turn_interval": float(getattr(args, "turn_interval", DEFAULT_TURN_INTERVAL)),
    }
    if turn_fn is not None:
        engine_kwargs["turn_fn"] = turn_fn
    engine = build_engine(**engine_kwargs)

    build_session = session_factory if session_factory is not None else RealtimeDuplexSession
    session = build_session(
        read_audio=resolved_media.source.read,
        sample_rate=resolved_media.source.sample_rate,
        play=resolved_media.sink.play,
        mute_during_playback=bool(getattr(args, "mute_during_playback", False)),
        on_utterance=_utterance_tap(engine),
        on_response=_response_tap(engine),
    )

    feed_lines = (
        lines
        if lines is not None
        else open_runtime_lines(feed=getattr(args, "feed", "-"), stdin=stdin)
    )
    reader = _CueReader(
        feed_lines, engine, export=export, max_events=getattr(args, "max_events", None)
    )

    return _EmbodyLayer(
        profile=getattr(resolved_media, "profile", "unknown"),
        media=resolved_media,
        session=session,
        registry=registry,
        engine=engine,
        reader=reader,
        export=export,
    )


def cmd_agent_embody(
    args: argparse.Namespace,
    *,
    stream: TextIO | None = None,
    compose: Callable[..., _EmbodyLayer] | None = None,
) -> int:
    """Run the embodiment layer in the foreground: hear, think, act, speak.

    ``compose`` is the one injection seam the verb itself takes — tests hand in
    a :func:`_compose_embody_seam` closure carrying fakes, production takes the
    default. ``stream`` is the ``--export`` sink (default stdout).

    Logging is installed here on purpose. Every named failure this layer can
    produce is a ``[SENSE …]`` line at INFO, and a process with no handler on
    the ``reachy`` logger drops those on the floor — so without this the
    observability contract (spec c27) would be satisfied on paper and invisible
    in practice. Stderr-only by construction, so ``--export -``'s stdout stays
    pure JSONL.
    """
    install_logging(getattr(args, "log_level", None))
    json_mode = bool(getattr(args, "json", False))

    export_hook = build_export_hook(args, stream=stream)
    _require_readable_feed(str(getattr(args, "feed", "-")))

    builder = compose if compose is not None else _compose_embody_seam
    layer = builder(args, export=export_hook)

    emit_diagnostic(
        f"[embody] layer up: profile={layer.profile}; ears+mouth on one realtime "
        "session, cognition on the streaming HTTP lane, actions via the spools"
    )

    turns = 0
    try:
        layer.start()
        turns = layer.engine.run(max_turns=getattr(args, "max_turns", None), stop=layer.should_stop)
    except KeyboardInterrupt:
        layer.request_stop()
        emit_diagnostic("[embody] interrupted; shutting the layer down")
    finally:
        layer.close()

    stats = {
        "profile": layer.profile,
        "turns": turns,
        "events": layer.reader.events,
        "cues": layer.reader.cues,
        "utterances": int(getattr(layer.session, "utterances", 0) or 0),
    }
    if json_mode and export_hook is None:
        emit_result({"status": "ok", **stats}, json_mode=True)
    else:
        emit_diagnostic(
            f"[embody] layer down: {stats['turns']} turn(s), {stats['cues']} cue(s) "
            f"over {stats['events']} runtime event(s), {stats['utterances']} utterance(s)"
        )
    return 0


# ---------------------------------------------------------------------------
# overview (rubric-required; hand-built — no transport line)
# ---------------------------------------------------------------------------

_VERBS = [
    "agent attach — read the runtime event feed, act via the intent spool, "
    "publish the agent's own cognition feed",
    "agent embody — run the embodiment layer: hear and speak out loud over one "
    "realtime duplex session, think over the streaming HTTP lane, act through "
    "the direct-operation action set",
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
                "SELF-EXTENSION: the `forge` tool hands a natural-language goal to a "
                "coder model; generated code must pass a fail-closed AST validator "
                "before it is auto-activated and becomes callable on the NEXT turn "
                "(and again after a restart, reloaded from the active/ store).",
                "OUTPUT: the agent publishes its OWN thinking/message/emotion feed via "
                "--export - (decision c27: the runtime feed carries no cognition block).",
                "Detaching changes nothing about the loop — it keeps ticking and its "
                "rules keep running, agent attached or not.",
                "EMBODY (the other verb): the embodiment layer — a detachable realtime "
                "harness that HEARS and SPEAKS out loud over ONE lobes /v1/realtime "
                "duplex session (ungated: it hears everyone), thinks over the streaming "
                "/v1/chat/completions lane, and operates the robot through the closed "
                "five-tool direct-operation action set (goto / speak / harmonics / "
                "run_behavior / create_rule). It constructs no ReachyMini: audio comes "
                "from the runtime's tee socket and goes out the daemon's HTTP media "
                "route. Enable or disable it at will — the runtime is unchanged either "
                "way.",
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

    embody = noun_sub.add_parser(
        "embody",
        help="Run the embodiment layer: hear and speak out loud over one realtime "
        "duplex session, think over the streaming HTTP lane, act via the "
        "direct-operation action set.",
    )
    embody.add_argument(
        "--feed",
        default="-",
        metavar="PATH",
        help="Runtime-event JSONL source: a path (stream/FIFO/file) or '-' for stdin "
        "(default). The layer never spawns the runtime; it reads what the runtime "
        "writes (behavior engine run --export -). The MQTT bus is the intended "
        "primary route and falls back here today (events-cli is publish-only).",
    )
    embody.add_argument(
        "--media-profile",
        default=None,
        dest="media_profile",
        metavar="PROFILE",
        help="Audio profile: 'robot' (the runtime's tee socket in, the daemon HTTP "
        "media route out — the default) or 'bench' (dev-box mic + speakers). "
        "Overrides REACHY_EMBODY_MEDIA_PROFILE. Same code path either way.",
    )
    embody.add_argument(
        "--spool-dir",
        default=None,
        dest="spool_dir",
        metavar="DIR",
        help="Override the intents-spool root (default: the shared state dir, so the "
        "layer writes into the SAME spool the running engine drains).",
    )
    embody.add_argument(
        "--await-timeout",
        type=float,
        default=1.0,
        dest="await_timeout",
        help="Seconds an action waits for the engine to confirm a spool command "
        "before returning 'submitted, unconfirmed' (default: 1.0).",
    )
    embody.add_argument(
        "--turn-interval",
        type=float,
        default=DEFAULT_TURN_INTERVAL,
        dest="turn_interval",
        help=f"Seconds to wait between turns when nothing is pending "
        f"(default: {DEFAULT_TURN_INTERVAL}).",
    )
    embody.add_argument(
        "--mute-during-playback",
        action="store_true",
        dest="mute_during_playback",
        help="Withhold microphone audio while the layer is speaking. OFF by default: "
        "Reachy has hardware AEC against its own speakers, so the layer keeps "
        "hearing while it talks (which is what makes barge-in possible). This is "
        "the one-flip fallback if live AEC proves insufficient.",
    )
    embody.add_argument(
        "--max-turns",
        type=int,
        default=None,
        dest="max_turns",
        help="Stop after this many turns that ran (default: unbounded).",
    )
    embody.add_argument(
        "--max-events",
        type=int,
        default=None,
        dest="max_events",
        help="Stop reading runtime events after this many (default: unbounded).",
    )
    add_export_args(embody)
    add_log_level_arg(embody)
    embody.add_argument("--json", action="store_true", help=_JSON_HELP)
    embody.set_defaults(func=cmd_agent_embody)
