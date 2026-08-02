"""Offline tests for :mod:`reachy.speech.realtime_duplex` (embodiment-layer, task t9).

Every behavioural test drives the REAL
:class:`~reachy.speech.realtime_duplex.RealtimeDuplexSession` over a real
loopback socket against :class:`tests.fake_realtime_server.FakeRealtimeServer`
— no mock of the wire, no live gateway, no new dependency. The sibling suite
``tests/test_realtime_client.py`` is the style model; two of its harness
properties carry over verbatim and shape how these tests are written:

* the scripted server sends its whole sequence **and a graceful CLOSE** within
  a millisecond of the handshake, so ``connected`` is a window too short to
  poll — every wait keys on a CUMULATIVE counter;
* a scenario that must keep a session OPEN is ``CLOSE_MID_STREAM`` with a
  frame target it can never reach (the idiom that file established).

Three acceptance criteria are pinned here, and each names its tests:

1. **duplex over ONE socket** — appends in, utterances AND response audio out
   (``Scenario.DUPLEX_HAPPY_PATH``), with the send surface pinned to the three
   legal frame kinds both behaviourally and by AST scan (h13).
2. **ungated by construction** — no engagement/name-match import, directly or
   transitively, and not in ``sys.modules`` after a fresh import (c4); every
   failure is a named drop plus a backoff reconnect, never a raise.
3. **the mute seam exists and defaults OFF** — the AEC decision, flippable by
   configuration alone.

A fourth section was added by the foreground-Gemma arc's task t6 (spec claims
c6/c12, honesty h5/h10): **chunked, cancellable playback** — response audio
spoken as chunk groups as the deltas arrive, a skip-remaining cancel that
empties the queue within one chunk boundary, playback that never runs on the
session pump, and the pre-t6 guarantee that a reply cut before any chunk
played is never spoken. It leans on the fake server's
``RESPONSE_HOLD_BEFORE_DONE`` scenario, which withholds ``response.done`` (so
"played before done" is expressible at all) and PINGs while it holds (so
"a wedged mouth starves no keepalive" is expressible at all).
"""

from __future__ import annotations

import ast
import base64
import collections
import dataclasses
import inspect
import logging
import socket
import subprocess  # nosec B404 — fixed argv, sys.executable, never shell=True
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from reachy.speech import realtime
from reachy.speech import realtime_duplex as duplex
from reachy.speech import realtime_wire as wire
from reachy.speech.realtime_duplex import (
    DEFAULT_OUTPUT_SAMPLE_RATE,
    REASON_HANDSHAKE_REFUSED,
    REASON_LANE_UNAVAILABLE,
    REASON_MALFORMED_AUDIO_DELTA,
    REASON_MALFORMED_EVENT,
    REASON_NO_PLAYBACK_SINK,
    REASON_PLAYBACK_FAILED,
    REASON_RESPONSE_INTERRUPTED,
    REASON_SELF_MUTE,
    REASON_SESSION_DOWN,
    REASON_SOURCE_FAILED,
    REASON_STREAM_CLOSED,
    REASON_STT_FORWARD_FAILED,
    REASON_VAD_UNAVAILABLE,
    RealtimeDuplexSession,
    Response,
)
from tests.fake_realtime_server import (
    DEFAULT_RESPONSE_AUDIO,
    DEFAULT_RESPONSE_TEXT,
    DEFAULT_TRANSCRIPT,
    FakeRealtimeServer,
    Scenario,
)

_TIMEOUT = 5.0
_RATE = 16000
_MODULE_PATH = Path(duplex.__file__)
_REPO_ROOT = _MODULE_PATH.resolve().parent.parent.parent
_MODULE_DOTTED = "reachy.speech.realtime_duplex"

#: Every file this session's SEND path spans. Since the duplication cleanup the
#: wire mechanics (handshake, frame pump, PONG, CLOSE) have ONE owner —
#: ``reachy/speech/realtime.py``'s "Shared session mechanics" section — and this
#: module composes them, so an h13 scan of this file alone would be blind to
#: half the frames that actually leave. Anything reachable on the send path is
#: in this tuple, or the pins below are lying.
_SEND_PATH_MODULES = (_MODULE_PATH, Path(realtime.__file__))

_ENV_VARS = (
    duplex.REALTIME_URL_ENV,
    duplex.REALTIME_API_KEY_ENV,
    duplex.OPENAI_URL_BASE_ENV,
    duplex.OPENAI_API_KEY_ENV,
)


@pytest.fixture(autouse=True)
def _clean_realtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this file may inherit a developer's realtime/gateway config."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _wait_until(predicate, timeout: float = _TIMEOUT, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class _Source:
    """A pull source under the test's control.

    It hands over exactly what the test has ``offer``ed and ``None`` otherwise,
    which is the contract ``read_audio`` declares ("nothing this call" is
    silence, never a fault) and also what makes the connect-time stale drain
    observable: chunks offered BEFORE the session comes up are the standing
    backlog, chunks offered after it are live audio.
    """

    def __init__(self, *, on_read=None) -> None:
        self._pending: collections.deque = collections.deque()
        self.reads = 0
        self._on_read = on_read

    def offer(self, chunk) -> None:
        self._pending.append(chunk)

    def __call__(self):
        self.reads += 1
        if self._on_read is not None:
            self._on_read()
        try:
            return self._pending.popleft()
        except IndexError:
            return None


class _Sink:
    """Records every ``play(pcm16, samplerate=...)`` call, optionally blocking.

    ``threads`` records which thread each call ran on, because "playback never
    runs on the session pump" is a pinned property, not a convention (the robot
    sink's daemon-HTTP route is a seconds-long round trip).
    """

    def __init__(self, *, block: threading.Event | None = None, boom: bool = False) -> None:
        self.calls: list[tuple[bytes, int]] = []
        self.threads: list[str] = []
        self.entered = threading.Event()
        self._block = block
        self._boom = boom

    def __call__(self, pcm16_bytes: bytes, *, samplerate: int) -> None:
        self.threads.append(threading.current_thread().name)
        self.entered.set()
        if self._boom:
            raise RuntimeError("sink fault")
        if self._block is not None:
            self._block.wait(timeout=_TIMEOUT)
        self.calls.append((pcm16_bytes, samplerate))

    @property
    def played(self) -> bytes:
        return b"".join(pcm for pcm, _rate in self.calls)


#: :class:`~reachy.speech.realtime_duplex.Limits` field names (issue #141/
#: S107) — lets this module's tests keep passing flat bounds
#: (``backoff_initial_s=5.0``) while the constructor itself now takes only
#: ``limits=``.
_LIMIT_FIELDS = {field.name for field in dataclasses.fields(duplex.Limits)}


def _session(server: FakeRealtimeServer | None = None, **kwargs) -> RealtimeDuplexSession:
    """A CONSTRUCTED (not started) session, pointed at *server* unless given a url."""
    if server is not None:
        kwargs.setdefault("url", server.url)
    kwargs.setdefault("sample_rate", _RATE)
    kwargs.setdefault("read_audio", lambda: None)
    kwargs.setdefault("backoff_initial_s", 0.02)
    kwargs.setdefault("backoff_max_s", 0.05)
    limit_kwargs = {name: kwargs.pop(name) for name in list(kwargs) if name in _LIMIT_FIELDS}
    if limit_kwargs:
        kwargs.setdefault("limits", duplex.Limits(**limit_kwargs))
    return RealtimeDuplexSession(**kwargs)


