"""Agent tool registry — OpenAI function-tool definitions + a dispatcher.

This is the tool layer a future *agent-cognition* engine consumes.  Where the
``think`` engine today parses expression + speech out of the ``*emoji*`` /
``"speech"`` marker convention (:mod:`reachy.speech.markers`), an agent instead
*calls tools*: the LLM emits structured ``tool_calls`` and this registry both
**publishes** their JSON-schema definitions (the OpenAI ``tools=`` array shape)
and **executes** them, returning an OpenAI tool-result message the engine can
append to the conversation.

Three tools ship in v1:

* ``speak``      — text → Reachy's spoken (TTS) voice.
* ``harmonics``  — text → Reachy's harmonic melodic voice (chirp/sing).
* ``apply_pose`` — a catalog emoji → one calm body expression on the serial
  :class:`~reachy.motion.queue.MotionQueue` (the SAME action the ``*emoji*``
  marker path enqueues).

Design
------
* **Every side effect is an injected seam.**  The registry never opens a media
  session, never hits the network, and never touches a robot on its own.  The
  synthesize + sample rate for ``speak`` / ``harmonics`` arrive as
  :class:`~reachy.speech.voice.VoiceEngine` objects (default:
  :func:`~reachy.speech.voice.resolve_voice_engine`), playback arrives as a
  ``play`` callable (default: :func:`reachy.speech.playback.play_audio`), and
  the pose seam arrives as an ``express`` callable (an
  :meth:`~reachy.motion.expression.ExpressionProducer.express`).  So a unit test
  needs no robot, no network, and no audio device.
* **Speak and harmonics are two tools, not one exclusive engine.**  Where
  :func:`~reachy.speech.voice.resolve_voice_engine` picks *one* engine per
  process, the agent sees both as callable tools side by side — one turn can
  invoke both, each synthesizing at its own sample rate (TTS 24 kHz, resampled
  to the device rate downstream by playback; harmonic 16 kHz).
* **Extensible by one definition + handler.**  A new capability is a single
  :class:`Tool` (an OpenAI definition + a handler); register it at construction
  (``extra_tools=``) or afterward (:meth:`ToolRegistry.register`).
* **Handlers degrade cleanly.**  :meth:`ToolRegistry.dispatch` never raises out:
  an unknown tool name, malformed arguments, or a handler exception all become
  an *error* tool-result message so the engine's tool loop can never die on a
  bad tool call.

Import boundary
---------------
This module intentionally imports neither :mod:`reachy.speech.llm` nor
:mod:`reachy.speech.events` — it is a peer of :mod:`reachy.speech.voice`, not of
:mod:`reachy.speech.cognition`.  The tool *definitions* are produced here; the
*decision* to call them (the LLM tool loop) lives in the cognition/agent engine.
The pose seam is injected as a plain callable, so this module does not even
import :mod:`reachy.motion`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from reachy.speech.playback import play_audio
from reachy.speech.voice import VoiceEngine, resolve_voice_engine

log = logging.getLogger(__name__)

# Type aliases for the two injected callable seams.
#: A handler receives the parsed arguments dict and returns the tool-result content string.
Handler = Callable[[dict], str]
#: A playback seam: ``play(pcm_bytes, *, samplerate=...)`` (see ``playback.play_audio``).
PlaySeam = Callable[..., None]
#: An expression seam: ``express(emoji)`` (see ``ExpressionProducer.express``).
ExpressSeam = Callable[[str], object]

# Canonical v1 tool names.
SPEAK = "speak"
HARMONICS = "harmonics"
APPLY_POSE = "apply_pose"


# ---------------------------------------------------------------------------
# Tool value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """One agent tool: its OpenAI *definition* + the *handler* that executes it.

    ``definition`` is the OpenAI tools-array entry —
    ``{"type": "function", "function": {"name", "description", "parameters"}}``
    — ready to drop into a ``tools=`` payload.  ``handler`` takes the parsed
    arguments dict and returns the tool-result *content* string.
    """

    definition: dict
    handler: Handler

    @property
    def name(self) -> str:
        """The tool's function name (its dispatch key)."""
        return self.definition["function"]["name"]


