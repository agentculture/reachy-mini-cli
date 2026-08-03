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
from urllib.parse import parse_qs, urlsplit

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
    REASON_VOICE_PROMPT_INVALID,
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
    duplex.ENV_VOICE_PROMPT,
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

    ``block_after`` lets the first N chunks complete before the sink wedges,
    which is what makes a MEASURED said/unsaid split deterministic (task t16):
    the test waits until exactly N chunks are confirmed, and only then lets the
    interjection land.
    """

    def __init__(
        self,
        *,
        block: threading.Event | None = None,
        boom: bool = False,
        block_after: int = 0,
    ) -> None:
        self.calls: list[tuple[bytes, int]] = []
        self.threads: list[str] = []
        self.entered = threading.Event()
        self._block = block
        self._boom = boom
        self._block_after = max(0, int(block_after))
        self._started = 0

    def __call__(self, pcm16_bytes: bytes, *, samplerate: int) -> None:
        self.threads.append(threading.current_thread().name)
        self._started += 1
        self.entered.set()
        if self._boom:
            raise RuntimeError("sink fault")
        if self._block is not None and self._started > self._block_after:
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
# h13/h20 — the send surface is CLOSED, and widening it is a DECISION          #
#                                                                             #
# Four frame kinds leave this client, and the fourth arrived by decision c28   #
# rather than by drift: session config (connect-URL query params, never a      #
# frame), ``input_audio_buffer.append``, ``response.create``, and — since      #
# task t10 — ``conversation.item.create``. The pins below widened from three   #
# kinds to four in that same change, which is honesty condition h20's whole    #
# requirement.                                                                 #
# --------------------------------------------------------------------------- #


def test_h13_only_the_legal_frame_kinds_ever_reach_the_server() -> None:
    """Behavioural half: what actually went out over a full duplex exchange.

    No item is in play here (no re-seed seam, and this gateway announces no
    item support), so the exchange is exactly the two event kinds it always
    was — the fourth frame kind costs a session that does not use it nothing.
    Its own behavioural pin is
    ``test_h20_the_send_surface_is_four_frame_kinds_once_the_item_channel_is_used``.
    """
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


def test_h20_the_modules_outbound_event_family_is_exactly_the_three_legal_kinds() -> None:
    """AST half (h13, widened by h20): every outbound EVENT this session can construct.

    Stronger than calling the known senders: it walks the source for every way
    an event can come into being — a call to one of the wire's ``build_*``
    event builders, or a hand-written dict literal carrying a ``"type"`` key —
    and asserts the resulting set is exactly ``input_audio_buffer.append``,
    ``response.create`` and ``conversation.item.create``. A FOURTH sender added
    later under any name fails this immediately; an unrecognised
    ``build_*_event`` callee fails it too, rather than being silently ignored.
    Session config is not in the set on purpose: it rides the connect URL's
    query params, never a frame — which is why the SEND SURFACE is four kinds
    while this EVENT family is three.

    **This pin widened from two members to three in the SAME change that landed
    ``conversation.item.create``, citing decision c28** — honesty condition h20
    in as many words. The send surface is a pinned boundary precisely so
    growing it has to be a decision somebody took and can be found later; a
    widening that arrived quietly, inside a change about something else, would
    be indistinguishable from drift. Task t8's per-utterance arming, by
    contrast, landed with NO pin change at all, because it REUSES
    ``response.create``.

    It scans BOTH source files, because this session's send path spans both:
    the shared wire mechanics live in ``reachy/speech/realtime.py`` (its
    "Shared session mechanics" section) and this module composes them. Scanning
    only this file would have left the shared owner free to grow another
    sender unseen. The sibling pin — that the shared owner can build NOTHING
    but ``append``, so the ears-only client can neither arm nor inject an item —
    lives in ``tests/test_realtime_client.py``.
    """
    builders = {
        "build_append_event": wire.APPEND_EVENT_TYPE,
        "build_response_create_event": wire.RESPONSE_CREATE_EVENT_TYPE,
        "build_conversation_item_create_event": wire.CONVERSATION_ITEM_CREATE_EVENT_TYPE,
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
    assert found == {
        wire.APPEND_EVENT_TYPE,
        wire.RESPONSE_CREATE_EVENT_TYPE,
        wire.CONVERSATION_ITEM_CREATE_EVENT_TYPE,
    }


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
# t9 — connect-time voice conventions (spec claim c10, honesty h8)             #
# --------------------------------------------------------------------------- #


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_the_default_construction_sends_no_system_prompt_query_param() -> None:
    """Nothing configured -> the connect URL omits the key entirely, so the
    GATEWAY's own operator-configured default applies (issue #151/#153, spec
    c10) -- never a blank override. Re-proves, from the property side, the
    exact query pinned behaviourally by
    test_the_sample_rate_rides_the_connect_url_and_is_not_hardcoded."""
    client = _session(url="ws://box/v1/realtime")
    assert "system_prompt" not in _query(client.connect_url)


def test_an_explicit_system_prompt_rides_the_connect_url() -> None:
    client = _session(url="ws://box/v1/realtime", system_prompt="Be warm and brief.")
    assert _query(client.connect_url)["system_prompt"] == ["Be warm and brief."]


def test_the_env_var_configures_the_voice_prompt_when_nothing_explicit_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(duplex.ENV_VOICE_PROMPT, "Speak like a lighthouse keeper.")
    client = _session(url="ws://box/v1/realtime")
    assert _query(client.connect_url)["system_prompt"] == ["Speak like a lighthouse keeper."]


def test_an_explicit_system_prompt_overrides_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(duplex.ENV_VOICE_PROMPT, "from the environment")
    client = _session(url="ws://box/v1/realtime", system_prompt="from the caller")
    assert _query(client.connect_url)["system_prompt"] == ["from the caller"]


def test_a_configured_system_prompt_reaches_the_real_wire() -> None:
    """The property is not enough on its own -- prove it against a live handshake."""
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _session(
            server, system_prompt="Be brief.", backoff_initial_s=5.0, backoff_max_s=5.0
        )
        client.start()
        try:
            assert _established(client)
        finally:
            client.close()
    query = _query(server.request_path or "")
    assert query.get("system_prompt") == ["Be brief."]
    assert query.get("input_sample_rate") == [str(_RATE)]


def test_a_blank_system_prompt_is_a_named_drop_and_never_sent(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    client = _session(url="ws://box/v1/realtime", system_prompt="   ")
    assert "system_prompt" not in _query(client.connect_url)
    assert _count_reason(sense_log, REASON_VOICE_PROMPT_INVALID) == 1


def test_an_over_long_system_prompt_is_a_named_drop_and_never_sent(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    client = _session(
        url="ws://box/v1/realtime", system_prompt="x" * (duplex.MAX_VOICE_PROMPT_CHARS + 1)
    )
    assert "system_prompt" not in _query(client.connect_url)
    assert _count_reason(sense_log, REASON_VOICE_PROMPT_INVALID) == 1


def test_nothing_configured_never_logs_a_voice_prompt_drop(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """The common, unconfigured case is not a failure -- no line at all."""
    _session(url="ws://box/v1/realtime")
    assert _count_reason(sense_log, REASON_VOICE_PROMPT_INVALID) == 0


def test_h13_the_three_frame_pin_is_unaffected_by_a_configured_system_prompt() -> None:
    """Criterion 1 (spec c10): the send surface stays exactly the two legal
    frame KINDS even with a system prompt configured -- the in-task assertion
    the c10/h8 acceptance criteria require. Session config, prompt included,
    rides the connect URL, never a frame; the static h13 pins right below this
    section are unmodified by this task and keep covering the rest."""
    source = _Source()
    source.offer(_chunk())
    with FakeRealtimeServer(Scenario.DUPLEX_HAPPY_PATH, wait_timeout=_TIMEOUT) as server:
        client = _session(
            server,
            read_audio=source,
            system_prompt="Be brief.",
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
    assert _query(server.request_path or "").get("system_prompt") == ["Be brief."]


# --------------------------------------------------------------------------- #
# resolve_voice_prompt — pure, no socket (spec c10, honesty h8)                #
# --------------------------------------------------------------------------- #


def test_resolve_voice_prompt_returns_none_when_nothing_is_configured() -> None:
    assert duplex.resolve_voice_prompt(env={}) is None


def test_resolve_voice_prompt_precedence_explicit_over_env() -> None:
    env = {duplex.ENV_VOICE_PROMPT: "from env"}
    assert duplex.resolve_voice_prompt("from explicit", env=env) == "from explicit"


def test_resolve_voice_prompt_reads_the_env_var_when_nothing_explicit() -> None:
    env = {duplex.ENV_VOICE_PROMPT: "from env"}
    assert duplex.resolve_voice_prompt(env=env) == "from env"


def test_resolve_voice_prompt_strips_surrounding_whitespace() -> None:
    assert duplex.resolve_voice_prompt("  Be brief.  ", env={}) == "Be brief."


def test_resolve_voice_prompt_rejects_a_blank_explicit_override() -> None:
    assert duplex.resolve_voice_prompt("   ", env={}) is None


def test_resolve_voice_prompt_rejects_a_blank_env_override() -> None:
    env = {duplex.ENV_VOICE_PROMPT: ""}
    assert duplex.resolve_voice_prompt(env=env) is None


def test_resolve_voice_prompt_rejects_an_over_long_override() -> None:
    text = "x" * (duplex.MAX_VOICE_PROMPT_CHARS + 1)
    assert duplex.resolve_voice_prompt(text, env={}) is None


def test_resolve_voice_prompt_accepts_exactly_the_cap() -> None:
    text = "x" * duplex.MAX_VOICE_PROMPT_CHARS
    assert duplex.resolve_voice_prompt(text, env={}) == text


def test_resolve_voice_prompt_is_pure_and_logs_nothing(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """The resolver itself never touches senselog -- the SESSION is what turns
    a rejected override into a named, counted drop (see the tests above)."""
    duplex.resolve_voice_prompt("   ", env={})
    duplex.resolve_voice_prompt("x" * (duplex.MAX_VOICE_PROMPT_CHARS + 1), env={})
    duplex.resolve_voice_prompt(env={})
    assert _count_reason(sense_log, REASON_VOICE_PROMPT_INVALID) == 0


def test_default_voice_prompt_is_chunk_friendly_and_stays_in_the_spoken_register() -> None:
    """Loosely pins DEFAULT_VOICE_PROMPT's content against the design intent
    (issue #151): no markdown/lists/code/emoji, and it asks for natural,
    complete, sentence-shaped spoken thoughts rather than a hard cap on how
    many sentences a reply may contain."""
    text = duplex.DEFAULT_VOICE_PROMPT.lower()
    assert "markdown" in text
    assert "sentence" in text
    assert "one or two" not in text  # not upstream's own terse cap, deliberately
    assert 0 < len(duplex.DEFAULT_VOICE_PROMPT) <= duplex.MAX_VOICE_PROMPT_CHARS


def test_default_voice_prompt_round_trips_through_resolve() -> None:
    """A sanity/regression pin: this module's own shipped default must always
    itself be a VALID override (never blank, never over the cap)."""
    resolved = duplex.resolve_voice_prompt(duplex.DEFAULT_VOICE_PROMPT, env={})
    assert resolved == duplex.DEFAULT_VOICE_PROMPT


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

    seams = {
        "read_audio",
        "play",
        "on_utterance",
        "on_response",
        "on_speech_started",
        "clock",
    }
    assert seams <= names, "an injectable seam must stay an explicit parameter"
    assert all(params[name].kind is inspect.Parameter.KEYWORD_ONLY for name in names)


def test_a_bound_passed_through_limits_reaches_the_session() -> None:
    """Behavioural proof, not just a signature check: the value actually takes."""
    client = _session(read_audio=lambda: None, limits=duplex.Limits(join_timeout_s=0.0))
    assert client._join_timeout_s == 0.0


# --------------------------------------------------------------------------- #
# criterion 5 — per-utterance arming: the MECHANISM (issue #149, task t8)      #
#                                                                             #
# The POLICY — which utterance deserves a reply — is not here and must never   #
# be: ``tests/test_agent_embody.py`` pins the composition root driving these   #
# calls from ``reachy.embody.attention``. What this section pins is the        #
# mechanism that policy needs: a session that does not arm itself, an          #
# ``arm_once`` that buys exactly one reply, a capability check that degrades   #
# to today's behaviour, and the c46 completion-clearing property that keeps a  #
# reply interruptible while it is being spoken.                                #
# --------------------------------------------------------------------------- #

_AMBIENT = "could you pass me the salt please"
_ADDRESSED = "reachy, are you listening"


def _arm_on_name(box: dict, name: str = "reachy"):
    """An ``on_utterance`` tap that arms only for an utterance naming the robot.

    A hand-written stand-in for :class:`reachy.embody.attention.AttentionGate`,
    on purpose: this module may not import a gate (the c4 pins above), and the
    mechanism must be demonstrable without one. The real gate drives the real
    thing one level up.
    """

    def _heard(utterance) -> None:
        if name in (getattr(utterance, "text", "") or "").lower():
            box["client"].arm_once()

    return _heard


def test_a_per_utterance_session_does_not_arm_itself_on_session_created() -> None:
    """The whole defect in one assertion: nobody asked, so nobody answers.

    Today's session arms on ``session.created`` and the gateway then answers
    EVERY committed utterance — which is why the room's ambient chatter gets
    spoken replies however firmly the layer decided to ignore it (#149).
    """
    with FakeRealtimeServer(
        Scenario.ONE_SHOT_ARMING,
        announce_one_shot_arming=True,
        transcripts=[_AMBIENT],
        arm_grace_s=0.2,
    ) as server:
        client = _session(server, arm_per_utterance=True, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: client.utterances >= 1)
            assert _wait_until(lambda: server.unanswered_transcripts >= 1)
        finally:
            client.close()

    assert server.response_create_count == 0, "an ambient utterance asked for a reply"
    assert client.arms_sent == 0
    assert client.responses == 0
    assert server.answered_texts == []
    assert server.unanswered_texts == [_AMBIENT]
    assert server.is_armed is False


def test_a_cold_ambient_utterance_is_silent_and_an_admitted_one_arms_exactly_one_reply() -> None:
    """Criterion 1, over ONE session: silence, then exactly one spoken reply.

    Both utterances are HEARD — the wire is ungated and stays that way — but
    only the one the caller admits reaches the room as sound.
    """
    box: dict = {}
    with FakeRealtimeServer(
        Scenario.ONE_SHOT_ARMING,
        announce_one_shot_arming=True,
        transcripts=[_AMBIENT, _ADDRESSED],
        arm_grace_s=0.2,
    ) as server:
        client = _session(
            server,
            arm_per_utterance=True,
            on_utterance=_arm_on_name(box),
            backoff_initial_s=5.0,
            backoff_max_s=5.0,
        )
        box["client"] = client
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 1)
            assert _wait_until(lambda: client.utterances >= 2)
        finally:
            client.close()

    assert server.response_create_count == 1, "exactly one reply was asked for"
    assert client.arms_sent == 1
    # WHICH one was answered, not just how many: the counts alone are satisfied
    # by a client that got the wiring exactly backwards.
    assert server.answered_texts == [_ADDRESSED]
    assert server.unanswered_texts == [_AMBIENT]
    assert client.supports_one_shot_arming is True
    # h13 stays exactly as wide as it was: per-utterance arming REUSES
    # response.create and adds no frame kind (the AST pins above are untouched).
    assert set(_sent_event_types(server)) <= {
        wire.APPEND_EVENT_TYPE,
        wire.RESPONSE_CREATE_EVENT_TYPE,
    }


def test_h9_a_gateway_without_one_shot_arming_degrades_to_arming_once(sense_log) -> None:
    """Criterion 2: never half-deploy — degrade to today's behaviour, and say so.

    ``ConversationBridge.arm()`` latches ``armed = True`` with no disarm today
    (lobes-cli#170 item 1 is the ask). Against that gateway a per-utterance
    session must NOT go quiet — silence would be a regression the operator
    never asked for — so it arms once at connect exactly as it always did, and
    names the degrade once.
    """
    box: dict = {}
    with FakeRealtimeServer(
        Scenario.ONE_SHOT_ARMING,
        announce_one_shot_arming=False,
        transcripts=[_AMBIENT, _ADDRESSED],
        arm_grace_s=0.2,
    ) as server:
        client = _session(
            server,
            arm_per_utterance=True,
            on_utterance=_arm_on_name(box),
            backoff_initial_s=5.0,
            backoff_max_s=5.0,
        )
        box["client"] = client
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 2)
        finally:
            client.close()

    assert client.supports_one_shot_arming is False
    assert client.arms_sent == 1, "today's arm-once behaviour, unchanged"
    assert server.answered_texts == [_AMBIENT, _ADDRESSED], "the old gateway answers everything"
    assert server.unanswered_texts == []
    assert _count_reason(sense_log, duplex.REASON_ONE_SHOT_ARMING_UNSUPPORTED) == 1
    # The caller's request was seen and declined rather than silently dropped.
    assert client.arms_declined >= 1


def test_arm_once_is_declined_and_counted_when_the_gateway_never_announced() -> None:
    """The degraded return value is the caller's own answer, not just a log line."""
    client = _session(url="ws://127.0.0.1:1/v1/realtime", arm_per_utterance=True)
    assert client.supports_one_shot_arming is False
    assert client.arm_once() is False
    assert client.arms_declined == 1
    assert client.arms_sent == 0


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"config": None},
        {"config": {}},
        {"config": {"arming": "latched"}},
        {"config": {"arming": ""}},
        {"config": {"arming": True}},
        {"arming": "one_shot"},  # right value, wrong place
        {"config": "one_shot"},
    ],
)
def test_the_capability_probe_needs_an_explicit_affirmative_announcement(event) -> None:
    """Fail CLOSED: anything short of the announced value means arm-once."""
    assert duplex.announces_one_shot_arming(event) is False


