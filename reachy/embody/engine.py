"""The embodiment layer's cognition loop: cues + utterances in, streamed turns out.

This is the layer's MIND. It sits between two things that already exist: the
perception side (:mod:`reachy.embody.cues` mapping the runtime's own exported
events to cue text, and :class:`~reachy.speech.realtime_duplex.
RealtimeDuplexSession` handing over what it heard) and the action side
(:class:`~reachy.embody.tools.EmbodyToolRegistry`, the closed five-tool set).
It contributes exactly one thing of its own: a streaming
``/v1/chat/completions`` turn loop that turns the former into the latter.

Cue-triggered, not polled
-------------------------
:class:`~reachy.speech.agent_turn.AgentTurnEngine` is the closest relative and
several seams are cited from it verbatim (the bounded rolling history, the
``run(max_turns=, stop=, before_turn=)`` loop shape, the export-before-dispatch
ordering, the ``max_tool_rounds`` bound). One thing is deliberately NOT
inherited:

* **No permanent failure latch.** That engine mutes its audio sink for the
  process lifetime after a streak of failures. This one runs beside a robot
  that is meant to stay switched on: every failure is a named, counted drop and
  the very next turn tries again. A layer that goes permanently quiet because
  the gateway blipped is indistinguishable from a layer that crashed.

Three input classes, not one queue (issue #143)
-----------------------------------------------
A turn is TRIGGERED — that is what makes "the robot's own rule fired, so it
says something about it" possible at all — but not by everything that arrives.
Measured live on 2026-08-02 with the bus bridged into the feed: **187 cues in
~40 s produced 23 turns and 19 queue-full drops**, and not one of those turns
was prompted by something the robot DECIDED. The mix was 145 "speech from the
left/ahead/right" plus 44 "loud sound", zero rule fires. The layer already has
its own ears (a ``/v1/realtime`` duplex session with server-side VAD), so
those cues told it nothing it did not already hear — they simply arrived at
tick rate instead of utterance rate. So intake splits three ways:

============  ==========================================  =================
class         events                                      effect
============  ==========================================  =================
heard         an utterance from the duplex session        runs a turn, if
                                                          ATTENTION admits it
**alert**     a rule FIRE                                 runs a turn
**context**   ``sense`` / ``intent`` / ``motion``, and a   parked, drained
              rule SUPPRESSION                            by the next turn
============  ==========================================  =================

The "if attention admits it" qualifier is issue #148, and it is the one clause
of this policy that is about WHO rather than about WHAT. The ear stays ungated
— the duplex session surfaces every voice in the room — but a turn is woken
only by an utterance that named the robot, or by one arriving while a
conversation the name opened is still live. The whole rule lives in
:mod:`reachy.embody.attention`; what matters here is that it gates the HEARD
class alone. An alert is the robot's own reflex firing: attention gates the
ear, never the robot's reactions, so a rule fire runs a turn from cold.

The context half is where :class:`~reachy.speech.agent_turn.AgentTurnEngine`'s
snapshot-only buffer is CITED rather than imported: cues that arrive during a
turn accumulate for the next one and cause none of their own. What is added on
top is COALESCING — 145 near-identical lines must reach a turn as one fact
carrying a count, not as 145 strings — keyed on the rendered cue text, which
is exactly the identity the closed cue vocabulary already expresses (see
:mod:`reachy.runtime_cues`: a fixed phrase per perception, so equal text means
the same fact happened again).

Alert containment, because the flood has a front door too
----------------------------------------------------------
``reachy/behavior/rules.py`` permits ``cooldown_s = 0`` and several rules can
fire in one tick, so "only alerts trigger" alone would let the same flood back
in wearing the one class that is allowed through. Two bounds close it, and
both are about turns rather than about cues:

* alerts arriving while a turn is pending or running COALESCE into the ONE
  turn that drains them next — the trigger buffer is drained whole, so ten
  fires inside one turn window cost a second turn, never ten;
* :data:`DEFAULT_MIN_ALERT_INTERVAL_S` bounds how often an alert may TRIGGER.
  Inside the interval an alert is DEFERRED, never dropped: it stays pending
  and rides the next turn that runs. An utterance is exempt — a person talking
  outranks a rate limit — and the first alert after quiet is never delayed,
  because the interval is measured from the last alert-triggered turn.

Both bounds are observable by construction: every turn's senselog line and its
exported ``thinking`` block carry ``triggers=T context=N coalesced-from=M``. A
silent coalescer is indistinguishable from a dropper.

Every LLM call streams (spec claim c6), and the reason is measured
------------------------------------------------------------------
Both lanes — the tool-bearing turn (:meth:`run_turn`, the ``worker`` model) and
the tool-less perception question (:meth:`ask`, the ``senses`` model) — go
through :func:`reachy.speech.llm.stream_turn` with ``stream=true``. Non-streaming
was not a style choice to reject: with thinking enabled, our own gateway took
**43.2 s** to the first content delta while the largest gap BETWEEN chunks was
**0.124 s** (``docs/evidence/2026-08-01-cited-findings-from-embodiment-
sibling.md``). A total deadline that survives the former is uselessly long for
detecting the latter.

Which is exactly why honesty condition h6 — "a stalled stream resolves as a
named timeout drop, never a hang" — is armed on **inter-chunk idle**, never on
total elapsed. The mechanism is that ``urlopen``'s timeout becomes the SOCKET
timeout, so it applies per read: a stream that keeps producing is never killed
however long the whole turn takes, and one that goes quiet is named
(:data:`REASON_STREAM_IDLE`) within one idle budget. Arm it on total elapsed and
every long think dies looking like a broken model.
:data:`DEFAULT_IDLE_TIMEOUT_S` is generous for one reason: the FIRST read also
covers time-to-first-token, and the gateway lazy-loads the worker model.

The model is a per-request field, from process env only
-------------------------------------------------------
:class:`EmbodyModels` resolves ``worker`` and ``senses`` from
:data:`ENV_WORKER_MODEL` / :data:`ENV_SENSES_MODEL` (defaulting to the ROLE
names, which lobes' ``resolve_model`` accepts), and the chosen name travels as
the request body's ``model`` field — one per call. It reads no file and writes
no variable, and that is a requirement rather than an implementation detail: an
``environment.d`` drop-in would re-point the RUNTIME's engagement classifier
too, silently changing the reflex robot while configuring the layer.
``tests/test_embody_engine.py`` proves both halves, the second by AST.

The export contract
-------------------
Per turn the engine emits, through the shared
:class:`~reachy.export.exporter.ExportHook` (``docs/export-schema.md``):

* ``message`` — one per voice tool call (``speak`` / ``harmonics``), emitted
  BEFORE dispatch, and one per :meth:`note_spoken`. As the schema says of
  ``agent attach``'s publish-only seams, the block names the utterance the mind
  CHOSE, not a speaker that moved.
* ``emotion`` — when the model's own reply text carries an emoji, resolved to a
  pose through the hook's ``pose_resolver`` (the shipped expressions catalog).
  The layer's action set has no ``apply_pose`` tool, so this is the one place an
  expression can come from; it is a plain codepoint scan
  (:func:`first_emoji`), NOT a resurrection of the retired ``*emoji*`` marker
  grammar — the text is neither consumed nor rewritten.
* ``thinking`` — exactly one per turn, last, carrying every perception line the
  turn read (its triggers first, then the drained context) and the raw turn
  text, which OPENS with this turn's ``[triggers=… context=… coalesced-from=…]``
  drain counts and continues with the model's streamed ``reasoning`` (see
  :data:`reachy.speech.llm.REASONING_DELTA_KEYS` — the gateway sends
  ``reasoning``, not the documented ``reasoning_content``), its content, every
  tool call, every tool RESULT including refusals, and any named drop. That last
  part is what puts the red-team refusals on the feed.

Import boundary
---------------
Like the rest of ``reachy/embody/``: no ``reachy_mini``, no ``reachy.daemon``,
no subprocess, no shell (``tests/test_embody_redteam.py`` walks this package by
AST). The audio devices belong to :mod:`reachy.embody.media` and the socket to
:mod:`reachy.speech.realtime_duplex`; this module owns no I/O but the one HTTP
lane, and reaches even that through an injectable ``turn_fn``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from reachy import senselog
from reachy.cli._errors import CliError
from reachy.embody.attention import DEFAULT_ATTENTION_WINDOW_S, LABEL_COLD, AttentionGate
from reachy.embody.cues import ClassifiedCue, CueClass
from reachy.embody.tools import HARMONICS, SPEAK
from reachy.export.events import EmotionEvent, MessageEvent, ThinkingEvent
from reachy.export.exporter import ExportHook
from reachy.speech import llm as _llm

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Senselog identity                                                           #
# --------------------------------------------------------------------------- #

#: ``[SENSE stage=turn source=embody event=<id>]`` — the same ``turn`` stage
#: :class:`~reachy.speech.agent_turn.AgentTurnEngine` logs under, with the layer's
#: own source so one journal can be split by which mind thought what.
STAGE = "turn"
SOURCE = "embody"

# --------------------------------------------------------------------------- #
# Model roles                                                                 #
# --------------------------------------------------------------------------- #

#: The tool-bearing conversational lane (qwen on thor, per the spec).
ROLE_WORKER = "worker"
#: The cheap perception lane (gemma), used by :meth:`EmbodyTurnEngine.ask`.
ROLE_SENSES = "senses"
#: Every role this module knows. A name outside it is refused, never guessed.
ROLES: tuple[str, ...] = (ROLE_WORKER, ROLE_SENSES)

#: Process-scoped overrides. Deliberately NOT ``REACHY_OPENAI_MODEL_ID``: that
#: one is read by the runtime's engagement classifier as well, so pointing it at
#: the layer's worker model would change the reflex robot's behaviour.
ENV_WORKER_MODEL = "REACHY_EMBODY_WORKER_MODEL"
ENV_SENSES_MODEL = "REACHY_EMBODY_SENSES_MODEL"

# --------------------------------------------------------------------------- #
# Named drop reasons — every failure names one, never a silent no-op          #
# --------------------------------------------------------------------------- #

#: No delta arrived within the inter-chunk idle budget (honesty condition h6).
REASON_STREAM_IDLE = "stream-idle-timeout"
#: The endpoint refused, was unreachable, or answered non-2xx.
REASON_ENDPOINT_UNREACHABLE = "llm-endpoint-unreachable"
#: The stream failed some other way (a reset, an unexpected fault in the turn).
REASON_STREAM_FAILED = "stream-failed"
#: The model kept calling tools past :data:`DEFAULT_MAX_TOOL_ROUNDS`.
REASON_TOOL_ROUNDS_EXHAUSTED = "tool-rounds-exhausted"
#: The pending-TRIGGER buffer was full; the newest trigger was refused.
REASON_INPUT_QUEUE_FULL = "input-queue-full"
#: The context park already holds :data:`DEFAULT_MAX_CONTEXT` DISTINCT facts and
#: a new one arrived. A repeat of a parked fact can never reach this: it
#: coalesces, so a flood of one perception cannot fill the park.
REASON_CONTEXT_PARK_FULL = "context-park-full"
#: A blank cue/utterance was submitted.
REASON_EMPTY_INPUT = "empty-input"
#: A turn produced no text, no reasoning and no tool call.
REASON_SILENT_TURN = "silent-turn"
#: An utterance arrived while attention was COLD and named nobody (issue #148).
#: Imported from :mod:`reachy.embody.attention` rather than retyped: the gate's
#: label IS the drop reason, exactly as the runtime's engagement labels are.
REASON_NOT_ADDRESSED_COLD = LABEL_COLD

#: Every reason this module can emit, in one place so the journal, the export
#: feed, the operator docs and the tests share ONE vocabulary.
DROP_REASONS: tuple[str, ...] = (
    REASON_STREAM_IDLE,
    REASON_ENDPOINT_UNREACHABLE,
    REASON_STREAM_FAILED,
    REASON_TOOL_ROUNDS_EXHAUSTED,
    REASON_INPUT_QUEUE_FULL,
    REASON_CONTEXT_PARK_FULL,
    REASON_EMPTY_INPUT,
    REASON_SILENT_TURN,
    REASON_NOT_ADDRESSED_COLD,
)

# --------------------------------------------------------------------------- #
# Defaults                                                                    #
# --------------------------------------------------------------------------- #

#: The inter-chunk idle budget, in seconds. It bounds ONE read, so it must also
#: cover time-to-first-token — the gateway lazy-loads the worker model, and the
#: probe measured 43.2 s to the first content delta with thinking on. A stall
#: therefore costs one budget of silence and then names itself; a long think
#: costs nothing at all, because every later chunk resets the clock.
DEFAULT_IDLE_TIMEOUT_S = 90.0
#: Sampling temperature for both lanes.
DEFAULT_TEMPERATURE = 0.7
#: Rounds one turn may take before the tool loop is force-stopped (cited from
#: :data:`reachy.speech.agent_turn.DEFAULT_MAX_TOOL_ROUNDS`).
DEFAULT_MAX_TOOL_ROUNDS = 6
#: Prior (perception, reply) pairs kept for context — the same 6-entry
#: discipline the engagement classifier and the agent engine use.
DEFAULT_HISTORY_MAXLEN = 6
#: Pending TRIGGERS (utterances + alerts) held between turns. Bounded: a
#: runtime feed that outruns cognition must drop the NEWEST by name, never grow
#: without bound.
DEFAULT_MAX_PENDING = 32
#: DISTINCT facts the context park holds. Small on purpose: the cue vocabulary
#: is closed and the measured 40 s flood was six facts arriving 187 times, so a
#: park that needs more than this is describing a robot in a genuinely novel
#: situation, not a busy one.
DEFAULT_MAX_CONTEXT = 24
#: Seconds between ALERT-triggered turns. The first alert after quiet is never
#: delayed; this only bites on a burst, where the fires it holds back are
#: deferred into the next turn rather than dropped. Sized against the measured
#: defect: 23 turns in 40 s (~34/min) becomes at most 12/min from alerts, while
#: a single reflex the robot should react to still gets an immediate turn.
DEFAULT_MIN_ALERT_INTERVAL_S = 5.0
#: Recent already-spoken replies carried into the next turn's context.
DEFAULT_SPOKEN_MAXLEN = 4
#: Minimum gap between turns in :meth:`EmbodyTurnEngine.run`.
DEFAULT_TURN_INTERVAL = 0.5

#: Tool calls exported as ``message`` blocks. Imported from the action set, never
#: retyped, so a rename cannot leave the export mapping pointing at a dead name.
DEFAULT_VOICE_TOOLS: frozenset[str] = frozenset({SPEAK, HARMONICS})

DEFAULT_EMBODY_SYSTEM_PROMPT = (
    "You are Reachy Mini, a small expressive desk robot, present in the room with "
    "the people you can hear. You perceive two kinds of thing: what people say "
    "near you, and what your own body just did on its own — its reflex rules "
    "firing, a hand petting your head, a face appearing. Your spoken conversation "
    "is already handled: you do not need to reply in words to everything you hear. "
    "What you decide here is what to DO. You act only through your tools: goto "
    "(move your head, antennas or body), run_behavior (run one of your movement "
    "sets), speak and harmonics (say something out loud, or chirp), and "
    "create_rule (teach yourself a new standing reaction that keeps firing on its "
    "own afterwards). When nothing is worth doing, do nothing and call no tools. "
    "Keep any speech to one or two short, natural first-person sentences. Never "
    "narrate raw sensor readings. If you want to show an expression, put a single "
    "emoji in your reply text."
)

# --------------------------------------------------------------------------- #
# Input kinds                                                                 #
# --------------------------------------------------------------------------- #

#: A runtime perception the robot's own reflexes DECIDED — a rule fire. The one
#: cue class that triggers, because it is the one the layer cannot learn any
#: other way (see :class:`reachy.embody.cues.CueClass`).
KIND_ALERT = "alert"
#: Something a person said. The layer HEARS everyone (spec claim c4, pinned in
#: the wire); whether what it heard wakes the mind is
#: :mod:`reachy.embody.attention`'s decision, taken in
#: :meth:`EmbodyTurnEngine.submit_utterance`.
KIND_UTTERANCE = "utterance"


@dataclass(frozen=True)
class Input:
    """One pending TRIGGER: what kind it was, and the text a turn will read."""

    kind: str
    text: str

    def render(self) -> str:
        """The line this input contributes to a turn's perception list."""
        if self.kind == KIND_UTTERANCE:
            return f'heard: "{self.text}"'
        return self.text