def _established(client: RealtimeDuplexSession, sessions: int = 1) -> bool:
    return _wait_until(lambda: client.sessions >= sessions)


def _chunk(samples: int = 160) -> np.ndarray:
    """A deterministic float32 mono chunk in [-1, 1] (a quarter-amplitude ramp)."""
    return (np.arange(samples, dtype=np.float32) % 8 - 4.0) / 16.0


def _expected_bytes(chunk: np.ndarray) -> bytes:
    return (np.clip(chunk, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def _count_reason(caplog: pytest.LogCaptureFixture, reason: str) -> int:
    needle = f"reason={reason}"
    return sum(1 for message in _messages(caplog) if needle in message)


def _dead_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _dead_url() -> str:
    return f"ws://127.0.0.1:{_dead_port()}/v1/realtime"


def _sent_event_types(server: FakeRealtimeServer) -> list[str]:
    """Every event type the CLIENT sent, in arrival order."""
    types: list[str] = []
    for opcode, payload in server.received_frames:
        if opcode != wire.OPCODE_TEXT:
            continue
        event = wire.decode_event(payload)
        if event is not None:
            types.append(event["type"])
    return types


def _delta_event(response_id: str, pcm: bytes) -> dict:
    return {
        "type": "response.audio.delta",
        "response_id": response_id,
        "delta": base64.b64encode(pcm).decode("ascii"),
    }


def _feed_response(client: RealtimeDuplexSession, response_id: str, pcm: bytes) -> None:
    """Drive one complete response through the dispatcher.

    Used only by the tests that need a session to stay OPEN while the mouth is
    busy: every scripted ``response_*`` scenario closes the socket immediately
    after ``response.done``, so a real one cannot coexist with a held-open
    session. Same escape hatch ``tests/test_realtime_client.py`` uses for the
    branches its ears-only harness cannot script.
    """
    client._dispatch_event({"type": "response.created", "response_id": response_id})
    client._dispatch_event(_delta_event(response_id, pcm))
    client._dispatch_event({"type": "response.done", "response_id": response_id})


@pytest.fixture
def sense_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="reachy.sense")
    return caplog


# --------------------------------------------------------------------------- #
# criterion 1 — ONE socket carries appends in, utterances AND audio out        #
# --------------------------------------------------------------------------- #


def test_one_socket_carries_appends_in_and_both_utterances_and_audio_out() -> None:
    """The whole of criterion 1 in one exchange, over ONE connection.

    The chunk is offered BEFORE the session starts, with the connect-time
    backlog drain disabled: this scenario scripts its whole sequence and closes
    within milliseconds of being armed, so audio offered afterwards would race
    that close. Pre-offering makes the append the first thing the worker sends
    on its first pump — deterministic on any loaded box.
    """
    source = _Source()
    sink = _Sink()
    source.offer(_chunk())
    with FakeRealtimeServer(Scenario.DUPLEX_HAPPY_PATH, wait_timeout=_TIMEOUT) as server:
        client = _session(
            server,
            read_audio=source,
            play=sink,
            stale_drain_max_chunks=0,
            backoff_initial_s=5.0,
            backoff_max_s=5.0,
        )
        client.start()
        try:
            assert _wait_until(lambda: client.utterances >= 1)
            assert _wait_until(lambda: client.responses >= 1)
            assert _wait_until(lambda: len(server.append_payloads) >= 1)
            assert _wait_until(lambda: bool(sink.calls))
        finally:
            client.close()

    # ONE socket for all of it.
    assert server.connections_accepted == 1

    utterance = client.take_utterance()
    assert utterance is not None
    assert utterance.text == DEFAULT_TRANSCRIPT

    response = client.take_response()
    assert response is not None
    assert response.text == DEFAULT_RESPONSE_TEXT
    assert response.audio == DEFAULT_RESPONSE_AUDIO
    assert response.interrupted is False

    assert server.append_payloads == [_expected_bytes(_chunk())]
    assert sink.played == DEFAULT_RESPONSE_AUDIO
    assert sink.calls[0][1] == DEFAULT_OUTPUT_SAMPLE_RATE


def test_response_audio_deltas_reassemble_contiguously_and_in_order() -> None:
    """Three base64 deltas, one contiguous PCM16 payload — order is the assertion."""
    audio = bytes(range(48))
    sink = _Sink()
    with FakeRealtimeServer(
        Scenario.RESPONSE_HAPPY_PATH,
        response_audio=audio,
        response_chunk_bytes=8,
        wait_timeout=_TIMEOUT,
    ) as server:
        client = _session(server, play=sink, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: bool(sink.calls))
        finally:
            client.close()
    assert sink.played == audio
    assert client.response_audio_bytes == len(audio)
    assert (
        len([event for event in server.sent_events if event["type"] == "response.audio.delta"]) == 6
    )


def test_the_session_arms_itself_with_exactly_one_response_create() -> None:
    with FakeRealtimeServer(Scenario.RESPONSE_HAPPY_PATH, wait_timeout=_TIMEOUT) as server:
        client = _session(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 1)
        finally:
            client.close()
    assert server.response_create_count == 1
    assert client.arms_sent == 1


def test_an_unarmed_session_is_ears_only_and_never_asks_for_a_reply() -> None:
    """``arm_on_connect=False`` degrades this client to the runtime's own shape."""
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _session(server, arm_on_connect=False, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: client.utterances >= 1)
        finally:
            client.close()
    assert server.response_create_count == 0
    assert client.arms_sent == 0
    assert _sent_event_types(server) == [] or "response.create" not in _sent_event_types(server)


def test_arm_can_be_requested_again_from_the_caller_thread() -> None:
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server)
        client.start()
        try:
            assert _established(client)
            assert _wait_until(lambda: client.arms_sent >= 1)
            client.arm()
            assert _wait_until(lambda: client.arms_sent >= 2)
            assert _wait_until(lambda: server.response_create_count >= 2)
        finally:
            client.close()


# --------------------------------------------------------------------------- #
# h13 — the send surface is CLOSED                                             #
# --------------------------------------------------------------------------- #


def test_h13_only_append_and_response_create_frames_ever_reach_the_server() -> None:
    """Behavioural half: what actually went out over a full duplex exchange."""
    source = _Source()
    source.offer(_chunk())  # pre-offered — see the criterion-1 test's docstring
    with FakeRealtimeServer(Scenario.DUPLEX_HAPPY_PATH, wait_timeout=_TIMEOUT) as server:
        client = _session(
            server,
            read_audio=source,
            stale_drain_max_chunks=0,
            backoff_initial_s=5.0,
            backoff_max_s=5.0,
        )
        client.start()
        try:
            assert _wait_until(lambda: len(server.append_payloads) >= 1)
            assert _wait_until(lambda: client.responses >= 1)
        finally:
            client.close()

    types = set(_sent_event_types(server))
    assert types == {wire.APPEND_EVENT_TYPE, wire.RESPONSE_CREATE_EVENT_TYPE}
    assert wire.OPCODE_BINARY not in server.received_opcodes
    assert server.malformed_append_count == 0