def test_the_capability_probe_recognises_the_announced_value() -> None:
    announced = {"type": "session.created", "config": {"arming": duplex.ARMING_MODE_ONE_SHOT}}
    assert duplex.announces_one_shot_arming(announced) is True


def test_c46_one_shot_arming_clears_at_completion_so_a_reply_stays_interruptible() -> None:
    """c46/h31: ``armed`` must survive the whole reply, or barge-in dies with it.

    Every floor call upstream sits behind ``if self.armed``
    (``lobes/realtime/_conversation.py``:450), so a gateway clearing ``armed``
    when it consumes ``response.create`` would answer the turn and then be
    unable to honour the human who speaks over it. The fake gateway therefore
    holds the reply open and this test reads ``is_armed`` MID-SYNTHESIS —
    still armed — and again after it ends: spent.
    """
    box: dict = {}
    sink = _Sink()
    with FakeRealtimeServer(
        Scenario.ONE_SHOT_ARMING,
        announce_one_shot_arming=True,
        transcripts=[_ADDRESSED],
        arm_grace_s=0.2,
        hold_response=True,
        interrupt_response=True,
        response_audio=bytes(64),
        response_chunk_bytes=16,
        wait_timeout=_TIMEOUT,
    ) as server:
        client = _session(
            server,
            play=sink,
            on_utterance=_arm_on_name(box),
            arm_per_utterance=True,
            playback_chunk_bytes=16,
            playback_first_chunk_bytes=16,
            backoff_initial_s=5.0,
            backoff_max_s=5.0,
        )
        box["client"] = client
        client.start()
        try:
            assert _wait_until(lambda: server.arms_consumed >= 1)
            # The reply is in flight and the mouth is speaking it...
            assert _wait_until(lambda: sink.entered.is_set())
            assert server.is_armed is True, "cleared at consumption — barge-in is dead"
        finally:
            server.release_response_done()
            assert _wait_until(lambda: client.responses >= 1)
            client.close()

    assert server.is_armed is False, "one arm must not buy a second reply"
    assert server.arms_consumed == 1


