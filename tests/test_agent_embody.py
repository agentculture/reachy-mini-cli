"""``agent embody`` — the embodiment layer's composition root (task t11).

This is the task where the seams built in waves 1-3 have to actually MEET, and
the arc's recurring failure mode is two ends of one contract disagreeing while
both sides' tests pass (the t4/t6 audio-tee wire is the arc's own worked
example: header-vs-headerless, float32-vs-int16, and nothing failing loudly).
So this module is deliberately weighted towards *joins* rather than towards
either end:

* the duplex session's ears reach the turn engine as an UTTERANCE, and its
  mouth reaches the same engine as ALREADY-SAID context — never as a trigger
  (the one wiring t10 flagged as the thing t11 could miss);
* the runtime feed's lines reach the engine as CLASSIFIED cues through
  :func:`reachy.embody.cues.classified_cues_for_line`, so a rule FIRE
  triggers a turn and every other cue parks (issue #143);
* the media profile's source and sink are the SAME two objects the session
  reads and speaks through, and the same sink the voice tools render into.

Plus the three acceptance criteria the plan names:

* **h15** — every cognition import in the command module is function-local:
  the parser build loads no cognition module (embody's own modules included)
  and the runtime's import closure gains no edge to the layer.
* **h14** — no ``reachy_mini`` anywhere the layer can reach at run time; its
  I/O is the tee socket, the bus, the spools and the daemon HTTP route.
* **h22** — every named failure mode reaches the journal AND the export feed,
  and killing the export consumer mid-run leaves the layer alive.

Nothing here opens a socket, a gateway, a broker, a robot or an audio device:
the media profile is the real :class:`~reachy.embody.media.EmbodySource` /
:class:`~reachy.embody.media.EmbodySink` wrappers over recording backends, the
duplex session is a double, and the turn engine is the REAL
:class:`~reachy.embody.engine.EmbodyTurnEngine` driven by a scripted
``turn_fn``.
"""

from __future__ import annotations

import ast
import base64
import collections
import io
import json
import logging
import subprocess  # nosec B404 — fixed argv, sys.executable, never shell=True
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import reachy.cli._commands.agent as agent_mod
from reachy.cli import _build_parser
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.embody.media import EmbodyMedia, EmbodySink, EmbodySource
from reachy.embody.tools import SPEAK
from reachy.explain.catalog import ENTRIES
from reachy.export.exporter import ExportHook
from reachy.speech.llm import ToolCall, TurnResult
from reachy.speech.realtime_duplex import Response, Utterance
from tests.conftest import WAIT_BUDGET_S

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG_ROOT = _REPO_ROOT / "reachy"
_COMMAND_MODULE = _PKG_ROOT / "cli" / "_commands" / "agent.py"


# --------------------------------------------------------------------------- #
# Doubles                                                                     #
# --------------------------------------------------------------------------- #


class _SourceBackend:
    """A media source backend that hands out one canned chunk, then silence."""

    def __init__(self, chunks: list | None = None) -> None:
        self._chunks = list(chunks or [])
        self.reads = 0
        self.closed = 0

    def read_native(self):
        self.reads += 1
        if not self._chunks:
            return None
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed += 1


class _SinkBackend:
    """A media sink backend recording every (pcm, samplerate) it is handed."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.played: list[tuple[bytes, int]] = []
        self.closed = 0
        self._fail = fail

    def play(self, pcm16_bytes: bytes, *, samplerate: int) -> None:
        if self._fail is not None:
            raise self._fail
        self.played.append((pcm16_bytes, samplerate))

    def close(self) -> None:
        self.closed += 1


def _media(*, sink_fails: BaseException | None = None, rate: int = 16000) -> EmbodyMedia:
    """The REAL profile-agnostic wrappers over recording backends."""
    return EmbodyMedia(
        profile="bench",
        source=EmbodySource(_SourceBackend(), target_sample_rate=rate),
        sink=EmbodySink(_SinkBackend(fail=sink_fails)),
    )


class _FakeSession:
    """A :class:`~reachy.speech.realtime_duplex.RealtimeDuplexSession` double.

    Records the composition kwargs so the wiring assertions can look at exactly
    what the composition root handed the real class, and exposes the two taps so
    a test can fire them the way the session worker thread would.
    """

    def __init__(self, *, start_error: BaseException | None = None, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = 0
        self.closed = 0
        self.utterances = 0
        self._start_error = start_error

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.started += 1

    def close(self) -> None:
        self.closed += 1

    # -- the two taps, fired the way the session worker would ---------------- #

    def hear(self, text: str) -> None:
        self.kwargs["on_utterance"](Utterance(text=text, t=1.0))

    def reply(self, text: str, *, interrupted: bool = False) -> None:
        self.kwargs["on_response"](
            Response(
                response_id="r1",
                text=text,
                audio=b"",
                samplerate=24000,
                t=1.0,
                interrupted=interrupted,
            )
        )


class _SessionFactory:
    def __init__(self, **overrides) -> None:
        self._overrides = overrides
        self.built: list[_FakeSession] = []

    def __call__(self, **kwargs) -> _FakeSession:
        session = _FakeSession(**{**kwargs, **self._overrides})
        self.built.append(session)
        return session

    @property
    def last(self) -> _FakeSession:
        return self.built[-1]


class _ScriptedTurn:
    """A ``turn_fn`` double returning canned turns and recording every call."""

    def __init__(self, *results: TurnResult) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    def __call__(self, messages: list[dict], **kwargs) -> TurnResult:
        self.calls.append({"messages": [dict(m) for m in messages], "kwargs": kwargs})
        if not self._results:
            return TurnResult(content="", tool_calls=[], finish_reason="stop")
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]

    def last_user_content(self) -> str:
        for message in reversed(self.calls[-1]["messages"]):
            if message.get("role") == "user":
                return message.get("content") or ""
        return ""


class _Sink:
    """An export sink recording every emitted block."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)

    def hook(self) -> ExportHook:
        return ExportHook(emit=self.emit, pose_resolver={}.get, time_fn=lambda: 1234.5)

    def of_type(self, block: str) -> list[object]:
        return [event for event in self.events if getattr(event, "t", None) == block]

    def texts(self, block: str) -> list[str]:
        return [getattr(event, "text", "") for event in self.of_type(block)]


class _BrokenStream(io.StringIO):
    """A stdout double that dies after *ok_writes* successful writes.

    Models the operator's consumer being killed mid-conversation: the first
    lines land, then every later write raises ``BrokenPipeError`` exactly as a
    closed pipe would.
    """

    def __init__(self, ok_writes: int = 1) -> None:
        super().__init__()
        self._budget = ok_writes
        self.attempts = 0

    def write(self, text: str) -> int:  # type: ignore[override]
        self.attempts += 1
        if self._budget <= 0:
            raise BrokenPipeError("consumer went away")
        self._budget -= 1
        return super().write(text)


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Every spool / overlay / socket write lands in this test's own tmp dir."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_EMBODY_MEDIA_PROFILE", raising=False)


def _parse(*argv: str):
    """Parse a real ``agent embody`` command line through the real parser."""
    return _build_parser().parse_args(["agent", "embody", *argv])


def _speak_turn(text: str) -> TurnResult:
    return TurnResult(
        content="",
        tool_calls=[
            ToolCall(
                id="c1",
                name=SPEAK,
                arguments={"text": text},
                arguments_json=f'{{"text": "{text}"}}',
            )
        ],
        finish_reason="tool_calls",
    )


