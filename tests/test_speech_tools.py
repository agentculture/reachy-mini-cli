"""Tests for the agent tool registry (:mod:`reachy.speech.tools`).

The registry is the tool layer a future agent-cognition engine consumes: it
publishes OpenAI ``tools=``-shaped function definitions and dispatches tool
calls to injected handler seams.  Everything is exercised through fakes — no
robot, no network, no audio device.

Acceptance criteria (task t3)
-----------------------------
1. ``speak`` / ``harmonics`` / ``apply_pose`` are defined with JSON-schema
   parameters (OpenAI tools-array shape) and a ``dispatch`` that returns an
   OpenAI tool-result message dict.
2. ``apply_pose`` with a catalog emoji enqueues the IDENTICAL motion action the
   ``*emoji*`` marker path (``ExpressionProducer.express``) produces.
3. One turn can invoke both ``speak`` and ``harmonics``; each synthesizes at its
   own sample rate (TTS 24 kHz, harmonic 16 kHz) through the injected play seam.
4. Adding a capability requires only one new tool definition + handler
   (a fake tool registered at construction is listed and dispatched).

Plus the degrade contract (unknown tool / malformed args / handler raise never
escape dispatch) and the import boundary (no ``reachy.speech.llm`` /
``reachy.speech.events`` in the module's import graph).
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess  # nosec B404 — fixed-arg subprocess for an import-boundary probe
import sys

import pytest

import reachy.speech.tools as tools_mod
from reachy.motion.expression import ExpressionProducer
from reachy.speech.harmonic import HARMONIC_SAMPLE_RATE
from reachy.speech.tools import ToolRegistry, function_tool
from reachy.speech.tts import DEFAULT_SAMPLE_RATE as TTS_SAMPLE_RATE
from reachy.speech.voice import VoiceEngine

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _PlayRecorder:
    """Record ``play(pcm, samplerate=...)`` calls in arrival order."""

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int]] = []

    def __call__(self, pcm: bytes, *, samplerate: int, **_kw) -> None:
        self.calls.append((pcm, samplerate))


class _RecordingQueue:
    """A minimal fake :class:`~reachy.motion.queue.MotionQueue` — records submits."""

    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, action) -> None:
        self.submitted.append(action)


def _fake_engine(name: str, samplerate: int, sink: list[str]) -> VoiceEngine:
    """A :class:`VoiceEngine` whose synthesize records its text and returns tagged PCM."""

    def _synth(text: str, **_kw) -> bytes:
        sink.append(text)
        return f"{name}-pcm:{text}".encode()

    return VoiceEngine(name=name, synthesize=_synth, samplerate=samplerate)


# ---------------------------------------------------------------------------
# AC-1: tools-array shape + dispatch contract
# ---------------------------------------------------------------------------


def test_tools_array_has_the_three_v1_tools_in_openai_shape() -> None:
    reg = ToolRegistry()
    defs = reg.tools()
    assert isinstance(defs, list)
    names = [d["function"]["name"] for d in defs]
    assert names == ["speak", "harmonics", "apply_pose"]
    for d in defs:
        assert d["type"] == "function"
        fn = d["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert isinstance(params["required"], list)


def test_speak_and_apply_pose_declare_expected_json_schema_params() -> None:
    reg = ToolRegistry()
    by_name = {d["function"]["name"]: d["function"]["parameters"] for d in reg.tools()}
    assert by_name["speak"]["properties"]["text"]["type"] == "string"
    assert by_name["speak"]["required"] == ["text"]
    assert by_name["harmonics"]["properties"]["text"]["type"] == "string"
    assert by_name["apply_pose"]["properties"]["emoji"]["type"] == "string"
    assert by_name["apply_pose"]["required"] == ["emoji"]


def test_dispatch_returns_openai_tool_result_message() -> None:
    synths: list[str] = []
    play = _PlayRecorder()
    reg = ToolRegistry(
        speak_engine=_fake_engine("tts", TTS_SAMPLE_RATE, synths),
        play=play,
    )
    result = reg.dispatch("speak", json.dumps({"text": "hello"}), tool_call_id="call_1")
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call_1"
    assert isinstance(result["content"], str)
    assert synths == ["hello"]


# ---------------------------------------------------------------------------
# AC-3: one turn drives both speak and harmonics at their own sample rates
# ---------------------------------------------------------------------------


def test_speak_synthesizes_and_plays_at_tts_rate() -> None:
    synths: list[str] = []
    play = _PlayRecorder()
    reg = ToolRegistry(
        speak_engine=_fake_engine("tts", TTS_SAMPLE_RATE, synths),
        play=play,
    )
    reg.dispatch("speak", json.dumps({"text": "spoken"}), tool_call_id="s")
    assert synths == ["spoken"]
    assert play.calls == [(b"tts-pcm:spoken", TTS_SAMPLE_RATE)]
    assert TTS_SAMPLE_RATE == 24000


def test_harmonics_synthesizes_and_plays_at_harmonic_rate() -> None:
    synths: list[str] = []
    play = _PlayRecorder()
    reg = ToolRegistry(
        harmonic_engine=_fake_engine("harmonic", HARMONIC_SAMPLE_RATE, synths),
        play=play,
    )
    reg.dispatch("harmonics", json.dumps({"text": "la la"}), tool_call_id="h")
    assert synths == ["la la"]
    assert play.calls == [(b"harmonic-pcm:la la", HARMONIC_SAMPLE_RATE)]
    assert HARMONIC_SAMPLE_RATE == 16000


def test_one_turn_invokes_both_speak_and_harmonics_each_at_own_rate() -> None:
    tts_synths: list[str] = []
    harm_synths: list[str] = []
    play = _PlayRecorder()
    reg = ToolRegistry(
        speak_engine=_fake_engine("tts", TTS_SAMPLE_RATE, tts_synths),
        harmonic_engine=_fake_engine("harmonic", HARMONIC_SAMPLE_RATE, harm_synths),
        play=play,
    )
    reg.dispatch("speak", json.dumps({"text": "words"}), tool_call_id="a")
    reg.dispatch("harmonics", json.dumps({"text": "melody"}), tool_call_id="b")
    assert tts_synths == ["words"]
    assert harm_synths == ["melody"]
    # Each played through the SAME injected play seam, each at its own rate.
    assert play.calls == [
        (b"tts-pcm:words", 24000),
        (b"harmonic-pcm:melody", 16000),
    ]


# ---------------------------------------------------------------------------
# AC-2: apply_pose enqueues the IDENTICAL action the *emoji* marker path does
# ---------------------------------------------------------------------------


def test_apply_pose_enqueues_identical_action_as_marker_path() -> None:
    """The reference: ``ExpressionProducer.express`` is what the ``*emoji*`` marker
    path calls (see ``ExpressionProducer.on_marker`` / ``consume``).  Wire the
    registry's express seam to the SAME producer and assert the apply_pose call
    enqueues a MotionAction equal to the one ``express`` enqueued directly."""
    queue = _RecordingQueue()
    producer = ExpressionProducer(queue=queue)

    # Reference: the marker path.
    ref_action = producer.express("🤔")

    # The tool path, over the same producer/express seam.
    reg = ToolRegistry(express=producer.express)
    result = reg.dispatch("apply_pose", json.dumps({"emoji": "🤔"}), tool_call_id="p")

    assert result["role"] == "tool"
    assert len(queue.submitted) == 2
    # The apply_pose action is IDENTICAL (value-equal) to the marker-path action.
    assert queue.submitted[1] == queue.submitted[0]
    assert queue.submitted[1] == ref_action


def test_apply_pose_unknown_emoji_still_enqueues_neutral_fallback() -> None:
    queue = _RecordingQueue()
    producer = ExpressionProducer(queue=queue)
    ref_neutral = producer.express("✨")  # sparkles: absent from the starter catalog

    reg = ToolRegistry(express=producer.express)
    reg.dispatch("apply_pose", json.dumps({"emoji": "✨"}), tool_call_id="n")

    assert queue.submitted[1] == ref_neutral


# ---------------------------------------------------------------------------
# AC-4: adding a capability = one tool definition + handler (construction-time)
# ---------------------------------------------------------------------------


def test_extra_tool_registered_at_construction_is_listed_and_dispatched() -> None:
    seen: list[dict] = []

    def echo_handler(args: dict) -> str:
        seen.append(args)
        return json.dumps({"echoed": args.get("value")})

    fake = function_tool(
        name="echo",
        description="Echo a value back — a fake capability.",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        handler=echo_handler,
    )
    reg = ToolRegistry(extra_tools=[fake])

    names = [d["function"]["name"] for d in reg.tools()]
    assert "echo" in names

    result = reg.dispatch("echo", json.dumps({"value": "hi"}), tool_call_id="e")
    assert result["tool_call_id"] == "e"
    assert json.loads(result["content"]) == {"echoed": "hi"}
    assert seen == [{"value": "hi"}]


def test_register_after_construction_also_works() -> None:
    reg = ToolRegistry()
    calls: list[dict] = []
    reg.register(
        function_tool(
            name="noop",
            description="A no-op capability.",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: calls.append(args) or "done",
        )
    )
    assert "noop" in [d["function"]["name"] for d in reg.tools()]
    result = reg.dispatch("noop", "{}", tool_call_id="z")
    assert result["content"] == "done"
    assert calls == [{}]


# ---------------------------------------------------------------------------
# Degrade contract — dispatch never raises out
# ---------------------------------------------------------------------------


def test_dispatch_unknown_tool_returns_error_result() -> None:
    reg = ToolRegistry()
    result = reg.dispatch("does_not_exist", "{}", tool_call_id="u")
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "u"
    assert "error" in json.loads(result["content"])


def test_dispatch_malformed_arguments_returns_error_result_without_calling_handler() -> None:
    synths: list[str] = []
    reg = ToolRegistry(speak_engine=_fake_engine("tts", TTS_SAMPLE_RATE, synths))
    result = reg.dispatch("speak", "{not valid json", tool_call_id="m")
    assert result["role"] == "tool"
    assert "error" in json.loads(result["content"])
    assert synths == []  # handler never ran


def test_handler_exception_is_caught_and_returned_as_error_result() -> None:
    def boom(_text: str, **_kw) -> bytes:
        raise RuntimeError("synth exploded")

    reg = ToolRegistry(
        speak_engine=VoiceEngine(name="tts", synthesize=boom, samplerate=TTS_SAMPLE_RATE),
        play=_PlayRecorder(),
    )
    result = reg.dispatch("speak", json.dumps({"text": "x"}), tool_call_id="b")
    assert result["role"] == "tool"
    payload = json.loads(result["content"])
    assert "error" in payload


def test_apply_pose_without_express_seam_degrades_cleanly() -> None:
    reg = ToolRegistry()  # no express seam injected
    result = reg.dispatch("apply_pose", json.dumps({"emoji": "🤔"}), tool_call_id="q")
    assert result["role"] == "tool"
    assert "error" in json.loads(result["content"])


def test_dispatch_tolerates_a_dict_arguments_object() -> None:
    synths: list[str] = []
    play = _PlayRecorder()
    reg = ToolRegistry(speak_engine=_fake_engine("tts", TTS_SAMPLE_RATE, synths), play=play)
    # Some callers may hand an already-parsed dict rather than a JSON string.
    result = reg.dispatch("speak", {"text": "direct"}, tool_call_id="d")
    assert result["role"] == "tool"
    assert synths == ["direct"]


def test_dispatch_tool_call_id_defaults_to_none() -> None:
    reg = ToolRegistry()
    result = reg.dispatch("does_not_exist", "{}")
    assert result["tool_call_id"] is None


# ---------------------------------------------------------------------------
# Import boundary — no reachy.speech.llm / reachy.speech.events
# ---------------------------------------------------------------------------


def _imported_modules(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def test_tools_module_does_not_import_llm_or_events_directly() -> None:
    for name in _imported_modules(tools_mod):
        assert "speech.llm" not in name, f"tools.py must not import the LLM client ({name!r})"
        assert "speech.events" not in name, f"tools.py must not import the event bus ({name!r})"
    assert "llm" not in tools_mod.__dict__
    assert "events" not in tools_mod.__dict__


def test_importing_tools_does_not_pull_llm_or_events_into_sys_modules() -> None:
    """A fresh interpreter importing reachy.speech.tools must not transitively
    import reachy.speech.llm or reachy.speech.events (the say-boundary discipline,
    applied to this peer module)."""
    code = (
        "import sys, reachy.speech.tools;"
        "assert 'reachy.speech.llm' not in sys.modules, 'llm leaked';"
        "assert 'reachy.speech.events' not in sys.modules, 'events leaked';"
        "print('ok')"
    )
    proc = subprocess.run(  # nosec B603 — fixed args, sys.executable, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