def test_c46_a_barge_in_still_cuts_the_reply_short_under_one_shot_arming(sense_log) -> None:
    """h31's other half: the interruption itself still lands, and is named.

    The reply is held open long enough for the mouth to speak its first chunks
    and is then INTERRUPTED. Under one-shot arming that must behave exactly as
    it did before: the remainder is skipped, the played prefix is kept, and the
    cut is a named drop.
    """
    box: dict = {}
    block = threading.Event()
    sink = _Sink(block=block)
    with FakeRealtimeServer(
        Scenario.ONE_SHOT_ARMING,
        announce_one_shot_arming=True,
        transcripts=[_ADDRESSED],
        arm_grace_s=0.2,
        hold_response=True,
        interrupt_response=True,
        response_audio=bytes(range(64)),
        response_chunk_bytes=16,
        wait_timeout=_TIMEOUT,
    ) as server:
        client = _session(
            server,
            play=sink,
            on_utterance=_arm_on_name(box),
            arm_per_utterance=True,
            playback_chunk_bytes=16,
            playback_first_chunk_bytes=16,
            backoff_initial_s=5.0,
            backoff_max_s=5.0,
        )
        box["client"] = client
        client.start()
        try:
            # The mouth is inside its FIRST chunk and blocked there, so the
            # rest of the reply is queued and cancellable when the cut lands.
            assert _wait_until(lambda: sink.entered.is_set())
            server.release_response_done()
            assert _wait_until(lambda: client.responses >= 1)
            assert _wait_until(lambda: client.chunks_cancelled >= 1)
            block.set()
            # The blocked chunk is only CONFIRMED when ``play`` returns, so
            # wait on that rather than on ``close()``'s bounded join.
            assert _wait_until(lambda: client.played >= 1)
        finally:
            block.set()
            client.close()

    assert _count_reason(sense_log, REASON_RESPONSE_INTERRUPTED) >= 1
    assert client.cancelled_bytes > 0, "the unspoken remainder was spoken anyway"
    assert sink.played == bytes(range(16)), "the prefix the room heard was not kept"


# =========================================================================== #
# t7 — the said/unsaid split: measured at the sink, estimated inside one chunk #
# (spec claims c34/c39/c41, honesty h22/h24/h26)                              #
# =========================================================================== #

#: A reply whose words map onto ``_LONG_REPLY``'s six chunks cleanly enough to
#: reason about by hand: 27 characters over 48 audio bytes.
_SPLIT_TEXT = "one two three four five six"


def _progress(**kwargs) -> duplex.PlaybackProgress:
    """A hand-built measurement record — the whole point of the pure split."""
    fields = {
        "response_id": "resp_split",
        "queued_bytes": 48,
        "played_bytes": 0,
        "in_flight_bytes": 0,
        "skipped_bytes": 0,
        "cancelled": True,
        "total_bytes": 48,
    }
    fields.update(kwargs)
    return duplex.PlaybackProgress(**fields)


def test_t7_the_estimator_cites_its_lobes_donor_by_path() -> None:
    """h26's second half: the module docstring names the donor FUNCTION and file.

    Cite-don't-import across repos: nothing here may import lobes, so the only
    thing tying this arithmetic to the reasoning behind it is the citation.
    """
    doc = duplex.estimate_spoken_prefix.__doc__ or ""

    assert "lobes/realtime/_floor.py" in doc
    assert "estimate_spoken_prefix" in doc
    assert "323" in doc


