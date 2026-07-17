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
import logging
import subprocess  # nosec B404 — fixed-arg subprocess for an import-boundary probe
import sys

import pytest

import reachy.speech.tools as tools_mod
from reachy.motion.expression import ExpressionProducer
from reachy.speech.expressions import Catalog
from reachy.speech.harmonic import HARMONIC_SAMPLE_RATE
from reachy.speech.tools import ToolRegistry, function_tool
from reachy.speech.tts import DEFAULT_SAMPLE_RATE as TTS_SAMPLE_RATE
from reachy.speech.voice import VoiceEngine

# ---------------------------------------------------------------------------
# [SENSE] instrumentation (task t4)
# ---------------------------------------------------------------------------

_SENSE_LOGGER_NAME = "reachy.sense"


def _sense_records(caplog) -> list:
    return [r for r in caplog.records if r.name == _SENSE_LOGGER_NAME]


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


# ---------------------------------------------------------------------------
# Task t6: apply_pose advertises the catalog (enum) + rejects unknown keys
# ---------------------------------------------------------------------------


def test_apply_pose_emoji_property_publishes_the_full_catalog_as_a_sorted_enum() -> None:
    """The default registry (no explicit ``catalog_keys``) advertises the SAME
    key set as the shipped expression catalog, sorted for determinism."""
    reg = ToolRegistry()
    catalog = Catalog()
    by_name = {d["function"]["name"]: d["function"]["parameters"] for d in reg.tools()}
    assert by_name["apply_pose"]["properties"]["emoji"]["enum"] == sorted(catalog.keys())


def test_apply_pose_description_no_longer_lists_only_three_hardcoded_examples() -> None:
    """Guard the intent of the change: the description must not hard-name only
    the old 3-emoji example set — it should point at the enum instead."""
    reg = ToolRegistry()
    by_name = {d["function"]["name"]: d["function"]["description"] for d in reg.tools()}
    description = by_name["apply_pose"]
    assert "enum" in description.lower()
    assert "🤔, 😮, 🎉" not in description


def test_extra_toml_key_reaches_the_published_schema_with_no_code_change(tmp_path) -> None:
    """A registry built from a temp TOML with an EXTRA key (beyond the shipped
    catalog) advertises that key too — proving the enum is data-driven, not a
    hardcoded list that would need a code change to grow."""
    toml_path = tmp_path / "expressions.toml"
    toml_path.write_text(
        '[neutral]\nhead_x = 0.0\n\n["🤔"]\nhead_pitch = 6.0\n\n["🛸"]\nhead_pitch = 3.0\n'
    )
    catalog = Catalog(str(toml_path))
    reg = ToolRegistry(catalog_keys=catalog.keys())
    by_name = {d["function"]["name"]: d["function"]["parameters"] for d in reg.tools()}
    enum = by_name["apply_pose"]["properties"]["emoji"]["enum"]
    assert enum == sorted(catalog.keys())
    assert "🛸" in enum  # the extra key, present with no code change


def test_apply_pose_unknown_emoji_is_rejected_and_never_calls_express() -> None:
    """An emoji absent from the catalog is REJECTED (an error tool-result naming
    the valid keys) and the express seam must NEVER be invoked for it — this
    replaces the old silent-neutral-fallback behavior."""
    queue = _RecordingQueue()
    producer = ExpressionProducer(queue=queue)
    catalog = Catalog()

    reg = ToolRegistry(express=producer.express)
    result = reg.dispatch("apply_pose", json.dumps({"emoji": "✨"}), tool_call_id="n")

    assert result["role"] == "tool"
    payload = json.loads(result["content"])
    assert "error" in payload
    for key in catalog.keys():
        assert key in payload["error"]
    # The express seam was never called for the unknown key — nothing enqueued.
    assert queue.submitted == []


def test_apply_pose_valid_catalog_emoji_still_calls_express_and_returns_ok() -> None:
    """Existing behavior for a valid catalog emoji is unchanged: express IS
    called and the tool-result reports success."""
    queue = _RecordingQueue()
    producer = ExpressionProducer(queue=queue)

    reg = ToolRegistry(express=producer.express)
    result = reg.dispatch("apply_pose", json.dumps({"emoji": "🤔"}), tool_call_id="p")

    assert result["role"] == "tool"
    payload = json.loads(result["content"])
    assert payload == {"status": "ok", "emoji": "🤔"}
    assert len(queue.submitted) == 1


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


def test_apply_pose_unknown_emoji_no_longer_silently_falls_back_to_neutral() -> None:
    """Superseded behavior (t6): an unknown emoji used to silently no-op to the
    neutral pose.  It is now REJECTED before express is ever called — see
    ``test_apply_pose_unknown_emoji_is_rejected_and_never_calls_express`` above
    for the current contract."""
    queue = _RecordingQueue()
    producer = ExpressionProducer(queue=queue)

    reg = ToolRegistry(express=producer.express)
    reg.dispatch("apply_pose", json.dumps({"emoji": "✨"}), tool_call_id="n")

    # No neutral-fallback action was enqueued — express was never reached.
    assert queue.submitted == []


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
# describe_scene tool (task t10) — an INJECTED zero-arg seam, listed only when given
# ---------------------------------------------------------------------------


