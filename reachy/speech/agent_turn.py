"""The agent turn engine: sense events → a tool-use turn → structured actions.

:class:`AgentTurnEngine` is the tool-use counterpart of
:class:`~reachy.speech.cognition.CognitionEngine`. Where the marker engine parses
expression + speech out of the ``*emoji*`` / ``"speech"`` convention, the agent
engine *acts through tools*: an LLM turn emits structured ``tool_calls`` that this
engine executes through an injected :class:`~reachy.speech.tools.ToolRegistry`
(``speak`` / ``harmonics`` / ``apply_pose`` in v1). It consumes the *same*
:class:`~reachy.speech.events.EventBuffer` and feeds the *same*
``thinking`` / ``message`` / ``emotion`` export sinks, so a later wiring task can
swap it in behind ``listen``'s folded :class:`~reachy.motion.listen_think.ThinkHook`
seam without touching the loop.

Serialized agent turns
----------------------
Exactly **one** turn runs at a time: :meth:`run_turn` holds a
:class:`threading.Lock` for the whole turn, so a second concurrent call blocks until
the first completes. A turn *snapshots* the event buffer at its very start — before
any LLM work — so cues that arrive *during* a turn accumulate for the **next** turn.
This mirrors :class:`CognitionEngine`'s serialized-cognition guarantee.

The tool loop
-------------
One :meth:`run_turn` runs the full OpenAI tool loop:

1. Snapshot the buffer → build the messages (system prompt + a bounded rolling
   history of prior turns + the current perception as a user message).
2. Call the LLM turn function (``turn_fn``, default
   :func:`reachy.speech.llm.stream_turn`) with the registry's published ``tools=``.
3. For each returned :class:`~reachy.speech.llm.ToolCall`: emit its export block (a
   ``MessageEvent`` for a speak/harmonics utterance, an ``EmotionEvent`` for an
   apply_pose), then dispatch it through the registry and append the tool-result
   message to the conversation.
4. Loop until a turn returns **no** tool calls (its ``content`` is the final
   assistant text) — bounded by ``max_tool_rounds`` (default 6) so a model that
   never stops calling tools can never spin forever.
5. Emit exactly one ``ThinkingEvent`` for the whole turn — the sense cues plus the
   raw turn text (each round's content and a representation of the tool calls it
   made).

Export blocks match ``docs/export-schema.md`` exactly and reuse the export
:class:`~reachy.export.exporter.ExportHook` contract from :mod:`reachy.speech.cognition`
(``emit`` sink + ``pose_resolver`` + ``time_fn``), so the two feeds cannot drift.

Audio is an optional sink (the registry-dispatch seam)
------------------------------------------------------
The registry *executes speech itself* — its ``speak`` / ``harmonics`` handlers
synthesize and play — and :meth:`ToolRegistry.dispatch` never raises: a synth /
playback failure comes back as an **error tool-result** (``content`` is
``{"error": …}``). The engine observes that result to carry over
:class:`CognitionEngine`'s ``audio_optional`` degradation without touching the
registry:

* ``audio_optional=False`` (default): an error result from an audio tool aborts the
  turn with a :class:`~reachy.cli._errors.CliError` (exit-2) — the strict fail-fast
  contract of standalone ``think`` / ``say``.
* ``audio_optional=True`` (the folded ``listen --live`` path): the failure is
  absorbed — logged once, the utterance still exported as a ``MessageEvent`` — and
  after ``audio_mute_threshold`` consecutive failures the audio sink **latches
  off**: subsequent speak/harmonics calls skip dispatch entirely (a synthetic
  "muted" tool-result keeps the loop moving) so a hard-down TTS never throttles the
  agent. Non-audio tools (apply_pose) and every export block keep flowing.

This is the same ``_note_audio_failure`` streak/latch pattern as
:class:`CognitionEngine`; the only difference is *where* the failure surfaces (a
dispatch result here vs. a re-raised worker exception there).

Determinism
-----------
Every collaborator is injectable — the turn function, the registry, the export hook,
``sleep`` (inter-turn pacing in :meth:`run`) — and there is no wall-clock or
randomness in the logic (timestamps come from the export hook's ``time_fn``). So the
whole engine is exercised by fakes with no live LLM, no robot, and no real threads.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import deque
from collections.abc import Callable
from typing import Protocol

from reachy import senselog
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.export.events import EmotionEvent, MessageEvent, ThinkingEvent
from reachy.export.exporter import ExportHook
from reachy.speech import llm as _llm
from reachy.speech.events import SenseCue
from reachy.speech.tools import APPLY_POSE, HARMONICS, SPEAK, ToolRegistry

logger = logging.getLogger(__name__)

# Default system prompt — a module constant (like cognition.py's) so ops can tune
# the agent's guidance without a code change. First-person, tool-only, quiet by
# default.
DEFAULT_AGENT_SYSTEM_PROMPT = (
    "You are Reachy Mini, a small expressive desk robot. You continuously perceive "
    "events through your microphone, camera, and touch — sounds, motion, words "
    "people say near you, and the feel of being petted or patted. You act ONLY "
    "through the tools you are given: speak (your spoken voice), harmonics (a short "
    "melodic chirp), and apply_pose (a body expression by emoji). Decide what — if "
    "anything — is worth doing, then call the matching tool(s). Speak or express "
    "only when something genuinely warrants it; when nothing does, do nothing and "
    "call no tools. Keep any speech to one or two short, natural first-person "
    "sentences. Do not narrate raw sensor readings."
)

# Minimum gap between turns in the run() loop (seconds).
DEFAULT_TURN_INTERVAL = 1.0

# How many LLM rounds one turn may take before the tool loop is force-stopped. A
# turn normally ends when the model returns no tool calls; this bound guarantees
# termination if it never does.
DEFAULT_MAX_TOOL_ROUNDS = 6

# Consecutive audio-dispatch failures (in audio_optional mode) before the engine
# latches the audio sink off. A small streak (not 1) tolerates a single transient
# blip without muting the session — mirrors cognition.DEFAULT_AUDIO_MUTE_THRESHOLD.
DEFAULT_AUDIO_MUTE_THRESHOLD = 2

# Rolling history depth — how many prior turns' (perception, reply) pairs are kept
# for context. Mirrors the 6-entry discipline used elsewhere in the repo (the
# engagement classifier's accepted-turn window).
DEFAULT_HISTORY_MAXLEN = 6


# ---------------------------------------------------------------------------
# Collaborator protocols (documentation; the engine accepts any matching object)
# ---------------------------------------------------------------------------


class _TurnFn(Protocol):
    def __call__(self, messages: list[dict], **kwargs) -> _llm.TurnResult: ...


class _RegistryLike(Protocol):
    def tools(self) -> list[dict]: ...

    def dispatch(self, name: str, arguments_json, tool_call_id=None) -> dict: ...


class _BufferLike(Protocol):
    def snapshot(self) -> list[SenseCue]: ...


# ---------------------------------------------------------------------------
# Prompt / message construction
# ---------------------------------------------------------------------------


def build_user_message(cues: list[SenseCue]) -> str:
    """Render a turn's sense cues into the user-message content string.

    Oldest first, one bullet per cue — identical in shape to
    :func:`reachy.speech.cognition.build_messages`'s user content, so the two
    engines present perceptions to the model the same way.
    """
    lines = [f"- {cue.text}" for cue in cues]
    return "I just perceived:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AgentTurnEngine:
    """Runs serialized tool-use turns over the shared sense buffer + export sinks.

    Parameters
    ----------
    buffer:
        The sense-event source. Its :meth:`~EventBuffer.snapshot` is the engine's
        only input. Exposed as the public :attr:`buffer` attribute so
        :class:`~reachy.motion.listen_think.ThinkHook` (which reads ``.buffer`` via
        ``getattr``) can drive this engine unchanged.
    registry:
        The :class:`~reachy.speech.tools.ToolRegistry` whose :meth:`tools` is
        published to the model and whose :meth:`dispatch` executes each tool call.
        Defaults to a fresh :class:`ToolRegistry` (the real speak/harmonics/pose
        tools). Tests inject a fake.
    turn_fn:
        The LLM turn function ``(messages, *, tools=…, **kw) -> TurnResult``.
        Defaults to :func:`reachy.speech.llm.stream_turn`.
    export:
        Optional :class:`~reachy.export.exporter.ExportHook` — the same contract the
        marker engine uses. When given, each turn emits ``emotion`` / ``message``
        blocks in tool-call order and one ``thinking`` block at turn end. ``None``
        (default) means no export path is entered.
    system_prompt:
        The system message prepended to every turn. Defaults to
        :data:`DEFAULT_AGENT_SYSTEM_PROMPT`.
    llm_kwargs:
        Optional keyword dict forwarded to ``turn_fn`` on every call (e.g.
        ``base_url``, ``model``, ``temperature``).
    sleep / turn_interval:
        Inter-turn pacing for :meth:`run` — an injectable ``(seconds) -> None`` and
        the minimum gap between turns.
    audio_optional:
        When ``True`` an audio-tool dispatch failure degrades to "no speech" and
        latches off after a streak, instead of aborting the turn. See the module
        docstring's audio-sink note. Default ``False`` (strict fail-fast).
    max_tool_rounds:
        The tool-loop bound (default :data:`DEFAULT_MAX_TOOL_ROUNDS`).
    history_maxlen:
        Rolling-history depth for multi-turn coherence (default
        :data:`DEFAULT_HISTORY_MAXLEN`).
    audio_tools / pose_tool:
        The tool names treated as spoken utterances / as a body expression, for the
        export mapping and the audio-optional seam. Default to the v1 registry names.
    """

    def __init__(
        self,
        *,
        buffer: _BufferLike,
        registry: _RegistryLike | None = None,
        turn_fn: _TurnFn | None = None,
        export: ExportHook | None = None,
        system_prompt: str = DEFAULT_AGENT_SYSTEM_PROMPT,
        llm_kwargs: dict | None = None,
        sleep: Callable[[float], None] | None = None,
        turn_interval: float = DEFAULT_TURN_INTERVAL,
        audio_optional: bool = False,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        history_maxlen: int = DEFAULT_HISTORY_MAXLEN,
        audio_tools: frozenset[str] | None = None,
        pose_tool: str = APPLY_POSE,
    ) -> None:
        # Public: ThinkHook reads engine.buffer via getattr — keep it public so this
        # engine is drop-in behind the folded seam.
        self.buffer = buffer
        self._registry = registry if registry is not None else ToolRegistry()
        self._turn_fn = turn_fn if turn_fn is not None else _llm.stream_turn
        self._export = export
        self._system_prompt = system_prompt
        self._llm_kwargs = dict(llm_kwargs or {})
        import time

        self._sleep = sleep if sleep is not None else time.sleep
        self._turn_interval = turn_interval
        self._max_tool_rounds = max(1, int(max_tool_rounds))

        # Audio-optional latch state (mirrors CognitionEngine).
        self._audio_optional = audio_optional
        self._audio_mute_threshold = DEFAULT_AUDIO_MUTE_THRESHOLD
        self._audio_muted = False
        self._audio_fail_streak = 0
        self._audio_tools = (
            audio_tools if audio_tools is not None else frozenset({SPEAK, HARMONICS})
        )
        self._pose_tool = pose_tool

        # Bounded rolling history of prior turns' (user perception, final reply).
        self._history: deque[tuple[str, str]] = deque(maxlen=max(0, int(history_maxlen)))

        # One turn at a time: a concurrent run_turn() blocks here until release.
        import threading

        self._turn_lock = threading.Lock()

    # ------------------------------------------------------------------
    # One serialized turn
    # ------------------------------------------------------------------

    def run_turn(self) -> bool:
        """Execute exactly one serialized agent turn.

        Snapshots the buffer (atomically clearing it); with no cues it is a no-op
        (no LLM call, no dispatch) and returns ``False``. Otherwise it runs the full
        tool loop and returns ``True``.

        Raises
        ------
        reachy.cli._errors.CliError
            Propagated from ``turn_fn`` (e.g. an unreachable endpoint). In strict
            mode (``audio_optional`` False) a failed audio-tool dispatch also raises.
        """
        with self._turn_lock:
            cues = self.buffer.snapshot()
            if not cues:
                return False
            self._run_agent_turn(cues)
            return True

    def _run_agent_turn(self, cues: list[SenseCue]) -> None:
        """Run the bounded LLM → tool-dispatch loop for one turn."""
        senselog.stage("turn", "agent", uuid.uuid4().hex[:8], f"cue_count={len(cues)}")
        user_content = build_user_message(cues)
        conversation = self._build_messages(user_content)
        raw_rounds: list[str] = []
        result: _llm.TurnResult | None = None

        for _round in range(self._max_tool_rounds):
            result = self._turn_fn(conversation, tools=self._registry.tools(), **self._llm_kwargs)
            raw_rounds.append(_render_round(result))
            if not result.tool_calls:
                break
            conversation.append(_assistant_tool_message(result))
            for call in result.tool_calls:
                self._process_tool_call(call, conversation)

        final_text = (result.content if result is not None else "") or ""

        if self._export is not None:
            self._export.emit(
                ThinkingEvent(
                    cues=[cue.text for cue in cues],
                    text="\n".join(part for part in raw_rounds if part),
                    ts=self._export.time_fn(),
                )
            )

        self._history.append((user_content, final_text))

    # ------------------------------------------------------------------
    # Message construction
    # ------------------------------------------------------------------

    def _build_messages(self, user_content: str) -> list[dict]:
        """System prompt + bounded rolling history + the current perception."""
        messages: list[dict] = [{"role": "system", "content": self._system_prompt}]
        for prior_user, prior_reply in self._history:
            messages.append({"role": "user", "content": prior_user})
            if prior_reply.strip():
                messages.append({"role": "assistant", "content": prior_reply})
        messages.append({"role": "user", "content": user_content})
        return messages

    # ------------------------------------------------------------------
    # Tool dispatch + export
    # ------------------------------------------------------------------

    def _process_tool_call(self, call: _llm.ToolCall, conversation: list[dict]) -> None:
        """Emit the call's export block (in order), then dispatch it.

        The export block is emitted first and independently of dispatch success, so
        a spoken utterance still reaches the export feed even when its audio sink is
        dead or muted — matching :class:`CognitionEngine`, where export runs on the
        producer thread ahead of the speak worker.
        """
        if self._export is not None:
            self._emit_tool_export(call)
        if call.name in self._audio_tools:
            self._dispatch_audio(call, conversation)
        else:
            conversation.append(self._registry.dispatch(call.name, call.arguments_json, call.id))

    def _emit_tool_export(self, call: _llm.ToolCall) -> None:
        """Export the ``message`` / ``emotion`` block for one tool call, if it maps."""
        hook = self._export  # never None: guarded by the caller
        if call.name in self._audio_tools:
            text = call.arguments.get("text")
            if isinstance(text, str) and text.strip():
                hook.emit(MessageEvent(text=text, ts=hook.time_fn()))
        elif call.name == self._pose_tool:
            emoji = call.arguments.get("emoji")
            if isinstance(emoji, str) and emoji:
                pose = hook.pose_resolver(emoji) if hook.pose_resolver is not None else None
                hook.emit(EmotionEvent(emoji=emoji, pose=pose, ts=hook.time_fn()))

    def _dispatch_audio(self, call: _llm.ToolCall, conversation: list[dict]) -> None:
        """Dispatch a speak/harmonics call under the audio-optional latch.

        When the audio sink has latched off, the actual dispatch (synth + playback)
        is skipped and a synthetic "muted" tool-result keeps the loop moving. When
        active, a dispatch that comes back as an error result is a strict-mode abort
        or, under ``audio_optional``, a counted failure that may latch the sink off.
        """
        if self._audio_muted:
            conversation.append(_muted_tool_message(call.id))
            return

        message = self._registry.dispatch(call.name, call.arguments_json, call.id)
        if _is_error_result(message):
            if not self._audio_optional:
                raise CliError(
                    code=EXIT_ENV_ERROR,
                    message=f"speech tool {call.name!r} failed: {_error_text(message)}",
                    remediation=(
                        "start the TTS/harmonic voice endpoint, or run with the "
                        "audio-optional live path so speech degrades gracefully"
                    ),
                )
            self._note_audio_failure(error_text=_error_text(message))
        else:
            self._audio_fail_streak = 0
        conversation.append(message)

    def _note_audio_failure(self, *, error_text: str = "") -> None:
        """Record one audio-dispatch failure (log once, latch off after the streak).

        Mirrors :meth:`CognitionEngine._note_audio_failure`: logs on the first
        failure of a streak, and once :attr:`_audio_mute_threshold` consecutive
        failures accumulate, latches the audio sink off so no further synth is
        attempted — the agent keeps thinking + acting on non-audio tools at full
        speed.
        """
        self._audio_fail_streak += 1
        if self._audio_fail_streak == 1:
            if error_text:
                logger.warning(
                    "agent audio sink failed (%s); continuing without speech (audio is optional)",
                    error_text,
                )
            else:
                logger.warning(
                    "agent audio sink failed; continuing without speech (audio is optional)",
                )
            senselog.drop("action", "speech", uuid.uuid4().hex[:8], "audio-muted")
        if not self._audio_muted and self._audio_fail_streak >= self._audio_mute_threshold:
            self._audio_muted = True
            logger.warning(
                "agent audio muted after %d consecutive failures; tool actions + export "
                "sinks unaffected",
                self._audio_fail_streak,
            )
            senselog.drop("action", "speech", uuid.uuid4().hex[:8], "audio-muted")

    # ------------------------------------------------------------------
    # The thin loop
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        max_turns: int | None = None,
        stop: Callable[[], bool] | None = None,
        before_turn: Callable[[], None] | None = None,
    ) -> int:
        """Repeatedly run turns while cues exist, until stopped. Returns turns run.

        Signature and semantics mirror :meth:`CognitionEngine.run` so the folded
        :class:`~reachy.motion.listen_think.ThinkHook` — which calls
        ``engine.run(stop=…)`` on a worker thread — drives this engine unchanged.

        Parameters
        ----------
        max_turns:
            Stop after this many turns that *ran* (cues existed and a turn was
            produced). ``None`` runs until ``stop`` fires.
        stop:
            Optional zero-arg predicate; the loop exits when it returns truthy
            (checked before each turn).
        before_turn:
            Optional zero-arg hook called at the top of each iteration — how a caller
            pumps fresh perceptions into the buffer.
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
                # No producer hook and nothing left to consume: an empty buffer will
                # never refill on its own, so stop rather than busy-loop to max_turns.
                break
        return ran


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _render_round(result: _llm.TurnResult) -> str:
    """Render one LLM round to raw text: its content + a repr of each tool call.

    The result becomes part of the turn's :class:`ThinkingEvent.text` — the raw
    turn text "including tool-call representations". A tool call renders as
    ``name(arguments_json)`` so the exported thought shows *what the agent did*.
    """
    parts: list[str] = []
    if result.content:
        parts.append(result.content)
    for call in result.tool_calls:
        parts.append(f"{call.name}({call.arguments_json})")
    return " ".join(parts)