def test_t7_the_estimator_backs_up_to_the_previous_word_boundary() -> None:
    """A third of the audio played -> a whole-word prefix, never a split word."""
    said = duplex.estimate_spoken_prefix(_SPLIT_TEXT, 16, 48)

    assert said == "one two"
    assert _SPLIT_TEXT.startswith(said)
    assert not said.endswith(" ")


@pytest.mark.parametrize("played", [1, 8, 16, 24, 32, 40, 47])
def test_t7_the_estimator_never_counts_more_than_the_measured_proportion(played: int) -> None:
    """h26: unsaid-biased at every offset — nothing beyond the measured boundary.

    The bound is the donor's own: the proportional cut, floored at one
    character (something played, so something was heard), and then backed up to
    a word boundary — never past it.
    """
    said = duplex.estimate_spoken_prefix(_SPLIT_TEXT, played, 48)

    assert len(said) <= max(1, len(_SPLIT_TEXT) * played // 48)
    assert _SPLIT_TEXT.startswith(said)


def test_t7_the_estimator_keeps_the_donors_no_measurement_branch() -> None:
    """Faithful to the donor, INCLUDING the branch its own caller has to guard.

    ``estimate_spoken_prefix`` answers "assume it all played" when it is handed
    no total, which is exactly wrong for a cut that landed before any audio
    existed — hence the guard in :func:`~reachy.speech.realtime_duplex.
    split_spoken`, cited from the donor's caller.
    """
    assert duplex.estimate_spoken_prefix(_SPLIT_TEXT, 0, 0) == _SPLIT_TEXT
    assert duplex.estimate_spoken_prefix(_SPLIT_TEXT, 99, 48) == _SPLIT_TEXT
    assert duplex.estimate_spoken_prefix(_SPLIT_TEXT, 0, 48) == ""
    assert duplex.estimate_spoken_prefix("", 24, 48) == ""


def test_t7_the_split_carries_the_zero_measurement_guard_the_donor_caller_needs() -> None:
    """A cut with nothing measured says NOTHING was said — never the whole reply."""
    split = duplex.split_spoken(_SPLIT_TEXT, _progress(total_bytes=0, queued_bytes=0))

    assert split.said == ""
    assert split.unsaid == _SPLIT_TEXT


def test_t7_a_split_is_measured_to_the_chunk_boundary_and_estimates_only_inside_it() -> None:
    """The advantage over the donor: two confirmed chunks, one uncertain one.

    ``played_bytes`` is what the sink RETURNED from, so the prefix is exact to
    the chunk boundary. The chunk still inside ``play`` is not counted as said
    — its words land in the remainder, where the worst case is the mind
    offering to repeat something the room half-heard, rather than believing it
    said a sentence nobody got.
    """
    split = duplex.split_spoken(
        _SPLIT_TEXT, _progress(played_bytes=16, in_flight_bytes=8, skipped_bytes=24)
    )

    assert split.said == "one two"
    assert split.unsaid == "three four five six"
    assert "three" in split.unsaid, "the boundary chunk's words are never counted as said"
    assert (split.played_bytes, split.in_flight_bytes, split.total_bytes) == (16, 8, 48)
    assert split.cut is True


def test_t7_said_and_unsaid_partition_the_reply_so_nothing_is_discarded() -> None:
    """c34: never discarded silently, never recorded as spoken — so both halves."""
    for played in (0, 8, 16, 24, 32, 40, 48):
        split = duplex.split_spoken(_SPLIT_TEXT, _progress(played_bytes=played))
        rejoined = " ".join(part for part in (split.said, split.unsaid) if part)

        assert rejoined.split() == _SPLIT_TEXT.split(), played
        assert _SPLIT_TEXT.startswith(split.said)


def test_t7_a_reply_that_played_whole_leaves_nothing_unsaid() -> None:
    split = duplex.split_spoken(_SPLIT_TEXT, _progress(played_bytes=48, cancelled=False))

    assert split.said == _SPLIT_TEXT
    assert split.unsaid == ""
    assert split.complete is True
    assert split.cut is False


def test_t7_the_session_splits_a_reply_cut_mid_playback_at_the_measured_boundary() -> None:
    """End to end over the dispatcher: two chunks heard, four never spoken."""
    sink = _Sink()
    client = _chunked(url=_dead_url(), play=sink)
    client.start()
    try:
        _feed_deltas(client, "resp_cut", _LONG_REPLY[:16], _TEST_CHUNK_BYTES)
        assert _wait_until(lambda: len(sink.calls) == 2)
        client.cancel_playback()
        client._dispatch_event(
            {"type": "response.text.done", "response_id": "resp_cut", "text": _SPLIT_TEXT}
        )
        for start in range(16, len(_LONG_REPLY), _TEST_CHUNK_BYTES):
            client._dispatch_event(
                _delta_event("resp_cut", _LONG_REPLY[start : start + _TEST_CHUNK_BYTES])
            )
        client._dispatch_event({"type": "response.done", "response_id": "resp_cut"})
        assert _wait_until(lambda: client.responses >= 1)
    finally:
        client.close()

    split = client.spoken_split("resp_cut")
    assert split is not None
    assert split.response_id == "resp_cut"
    assert split.said == "one two"
    assert split.unsaid == "three four five six"
    assert split.cut is True
    assert (split.played_bytes, split.total_bytes) == (16, len(_LONG_REPLY))
    # The RECORD is unchanged by the split: what the server said and what the
    # room heard are two different facts (t6's own pin, restated here).
    response = client.take_response()
    assert response is not None
    assert response.audio == _LONG_REPLY
    assert response.text == _SPLIT_TEXT


def test_t7_a_split_is_withheld_until_the_reply_is_complete() -> None:
    """Mid-stream there is no honest TOTAL, so there is no honest split either.

    Splitting against the audio that happens to have arrived would divide the
    full text by a fraction of its audio and overstate what the room heard —
    the one direction c34 forbids.
    """
    sink = _Sink()
    client = _chunked(url=_dead_url(), play=sink)
    client.start()
    try:
        _feed_deltas(client, "resp_live", _LONG_REPLY[:16], _TEST_CHUNK_BYTES)
        assert _wait_until(lambda: len(sink.calls) == 2)
        client._dispatch_event(
            {"type": "response.text.done", "response_id": "resp_live", "text": _SPLIT_TEXT}
        )

        assert client.spoken_split("resp_live") is None
        assert client.playback_progress("resp_live") is not None, "the MEASUREMENT is live"
    finally:
        client.close()


def test_t7_a_split_is_none_for_a_reply_this_session_never_saw() -> None:
    client = _session(url=_dead_url())
    assert client.spoken_split("resp_never") is None
    assert client.spoken_split() is None


def test_t7_a_client_side_cut_sends_nothing_to_the_server_and_claims_no_agreement() -> None:
    """c39, behaviourally: the layer does not try to correct the floor's history.

    A client-local cut is INVISIBLE to the floor — wire delivery completed, so
    the server sends ``response.done`` and appends the FULL reply to its own
    history. Phase 1 lives with that divergence knowingly: the client is the
    measured authority for the LAYER's record.

    Task t10 made this a STRONGER statement than it was when it was written.
    The ``conversation.item.create`` channel now exists, so "the cut sends
    nothing" is no longer true merely because no frame could carry a
    correction — it is true because a correction is a POLICY about the
    canonical history, and that belongs to the layer one level up (decision
    c27, task t11). This session, with no re-seed seam and no item support
    announced, still puts exactly the two event kinds on the wire.
    """
    release = threading.Event()
    sink = _Sink(block=release)
    with _hold_server(response_text=_SPLIT_TEXT) as server:
        client = _chunked(server, play=sink)
        client.start()
        try:
            assert sink.entered.wait(timeout=_TIMEOUT)
            assert _wait_until(lambda: client.chunks_queued >= 6)
            client.cancel_playback()
        finally:
            release.set()
            server.release_response_done()
            assert _wait_until(lambda: client.responses >= 1)
            client.close()

    assert set(_sent_event_types(server)) <= {
        wire.APPEND_EVENT_TYPE,
        wire.RESPONSE_CREATE_EVENT_TYPE,
    }, "the cut must not correct the floor's history behind the layer's back"
    split = client.spoken_split()
    assert split is not None
    assert split.cut is True
    assert not any(
        field.name in {"server_said", "server_history", "floor_agrees"}
        for field in dataclasses.fields(duplex.SpokenSplit)
    ), "the split must claim nothing about the server's own record"


def test_t7_the_phase_one_server_overstatement_is_documented() -> None:
    """h24: recorded here, not discovered later by whoever reads the histories."""
    doc = " ".join((duplex.__doc__ or "").split()).lower()

    assert "response.done" in doc
    assert "overstates" in doc
    assert "invisible to the floor" in doc
    assert "conversation.item.create" in doc, "and it names what closes the gap"


# =========================================================================== #
# t16 — the TAIL cut: the MECHANISM only (spec c34/c35, honesty h22)          #
#                                                                            #
# The server-driven ``response.interrupted`` path is t6's and is unchanged —  #
# upstream paces its delivery to the playhead precisely so barge-in stays     #
# live, and it covers the bulk of a reply. What it cannot cover is the lag    #
# THIS client adds after receipt (up to one chunk, plus the daemon's          #
# upload-then-play round trip), which lands AFTER ``response.done``: the      #
# floor is LISTENING again while the room still hears audio.                  #
#                                                                            #
# What this module gained for that is two things and no more: a tap on the    #
# server's OWN VAD onset, and a predicate saying whether a cut would withhold #
# anything. It acts on neither. The POLICY — cut, measure, record — lives in  #
# ``reachy/cli/_commands/agent.py`` and is pinned in                          #
# ``tests/test_agent_embody.py``; the three gate-free pins above are          #
# unchanged, which is the point.                                              #
# =========================================================================== #


def _cut_on_speech(box: dict):
    """A hand-written stand-in for the composition root's tail-cut policy.

    Deliberately hand-written rather than imported, for the same reason
    :func:`_arm_on_name` is: this module may reach no gate and no layer, and
    the mechanism has to be demonstrable without one. The real policy — which
    additionally asks for the measured split and records it — is one level up.
    """
    box.setdefault("cuts", [])
    box.setdefault("noops", [])

    def _started(event: dict) -> None:
        client = box["client"]
        if not client.playback_pending:
            box["noops"].append(event)
            return
        box["cuts"].append(client.cancel_playback())

    return _started


def _tail_server(**kwargs) -> FakeRealtimeServer:
    kwargs.setdefault("response_audio", _LONG_REPLY)
    kwargs.setdefault("response_chunk_bytes", _TEST_CHUNK_BYTES)
    kwargs.setdefault("response_text", _SPLIT_TEXT)
    kwargs.setdefault("wait_timeout", _TIMEOUT)
    return FakeRealtimeServer(Scenario.RESPONSE_TAIL_INTERJECTION, **kwargs)


def test_t16_the_servers_vad_onset_reaches_the_caller_as_a_tap() -> None:
    """The mechanism: ``speech_started`` is published, on the worker thread.

    It is the SERVER's VAD, never a loudness reading taken here (spec c35) —
    which is why the tap carries the raw event rather than any number this
    client computed.
    """
    seen: list[dict] = []
    threads: list[str] = []

    def _started(event: dict) -> None:
        seen.append(event)
        threads.append(threading.current_thread().name)

    with FakeRealtimeServer(Scenario.HAPPY_PATH, wait_timeout=_TIMEOUT) as server:
        client = _session(server, on_speech_started=_started)
        client.start()
        try:
            assert _wait_until(lambda: bool(seen))
        finally:
            client.close()

    assert seen[0]["type"] == realtime.SPEECH_STARTED
    assert threads == [duplex.WORKER_THREAD_NAME]


def test_t16_speech_stopped_is_not_interruption_evidence() -> None:
    """Only the ONSET may cut: an ending is the human giving the floor back."""
    seen: list[dict] = []
    client = _session(url=_dead_url(), on_speech_started=seen.append)

    client._dispatch_event({"type": realtime.SPEECH_STOPPED, "item_id": "item_1"})
    assert seen == []

    client._dispatch_event({"type": realtime.SPEECH_STARTED, "item_id": "item_1"})
    assert len(seen) == 1


def test_t16_playback_pending_names_the_audio_a_cut_would_actually_withhold() -> None:
    """Criterion 1's other half: "nothing playing" has to be answerable.

    A chunk already inside ``play`` is NOT pending: it cannot be recalled, so
    cutting on it would withhold nothing while still recording its words as
    unsaid — a reply the room heard in full, filed as truncated.
    """
    release = threading.Event()
    sink = _Sink(block=release)
    client = _chunked(url=_dead_url(), play=sink)
    assert client.playback_pending is False, "an idle session has nothing to cut"
    client.start()
    try:
        _feed_deltas(client, "resp_pending", _LONG_REPLY, _TEST_CHUNK_BYTES)
        assert sink.entered.wait(timeout=_TIMEOUT)
        assert _wait_until(lambda: client.playback_pending is True)
        client.cancel_playback()
        assert client.playback_pending is False, "the cut drained the queue"
        # One chunk is still inside ``play`` — busy, but nothing left to skip.
        assert client.speaking is True
    finally:
        release.set()
        client.close()


def test_playback_pending_covers_the_chunk_taken_but_not_yet_committed() -> None:
    """The guard must be as wide as the cut it guards (Qodo, PR #158).

    A chunk the mouth has ``get``-ed but not yet committed to ``play`` is OUT
    of the queue and STILL cancellable: ``_skip_remaining`` bumps the
    generation and ``_begin_chunk`` re-checks it immediately before speaking.
    So ``self._playback.empty()`` is a strictly NARROWER predicate than what
    ``cancel_playback`` can withhold, and a ``speech_started`` landing in that
    window would read "nothing to cut" about a chunk a cut would in fact have
    skipped — the one-chunk boundary the design promises, lost.

    This drives the mouth into exactly that window: the consumer is blocked
    between the queue and ``_begin_chunk``, so the queue is EMPTY while the
    chunk is still skippable. The old ``empty()`` implementation returns False
    here; the counter returns True, and a cut then really does skip it.
    """
    adopted = threading.Event()
    proceed = threading.Event()
    client = _chunked(url=_dead_url(), play=_Sink())
    real_begin = client._begin_chunk

    def _stalled_begin(item):
        adopted.set()
        assert proceed.wait(timeout=_TIMEOUT)
        return real_begin(item)

    client._begin_chunk = _stalled_begin  # type: ignore[method-assign]
    client.start()
    try:
        # EXACTLY one chunk, so the queue drains to empty while the mouth
        # holds it — the window this test exists to describe.
        _feed_deltas(client, "resp_window", _LONG_REPLY[:_TEST_CHUNK_BYTES], _TEST_CHUNK_BYTES)
        assert adopted.wait(timeout=_TIMEOUT), "the mouth never took a chunk"
        # The precondition that makes this test meaningful: the queue really is
        # empty, so the OLD predicate would have said "nothing to cut".
        assert client._playback.empty() is True
        assert client.playback_pending is True, "a cut here would still withhold audio"
        before = client.chunks_cancelled
        client.cancel_playback()
        proceed.set()
        assert _wait_until(lambda: client.chunks_cancelled > before), "the chunk was not skipped"
    finally:
        proceed.set()
        client.close()


def test_t16_the_wire_itself_cuts_nothing_on_a_vad_onset() -> None:
    """The gate-free pin, behaviourally: the module publishes, it does not act.

    A recording tap observes the very onset a policy WOULD cut on, and the
    reply plays out whole. Anything else would mean this module had formed an
    opinion about whose speech is worth stopping the robot for — the opinion
    the three structural c4 pins above say it cannot hold.
    """
    seen: list[dict] = []
    sink = _Sink()
    with _tail_server() as server:
        client = _chunked(server, play=sink, on_speech_started=seen.append)
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 1)
            assert _wait_until(lambda: sink.played == _LONG_REPLY)
            server.release_interjection()
            assert _wait_until(lambda: bool(seen))
        finally:
            client.close()

    assert sink.played == _LONG_REPLY, "the wire cut a reply nobody asked it to cut"
    assert client.chunks_cancelled == 0