def _resolve_constant(node: ast.AST) -> str | None:
    """Resolve an AST value to the string it denotes, via the two modules' globals."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        value = vars(duplex).get(node.id)
        return value if isinstance(value, str) else None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        holder = {"wire": wire, "duplex": duplex}.get(node.value.id)
        value = getattr(holder, node.attr, None) if holder is not None else None
        return value if isinstance(value, str) else None
    return None


def _callee(node: ast.Call) -> str | None:
    """``wire.build_append_event(...)`` -> ``"build_append_event"``."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def test_h13_the_modules_outbound_event_family_is_exactly_the_two_legal_kinds() -> None:
    """AST half (h13): every outbound EVENT this session can construct.

    Stronger than calling the two known senders: it walks the source for every
    way an event can come into being — a call to one of the wire's ``build_*``
    event builders, or a hand-written dict literal carrying a ``"type"`` key —
    and asserts the resulting set is exactly ``input_audio_buffer.append`` plus
    ``response.create``. A THIRD sender added later under any name fails this
    immediately; an unrecognised ``build_*_event`` callee fails it too, rather
    than being silently ignored. Session config is not in the set on purpose:
    it rides the connect URL's query params, never a frame.

    It scans BOTH source files, because this session's send path spans both:
    the shared wire mechanics live in ``reachy/speech/realtime.py`` (its
    "Shared session mechanics" section) and this module composes them. Scanning
    only this file would have left the shared owner free to grow a third
    sender unseen. The sibling pin — that the shared owner can build NOTHING
    but ``append``, so the ears-only client cannot arm — lives in
    ``tests/test_realtime_client.py``.
    """
    builders = {
        "build_append_event": wire.APPEND_EVENT_TYPE,
        "build_response_create_event": wire.RESPONSE_CREATE_EVENT_TYPE,
    }
    found: set[str] = set()
    for path in _SEND_PATH_MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = _callee(node)
                if name in builders:
                    found.add(builders[name])
                elif name is not None and name.startswith("build_") and name.endswith("_event"):
                    pytest.fail(f"unrecognised outbound event builder in {path.name}: {name}")
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "type":
                        resolved = _resolve_constant(value)
                        if resolved is not None:
                            found.add(resolved)
    assert found == {wire.APPEND_EVENT_TYPE, wire.RESPONSE_CREATE_EVENT_TYPE}


def test_h13_the_module_serialises_no_event_of_its_own() -> None:
    """The AST pin above can only see what it can resolve, so close the hole:
    neither this module nor the shared owner imports ``json``, so neither can
    hand-roll an event payload past that scan — every outbound payload comes
    from the wire module."""
    for path in _SEND_PATH_MODULES:
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert "json" not in imported, f"{path.name} can serialise an event of its own"


#: Every function that puts an opcode on the wire, and WHICH positional
#: argument carries it. ``_send`` is each client's funnel, ``_ws_send`` is the
#: shared one it forwards to, and ``build_frame`` is the wire's own encoder.
_SEND_OPCODE_ARG = {"_send": 0, "_ws_send": 1, "build_frame": 0}


def test_h13_every_frame_the_module_can_send_is_text_pong_or_close() -> None:
    """No BINARY frame exists anywhere in the send path (audio is base64 TEXT).

    Scans the opcode argument of every send call site (see
    :data:`_SEND_OPCODE_ARG`) rather than every ``OPCODE_*`` the source merely
    *mentions* — the pump compares against ``OPCODE_PING`` when handling an
    inbound keepalive, which is a read, not a write. Every such argument must
    be a literal ``wire.OPCODE_*``; the ONE permitted indirection is a funnel
    (``_send`` / ``_ws_send``) forwarding its own ``opcode`` parameter, which is
    what makes them the funnels every other call site has to pass through.

    Both files are scanned, for the reason the event-family pin above states:
    since the duplication cleanup, the PONG and CLOSE frames this session sends
    are emitted by the shared mechanics in ``reachy/speech/realtime.py``. The
    union must still be exactly the three legal kinds.
    """
    opcodes: set[int] = set()
    for path in _SEND_PATH_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            for node in ast.walk(func):
                index = (
                    _SEND_OPCODE_ARG.get(_callee(node) or "")
                    if isinstance(node, ast.Call)
                    else None
                )
                if index is None:
                    continue
                assert len(node.args) > index, "a send call must name its opcode positionally"
                arg = node.args[index]
                if func.name in _SEND_OPCODE_ARG and isinstance(arg, ast.Name):
                    assert arg.id == "opcode", f"opaque opcode forwarded by {func.name}: {arg.id}"
                    continue  # a funnel forwarding its own parameter
                assert isinstance(arg, ast.Attribute), f"opaque opcode argument: {ast.dump(arg)}"
                value = getattr(wire, arg.attr, None)
                assert isinstance(value, int), f"unknown opcode constant: {arg.attr}"
                opcodes.add(value)
    assert opcodes == {wire.OPCODE_TEXT, wire.OPCODE_PONG, wire.OPCODE_CLOSE}


# --------------------------------------------------------------------------- #
# criterion 2a — UNGATED by construction (c4)                                  #
# --------------------------------------------------------------------------- #