def test_describe_scene_is_not_advertised_without_a_seam() -> None:
    """No ``describe_scene`` seam -> the tool is not listed at all (unlike apply_pose,
    which is always listed but degrades)."""
    reg = ToolRegistry()
    names = [d["function"]["name"] for d in reg.tools()]
    assert "describe_scene" not in names
    assert reg.names() == ["speak", "harmonics", "apply_pose"]


def test_describe_scene_registered_when_seam_provided() -> None:
    reg = ToolRegistry(describe_scene=lambda: "a person waving")
    names = [d["function"]["name"] for d in reg.tools()]
    assert "describe_scene" in names
    # It is an OpenAI function-tool with a (no-arg) object parameter schema.
    by_name = {d["function"]["name"]: d["function"] for d in reg.tools()}
    fn = by_name["describe_scene"]
    assert fn["parameters"]["type"] == "object"
    assert isinstance(fn["description"], str) and fn["description"]


def test_describe_scene_dispatch_returns_the_description() -> None:
    reg = ToolRegistry(describe_scene=lambda: "a person waving at the desk")
    result = reg.dispatch("describe_scene", "{}", tool_call_id="d")
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "d"
    # The description reaches the agent verbatim in the tool-result content.
    assert "a person waving at the desk" in result["content"]


def test_describe_scene_seam_is_an_injected_zero_arg_callable() -> None:
    calls: list[int] = []

    def _seam() -> str:
        calls.append(1)
        return "a lamp on a table"

    reg = ToolRegistry(describe_scene=_seam)
    reg.dispatch("describe_scene", "{}", tool_call_id="d")
    assert calls == [1]  # the seam was invoked once, with no arguments


def test_describe_scene_empty_result_is_an_error_result() -> None:
    reg = ToolRegistry(describe_scene=lambda: "   ")
    result = reg.dispatch("describe_scene", "{}", tool_call_id="d")
    assert "error" in json.loads(result["content"])


def test_describe_scene_seam_raise_becomes_an_error_result() -> None:
    def _boom() -> str:
        raise RuntimeError("vlm-unreachable")

    reg = ToolRegistry(describe_scene=_boom)
    result = reg.dispatch("describe_scene", "{}", tool_call_id="d")
    assert result["role"] == "tool"
    assert "error" in json.loads(result["content"])


# ---------------------------------------------------------------------------
# forge tool (task t13) — an INJECTED dispatch seam, listed only when given
# ---------------------------------------------------------------------------


def test_forge_is_not_advertised_without_a_seam() -> None:
    """No ``forge`` seam -> the tool is not listed (like describe_scene, opt-in)."""
    reg = ToolRegistry()
    assert "forge" not in reg.names()


def test_forge_registered_when_seam_provided() -> None:
    reg = ToolRegistry(forge=lambda goal, improve=None: None)
    assert "forge" in reg.names()
    by_name = {d["function"]["name"]: d["function"] for d in reg.tools()}
    fn = by_name["forge"]
    assert fn["parameters"]["properties"]["goal"]["type"] == "string"
    assert fn["parameters"]["required"] == ["goal"]
    # improve is an OPTIONAL string param (not required)
    assert "improve" in fn["parameters"]["properties"]
    assert "improve" not in fn["parameters"]["required"]


def test_forge_dispatch_returns_immediately_with_a_status_string_and_calls_the_seam() -> None:
    calls: list[tuple] = []

    def _seam(goal, improve=None):
        calls.append((goal, improve))
        return "a-thread-object"  # the seam returns immediately (background dispatch)

    reg = ToolRegistry(forge=_seam)
    result = reg.dispatch("forge", json.dumps({"goal": "wave at people"}), tool_call_id="f")
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "f"
    # a status string naming the goal + the announce-when-ready promise
    assert "forging" in result["content"]
    assert "wave at people" in result["content"]
    assert "announced" in result["content"]
    # the injected dispatch seam was invoked once, goal + (None) improve
    assert calls == [("wave at people", None)]


def test_forge_dispatch_forwards_the_optional_improve_argument() -> None:
    calls: list[tuple] = []
    reg = ToolRegistry(forge=lambda goal, improve=None: calls.append((goal, improve)))
    reg.dispatch(
        "forge",
        json.dumps({"goal": "wave better", "improve": "wave-hello"}),
        tool_call_id="f",
    )
    assert calls == [("wave better", "wave-hello")]


def test_forge_dispatch_missing_goal_is_an_error_result_without_calling_seam() -> None:
    calls: list = []
    reg = ToolRegistry(forge=lambda goal, improve=None: calls.append(goal))
    result = reg.dispatch("forge", json.dumps({"improve": "x"}), tool_call_id="f")
    assert "error" in json.loads(result["content"])
    assert calls == [], "a missing goal must not reach the dispatch seam"