def test_t16_a_vad_onset_over_the_tail_cuts_the_queue_within_one_chunk() -> None:
    """Criterion 1, end to end over a real socket, in the window that matters.

    ``response.done`` has already landed — the floor is LISTENING and will send
    no ``response.interrupted`` however loudly the room talks — and the mouth
    is still three chunks behind. The onset arrives, the policy cuts, and the
    room keeps only the chunk already committed to the speaker.
    """
    release = threading.Event()
    sink = _Sink(block=release, block_after=2)
    box: dict = {}
    with _tail_server() as server:
        client = _chunked(server, play=sink, on_speech_started=_cut_on_speech(box))
        box["client"] = client
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 1), "the reply never completed"
            assert _wait_until(lambda: len(sink.calls) == 2), "two chunks were never confirmed"
            server.release_interjection()
            assert _wait_until(lambda: bool(box["cuts"]))
        finally:
            release.set()
            client.close()

    (cut,) = box["cuts"]
    assert cut.cancelled is True
    assert (cut.played_bytes, cut.in_flight_bytes, cut.skipped_bytes) == (16, 8, 24)
    # ... and the measured split of exactly that cut (criterion 2's input).
    split = client.spoken_split(cut.response_id)
    assert split is not None
    assert (split.said, split.unsaid) == ("one two", "three four five six")
    assert split.cut is True