@dataclass
class Parked:
    """One CONTEXT fact in the park, and how many times it has been perceived.

    Mutable, unlike :class:`Input`, because coalescing IS a mutation of the
    entry already there: the whole point is that the 145th "speech from the
    left" costs one increment rather than a 145th list slot. Every mutation
    happens under the engine's intake lock and is O(1).
    """

    text: str
    count: int = 1

    def render(self) -> str:
        """``"speech from the left (x145)"`` — or the bare fact when seen once.

        A single sighting reads as a fact, not a tally: ``(x1)`` on every quiet
        line would be noise in the one place the model is meant to skim.
        """
        return self.text if self.count == 1 else f"{self.text} (x{self.count})"


# --------------------------------------------------------------------------- #
# Model selection                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EmbodyModels:
    """The per-request model name for each lane.

    The defaults are the ROLE names themselves: lobes' ``resolve_model`` accepts
    a role, which is what keeps a gateway-side model promotion from breaking the
    layer (the deployed ``worker`` role has already moved once). An operator who
    wants a specific served id sets :data:`ENV_WORKER_MODEL` /
    :data:`ENV_SENSES_MODEL` in the LAYER PROCESS's environment — never in
    ``environment.d``, which the runtime reads too.
    """

    worker: str = ROLE_WORKER
    senses: str = ROLE_SENSES

    @classmethod
    def resolve(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        worker: str | None = None,
        senses: str | None = None,
    ) -> "EmbodyModels":
        """Resolve from explicit arguments, then *env*, then the role names.

        *env* defaults to ``os.environ`` — the PROCESS environment, read once
        per call and never written. No file is opened here, by design and by
        test.
        """
        source = env if env is not None else os.environ
        return cls(
            worker=worker or source.get(ENV_WORKER_MODEL) or ROLE_WORKER,
            senses=senses or source.get(ENV_SENSES_MODEL) or ROLE_SENSES,
        )

    def model_for(self, role: str) -> str:
        """The model name for *role*; an unknown role is refused, not guessed."""
        if role == ROLE_WORKER:
            return self.worker
        if role == ROLE_SENSES:
            return self.senses
        raise ValueError(f"unknown model role {role!r}; the layer has exactly {ROLES}")