def test_forge_seam_does_not_block_the_dispatch_call() -> None:
    """The handler must return promptly — it hands off to the background seam and returns."""
    reg = ToolRegistry(forge=lambda goal, improve=None: None)
    # If the handler blocked on the forge round-trip this would hang; it must not.
    result = reg.dispatch("forge", json.dumps({"goal": "spin"}), tool_call_id="f")
    assert result["role"] == "tool"


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
# [SENSE] instrumentation (task t4)
# ---------------------------------------------------------------------------


def test_dispatch_logs_a_sense_action_line_naming_the_tool(caplog) -> None:
    """Every dispatch call — successful or not — logs one [SENSE stage=action] line
    naming the tool that was called."""
    queue = _RecordingQueue()
    producer = ExpressionProducer(queue=queue)
    reg = ToolRegistry(express=producer.express)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        result = reg.dispatch("apply_pose", json.dumps({"emoji": "🤔"}), tool_call_id="p")

    assert result["role"] == "tool"
    records = _sense_records(caplog)
    action_records = [r for r in records if "stage=action" in r.getMessage()]
    assert len(action_records) == 1
    assert "source=apply_pose" in action_records[0].getMessage()


def test_dispatch_success_emits_no_sense_drop_line(caplog) -> None:
    queue = _RecordingQueue()
    producer = ExpressionProducer(queue=queue)
    reg = ToolRegistry(express=producer.express)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        reg.dispatch("apply_pose", json.dumps({"emoji": "🤔"}), tool_call_id="p")

    records = _sense_records(caplog)
    drop_records = [r for r in records if "dropped" in r.getMessage()]
    assert drop_records == []


def test_dispatch_unknown_tool_emits_a_sense_drop_line_with_tool_error_reason(caplog) -> None:
    reg = ToolRegistry()

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        reg.dispatch("does_not_exist", "{}", tool_call_id="u")

    records = _sense_records(caplog)
    drop_records = [r for r in records if "dropped reason=tool-error" in r.getMessage()]
    assert len(drop_records) == 1


def test_dispatch_malformed_arguments_emits_a_sense_drop_line(caplog) -> None:
    reg = ToolRegistry(speak_engine=_fake_engine("tts", TTS_SAMPLE_RATE, []))

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        reg.dispatch("speak", "{not valid json", tool_call_id="m")

    records = _sense_records(caplog)
    drop_records = [r for r in records if "dropped reason=tool-error" in r.getMessage()]
    assert len(drop_records) == 1


def test_dispatch_handler_exception_emits_a_sense_drop_line(caplog) -> None:
    def boom(_text: str, **_kw) -> bytes:
        raise RuntimeError("synth exploded")

    reg = ToolRegistry(
        speak_engine=VoiceEngine(name="tts", synthesize=boom, samplerate=TTS_SAMPLE_RATE),
        play=_PlayRecorder(),
    )

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        reg.dispatch("speak", json.dumps({"text": "x"}), tool_call_id="b")

    records = _sense_records(caplog)
    drop_records = [r for r in records if "dropped reason=tool-error" in r.getMessage()]
    assert len(drop_records) == 1


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


def test_tools_module_does_not_import_motion_directly() -> None:
    """CRITICAL BOUNDARY (task t4): adding reachy.senselog instrumentation to
    dispatch() must not smuggle in a reachy.motion import — the pose seam stays a
    plain injected callable (see the module docstring's Import boundary note)."""
    for name in _imported_modules(tools_mod):
        assert "reachy.motion" not in name, f"tools.py must not import motion ({name!r})"
    assert "motion" not in tools_mod.__dict__


def test_tools_module_does_not_import_vision_directly() -> None:
    """CRITICAL BOUNDARY (task t10): the ``describe_scene`` seam is an injected
    zero-arg callable — tools.py must not import reachy.vision (which would pull cv2
    into the tool layer)."""
    for name in _imported_modules(tools_mod):
        assert "reachy.vision" not in name, f"tools.py must not import vision ({name!r})"
    assert "vision" not in tools_mod.__dict__


def test_tools_module_does_not_import_forge_directly() -> None:
    """CRITICAL BOUNDARY (task t13): the ``forge`` seam is an injected dispatch callable —
    tools.py must NOT import reachy.forge (the forge callable arrives injected)."""
    for name in _imported_modules(tools_mod):
        assert "reachy.forge" not in name, f"tools.py must not import forge ({name!r})"
    assert "forge" not in tools_mod.__dict__


def test_importing_tools_does_not_pull_forge_into_sys_modules() -> None:
    """A fresh interpreter importing reachy.speech.tools must not transitively import
    reachy.forge (the forge dispatch seam is injected at composition, never imported here)."""
    code = (
        "import sys, reachy.speech.tools;"
        "assert 'reachy.forge' not in sys.modules, 'forge leaked';"
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


def test_tools_module_imports_senselog_directly() -> None:
    """reachy.senselog is none of llm/events/motion — it is the one new import this
    task adds, and it is safe (stdlib-only logging helper)."""
    assert tools_mod.senselog.__name__ == "reachy.senselog"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