def test_t16_a_second_onset_after_the_queue_drained_is_a_clean_noop() -> None:
    """Criterion 1's no-op half — and the reason one reply is never cut twice.

    A cut drains the queue AND stamps the reply stale, so nothing of it can be
    re-queued: a human interjecting twice over one tail finds nothing left to
    withhold the second time. That is what makes at-most-one cut per reply
    structural rather than a flag someone has to remember to clear.
    """
    sink = _Sink()
    box: dict = {}
    client = _chunked(url=_dead_url(), play=sink, on_speech_started=_cut_on_speech(box))
    box["client"] = client
    client.start()
    try:
        _feed_deltas(client, "resp_tail", _LONG_REPLY, _TEST_CHUNK_BYTES)
        client._dispatch_event(
            {"type": "response.text.done", "response_id": "resp_tail", "text": _SPLIT_TEXT}
        )
        client._dispatch_event({"type": "response.done", "response_id": "resp_tail"})
        assert _wait_until(lambda: client.chunks_queued >= 6)

        client._dispatch_event({"type": realtime.SPEECH_STARTED, "item_id": "item_1"})
        cancelled = client.chunks_cancelled
        client._dispatch_event({"type": realtime.SPEECH_STARTED, "item_id": "item_2"})
    finally:
        client.close()

    assert len(box["cuts"]) == 1, "the same reply was cut twice"
    assert len(box["noops"]) == 1, "the second onset withheld nothing, and said so"
    assert client.chunks_cancelled == cancelled


def test_t16_a_raising_speech_started_tap_never_stops_the_session() -> None:
    """The tap runs on the worker thread, so it may not take the session down."""

    def _boom(_event: dict) -> None:
        raise RuntimeError("policy fault")

    client = _session(url=_dead_url(), on_speech_started=_boom)
    client._dispatch_event({"type": realtime.SPEECH_STARTED, "item_id": "item_1"})
    client._dispatch_event({"type": realtime.SPEECH_STARTED, "item_id": "item_2"})
    assert client.ignored_events == 0


def test_t16_the_tail_window_and_its_mechanism_are_documented() -> None:
    """The gap is a client-side one, and the docstring has to say whose it is.

    Without this, the next reader sees a cut path beside ``response.interrupted``
    and reasonably concludes one of them is redundant.
    """
    doc = " ".join((duplex.__doc__ or "").split()).lower()

    assert "delivery_pause_ms" in doc, "upstream's pacing is why the gap is only the tail"
    assert "on_speech_started" in doc
    assert "playback_pending" in doc
    assert "locator" in doc, "c35: never a loudness reading taken here"


# =========================================================================== #
# t10 — ``conversation.item.create``: the FOURTH frame kind (decision c28)    #
#                                                                            #
# The mouth knows nothing the mind knows (issue #153) because there was no    #
# channel to tell it. Decision c27 makes the LAYER the curator of the         #
# canonical conversation history; decision c28 chose                          #
# ``conversation.item.create`` as the per-turn channel that pushes it into    #
# the floor's generate call — and accepted, explicitly and on both repos,     #
# that this client's pinned send surface widens from three frame kinds to     #
# four. Honesty condition h20 requires the pin to widen in the SAME change,   #
# which is what the renamed AST pins above do.                                #
#                                                                            #
# Upstream has NOT shipped item parity (agentculture/lobes-cli#170 item 2 is  #
# the ask, unanswered at build time), so both the schema and the capability   #
# announcement are provisional and fail CLOSED — no item is ever sent to a    #
# gateway that did not announce support. That is the same contract shape      #
# ``announces_one_shot_arming`` established in task t8.                       #
# =========================================================================== #

_ITEM = wire.CONVERSATION_ITEM_CREATE_EVENT_TYPE
_ARM = wire.RESPONSE_CREATE_EVENT_TYPE

#: The one wait in this section that spans a DROP and a full reconnect, so it
#: budgets for a starved thread rather than for a socket round trip: under
#: ``pytest -n auto`` this box runs dozens of socket-owning threads at once,
#: and the ordinary ``_TIMEOUT`` proved tight enough to fail there
#: occasionally. Costs nothing when things are fast (every wait is a poll).
_RECONNECT_TIMEOUT = 20.0

#: What a re-seed carries: Gemma's m-window (one turn here, both roles) and the
#: Qwen-maintained summary of everything older (spec claim c40). The CONTENT is
#: the layer's business (task t11 curates it); what this file pins is that
#: whatever the seam returns crosses the wire, in order, before the arm.
_SUMMARY = duplex.ConversationItem.context("earlier: the operator asked about the weather")
_TURN = duplex.ConversationItem.history("user", "and what about tomorrow?")


class _Reseed:
    """The injected re-seed seam, counting its calls.

    A plain callable returning a fixed list would pin the ordering just as
    well; counting calls is what additionally proves the seam is consulted
    ONCE PER SESSION rather than once per process — the property that makes a
    reconnect re-seed at all.
    """

    def __init__(self, *items: duplex.ConversationItem, boom: bool = False) -> None:
        self._items = list(items)
        self._boom = boom
        self.calls = 0

    def __call__(self) -> list[duplex.ConversationItem]:
        self.calls += 1
        if self._boom:
            raise RuntimeError("the canonical history is unavailable")
        return list(self._items)


def _items_announced(client: RealtimeDuplexSession) -> None:
    """Teach a socket-less session that its gateway announces items.

    The direct-dispatch escape hatch the rest of this file already uses, for
    the unit-scale checks that need the capability WITHOUT a live session: this
    event carries no re-seed, so nothing is sent and no socket is touched.
    """
    client._dispatch_event(
        {
            "type": realtime.SESSION_CREATED,
            "session_id": "sess_items",
            "config": {duplex.ITEMS_CONFIG_KEY: duplex.ITEMS_MODE_CONTEXT_AND_HISTORY},
        }
    )