# --------------------------------------------------------------------------- #
# Bounds, grouped into one frozen home (issue #141, python:S107)              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Limits:
    """:class:`EmbodyTurnEngine`'s numeric bounds, out of the constructor's kwargs.

    Every field here was a bare keyword parameter on
    :class:`EmbodyTurnEngine` before this task; the constructor's OTHER
    parameters are injected SEAMS (a collaborator, a callable tap, a clock)
    and none of those moved — grouping seams in here too would just relocate
    the S107 complaint rather than fix its actual defect. This class does not
    re-explain each bound: the measured reasoning behind every default lives
    with its ``DEFAULT_*`` constant above (the one documented home this module
    already keeps), and every field here simply carries that same constant
    forward unchanged, so the refactor cannot silently change a number while
    moving it.
    """

    #: The inter-chunk idle budget passed to every streamed call.
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S
    #: Rounds one turn may take before the tool loop is force-stopped.
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    #: Prior (perception, reply) pairs kept in the rolling history.
    history_maxlen: int = DEFAULT_HISTORY_MAXLEN
    #: Pending TRIGGERS (utterances + alerts) held between turns.
    max_pending: int = DEFAULT_MAX_PENDING
    #: DISTINCT facts the context park holds.
    max_context: int = DEFAULT_MAX_CONTEXT
    #: Seconds between ALERT-triggered turns; ``0`` disables the bound.
    min_alert_interval_s: float = DEFAULT_MIN_ALERT_INTERVAL_S
    #: Recent already-spoken replies carried into the next turn's context.
    spoken_maxlen: int = DEFAULT_SPOKEN_MAXLEN
    #: Minimum gap between turns in :meth:`EmbodyTurnEngine.run`.
    turn_interval: float = DEFAULT_TURN_INTERVAL
    #: How long attention stays open after the last utterance heard or answer
    #: spoken (issue #148); ``0`` means name-only forever. It lives here rather
    #: than as a constructor parameter for the reason this class exists at all:
    #: a loose bound would put the count back over ``python:S107``'s threshold.
    #: The measured argument for the default is on
    #: :data:`reachy.embody.attention.DEFAULT_ATTENTION_WINDOW_S`.
    attention_window_s: float = DEFAULT_ATTENTION_WINDOW_S


