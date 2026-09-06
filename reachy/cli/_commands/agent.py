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

The mapping itself (the per-type functions above, plus the DoA band / loudness
floor / pat-phrasing constants they use) lives in :mod:`reachy.runtime_cues`,
shared with the embodiment layer's own cue reader
(:mod:`reachy.embody.cues`) — SonarCloud flagged the two as duplicated
blocks on PR #140. See that module's docstring for exactly what is shared and
what deliberately stays local to each caller (this module's dispatch never
logs a drop for an unrecognised event; ``reachy.embody.cues`` does).
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from reachy import runtime_cues, senselog
from reachy.cli._commands._robot import emit_payload
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

# Inert voice sample rates for the publish-only speak/harmonics tools (a no-op
# playback ignores them — they only satisfy the VoiceEngine shape).
_TTS_RATE = 24000
_HARMONIC_RATE = 16000


# ---------------------------------------------------------------------------
# Runtime-event → perception-cue mapping
# ---------------------------------------------------------------------------
#
# The per-type mapper functions, the DoA band / loudness floor / pat-phrasing
# constants they use, and the JSONL line parser all live in
# :mod:`reachy.runtime_cues` — the shared owner cited by both this module's
# ``_CUE_MAPPERS`` and :mod:`reachy.embody.cues`'s ``CUE_MAPPERS`` (SonarCloud
# PR #140: the two were duplicated blocks before this extraction). See that
# module's docstring for exactly what is shared and what is not — in
# particular, this dispatch stays silent on an unrecognised/malformed event
# where ``reachy.embody.cues`` logs a named drop; that difference is
# deliberate and is NOT flattened by sharing the per-type mappers.