def _assistant_tool_message(result: _llm.TurnResult) -> dict:
    """Build the OpenAI assistant message carrying this round's tool calls.

    Appended to the conversation before the tool-result messages so the next LLM
    round sees its own calls paired with their results (the OpenAI tool protocol).
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


def _muted_tool_message(tool_call_id: str | None) -> dict:
    """A synthetic tool-result standing in for a skipped (muted) audio call."""
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps({"status": "muted", "note": "audio sink disabled"}),
    }


#: Fallback error text when an error tool-result carries no readable message.
_UNKNOWN_ERROR = "unknown error"


def _is_error_result(message: dict) -> bool:
    """Whether a dispatch tool-result carries an ``{"error": …}`` content payload."""
    content = message.get("content")
    if not isinstance(content, str):
        return False
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):  # JSONDecodeError is a ValueError
        return False
    return isinstance(payload, dict) and "error" in payload


def _error_text(message: dict) -> str:
    """Extract the error string from an error tool-result (best-effort)."""
    try:
        payload = json.loads(message.get("content") or "")
    except (TypeError, ValueError):  # JSONDecodeError is a ValueError
        return _UNKNOWN_ERROR
    if isinstance(payload, dict):
        return str(payload.get("error", _UNKNOWN_ERROR))
    return _UNKNOWN_ERROR