def function_tool(
    *,
    name: str,
    description: str,
    parameters: dict,
    handler: Handler,
) -> Tool:
    """Build a :class:`Tool` from its pieces (the OpenAI function-tool shape).

    Adding a capability is exactly this: one call giving a name, a
    human-readable description, a JSON-schema ``parameters`` object, and a
    handler — then hand the result to :class:`ToolRegistry` via ``extra_tools``
    or :meth:`ToolRegistry.register`.
    """
    return Tool(
        definition={
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        },
        handler=handler,
    )


# ---------------------------------------------------------------------------
# Built-in handler factories
# ---------------------------------------------------------------------------


def _require_text(arguments: dict) -> str:
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a non-empty 'text' string is required")
    return text


def _make_voice_handler(engine: VoiceEngine, play: PlaySeam) -> Handler:
    """A speak/harmonics handler: synthesize *text* via *engine*, then play it.

    The clip is played through the injected ``play`` seam at the engine's own
    sample rate (:attr:`VoiceEngine.samplerate`) — so ``speak`` (24 kHz) and
    ``harmonics`` (16 kHz) each render correctly through the same playback path.
    """

    def handler(arguments: dict) -> str:
        text = _require_text(arguments)
        pcm = engine.synthesize(text)
        play(pcm, samplerate=engine.samplerate)
        return json.dumps(
            {"status": "ok", "engine": engine.name, "chars": len(text), "bytes": len(pcm)}
        )

    return handler


def _make_pose_handler(express: ExpressSeam | None) -> Handler:
    """An apply_pose handler: enqueue one calm expression for a catalog emoji.

    ``express`` is the injected :meth:`ExpressionProducer.express` seam, so the
    action enqueued is byte-for-byte the one the ``*emoji*`` marker path
    produces.  When no express seam was injected the tool degrades cleanly (a
    handler-level error, caught by :meth:`ToolRegistry.dispatch`)."""

    def handler(arguments: dict) -> str:
        if express is None:
            raise RuntimeError(
                "apply_pose is unavailable: no expression seam was injected into the registry"
            )
        emoji = arguments.get("emoji")
        if not isinstance(emoji, str) or not emoji:
            raise ValueError("a non-empty 'emoji' string is required")
        express(emoji)
        return json.dumps({"status": "ok", "emoji": emoji})

    return handler


# ---------------------------------------------------------------------------
# Built-in tool definitions
# ---------------------------------------------------------------------------


def _speak_tool(engine: VoiceEngine, play: PlaySeam) -> Tool:
    return function_tool(
        name=SPEAK,
        description="Speak text aloud in Reachy's spoken (TTS) voice.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The words to speak aloud."}},
            "required": ["text"],
        },
        handler=_make_voice_handler(engine, play),
    )


def _harmonics_tool(engine: VoiceEngine, play: PlaySeam) -> Tool:
    return function_tool(
        name=HARMONICS,
        description=(
            "Render text as a short melodic phrase in Reachy's harmonic voice "
            "(chirp/sing) — an expressive, non-speech vocalization."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to render as a melody."}
            },
            "required": ["text"],
        },
        handler=_make_voice_handler(engine, play),
    )