# --- the capability probe: explicit affirmative only, or nothing --------------


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"config": None},
        {"config": {}},
        {"config": {"items": "supported"}},
        {"config": {"items": ""}},
        {"config": {"items": True}},
        {"config": {"items": ["context", "history"]}},
        {"items": "context_and_history"},  # right value, wrong place
        {"config": "context_and_history"},
    ],
)
def test_the_item_capability_probe_needs_an_explicit_affirmative_announcement(event) -> None:
    """Fail CLOSED, exactly like ``announces_one_shot_arming``: anything short of
    the announced value means this gateway gets no items at all.

    The direction matters here even more than it does for arming. A wrong guess
    about arming costs the politeness fix; a wrong guess here would send frames
    a gateway never agreed to parse, against a schema nobody upstream has seen.
    """
    assert duplex.announces_conversation_items(event) is False


def test_the_item_capability_probe_recognises_the_announced_value() -> None:
    announced = {
        "type": "session.created",
        "config": {duplex.ITEMS_CONFIG_KEY: duplex.ITEMS_MODE_CONTEXT_AND_HISTORY},
    }
    assert duplex.announces_conversation_items(announced) is True


# --- criterion 1: the fourth frame kind, on a real socket ---------------------


def test_h20_the_send_surface_is_four_frame_kinds_once_the_item_channel_is_used() -> None:
    """Criterion 1, behaviourally: append + response.create + the item frame.

    The AST pins above widen from two event kinds to three in this same change
    (h20, citing decision c28); this is the other half — what actually leaves
    the client over a full duplex exchange with a re-seed in play. Session
    config still rides the connect URL, which is why the SURFACE is four kinds
    while the EVENT family is three.
    """
    source = _Source()
    source.offer(_chunk())
    reseed = _Reseed(_SUMMARY, _TURN)
    with FakeRealtimeServer(
        Scenario.DUPLEX_HAPPY_PATH, announce_conversation_items=True, wait_timeout=_TIMEOUT
    ) as server:
        client = _session(
            server,
            read_audio=source,
            reseed=reseed,
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

    assert set(_sent_event_types(server)) == {wire.APPEND_EVENT_TYPE, _ARM, _ITEM}
    assert wire.OPCODE_BINARY not in server.received_opcodes
    assert client.supports_conversation_items is True
    assert client.items_sent == 2
    assert reseed.calls == 1


def test_an_items_disposition_survives_the_wire_as_context_or_history() -> None:
    """The ONE distinction the schema exists to carry, checked end to end.

    lobes#170 item 2's constraint is that ephemeral CONTEXT must be
    distinguishable from a HISTORY turn, because the floor auto-appends both
    roles already and items landing beside those auto-appends would duplicate
    and drift. A client that sent both dispositions as one kind would pass every
    other test in this section — so the harness sorts them, and this asserts the
    sort.
    """
    reseed = _Reseed(_SUMMARY, _TURN)
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM,
        announce_conversation_items=True,
        close_after_frames=10_000,
        wait_timeout=10.0,
    ) as server:
        client = _session(server, reseed=reseed, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: len(server.items_received) >= 2)
        finally:
            client.close()

    assert server.context_items == [("system", _SUMMARY.text)]
    assert server.history_items == [("user", _TURN.text)]
    assert [item["type"] for item in server.items_received] == [wire.ITEM_TYPE_MESSAGE] * 2


# --- criterion 2 (c44/h29): no item support -> ONE named drop + the degrade ---


