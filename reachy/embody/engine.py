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
ordering, the ``max_tool_rounds`` bound). Two things are deliberately NOT
inherited:

* **No permanent failure latch.** That engine mutes its audio sink for the
  process lifetime after a streak of failures. This one runs beside a robot
  that is meant to stay switched on: every failure is a named, counted drop and
  the very next turn tries again. A layer that goes permanently quiet because
  the gateway blipped is indistinguishable from a layer that crashed.
* **No snapshot-only buffer.** A turn here is TRIGGERED by an arriving cue or
  utterance (:meth:`submit_cue` / :meth:`submit_utterance`), which is what makes
  "the robot's own rule fired, so it says something about it" possible at all.

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
* ``thinking`` — exactly one per turn, last, carrying the cues that triggered it
  and the raw turn text: the model's streamed ``reasoning`` (see
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
#: The pending-input buffer was full; the newest input was refused.
REASON_INPUT_QUEUE_FULL = "input-queue-full"
#: A blank cue/utterance was submitted.
REASON_EMPTY_INPUT = "empty-input"
#: A turn produced no text, no reasoning and no tool call.
REASON_SILENT_TURN = "silent-turn"

#: Every reason this module can emit, in one place so the journal, the export
#: feed, the operator docs and the tests share ONE vocabulary.
DROP_REASONS: tuple[str, ...] = (
    REASON_STREAM_IDLE,
    REASON_ENDPOINT_UNREACHABLE,
    REASON_STREAM_FAILED,
    REASON_TOOL_ROUNDS_EXHAUSTED,
    REASON_INPUT_QUEUE_FULL,
    REASON_EMPTY_INPUT,
    REASON_SILENT_TURN,
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
#: Pending cues/utterances held between turns. Bounded: a runtime feed that
#: outruns cognition must drop the NEWEST by name, never grow without bound.
DEFAULT_MAX_PENDING = 32
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

#: A runtime perception (a rule fired, a pat, a face) — see :mod:`reachy.embody.cues`.
KIND_CUE = "cue"
#: Something a person said, ungated (spec claim c4 — the layer hears everyone).
KIND_UTTERANCE = "utterance"


@dataclass(frozen=True)
class Input:
    """One pending trigger: what kind it was, and the text a turn will read."""

    kind: str
    text: str

    def render(self) -> str:
        """The line this input contributes to a turn's perception list."""
        if self.kind == KIND_UTTERANCE:
            return f'heard: "{self.text}"'
        return self.text


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
        system_prompt: the system message on every turn.
        base_url / api_key: forwarded to *turn_fn* per call. ``None`` lets
            :class:`reachy.speech.llm.LlmConfig` resolve them from the
            ``REACHY_OPENAI_*`` environment as usual.
        temperature / max_tokens: sampling controls, forwarded per call.
        idle_timeout_s: the INTER-CHUNK idle budget (see the module docstring).
        enable_thinking: ask the server for streamed reasoning. Off by default —
            it is what buys the 43 s time-to-first-content the probe measured.
        max_tool_rounds / history_maxlen / max_pending / spoken_maxlen: bounds.
        voice_tools: tool names exported as ``message`` blocks.
        on_content / on_reasoning: optional taps fired per delta, on the calling
            thread, as the stream arrives.
        cancel: zero-arg predicate; truthy aborts an in-flight stream after the
            current chunk. A composition root passes the same predicate it gives
            :meth:`run`'s ``stop``.
        sleep / turn_interval: inter-turn pacing for :meth:`run`.
    """

    def __init__(
        self,
        *,
        registry: _RegistryLike,
        turn_fn: _TurnFn | None = None,
        export: ExportHook | None = None,
        models: EmbodyModels | None = None,
        system_prompt: str = DEFAULT_EMBODY_SYSTEM_PROMPT,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int | None = None,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        enable_thinking: bool = False,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        history_maxlen: int = DEFAULT_HISTORY_MAXLEN,
        max_pending: int = DEFAULT_MAX_PENDING,
        spoken_maxlen: int = DEFAULT_SPOKEN_MAXLEN,
        voice_tools: frozenset[str] | None = None,
        on_content: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] | None = None,
        turn_interval: float = DEFAULT_TURN_INTERVAL,
    ) -> None:
        self._registry = registry
        self._turn_fn = turn_fn if turn_fn is not None else _llm.stream_turn
        self._export = export
        self._models = models if models is not None else EmbodyModels.resolve()
        self._system_prompt = system_prompt
        self._base_url = base_url
        self._api_key = api_key
        self._temperature = float(temperature)
        self._max_tokens = max_tokens
        self._idle_timeout_s = max(0.1, float(idle_timeout_s))
        self._enable_thinking = bool(enable_thinking)
        self._max_tool_rounds = max(1, int(max_tool_rounds))
        self._voice_tools = voice_tools if voice_tools is not None else DEFAULT_VOICE_TOOLS
        self._on_content = on_content
        self._on_reasoning = on_reasoning
        self._cancel = cancel if cancel is not None else _never
        self._sleep = sleep if sleep is not None else time.sleep
        self._turn_interval = float(turn_interval)

        self._pending: deque[Input] = deque(maxlen=None)
        self._max_pending = max(1, int(max_pending))
        self._spoken: deque[str] = deque(maxlen=max(0, int(spoken_maxlen)))
        self._history: deque[tuple[str, str]] = deque(maxlen=max(0, int(history_maxlen)))
        self._last_text = ""
        # One turn at a time; ``ask`` is deliberately outside it.
        self._turn_lock = threading.Lock()

        self.turns = 0
        self.rounds = 0
        self.tool_calls = 0
        self.refusals = 0
        self.stream_timeouts = 0
        self.stream_failures = 0
        self.dropped_inputs = 0

    # ------------------------------------------------------------------ #
    # Intake — O(1), safe from any thread, never raises                  #
    # ------------------------------------------------------------------ #

    def submit_cue(self, text: str) -> bool:
        """Offer one runtime perception cue. Returns whether it was accepted."""
        return self._offer(KIND_CUE, text)

    def submit_cues(self, texts: Iterable[str]) -> int:
        """Offer several cues (what :func:`reachy.embody.cues.cues_for_line` returns)."""
        return sum(1 for text in texts if self.submit_cue(text))

    def submit_utterance(self, text: str) -> bool:
        """Offer one heard utterance. **Ungated** — the layer hears everyone (c4)."""
        return self._offer(KIND_UTTERANCE, text)

    def note_spoken(self, text: str) -> None:
        """Record something the layer's MOUTH already said. Does NOT trigger a turn.

        The duplex session answers speech on its own, server-side. Without this
        the thinking mind would have no idea it had already replied and would
        cheerfully call ``speak`` to say it again. It is context, not a trigger —
        a robot that treats its own voice as a perception talks to itself.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return
        self._spoken.append(cleaned)
        if self._export is not None:
            self._export.emit(MessageEvent(text=cleaned, ts=self._export.time_fn()))

    @property
    def pending(self) -> int:
        """How many triggers are waiting for the next turn."""
        return len(self._pending)

    @property
    def last_text(self) -> str:
        """The final assistant text of the last turn that ran (``""`` if it failed)."""
        return self._last_text

    def _offer(self, kind: str, text: str) -> bool:
        cleaned = (text or "").strip()
        if not cleaned:
            self._drop(REASON_EMPTY_INPUT, kind)
            return False
        if len(self._pending) >= self._max_pending:
            self.dropped_inputs += 1
            self._drop(REASON_INPUT_QUEUE_FULL, f"{kind} dropped, {len(self._pending)} pending")
            return False
        self._pending.append(Input(kind=kind, text=cleaned))
        return True

    # ------------------------------------------------------------------ #
    # One turn                                                           #
    # ------------------------------------------------------------------ #

    def run_turn(self) -> bool:
        """Run one turn over everything pending. ``False`` when there was nothing.

        The pending inputs are DRAINED into the turn, so a failure consumes them
        rather than retrying forever against a sick gateway — but the failure is
        always named, on the journal and on the export feed, and the perception
        still enters the rolling history so the next turn knows it happened.

        Exactly ONE turn runs at a time (cited from
        :meth:`reachy.speech.agent_turn.AgentTurnEngine.run_turn`): a second
        concurrent call blocks here rather than interleaving two conversations
        into one history. :meth:`ask` is deliberately NOT behind this lock — a
        perception question must not have to wait out a long turn.
        """
        with self._turn_lock:
            inputs = self._drain()
            if not inputs:
                return False
            self._run_turn(inputs)
            return True

    def _run_turn(self, inputs: list[Input]) -> None:
        event = uuid.uuid4().hex[:8]
        senselog.stage(STAGE, SOURCE, event, f"turn inputs={len(inputs)}")
        self.turns += 1
        before_refusals, before_rounds = self.refusals, self.rounds
        cues = [item.render() for item in inputs]
        user_content = self._build_user_content(cues, self._drain_spoken())
        conversation = self._build_messages(user_content)
        raw: list[str] = []

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

    def _build_user_content(self, cues: list[str], spoken: list[str]) -> str:
        lines = ["I just perceived:"]
        lines.extend(f"- {cue}" for cue in cues)
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

    def _drain(self) -> list[Input]:
        items: list[Input] = []
        while self._pending:
            items.append(self._pending.popleft())
        return items

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