def _wait_for(predicate, *, budget: float = WAIT_BUDGET_S) -> None:
    """Wait on the CONDITION, never on a sleep — see ``tests.conftest``."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition never became true within the shared wait budget")


def _compose(argv: list[str] | None = None, **seams):
    """Compose the layer over injected seams; returns (layer, args, sink)."""
    args = _parse(*(argv or []))
    sink = seams.pop("sink", None) or _Sink()
    export = seams.pop("export", sink.hook())
    layer = agent_mod._compose_embody_seam(args, export=export, **seams)
    return layer, args, sink


# --------------------------------------------------------------------------- #
# AST helpers, shared by the h14/h15 boundary tests                           #
# --------------------------------------------------------------------------- #


def _dotted(path: Path) -> str:
    parts = list(path.relative_to(_REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_names(path: Path, dotted: str) -> set[str]:
    """Every dotted name *path* imports, in ANY syntactic form (``ast.walk``)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = dotted.split(".")[: -node.level] or []
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if not module:
                continue
            names.add(module)
            for alias in node.names:
                names.add(f"{module}.{alias.name}")
    return names


def _repo_modules() -> dict[str, Path]:
    return {_dotted(p): p for p in sorted(_PKG_ROOT.rglob("*.py"))}


def _resolve(dotted: str, modules: dict[str, Path]) -> str | None:
    candidate = dotted
    while candidate and candidate not in modules:
        candidate = candidate.rsplit(".", 1)[0] if "." in candidate else ""
    return candidate or None


def _closure(starts) -> set[str]:
    modules = _repo_modules()
    graph = {name: _imported_names(path, name) for name, path in modules.items()}
    seen: set[str] = set()
    queue = collections.deque(starts)
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        for dep in graph.get(current, ()):
            if not dep.startswith("reachy"):
                continue
            resolved = _resolve(dep, modules)
            if resolved is not None and resolved not in seen:
                queue.append(resolved)
    return seen