@dataclass(frozen=True)
class RequestConfig:
    """The per-call LLM request template, grouped for the SAME reason as :class:`Limits`.

    :class:`Limits` alone — the resource/time bounds issue #141 names by
    example (``max_tool_rounds``, the several timeouts, …) — still leaves this
    engine's constructor at 17 parameters. This project's configured
    ``python:S107`` threshold is **13 authorized parameters** (verified
    against SonarCloud, not assumed), so bounds alone do not clear the rule
    here — measured, not guessed, and the reason this second dataclass exists
    at all. Its six fields are neither seams (none is a callable) nor
    resource/time bounds; they are the plain, per-call shape of every
    streamed request: which system message opens the turn, which endpoint and
    key to call, and how the model samples. Every field keeps the exact
    default it had as a bare parameter.
    """

    #: The system message on every turn.
    system_prompt: str = DEFAULT_EMBODY_SYSTEM_PROMPT
    #: Forwarded to *turn_fn* per call. ``None`` lets
    #: :class:`reachy.speech.llm.LlmConfig` resolve them from the
    #: ``REACHY_OPENAI_*`` environment as usual.
    base_url: str | None = None
    api_key: str | None = None
    #: Sampling controls, forwarded per call.
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int | None = None
    #: Ask the server for streamed reasoning. Off by default, and the default
    #: is a PRODUCT decision about a robot that answers out loud — measured
    #: live against the deployed gateway on 2026-08-02
    #: (``docs/evidence/2026-08-02-probe-thinking-vs-reasoning-deltas.md``)::
    #:
    #:     model    enable_thinking   delta keys        first *content*
    #:     worker   False (shipped)   content, role     0.22 s
    #:     worker   True              + reasoning       9.72 s
    #:     cortex   False (shipped)   content, role     0.27 s
    #:     cortex   True              + reasoning       17.96 s
    #:
    #: So turning this on costs 9-18 SECONDS before the robot says or does
    #: anything. For a layer whose whole point is realtime conversation that
    #: is not a trade worth making, and no amount of tuning elsewhere
    #: recovers it. The consequence is worth stating plainly rather than
    #: discovering: with the shipped default the gateway sends **no
    #: reasoning key at all**, so :attr:`TurnResult.reasoning` is empty and
    #: the exported ``thinking`` block carries cues, reply text, tool calls
    #: and results — but no model reasoning. The reasoning seam is correct
    #: and dormant, NOT broken. Flip this to ``True`` and it fills
    #: immediately.
    enable_thinking: bool = False


# --------------------------------------------------------------------------- #
# Collaborator protocols (documentation; any matching object is accepted)     #
# --------------------------------------------------------------------------- #


class _RegistryLike(Protocol):
    def tools(self) -> list[dict]: ...

    def dispatch(self, name: str, arguments_json=None, tool_call_id=None) -> dict: ...


class _TurnFn(Protocol):
    def __call__(self, messages: list[dict], **kwargs) -> _llm.TurnResult: ...


# --------------------------------------------------------------------------- #
# Emoji scan — the layer's only expression source                             #
# --------------------------------------------------------------------------- #

#: Codepoint ranges treated as an expression emoji: misc symbols + dingbats, and
#: the pictograph planes the shipped catalog's keys live in (🤔 U+1F914 …).
_EMOJI_RANGES: tuple[tuple[int, int], ...] = ((0x2600, 0x27BF), (0x1F000, 0x1FAFF))
#: Joiners and variation selectors are modifiers, never the expression itself.
_EMOJI_SKIP: frozenset[int] = frozenset({0x200D, 0xFE0E, 0xFE0F})


def first_emoji(text: str) -> str | None:
    """The first expression emoji in *text*, or ``None``.

    A plain codepoint scan — not a grammar. The retired ``*emoji*`` marker
    parser had to find delimiters, strip them and split speech out of the
    stream; this only reports whether the model chose to show a face, and leaves
    the text exactly as it was.
    """
    for char in text:
        code = ord(char)
        if code in _EMOJI_SKIP:
            continue
        if any(low <= code <= high for low, high in _EMOJI_RANGES):
            return char
    return None


# --------------------------------------------------------------------------- #
# The engine                                                                  #
# --------------------------------------------------------------------------- #