_GATE_MODULES = ("reachy.speech.engagement", "reachy.speech.name_match")


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(_REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_names(path: Path, dotted: str) -> set[str]:
    """Every dotted name *path* imports, in any syntactic form (the technique
    ``tests/test_zero_llm_boundary.py`` establishes: AST, never grep, and
    function-local / ``TYPE_CHECKING`` imports count)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = dotted.split(".")[: -node.level] or []
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if not module:
                continue
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def _closure(start: str) -> set[str]:
    """The static import closure of *start* inside the ``reachy`` package."""
    modules = {_module_name(p): p for p in sorted((_REPO_ROOT / "reachy").rglob("*.py"))}
    assert start in modules
    seen = {start}
    queue = collections.deque([start])
    while queue:
        current = queue.popleft()
        for dep in _imported_names(modules[current], current):
            if not dep.startswith("reachy"):
                continue
            candidate = dep
            while candidate and candidate not in modules:
                candidate = candidate.rsplit(".", 1)[0] if "." in candidate else ""
            if candidate and candidate not in seen:
                seen.add(candidate)
                queue.append(candidate)
    return seen


def test_c4_the_module_imports_no_engagement_or_name_match_gate() -> None:
    """The layer hears ALL speech: the runtime's admission gate is not here."""
    imported = _imported_names(_MODULE_PATH, _MODULE_DOTTED)
    for gate in _GATE_MODULES:
        assert not any(name == gate or name.startswith(gate + ".") for name in imported)


def test_c4_no_gate_is_reachable_from_the_modules_whole_import_closure() -> None:
    """Transitively too — a gate one hop away would gate the layer just as well."""
    closure = _closure(_MODULE_DOTTED)
    assert closure & set(_GATE_MODULES) == set()
    # Vacuity guard: the closure really was computed, not silently empty.
    assert "reachy.speech.realtime_wire" in closure


def test_c4_a_fresh_import_loads_no_gate_and_no_language_model() -> None:
    """Runtime proof, in a subprocess (never evict modules in-process — the
    lesson ``tests/test_sleep_boundary.py`` records)."""
    forbidden = (*_GATE_MODULES, "reachy.speech.llm")
    code = (
        f"import sys, {_MODULE_DOTTED};"
        f"print([name for name in {forbidden!r} if name in sys.modules])"
    )
    proc = subprocess.run(  # nosec B603 — fixed argv, sys.executable, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    assert proc.stdout.strip() == "[]"


def test_c4_an_utterance_the_runtime_gate_would_drop_still_reaches_the_caller() -> None:
    """Ambient human-to-human chatter, no robot name, no warm conversation —
    the exact shape ``reachy/speech/engagement.py`` drops as ``not-addressed``.
    The layer surfaces it."""
    ambient = "could you pass me the salt please"
    with FakeRealtimeServer(Scenario.HAPPY_PATH, transcript=ambient) as server:
        client = _session(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: client.utterances >= 1)
        finally:
            client.close()
    utterance = client.take_utterance()
    assert utterance is not None
    assert utterance.text == ambient


def test_the_module_never_imports_reachy_mini() -> None:
    """h14 support: the layer's whole closure constructs no SDK client."""
    for name in _closure(_MODULE_DOTTED):
        source = (_REPO_ROOT / Path(*name.split("."))).with_suffix(".py")
        if not source.exists():
            source = _REPO_ROOT / Path(*name.split(".")) / "__init__.py"
        assert "reachy_mini" not in {
            dep.split(".")[0] for dep in _imported_names(source, name)
        }, f"{name} imports reachy_mini"


# --------------------------------------------------------------------------- #
# criterion 2b — every failure is a NAMED drop, never a raise                  #
# --------------------------------------------------------------------------- #


def test_a_refused_handshake_is_a_named_drop_and_never_raises(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    with FakeRealtimeServer(Scenario.UNAUTHORIZED) as server:
        client = _session(server)
        client.start()
        try:
            assert _wait_until(lambda: client.connect_failures >= 2)
            # The caller's thread keeps working throughout: no raise, no block.
            client.arm()
            assert client.take_utterance() is None
            assert client.take_response() is None
        finally:
            client.close()

    assert _count_reason(sense_log, REASON_HANDSHAKE_REFUSED) == 1
    assert any("401" in message for message in _messages(sense_log))
    assert _count_reason(sense_log, REASON_SESSION_DOWN) == 1
    assert server.refusals[0] == (401, "unauthorized")


def test_a_404_handshake_is_the_named_lane_unavailable_drop_not_a_generic_refusal(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """A 404 on ``/v1/realtime`` means the gateway's stt lane is declared off —
    an operator fix, not a transient outage. It gets its own reason, keeps
    reconnecting on the same backoff (the lane can be switched on under us),
    and stays latched to one line."""
    with FakeRealtimeServer(Scenario.ROLE_INFEASIBLE) as server:
        client = _session(server)
        client.start()
        try:
            assert _wait_until(lambda: client.connect_failures >= 3)
        finally:
            client.close()

    assert _count_reason(sense_log, REASON_LANE_UNAVAILABLE) == 1
    assert _count_reason(sense_log, REASON_HANDSHAKE_REFUSED) == 0
    assert _count_reason(sense_log, REASON_SESSION_DOWN) == 1
    assert any("capabilities" in message for message in _messages(sense_log))
    assert server.refusals[0] == (404, "role_infeasible")
    assert client.lane_unavailable is True


def test_a_mid_stream_close_is_a_named_drop_and_a_backoff_reconnect_follows(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    source = _Source()
    with FakeRealtimeServer(Scenario.CLOSE_MID_STREAM, close_after_frames=1) as server:
        client = _session(server, read_audio=source)
        client.start()
        try:
            deadline = time.monotonic() + _TIMEOUT
            while time.monotonic() < deadline and server.connections_accepted < 2:
                source.offer(_chunk())
                time.sleep(0.02)
            assert server.connections_accepted >= 2
        finally:
            client.close()

    assert _count_reason(sense_log, REASON_STREAM_CLOSED) >= 1
    assert _count_reason(sense_log, REASON_SESSION_DOWN) >= 1


def test_a_malformed_json_event_is_a_named_drop_and_never_raises(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    with FakeRealtimeServer(Scenario.MALFORMED_JSON) as server:
        client = _session(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: _count_reason(sense_log, REASON_MALFORMED_EVENT) >= 1)
        finally:
            client.close()
    assert server.connections_accepted >= 1


def test_vad_unavailable_and_stt_forward_failed_stay_distinct_named_drops(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    with FakeRealtimeServer(Scenario.ERROR_VAD_UNAVAILABLE) as server:
        client = _session(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: _count_reason(sense_log, REASON_VAD_UNAVAILABLE) >= 1)
        finally:
            client.close()
    assert _count_reason(sense_log, REASON_STT_FORWARD_FAILED) == 0

    with FakeRealtimeServer(Scenario.ERROR_STT_FORWARD_FAILED) as server:
        client = _session(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: _count_reason(sense_log, REASON_STT_FORWARD_FAILED) >= 1)
        finally:
            client.close()


def test_a_malformed_audio_delta_is_a_named_drop_and_nothing_is_played(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    sink = _Sink()
    with FakeRealtimeServer(
        Scenario.RESPONSE_AUDIO_DELTA_MALFORMED, wait_timeout=_TIMEOUT
    ) as server:
        client = _session(server, play=sink, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: _count_reason(sense_log, REASON_MALFORMED_AUDIO_DELTA) >= 1)
        finally:
            client.close()
    assert sink.calls == []
    assert server.connections_accepted >= 1


def test_an_interrupted_response_is_named_and_never_spoken_over_the_speaker(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """A barge-in means the human is talking again: playing the truncated reply
    afterwards would talk over them. The text still reaches the caller."""
    sink = _Sink()
    with FakeRealtimeServer(Scenario.RESPONSE_INTERRUPTED, wait_timeout=_TIMEOUT) as server:
        client = _session(server, play=sink, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 1)
        finally:
            client.close()
    assert _count_reason(sense_log, REASON_RESPONSE_INTERRUPTED) == 1
    assert sink.calls == []
    response = client.take_response()
    assert response is not None
    assert response.interrupted is True
    assert response.text == DEFAULT_RESPONSE_TEXT


def test_a_raising_audio_source_is_a_named_drop_and_the_session_survives(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    state = {"boom": True}

    def _read():
        if state["boom"]:
            raise RuntimeError("source fault")
        return None

    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server, read_audio=_read)
        client.start()
        try:
            assert _established(client)
            assert _wait_until(lambda: _count_reason(sense_log, REASON_SOURCE_FAILED) >= 1)
            state["boom"] = False
            assert client.connected is True
        finally:
            client.close()
    # LATCHED: a permanently broken source costs one line, not one per read.
    assert _count_reason(sense_log, REASON_SOURCE_FAILED) == 1


def test_a_raising_playback_sink_is_a_named_drop_and_the_session_survives(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    sink = _Sink(boom=True)
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server, play=sink)
        client.start()
        try:
            assert _established(client)
            _feed_response(client, "resp_boom", b"\x01\x02")
            assert _wait_until(lambda: _count_reason(sense_log, REASON_PLAYBACK_FAILED) >= 1)
            assert client.connected is True
        finally:
            client.close()
    assert client.take_response() is not None
    # A chunk the sink threw away is never counted as heard: the measurement
    # is what the sink CONFIRMED, which is what makes it usable as truth.
    assert client.played_bytes == 0
    progress = client.playback_progress("resp_boom")
    assert progress is not None
    assert (progress.played_bytes, progress.skipped_bytes) == (0, 2)


def test_no_playback_sink_at_all_is_a_named_drop_not_a_crash(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    client = _session(url=_dead_url(), play=None)
    try:
        _feed_response(client, "resp_mute", b"\x01\x02")
        assert _wait_until(lambda: _count_reason(sense_log, REASON_NO_PLAYBACK_SINK) >= 1)
    finally:
        client.close()
    response = client.take_response()
    assert response is not None
    assert response.audio == b"\x01\x02"


def test_session_down_logs_once_however_many_attempts_fail(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    client = _session(url=_dead_url(), backoff_initial_s=0.01, backoff_max_s=0.02)
    client.start()
    try:
        assert _wait_until(lambda: client.connect_failures >= 4)
        assert client.session_down is True
        assert _count_reason(sense_log, REASON_SESSION_DOWN) == 1
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# criterion 3 — the mute seam exists, and defaults OFF (the AEC decision)      #
# --------------------------------------------------------------------------- #


def test_the_mute_seam_is_off_by_default_in_the_constructor_signature() -> None:
    """Pinned as a SIGNATURE default, because the shipped default IS the
    decision: Reachy has hardware AEC, so the layer hears while it speaks and
    barge-in stays possible. Flipping it is configuration, not a code change."""
    parameter = inspect.signature(RealtimeDuplexSession).parameters["mute_during_playback"]
    assert parameter.default is False
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_by_default_the_layer_keeps_hearing_while_its_mouth_is_busy() -> None:
    release = threading.Event()
    sink = _Sink(block=release)
    source = _Source()
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server, read_audio=source, play=sink)
        client.start()
        try:
            assert _established(client)
            _feed_response(client, "resp_1", b"\x01\x02")
            assert sink.entered.wait(timeout=_TIMEOUT)  # the mouth is BUSY
            assert client.speaking is True
            source.offer(_chunk())  # audio captured DURING playback
            assert _wait_until(lambda: len(server.append_payloads) >= 1)
        finally:
            release.set()
            client.close()
    assert client.muted_chunks == 0
    assert server.append_payloads == [_expected_bytes(_chunk())]


def test_mute_during_playback_withholds_audio_while_the_mouth_is_busy(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()
    sink = _Sink(block=release)
    source = _Source()
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server, read_audio=source, play=sink, mute_during_playback=True)
        client.start()
        try:
            assert _established(client)
            _feed_response(client, "resp_1", b"\x01\x02")
            assert sink.entered.wait(timeout=_TIMEOUT)
            source.offer(_chunk())
            assert _wait_until(lambda: client.muted_chunks >= 1)
            # Withheld, not merely delayed: nothing reached the wire.
            assert server.append_payloads == []
            release.set()
            # ... and hearing resumes by itself once the mouth is idle.
            assert _wait_until(lambda: client.speaking is False)
            source.offer(_chunk())
            assert _wait_until(lambda: len(server.append_payloads) >= 1)
        finally:
            release.set()
            client.close()
    assert _count_reason(sense_log, REASON_SELF_MUTE) == 1  # latched per episode


# --------------------------------------------------------------------------- #
# t6 — chunked, cancellable playback (spec c6/c12, honesty h5/h10)             #
# --------------------------------------------------------------------------- #
#
# Three acceptance criteria, each named by its tests below:
#
# 1. response audio plays as CHUNK GROUPS as the deltas arrive (not
#    accumulate-then-play), and a skip-remaining cancel empties the queue
#    within one chunk boundary;
# 2. the session pump and the keepalive are never starved by playback — ``play``
#    stays on its dedicated thread;
# 3. a reply cancelled BEFORE any chunk played is still never spoken and never
#    recorded as spoken (the pre-t6 behaviour, preserved exactly).
#
# The chunk size is a ``Limits`` bound, so every test here picks a tiny one
# (8 bytes = 4 PCM16 samples) and drives the fake server's matching 8-byte
# deltas: the SHIPPED default is a second of speech, which no offline test
# should have to synthesize.

_HOLD = Scenario.RESPONSE_HOLD_BEFORE_DONE
_TEST_CHUNK_BYTES = 8
_LONG_REPLY = bytes(range(48))  # six 8-byte chunks


def _chunked(server: FakeRealtimeServer | None = None, **kwargs) -> RealtimeDuplexSession:
    """A session whose playback chunk is one fake-server delta."""
    kwargs.setdefault("playback_chunk_bytes", _TEST_CHUNK_BYTES)
    kwargs.setdefault("playback_first_chunk_bytes", _TEST_CHUNK_BYTES)
    kwargs.setdefault("backoff_initial_s", 5.0)
    kwargs.setdefault("backoff_max_s", 5.0)
    return _session(server, **kwargs)


def _hold_server(**kwargs) -> FakeRealtimeServer:
    kwargs.setdefault("response_audio", _LONG_REPLY)
    kwargs.setdefault("response_chunk_bytes", _TEST_CHUNK_BYTES)
    kwargs.setdefault("wait_timeout", _TIMEOUT)
    return FakeRealtimeServer(_HOLD, **kwargs)


def _feed_deltas(client: RealtimeDuplexSession, response_id: str, audio: bytes, step: int) -> None:
    """Dispatch ``response.created`` + one delta per *step* bytes, nothing more.

    The direct-dispatch escape hatch ``_feed_response`` already documents: a
    scripted scenario cannot hold a socket open across an arbitrary test-driven
    cancel, and these tests need to interleave a cancel with the delta stream.
    """
    client._dispatch_event({"type": "response.created", "response_id": response_id})
    for start in range(0, len(audio), step):
        client._dispatch_event(_delta_event(response_id, audio[start : start + step]))


def test_response_audio_plays_as_chunk_groups_while_the_reply_is_still_arriving() -> None:
    """Criterion 1a: spoken as the deltas arrive, NOT accumulated until done.

    The hold scenario withholds ``response.done``, so a client that still
    accumulated the whole reply would have spoken nothing at all here.
    """
    sink = _Sink()
    with _hold_server() as server:
        client = _chunked(server, play=sink)
        client.start()
        try:
            assert _wait_until(lambda: len(sink.calls) >= 6)
            # ... and the reply record does not even exist yet.
            assert client.responses == 0
            assert "response.done" not in [event["type"] for event in server.sent_events]
        finally:
            server.release_response_done()
            client.close()
    assert sink.played == _LONG_REPLY
    assert [len(pcm) for pcm, _rate in sink.calls] == [_TEST_CHUNK_BYTES] * 6
    assert {rate for _pcm, rate in sink.calls} == {DEFAULT_OUTPUT_SAMPLE_RATE}


def test_a_sub_chunk_reply_is_still_spoken_whole_when_the_server_says_done() -> None:
    """The remainder is flushed at ``response.done``: chunking never eats a tail."""
    sink = _Sink()
    audio = bytes(range(20))  # deliberately not a multiple of the chunk size
    with FakeRealtimeServer(
        Scenario.RESPONSE_HAPPY_PATH,
        response_audio=audio,
        response_chunk_bytes=4,
        wait_timeout=_TIMEOUT,
    ) as server:
        client = _chunked(server, play=sink)
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 1)
            assert _wait_until(lambda: len(sink.played) == len(audio))
        finally:
            client.close()
    assert sink.played == audio
    assert [len(pcm) for pcm, _rate in sink.calls] == [8, 8, 4]


def test_a_skip_remaining_cancel_empties_the_playback_queue_within_one_chunk(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """Criterion 1b: ``cancel_playback()`` skips everything still queued.

    The sink blocks INSIDE the first chunk, so the cut lands with one chunk
    already committed to the speaker and five queued behind it. Exactly the
    committed one is heard — that is what "within one chunk boundary" means,
    and it is the whole reason chunk size is a tunable bound.
    """
    release = threading.Event()
    sink = _Sink(block=release)
    with _hold_server() as server:
        client = _chunked(server, play=sink)
        client.start()
        try:
            assert sink.entered.wait(timeout=_TIMEOUT)  # chunk 1 is in the speaker
            assert _wait_until(lambda: client.chunks_queued >= 6)
            cut = client.cancel_playback()
            assert client.chunks_cancelled == 5  # synchronous: the queue is empty NOW
        finally:
            release.set()
            server.release_response_done()
            client.close()

    assert sink.played == _LONG_REPLY[:_TEST_CHUNK_BYTES]
    assert cut.cancelled is True
    assert cut.skipped_bytes == 40
    # Measured at the sink: the boundary chunk was still in flight, so it is
    # reported as such rather than counted as heard (t7 estimates inside it).
    assert cut.played_bytes == 0
    assert cut.in_flight_bytes == _TEST_CHUNK_BYTES
    assert _count_reason(sense_log, duplex.REASON_PLAYBACK_CANCELLED) == 1


def test_a_cancel_also_refuses_the_chunks_of_that_reply_that_have_not_arrived_yet() -> None:
    """Skip-remaining outlives the queue: the cut reply stops FEEDING too.

    Otherwise "stop talking" would only skip what happened to be buffered and
    the robot would carry on the moment the next delta landed.
    """
    sink = _Sink()
    client = _chunked(url=_dead_url(), play=sink)
    client.start()
    try:
        _feed_deltas(client, "resp_cut", _LONG_REPLY[:8], _TEST_CHUNK_BYTES)
        assert _wait_until(lambda: len(sink.calls) == 1)
        client.cancel_playback()
        for start in range(8, len(_LONG_REPLY), _TEST_CHUNK_BYTES):
            client._dispatch_event(
                _delta_event("resp_cut", _LONG_REPLY[start : start + _TEST_CHUNK_BYTES])
            )
        client._dispatch_event({"type": "response.done", "response_id": "resp_cut"})
        assert _wait_until(lambda: client.responses >= 1)
    finally:
        client.close()

    assert sink.played == _LONG_REPLY[:8]
    assert client.chunks_queued == 1
    # The RECORD still carries every byte the server sent — what the room heard
    # is the measurement, not the record (the said/unsaid split t7 builds on).
    response = client.take_response()
    assert response is not None
    assert response.audio == _LONG_REPLY
    progress = client.playback_progress("resp_cut")
    assert progress is not None
    assert progress.played_bytes == 8
    assert progress.cancelled is True


def test_a_server_barge_in_mid_reply_skips_the_remainder_and_keeps_the_played_prefix(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """``response.interrupted`` is the server-side half of the same cut.

    Also the measurement t7 consumes: the cut reply's own progress record says
    what the room got (one chunk, confirmed by the sink) and what it never got
    (the other five), against a RECORD that still carries the whole reply.
    """
    release = threading.Event()
    sink = _Sink(block=release)
    client = _chunked(url=_dead_url(), play=sink)
    client.start()
    try:
        _feed_deltas(client, "resp_barge", _LONG_REPLY, _TEST_CHUNK_BYTES)
        assert sink.entered.wait(timeout=_TIMEOUT)
        assert _wait_until(lambda: client.chunks_queued >= 6)
        client._dispatch_event({"type": "response.interrupted", "response_id": "resp_barge"})
        assert client.chunks_cancelled == 5
        release.set()
        assert _wait_until(lambda: client.played >= 1)
    finally:
        release.set()
        client.close()

    assert sink.played == _LONG_REPLY[:_TEST_CHUNK_BYTES]
    assert _count_reason(sense_log, REASON_RESPONSE_INTERRUPTED) == 1
    response = client.take_response()
    assert response is not None
    assert response.interrupted is True
    assert response.audio == _LONG_REPLY
    progress = client.playback_progress("resp_barge")
    assert progress is not None
    assert progress.played_bytes == _TEST_CHUNK_BYTES
    assert progress.skipped_bytes == len(_LONG_REPLY) - _TEST_CHUNK_BYTES
    assert progress.in_flight_bytes == 0
    assert progress.cancelled is True


def test_a_reply_cut_before_any_chunk_played_is_never_spoken(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """Criterion 3: the pre-t6 behaviour, preserved exactly.

    At the SHIPPED chunk size a short reply never reaches a chunk boundary
    before the barge-in, so nothing was ever handed to the mouth: the reply is
    published with ``interrupted=True`` (which is what
    ``_commands/agent.py``'s ``_response_tap`` keys on to keep it out of the
    already-said record) and the speaker stays silent.
    """
    sink = _Sink()
    with FakeRealtimeServer(Scenario.RESPONSE_INTERRUPTED, wait_timeout=_TIMEOUT) as server:
        client = _session(server, play=sink, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 1)
        finally:
            client.close()

    assert sink.calls == []
    assert client.played == 0
    assert client.played_bytes == 0
    assert _count_reason(sense_log, REASON_RESPONSE_INTERRUPTED) == 1
    response = client.take_response()
    assert response is not None
    assert response.interrupted is True
    progress = client.playback_progress(response.response_id)
    assert progress is not None
    assert (progress.played_bytes, progress.queued_bytes, progress.in_flight_bytes) == (0, 0, 0)


def test_playback_runs_only_on_the_dedicated_mouth_thread() -> None:
    """Criterion 2a: ``play`` never runs on the session pump. Six chunks, six
    calls, one thread — and it is not the worker."""
    sink = _Sink()
    with _hold_server() as server:
        client = _chunked(server, play=sink)
        client.start()
        try:
            assert _wait_until(lambda: len(sink.calls) >= 6)
        finally:
            server.release_response_done()
            client.close()
    assert set(sink.threads) == {duplex.PLAYBACK_THREAD_NAME}
    assert duplex.WORKER_THREAD_NAME not in set(sink.threads)


def test_a_blocking_mouth_starves_neither_the_session_pump_nor_the_keepalive() -> None:
    """Criterion 2b: with the sink WEDGED inside a chunk, the session still
    answers the server's PINGs and still forwards fresh microphone audio.

    The hold scenario PINGs throughout precisely so this can key on a pong that
    arrives strictly AFTER the mouth blocked, rather than on a cumulative count
    that a fast client could have satisfied beforehand.
    """
    release = threading.Event()
    sink = _Sink(block=release)
    source = _Source()
    with _hold_server(hold_ping_interval_s=0.02) as server:
        client = _chunked(server, play=sink, read_audio=source, stale_drain_max_chunks=0)
        client.start()
        try:
            assert sink.entered.wait(timeout=_TIMEOUT)  # the mouth is WEDGED
            pongs = server.pong_count
            assert _wait_until(lambda: server.pong_count > pongs)  # keepalive alive
            source.offer(_chunk())
            assert _wait_until(lambda: len(server.append_payloads) >= 1)  # pump alive
        finally:
            release.set()
            server.release_response_done()
            client.close()
    assert server.append_payloads == [_expected_bytes(_chunk())]
    assert client.pongs_sent >= 1


def test_a_playback_overrun_truncates_the_tail_rather_than_leaving_a_hole(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """A full mouth queue stops FEEDING that reply instead of dropping a chunk
    out of its middle: speech that stops early is honest, speech with a hole in
    it is a defect. Named, latched, and the tail is never spoken."""
    release = threading.Event()
    sink = _Sink(block=release)
    client = _chunked(url=_dead_url(), play=sink, playback_maxsize=1)
    client.start()
    try:
        _feed_deltas(client, "resp_full", _LONG_REPLY, _TEST_CHUNK_BYTES)
        client._dispatch_event({"type": "response.done", "response_id": "resp_full"})
        assert _wait_until(lambda: _count_reason(sense_log, duplex.REASON_PLAYBACK_QUEUE_FULL) >= 1)
        release.set()
        assert _wait_until(lambda: client.played >= 1)
    finally:
        release.set()
        client.close()

    played = sink.played
    assert played, "the chunks that fit were still spoken"
    assert _LONG_REPLY.startswith(played), "a hole was played, not a truncation"
    assert len(played) < len(_LONG_REPLY)
    assert _count_reason(sense_log, duplex.REASON_PLAYBACK_QUEUE_FULL) == 1  # latched


def test_the_playback_chunk_bounds_are_documented_defaults_in_limits() -> None:
    """The chunk size is a ``Limits`` value with a ``DEFAULT_*`` constant, so
    t1's measured per-chunk daemon round trip can retune it in one place."""
    limits = duplex.Limits()
    assert limits.playback_chunk_bytes == duplex.DEFAULT_PLAYBACK_CHUNK_BYTES
    assert limits.playback_first_chunk_bytes == duplex.DEFAULT_PLAYBACK_FIRST_CHUNK_BYTES
    # A whole number of PCM16 samples (a half-sample chunk would click), and
    # one second of speech at the output rate: the cut latency an interjection
    # pays against the round trip the daemon route pays.
    assert duplex.DEFAULT_PLAYBACK_CHUNK_BYTES % 2 == 0
    assert duplex.DEFAULT_PLAYBACK_CHUNK_BYTES == DEFAULT_OUTPUT_SAMPLE_RATE * 2
    # The first chunk is smaller, so the robot starts speaking sooner.
    assert duplex.DEFAULT_PLAYBACK_FIRST_CHUNK_BYTES % 2 == 0
    assert 0 < duplex.DEFAULT_PLAYBACK_FIRST_CHUNK_BYTES <= duplex.DEFAULT_PLAYBACK_CHUNK_BYTES


def test_the_first_chunk_can_be_smaller_so_speech_starts_sooner() -> None:
    """Injectable, and independently: the first group is its own bound."""
    sink = _Sink()
    with _hold_server() as server:
        client = _chunked(server, play=sink, playback_first_chunk_bytes=4)
        client.start()
        try:
            assert _wait_until(lambda: len(sink.calls) >= 2)
        finally:
            server.release_response_done()
            client.close()
    assert [len(pcm) for pcm, _rate in sink.calls][:2] == [4, 8]


def test_cancel_playback_is_a_clean_noop_when_the_mouth_is_idle(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """Safe from any thread, before ``start`` and after ``close``, and silent
    when there is nothing to cut — a cancel that skipped nothing is not a drop."""
    client = _chunked(url=_dead_url(), play=_Sink())
    progress = client.cancel_playback()
    client.close()
    assert client.cancel_playback().skipped_bytes == 0
    assert progress.skipped_bytes == 0
    assert progress.played_bytes == 0
    assert _count_reason(sense_log, duplex.REASON_PLAYBACK_CANCELLED) == 0


def test_the_playback_progress_record_is_frozen() -> None:
    progress = duplex.PlaybackProgress(
        response_id="resp_1",
        queued_bytes=8,
        played_bytes=8,
        in_flight_bytes=0,
        skipped_bytes=0,
        cancelled=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        progress.played_bytes = 0  # type: ignore[misc]


def test_playback_progress_is_none_for_a_reply_this_session_never_saw() -> None:
    client = _chunked(url=_dead_url())
    assert client.playback_progress("resp_never") is None
    assert client.playback_progress() is None


# --------------------------------------------------------------------------- #
# the audio-in contract                                                        #
# --------------------------------------------------------------------------- #


def test_float32_chunks_go_out_as_base64_pcm16_mono_little_endian() -> None:
    source = _Source()
    chunk = _chunk()
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server, read_audio=source)
        client.start()
        try:
            assert _established(client)
            source.offer(chunk)
            assert _wait_until(lambda: len(server.append_payloads) >= 1)
        finally:
            client.close()
    assert server.append_payloads == [_expected_bytes(chunk)]
    assert client.bytes_sent == len(_expected_bytes(chunk))


def test_a_stereo_chunk_is_channel_selected_never_interleaved() -> None:
    """``(N, 2)`` must yield N samples, not 2N — the documented ``to_mono``
    hazard, which this module inherits by reusing that one coercion."""
    source = _Source()
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server, read_audio=source)
        client.start()
        try:
            assert _established(client)
            source.offer(np.zeros((32, 2), dtype=np.float32))
            assert _wait_until(lambda: len(server.append_payloads) >= 1)
        finally:
            client.close()
    assert len(server.append_payloads[0]) == 64


def test_nothing_the_source_can_hand_over_ever_raises() -> None:
    source = _Source()
    for bad in (None, np.zeros(0, dtype=np.float32), b"", "not audio", object()):
        source.offer(bad)
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server, read_audio=source, stale_drain_max_chunks=0)
        client.start()
        try:
            assert _established(client)
            assert _wait_until(lambda: source.reads >= 6)
            assert client.connected is True
        finally:
            client.close()
    assert server.append_payloads == []


def test_a_standing_backlog_is_discarded_before_the_session_goes_live() -> None:
    """Replaying seconds-old audio into a server-side VAD manufactures
    utterances nobody spoke — the same discipline the runtime's own session
    applies to its queue, adapted to a pull source."""
    source = _Source()
    for _ in range(3):
        source.offer(_chunk())
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server, read_audio=source)
        client.start()
        try:
            assert _established(client)
            source.offer(_chunk())
            assert _wait_until(lambda: len(server.append_payloads) >= 1)
        finally:
            client.close()
    assert client.stale_chunks_discarded == 3
    assert server.append_payloads == [_expected_bytes(_chunk())]


def test_the_source_is_never_read_from_the_callers_thread() -> None:
    """``read_audio`` belongs to the session worker: a source that blocks (the
    tee socket's read timeout, a mic device) must never stall a caller."""
    seen: list[str] = []

    def _read():
        seen.append(threading.current_thread().name)
        return None

    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server, read_audio=_read)
        client.start()
        try:
            assert _established(client)
            assert _wait_until(lambda: bool(seen))
        finally:
            client.close()
    assert set(seen) == {duplex.WORKER_THREAD_NAME}


def test_the_sample_rate_rides_the_connect_url_and_is_not_hardcoded() -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _session(server, sample_rate=24000, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _established(client)
        finally:
            client.close()
    assert server.last_input_sample_rate == 24000
    assert server.request_path == "/v1/realtime?input_sample_rate=24000"


# --------------------------------------------------------------------------- #
# lifecycle + the caller-thread contract                                       #
# --------------------------------------------------------------------------- #


def test_start_returns_immediately_even_when_the_handshake_will_hang() -> None:
    """The blocking connect belongs to the worker: a gateway that accepts TCP
    and then says nothing must not hold the composition root."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    url = f"ws://127.0.0.1:{listener.getsockname()[1]}/v1/realtime"
    client = _session(url=url, connect_timeout_s=_TIMEOUT)
    try:
        started = time.monotonic()
        client.start()
        client.arm()
        assert client.take_utterance() is None
        assert client.take_response() is None
        assert (time.monotonic() - started) < 0.5
    finally:
        client.close()
        listener.close()


def test_start_is_idempotent_and_close_joins_every_thread() -> None:
    before = threading.active_count()
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _session(server, play=_Sink())
        client.start()
        worker = client.worker
        client.start()
        assert client.worker is worker
        assert _established(client)
        client.close()
        client.close()  # idempotent
        assert client.worker is not None
        assert client.worker.is_alive() is False
    assert _wait_until(lambda: threading.active_count() <= before, timeout=2.0)


def test_close_before_start_is_a_clean_noop() -> None:
    client = _session(url=_dead_url())
    client.close()
    client.close()
    assert client.worker is None


def test_the_context_manager_starts_and_closes_the_session() -> None:
    before = threading.active_count()
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        with _session(server) as client:
            assert _established(client)
        assert client.worker is not None
        assert client.worker.is_alive() is False
    assert _wait_until(lambda: threading.active_count() <= before, timeout=2.0)


def test_the_response_record_is_frozen() -> None:
    response = Response(
        response_id="resp_1",
        text="hi",
        audio=b"\x00\x01",
        samplerate=DEFAULT_OUTPUT_SAMPLE_RATE,
        t=1.0,
        interrupted=False,
        item_id=None,
        session_id=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        response.text = "no"  # type: ignore[misc]


def test_the_response_callback_receives_the_same_record_as_the_queue() -> None:
    seen: list[Response] = []
    with FakeRealtimeServer(Scenario.RESPONSE_HAPPY_PATH, wait_timeout=_TIMEOUT) as server:
        client = _session(server, on_response=seen.append, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 1)
        finally:
            client.close()
    response = client.take_response()
    assert response is not None
    assert seen == [response]


def test_a_raising_response_callback_never_stops_the_session() -> None:
    def _boom(_response: Response) -> None:
        raise RuntimeError("callback fault")

    with FakeRealtimeServer(Scenario.RESPONSE_HAPPY_PATH, wait_timeout=_TIMEOUT) as server:
        client = _session(server, on_response=_boom, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 1)
        finally:
            client.close()
    assert client.take_response() is not None


def test_a_server_ping_is_answered_with_a_pong() -> None:
    with FakeRealtimeServer(Scenario.PING_EXPECT_PONG, pong_wait_s=_TIMEOUT) as server:
        client = _session(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert server.wait_for_pong(timeout=_TIMEOUT) is True
        finally:
            client.close()
    assert client.pongs_sent >= 1


# --------------------------------------------------------------------------- #
# configuration — ONE owner for the endpoint + key resolution                  #
# --------------------------------------------------------------------------- #


def test_the_endpoint_and_key_resolution_have_one_owner() -> None:
    """Cited from ``reachy.speech.realtime``, never re-derived: a second copy of
    the precedence rules is exactly how the two ends of one wire drift apart."""
    from reachy.speech import realtime

    assert duplex.resolve_realtime_base_url is realtime.resolve_realtime_base_url
    assert duplex.resolve_realtime_api_key is realtime.resolve_realtime_api_key


def test_the_gateway_env_alone_targets_the_realtime_route_with_a_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH, require_bearer_token="sk-gateway") as server:
        monkeypatch.setenv(duplex.OPENAI_URL_BASE_ENV, f"http://{server.host}:{server.port}")
        monkeypatch.setenv(duplex.OPENAI_API_KEY_ENV, "sk-gateway")
        client = _session(url=None, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _established(client)
        finally:
            client.close()
    assert server.handshake_headers is not None
    assert server.handshake_headers["authorization"] == "Bearer sk-gateway"
    assert server.request_path == f"/v1/realtime?input_sample_rate={_RATE}"


def test_an_unusable_url_scheme_is_a_clean_setup_error() -> None:
    from reachy.cli._errors import CliError

    with pytest.raises(CliError):
        _session(url="ftp://box/realtime")


# --------------------------------------------------------------------------- #
# issue #141/S107 — bounds live in one frozen Limits, seams stay explicit      #
# --------------------------------------------------------------------------- #


def test_limits_defaults_match_the_documented_module_constants() -> None:
    """The refactor must not change a single default — only where it lives."""
    limits = duplex.Limits()
    assert limits.utterance_maxsize == duplex.DEFAULT_UTTERANCE_MAXSIZE
    assert limits.response_maxsize == duplex.DEFAULT_RESPONSE_MAXSIZE
    assert limits.playback_maxsize == duplex.DEFAULT_PLAYBACK_MAXSIZE
    assert limits.playback_chunk_bytes == duplex.DEFAULT_PLAYBACK_CHUNK_BYTES
    assert limits.playback_first_chunk_bytes == duplex.DEFAULT_PLAYBACK_FIRST_CHUNK_BYTES
    assert limits.max_response_bytes == duplex.DEFAULT_MAX_RESPONSE_BYTES
    assert limits.stale_drain_max_chunks == duplex.DEFAULT_STALE_DRAIN_MAX_CHUNKS
    assert limits.connect_timeout_s == duplex.DEFAULT_CONNECT_TIMEOUT_S
    assert limits.frame_timeout_s == duplex.DEFAULT_FRAME_TIMEOUT_S
    assert limits.poll_interval_s == duplex.DEFAULT_POLL_INTERVAL_S
    assert limits.backoff_initial_s == duplex.DEFAULT_BACKOFF_INITIAL_S
    assert limits.backoff_max_s == duplex.DEFAULT_BACKOFF_MAX_S
    assert limits.stable_after_s == duplex.DEFAULT_STABLE_AFTER_S
    assert limits.join_timeout_s == duplex.DEFAULT_JOIN_TIMEOUT_S


def test_limits_is_frozen() -> None:
    limits = duplex.Limits()
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.max_response_bytes = 1  # type: ignore[misc]


def test_the_constructor_keeps_seams_explicit_and_moves_only_bounds_into_limits() -> None:
    """S107's fix: bounds collapse into one keyword, injectable seams do not."""
    params = inspect.signature(RealtimeDuplexSession.__init__).parameters
    names = set(params) - {"self"}

    assert names.isdisjoint(_LIMIT_FIELDS), "a bound is still a bare parameter"
    assert "limits" in names

    seams = {"read_audio", "play", "on_utterance", "on_response", "clock"}
    assert seams <= names, "an injectable seam must stay an explicit parameter"
    assert all(params[name].kind is inspect.Parameter.KEYWORD_ONLY for name in names)


def test_a_bound_passed_through_limits_reaches_the_session() -> None:
    """Behavioural proof, not just a signature check: the value actually takes."""
    client = _session(read_audio=lambda: None, limits=duplex.Limits(join_timeout_s=0.0))
    assert client._join_timeout_s == 0.0