def _module_scope_imports(path: Path) -> set[str]:
    """Dotted names imported at MODULE scope, excluding ``TYPE_CHECKING`` blocks.

    A ``TYPE_CHECKING``-guarded import never runs, so it cannot put a module on
    the import path of ``reachy --help``; that is exactly the shape ``attach``
    already uses for ``SenseCue``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test
            guarded = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            )
            if guarded:
                continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= _imported_names_of_node(node)
    return names


def _imported_names_of_node(node) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            names.add(alias.name)
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module:
            names.add(module)
            for alias in node.names:
                names.add(f"{module}.{alias.name}")
    return names


def _run_probe(script: str, tmp_path: Path) -> str:
    """Run *script* in a FRESH interpreter and return its stdout.

    A subprocess, never in-process: several sibling test modules legitimately
    stub ``reachy_mini`` into ``sys.modules``, so an in-process probe would be
    answering a question about the worker rather than about the layer.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent(script), encoding="utf-8")
    proc = subprocess.run(  # nosec B603 — fixed argv, sys.executable, no shell
        [sys.executable, str(probe)],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    return proc.stdout.strip()


# =========================================================================== #
# 1. The seams meet — the joins this task exists to make                      #
# =========================================================================== #


def test_a_heard_utterance_reaches_the_turn_engine_as_a_trigger():
    """duplex ``on_utterance`` -> ``engine.submit_utterance`` (t9 -> t10 join)."""
    factory = _SessionFactory()
    turn = _ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    layer, _args, _sink = _compose(
        media=_media(), session_factory=factory, lines=iter(()), turn_fn=turn
    )
    try:
        factory.last.hear("is anyone there")
        assert layer.engine.pending == 1
        assert layer.engine.run_turn() is True
        assert 'heard: "is anyone there"' in turn.last_user_content()
    finally:
        layer.close()


def test_a_spoken_reply_is_recorded_as_already_said_never_as_a_trigger():
    """duplex ``on_response`` -> ``engine.note_spoken`` — the join t10 flagged.

    The duplex session answers server-side, so without this the mind would not
    know its own mouth had already spoken and would call ``speak`` to repeat
    it. It is CONTEXT, not a trigger: a robot that treats its own voice as a
    perception talks to itself.
    """
    factory = _SessionFactory()
    turn = _ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    sink = _Sink()
    layer, _args, _sink = _compose(
        media=_media(), session_factory=factory, lines=iter(()), turn_fn=turn, sink=sink
    )
    try:
        factory.last.reply("I am right here.")
        assert layer.engine.pending == 0, "a reply must not trigger a turn"
        assert "I am right here." in sink.texts("message")

        layer.engine.submit_utterance("are you still there?")
        assert layer.engine.run_turn() is True
        assert "I have already said out loud" in turn.last_user_content()
        assert "I am right here." in turn.last_user_content()
    finally:
        layer.close()


def test_an_interrupted_reply_is_never_recorded_as_spoken():
    """A barge-in truncated reply is deliberately NOT played, so it was not said.

    ``RealtimeDuplexSession._finish_response`` publishes an interrupted reply
    through ``on_response`` like any other (the record carries the audio and
    says why) but never speaks it. Recording it as already-said would make the
    mind believe it had answered when the human heard nothing.
    """
    factory = _SessionFactory()
    sink = _Sink()
    layer, _args, _sink = _compose(
        media=_media(), session_factory=factory, lines=iter(()), sink=sink
    )
    try:
        factory.last.reply("half a sen", interrupted=True)
        assert layer.engine.pending == 0
        assert sink.texts("message") == []
    finally:
        layer.close()


def test_runtime_feed_lines_become_cues_on_the_turn_engine(tmp_path):
    """feed line -> ``classified_cues_for_line`` -> ``engine.submit_cues``.

    The t8 -> t10 join, and since #143 it also carries the class: the rule
    FIRE is what makes the turn run, and the sense line rides along as the
    context that turn drains.
    """
    feed = tmp_path / "runtime.feed"
    feed.write_text(
        json.dumps(
            {"t": "rule", "ts": 1.0, "rule": "pat-acknowledge", "action": "fire", "behavior": "nod"}
        )
        + "\n"
        + json.dumps({"t": "sense", "ts": 2.0, "pat": ["scratch", "level1"]})
        + "\n",
        encoding="utf-8",
    )
    turn = _ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    layer, _args, _sink = _compose(
        [f"--feed={feed}"], media=_media(), session_factory=_SessionFactory(), turn_fn=turn
    )
    try:
        layer.start()
        _wait_for(lambda: layer.reader.done)
        assert layer.reader.events == 2
        assert layer.reader.cues == 2
        assert layer.engine.run_turn() is True
        content = turn.last_user_content()
        assert "a behavior rule fired (pat-acknowledge): now doing nod" in content
        assert "felt a gentle scratch on the head" in content
    finally:
        layer.close()


def test_the_duplex_session_reads_and_speaks_through_the_media_profile():
    """The session's ears/mouth ARE the profile's source/sink — no second path."""
    factory = _SessionFactory()
    media = _media(rate=16000)
    layer, _args, _sink = _compose(media=media, session_factory=factory, lines=iter(()))
    try:
        kwargs = factory.last.kwargs
        assert kwargs["read_audio"] == media.source.read
        assert kwargs["sample_rate"] == media.source.sample_rate == 16000
        assert kwargs["play"] == media.sink.play
        # The AEC decision: present, and OFF unless the operator flips it.
        assert kwargs["mute_during_playback"] is False
    finally:
        layer.close()


def test_the_composed_session_kwargs_bind_to_the_REAL_duplex_signature():
    """The double must not be the only thing that accepts what we hand it.

    Every other collaborator in this module's tests is the real class, so a
    renamed or dropped parameter fails immediately. The duplex session is the
    one exception — it is always a double, because constructing the real one
    starts threads that dial a gateway — and a double taking ``**kwargs``
    accepts anything. That is precisely the shape of this arc's recurring
    defect: two ends of one contract disagreeing while both sides' tests pass.
    So bind the kwargs we actually composed against the REAL signature.
    """
    import inspect

    from reachy.speech.realtime_duplex import RealtimeDuplexSession

    factory = _SessionFactory()
    layer, _args, _sink = _compose(media=_media(), session_factory=factory, lines=iter(()))
    try:
        signature = inspect.signature(RealtimeDuplexSession.__init__)
        # Raises TypeError naming the offending parameter if the two disagree.
        signature.bind(object(), **factory.last.kwargs)
    finally:
        layer.close()


def test_the_mute_during_playback_seam_is_one_flag_away():
    """The AEC fallback is configuration, not a code change (t9's own words)."""
    factory = _SessionFactory()
    layer, _args, _sink = _compose(
        ["--mute-during-playback"], media=_media(), session_factory=factory, lines=iter(())
    )
    try:
        assert factory.last.kwargs["mute_during_playback"] is True
    finally:
        layer.close()


def test_the_voice_tools_render_through_the_same_profile_sink():
    """``speak`` synthesises and plays through the profile's sink, not a new one."""
    media = _media()
    layer, _args, _sink = _compose(
        media=media,
        session_factory=_SessionFactory(),
        lines=iter(()),
        synthesize={"tts": lambda text: b"\x01\x02" * len(text)},
    )
    try:
        result = layer.registry.dispatch(SPEAK, json.dumps({"text": "hi"}), "call_1")
        assert json.loads(result["content"])["ok"] is True
        played = media.sink._backend.played
        assert len(played) == 1
        pcm, rate = played[0]
        assert pcm == b"\x01\x02" * 2
        assert rate == 24000, "the tts leg's PCM16 rate must travel with the audio"
    finally:
        layer.close()


def test_the_action_set_the_layer_composes_is_the_closed_five(tmp_path):
    """Composition must not widen the blast radius — the registry stays closed."""
    from reachy.embody.tools import ACTION_SET

    layer, _args, _sink = _compose(
        media=_media(), session_factory=_SessionFactory(), lines=iter(())
    )
    try:
        assert tuple(layer.registry.names()) == ACTION_SET
        assert not hasattr(layer.registry, "register"), "the layer must have no hot-register door"
    finally:
        layer.close()


# =========================================================================== #
# 2. h15 — the zero-LLM boundary survives a second cognition root             #
# =========================================================================== #


def test_no_cognition_or_layer_module_is_imported_at_command_module_scope():
    """h15's letter: every embody/cognition import in ``agent.py`` is deferred.

    ``_build_parser()`` imports EVERY command module, so one module-scope
    import here puts an LLM client (and now a realtime socket client) on the
    import path of ``say run``, ``daemon status`` and ``--help``.
    """
    forbidden_prefixes = ("reachy.embody", "reachy.speech", "reachy.forge")
    offenders = sorted(
        name
        for name in _module_scope_imports(_COMMAND_MODULE)
        if name.startswith(forbidden_prefixes)
    )
    assert offenders == [], (
        "reachy/cli/_commands/agent.py imports cognition/layer modules at module "
        f"scope: {offenders}. Move them inside the function that needs them "
        "(TYPE_CHECKING-only is fine) — see tests/test_zero_llm_boundary.py."
    )


def test_building_the_cli_parser_with_embody_registered_loads_no_layer_module(tmp_path):
    """The behavioural half, in a FRESH interpreter, pinned by equality."""
    forbidden = (
        "reachy.embody.engine",
        "reachy.embody.tools",
        "reachy.embody.media",
        "reachy.embody.cues",
        "reachy.speech.realtime_duplex",
        "reachy.speech.llm",
        "reachy.speech.events",
        "reachy.speech.agent_turn",
        "reachy.speech.tools",
        "reachy.forge",
    )
    out = _run_probe(
        f"""
        import json, sys
        from reachy.cli import _build_parser
        _build_parser()
        print(json.dumps([m for m in {forbidden!r} if m in sys.modules]))
        """,
        tmp_path,
    )
    assert json.loads(out) == [], (
        "building the CLI parser now loads the embodiment layer or a cognition "
        f"module: {out}. A module-scope import crept back into a command module."
    )


def test_the_agent_embody_verb_is_actually_registered():
    """Guard the guard: the parser test above would be vacuous without this."""
    args = _parse("--max-turns=1")
    assert args.func is agent_mod.cmd_agent_embody
    assert args.agent_command == "embody"


def test_the_presence_runtimes_import_closure_gains_no_edge_to_the_layer():
    """h15's other half: ``behavior engine run`` must not reach the harness.

    The spec is explicit that a second cognition root is legal *under exactly
    these conditions* — one of which is that ``_commands/behavior.py`` never
    gains an import edge to the layer. If it did, the equality pin in
    ``test_the_only_llm_edge_in_the_presence_runtime_is_the_engagement_gate``
    would start reporting the layer's own LLM lane as a runtime edge.
    """
    runtime = _closure(["reachy.cli._commands.behavior"])
    leaked = sorted(
        module
        for module in runtime
        if module.startswith("reachy.embody") or module == "reachy.speech.realtime_duplex"
    )
    assert leaked == [], (
        f"the presence runtime can now load the embodiment layer: {leaked}. "
        "The dependency runs ONE way — the layer reads the runtime's exported "
        "surfaces; the runtime never imports the layer."
    )


def test_no_behavior_or_motion_module_imports_the_layer():
    """The package boundary the layer's own ``__init__`` promises."""
    offenders: list[str] = []
    for name, path in _repo_modules().items():
        if not name.startswith(("reachy.behavior.", "reachy.motion.")):
            continue
        for imported in _imported_names(path, name):
            if imported.startswith("reachy.embody"):
                offenders.append(f"{name} imports {imported}")
    assert offenders == [], "\n".join(offenders)


# =========================================================================== #
# 3. h14 — the layer never constructs a ReachyMini                            #
# =========================================================================== #


def test_neither_the_layer_package_nor_its_composition_root_names_reachy_mini():
    """The static half of h14, over the layer AND the module that composes it."""
    targets = {
        name: path for name, path in _repo_modules().items() if name.startswith("reachy.embody")
    }
    targets["reachy.cli._commands.agent"] = _COMMAND_MODULE
    assert "reachy.embody.media" in targets, "the layer scan found nothing to scan"

    offenders = sorted(
        f"{name} imports {imported}"
        for name, path in targets.items()
        for imported in _imported_names(path, name)
        if imported == "reachy_mini" or imported.startswith("reachy_mini.")
    )
    assert offenders == [], (
        "the embodiment layer named reachy_mini:\n  "
        + "\n  ".join(offenders)
        + "\nThe single-SDK-owner model gives the SDK to the runtime; the layer "
        "hears through the tee and speaks through the daemon HTTP route."
    )


def test_composing_the_whole_layer_never_pulls_reachy_mini_into_sys_modules(tmp_path):
    """The behavioural half of h14: a REAL composition in a fresh interpreter.

    Static absence is not enough — ``reachy.speech.playback`` and
    ``reachy.robot.transport`` both carry a lazy ``reachy_mini`` import on the
    sdk leg, and the layer's whole claim is that it never takes that leg. So
    this composes the layer for real (media profile, registry, turn engine,
    duplex session) and asks the interpreter.
    """
    out = _run_probe(
        f"""
        import os, sys
        os.environ["REACHY_STATE_DIR"] = {str(tmp_path)!r}
        os.environ["REACHY_EMBODY_MEDIA_PROFILE"] = "robot"
        # Never touch a live service: the daemon media route and the TTS lane are
        # both pointed at a guaranteed-dead loopback, and synthesis is a stub.
        os.environ["REACHY_BASE_URL"] = "http://127.0.0.1:1"
        os.environ["REACHY_TTS_URL"] = "http://127.0.0.1:1"
        # The hostile setting: an operator who exported REACHY_TRANSPORT=sdk must
        # STILL not be able to steer the layer onto the sdk leg.
        os.environ["REACHY_TRANSPORT"] = "sdk"

        import reachy.cli._commands.agent as agent
        from reachy.cli import _build_parser

        class FakeSession:
            def __init__(self, **kwargs): self.kwargs = kwargs
            def start(self): pass
            def close(self): pass

        def _silent(_text):
            return b"\\x00\\x00"

        args = _build_parser().parse_args(["agent", "embody"])
        layer = agent._compose_embody_seam(
            args,
            export=None,
            session_factory=lambda **kw: FakeSession(**kw),
            lines=iter(()),
            synthesize={{"tts": _silent, "harmonic": _silent}},
        )
        layer.registry.dispatch("speak", '{{"text": "hello"}}', "c1")
        layer.registry.dispatch("harmonics", '{{"text": "hello"}}', "c2")
        layer.close()
        print("reachy_mini" if "reachy_mini" in sys.modules else "clean")
        """,
        tmp_path,
    )
    assert out.splitlines()[-1] == "clean", (
        "composing (and speaking through) the embodiment layer pulled reachy_mini "
        "into sys.modules — something took the sdk leg"
    )


def test_the_only_reachy_mini_namers_in_the_layer_closure_are_the_sdk_legs_it_never_takes():
    """h14's positive statement, pinned by EQUALITY so it fails both ways.

    The layer's *static* closure does contain two modules that name
    ``reachy_mini`` — and pretending otherwise would be the dishonest version
    of this claim. Both are lazy sdk legs the layer provably never reaches:

    * ``reachy.speech.playback`` — its ``_open_sdk_media`` leg. The layer's
      robot sink names ``transport="http"`` as a literal keyword on every call,
      so ``REACHY_TRANSPORT=sdk`` cannot steer it there.
    * ``reachy.robot.sdk_transport`` — reached only through
      ``reachy.robot.transport``'s registry, which the layer never calls.

    The sibling test above proves neither is actually loaded at run time.
    """
    modules = _repo_modules()
    closure = _closure(
        [name for name in modules if name.startswith("reachy.embody")]
        + ["reachy.speech.realtime_duplex"]
    )
    namers = {
        name
        for name in closure
        if any(
            imported == "reachy_mini" or imported.startswith("reachy_mini.")
            for imported in _imported_names(modules[name], name)
        )
    }
    assert namers == {"reachy.speech.playback", "reachy.robot.sdk_transport"}, (
        f"the set of reachy_mini-naming modules in the layer's closure changed: "
        f"{sorted(namers)}. Either a new sdk leg entered the closure (fix it), or "
        "one left (tighten this expectation and say so)."
    )


def test_the_layer_never_reaches_the_runtimes_held_sdk_clients():
    """The held mic/camera/pose clients belong to the runtime process alone."""
    forbidden = {
        "reachy.robot.media_client",
        "reachy.robot.state_reader",
        "reachy.robot.sdk_transport.SdkTransport",
    }
    modules = _repo_modules()
    offenders = sorted(
        f"{name} imports {imported}"
        for name, path in modules.items()
        if name.startswith("reachy.embody") or name == "reachy.cli._commands.agent"
        for imported in _imported_names(path, name)
        if imported in forbidden
    )
    assert offenders == [], "\n  ".join(offenders)


# =========================================================================== #
# 4. h22 — every named failure is on the journal AND on the feed              #
# =========================================================================== #


def test_the_layers_failure_vocabulary_is_declared_named_and_unique():
    """A vocabulary that is not exported cannot be grepped, documented or tested."""
    assert agent_mod.EMBODY_REASONS, "the layer declares no named failure modes"
    assert len(set(agent_mod.EMBODY_REASONS)) == len(agent_mod.EMBODY_REASONS)
    for reason in agent_mod.EMBODY_REASONS:
        assert reason == reason.strip(), reason
        assert " " not in reason, reason


@pytest.mark.parametrize("reason", sorted(agent_mod.EMBODY_REASONS))
def test_every_named_failure_reaches_the_journal_and_the_export_feed(reason, caplog):
    """h22, stated exactly: a drop names itself in BOTH places, never one only."""
    sink = _Sink()
    with caplog.at_level(logging.INFO, logger="reachy"):
        agent_mod._embody_drop(sink.hook(), "layer", reason, "detail here")

    journal = "\n".join(record.getMessage() for record in caplog.records)
    assert f"[SENSE stage={agent_mod.EMBODY_STAGE}" in journal
    assert reason in journal
    assert any(
        reason in text for text in sink.texts("thinking")
    ), f"{reason} never reached the export feed"


def test_a_drop_without_an_export_hook_is_still_a_named_journal_line(caplog):
    """The feed is optional; the journal is not."""
    with caplog.at_level(logging.INFO, logger="reachy"):
        agent_mod._embody_drop(None, "layer", agent_mod.REASON_SPEAK_FAILED, "")
    assert agent_mod.REASON_SPEAK_FAILED in "\n".join(r.getMessage() for r in caplog.records)


def test_a_dead_speaker_is_a_named_drop_on_the_feed_and_a_refusal_to_the_model():
    """A wedged sink must not vanish: the model is told, and the feed shows it."""
    sink = _Sink()
    layer, _args, _sink = _compose(
        media=_media(sink_fails=RuntimeError("no such device")),
        session_factory=_SessionFactory(),
        lines=iter(()),
        sink=sink,
    )
    try:
        result = layer.registry.dispatch(SPEAK, json.dumps({"text": "hello"}), "c1")
        payload = json.loads(result["content"])
        assert payload["ok"] is False
        assert any(agent_mod.REASON_SPEAK_FAILED in text for text in sink.texts("thinking"))
    finally:
        layer.close()


def test_a_synthesis_failure_is_a_named_drop_not_a_crash():
    """A wedged TTS resolves the same way a wedged speaker does."""
    sink = _Sink()

    def _boom(_text: str) -> bytes:
        raise RuntimeError("tts is down")

    layer, _args, _sink = _compose(
        media=_media(),
        session_factory=_SessionFactory(),
        lines=iter(()),
        synthesize={"tts": _boom},
        sink=sink,
    )
    try:
        result = layer.registry.dispatch(SPEAK, json.dumps({"text": "hello"}), "c1")
        assert json.loads(result["content"])["ok"] is False
        assert any(agent_mod.REASON_SPEAK_FAILED in text for text in sink.texts("thinking"))
    finally:
        layer.close()


def test_a_cue_source_that_dies_mid_stream_is_a_named_drop_not_a_crash():
    """The runtime feed going away must not take the conversation with it."""

    def _dying_lines():
        yield json.dumps({"t": "sense", "ts": 1.0, "pat": ["scratch", "level1"]})
        raise OSError("the runtime went away")

    sink = _Sink()
    layer, _args, _sink = _compose(
        media=_media(), session_factory=_SessionFactory(), lines=_dying_lines(), sink=sink
    )
    try:
        layer.start()
        _wait_for(lambda: layer.reader.done)
        assert layer.engine.parked == 1, "the cue read before the fault still landed"
        assert any(agent_mod.REASON_CUE_SOURCE_FAILED in text for text in sink.texts("thinking"))
    finally:
        layer.close()


def test_a_voice_that_will_not_resolve_leaves_the_tool_advertised_but_refusing(monkeypatch):
    """The action set must not change SHAPE with the box's audio configuration.

    A model that finds a different tool list on every start learns a different
    robot every time, so an unresolvable voice engine yields an advertised tool
    that refuses by name — never a missing tool.
    """
    from reachy.embody.tools import ACTION_SET, REFUSAL_NO_VOICE
    from reachy.speech import voice as voice_mod

    def _no_voice(_name=None):
        raise CliError(code=1, message="no such engine", remediation="install one")

    monkeypatch.setattr(voice_mod, "resolve_voice_engine", _no_voice)
    sink = _Sink()
    layer, _args, _sink = _compose(
        media=_media(), session_factory=_SessionFactory(), lines=iter(()), sink=sink
    )
    try:
        assert tuple(layer.registry.names()) == ACTION_SET
        result = layer.registry.dispatch(SPEAK, json.dumps({"text": "hello"}), "c1")
        assert json.loads(result["content"])["refusal"] == REFUSAL_NO_VOICE
        assert any(agent_mod.REASON_VOICE_UNAVAILABLE in text for text in sink.texts("thinking"))
    finally:
        layer.close()


def test_a_resource_that_refuses_to_close_is_a_named_drop_not_a_raise():
    """A fault in teardown must never mask the reason the layer was stopping."""

    class _StuckSession(_FakeSession):
        def close(self) -> None:
            raise RuntimeError("the socket will not let go")

    sink = _Sink()
    layer, _args, _sink = _compose(
        media=_media(),
        session_factory=lambda **kwargs: _StuckSession(**kwargs),
        lines=iter(()),
        sink=sink,
    )
    layer.close()  # must not raise
    assert any(agent_mod.REASON_SHUTDOWN_FAILED in text for text in sink.texts("thinking"))
    layer.close()  # idempotent: a second close is a no-op, not a second fault
    assert len([t for t in sink.texts("thinking") if agent_mod.REASON_SHUTDOWN_FAILED in t]) == 1


def test_an_export_sink_that_raises_never_takes_the_drop_path_down(caplog):
    """The observability path itself must not become a failure mode."""

    def _hostile(_event):
        raise RuntimeError("sink exploded")

    hook = ExportHook(emit=_hostile, pose_resolver={}.get, time_fn=lambda: 0.0)
    with caplog.at_level(logging.INFO, logger="reachy"):
        agent_mod._embody_drop(hook, "layer", agent_mod.REASON_SPEAK_FAILED, "detail")
    assert agent_mod.REASON_SPEAK_FAILED in "\n".join(r.getMessage() for r in caplog.records)


def test_max_events_bounds_the_cue_reader(tmp_path):
    """``--max-events`` stops the intake, exactly as ``attach``'s does."""
    feed = tmp_path / "runtime.feed"
    feed.write_text(
        "\n".join(
            json.dumps({"t": "sense", "ts": float(i), "pat": ["scratch", "level1"]})
            for i in range(5)
        )
        + "\n",
        encoding="utf-8",
    )
    layer, _args, _sink = _compose(
        [f"--feed={feed}", "--max-events=2"],
        media=_media(),
        session_factory=_SessionFactory(),
    )
    try:
        layer.start()
        layer.start()  # idempotent: one reader thread, not two
        _wait_for(lambda: layer.reader.done)
        assert layer.reader.events == 2
        assert layer.reader.cues == 2, "both cues were accepted"
        assert layer.engine.parked == 1, "and, being the same fact twice, coalesced"
    finally:
        layer.close()


def test_stopping_the_layer_stops_the_reader_mid_feed():
    """``close()`` must end the intake even while the runtime is still writing."""
    box: list = []

    def _endless_lines():
        yield json.dumps({"t": "sense", "ts": 1.0, "pat": ["scratch", "level1"]})
        box[0].reader.stop()  # the operator's Ctrl-C, landing between two lines
        for i in range(2, 1000):
            yield json.dumps({"t": "sense", "ts": float(i), "pat": ["scratch", "level1"]})

    layer, _args, _sink = _compose(
        media=_media(), session_factory=_SessionFactory(), lines=_endless_lines()
    )
    box.append(layer)
    try:
        layer.start()
        _wait_for(lambda: layer.reader.done)
        assert layer.reader.events == 1, "the reader kept consuming after it was stopped"
    finally:
        layer.close()


def test_an_explicit_stop_ends_the_run_even_with_a_live_feed():
    """``Ctrl-C`` (and ``close``) beat the reader-exhausted rule."""
    layer, _args, _sink = _compose(
        media=_media(), session_factory=_SessionFactory(), lines=iter(())
    )
    try:
        layer.engine.submit_utterance("are you still there?")
        layer.reader.done = False
        assert layer.should_stop() is False
        layer.request_stop()
        assert layer.should_stop() is True
    finally:
        layer.close()


def test_a_keyboard_interrupt_shuts_the_layer_down_cleanly(tmp_path):
    """The operator's own stop is a clean exit-0 that still closes every resource."""

    class _InterruptingEngine:
        pending = 0

        def submit_cues(self, texts) -> int:
            return 0

        def submit_utterance(self, text) -> bool:
            return False

        def note_spoken(self, text) -> None:
            return None

        def run(self, **_kwargs) -> int:
            raise KeyboardInterrupt

    factory = _SessionFactory()
    media = _media()
    code = agent_mod.cmd_agent_embody(
        _parse("--json"),
        compose=lambda args, *, export: agent_mod._compose_embody_seam(
            args,
            export=export,
            media=media,
            session_factory=factory,
            engine_factory=lambda **_kw: _InterruptingEngine(),
            lines=iter(()),
        ),
    )
    assert code == 0
    assert factory.last.closed == 1
    assert media.source._backend.closed == 1


def test_a_session_that_refuses_to_start_is_a_named_drop_not_a_crash():
    """A dead gateway leaves a deaf, mute layer that still thinks about cues."""
    sink = _Sink()
    factory = _SessionFactory(start_error=OSError("gateway refused"))
    layer, _args, _sink = _compose(
        media=_media(), session_factory=factory, lines=iter(()), sink=sink
    )
    try:
        layer.start()
        assert any(agent_mod.REASON_SESSION_START_FAILED in text for text in sink.texts("thinking"))
    finally:
        layer.close()


def test_killing_the_export_consumer_mid_run_leaves_the_layer_alive(tmp_path, capsys):
    """h22's second half: a disconnecting consumer never kills the conversation."""
    feed = tmp_path / "runtime.feed"
    feed.write_text(
        "\n".join(
            [json.dumps({"t": "rule", "ts": 0.0, "rule": "pat-acknowledge", "action": "fire"})]
            + [
                json.dumps({"t": "sense", "ts": float(i), "pat": ["scratch", "level1"]})
                for i in range(1, 4)
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    stream = _BrokenStream(ok_writes=1)
    turn = _ScriptedTurn(_speak_turn("hi"), TurnResult(content="done", finish_reason="stop"))
    factory = _SessionFactory()
    media = _media()

    code = agent_mod.cmd_agent_embody(
        _parse(f"--feed={feed}", "--export=-", "--max-turns=3", "--turn-interval=0.001"),
        stream=stream,
        compose=lambda args, *, export: agent_mod._compose_embody_seam(
            args,
            export=export,
            media=media,
            session_factory=factory,
            turn_fn=turn,
            synthesize={"tts": lambda text: b"\x00\x00"},
        ),
    )

    assert code == 0, "the layer died when its export consumer went away"
    assert stream.attempts > 1, "the exporter never tried to write past the break"
    assert len(turn.calls) >= 1, "no turn ran at all"
    assert media.sink._backend.played, "the layer stopped speaking when the feed broke"


# =========================================================================== #
# 5. The clip -> ask() perception lane (task t11, issue #139's h9 blocker)    #
# =========================================================================== #
#
# EmbodyTurnEngine.ask() (the tool-less senses lane) was designed and never
# wired anywhere in reachy/ — exactly what blocked #139's h9 acceptance ("ask
# the worker model where it is"). _ClipAsker is its first real caller: it
# reads state.json's 'clip' key (reachy/behavior/clip_rider.py's path
# reference), and when it names a fresh clip, calls ask() and parks the
# answer as CONTEXT for the next TRIGGERED turn (t7/#143's policy) — never a
# trigger of its own. Every negative path (missing/unavailable, stale,
# unreadable, ask() raising, ask() answering empty) resolves to exactly one
# named, non-blocking drop.


class _FakeAskEngine:
    """A minimal engine double exposing only what _ClipAsker calls.

    Deliberately NOT the real EmbodyTurnEngine for the per-scenario drop
    tests below — a bare double keeps each test a single-purpose unit test of
    _ClipAsker's OWN policy (never blocks, never raises, names every negative
    path) with no gateway, no tool registry and no turn machinery involved.
    The full loop — the answer actually reaching a REAL triggered turn's
    prompt — is proven separately below with the real EmbodyTurnEngine.
    """

    def __init__(self, ask_fn=None) -> None:
        self._ask_fn = ask_fn if ask_fn is not None else (lambda prompt, **kw: "")
        self.ask_calls: list[object] = []
        self.submitted: list[tuple[str, object]] = []

    def ask(self, prompt, **kwargs):
        self.ask_calls.append(prompt)
        return self._ask_fn(prompt, **kwargs)

    def submit_cue(self, text: str, *, cue_class=None) -> bool:
        self.submitted.append((text, cue_class))
        return True


def _clip_file(tmp_path: Path, name: str = "clip.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"not a real mp4, just some bytes")
    return path


# --------------------------------------------------------------------------- #
# build_clip_question — the pure content-shaping helper _ClipAsker calls      #
# --------------------------------------------------------------------------- #
#
# Lives (and is tested) here, not in reachy.embody.engine: that module's own
# "the engine reads no file" claim is machine-checked by an AST scan over the
# WHOLE module (tests/test_embody_engine.py), so the one genuine file read
# this feature needs belongs on the composition-root side of that boundary.


def test_build_clip_question_produces_the_probed_wire_shape(tmp_path):
    """The exact shape docs/evidence/2026-08-01-probe-video-wire-format.md proved works."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not really an mp4, just some bytes")

    content = agent_mod.build_clip_question(clip, "describe what you see")

    encoded = base64.b64encode(b"not really an mp4, just some bytes").decode("ascii")
    assert content == [
        {"type": "text", "text": "describe what you see"},
        {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{encoded}"}},
    ]


def test_build_clip_question_defaults_to_the_shipped_prompt(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    content = agent_mod.build_clip_question(clip)
    assert content[0] == {"type": "text", "text": agent_mod.DEFAULT_CLIP_PROMPT}


def test_build_clip_question_raises_on_an_unreadable_path(tmp_path):
    """A pure, RAISING helper by design — the caller (_ClipAsker) names the drop."""
    with pytest.raises(OSError):
        agent_mod.build_clip_question(tmp_path / "does-not-exist.mp4")


def test_a_missing_clip_block_is_a_named_drop_never_a_call_to_ask():
    engine = _FakeAskEngine()
    sink = _Sink()
    asker = agent_mod._ClipAsker(engine, read_clip=lambda: None, export=sink.hook())

    asker.poll_once()

    assert engine.ask_calls == [], "no clip reference means nothing to ask about"
    assert engine.submitted == []
    assert any(agent_mod.REASON_CLIP_UNAVAILABLE in t for t in sink.texts("thinking"))


def test_an_unavailable_clip_block_names_the_blocks_own_reason():
    engine = _FakeAskEngine()
    sink = _Sink()
    asker = agent_mod._ClipAsker(
        engine,
        read_clip=lambda: {"available": False, "reason": "vision-extra-absent"},
        export=sink.hook(),
    )

    asker.poll_once()

    assert engine.ask_calls == []
    assert any(
        agent_mod.REASON_CLIP_UNAVAILABLE in t and "vision-extra-absent" in t
        for t in sink.texts("thinking")
    )


def test_a_clip_reader_that_raises_is_a_named_drop_never_a_crash():
    """An unreadable/corrupt state.json must not take the poller down with it."""
    engine = _FakeAskEngine()
    sink = _Sink()

    def _boom():
        raise OSError("state.json vanished mid-read")

    asker = agent_mod._ClipAsker(engine, read_clip=_boom, export=sink.hook())

    asker.poll_once()  # must not raise

    assert engine.ask_calls == []
    assert any(agent_mod.REASON_CLIP_UNAVAILABLE in t for t in sink.texts("thinking"))


def test_a_stale_clip_is_a_named_drop_never_asked_about(tmp_path):
    clip = _clip_file(tmp_path)
    engine = _FakeAskEngine()
    sink = _Sink()
    old_ts = 1_000.0
    asker = agent_mod._ClipAsker(
        engine,
        read_clip=lambda: {"available": True, "ts": old_ts, "path": str(clip)},
        export=sink.hook(),
        clock=lambda: old_ts + agent_mod.DEFAULT_CLIP_STALE_AFTER_S + 1.0,
    )

    asker.poll_once()

    assert engine.ask_calls == [], "a stale clip must never be asked about"
    assert any(agent_mod.REASON_CLIP_STALE in t for t in sink.texts("thinking"))


def test_an_unreadable_clip_path_is_a_named_drop_never_asked_about():
    engine = _FakeAskEngine()
    sink = _Sink()
    asker = agent_mod._ClipAsker(
        engine,
        read_clip=lambda: {
            "available": True,
            "ts": time.monotonic(),
            "path": "/nonexistent/clip.mp4",
        },
        export=sink.hook(),
    )

    asker.poll_once()

    assert engine.ask_calls == [], "an unreadable path must never reach ask()"
    assert any(agent_mod.REASON_CLIP_UNREADABLE in t for t in sink.texts("thinking"))


def test_ask_raising_is_a_named_drop_never_a_raised_exception(tmp_path):
    clip = _clip_file(tmp_path)

    def _boom(prompt, **kw):
        raise RuntimeError("the senses gateway exploded")

    engine = _FakeAskEngine(ask_fn=_boom)
    sink = _Sink()
    asker = agent_mod._ClipAsker(
        engine,
        read_clip=lambda: {"available": True, "ts": time.monotonic(), "path": str(clip)},
        export=sink.hook(),
    )

    asker.poll_once()  # must not raise

    assert engine.submitted == [], "a failed ask must never reach context"
    assert any(agent_mod.REASON_CLIP_ASK_FAILED in t for t in sink.texts("thinking"))


def test_an_empty_answer_is_a_named_drop_never_context(tmp_path):
    clip = _clip_file(tmp_path)
    engine = _FakeAskEngine(ask_fn=lambda prompt, **kw: "   ")
    sink = _Sink()
    asker = agent_mod._ClipAsker(
        engine,
        read_clip=lambda: {"available": True, "ts": time.monotonic(), "path": str(clip)},
        export=sink.hook(),
    )

    asker.poll_once()

    assert engine.submitted == []
    assert any(agent_mod.REASON_CLIP_ASK_EMPTY in t for t in sink.texts("thinking"))


def test_a_fresh_clip_reaches_ask_and_the_answer_lands_as_context(tmp_path):
    clip = _clip_file(tmp_path)
    engine = _FakeAskEngine(ask_fn=lambda prompt, **kw: "a kitchen, someone is cooking")
    asker = agent_mod._ClipAsker(
        engine, read_clip=lambda: {"available": True, "ts": time.monotonic(), "path": str(clip)}
    )

    asker.poll_once()

    assert len(engine.ask_calls) == 1
    assert asker.asks == 1
    assert engine.submitted, "the answer never reached the engine as context"
    text, cue_class = engine.submitted[0]
    assert "a kitchen" in text

    from reachy.embody.cues import CueClass

    assert cue_class is CueClass.CONTEXT, "the answer must land as CONTEXT, never a trigger"


def test_ask_is_called_with_the_probed_multimodal_wire_shape(tmp_path):
    """The content _ClipAsker builds is exactly build_clip_question's shape."""
    clip = _clip_file(tmp_path)
    engine = _FakeAskEngine(ask_fn=lambda prompt, **kw: "a kitchen")
    asker = agent_mod._ClipAsker(
        engine, read_clip=lambda: {"available": True, "ts": time.monotonic(), "path": str(clip)}
    )

    asker.poll_once()

    assert engine.ask_calls[0] == agent_mod.build_clip_question(clip, agent_mod.DEFAULT_CLIP_PROMPT)


def test_a_custom_prompt_reaches_build_clip_question(tmp_path):
    clip = _clip_file(tmp_path)
    engine = _FakeAskEngine(ask_fn=lambda prompt, **kw: "a kitchen")
    asker = agent_mod._ClipAsker(
        engine,
        read_clip=lambda: {"available": True, "ts": time.monotonic(), "path": str(clip)},
        prompt="where are you right now?",
    )

    asker.poll_once()

    assert engine.ask_calls[0] == agent_mod.build_clip_question(clip, "where are you right now?")


def test_the_same_clip_reference_is_never_asked_about_twice(tmp_path):
    clip = _clip_file(tmp_path)
    engine = _FakeAskEngine(ask_fn=lambda prompt, **kw: "a kitchen")
    ts = time.monotonic()
    asker = agent_mod._ClipAsker(
        engine, read_clip=lambda: {"available": True, "ts": ts, "path": str(clip)}
    )

    asker.poll_once()
    asker.poll_once()
    asker.poll_once()

    assert len(engine.ask_calls) == 1, "an unchanged clip reference must not be re-asked"


def test_a_fresh_clip_reference_after_a_stale_one_is_still_asked_about(tmp_path):
    """A stale ts is dropped and not asked about; a LATER fresh ts still reaches ask()."""
    clip = _clip_file(tmp_path)
    engine = _FakeAskEngine(ask_fn=lambda prompt, **kw: "a kitchen")
    state = {"ts": 1.0}
    now = 1.0 + agent_mod.DEFAULT_CLIP_STALE_AFTER_S + 1.0

    def _read():
        return {"available": True, "ts": state["ts"], "path": str(clip)}

    asker = agent_mod._ClipAsker(engine, read_clip=_read, clock=lambda: now)
    asker.poll_once()  # stale relative to `now` -> dropped, never asked
    assert engine.ask_calls == []

    state["ts"] = now - 1.0  # a NEW clip lands, fresh relative to `now`
    asker.poll_once()
    assert len(engine.ask_calls) == 1


def test_a_permanently_unavailable_clip_logs_only_once():
    """Dedup, mirroring ClipRider._report — a bare box must not flood the journal."""
    engine = _FakeAskEngine()
    sink = _Sink()
    asker = agent_mod._ClipAsker(engine, read_clip=lambda: None, export=sink.hook())

    asker.poll_once()
    asker.poll_once()
    asker.poll_once()

    assert len(sink.texts("thinking")) == 1, "the same failure must not spam the feed"


def test_recovery_after_a_failure_reports_a_fresh_drop_on_the_next_one(tmp_path):
    """The dedup latch resets on success, so a LATER distinct failure still reports."""
    clip = _clip_file(tmp_path)
    state = {"ts": 1.0, "available": True}
    engine = _FakeAskEngine(ask_fn=lambda prompt, **kw: "a kitchen")
    sink = _Sink()

    def _read():
        if not state["available"]:
            return None
        return {"available": True, "ts": state["ts"], "path": str(clip)}

    asker = agent_mod._ClipAsker(
        engine, read_clip=_read, export=sink.hook(), clock=lambda: state["ts"]
    )
    asker.poll_once()  # succeeds, resets the dedup latch
    assert len(sink.texts("thinking")) == 0

    # A later poll finds the SAME clip missing entirely (block gone) -> a fresh drop
    state["available"] = False
    asker.poll_once()
    assert len(sink.texts("thinking")) == 1


def test_a_slow_ask_never_delays_or_blocks_a_pending_turn(tmp_path):
    """The core constraint: a stuck senses-lane call must never touch a turn.

    ask() is deliberately outside EmbodyTurnEngine's _turn_lock, and
    _ClipAsker runs on its own thread rather than being wired into run()'s
    before_turn hook — so a poll stuck inside ask() must never delay a
    concurrent run_turn() on the very same engine.
    """
    from reachy.embody.engine import EmbodyModels, EmbodyTurnEngine

    clip = _clip_file(tmp_path)
    release = threading.Event()
    entered_ask = threading.Event()

    def _turn_fn(messages, **kwargs):
        if kwargs.get("model") == "senses":
            entered_ask.set()
            release.wait(WAIT_BUDGET_S)
            return TurnResult(content="a kitchen", finish_reason="stop")
        return TurnResult(content="ok", finish_reason="stop")

    class _MiniRegistry:
        def tools(self) -> list[dict]:
            return []

        def dispatch(self, name, arguments_json=None, tool_call_id=None) -> dict:
            return {"role": "tool", "tool_call_id": tool_call_id, "content": "{}"}

    engine = EmbodyTurnEngine(
        registry=_MiniRegistry(),
        turn_fn=_turn_fn,
        models=EmbodyModels(worker="worker", senses="senses"),
    )
    asker = agent_mod._ClipAsker(
        engine, read_clip=lambda: {"available": True, "ts": time.monotonic(), "path": str(clip)}
    )

    poll_thread = threading.Thread(target=asker.poll_once, daemon=True)
    poll_thread.start()
    try:
        assert entered_ask.wait(WAIT_BUDGET_S), "the poll never reached ask()"

        engine.submit_utterance("where are you?")
        started = time.monotonic()
        assert engine.run_turn() is True
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"run_turn() waited on the stuck ask() call ({elapsed:.2f}s)"
    finally:
        release.set()
        poll_thread.join(WAIT_BUDGET_S)


def test_the_answer_becomes_context_for_the_next_triggered_turn(tmp_path):
    """The full loop with the REAL engine: clip -> ask() -> CONTEXT -> next turn's prompt."""
    from reachy.embody.engine import EmbodyModels, EmbodyTurnEngine

    clip = _clip_file(tmp_path)
    calls: list[dict] = []

    def _turn_fn(messages, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("model") == "senses":
            return TurnResult(content="a kitchen, someone is cooking", finish_reason="stop")
        return TurnResult(content="ok", finish_reason="stop")

    class _MiniRegistry:
        def tools(self) -> list[dict]:
            return []

        def dispatch(self, name, arguments_json=None, tool_call_id=None) -> dict:
            return {"role": "tool", "tool_call_id": tool_call_id, "content": "{}"}

    engine = EmbodyTurnEngine(
        registry=_MiniRegistry(),
        turn_fn=_turn_fn,
        models=EmbodyModels(worker="worker", senses="senses"),
    )
    asker = agent_mod._ClipAsker(
        engine, read_clip=lambda: {"available": True, "ts": time.monotonic(), "path": str(clip)}
    )

    asker.poll_once()
    assert engine.pending == 0, "the clip answer must never trigger a turn on its own"
    assert engine.parked == 1

    engine.submit_utterance("where are you?")
    assert engine.run_turn() is True

    worker_call = next(c for c in calls if c["kwargs"].get("model") == "worker")
    user_content = next(m["content"] for m in worker_call["messages"] if m.get("role") == "user")
    assert "a kitchen" in user_content
    assert 'heard: "where are you?"' in user_content


def test_the_composition_root_starts_and_stops_the_clip_asker_with_the_layer(tmp_path):
    """h22-style wiring proof: _compose_embody_seam builds and lifecycles a real asker."""
    seen: dict = {"available": True, "ts": time.monotonic(), "path": str(_clip_file(tmp_path))}
    turn = _ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    layer, _args, _sink = _compose(
        media=_media(),
        session_factory=_SessionFactory(),
        lines=iter(()),
        turn_fn=turn,
        clip_reader=lambda: seen,
        clip_poll_interval=0.01,
    )
    try:
        assert layer.clip_asker is not None
        layer.start()
        _wait_for(lambda: layer.clip_asker.asks >= 1)
        assert layer.engine.parked == 1
    finally:
        layer.close()
    assert layer.clip_asker.thread is not None
    _wait_for(lambda: not layer.clip_asker.thread.is_alive())


def test_a_missing_clip_reader_seam_falls_back_to_the_real_state_json(tmp_path):
    """No injected clip_reader -> the production seam reads state.json under REACHY_STATE_DIR."""
    layer, _args, _sink = _compose(
        media=_media(), session_factory=_SessionFactory(), lines=iter(())
    )
    try:
        assert layer.clip_asker is not None
        # No ClipRider ever ran in this tmp state dir, so the read resolves to
        # "no clip block published yet" rather than raising.
        layer.clip_asker.poll_once()
        assert layer.clip_asker.asks == 0
    finally:
        layer.close()


# =========================================================================== #
# 6. The verb itself: end to end, errors, overview, catalog                   #
# =========================================================================== #


def test_embody_runs_turns_over_a_feed_and_publishes_its_own_cognition_feed(tmp_path):
    """The whole verb, end to end, over injected seams — no gateway, no robot."""
    feed = tmp_path / "runtime.feed"
    feed.write_text(
        json.dumps(
            {"t": "rule", "ts": 1.0, "rule": "pat-acknowledge", "action": "fire", "behavior": "nod"}
        )
        + "\n",
        encoding="utf-8",
    )
    stream = io.StringIO()
    turn = _ScriptedTurn(
        _speak_turn("that tickles"), TurnResult(content="🙂 done", finish_reason="stop")
    )
    layer_box: list = []

    def _compose_seam(args, *, export):
        layer = agent_mod._compose_embody_seam(
            args,
            export=export,
            media=_media(),
            session_factory=_SessionFactory(),
            turn_fn=turn,
            synthesize={"tts": lambda text: b"\x00\x00"},
        )
        layer_box.append(layer)
        return layer

    code = agent_mod.cmd_agent_embody(
        _parse(f"--feed={feed}", "--export=-", "--max-turns=1", "--turn-interval=0.001"),
        stream=stream,
        compose=_compose_seam,
    )

    assert code == 0
    blocks = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    kinds = {block["t"] for block in blocks}
    assert kinds <= {"thinking", "message", "emotion"}, "a runtime block leaked onto c27's feed"
    assert "message" in kinds
    assert "thinking" in kinds
    assert any(block.get("text") == "that tickles" for block in blocks if block["t"] == "message")
    assert layer_box[0].session.closed == 1, "the session was not closed at shutdown"


def test_embody_summary_goes_to_stdout_as_json_when_not_exporting(tmp_path, capsys):
    feed = tmp_path / "runtime.feed"
    feed.write_text(
        json.dumps({"t": "rule", "ts": 1.0, "rule": "pat-acknowledge", "action": "fire"}) + "\n"
    )
    turn = _ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))

    code = agent_mod.cmd_agent_embody(
        _parse(f"--feed={feed}", "--json", "--max-turns=1", "--turn-interval=0.001"),
        compose=lambda args, *, export: agent_mod._compose_embody_seam(
            args,
            export=export,
            media=_media(),
            session_factory=_SessionFactory(),
            turn_fn=turn,
            synthesize={"tts": lambda text: b"\x00\x00"},
        ),
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["turns"] >= 1
    assert payload["events"] == 1
    assert payload["profile"] == "bench"


def test_an_unreadable_feed_is_a_clean_environment_error():
    """A typo'd feed path fails like ``attach``'s does — never a traceback."""
    args = _parse("--feed=/nonexistent/runtime.feed")
    with pytest.raises(CliError) as excinfo:
        agent_mod.cmd_agent_embody(args)
    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "behavior engine run --export -" in excinfo.value.remediation


def test_an_unknown_media_profile_is_a_clean_user_error():
    from reachy.cli._errors import EXIT_USER_ERROR

    args = _parse("--media-profile=submarine")
    # `iter(())` is hoisted out of the block so the ONLY call inside it is the
    # one under test — otherwise the assertion could be satisfied by the wrong
    # invocation raising (Sonar S5778).
    empty_lines = iter(())
    with pytest.raises(CliError) as excinfo:
        agent_mod._compose_embody_seam(args, export=None, lines=empty_lines)
    assert excinfo.value.code == EXIT_USER_ERROR


def test_the_overview_names_embody_in_text_and_json(capsys):
    import argparse as _argparse

    agent_mod.cmd_agent_overview(_argparse.Namespace(json=False))
    assert "agent embody" in capsys.readouterr().out

    agent_mod.cmd_agent_overview(_argparse.Namespace(json=True))
    payload = json.loads(capsys.readouterr().out)
    blob = json.dumps(payload)
    assert "embody" in blob


def test_the_explain_catalog_resolves_the_embody_verb():
    assert ("agent", "embody") in ENTRIES
    body = ENTRIES[("agent", "embody")]
    assert "embody" in body
    assert "reachy-mini-cli agent embody" in body


# --------------------------------------------------------------------------- #
# Operand order — issue #147                                                   #
# --------------------------------------------------------------------------- #


def test_embody_operating_flags_survive_being_written_before_the_subcommand() -> None:
    """``embody --feed X start`` must reach the child, not be reset to a default.

    These flags are declared on the parent verb AND on ``start``/``restart``,
    because the bare verb is itself the foreground loop. Argparse applies a
    sub-parser's defaults over values the parent already parsed, so before this
    was fixed the spawned layer got ``--feed -``, read ``/dev/null`` as a
    detached process, and exited having connected the tee and armed a realtime
    session — every log line reading as success.
    """
    from reachy.cli import _build_parser

    parser = _build_parser()
    for verb in ("start", "restart"):
        before = parser.parse_args(
            ["agent", "embody", "--feed", "/tmp/f.fifo", "--media-profile", "robot", verb]
        )
        after = parser.parse_args(
            ["agent", "embody", verb, "--feed", "/tmp/f.fifo", "--media-profile", "robot"]
        )
        assert before.feed == after.feed == "/tmp/f.fifo", f"{verb}: --feed did not survive"
        assert before.media_profile == after.media_profile == "robot"


def test_embody_subcommand_flags_still_win_over_the_parents() -> None:
    """Inheriting the parent's value must not make the sub-parser's flag inert."""
    from reachy.cli import _build_parser

    args = _build_parser().parse_args(
        ["agent", "embody", "--feed", "/tmp/parent", "start", "--feed", "/tmp/child"]
    )
    assert args.feed == "/tmp/child"


def test_embody_operating_defaults_still_apply_when_nothing_is_given() -> None:
    """SUPPRESS on the sub-parser must not leave the dest missing entirely."""
    from reachy.cli import _build_parser

    args = _build_parser().parse_args(["agent", "embody", "start"])
    assert args.feed == "-"
    assert args.media_profile is None
    assert args.mute_during_playback is False