class EmbodyTurnEngine:
    """Streaming, cue-triggered cognition over the layer's closed action set.

    Every collaborator is injected, so the whole engine is exercised with no
    gateway, no robot, no threads and no clock.

    Args:
        registry: the action set. Anything exposing ``tools()`` and
            ``dispatch(name, arguments_json, tool_call_id)`` — in production
            :class:`reachy.embody.tools.EmbodyToolRegistry`, which never raises
            and returns a named refusal instead.
        turn_fn: the streaming turn function, default
            :func:`reachy.speech.llm.stream_turn`. Called as
            ``turn_fn(messages, model=…, tools=…, timeout=…, on_content=…,
            on_reasoning=…, cancel=…, …)``.
        export: the shared :class:`~reachy.export.exporter.ExportHook`. ``None``
            means no export path is entered at all.
        models: :class:`EmbodyModels`; default :meth:`EmbodyModels.resolve`.
        request: the per-call LLM request template — the system prompt, the
            endpoint + key, and the sampling controls (``temperature`` /
            ``max_tokens`` / ``enable_thinking``) — grouped into one frozen
            :class:`RequestConfig`. Grouped for the same S107 reason as
            ``limits`` below (see :class:`RequestConfig`'s docstring for why
            bounds alone do not clear the rule here); every field keeps the
            exact default it had as a bare parameter.
        limits: the engine's numeric bounds — the inter-chunk idle timeout, the
            tool-round cap, the rolling-history / pending-trigger / context-park
            / already-spoken sizes, the alert-containment interval (issue
            #143) and the inter-turn pacing — grouped into one frozen
            :class:`Limits` (issue #141/``python:S107``). Every field keeps
            the exact default it had as a bare parameter; see :class:`Limits`
            for what each one bounds.
        voice_tools: tool names exported as ``message`` blocks.
        on_content / on_reasoning: optional taps fired per delta, on the calling
            thread, as the stream arrives.
        cancel: zero-arg predicate; truthy aborts an in-flight stream after the
            current chunk. A composition root passes the same predicate it gives
            :meth:`run`'s ``stop``.
        now_fn: the monotonic clock the alert interval is measured on
            (default :func:`time.monotonic`). Injected so the containment
            bounds are testable without sleeping.
        sleep: the callable :meth:`run` sleeps with between turns (default
            :func:`time.sleep`), paced by ``limits.turn_interval``.
    """

    def __init__(
        self,
        *,
        registry: _RegistryLike,
        turn_fn: _TurnFn | None = None,
        export: ExportHook | None = None,
        models: EmbodyModels | None = None,
        request: RequestConfig | None = None,
        limits: Limits | None = None,
        voice_tools: frozenset[str] | None = None,
        on_content: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        now_fn: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._registry = registry
        self._turn_fn = turn_fn if turn_fn is not None else _llm.stream_turn
        self._export = export
        self._models = models if models is not None else EmbodyModels.resolve()
        self._request = request if request is not None else RequestConfig()
        self._system_prompt = self._request.system_prompt
        self._base_url = self._request.base_url
        self._api_key = self._request.api_key
        self._temperature = float(self._request.temperature)
        self._max_tokens = self._request.max_tokens
        self._limits = limits if limits is not None else Limits()
        self._idle_timeout_s = max(0.1, float(self._limits.idle_timeout_s))
        self._enable_thinking = bool(self._request.enable_thinking)
        self._max_tool_rounds = max(1, int(self._limits.max_tool_rounds))
        self._voice_tools = voice_tools if voice_tools is not None else DEFAULT_VOICE_TOOLS
        self._on_content = on_content
        self._on_reasoning = on_reasoning
        self._cancel = cancel if cancel is not None else _never
        self._now = now_fn if now_fn is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._turn_interval = float(self._limits.turn_interval)

        self._triggers: deque[Input] = deque(maxlen=None)
        self._max_pending = max(1, int(self._limits.max_pending))
        # Insertion-ordered by construction (``dict``), so the park reads back
        # in the order the robot first noticed each fact — stable across a
        # flood, where a recency ordering would churn every line every tick.
        self._context: dict[str, Parked] = {}
        self._max_context = max(1, int(self._limits.max_context))
        self._min_alert_interval_s = max(0.0, float(self._limits.min_alert_interval_s))
        # ONE clock for the layer: the gate is a time-based state machine and a
        # second clock would make "the window elapsed" untestable and, under an
        # injected clock, wrong.
        self._attention = AttentionGate(window_s=self._limits.attention_window_s, clock=self._now)
        # -inf, never 0.0: an injected clock may start anywhere, and the FIRST
        # alert after quiet must never be the one the interval delays.
        self._last_alert_turn = float("-inf")
        self._deferral_logged = False
        self._spoken: deque[str] = deque(maxlen=max(0, int(self._limits.spoken_maxlen)))
        self._history: deque[tuple[str, str]] = deque(
            maxlen=max(0, int(self._limits.history_maxlen))
        )
        self._last_text = ""
        # One turn at a time; ``ask`` is deliberately outside it.
        self._turn_lock = threading.Lock()
        # Guards the two intake structures ONLY, and is never held across a
        # turn, an LLM call or a log write: two threads submit (the cue reader
        # and the duplex utterance tap) while a third drains under
        # ``_turn_lock``, and both bounds are check-then-act.
        self._intake_lock = threading.Lock()

        self.turns = 0
        self.rounds = 0
        self.tool_calls = 0
        self.refusals = 0
        self.stream_timeouts = 0
        self.stream_failures = 0
        self.dropped_inputs = 0
        # Counted apart from ``dropped_inputs``, which means "a bound was hit":
        # an unaddressed utterance is not a resource failure, it is the gate
        # working, and folding the two would make a busy room look like a sick
        # layer on the summary line.
        self.unaddressed_utterances = 0

    # ------------------------------------------------------------------ #
    # Intake — O(1), safe from any thread, never raises                  #
    # ------------------------------------------------------------------ #

    def submit_cue(self, text: str, *, cue_class: CueClass = CueClass.CONTEXT) -> bool:
        """Offer one runtime perception cue. Returns whether it was accepted.

        The class defaults to :attr:`~reachy.embody.cues.CueClass.CONTEXT`
        because that is the fail-safe direction of the #143 policy: a caller
        that has not thought about which lane a cue belongs to must not be able
        to wake the mind up by accident. An ALERT is always named explicitly.
        """
        if cue_class is CueClass.ALERT:
            return self._offer_trigger(KIND_ALERT, text)
        return self._offer_context(text)

    def submit_cues(self, cues: Iterable[str | ClassifiedCue]) -> int:
        """Offer several cues, routing each by its class. Returns how many landed.

        Accepts what :func:`reachy.embody.cues.classified_cues_for_line`
        returns — the composition root's intake — and, for a caller that has no
        classification to give, bare strings, which park.
        """
        accepted = 0
        for cue in cues:
            if isinstance(cue, ClassifiedCue):
                accepted += self.submit_cue(cue.text, cue_class=cue.cue_class)
            else:
                accepted += self.submit_cue(cue)
        return accepted

    def submit_utterance(self, text: str) -> bool:
        """Offer one heard utterance, subject to ATTENTION (issue #148).

        The layer's ear stays ungated — the duplex session surfaces every voice
        in the room and its own boundary tests pin that — but hearing is not
        the same as being addressed. While attention is cold only an utterance
        that NAMES the robot wakes a turn; while it is warm anything does, and
        every admission extends the window. A refusal is a NAMED drop carrying
        the text it ignored, never a silent no-op: "why is it ignoring me?" has
        to be answerable from the journal.

        The gate deliberately runs BEFORE the pending-trigger bound, and the
        admission stands even if that bound then refuses the utterance: the
        robot was addressed, which is a fact about the room, not about how full
        a queue happened to be.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            self._drop(REASON_EMPTY_INPUT, KIND_UTTERANCE)
            return False
        verdict = self._attention.decide(cleaned)
        if not verdict.admitted:
            self.unaddressed_utterances += 1
            self._drop(verdict.label, f'"{cleaned[:60]}"')
            return False
        if verdict.opened:
            senselog.stage(
                STAGE,
                SOURCE,
                uuid.uuid4().hex[:8],
                f"attention open ({verdict.label}) for {self._attention.window_s:g}s",
            )
        return self._offer_trigger(KIND_UTTERANCE, cleaned)

    def note_spoken(self, text: str) -> None:
        """Record something the layer's MOUTH already said. Does NOT trigger a turn.

        The duplex session answers speech on its own, server-side. Without this
        the thinking mind would have no idea it had already replied and would
        cheerfully call ``speak`` to say it again. It is context, not a trigger —
        a robot that treats its own voice as a perception talks to itself.

        It also EXTENDS attention, so a long answer cannot time the human out
        mid-exchange — but only while attention is already warm. That
        asymmetry is load-bearing: the session is armed once and the server
        replies to every committed utterance, including the ambient ones the
        gate has just refused, so a voice that could OPEN attention would be a
        robot waking itself up (see :mod:`reachy.embody.attention`).
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return
        self._spoken.append(cleaned)
        self._attention.note_spoken()
        if self._export is not None:
            self._export.emit(MessageEvent(text=cleaned, ts=self._export.time_fn()))

    @property
    def attention(self) -> AttentionGate:
        """The two-state attention gate (issue #148).

        Exposed rather than injected: it is state the engine owns and shares a
        clock with, and a composition root configures it through
        :attr:`Limits.attention_window_s` like every other bound. A caller that
        knows the robot was addressed some other way opens the window with
        :meth:`~reachy.embody.attention.AttentionGate.note_addressed`.
        """
        return self._attention

    @property
    def pending(self) -> int:
        """How many TRIGGERS are waiting for the next turn.

        Parked context is deliberately not counted: a composition root uses
        this to decide whether the layer still has thinking to do (see
        ``_EmbodyLayer.should_stop``), and context that can never cause a turn
        would keep a finished run spinning forever.
        """
        return len(self._triggers)

    @property
    def parked(self) -> int:
        """How many DISTINCT context facts the park is holding."""
        return len(self._context)

    @property
    def last_text(self) -> str:
        """The final assistant text of the last turn that ran (``""`` if it failed)."""
        return self._last_text

    def _offer_trigger(self, kind: str, text: str) -> bool:
        """Park-free intake for the two classes that RUN a turn. O(1)."""
        cleaned = (text or "").strip()
        if not cleaned:
            self._drop(REASON_EMPTY_INPUT, kind)
            return False
        with self._intake_lock:
            depth = len(self._triggers)
            if depth < self._max_pending:
                self._triggers.append(Input(kind=kind, text=cleaned))
                return True
        self.dropped_inputs += 1
        self._drop(REASON_INPUT_QUEUE_FULL, f"{kind} dropped, {depth} pending")
        return False

    def _offer_context(self, text: str) -> bool:
        """Coalescing intake for everything that never runs a turn. O(1).

        Keyed on the cue TEXT: the vocabulary is closed (one fixed phrase per
        perception, :mod:`reachy.runtime_cues`), so equal text means the same
        fact happened again, and the count is the only thing worth keeping
        about the repeat.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            self._drop(REASON_EMPTY_INPUT, "context")
            return False
        with self._intake_lock:
            entry = self._context.get(cleaned)
            if entry is not None:
                entry.count += 1
                return True
            distinct = len(self._context)
            if distinct < self._max_context:
                self._context[cleaned] = Parked(text=cleaned)
                return True
        self.dropped_inputs += 1
        self._drop(REASON_CONTEXT_PARK_FULL, f"{distinct} distinct facts parked")
        return False

    # ------------------------------------------------------------------ #
    # One turn                                                           #
    # ------------------------------------------------------------------ #

    def run_turn(self) -> bool:
        """Run one turn over everything pending. ``False`` when there was nothing.

        A turn runs only when a TRIGGER is waiting — an utterance or an alert.
        Parked context alone is never a reason to think (issue #143); it is
        drained into whatever turn a trigger causes next, and if none ever
        comes it is simply never read, which is the correct outcome for
        ambient background.

        The pending triggers are DRAINED into the turn, so a failure consumes
        them rather than retrying forever against a sick gateway — but the
        failure is always named, on the journal and on the export feed, and the
        perception still enters the rolling history so the next turn knows it
        happened.

        Exactly ONE turn runs at a time (cited from
        :meth:`reachy.speech.agent_turn.AgentTurnEngine.run_turn`): a second
        concurrent call blocks here rather than interleaving two conversations
        into one history. :meth:`ask` is deliberately NOT behind this lock — a
        perception question must not have to wait out a long turn.
        """
        with self._turn_lock:
            if not self._triggers or self._alert_deferred():
                return False
            triggers = self._drain_triggers()
            if not triggers:
                return False
            self._run_turn(triggers, self._drain_context())
            return True

    def _alert_deferred(self) -> bool:
        """Whether the pending triggers are alerts that must wait out the interval.

        The bound is on alert-triggered TURNS, not on alert cues: an alert held
        back here stays pending and rides the next turn that runs, so a burst
        costs latency, never a lost reflex. An utterance among the triggers
        lifts the bound outright — a person talking is not rate-limited — and
        the alerts waiting with it ride that turn too.
        """
        if self._min_alert_interval_s <= 0.0:
            return False
        with self._intake_lock:
            heard = any(item.kind == KIND_UTTERANCE for item in self._triggers)
            waiting = len(self._triggers)
        if heard:
            return False
        waited = self._now() - self._last_alert_turn
        if waited >= self._min_alert_interval_s:
            return False
        if not self._deferral_logged:
            # Once per deferral window: ``run`` re-asks every ``turn_interval``,
            # and a line per ask would bury the turn it is about to describe.
            self._deferral_logged = True
            senselog.stage(
                STAGE,
                SOURCE,
                uuid.uuid4().hex[:8],
                f"alert deferred waiting={waiting} for "
                f"{self._min_alert_interval_s - waited:.1f}s",
            )
        return True

    def _run_turn(self, triggers: list[Input], context: list[Parked]) -> None:
        event = uuid.uuid4().hex[:8]
        counts = (
            f"triggers={len(triggers)} context={len(context)} "
            f"coalesced-from={sum(entry.count for entry in context)}"
        )
        senselog.stage(STAGE, SOURCE, event, f"turn {counts}")
        self.turns += 1
        if any(item.kind == KIND_ALERT for item in triggers):
            self._last_alert_turn = self._now()
        self._deferral_logged = False
        before_refusals, before_rounds = self.refusals, self.rounds
        trigger_lines = [item.render() for item in triggers]
        context_lines = [entry.render() for entry in context]
        cues = trigger_lines + context_lines
        user_content = self._build_user_content(trigger_lines, context_lines, self._drain_spoken())
        conversation = self._build_messages(user_content)
        # Seeded, not appended: the drain counts open the block so a feed reader
        # can see what a turn was built from before reading what it thought.
        raw: list[str] = [f"[{counts}]"]

        result = self._tool_loop(conversation, raw, event)
        self._last_text = (result.content if result is not None else "") or ""
        self._history.append((user_content, self._last_text))
        if self._export is not None:
            self._export.emit(
                ThinkingEvent(
                    cues=cues,
                    text="\n".join(part for part in raw if part),
                    ts=self._export.time_fn(),
                )
            )
        senselog.stage(
            STAGE,
            SOURCE,
            event,
            f"turn done rounds={self.rounds - before_rounds} "
            f"refusals={self.refusals - before_refusals} chars={len(self._last_text)}",
        )

    def _tool_loop(
        self, conversation: list[dict], raw: list[str], event: str
    ) -> _llm.TurnResult | None:
        """The bounded round loop. Returns the last result, or ``None`` if none ran."""
        result: _llm.TurnResult | None = None
        for round_index in range(self._max_tool_rounds):
            result = self._stream(
                conversation, role=ROLE_WORKER, tools=self._registry.tools(), raw=raw
            )
            if result is None:
                return None
            self.rounds += 1
            self._render_result(result, raw)
            self._emit_expression(result)
            if not result.tool_calls:
                if round_index == 0 and not result.content and not result.reasoning:
                    self._drop(REASON_SILENT_TURN, "no text, no reasoning, no tool call")
                return result
            conversation.append(_assistant_tool_message(result))
            for call in result.tool_calls:
                self._process_tool_call(call, conversation, raw)
        self._drop(REASON_TOOL_ROUNDS_EXHAUSTED, f"stopped after {self._max_tool_rounds} rounds")
        raw.append(f"[drop reason={REASON_TOOL_ROUNDS_EXHAUSTED}]")
        senselog.stage(STAGE, SOURCE, event, "tool loop bound reached")
        return result

    # ------------------------------------------------------------------ #
    # The perception question (the senses lane)                          #
    # ------------------------------------------------------------------ #

    def ask(self, prompt: str, *, role: str = ROLE_SENSES, system: str | None = None) -> str:
        """Ask one tool-less streaming question and return the answer text.

        This is the ``senses`` lane: a cheap perception question (describe this
        clip, is that a face) whose answer becomes a cue, not an action. It
        publishes no tools — the ONE no-tools request the layer makes, which is
        why lobes-cli#161 (a tool call on a no-tools request returns
        ``content: null``) can cost at most an empty answer here and never a
        lost action. It emits no export block: the feed is about turns.
        """
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        result = self._stream(messages, role=role, tools=None, raw=None)
        return (result.content if result is not None else "") or ""

    # ------------------------------------------------------------------ #
    # The one streaming call                                             #
    # ------------------------------------------------------------------ #

    def _stream(
        self,
        messages: list[dict],
        *,
        role: str,
        tools: list[dict] | None,
        raw: list[str] | None,
    ) -> _llm.TurnResult | None:
        """One streamed call. Every failure is a NAMED drop and a ``None``, never a raise.

        ``timeout`` is the socket deadline, which applies PER READ — that is what
        makes :data:`REASON_STREAM_IDLE` an inter-chunk bound rather than a total
        one. See the module docstring; this is honesty condition h6.
        """
        kwargs: dict = {
            "model": self._models.model_for(role),
            "temperature": self._temperature,
            "timeout": self._idle_timeout_s,
            "base_url": self._base_url,
            "api_key": self._api_key,
            "on_content": self._on_content,
            "on_reasoning": self._on_reasoning,
            "enable_thinking": self._enable_thinking,
            "cancel": self._cancel,
        }
        if tools is not None:
            kwargs["tools"] = tools
        if self._max_tokens is not None:
            kwargs["max_tokens"] = self._max_tokens

        try:
            return self._turn_fn(messages, **kwargs)
        except TimeoutError:
            # socket.timeout IS TimeoutError, and it fires per READ: this is a
            # gap BETWEEN chunks, not a slow turn. Must precede the OSError arm.
            self.stream_timeouts += 1
            return self._fail(
                REASON_STREAM_IDLE,
                f"no delta for {self._idle_timeout_s:g}s on the {role} lane",
                raw,
            )
        except CliError as err:
            self.stream_failures += 1
            return self._fail(REASON_ENDPOINT_UNREACHABLE, err.message, raw)
        except OSError as err:
            self.stream_failures += 1
            return self._fail(REASON_STREAM_FAILED, f"{type(err).__name__}: {err}", raw)
        except Exception as err:  # noqa: BLE001 - a bad turn must never kill the layer
            self.stream_failures += 1
            logger.warning("[embody] %s turn raised", role, exc_info=True)
            return self._fail(REASON_STREAM_FAILED, f"{type(err).__name__}: {err}", raw)

    def _fail(self, reason: str, detail: str, raw: list[str] | None) -> None:
        self._drop(reason, detail)
        if raw is not None:
            raw.append(f"[drop reason={reason} {detail}]")
        return None

    # ------------------------------------------------------------------ #
    # Messages                                                           #
    # ------------------------------------------------------------------ #

    def _build_user_content(
        self, triggers: list[str], context: list[str], spoken: list[str]
    ) -> str:
        """The turn's perception, with the background kept visibly separate.

        Two sections rather than one list: what made the robot think, then what
        has merely been going on around it. Folded together, a coalesced
        ``"speech from the left (x145)"`` reads to the model exactly like the
        rule fire that actually woke it up.
        """
        lines = ["I just perceived:"]
        lines.extend(f"- {line}" for line in triggers)
        if context:
            lines.append("Meanwhile, in the background:")
            lines.extend(f"- {line}" for line in context)
        if spoken:
            lines.append("I have already said out loud:")
            lines.extend(f'- "{said}"' for said in spoken)
        return "\n".join(lines)

    def _build_messages(self, user_content: str) -> list[dict]:
        """System prompt + bounded rolling history + the current perception."""
        messages: list[dict] = [{"role": "system", "content": self._system_prompt}]
        for prior_user, prior_reply in self._history:
            messages.append({"role": "user", "content": prior_user})
            if prior_reply.strip():
                messages.append({"role": "assistant", "content": prior_reply})
        messages.append({"role": "user", "content": user_content})
        return messages

    def _drain_triggers(self) -> list[Input]:
        """Take every pending trigger at once.

        Draining WHOLE is the alert coalescer: ten rule fires waiting together
        become one turn's perception list, never ten turns.
        """
        with self._intake_lock:
            items = list(self._triggers)
            self._triggers.clear()
        return items

    def _drain_context(self) -> list[Parked]:
        """Take the parked facts this turn will show, emptying the park.

        Drained rather than snapshotted-and-kept: the park describes what has
        happened SINCE the last turn, so carrying an entry forward would make
        every later turn re-read the same background — the failure
        :meth:`_drain_spoken` avoids one buffer over.
        """
        with self._intake_lock:
            taken = list(self._context.values())
            self._context.clear()
        return taken

    def _drain_spoken(self) -> list[str]:
        """Take the already-spoken lines this turn will carry, emptying the buffer.

        Drained rather than read-then-cleared: a ``note_spoken`` landing between
        the read and the clear would otherwise be swallowed without ever having
        been shown to the model — a lost update that presents as the robot
        repeating itself, which is the exact failure this buffer exists to
        prevent.
        """
        taken: list[str] = []
        while self._spoken:
            taken.append(self._spoken.popleft())
        return taken

    # ------------------------------------------------------------------ #
    # Tool dispatch + export                                             #
    # ------------------------------------------------------------------ #

    def _process_tool_call(
        self, call: _llm.ToolCall, conversation: list[dict], raw: list[str]
    ) -> None:
        """Export the call's block, dispatch it, and feed the RESULT back in.

        The export comes first and independently of the dispatch outcome — cited
        from :meth:`reachy.speech.agent_turn.AgentTurnEngine._process_tool_call`,
        and matching ``docs/export-schema.md``'s "intent, not proof" semantics.
        The result (a refusal included, verbatim, with its name) is appended to
        the conversation, so the model learns in the SAME turn that the validator
        said no, and to the raw text, so the feed shows it too.
        """
        self.tool_calls += 1
        if self._export is not None and call.name in self._voice_tools:
            text = call.arguments.get("text")
            if isinstance(text, str) and text.strip():
                self._export.emit(MessageEvent(text=text, ts=self._export.time_fn()))

        message = self._registry.dispatch(call.name, call.arguments_json, call.id)
        refusal = _refusal_name(message)
        if refusal is not None:
            self.refusals += 1
        raw.append(f"-> {message.get('content')}")
        conversation.append(message)

    def _emit_expression(self, result: _llm.TurnResult) -> None:
        """Emit an ``emotion`` block when the model's own reply shows a face."""
        if self._export is None:
            return
        emoji = first_emoji(result.content or "")
        if emoji is None:
            return
        resolver = self._export.pose_resolver
        pose = resolver(emoji) if resolver is not None else None
        self._export.emit(EmotionEvent(emoji=emoji, pose=pose, ts=self._export.time_fn()))

    @staticmethod
    def _render_result(result: _llm.TurnResult, raw: list[str]) -> None:
        """Append one round's raw text: reasoning, content, then each tool call."""
        if result.reasoning:
            raw.append(result.reasoning)
        if result.content:
            raw.append(result.content)
        for call in result.tool_calls:
            raw.append(f"{call.name}({call.arguments_json})")

    # ------------------------------------------------------------------ #
    # The thin loop                                                      #
    # ------------------------------------------------------------------ #

    def run(
        self,
        *,
        max_turns: int | None = None,
        stop: Callable[[], bool] | None = None,
        before_turn: Callable[[], None] | None = None,
    ) -> int:
        """Run turns until stopped; returns how many RAN. Shape cited from the agent engine.

        Args:
            max_turns: stop after this many turns that actually ran.
            stop: zero-arg predicate checked before each turn.
            before_turn: called at the top of each iteration — how a composition
                root pumps freshly-read cues in before the turn reads them.
        """
        ran = 0
        first = True
        while True:
            if stop is not None and stop():
                break
            if max_turns is not None and ran >= max_turns:
                break
            if before_turn is not None:
                before_turn()
            if not first:
                self._sleep(self._turn_interval)
            first = False
            if self.run_turn():
                ran += 1
            elif before_turn is None and stop is None and max_turns is not None:
                # Nothing produces input and nothing will: stop rather than spin.
                break
        return ran

    # ------------------------------------------------------------------ #
    # Small helpers                                                      #
    # ------------------------------------------------------------------ #

    def _drop(self, reason: str, detail: str = "") -> None:
        senselog.drop(
            STAGE, SOURCE, uuid.uuid4().hex[:8], f"{reason} ({detail})" if detail else reason
        )


def _never() -> bool:
    return False


def _refusal_name(message: dict) -> str | None:
    """The ``refusal`` name in a tool result, or ``None`` when it was performed."""
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict) and payload.get("ok") is False:
        name = payload.get("refusal")
        return name if isinstance(name, str) else "unnamed-refusal"
    return None


def _assistant_tool_message(result: _llm.TurnResult) -> dict:
    """The OpenAI assistant message carrying this round's tool calls.

    Appended before the tool results so the next round sees its own calls paired
    with their outcomes (the OpenAI tool protocol) — cited from
    :func:`reachy.speech.agent_turn._assistant_tool_message`.
    """
    return {
        "role": "assistant",
        "content": result.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in result.tool_calls
        ],
    }