def _apply_pose_tool(express: ExpressSeam | None) -> Tool:
    return function_tool(
        name=APPLY_POSE,
        description=(
            "Apply a body expression by catalog emoji (e.g. 🤔, 😮, 🎉). Enqueues "
            "one calm one-shot pose move; an unknown emoji falls back to neutral."
        ),
        parameters={
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "A catalog emoji key (unknown emoji fall back to neutral).",
                }
            },
            "required": ["emoji"],
        },
        handler=_make_pose_handler(express),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Publishes agent tool definitions and dispatches tool calls to handlers.

    Parameters
    ----------
    express:
        The pose seam — an :meth:`ExpressionProducer.express` callable that
        enqueues a catalog-emoji expression onto the serial motion queue.  When
        ``None`` (the default), ``apply_pose`` is still *listed* but degrades to
        a clean error if actually called.
    speak_engine / harmonic_engine:
        The :class:`~reachy.speech.voice.VoiceEngine` (synthesize + sample rate)
        backing ``speak`` / ``harmonics``.  Default to
        :func:`~reachy.speech.voice.resolve_voice_engine` for ``"tts"`` /
        ``"harmonic"`` — the same resolver ``say`` / ``think`` use.
    play:
        The playback seam — ``play(pcm, *, samplerate=...)``.  Default:
        :func:`reachy.speech.playback.play_audio`.
    extra_tools:
        Additional :class:`Tool` objects to register at construction — proving a
        new capability needs only one definition + handler.
    """

    def __init__(
        self,
        *,
        express: ExpressSeam | None = None,
        speak_engine: VoiceEngine | None = None,
        harmonic_engine: VoiceEngine | None = None,
        play: PlaySeam | None = None,
        extra_tools: Iterable[Tool] = (),
    ) -> None:
        speak_engine = speak_engine or resolve_voice_engine("tts")
        harmonic_engine = harmonic_engine or resolve_voice_engine("harmonic")
        play = play or play_audio

        # Insertion order defines the published tools-array order.
        self._tools: dict[str, Tool] = {}
        for tool in (
            _speak_tool(speak_engine, play),
            _harmonics_tool(harmonic_engine, play),
            _apply_pose_tool(express),
        ):
            self._tools[tool.name] = tool
        for tool in extra_tools:
            self.register(tool)

    # ------------------------------------------------------------------
    # Registration / publication
    # ------------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        """Register (or replace) *tool* by its name.

        Registering a name that already exists replaces it — so a caller can
        override a built-in (e.g. swap the ``speak`` handler) as easily as add a
        new capability.
        """
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        """The registered tool names, in publication order."""
        return list(self._tools)

    def tools(self) -> list[dict]:
        """The OpenAI ``tools=`` array — one definition per registered tool.

        The returned list is ready to pass straight as the ``tools=`` payload to
        the LLM client.
        """
        return [tool.definition for tool in self._tools.values()]

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(
        self,
        name: str,
        arguments_json: str | Mapping | None,
        tool_call_id: str | None = None,
    ) -> dict:
        """Execute the tool *name* and return an OpenAI tool-result message dict.

        The returned dict is ``{"role": "tool", "tool_call_id": <id>, "content":
        <str>}`` — ready to append to the conversation for the next LLM turn.
        *arguments_json* is the OpenAI ``function.arguments`` payload: normally a
        JSON *string*, but an already-parsed mapping is tolerated.

        This method **never raises out**.  An unknown tool name, malformed
        arguments, or a handler exception each become an *error* tool-result
        (``content`` is ``{"error": "..."}``), so the engine's tool loop keeps
        running on a bad tool call.
        """
        tool = self._tools.get(name)
        if tool is None:
            return self._error(tool_call_id, f"unknown tool: {name!r}")

        try:
            arguments = _parse_arguments(arguments_json)
        except (ValueError, TypeError) as exc:
            return self._error(tool_call_id, f"malformed arguments for {name!r}: {exc}")

        try:
            content = tool.handler(arguments)
        except Exception as exc:  # noqa: BLE001 — a bad tool call must never kill the loop
            log.warning("[tools] handler for %r raised: %s", name, exc)
            return self._error(tool_call_id, f"{name!r} failed: {exc}")

        return _tool_message(tool_call_id, content)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _error(tool_call_id: str | None, message: str) -> dict:
        return _tool_message(tool_call_id, json.dumps({"error": message}))


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _parse_arguments(arguments_json: str | Mapping | None) -> dict:
    """Coerce the OpenAI ``function.arguments`` payload to a plain dict.

    Accepts a JSON string (the normal wire shape), an already-parsed mapping, or
    ``None`` (→ ``{}``).  Raises ``ValueError`` / ``TypeError`` on anything that
    is neither — :meth:`ToolRegistry.dispatch` turns that into an error result.
    """
    if arguments_json is None or arguments_json == "":
        return {}
    if isinstance(arguments_json, Mapping):
        return dict(arguments_json)
    parsed = json.loads(arguments_json)
    if not isinstance(parsed, dict):
        raise ValueError("arguments must decode to a JSON object")
    return parsed


def _tool_message(tool_call_id: str | None, content: str) -> dict:
    """Build the OpenAI tool-result message dict."""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