def test_c44_a_gateway_without_item_support_yields_one_items_unsupported_drop(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """Criterion 2: never half-deploy, and say so exactly once.

    Every gateway shipping today announces nothing about items — parity is
    parked upstream — so this is not a hypothetical branch, it is the deployed
    one. The layer must not send a frame the gateway never agreed to parse, must
    not go silent about it, and must not bury the journal under one line per
    item: ONE named ``items-unsupported`` drop for the session, however many
    items were declined (:attr:`items_declined` carries the count).

    The degrade is task t9's already-shipped leg: the connect-time
    ``system_prompt`` still reaches the gateway on the connect URL, so the
    session keeps whatever context that channel can carry. Pinned here against
    the handshake the fake server actually received, not against the client's
    own idea of its URL.
    """
    reseed = _Reseed(_SUMMARY, _TURN)
    with FakeRealtimeServer(Scenario.DUPLEX_HAPPY_PATH, wait_timeout=_TIMEOUT) as server:
        client = _session(
            server,
            reseed=reseed,
            system_prompt="Be brief.",
            backoff_initial_s=5.0,
            backoff_max_s=5.0,
        )
        client.start()
        try:
            assert _wait_until(lambda: client.responses >= 1)
        finally:
            client.close()

    assert client.supports_conversation_items is False
    assert server.items_received == [], "no item may reach a gateway that never announced one"
    assert _count_reason(sense_log, duplex.REASON_ITEMS_UNSUPPORTED) == 1
    assert client.items_declined == 2, "the attempt was counted, not just refused"
    assert client.items_sent == 0
    # ... and the degraded context channel is the one t9 shipped.
    assert _query(server.request_path or "").get("system_prompt") == ["Be brief."]
    assert set(_sent_event_types(server)) <= {wire.APPEND_EVENT_TYPE, _ARM}


def test_send_item_is_declined_and_counted_when_the_gateway_never_announced() -> None:
    """The degraded return value is the caller's own answer, not just a log line.

    The same shape :meth:`arm_once` already has, and for the same reason: a
    caller (task t12's scope injector, t13's snapshot producer) needs to know
    its context did not land, so it can say so on the export feed rather than
    reasoning as if the floor had been told.
    """
    client = _session(url=_dead_url())
    assert client.supports_conversation_items is False
    assert client.send_item(_SUMMARY) is False
    assert client.items_declined == 1
    assert client.items_sent == 0


def test_only_one_items_unsupported_line_is_logged_however_many_items_are_declined(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """Latched per session — the #99 journal-flood discipline this module already
    applies to every other repeating failure."""
    client = _session(url=_dead_url())
    for _ in range(5):
        client.send_item(_SUMMARY)
    assert client.items_declined == 5
    assert _count_reason(sense_log, duplex.REASON_ITEMS_UNSUPPORTED) == 1


# --- criterion 3 (c40/h25): re-seed BEFORE re-arm, on every reconnect --------


def test_c40_a_forced_session_drop_re_seeds_before_re_arming() -> None:
    """Criterion 3, and the ordering is the whole claim.

    A session close wipes the floor's ephemeral history (lobes
    ``_session.py``'s ``teardown``: ``self._history = []`` — close releases it
    all), so a reconnect that armed FIRST would let the gateway answer the next
    turn from an empty history: Gemma silently resets to amnesia, and nothing in
    any log says so. Ordering is therefore not an implementation detail, it is
    the claim — pinned on the SEQUENCE the server received, across two
    connections, because no per-kind counter can express "before".

    The mechanism that guarantees it is structural rather than a convention:
    the re-seed seam is consulted inside ``session.created`` handling and its
    items go out on that same worker turn, while the arm is a flag the NEXT
    pump sends. A caller cannot interleave them and a future edit cannot
    reorder them without moving the arm INTO the event handler, which this test
    would catch immediately.

    The drop is keyed on the ARM (``DROP_AFTER_ARM``) rather than on a frame
    count, so the trigger is the same event the claim is about: by the time it
    fires, everything the client sends before arming has provably arrived, on
    a loaded box as much as an idle one.
    """
    reseed = _Reseed(_SUMMARY, _TURN)
    with FakeRealtimeServer(
        Scenario.DROP_AFTER_ARM, announce_conversation_items=True, wait_timeout=_RECONNECT_TIMEOUT
    ) as server:
        client = _session(server, reseed=reseed)
        client.start()
        try:
            assert _wait_until(
                lambda: len(server.received_event_types) >= 6, timeout=_RECONNECT_TIMEOUT
            )
        finally:
            client.close()

    assert server.connections_accepted >= 2, "the forced drop and its reconnect both happened"
    assert server.received_event_types[:6] == [_ITEM, _ITEM, _ARM, _ITEM, _ITEM, _ARM]
    assert reseed.calls >= 2, "the seam is consulted once per SESSION, not once per process"
    assert client.reseeds >= 2
    # Both sessions received the whole seed, not a truncated one.
    assert server.context_items[:2] == [("system", _SUMMARY.text)] * 2
    assert server.history_items[:2] == [("user", _TURN.text)] * 2


def test_a_reseed_is_never_sent_to_a_gateway_that_did_not_announce_items(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """The re-seed path obeys the capability check too — it is not a back door.

    Worth its own test because the re-seed runs inside the session's own
    ``session.created`` handling rather than through the caller-thread
    :meth:`send_item`, so an implementation could plausibly have checked the
    capability in one place and not the other.
    """
    reseed = _Reseed(_SUMMARY, _TURN)
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=10.0
    ) as server:
        client = _session(server, reseed=reseed, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _established(client)
            assert _wait_until(lambda: client.items_declined >= 2)
        finally:
            client.close()

    assert server.items_received == []
    assert _count_reason(sense_log, duplex.REASON_ITEMS_UNSUPPORTED) == 1


def test_a_raising_reseed_seam_is_a_named_drop_and_the_session_still_arms(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """A broken mind must not cost the robot its voice.

    The seam is injected, so it can raise for reasons this module cannot
    anticipate. It runs on the worker thread, where an escaping exception would
    take the session down and start a reconnect that re-ran the same broken
    seam — a crash loop dressed as a network problem.
    """
    reseed = _Reseed(_SUMMARY, boom=True)
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM,
        announce_conversation_items=True,
        close_after_frames=10_000,
        wait_timeout=10.0,
    ) as server:
        client = _session(server, reseed=reseed, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _established(client)
            assert _wait_until(lambda: client.arms_sent >= 1)
        finally:
            client.close()

    assert _count_reason(sense_log, duplex.REASON_RESEED_FAILED) == 1
    assert server.items_received == []
    assert server.connections_accepted == 1, "the session survived its own broken seam"


# --- every other failure is named too, and none of them ends the session ------


def test_a_gateway_that_rejects_an_item_is_a_named_drop_and_the_session_survives(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """A refusal is an answer, not an outage (the schema is provisional, after all).

    A gateway that announces items and then rejects the shape we send is
    exactly what a version skew looks like, and it is the most likely way this
    provisional schema goes wrong in practice. The refusal arrives as a named
    ``error`` event, which this client already turns into a named drop — what
    this pins is that the session, the ears and the mouth all outlive it.
    """
    reseed = _Reseed(_SUMMARY)
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM,
        announce_conversation_items=True,
        reject_items=True,
        close_after_frames=10_000,
        wait_timeout=10.0,
    ) as server:
        client = _session(server, reseed=reseed, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: bool(server.rejected_items))
            assert _wait_until(lambda: _count_reason(sense_log, duplex.REASON_SERVER_ERROR) >= 1)
        finally:
            client.close()

    assert server.connections_accepted == 1
    assert client.sessions == 1


def test_an_invalid_item_is_refused_before_the_wire_and_never_becomes_a_frame(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """Fail closed on the caller's thread, so a bad item costs a drop, not a raise.

    The wire builder raises :class:`ValueError` for an unknown role or
    disposition (guessing one is the duplicate-and-drift failure the schema
    exists to prevent). That raise must never reach a caller: this surface is
    O(1) and non-raising like every other caller-thread method here.
    """
    client = _session(url=_dead_url())
    _items_announced(client)
    assert client.supports_conversation_items is True

    bogus = duplex.ConversationItem(role="robot", text="hello", disposition="context")
    assert client.send_item(bogus) is False
    assert client.send_item(duplex.ConversationItem("system", "hi", "ephemeral")) is False
    assert client.send_item(duplex.ConversationItem("system", "   ", "context")) is False
    assert _count_reason(sense_log, duplex.REASON_ITEM_INVALID) == 3
    assert client.items_sent == 0
    client.close()


def test_a_full_item_queue_evicts_the_oldest_and_names_it(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """A bounded queue that dropped silently would lose context invisibly."""
    client = _session(url=_dead_url(), item_maxsize=1)
    _items_announced(client)
    for index in range(4):
        assert client.send_item(duplex.ConversationItem.context(f"fact {index}")) is True
    assert _count_reason(sense_log, duplex.REASON_ITEM_QUEUE_FULL) == 1
    client.close()


def test_a_caller_pushed_item_reaches_the_gateway_on_the_next_pump() -> None:
    """The between-turns channel task t12/t13 push scopes and snapshots through."""
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM,
        announce_conversation_items=True,
        close_after_frames=10_000,
        wait_timeout=10.0,
    ) as server:
        client = _session(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: client.supports_conversation_items)
            assert client.send_item(duplex.ConversationItem.context("a person is at the desk"))
            assert _wait_until(lambda: bool(server.context_items))
        finally:
            client.close()

    assert server.context_items == [("system", "a person is at the desk")]
    assert client.items_sent == 1


# --- the provisional contract is written down where the next reader will look -


def test_the_item_channel_documents_its_provisional_contract_and_cites_c28() -> None:
    """h20's citation, pinned rather than trusted to a commit message.

    The widening is legible only if the reason travels with it: a reader who
    finds a fourth frame kind and no citation cannot tell a decision from a
    drift. Both the decision (c28) and the unshipped upstream ask (lobes#170
    item 2) have to be in the module's own docstring.
    """
    doc = " ".join((duplex.__doc__ or "").split()).lower()

    assert "conversation.item.create" in doc
    assert "c28" in doc, "the deliberate widening must cite the decision that took it"
    assert "lobes-cli#170" in doc, "and the upstream ask it tracks"
    assert "provisional" in doc
    assert "four" in doc, "the send surface is four frame kinds now, and says so"


def test_the_item_schema_restates_no_bound_of_its_own() -> None:
    """The layer validates nothing itself (the spec's own rule): an item's text
    is bounded by whoever produced it — ``ScopeLimits`` for a cognition scope,
    ``Limits.summary_max_chars`` for a rolling summary — and a bound copied here
    would be a second number to drift. The queue DEPTH is a bound this module
    genuinely owns (it is about this client's memory, not about content), so it
    lives in :class:`Limits` with every other one.
    """
    long_text = "x" * (duplex.MAX_VOICE_PROMPT_CHARS * 3)
    item = duplex.ConversationItem.context(long_text)
    assert item.valid is True
    assert "item_maxsize" in {field.name for field in dataclasses.fields(duplex.Limits)}


def test_a_reseed_seam_with_nothing_to_say_is_silent_and_not_a_failure(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """An empty re-seed is the ordinary cold-start case, not a fault.

    The layer's canonical history is empty the first time the robot comes up,
    and a drop line there would train an operator to ignore the one that
    matters.
    """
    reseed = _Reseed()
    client = _session(url=_dead_url(), reseed=reseed)
    _items_announced(client)

    assert reseed.calls == 1
    assert client.reseeds == 0
    assert client.items_sent == 0
    assert _count_reason(sense_log, duplex.REASON_ITEMS_UNSUPPORTED) == 0
    assert _count_reason(sense_log, duplex.REASON_ITEM_INVALID) == 0
    client.close()


def test_an_invalid_item_inside_a_reseed_is_named_and_the_rest_still_goes(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """One bad entry may not take the whole re-seed down with it.

    A re-seed is built from the layer's canonical history, so a single
    malformed turn in it must cost that turn — named — and not the session's
    entire memory. Everything offered here is invalid on purpose (a bad
    disposition, and something that is not an item at all) so nothing reaches a
    socket this test does not have.
    """
    reseed = _Reseed(
        duplex.ConversationItem("system", "seeded", "ephemeral"),
        "not an item at all",  # type: ignore[arg-type]
    )
    client = _session(url=_dead_url(), reseed=reseed)
    _items_announced(client)

    assert client.reseeds == 1, "the seam WAS consulted and did return something"
    assert client.items_sent == 0
    assert client.items_declined == 2
    assert _count_reason(sense_log, duplex.REASON_ITEM_INVALID) == 2
    client.close()