_CUE_MAPPERS: dict[str, Callable[[dict], list[str]]] = {
    "sense": runtime_cues.sense_cues,
    "rule": runtime_cues.rule_cues,
    "intent": runtime_cues.intent_cues,
    "motion": runtime_cues.motion_cues,
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
    """Parse one JSONL feed line into an event dict, or ``None`` for junk/blank.

    Delegates to the shared :func:`reachy.runtime_cues.parse_runtime_line` —
    identical logic to :func:`reachy.embody.cues.parse_runtime_line`.
    """
    return runtime_cues.parse_runtime_line(line)


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
    except Exception:
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
    except Exception:
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
        except Exception:
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
#   media.sink.play     -> duplex play            (the layer's ONLY MOUTH: the
#                          FOREGROUND voice's own reply — c2. The speak /
#                          harmonics tools used to render here too and no
#                          longer do; see "The interjection route" below)
#   speak / harmonics   -> InterjectionPolicy.admit -> engine.note_interjection
#                          (a PROPOSAL: refused by name, or parked as a
#                          speakable cognition scope. Never audio — c2/h1)
#   interjection line   -> InterjectionPolicy.admit_event -> the same, with
#                          alert=True (an EXTERNAL proposal may wake the mind)
#   duplex on_utterance -> engine.submit_utterance   (a TRIGGER, if attention
#                          admits it: the name opens the window — #148)
#                       -> session.arm_once          (ONE spoken reply, and
#                          only for an utterance attention admitted — #149;
#                          the same verdict governs the mind AND the mouth)
#   duplex on_response  -> engine.note_spoken        (CONTEXT, never a trigger;
#                          extends a live attention window, never opens one)
#                       -> engine.note_interrupted_reply  (for a CUT reply: the
#                          measured said half as spoken, the remainder kept as
#                          a wanted-to-say artifact the next turn reads — c34)
#                       -> engine.floor_correction -> session.send_item  (and
#                          the floor is TOLD what the room actually heard, as a
#                          history item — c39's phase-1 overstatement closing)
#   duplex on_speech_started -> session.cancel_playback + the same
#                          note_interrupted_reply  (the TAIL cut: after
#                          response.done the floor can no longer interrupt
#                          anything, and our queue is still draining — c34/c35;
#                          attention deliberately does NOT gate it)
#   duplex reseed       -> engine.floor_reseed       (on EVERY session.created,
#                          BEFORE the arm: the layer curates the canonical
#                          history and pushes a projection of it, so the floor's
#                          own history is what the layer put there — c27/c40)
#   runtime feed line   -> classified_cues_for_runtime_event -> submit_cues
#                          (a rule FIRE triggers; every other cue parks — #143)
#   the shared history  -> SummaryProducer -> engine.update_summary (ONE
#                          summary, Qwen's, on its own thread — c30)
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

EMBODY_SOURCE_SESSION = "session"
EMBODY_SOURCE_CUES = "cue-reader"
EMBODY_SOURCE_SHUTDOWN = "shutdown"
#: The clip -> ``ask()`` perception lane (task t11, issue #139's h9 blocker).
EMBODY_SOURCE_CLIP = "clip"
#: Resolving the names the robot answers to, at composition (#177).
EMBODY_SOURCE_NAMES = "names"

#: The duplex session refused to start; the layer is deaf and mute but alive.
REASON_SESSION_START_FAILED = "session-start-failed"
#: The runtime line source raised mid-stream (the feed went away, the bus died).
REASON_CUE_SOURCE_FAILED = "cue-source-failed"
#: Asking the session for ONE spoken reply raised (issue #149). The utterance
#: was admitted and the room will now hear nothing back, so it cannot be a
#: swallowed exception: the tap runs on the session's own worker thread, where
#: a raise is caught and logged as an anonymous warning at best.
REASON_ARM_FAILED = "arm-failed"
#: Cutting the mouth off raised (task t16). The tap runs on the session's own
#: worker thread, where a raise is an anonymous warning at best — and the room
#: is still hearing a reply someone just talked over, so it cannot be silent.
REASON_TAIL_CUT_FAILED = "tail-cut-failed"
#: Asking the session what the room actually heard of a reply raised (task t7).
#: The tap falls back to the pre-measurement answer — the reply is recorded as
#: spoken unless the server itself said it was interrupted — so the mind is
#: never left with NO record of its own voice; the drop says the record is the
#: coarse one.
REASON_SPLIT_UNAVAILABLE = "cut-split-unavailable"
#: Pushing the layer's canonical record to the floor RAISED (task t11, decision
#: c27). ``send_item`` never raises and answers ``False`` for a gateway that
#: announced no item support — naming that degrade itself, once per session — so
#: this is the socket dying under a correction, not the ordinary no-items
#: gateway. Named because the tap runs on the session's own worker thread, and
#: because a floor left holding an overstated reply is a fact about the
#: conversation the operator should be able to see.
REASON_FLOOR_PUSH_FAILED = "floor-push-failed"
#: Closing a held resource raised. Named, never propagated — a fault in teardown
#: must not mask the reason the layer was stopping.
REASON_SHUTDOWN_FAILED = "shutdown-failed"
#: ``state.json``'s ``clip`` key is missing, unreadable, or names
#: ``available: false`` (no ``[vision]`` extra, no clip encoded yet, ...) — see
#: the block's own ``reason`` field, carried as the drop's detail.
REASON_CLIP_UNAVAILABLE = "clip-unavailable"
#: The clip block IS available but its ``ts`` (a MONOTONIC value from the
#: runtime process — see ``reachy/behavior/clip_rider.py``) is older than
#: :data:`DEFAULT_CLIP_STALE_AFTER_S`: asking about it would tell the mind
#: about a view the robot no longer has.
REASON_CLIP_STALE = "clip-stale"
#: The clip's ``path`` could not be turned into a question — missing, unreadable,
#: or the read otherwise raised.
REASON_CLIP_UNREADABLE = "clip-unreadable"
#: ``ask()`` itself raised (a dead senses-lane gateway, a timeout, ...).
REASON_CLIP_ASK_FAILED = "clip-ask-failed"
#: ``ask()`` returned no usable answer (an empty/blank stream) — nothing worth
#: parking as context.
REASON_CLIP_ASK_EMPTY = "clip-ask-empty"
#: The senses lane answered, but :func:`parse_perception_answer` could not
#: find the requested ``{"summary": ..., "entities": [...], "confidence":
#: ...}`` shape in it (task t13, issue #155 c7). NOT a lost observation: the
#: raw answer text still becomes a summary-only snapshot — this reason names
#: the DEGRADE, never the drop of the whole update. The senses lane is a
#: cheap model answering in free text; assume it will sometimes ignore the
#: requested format.
REASON_CLIP_ANSWER_UNSTRUCTURED = "clip-answer-unstructured"

#: The box-local rules overlay names the robot (#177), and reading it RAISED —
#: bad TOML, an unreadable file, a ``names`` entry the schema refuses. The layer
#: falls back to the SHIPPED names and keeps listening: a robot that will not
#: come up because someone mistyped a name in a config file is a worse failure
#: than one that answers only to "reachy" until the file is fixed. Named because
#: the fallback is otherwise indistinguishable from an overlay that simply
#: configured nothing.
REASON_NAMES_OVERLAY_REFUSED = "names-overlay-refused"

#: Every failure this composition root can name, in one place so the journal,
#: the export feed, the operator docs and the tests share ONE vocabulary — the
#: same discipline :mod:`reachy.embody.tools` and :mod:`reachy.embody.engine`
#: each keep for their own layer.
EMBODY_REASONS: frozenset[str] = frozenset(
    {
        REASON_SESSION_START_FAILED,
        REASON_CUE_SOURCE_FAILED,
        REASON_ARM_FAILED,
        REASON_TAIL_CUT_FAILED,
        REASON_SPLIT_UNAVAILABLE,
        REASON_FLOOR_PUSH_FAILED,
        REASON_SHUTDOWN_FAILED,
        REASON_CLIP_UNAVAILABLE,
        REASON_CLIP_STALE,
        REASON_CLIP_UNREADABLE,
        REASON_CLIP_ASK_FAILED,
        REASON_CLIP_ASK_EMPTY,
        REASON_CLIP_ANSWER_UNSTRUCTURED,
        REASON_NAMES_OVERLAY_REFUSED,
    }
)

EMBODY_READER_THREAD_NAME = "embody-cue-reader"
EMBODY_CLIP_THREAD_NAME = "embody-clip-asker"

#: Default gap between turns when nothing is pending (seconds).
DEFAULT_TURN_INTERVAL = 0.5

#: How often :class:`_ClipAsker` checks ``state.json``'s ``clip`` key. Bounded
#: well above the clip rider's own ``DEFAULT_ENCODE_INTERVAL_S`` (5 s,
#: ``reachy/behavior/clip_rider.py``) — an active runtime republishes a new
#: clip roughly every 5 s, but "ask a model to watch a video" is a genuine
#: senses-lane gateway call, and the #143 flood (23 streaming calls in 40 s
#: from cues ALONE) is exactly the failure mode a second, unbounded lane must
#: not reproduce. Combined with the change-detection in
#: :meth:`_ClipAsker.poll_once` (a clip whose ``ts`` has not moved since the
#: last check is a silent no-op, never a re-ask), this caps the senses lane at
#: one call per this many seconds even while the robot watches continuously.
DEFAULT_CLIP_POLL_INTERVAL_S = 20.0
#: How old a clip's ``ts`` may be (measured on THIS process's own monotonic
#: clock — the value itself came from the runtime process's monotonic clock,
#: which is the same system-wide counter on one host, never wall time; see
#: ``clip_rider.py``'s module docstring) before :class:`_ClipAsker` refuses to
#: ask about it. ~6x the rider's own encode cadence: loose enough to absorb
#: ordinary jitter between two independent processes, tight enough that a
#: runtime that has stopped publishing for half a minute reads as "no longer
#: the robot's current view" rather than as one.
DEFAULT_CLIP_STALE_AFTER_S = 30.0

#: Seconds ``agent embody stop`` waits after SIGTERM before escalating to
#: SIGKILL (task t12). Mirrors :data:`reachy.embody.supervisor.
#: DEFAULT_STOP_TIMEOUT` by VALUE, not by import: a command module registering
#: this noun's CLI flags must not import :mod:`reachy.embody` at module scope
#: (``tests/test_agent_embody.py``'s ``test_no_cognition_or_layer_module_is_
#: imported_at_command_module_scope`` — the package's own ``__init__``
#: contract), so this constant is independently owned here, exactly like every
#: sibling supervisor's ``DEFAULT_STOP_TIMEOUT`` (10.0) is already independently
#: defined three times over (sleep/vision/behavior).
EMBODY_DEFAULT_STOP_TIMEOUT = 10.0

#: Mirrors :data:`reachy.embody.attention.DEFAULT_ATTENTION_WINDOW_S` and
#: :data:`reachy.embody.engine.ENV_ATTENTION_WINDOW_S` by VALUE, not by import
#: — for the SAME reason ``DEFAULT_TURN_INTERVAL``/``EMBODY_DEFAULT_STOP_
#: TIMEOUT`` above are independently defined here rather than imported: a
#: command module registering this noun's CLI flags must not import
#: :mod:`reachy.embody` at module scope (it pulls in ``reachy.speech.llm``,
#: forbidden in ``_build_parser()``'s import closure by
#: ``tests/test_zero_llm_boundary.py``). Used only to spell out the default in
#: the flag's own help text; resolution against the real default happens in
#: :func:`reachy.embody.engine.resolve_attention_window_s`, imported
#: function-locally where composition actually needs it.
DEFAULT_ATTENTION_WINDOW_S = 45.0
ENV_ATTENTION_WINDOW_S = "REACHY_EMBODY_ATTENTION_WINDOW"


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
    except Exception:  # observability must never break the layer
        logger.warning("[embody] export sink raised while naming %s", reason, exc_info=True)


# ---------------------------------------------------------------------------
# The interjection route — where a PROPOSED utterance goes (issue #155, c2)
# ---------------------------------------------------------------------------
#
# There is no voice seam here any more, and its absence is the feature. Until
# task t12 this section built a ``synthesize`` + ``play_audio`` pair and handed
# it to the tool registry, so the WORKER model's text reached the speaker
# directly. The two-tempo architecture forbids exactly that: Gemma, rendered by
# the realtime floor, is the only voice the room hears (spec claim c2), and
# Qwen influences the conversation only through explicit typed events.
#
# So the layer's one remaining mouth is the duplex session's own playback leg
# (``resolved_media.sink.play``, passed to ``RealtimeDuplexSession``), and a
# ``speak``/``harmonics`` tool call becomes an INTERJECTION: governed by
# ``reachy.embody.interjection.InterjectionPolicy`` (default OFF, per-source
# default-deny, rate-bounded) and, if admitted, parked as a ``speakable``
# cognition scope the foreground voice may use or decline.


class _LateAttention:
    """The attention gate, reachable before the engine that owns it exists.

    The interjection policy needs the gate; the gate belongs to the engine; the
    engine is built from the registry the policy configures. Rather than break
    that circle by giving the policy a second gate — two state machines
    answering "is a conversation live?" is exactly how the two would come to
    disagree — this forwards to the ONE gate through the same one-slot box
    :func:`_compose_embody_seam` already uses for the session.

    Fail-closed while the slot is empty: no engine yet reads as COLD, so an
    interjection arriving before composition finishes is refused rather than
    admitted on a technicality.
    """

    def __init__(self, engine_of: Callable[[], object | None]) -> None:
        self._engine_of = engine_of

    def _gate(self) -> object | None:
        engine = self._engine_of()
        return getattr(engine, "attention", None) if engine is not None else None

    # ``now`` is FORWARDED, not decoration: this adapter stands in front of a
    # real :class:`~reachy.embody.attention.AttentionGate` whose own
    # ``is_warm``/``note_spoken`` honour an injected clock. Accepting the
    # argument and dropping it would make a caller's clock silently
    # ineffective — the adapter would answer from wall time while the caller
    # believed it had pinned the moment. (Sonar python:S1172 flagged the
    # unused parameter; forwarding is the fix, removing it would break the
    # seam's shape.)

    def is_warm(self, now: float | None = None) -> bool:
        gate = self._gate()
        return bool(gate.is_warm(now)) if gate is not None else False

    def note_spoken(self, now: float | None = None) -> bool:
        gate = self._gate()
        return bool(gate.note_spoken(now)) if gate is not None else False


def _interjection_publisher(
    engine_of: Callable[[], object | None], *, alert: bool
) -> Callable[[object], None]:
    """``publish(interjection)`` -> the engine's own record of it.

    *alert* is the one thing that differs between the two admission routes, and
    it is decided HERE, at composition, because only the composition knows
    which door an interjection came through: an external proposal off the wire
    is worth waking the mind for, the worker's own tool call is not (see
    :meth:`~reachy.embody.engine.EmbodyTurnEngine.note_interjection`).
    """

    def _publish(interjection: object) -> None:
        engine = engine_of()
        if engine is not None:
            engine.note_interjection(interjection, alert=alert)

    return _publish


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

    The line is parsed by :func:`~reachy.embody.cues.parse_runtime_line` and
    the resulting event mapped through
    :func:`~reachy.embody.cues.classified_cues_for_runtime_event`, not the bare
    ``cues_for_runtime_event``, so each cue reaches the engine carrying the #143 class
    its runtime event decided: a rule FIRE is an ALERT and triggers a turn,
    everything else parks. Erasing the class here is precisely the defect that
    turned 187 cues into 23 turns — the mapper always knew which was which.

    **Interjection lines are routed BEFORE the mapper (issue #155).**
    :mod:`reachy.embody.cues` recognises the layer's own ``interjection``
    family and REFUSES it by name, deliberately: a pure mapper holds no policy
    state, so it cannot decide whether an external source may put words in the
    robot's mouth, and falling through to "unrecognised" would hide the family
    behind a generic drop. The policy lives here, at the composition root, and
    this is the one place it is consulted for the wire route —
    ``policy.admit_event`` first, the mapper only for everything else. Without
    an injected policy the line still reaches the mapper and is still refused
    by name, so forgetting to wire this is loud rather than permissive.

    Everything it touches is O(1) and non-raising:
    :meth:`~reachy.embody.engine.EmbodyTurnEngine.submit_cues` routes into a
    bounded deque or a bounded coalescing park, and names its own overflow.
    """

    def __init__(
        self,
        lines: Iterable[str],
        engine: object,
        *,
        export: object = None,
        max_events: int | None = None,
        interjections: object = None,
    ) -> None:
        self._lines = lines
        self._engine = engine
        self._export = export
        self._max_events = max_events
        self._interjections = interjections
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
        from reachy.embody.cues import classified_cues_for_runtime_event, parse_runtime_line

        try:
            for line in self._lines:
                if self._stop.is_set():
                    break
                self.events += 1
                event = parse_runtime_line(line)
                if event is not None and not self._routed_interjection(event):
                    self.cues += self._engine.submit_cues(classified_cues_for_runtime_event(event))
                if self._max_events is not None and self.events >= self._max_events:
                    break
        except Exception as err:  # a dead feed must not kill the conversation
            _embody_drop(
                self._export,
                EMBODY_SOURCE_CUES,
                REASON_CUE_SOURCE_FAILED,
                f"{type(err).__name__}: {err}",
            )
        finally:
            self.done = True

    def _routed_interjection(self, event: dict) -> bool:
        """Hand an ``interjection`` line to the policy. ``True`` if it was handled.

        ``False`` for every other line type AND for an interjection when no
        policy was injected — in which case the mapper's own named refusal
        (``interjection-requires-policy``) is exactly the right outcome. The
        policy names its own drop for a refusal, so a rejected proposal is
        never silent on either path.
        """
        from reachy import runtime_cues

        if self._interjections is None or event.get("t") != runtime_cues.LINE_INTERJECTION:
            return False
        verdict = self._interjections.admit_event(event)
        if verdict.admitted and verdict.interjection is not None:
            self._engine.note_interjection(verdict.interjection, alert=True)
        return True


# ---------------------------------------------------------------------------
# The clip -> ask() perception lane (task t11, issue #139's h9 blocker)
# ---------------------------------------------------------------------------

#: The question :func:`build_clip_question` asks the ``senses`` lane about the
#: runtime's own rolling clip (issue #139's h9: "ask the worker model where it
#: is"). Framed as a single perception question, matching
#: :meth:`~reachy.embody.engine.EmbodyTurnEngine.ask`'s own contract — the
#: answer becomes a structured :class:`~reachy.embody.engine.
#: PerceptionSnapshot` for the next TRIGGERED turn (task t13, issue #155
#: c7), never a reply spoken on its own. Requests ONE JSON object so
#: :func:`parse_perception_answer` has a fixed shape to look for; the senses
#: lane is a cheap model and will sometimes ignore this, which is exactly
#: what that function's degrade path is for.
DEFAULT_CLIP_PROMPT = (
    "You are shown a short recent clip from your own camera. Reply with "
    "exactly one JSON object and nothing else, in this shape: "
    '{"summary": "<one or two plain sentences: where you appear to be and '
    'what is happening right now>", "entities": ["<a person, object, or '
    'place you notice>", ...], "confidence": <a number from 0.0 to 1.0 for '
    "how sure you are of this description>}. Use an empty list for "
    '"entities" if nothing stands out.'
)


def build_clip_question(path: str | Path, prompt: str = DEFAULT_CLIP_PROMPT) -> list[dict]:
    """The OpenAI-style multimodal content for :meth:`~reachy.embody.engine.
    EmbodyTurnEngine.ask` about one clip.

    One ``text`` part plus one ``video_url`` data-URI part, in that exact
    order and shape — ``docs/evidence/2026-08-01-probe-video-wire-format.md``
    (task t2) proved the deployed gateway decodes this correctly, streamed,
    with a recognizably correct description (and that the direction flips
    correctly between a forward and a reversed control clip, ruling out a
    lucky prior). ``ask()`` forwards whatever it is given as the user
    message's ``content`` verbatim, so no change to ``ask`` itself was needed
    to carry a clip beyond widening its type hint — only this content-shaping
    helper.

    Lives HERE rather than in :mod:`reachy.embody.engine` on purpose: that
    module's own model-config claim ("the engine reads no file and writes no
    environment variable") is machine-checked by an AST scan over the WHOLE
    module (``tests/test_embody_engine.py``), so a file read belongs on the
    composition-root side of that boundary — exactly where the clip's PATH is
    already resolved (:func:`_default_clip_reader`).

    A pure, RAISING helper on purpose: whatever reading *path* raises
    (typically :class:`OSError` — missing file, permission, a race with the
    clip rider's own overwrite-in-place rename) propagates unchanged, so this
    function stays trivially unit-testable against a real temp file. The
    caller (:class:`_ClipAsker`) is what turns a raise here into a named,
    non-blocking drop.
    """
    data = Path(path).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return [
        {"type": "text", "text": prompt},
        {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{encoded}"}},
    ]


#: :func:`parse_perception_answer`'s expected JSON keys (task t13, spec c7).
_PERCEPTION_SUMMARY_KEY = "summary"
_PERCEPTION_ENTITIES_KEY = "entities"
_PERCEPTION_CONFIDENCE_KEY = "confidence"


def _normalized_perception_keys(payload: dict) -> dict:
    """Re-key one parsed clip answer so a REFORMATTED key still resolves.

    The deployed senses model renders our own requested shape back with the
    key padded — ``{" summary": ...}`` — even though
    :data:`DEFAULT_CLIP_PROMPT` asks for ``{"summary": ...}``. Task t15's
    live acceptance caught it: three consecutive clip asks on the deployed
    gateway were dropped :data:`REASON_CLIP_ANSWER_UNSTRUCTURED` while every
    answer was in fact good
    (``docs/evidence/2026-08-03-t15-155-live-acceptance.md``).

    Whitespace and case are the two ways a model re-renders a key it is
    copying, so both are stripped here — and NOTHING else is. A genuinely
    different key (``description``) still misses, because tolerating a
    sloppy rendering of our contract is not the same as guessing at a
    synonym for it: the first keeps the parser honest about what the model
    said, the second would let it invent one. A padded key that collides
    with a clean one after normalisation loses to the clean one, so a
    well-formed answer is never degraded by this pass.
    """
    normalized: dict = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        clean = key.strip().casefold()
        if clean not in normalized or key == clean:
            normalized[clean] = value
    return normalized


def parse_perception_answer(raw: str) -> tuple[str, tuple[str, ...], float | None] | None:
    """Parse the senses lane's structured clip answer, or ``None`` if it isn't one.

    :data:`DEFAULT_CLIP_PROMPT` asks for one JSON object
    (``{"summary": ..., "entities": [...], "confidence": ...}``), but the
    senses lane is a CHEAP model answering in free text — assume it will
    sometimes wrap the object in a sentence or a code fence, or ignore the
    shape entirely. So this is tolerant, not strict: it looks for the FIRST
    ``{...}`` span in *raw* rather than requiring the whole reply to be JSON,
    and returns ``None`` — never raises — the moment anything about that span
    fails to yield a non-blank ``summary`` string. The caller
    (:meth:`_ClipAsker._ask_about`) is what turns a ``None`` here into the
    documented degrade: a summary-only snapshot plus one named drop
    (:data:`REASON_CLIP_ANSWER_UNSTRUCTURED`) — this function names no drop
    of its own, so it stays trivially unit-testable with no export sink at
    all, exactly like :func:`build_clip_question`.

    ``entities`` is coerced to a tuple of non-blank strings, silently
    dropping anything that is not a JSON string/number (never a whole-answer
    failure just because one list entry is the wrong shape) and defaulting
    to ``()`` when the key is missing or not a list. ``confidence`` is
    accepted only as a plain number — ``bool`` is explicitly excluded,
    because ``json`` parses ``true``/``false`` as :class:`bool`, a subtype of
    :class:`int` in Python, which would otherwise silently read as 0.0/1.0 —
    and clamped to ``[0.0, 1.0]``; anything else becomes ``None``, because
    "the model did not say how sure it was" is honest and a fabricated
    number is not.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except ValueError:  # json.JSONDecodeError IS a ValueError (Sonar python:S5713)
        return None
    if not isinstance(payload, dict):
        return None
    payload = _normalized_perception_keys(payload)
    summary = payload.get(_PERCEPTION_SUMMARY_KEY)
    if not isinstance(summary, str) or not summary.strip():
        return None
    entities_raw = payload.get(_PERCEPTION_ENTITIES_KEY)
    entities: tuple[str, ...] = ()
    if isinstance(entities_raw, list):
        entities = tuple(
            str(item).strip()
            for item in entities_raw
            if isinstance(item, (str, int, float))
            and not isinstance(item, bool)
            and str(item).strip()
        )
    confidence_raw = payload.get(_PERCEPTION_CONFIDENCE_KEY)
    confidence: float | None = None
    if isinstance(confidence_raw, (int, float)) and not isinstance(confidence_raw, bool):
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    return summary.strip(), entities, confidence


class _ClipAsker:
    """Poll the runtime's clip reference and turn it into perception CONTEXT.

    ``EmbodyTurnEngine.ask()`` (the tool-less ``senses`` lane) was built for
    exactly this — "a cheap perception question (describe this clip, is that a
    face) whose answer becomes a cue, not an action" — and had ZERO callers
    anywhere in :mod:`reachy` (issue #139's h9 blocker: "ask the worker model
    where it is"). This class is its first real caller: it reads
    ``state.json``'s ``clip`` key (:mod:`reachy.behavior.clip_rider`'s path
    reference), and when it names a fresh clip, asks about it and parks the
    answer as a structured :class:`~reachy.embody.engine.PerceptionSnapshot`
    (task t13, issue #155 c7) — never a trigger of its own. Since t13 the
    slot the answer lands in PERSISTS across turns (:meth:`~reachy.embody.
    engine.EmbodyTurnEngine._live_perception`) until a later poll supersedes
    it or it goes stale, rather than being drained by the one turn that
    happens to run right after a poll (issue #143's turn-drain policy still
    applies to the closed cue vocabulary — see the engine module docstring's
    "A state PERSISTS" section for why perception is the one exception).

    A background THREAD, for the same reason :class:`_CueReader` is one — but
    the constraint here is sharper than "must not block on I/O": ``ask()`` is
    a synchronous senses-lane network call, so running it from
    :meth:`~reachy.embody.engine.EmbodyTurnEngine.run`'s ``before_turn`` hook
    (the turn loop's OWN thread) would delay every pending trigger behind it —
    exactly the coupling h6/#139 forbid ("must never delay or block a turn").
    Polling on this thread means a stalled senses lane costs this thread
    alone; ``ask()`` is already deliberately outside the engine's
    ``_turn_lock`` (see that method's own docstring), so a concurrent
    ``run_turn()`` proceeds at full speed regardless of how long a poll is
    stuck inside ``ask()``.

    Cadence AND change-detection, never either alone: polling only on a bound
    interval (:data:`DEFAULT_CLIP_POLL_INTERVAL_S`) caps how often the senses
    lane is hit even while the runtime republishes a clip every
    ``DEFAULT_ENCODE_INTERVAL_S`` (5 s, ``reachy/behavior/clip_rider.py``);
    re-asking about the SAME clip on top of that (an unchanged ``ts``) would
    ask an identical question for no new information, so
    :meth:`poll_once` treats that as a silent no-op — nothing has gone wrong,
    there is simply nothing new to report.

    Every negative path resolves to exactly ONE named drop — never a raise,
    never a blocked or delayed turn: a missing/unavailable clip block
    (:data:`REASON_CLIP_UNAVAILABLE`, naming the block's own ``reason`` when it
    has one), one whose ``ts`` has not advanced within
    :data:`DEFAULT_CLIP_STALE_AFTER_S` (:data:`REASON_CLIP_STALE`), a path this
    process could not turn into a question (:data:`REASON_CLIP_UNREADABLE`),
    and ``ask()`` raising or answering empty (:data:`REASON_CLIP_ASK_FAILED` /
    :data:`REASON_CLIP_ASK_EMPTY`). Each is deduped against the last-reported
    state (mirroring :meth:`~reachy.behavior.clip_rider.ClipRider._report`) so
    a box with no clip ever published (no ``[vision]`` extra, the http-remote
    profile, a fresh boot) logs ONE line, not one every poll cycle forever.
    One outcome is a DEGRADE rather than a negative path:
    :data:`REASON_CLIP_ANSWER_UNSTRUCTURED` names an answer that reached the
    park anyway, as a summary-only snapshot, when it did not parse as the
    requested JSON shape (task t13) — the observation is never lost, only its
    structure.
    """

    def __init__(
        self,
        engine: object,
        *,
        read_clip: Callable[[], dict | None],
        export: object = None,
        prompt: str | None = None,
        poll_interval: float = DEFAULT_CLIP_POLL_INTERVAL_S,
        stale_after_s: float = DEFAULT_CLIP_STALE_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._read_clip = read_clip
        self._export = export
        self._prompt = prompt
        self._poll_interval = max(0.1, float(poll_interval))
        self._stale_after_s = max(0.0, float(stale_after_s))
        self._clock = clock
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._last_ts: float | None = None
        self._last_report: tuple[str, str] | None = None
        self.asks = 0
        self.drops = 0

    def start(self) -> None:
        """Start the poll thread. Idempotent."""
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name=EMBODY_CLIP_THREAD_NAME, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Ask the poller to stop after its current check. Never blocks."""
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_interval)

    def poll_once(self) -> None:
        """Check the clip reference once. NEVER raises; NEVER blocks a turn.

        Called by the poll loop (:meth:`_run`) and directly by tests — no
        thread is needed to exercise the policy.
        """
        try:
            block = self._read_clip()
        except Exception as err:  # a broken read is a named drop, not a raise
            self._report(REASON_CLIP_UNAVAILABLE, f"read raised {type(err).__name__}: {err}")
            return
        if not isinstance(block, dict) or not block.get("available"):
            reason = block.get("reason") if isinstance(block, dict) else None
            self._report(REASON_CLIP_UNAVAILABLE, reason or "no clip block published yet")
            return
        ts = block.get("ts")
        path = block.get("path")
        if not isinstance(ts, (int, float)) or not path:
            self._report(REASON_CLIP_UNAVAILABLE, "clip block carries no ts/path")
            return
        if ts == self._last_ts:
            return  # unchanged since the last ask — a quiet no-op, not a drop
        age = self._clock() - float(ts)
        if age > self._stale_after_s:
            self._last_ts = ts  # never retry the SAME stale clip
            self._report(REASON_CLIP_STALE, f"age={age:.1f}s > {self._stale_after_s:g}s")
            return
        self._last_ts = ts
        self._ask_about(str(path), float(ts))

    def _ask_about(self, path: str, ts: float) -> None:
        """Turn *path* into a question, ask it, and park the answer as a snapshot.

        *ts* is the clip's OWN monotonic capture time — already validated
        fresh enough by :meth:`poll_once`'s own staleness check — carried
        straight through as the resulting :class:`~reachy.embody.engine.
        PerceptionSnapshot`'s ``captured_at``. This is the freshness
        discipline the engine module docstring's "A state PERSISTS" section
        describes: a snapshot that sits unread in the park is judged by the
        true age of the FRAME it describes, not by when this call happened
        to run.
        """
        from reachy.embody.engine import PerceptionSnapshot

        try:
            content = build_clip_question(path, self._prompt or DEFAULT_CLIP_PROMPT)
        except Exception as err:  # an unreadable clip is a named drop
            self._report(REASON_CLIP_UNREADABLE, f"{path}: {type(err).__name__}: {err}")
            return
        try:
            answer = self._engine.ask(content)
        except Exception as err:  # ask() must never wedge this thread
            self._report(REASON_CLIP_ASK_FAILED, f"{type(err).__name__}: {err}")
            return
        answer = (answer or "").strip()
        if not answer:
            self._report(REASON_CLIP_ASK_EMPTY, "")
            return
        self._last_report = None  # the lane recovered; the next failure reports fresh
        self.asks += 1
        parsed = parse_perception_answer(answer)
        if parsed is None:
            # Degrade, never drop: the raw answer still becomes a
            # summary-only snapshot (task t13) — only the STRUCTURE was
            # lost, not the observation.
            self._report(REASON_CLIP_ANSWER_UNSTRUCTURED, answer[:120])
            summary, entities, confidence = answer, (), None
        else:
            summary, entities, confidence = parsed
        # A structured PERCEPTION snapshot, explicitly — never a trigger
        # (t7/#143's policy): a clip answer is a perception the NEXT
        # triggered turn reads, exactly like any other sense cue, never a
        # reason to run one by itself. Unlike a cue, this slot PERSISTS
        # across turns (:meth:`~reachy.embody.engine.EmbodyTurnEngine.
        # _live_perception`) until superseded or stale.
        self._engine.submit_perception(
            PerceptionSnapshot(
                summary=summary,
                entities=entities,
                confidence=confidence,
                captured_at=ts,
                frame_ref=path,
            )
        )

    def _report(self, reason: str, detail: str) -> None:
        key = (reason, detail)
        if key == self._last_report:
            return
        self._last_report = key
        self.drops += 1
        _embody_drop(self._export, EMBODY_SOURCE_CLIP, reason, detail)


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
        clip_asker: _ClipAsker | None = None,
        summary: object = None,
        export: object = None,
    ) -> None:
        self.profile = profile
        self.media = media
        self.session = session
        self.registry = registry
        self.engine = engine
        self.reader = reader
        self.clip_asker = clip_asker
        #: Qwen's rolling-summary maintenance pass
        #: (:class:`reachy.embody.summary.SummaryProducer`), on its own thread
        #: for the same reason the clip asker is: a whole gateway round trip
        #: charged to the turn loop would pause the robot mid-conversation.
        self.summary = summary
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
        except Exception as err:  # a dead gateway is deaf, not fatal
            _embody_drop(
                self._export,
                EMBODY_SOURCE_SESSION,
                REASON_SESSION_START_FAILED,
                f"{type(err).__name__}: {err}",
            )
        self.reader.start()
        if self.clip_asker is not None:
            self.clip_asker.start()
        if self.summary is not None:
            self.summary.start()

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
        if self.clip_asker is not None:
            self.clip_asker.stop()
        if self.summary is not None:
            self.summary.stop()
        for label, closer in (("session", self.session.close), ("media", self.media.close)):
            try:
                closer()
            except Exception as err:  # teardown names faults, never raises
                _embody_drop(
                    self._export,
                    EMBODY_SOURCE_SHUTDOWN,
                    REASON_SHUTDOWN_FAILED,
                    f"{label}: {type(err).__name__}: {err}",
                )


def _utterance_tap(
    engine: object,
    session_of: Callable[[], object | None],
    *,
    export: object = None,
) -> Callable[[object], None]:
    """duplex ``on_utterance`` -> a candidate TRIGGER, and the ROBOT'S VOICE.

    The SESSION is ungated — it hears everyone in the room, and its own
    boundary tests pin that — but the ENGINE decides whether what it heard is
    worth waking a mind for: while attention is cold only the robot's name
    admits an utterance (issue #148, :mod:`reachy.embody.attention`). This tap
    is where that ONE decision reaches BOTH of the things it should govern.

    **Attention gates the voice too (issue #149).** Until this task it gated
    only cognition, and the room got a spoken reply to every sentence anyway:
    the duplex session armed itself once at ``session.created`` and the gateway
    then answered every committed turn, so a conversation the robot had been
    told to ignore was answered — thoughtlessly — out loud. A robot butting
    into a conversation it was explicitly excluded from is worse than one that
    answers everything, so ignore has to mean ignore SILENTLY. An admitted
    utterance therefore also asks the session for exactly one reply
    (:meth:`~reachy.speech.realtime_duplex.RealtimeDuplexSession.arm_once`),
    and a refused one asks for nothing.

    **The policy lives here, and the mechanism lives in the wire.** The duplex
    module reaches no gate and must not (three structural pins in
    ``tests/test_realtime_duplex.py`` say so); it knows only that *someone*
    asked for a reply. Which someone, and on what grounds, is this function.

    **Why the admission is read from the gate's own counter rather than from
    ``submit_utterance``'s return.** That return is ``False`` for two unrelated
    reasons: the robot was not addressed, and the robot WAS addressed but the
    turn engine's trigger queue is full. Only the first is a reason to stay
    silent. The engine's own docstring is explicit that the admission stands
    through a full queue — "a fact about the room, not about how full a queue
    happened to be" — and the voice belongs to the realtime floor, not to this
    queue, so a saturated mind must not mute a robot mid-conversation.
    ``unaddressed_utterances`` moves on exactly the refusal, and nothing else.

    *session_of* is a late-bound accessor because the session is built WITH
    this tap: see :func:`_compose_embody_seam`. It yields ``None`` only before
    composition finishes, which is before the worker thread that fires the tap
    exists.
    """

    def _heard(utterance: object) -> None:
        text = (getattr(utterance, "text", "") or "").strip()
        if not text:
            return
        ignored_before = int(getattr(engine, "unaddressed_utterances", 0) or 0)
        engine.submit_utterance(text)
        if int(getattr(engine, "unaddressed_utterances", 0) or 0) != ignored_before:
            return  # attention refused it: the room hears nothing back
        session = session_of()
        if session is None:  # pragma: no cover - composition fills the slot first
            return
        try:
            session.arm_once()
        except Exception as err:  # this runs on the session's own worker thread
            _embody_drop(
                export,
                EMBODY_SOURCE_SESSION,
                REASON_ARM_FAILED,
                f"{type(err).__name__}: {err}",
            )

    return _heard


def _response_tap(
    engine: object,
    session_of: Callable[[], object | None],
    *,
    export: object = None,
) -> Callable[[object], None]:
    """duplex ``on_response`` -> already-said CONTEXT, never a trigger.

    The duplex session answers speech on its own, server-side, so without this
    the thinking mind would not know its own mouth had already replied and
    would cheerfully call ``speak`` to say it again (the wiring t10's docstring
    singles out as the one this composition could miss).

    **A CUT reply is recorded as what the room heard, not as nothing and not as
    everything (task t7, spec c34).** Since chunked playback landed, a reply a
    human talked over has usually been PARTLY spoken — so the session is asked
    for its measured said/unsaid split and the engine records both halves: the
    prefix as spoken, the remainder as a kept wanted-to-say artifact the next
    turn can weigh. Only when there is no measurement at all
    (:meth:`~reachy.speech.realtime_duplex.RealtimeDuplexSession.spoken_split`
    yields ``None`` — a reply this session never saw, or one still arriving)
    does an interrupted reply fall back to the conservative pre-t7 answer of
    recording nothing: the mind believing it had answered when the room heard
    nothing is the worse of the two errors.

    It is also the second half of the attention window (issue #148): an answer
    the layer actually spoke EXTENDS a live conversation, so a long reply never
    times the human out. It cannot OPEN one — the server answers ambient
    utterances the gate refused, and a robot that could wake itself with its
    own voice would never go quiet again.

    *session_of* is the same late-bound accessor :func:`_utterance_tap` takes,
    for the same reason: the session is built WITH this tap.
    """

    def _spoke(response: object) -> None:
        session = session_of()
        split = _measured_split(session, response, export=export)
        if split is not None and getattr(split, "cut", False):
            _record_cut(engine, session, split, export=export)
            return
        if getattr(response, "interrupted", False):
            return
        engine.note_spoken(getattr(response, "text", "") or "")

    return _spoke


def _tail_cut_tap(
    engine: object,
    session_of: Callable[[], object | None],
    *,
    export: object = None,
) -> Callable[[object], None]:
    """duplex ``on_speech_started`` -> stop talking, and record what was said.

    **This closes the TAIL only, and that is the whole design.** Upstream
    already paces its audio delivery to track the playhead (lobes-cli's
    ``lobes/realtime/_conversation.py``, ``delivery_pause_ms`` /
    ``DELIVERY_LEAD_MS``) precisely so a human can barge in mid-reply and the
    floor can see it; that path arrives as ``response.interrupted``, task t6
    wired it, and :func:`_response_tap` records it. What upstream cannot see is
    the lag THIS client adds after receipt — up to one playback chunk of
    accumulation plus the daemon's upload-then-play round trip — which lands
    AFTER ``response.done``. In that window the floor has returned to LISTENING
    while the room is still hearing audio, so the moment a listener is most
    likely to object (having now heard enough to object TO) produces no
    ``response.interrupted`` at all and the queued chunks play on.

    **VAD-verified speech only (spec claim c35).** The trigger is the server's
    own ``input_audio_buffer.speech_started``, not a loudness reading: an
    energy predicate is a LOCATOR, never a content filter, and a cough or a
    door slam must not be able to cut the robot off mid-sentence. This is the
    same evidence the floor itself would have acted on a second earlier.

    **Attention does NOT gate this, deliberately.** Being ignored for THINKING
    (issue #148) is not the same as being unable to stop the robot talking:
    anyone in the room may interrupt, and "anyone" reads as any external
    interlocutor — a peer robot or an automated system included (spec decision
    c36). Cutting also opens no attention window; it is not an address.

    **Two paths, one record.** Nothing here guards against double-recording
    with a flag, because the session's own drain semantics already make it
    impossible: a cut empties the mouth queue AND stamps the reply stale so
    none of it can be re-queued, and ``_finish_response`` drains that same
    queue before publishing a server-driven interrupt. So a second onset over
    the same reply finds ``playback_pending`` false and withholds nothing, and
    a server barge-in leaves nothing for this tap to cut. The one case that
    DOES reach the engine twice — a reply recorded whole at ``response.done``
    and then cut — is exactly what t7's ``_correct_spoken`` narrows: one entry
    replaced, never a second one added.

    **Why the pending check rather than "is there a split to record".** Every
    reply this session spoke has a measurement, so recording on measurement
    alone would file a reply the room heard in FULL as truncated. The question
    is whether a cut would WITHHOLD anything, which is what
    :attr:`~reachy.speech.realtime_duplex.RealtimeDuplexSession.
    playback_pending` answers — and it excludes the chunk already inside
    ``play``, which cannot be recalled.

    A split of ``None`` means the reply has not completed (the cut landed
    mid-reply, where there is no honest total to divide by). Nothing is
    recorded here then, on purpose: the ledger carries the cancellation, and
    :func:`_response_tap` records the measured split when ``response.done`` or
    ``response.interrupted`` finally lands.
    """

    def _started(_event: object) -> None:
        session = session_of()
        if session is None:  # pragma: no cover - composition fills the slot first
            return
        try:
            if not session.playback_pending:
                return  # nothing queued: a cut here would withhold nothing
            progress = session.cancel_playback()
        except Exception as err:  # this runs on the session's own worker thread
            _embody_drop(
                export,
                EMBODY_SOURCE_SESSION,
                REASON_TAIL_CUT_FAILED,
                f"{type(err).__name__}: {err}",
            )
            return
        split = _measured_split(session, progress, export=export)
        if split is not None and getattr(split, "cut", False):
            _record_cut(engine, session, split, export=export)

    return _started


def _record_cut(
    engine: object, session: object | None, split: object, *, export: object = None
) -> None:
    """ONE cut, recorded in both places it belongs: our record, and the floor's.

    The two cut paths (:func:`_response_tap`'s server-driven interrupt and
    :func:`_tail_cut_tap`'s client-side tail cut) share this so they cannot
    drift into telling the conversation two different stories — the same reason
    :func:`_measured_split` is one accessor for both.

    * The LAYER's record narrows here: :meth:`~reachy.embody.engine.
      EmbodyTurnEngine.note_interrupted_reply` records the measured said half as
      spoken and keeps the remainder as a wanted-to-say artifact (spec c34).
    * The FLOOR's record is CORRECTED here: the server saw a reply delivered at
      wire speed and appended it whole, so its history overstates what the room
      heard after every client-local cut (spec claim c39). The correction goes
      out as a ``history`` conversation item — ephemeral context would let the
      overstatement return on the next generate call.

    Where the gateway announced no item support the push is declined by
    ``send_item`` itself, which names the degrade once per session (c44/h29):
    the layer's own record is still right, the floor's is still overstated, and
    neither is dressed up as the other.
    """
    engine.note_interrupted_reply(split)
    _push_floor_item(session, engine.floor_correction(split), export=export)


def _push_floor_item(session: object | None, item: object | None, *, export: object = None) -> bool:
    """Send ONE projection of the canonical history to the floor. Never raises.

    Returns whether the floor took it. ``False`` is the ordinary answer today —
    conversation-item parity is parked upstream (agentculture/lobes-cli#170 item
    2), so ``send_item`` declines and names that degrade itself, once per
    session. Only a RAISE is named here: ``send_item`` promises not to, this
    runs on the session's own worker thread where an escaping exception is an
    anonymous warning at best, and a floor left holding an overstated reply is
    something the operator should be able to see.
    """
    if item is None:
        # Nothing to correct: the room heard the whole reply, so the floor's own
        # record is already true and a "correction" would only add noise.
        return False
    if session is None:  # pragma: no cover - composition fills the slot first
        return False
    try:
        return bool(session.send_item(_as_conversation_item(item)))
    except Exception as err:
        _embody_drop(
            export,
            EMBODY_SOURCE_SESSION,
            REASON_FLOOR_PUSH_FAILED,
            f"{type(err).__name__}: {err}",
        )
        return False


def _as_conversation_item(item: object) -> object:
    """Join the engine's :class:`~reachy.embody.engine.FloorItem` to the wire's own type.

    The two are structurally identical and deliberately distinct classes: the
    dependency runs ONE way (the engine must not import the WebSocket client, as
    its ``_SpokenSplitLike`` docstring records for the value travelling the
    other way), so joining them is this composition root's job. A mechanical
    1:1 field copy, and the wire re-validates every value on the way out, so a
    drift in either vocabulary fails closed at the frame instead of quietly
    mislabelling an item's disposition.
    """
    from reachy.speech.realtime_duplex import ConversationItem

    return ConversationItem(
        role=item.role,
        text=item.text,
        disposition=item.disposition,
    )


def _reseed_tap(engine: object) -> Callable[[], list[object]]:
    """duplex ``reseed`` -> the canonical history, projected onto a NEW session.

    Decision **c27**: the layer curates the conversation record and pushes
    projections to the floor, so lobes' server-side history is what the layer
    put there. This is the seam that does the pushing, and the ORDERING it
    depends on is the wire's: the client consults it inside ``session.created``
    handling, before it arms, because a session close wipes the floor's
    ephemeral history and a reconnect that armed first would answer out of an
    empty one — Gemma silently reset to amnesia (spec claim c40).

    WHAT to send is :meth:`~reachy.embody.engine.EmbodyTurnEngine.floor_reseed`
    — Gemma's ``m``-window as curated history turns plus Qwen's rolling summary
    as one ephemeral context item, both bounded where those bounds already live.
    This function adds no policy of its own; it is the type join, and it exists
    as a named function rather than a lambda so a test can pin that composition
    hands the session a real one.
    """

    def _seed() -> list[object]:
        return [_as_conversation_item(item) for item in engine.floor_reseed()]

    return _seed


def _measured_split(session: object | None, response: object, *, export: object = None) -> object:
    """Ask the session what the room heard of *response*. Never raises.

    *response* is anything carrying a ``response_id`` — the published
    :class:`~reachy.speech.realtime_duplex.Response` from
    :func:`_response_tap`, or the :class:`~reachy.speech.realtime_duplex.
    PlaybackProgress` a cut returns to :func:`_tail_cut_tap`. One accessor for
    both, so the two cut paths cannot ask the session two different questions.

    This runs on the session's own worker thread, where an escaping exception
    is logged as an anonymous warning at best — so a session that cannot answer
    is a NAMED drop and a ``None``, and the caller falls back to the coarse
    record rather than losing the reply entirely.
    """
    if session is None:  # pragma: no cover - composition fills the slot first
        return None
    try:
        # ``or ""`` on purpose: an id-less reply asks for the ANONYMOUS record,
        # never for "whatever is playing right now", which is what a bare
        # ``None`` would mean to the session and could name a different reply.
        return session.spoken_split(getattr(response, "response_id", None) or "")
    except Exception as err:
        _embody_drop(
            export,
            EMBODY_SOURCE_SESSION,
            REASON_SPLIT_UNAVAILABLE,
            f"{type(err).__name__}: {err}",
        )
        return None


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


def _default_clip_reader(root: Path | None) -> Callable[[], dict | None]:
    """The production ``read_clip`` seam: ``state.json``'s ``clip`` key.

    Same containment as every other spool consumer this composition root
    builds (:func:`_resolve_spool_dir` -> :class:`~reachy.embody.tools.
    EmbodyToolRegistry`'s ``spool_root``): :mod:`reachy.behavior.control`
    resolves the state dir on the layer's behalf, reaching
    :func:`~reachy.daemon.state_dir` only transitively — never its process
    surface — so this module still never imports :mod:`reachy.daemon` itself.
    *root* is the SAME override :meth:`_resolve_spool_dir` returns, so a test
    (or an operator's ``--spool-dir``) that redirects the tool registry's
    spool redirects this reader too — the clip lives under the ONE
    ``behavior/`` tree the runtime and the layer already share.
    """
    from reachy.behavior import control as control_mod

    def _read() -> dict | None:
        state = control_mod.read_state(root=root)
        block = state.get("clip") if isinstance(state, dict) else None
        return block if isinstance(block, dict) else None

    return _read


def _default_rules_loader() -> object:
    """Read the box's merged rules (shipped ⊕ the operator's overlay).

    FUNCTION-LOCAL import, like every other cognition-adjacent import in this
    module: ``_build_parser()`` imports every command module, so a module-scope
    import here would put the rules stack (and everything it reaches) in the
    import path of ``daemon status`` and ``--help``.
    ``test_building_the_cli_parser_loads_no_cognition_module`` pins that.
    """
    from reachy.behavior.rules import load_rules, overlay_rules_path

    return load_rules(overlay_rules_path())


def _resolve_embody_names(
    rules_loader: Callable[[], object] | None = None,
    *,
    export: object = None,
) -> tuple[str, ...]:
    """The names the robot answers to, resolved ONCE at layer start (#177).

    The operator configures them in ``<state_dir>/behavior/rules.toml``'s
    ``names`` array, and the runtime's own hearing gate reads the same file, so
    a robot cannot end up answering to one set of names through the runtime's
    ears and another through the layer's. Hot reload is a documented non-goal:
    a name that changed mid-conversation would mean the gate and the mind
    disagree about who was just addressed.

    A malformed overlay is NAMED and survived, never fatal — ``load_rules``
    raises :class:`~reachy.cli._errors.CliError` on a typo, and the layer falls
    back to :data:`~reachy.speech.name_match.SHIPPED_NAMES`. Any other
    exception (an unreadable state dir, a permission error) takes the same
    path: this runs before the layer has ears, so there is nothing to gain by
    letting it kill the process.
    """
    from reachy.speech.name_match import SHIPPED_NAMES

    loader = rules_loader if rules_loader is not None else _default_rules_loader
    try:
        names = tuple(str(name) for name in getattr(loader(), "names", ()))
    except CliError as exc:
        _embody_drop(export, EMBODY_SOURCE_NAMES, REASON_NAMES_OVERLAY_REFUSED, exc.message)
        return SHIPPED_NAMES
    except Exception as exc:  # never fatal — see the docstring
        _embody_drop(export, EMBODY_SOURCE_NAMES, REASON_NAMES_OVERLAY_REFUSED, str(exc))
        return SHIPPED_NAMES
    return names or SHIPPED_NAMES


def _apply_attention_names(
    engine: object,
    names: Sequence[str],
) -> None:
    """Hand the resolved names to the engine's attention gate.

    ``getattr`` rather than a hard attribute read because ``engine_factory`` is
    an injected seam: a composition test's stub engine has no gate, and the
    layer must compose anyway.
    """
    gate = getattr(engine, "attention", None)
    setter = getattr(gate, "set_names", None)
    if callable(setter):
        setter(tuple(names))
        senselog.stage(
            EMBODY_STAGE,
            EMBODY_SOURCE_NAMES,
            uuid.uuid4().hex[:8],
            "answering to " + ", ".join(names),
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
    interjection_limits: object | None = None,
    lines: Iterable[str] | None = None,
    stdin: TextIO | None = None,
    clip_reader: Callable[[], dict | None] | None = None,
    clip_poll_interval: float | None = None,
    summary_producer: object | None = None,
) -> _EmbodyLayer:
    """Build the whole layer — the ONE place the wave-1/2/3 seams meet.

    Every collaborator is injectable so the composition is exercised with no
    gateway, no broker, no robot, no tee socket and no audio device; the
    defaults are the real thing.

    Order matters in one place only: the media profile is built FIRST, because
    its sink is the duplex mouth and its source the duplex ears. Building it
    twice would give the layer two mouths and, on the robot profile, two
    readers contending for one tee socket. Since task t12 the sink has exactly
    ONE consumer — the realtime floor's own reply — because the worker's voice
    tools are proposals rather than playback (spec claim c2).
    """
    from reachy.embody.cues import open_runtime_lines
    from reachy.embody.engine import EmbodyTurnEngine, Limits, resolve_attention_window_s
    from reachy.embody.interjection import InterjectionPolicy
    from reachy.embody.media import build_media
    from reachy.embody.summary import SummaryProducer
    from reachy.embody.tools import EmbodyToolRegistry
    from reachy.speech.realtime_duplex import (
        DEFAULT_VOICE_PROMPT,
        RealtimeDuplexSession,
        resolve_voice_prompt,
    )

    resolved_media = (
        media if media is not None else build_media(getattr(args, "media_profile", None))
    )

    # The engine is built FROM the registry, and the policy the registry needs
    # wants the engine's attention gate — one slot closes that circle, exactly
    # as ``session_slot`` closes the session's, and for the same reason: the
    # slot is filled long before any thread that can read it exists.
    # Only the LIMITS are injectable, never the whole policy: the attention
    # wiring is this function's job and must not be something a caller can
    # forget, or an interjection would be judged against a gate nobody warms.
    engine_slot: list[object] = []
    policy = InterjectionPolicy(
        limits=interjection_limits,
        attention=_LateAttention(lambda: engine_slot[0] if engine_slot else None),
    )

    spool_root = _resolve_spool_dir(args)
    build_registry = registry_factory if registry_factory is not None else EmbodyToolRegistry
    registry = build_registry(
        interjection=policy,
        # alert=False: the worker's own proposal must not wake the worker.
        on_interjection=_interjection_publisher(
            lambda: engine_slot[0] if engine_slot else None, alert=False
        ),
        spool_root=spool_root,
        await_timeout=float(getattr(args, "await_timeout", 1.0)),
    )

    build_engine = engine_factory if engine_factory is not None else EmbodyTurnEngine
    engine_kwargs: dict[str, object] = {
        "registry": registry,
        "export": export,
        # issue #141/S107: the engine's bounds live in one frozen Limits now;
        # this composition root overrides turn_interval and attention_window_s
        # (issue #150, resolved explicit-flag > env > default), so every other
        # field takes the engine's own documented default.
        "limits": Limits(
            turn_interval=float(getattr(args, "turn_interval", DEFAULT_TURN_INTERVAL)),
            attention_window_s=resolve_attention_window_s(getattr(args, "attention_window", None)),
        ),
    }
    if turn_fn is not None:
        engine_kwargs["turn_fn"] = turn_fn
    engine = build_engine(**engine_kwargs)
    engine_slot.append(engine)
    # The names the robot answers to (#177), read from the SAME rules overlay
    # the runtime's hearing gate reads, once, here.
    # The overlay loader is a MODULE-LEVEL seam resolved at call time
    # (``_default_rules_loader``), not a fifteenth keyword: tests monkeypatch
    # the attribute, exactly like ``reachy.discover.sweep.read_interfaces``.
    _apply_attention_names(engine, _resolve_embody_names(export=export))

    build_session = session_factory if session_factory is not None else RealtimeDuplexSession
    # The utterance tap has to reach the session that is being built WITH it
    # (issue #149: an admitted utterance arms one reply). A one-slot box closes
    # that circle without a post-hoc attribute poke, and it is safe by
    # construction: the tap only ever fires on the session's worker thread,
    # which `start()` spawns — long after this function has returned.
    session_slot: list[object] = []
    session = build_session(
        read_audio=resolved_media.source.read,
        sample_rate=resolved_media.source.sample_rate,
        play=resolved_media.sink.play,
        mute_during_playback=bool(getattr(args, "mute_during_playback", False)),
        # Connect-time voice conventions (issue #151/#153, spec c10, honesty
        # h8, task t9): no CLI flag yet, so this is env-var-then-default —
        # REACHY_EMBODY_VOICE_PROMPT if set (and valid), else this module's
        # own chunk-friendly, longer-answer-permitting text. Passing `default=`
        # HERE, at the one production construction site, is the fix for the
        # capability having shipped with no caller: resolve_voice_prompt's own
        # bare-call contract stays "nothing configured -> None" (an absent
        # override is not a fault for a pure resolver), but on the deployed
        # robot "nothing configured" must not mean "silently inherit the
        # gateway's bare default" -- that is the whole point of this arc. A
        # REJECTED attempt (blank or over-long) still resolves to None here,
        # never to DEFAULT_VOICE_PROMPT -- see resolve_voice_prompt's
        # docstring for why a rejected override is never silently repaired.
        system_prompt=resolve_voice_prompt(default=DEFAULT_VOICE_PROMPT),
        # Opt in to per-ADMITTED-utterance arming. The wire's own default is
        # still arm-once, and against a gateway that cannot do one-shot arming
        # this degrades back to exactly that, with one named drop (h9).
        arm_per_utterance=True,
        on_utterance=_utterance_tap(
            engine, lambda: session_slot[0] if session_slot else None, export=export
        ),
        # Same late-bound accessor, same reason (task t7): the response tap
        # asks the session what the room actually heard of the reply it is
        # being handed.
        on_response=_response_tap(
            engine, lambda: session_slot[0] if session_slot else None, export=export
        ),
        # The tail cut (task t16): the server's own VAD onset stops the mouth
        # in the window AFTER ``response.done``, where the floor is listening
        # again and can no longer interrupt anything itself.
        on_speech_started=_tail_cut_tap(
            engine, lambda: session_slot[0] if session_slot else None, export=export
        ),
        # The canonical history's projection onto every NEW session (task t11,
        # decision c27). Task t10 built the whole items mechanism and left the
        # CONTENT to the layer, which is here: without this keyword the channel
        # ships built and unreachable, the exact shape t6's ``cancel_playback``
        # and t9's voice prompt each hit earlier in this arc. No late-bound slot
        # is needed — the seam reads the ENGINE, which already exists.
        reseed=_reseed_tap(engine),
    )
    session_slot.append(session)

    feed_lines = (
        lines
        if lines is not None
        else open_runtime_lines(feed=getattr(args, "feed", "-"), stdin=stdin)
    )
    reader = _CueReader(
        feed_lines,
        engine,
        export=export,
        max_events=getattr(args, "max_events", None),
        # The wire route reaches the SAME policy the tool route does: one
        # decision point, one place to get default-deny right.
        interjections=policy,
    )

    clip_asker_kwargs: dict[str, object] = {
        "read_clip": clip_reader if clip_reader is not None else _default_clip_reader(spool_root),
        "export": export,
    }
    if clip_poll_interval is not None:
        clip_asker_kwargs["poll_interval"] = clip_poll_interval
    clip_asker = _ClipAsker(engine, **clip_asker_kwargs)

    # ONE summary, Qwen's, never regenerated per lane (issue #154 decision
    # c30): this is the repo's only production caller of ``update_summary``.
    summary = summary_producer if summary_producer is not None else SummaryProducer(engine)

    return _EmbodyLayer(
        profile=getattr(resolved_media, "profile", "unknown"),
        media=resolved_media,
        session=session,
        registry=registry,
        engine=engine,
        reader=reader,
        clip_asker=clip_asker,
        summary=summary,
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
        "clip_asks": layer.clip_asker.asks if layer.clip_asker is not None else 0,
        # Reported beside the utterance count on purpose: "it heard 12 and
        # ignored 9" is the question an operator asks about a robot that stayed
        # quiet, and attention (#148) is the answer more often than a fault is.
        "unaddressed": int(getattr(layer.engine, "unaddressed_utterances", 0) or 0),
    }
    if json_mode and export_hook is None:
        emit_result({"status": "ok", **stats}, json_mode=True)
    else:
        emit_diagnostic(
            f"[embody] layer down: {stats['turns']} turn(s), {stats['cues']} cue(s) "
            f"over {stats['events']} runtime event(s), {stats['utterances']} utterance(s) "
            f"({stats['unaddressed']} not addressed to it)"
        )
    return 0


# ---------------------------------------------------------------------------
# embody start / stop / restart / status — the supervisor (task t12)
# ---------------------------------------------------------------------------
#
# reachy/embody/supervisor.py is a plain background-process supervisor (pid +
# log under the state dir), the same shape reachy.sleep.supervisor /
# reachy.vision.supervisor / reachy.behavior.supervisor already are — see that
# module's own docstring for why it needs no daemon-health preflight and how
# it stays out of the layer's "no shell reachable" claim
# (tests/test_embody_redteam.py's documented _CONTROL_PLANE_MODULES
# exemption).
#
# Every import of reachy.embody.supervisor here is FUNCTION-LOCAL, same as
# every other reachy.embody import in this module (h15): the package's own
# __init__ contract is "command modules import this package inside functions,
# never at module scope", and tests/test_agent_embody.py's
# test_no_cognition_or_layer_module_is_imported_at_command_module_scope scans
# this file for exactly that — a forbidden-prefix match on "reachy.embody",
# with no carve-out for a cognition-free submodule.


def cmd_agent_embody_start(args: argparse.Namespace) -> int:
    """Start the embodiment layer in the background (idempotent; h17: one command)."""
    from reachy.embody import supervisor as embody_supervisor

    data = embody_supervisor.start(
        feed=args.feed,
        media_profile=args.media_profile,
        spool_dir=args.spool_dir,
        await_timeout=args.await_timeout,
        turn_interval=args.turn_interval,
        mute_during_playback=args.mute_during_playback,
        attention_window=args.attention_window,
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_agent_embody_stop(args: argparse.Namespace) -> int:
    """Stop the layer this CLI started: SIGTERM, then SIGKILL if it lingers.

    h7/h26: signals ONLY the pid :mod:`reachy.embody.supervisor` tracked — the
    runtime, the daemon and every other process on the box are untouched — and
    touches nothing on disk: layer-authored ``embody-*`` rules PERSIST in the
    overlay (spec c26/q6), by design.
    """
    from reachy.embody import supervisor as embody_supervisor

    data = embody_supervisor.stop(timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_agent_embody_restart(args: argparse.Namespace) -> int:
    """Restart the background layer (re-reads flags/code)."""
    from reachy.embody import supervisor as embody_supervisor

    data = embody_supervisor.restart(
        feed=args.feed,
        media_profile=args.media_profile,
        spool_dir=args.spool_dir,
        await_timeout=args.await_timeout,
        turn_interval=args.turn_interval,
        mute_during_playback=args.mute_during_playback,
        attention_window=args.attention_window,
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_agent_embody_status(args: argparse.Namespace) -> int:
    """Report the layer's process state (pid + liveness) + its log path."""
    from reachy.embody import supervisor as embody_supervisor

    data = embody_supervisor.status()
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


# ---------------------------------------------------------------------------
# overview (rubric-required; hand-built — no transport line)
# ---------------------------------------------------------------------------

_VERBS = [
    "agent attach — read the runtime event feed, act via the intent spool, "
    "publish the agent's own cognition feed",
    "agent embody — run the embodiment layer in the FOREGROUND: hear and speak "
    "out loud over one realtime duplex session, think over the streaming HTTP "
    "lane, act through the direct-operation action set",
    "agent embody start — start the layer in the BACKGROUND (pid + log under "
    "the state dir; idempotent)",
    "agent embody stop — stop the layer this CLI started (SIGTERM, then "
    "SIGKILL if it lingers; touches ONLY the layer's own tracked pid — "
    "embody-authored rules PERSIST in the overlay)",
    "agent embody restart — stop then start (re-reads code/flags)",
    "agent embody status — report the layer's process state (pid + liveness)",
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
                'duplex session (it hears everyone; say "reachy" to wake it, and it '
                "keeps listening for ~45s after each thing heard or said), thinks over "
                "the streaming "
                "/v1/chat/completions lane, and operates the robot through the closed "
                "five-tool direct-operation action set (goto / speak / harmonics / "
                "run_behavior / create_rule). It constructs no ReachyMini: audio comes "
                "from the runtime's tee socket and goes out the daemon's HTTP media "
                "route. Enable or disable it at will — the runtime is unchanged either "
                "way.",
                "SUPERVISION: `embody start`/`stop`/`restart`/`status` manage the layer "
                "as a detached background process (pid + log under the state dir, "
                "SIGTERM->SIGKILL on stop) — the same shape as sleep/vision/behavior's "
                "engine. No systemd unit ships for it; stopping the layer removes its "
                "process trace only — layer-authored `embody-*` rules PERSIST in the "
                "rules overlay (the robot keeps what it was taught) and stay enumerable "
                "by prefix.",
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


def _add_embody_operating_args(parser: argparse.ArgumentParser, *, inherit: bool = False) -> None:
    """The layer's own operating flags — shared by ``embody`` (the foreground
    verb) and the ``start``/``restart`` supervisor verbs (task t12), so a
    background layer is configured identically to a foreground run. Mirrors
    ``behavior.py``'s ``_add_engine_tuning`` sharing the same shape between
    ``engine run`` and ``engine start``.

    *inherit* is for the SUB-parser copies, and it is load-bearing (issue #147).
    Because these flags are declared on the parent AND the sub-parser, argparse
    applies the sub-parser's defaults over a value the parent already parsed —
    so ``embody --feed <fifo> start`` spawned a layer with ``--feed -``, read
    ``/dev/null``, and exited having armed a realtime session and logged nothing
    but success. ``SUPPRESS`` means an unspecified sub-parser flag contributes
    NOTHING to the namespace, so the parent's value survives and either operand
    order works.
    """

    def _default(value):
        return argparse.SUPPRESS if inherit else value

    parser.add_argument(
        "--feed",
        default=_default("-"),
        metavar="PATH",
        help="Runtime-event JSONL source: a path (stream/FIFO/file) or '-' for stdin "
        "(default). The layer never spawns the runtime; it reads what the runtime "
        "writes (behavior engine run --export -). The MQTT bus is the intended "
        "primary route and falls back here today (events-cli is publish-only).",
    )
    parser.add_argument(
        "--media-profile",
        default=_default(None),
        dest="media_profile",
        metavar="PROFILE",
        help="Audio profile: 'robot' (the runtime's tee socket in, the daemon HTTP "
        "media route out — the default) or 'bench' (dev-box mic + speakers). "
        "Overrides REACHY_EMBODY_MEDIA_PROFILE. Same code path either way.",
    )
    parser.add_argument(
        "--spool-dir",
        default=_default(None),
        dest="spool_dir",
        metavar="DIR",
        help="Override the intents-spool root (default: the shared state dir, so the "
        "layer writes into the SAME spool the running engine drains).",
    )
    parser.add_argument(
        "--await-timeout",
        type=float,
        default=_default(1.0),
        dest="await_timeout",
        help="Seconds an action waits for the engine to confirm a spool command "
        "before returning 'submitted, unconfirmed' (default: 1.0).",
    )
    parser.add_argument(
        "--turn-interval",
        type=float,
        default=_default(DEFAULT_TURN_INTERVAL),
        dest="turn_interval",
        help=f"Seconds to wait between turns when nothing is pending "
        f"(default: {DEFAULT_TURN_INTERVAL}).",
    )
    parser.add_argument(
        "--mute-during-playback",
        action="store_true",
        default=_default(False),
        dest="mute_during_playback",
        help="Withhold microphone audio while the layer is speaking. OFF by default: "
        "Reachy has hardware AEC against its own speakers, so the layer keeps "
        "hearing while it talks (which is what makes barge-in possible). This is "
        "the one-flip fallback if live AEC proves insufficient.",
    )
    parser.add_argument(
        "--attention-window",
        type=float,
        default=_default(None),
        dest="attention_window",
        metavar="SECONDS",
        help="Seconds attention stays open after the last utterance heard or answer "
        "spoken; 0 means name-only forever (issue #150). Precedence: this flag, "
        f"then {ENV_ATTENTION_WINDOW_S} in the process environment, then the "
        f"layer's own default ({DEFAULT_ATTENTION_WINDOW_S:g}s). Like every other "
        f"flag in this group, unspecified here falls through to "
        f"{ENV_ATTENTION_WINDOW_S} at composition time, in THIS process (the "
        "foreground layer) or the spawned child (start/restart) alike.",
    )


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
        "direct-operation action set. See also: embody start/stop/restart/status "
        "to run it as a background process.",
    )
    _add_embody_operating_args(embody)
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

    # embody start/stop/restart/status (task t12) — the supervisor half. Nested
    # as SUB-subcommands of `embody` (not siblings of it under `agent`) so bare
    # `agent embody` keeps running the foreground loop unchanged (h17: one
    # command each way is `embody start` / `embody stop`, not a new top-level
    # verb family). parser_class propagates so nested parse errors keep the
    # structured error contract.
    embody_sub = embody.add_subparsers(dest="embody_command", parser_class=type(embody))

    embody_start = embody_sub.add_parser(
        "start", help="Start the embodiment layer in the background (idempotent)."
    )
    _add_embody_operating_args(embody_start, inherit=True)
    embody_start.add_argument("--json", action="store_true", help=_JSON_HELP)
    embody_start.set_defaults(func=cmd_agent_embody_start)

    embody_stop = embody_sub.add_parser("stop", help="Stop the layer this CLI started.")
    embody_stop.add_argument(
        "--timeout",
        type=float,
        default=EMBODY_DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait after SIGTERM before SIGKILL "
        f"(default: {EMBODY_DEFAULT_STOP_TIMEOUT:g}).",
    )
    embody_stop.add_argument("--json", action="store_true", help=_JSON_HELP)
    embody_stop.set_defaults(func=cmd_agent_embody_stop)

    embody_restart = embody_sub.add_parser(
        "restart", help="Restart the background layer (re-reads code/flags)."
    )
    _add_embody_operating_args(embody_restart, inherit=True)
    embody_restart.add_argument("--json", action="store_true", help=_JSON_HELP)
    embody_restart.set_defaults(func=cmd_agent_embody_restart)

    embody_status = embody_sub.add_parser(
        "status", help="Report the layer's process state (pid + liveness)."
    )
    embody_status.add_argument("--json", action="store_true", help=_JSON_HELP)
    embody_status.set_defaults(func=cmd_agent_embody_status)
