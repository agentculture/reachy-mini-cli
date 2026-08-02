"""Offline tests for :mod:`tests.fake_realtime_server` (task t2).

Proves the harness itself behaves before task t3 builds a session client
against it: each :class:`~tests.fake_realtime_server.Scenario` does what it
claims, the bound port is ephemeral (parallel-safe), shutdown leaks no
threads, and every recording attribute populates. Every test drives the
harness through a REAL loopback socket using
:mod:`reachy.speech.realtime_wire`'s client-side helpers directly (no mock),
which cross-validates wave 1's primitives against real bytes on a real wire —
exactly what the module docstring promises.

Extended for the embodiment-layer plan's task t3 (2026-08-01) with the
``response.*`` family: the "--- response.* family" section below round-trips
``response.create`` arming plus every inbound ``response.*`` event this wire
now speaks, using the same :class:`_TestClient` (grown one
``send_response_create`` method) against the three new
:class:`~tests.fake_realtime_server.Scenario` members. No production session
client drives this family yet (that is a later task); these tests play the
client themselves, proving the codec + fake server pair is usable end to end
before one exists.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time

import pytest

from reachy.speech import realtime_wire as wire
from tests.fake_realtime_server import (
    DEFAULT_RESPONSE_AUDIO,
    DEFAULT_RESPONSE_CHUNK_BYTES,
    DEFAULT_RESPONSE_TEXT,
    DEFAULT_TRANSCRIPT,
    FakeRealtimeServer,
    Scenario,
)

_CONNECT_TIMEOUT = 5.0
#: The response.* "arm and wait" scenarios (embodiment-layer plan, task t3)
#: give the SERVER up to ``FakeRealtimeServer``'s own ``_DEFAULT_WAIT_TIMEOUT``
#: (5.0 s) to notice the client's ``response.create`` before proceeding
#: anyway — a CLIENT-side read bound equal to that value leaves zero margin
#: for scheduling delay under a fully-loaded ``pytest -n auto`` run (measured
#: flaky at 5.0/5.0; a real-world contended box can genuinely eat several
#: seconds of thread-scheduling latency on TOP of the server's own wait).
#: 15.0 s gives 3x headroom over the server's worst case while costing
#: nothing in the overwhelmingly common fast path, where every read returns
#: in well under a second.
_IO_TIMEOUT = 15.0


class _TestClient:
    """A minimal hand-rolled WS client for these tests only — built on
    :mod:`reachy.speech.realtime_wire`'s pure primitives plus a real socket,
    mirroring ``lobes-cli``'s own smoke-test client shape at a much smaller
    scope (this file only needs connect / send / recv-one-frame)."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        path: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple["_TestClient", int, dict[str, str]]:
        key = wire.make_sec_websocket_key()
        sock = socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT)
        sock.settimeout(_IO_TIMEOUT)
        client = cls(sock)
        request = wire.build_handshake_request(
            f"{host}:{port}", path, key, extra_headers=extra_headers
        )
        sock.sendall(request)
        head = client._read_until(b"\r\n\r\n")
        status, headers = wire.parse_response_head(head)
        if status == 101:
            assert wire.verify_accept_key(key, headers.get("sec-websocket-accept", ""))
        return client, status, headers

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed before the marker arrived")
            self._buf.extend(chunk)
        idx = self._buf.index(marker) + len(marker)
        head = bytes(self._buf[:idx])
        del self._buf[:idx]
        return head

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n))
            if not chunk:
                break
            self._buf.extend(chunk)
        take = min(n, len(self._buf))
        data = bytes(self._buf[:take])
        del self._buf[:take]
        return data

    def read_frame(self) -> tuple[bool, int, bytes]:
        return wire.read_frame(self._recv_exact)

    def read_event(self) -> dict:
        """Read one frame and assert it is a decodable JSON TEXT event."""
        fin, opcode, payload = self.read_frame()
        assert fin is True
        assert opcode == wire.OPCODE_TEXT
        event = wire.decode_event(payload)
        assert event is not None, f"payload did not decode as an event: {payload!r}"
        return event

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        self._sock.sendall(wire.build_frame(opcode, payload, mask=True))

    def send_append(self, pcm: bytes) -> None:
        self.send_frame(wire.OPCODE_TEXT, wire.build_append_event(pcm).encode("utf-8"))

    def send_response_create(self) -> None:
        self.send_frame(wire.OPCODE_TEXT, wire.build_response_create_event().encode("utf-8"))

    def close(self) -> None:
        """Drain any pending inbound bytes (e.g. the server's own trailing WS
        CLOSE frame this test didn't explicitly read) before closing the fd.

        Closing a socket that still has UNREAD data sitting in its receive
        buffer makes the OS send an abortive RST instead of a graceful FIN —
        and an RST can discard bytes THIS client already sent moments earlier
        but the peer hadn't read yet (reproduced live under load: a
        `ConnectionResetError` on the SERVER's next read, right after it had
        successfully read this client's first frame). A short, bounded drain
        avoids the hazard the way a real WS client's closing handshake does.
        """
        try:
            self._sock.settimeout(0.2)
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def _connect(server: FakeRealtimeServer, **headers: str) -> tuple[_TestClient, int, dict]:
    extra = dict(headers) if headers else None
    return _TestClient.connect(server.host, server.port, wire.REALTIME_PATH, extra_headers=extra)


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll *predicate* until it is truthy or *timeout* elapses (avoids flat sleeps
    racing the reader thread — the recorded attributes populate asynchronously)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# --- lifecycle: ephemeral port, context manager, clean shutdown ------------------


def test_two_servers_bind_different_ephemeral_ports() -> None:
    """Parallel-safety criterion: nothing hardcodes a port."""
    with FakeRealtimeServer() as one, FakeRealtimeServer() as two:
        assert one.port != 0
        assert two.port != 0
        assert one.port != two.port


def test_context_manager_starts_and_cleanly_stops_with_no_leaked_threads() -> None:
    before = threading.active_count()
    with FakeRealtimeServer() as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.close()
    # stop() joins the accept thread and every per-connection thread.
    assert threading.active_count() <= before


def test_url_property_matches_host_and_port_and_the_realtime_path() -> None:
    with FakeRealtimeServer() as server:
        assert server.url == f"ws://{server.host}:{server.port}/v1/realtime"


# --- happy path -------------------------------------------------------------------


def test_happy_path_sequence_and_configurable_transcript() -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH, transcript="hello robot") as server:
        client, status, headers = _connect(server, Authorization="Bearer any-token-is-fine-here")
        assert status == 101
        assert "sec-websocket-accept" in headers

        created = client.read_event()
        assert created["type"] == "session.created"
        assert "session_id" in created
        assert created["session_id"]
        assert created["config"]["input_audio_format"] == "pcm16"

        started = client.read_event()
        assert started["type"] == "input_audio_buffer.speech_started"

        stopped = client.read_event()
        assert stopped["type"] == "input_audio_buffer.speech_stopped"
        assert stopped["item_id"] == started["item_id"]

        completed = client.read_event()
        assert completed["type"] == "conversation.item.input_audio_transcription.completed"
        assert completed["text"] == "hello robot"
        assert completed["item_id"] == started["item_id"]

        # A graceful close frame follows.
        fin, opcode, _payload = client.read_frame()
        assert fin is True
        assert opcode == wire.OPCODE_CLOSE

        client.close()

    # The handshake really was verified: a valid Sec-WebSocket-Key (else the
    # 101 would never have arrived) and the Authorization header we sent.
    assert server.connections_accepted == 1
    assert server.handshake_headers is not None
    assert server.handshake_headers["authorization"] == "Bearer any-token-is-fine-here"


def test_happy_path_default_transcript_is_the_documented_default() -> None:
    with FakeRealtimeServer() as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        client.read_event()  # speech_started
        client.read_event()  # speech_stopped
        completed = client.read_event()
        assert completed["text"] == DEFAULT_TRANSCRIPT
        client.close()


def test_happy_path_works_with_no_authorization_header_at_all() -> None:
    """No ``require_bearer_token`` configured -> auth is not required at all
    (mirrors the real gateway's documented default)."""
    with FakeRealtimeServer() as server:
        client, status, _headers = _connect(server)  # no Authorization header
        assert status == 101
        client.close()


def test_session_created_echoes_the_requested_input_sample_rate() -> None:
    with FakeRealtimeServer() as server:
        client, status, _headers = _TestClient.connect(
            server.host, server.port, "/v1/realtime?input_sample_rate=16000"
        )
        assert status == 101
        created = client.read_event()
        assert created["config"]["input_sample_rate"] == 16000
        assert server.last_input_sample_rate == 16000
        client.close()


# --- 401 / 426 handshake refusals --------------------------------------------------


def test_unauthorized_scenario_refuses_with_401_regardless_of_headers() -> None:
    with FakeRealtimeServer(Scenario.UNAUTHORIZED) as server:
        client, status, headers = _connect(server, Authorization="Bearer whatever")
        assert status == 401
        assert headers.get("www-authenticate") == "Bearer"
        client.close()
    assert server.refusals == [(401, "unauthorized")]
    assert server.connections_accepted == 0


def test_require_bearer_token_401s_on_missing_authorization() -> None:
    with FakeRealtimeServer(require_bearer_token="the-real-key") as server:
        client, status, _headers = _connect(server)  # no Authorization at all
        assert status == 401
        client.close()


def test_require_bearer_token_401s_on_a_wrong_token() -> None:
    with FakeRealtimeServer(require_bearer_token="the-real-key") as server:
        client, status, _headers = _connect(server, Authorization="Bearer nope")
        assert status == 401
        client.close()


def test_require_bearer_token_accepts_the_matching_token() -> None:
    with FakeRealtimeServer(require_bearer_token="the-real-key") as server:
        client, status, _headers = _connect(server, Authorization="Bearer the-real-key")
        assert status == 101
        client.close()


def test_plain_get_with_no_upgrade_headers_is_refused_with_426() -> None:
    """No scenario selection needed — 426 is intrinsic to a non-upgrade request,
    on every scenario, exactly like the real gateway's ``is_websocket_upgrade`` gate."""
    with FakeRealtimeServer() as server:
        sock = socket.create_connection((server.host, server.port), timeout=_CONNECT_TIMEOUT)
        sock.settimeout(_IO_TIMEOUT)
        sock.sendall(
            f"GET {wire.REALTIME_PATH} HTTP/1.1\r\nHost: {server.host}\r\n\r\n".encode("latin-1")
        )
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(4096)
        status, _headers = wire.parse_response_head(head)
        assert status == 426
        sock.close()
    assert server.refusals == [(426, "upgrade_required")]


# --- close mid-stream ---------------------------------------------------------------


def test_close_mid_stream_drops_the_connection_abruptly_after_n_frames() -> None:
    with FakeRealtimeServer(Scenario.CLOSE_MID_STREAM, close_after_frames=2) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created

        client.send_append(b"\x00\x01")
        client.send_append(b"\x02\x03")

        # No graceful CLOSE frame arrives — the socket just dies (EOF or reset).
        def drain_until_dead() -> None:
            for _ in range(5):
                client.read_frame()

        with pytest.raises((wire.FrameReadError, ConnectionError, OSError)):
            drain_until_dead()
        client.close()

    assert len(server.append_payloads) >= 2


# --- ping / pong ----------------------------------------------------------------------


def test_ping_expect_pong_scenario_records_a_pong_that_answers_the_ping() -> None:
    with FakeRealtimeServer(Scenario.PING_EXPECT_PONG, pong_wait_s=2.0) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created

        fin, opcode, payload = client.read_frame()
        assert fin is True
        assert opcode == wire.OPCODE_PING
        client.send_frame(wire.OPCODE_PONG, payload)

        started = client.read_event()
        assert started["type"] == "input_audio_buffer.speech_started"
        client.close()

    assert server.ping_sent_count == 1
    assert server.pong_count == 1


def test_ping_expect_pong_scenario_proceeds_even_when_no_pong_ever_arrives() -> None:
    with FakeRealtimeServer(Scenario.PING_EXPECT_PONG, pong_wait_s=0.2) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created

        fin, opcode, _payload = client.read_frame()
        assert opcode == wire.OPCODE_PING
        # Deliberately never send a PONG back.

        started = client.read_event()  # the scenario continues regardless
        assert started["type"] == "input_audio_buffer.speech_started"
        client.close()

    assert server.ping_sent_count == 1
    assert server.pong_count == 0
    assert server.wait_for_pong(timeout=0) is False


def test_happy_path_never_sends_a_ping() -> None:
    """The documented "no separate never-ping scenario" design decision."""
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        for _ in range(4):
            client.read_event()
        client.close()
    assert server.ping_sent_count == 0
    assert wire.OPCODE_PING not in server.received_opcodes


# --- malformed JSON ----------------------------------------------------------------------


def test_malformed_json_scenario_sends_a_frame_that_fails_to_decode() -> None:
    with FakeRealtimeServer(Scenario.MALFORMED_JSON) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created

        fin, opcode, payload = client.read_frame()
        assert fin is True
        assert opcode == wire.OPCODE_TEXT
        assert wire.decode_event(payload) is None
        with pytest.raises(json.JSONDecodeError):
            json.loads(payload)
        client.close()


# --- named error events -------------------------------------------------------------------


def test_error_vad_unavailable_scenario() -> None:
    with FakeRealtimeServer(Scenario.ERROR_VAD_UNAVAILABLE) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        error = client.read_event()
        assert error["type"] == "error"
        assert error["code"] == "vad_unavailable"
        assert error["message"]
        client.close()


def test_error_stt_forward_failed_scenario_arrives_after_a_committed_turn() -> None:
    with FakeRealtimeServer(Scenario.ERROR_STT_FORWARD_FAILED) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        started = client.read_event()
        assert started["type"] == "input_audio_buffer.speech_started"
        stopped = client.read_event()
        assert stopped["type"] == "input_audio_buffer.speech_stopped"
        error = client.read_event()
        assert error["type"] == "error"
        assert error["code"] == "stt_forward_failed"
        assert error["item_id"] == started["item_id"]
        client.close()


# --- observability: frames / opcodes / append payloads / malformed audio -------------------


def test_received_frames_and_opcodes_record_only_text_zero_binary() -> None:
    with FakeRealtimeServer() as server:
        client, status, _headers = _connect(server)
        assert status == 101
        pcm = bytes(range(256)) * 4
        client.send_append(pcm)
        for _ in range(4):
            client.read_event()
        client.close()
        assert _wait_until(lambda: len(server.append_payloads) >= 1)

    assert wire.OPCODE_BINARY not in server.received_opcodes
    assert wire.OPCODE_TEXT in server.received_opcodes
    assert server.append_payloads == [pcm]


def test_malformed_append_audio_field_is_counted_not_silently_dropped() -> None:
    with FakeRealtimeServer() as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        bad_event = json.dumps({"type": "input_audio_buffer.append", "audio": "not-base64!!"})
        client.send_frame(wire.OPCODE_TEXT, bad_event.encode("utf-8"))
        # Give the (independent) reader thread time to record it — bounded, never a hang.
        assert _wait_until(lambda: server.malformed_append_count > 0)
        client.close()

    assert server.malformed_append_count == 1
    assert server.append_payloads == []


def test_every_well_formed_append_payload_round_trips_as_valid_base64_pcm16() -> None:
    with FakeRealtimeServer() as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        chunks = [b"\x01\x02\x03\x04", b"", bytes(range(256))]
        for chunk in chunks:
            client.send_append(chunk)
        for _ in range(3):
            client.read_event()
        client.close()
        assert _wait_until(lambda: len(server.append_payloads) >= len(chunks))

    assert server.append_payloads == chunks
    for payload in server.append_payloads:
        # Round-trips through the exact wire codec a real client would use.
        assert base64.b64decode(base64.b64encode(payload)) == payload
    assert server.malformed_append_count == 0


def test_sent_events_records_every_event_this_server_sent() -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH, transcript="x") as server:
        client, status, _headers = _connect(server)
        assert status == 101
        for _ in range(4):
            client.read_event()
        client.close()

    types = [event["type"] for event in server.sent_events]
    assert types == [
        "session.created",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "conversation.item.input_audio_transcription.completed",
    ]


def test_last_session_and_item_ids_are_populated_and_opaque_strings() -> None:
    with FakeRealtimeServer() as server:
        client, status, _headers = _connect(server)
        assert status == 101
        for _ in range(4):
            client.read_event()
        client.close()

    assert isinstance(server.last_session_id, str)
    assert server.last_session_id
    assert isinstance(server.last_item_id, str)
    assert server.last_item_id


# --- scenario accepts either the enum or its plain string value -----------------------------


def test_scenario_selector_accepts_a_plain_string() -> None:
    with FakeRealtimeServer(scenario="error_vad_unavailable") as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        error = client.read_event()
        assert error["code"] == "vad_unavailable"
        client.close()


def test_unknown_scenario_string_raises_value_error() -> None:
    with pytest.raises(ValueError):
        FakeRealtimeServer(scenario="not-a-real-scenario")


# --- stop() is idempotent and safe to call without ever starting -----------------------------


def test_stop_without_start_does_not_raise() -> None:
    server = FakeRealtimeServer()
    server.stop(timeout=1.0)  # never started — must be a clean no-op


def test_stop_is_idempotent() -> None:
    server = FakeRealtimeServer()
    server.start()
    server.stop(timeout=2.0)
    server.stop(timeout=2.0)  # calling twice must not raise


# --- response.* family (embodiment-layer plan, task t3) ---------------------------


def test_response_happy_path_sequence_and_default_text() -> None:
    with FakeRealtimeServer(Scenario.RESPONSE_HAPPY_PATH) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created

        client.send_response_create()

        created = client.read_event()
        assert created["type"] == "response.created"
        assert created["response_id"] == server.last_response_id
        assert created["response_id"]

        text_done = client.read_event()
        assert text_done["type"] == "response.text.done"
        assert text_done["response_id"] == created["response_id"]
        assert text_done["text"] == DEFAULT_RESPONSE_TEXT

        deltas = []
        event = client.read_event()
        while event["type"] == "response.audio.delta":
            deltas.append(event)
            event = client.read_event()
        done = event
        assert done["type"] == "response.done"
        assert done["response_id"] == created["response_id"]

        client.close()

    # Multiple chunks (the DEFAULT_RESPONSE_CHUNK_BYTES default splits
    # DEFAULT_RESPONSE_AUDIO into more than one) reassemble CONTIGUOUSLY —
    # order preserved, nothing dropped, nothing duplicated.
    assert len(deltas) > 1
    assembled = b"".join(base64.b64decode(delta["delta"]) for delta in deltas)
    assert assembled == DEFAULT_RESPONSE_AUDIO
    assert server.response_create_count == 1


def test_response_happy_path_honours_custom_text_and_audio_and_chunk_size() -> None:
    audio = bytes(range(32, 32 + 20))  # 20 bytes, deliberately not a chunk_bytes multiple
    with FakeRealtimeServer(
        Scenario.RESPONSE_HAPPY_PATH,
        response_text="a custom scripted reply",
        response_audio=audio,
        response_chunk_bytes=6,
    ) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        client.send_response_create()
        client.read_event()  # response.created
        text_done = client.read_event()
        assert text_done["text"] == "a custom scripted reply"

        deltas = []
        event = client.read_event()
        while event["type"] == "response.audio.delta":
            deltas.append(event)
            event = client.read_event()
        assert event["type"] == "response.done"
        client.close()

    # ceil(20 / 6) == 4 chunks, the last one short — order-preserving reassembly.
    assert len(deltas) == 4
    assembled = b"".join(base64.b64decode(delta["delta"]) for delta in deltas)
    assert assembled == audio


def test_response_happy_path_proceeds_even_if_response_create_never_arrives() -> None:
    """Mirrors PING_EXPECT_PONG's "make the wait observable, never hang the
    connection" contract: a client that connects but never arms still gets a
    deterministic, bounded scenario (the point being the WAIT is what is
    tested here, not that arming is required for a session to function)."""
    with FakeRealtimeServer(Scenario.RESPONSE_HAPPY_PATH, wait_timeout=0.2) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        # Deliberately never send response.create.
        created = client.read_event()
        assert created["type"] == "response.created"
        client.close()
    assert server.response_create_count == 0
    assert server.wait_for_response_create(timeout=0) is False


def test_response_interrupted_delivers_one_partial_chunk_then_truncates() -> None:
    with FakeRealtimeServer(Scenario.RESPONSE_INTERRUPTED) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        client.send_response_create()

        client.read_event()  # response.created
        client.read_event()  # response.text.done
        delta = client.read_event()
        assert delta["type"] == "response.audio.delta"

        interrupted = client.read_event()
        assert interrupted["type"] == "response.interrupted"
        assert interrupted["response_id"] == delta["response_id"]
        assert interrupted["truncated"] is True

        # A graceful close follows — never a response.done for an interrupted reply.
        fin, opcode, _payload = client.read_frame()
        assert fin is True
        assert opcode == wire.OPCODE_CLOSE
        client.close()

    types = [event["type"] for event in server.sent_events]
    assert "response.done" not in types
    assert types.count("response.audio.delta") == 1


def test_response_audio_delta_malformed_scenario_decodes_the_envelope_but_not_the_field() -> None:
    """decode_event() (the wire-shape codec) never raises — this event
    decodes cleanly as an object with a ``type``. The malformedness is one
    level deeper, in the ``"delta"`` field's CONTENT, exactly mirroring how
    :attr:`~tests.fake_realtime_server.FakeRealtimeServer.malformed_append_count`
    already covers the inbound direction: a caller extracting PCM must guard
    its own ``base64.b64decode`` call, and that guard must never raise past it."""
    with FakeRealtimeServer(Scenario.RESPONSE_AUDIO_DELTA_MALFORMED) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        client.send_response_create()
        client.read_event()  # response.created
        client.read_event()  # response.text.done

        delta_event = client.read_event()
        assert delta_event["type"] == "response.audio.delta"
        assert isinstance(delta_event["delta"], str)

        decoded: bytes | None
        try:
            decoded = base64.b64decode(delta_event["delta"], validate=True)
        except ValueError:
            decoded = None
        assert decoded is None

        # A graceful close follows — the malformed event does not hang the session.
        fin, opcode, _payload = client.read_frame()
        assert fin is True
        assert opcode == wire.OPCODE_CLOSE
        client.close()


def test_response_hold_before_done_withholds_done_until_released_and_pings_meanwhile() -> None:
    """The hold scenario's two promises (foreground-Gemma plan, task t6).

    Every audio delta arrives BEFORE ``response.done`` is even sent — which is
    what lets a duplex client prove it speaks chunk groups as they arrive
    rather than accumulating the whole reply — and the hold keeps PINGing
    throughout, which is what lets that same client prove a blocked playback
    sink never starves its keepalive.
    """
    with FakeRealtimeServer(
        Scenario.RESPONSE_HOLD_BEFORE_DONE,
        response_audio=bytes(range(24)),
        response_chunk_bytes=8,
        hold_ping_interval_s=0.02,
    ) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        client.send_response_create()
        client.read_event()  # response.created
        client.read_event()  # response.text.done

        deltas = []
        pings = 0
        for _ in range(3):
            deltas.append(client.read_event())
        assert [event["type"] for event in deltas] == ["response.audio.delta"] * 3

        # response.done is NOT here yet: the next frames are keepalive PINGs.
        while pings < 2:
            _fin, opcode, _payload = client.read_frame()
            assert opcode == wire.OPCODE_PING, "response.done arrived before the release"
            pings += 1

        server.release_response_done()
        event = client.read_frame()
        while event[1] == wire.OPCODE_PING:
            event = client.read_frame()
        decoded = wire.decode_event(event[2])
        assert decoded is not None and decoded["type"] == "response.done"
        client.close()

    assert server.ping_sent_count >= 2
    assembled = b"".join(base64.b64decode(delta["delta"]) for delta in deltas)
    assert assembled == bytes(range(24))


def test_release_response_done_is_idempotent_and_safe_before_the_hold_runs() -> None:
    server = FakeRealtimeServer(Scenario.RESPONSE_HOLD_BEFORE_DONE)
    server.release_response_done()
    server.release_response_done()  # idempotent, and safe with nothing started
    server.stop(timeout=1.0)


def test_response_create_count_and_wait_for_response_create_observability() -> None:
    with FakeRealtimeServer(Scenario.RESPONSE_HAPPY_PATH) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        assert server.wait_for_response_create(timeout=0) is False
        client.send_response_create()
        assert server.wait_for_response_create(timeout=5.0) is True
        for _ in range(2 + DEFAULT_RESPONSE_CHUNK_BYTES):  # generous upper bound
            event = client.read_event()
            if event["type"] == "response.done":
                break
        client.close()
    assert server.response_create_count == 1


def test_last_response_id_is_populated_and_opaque() -> None:
    with FakeRealtimeServer(Scenario.RESPONSE_HAPPY_PATH) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        client.send_response_create()
        event = client.read_event()
        while event["type"] != "response.done":
            event = client.read_event()
        client.close()
    assert isinstance(server.last_response_id, str)
    assert server.last_response_id
    assert server.last_response_id != server.last_session_id
    assert server.last_response_id != server.last_item_id


# --- the client-side send surface, over a LIVE round trip (h13) -------------------


def test_the_live_send_surface_is_exactly_append_and_response_create() -> None:
    """Complements the AST-level pin in ``test_realtime_wire.py``
    (``test_the_wire_modules_outbound_frame_type_family_is_exactly_two_members``):
    drives a real client through BOTH outbound frame kinds against the fake
    server and checks the server only ever decoded those two ``type`` values —
    a live wire-level pin alongside the static source-level one."""
    with FakeRealtimeServer(Scenario.RESPONSE_HAPPY_PATH) as server:
        client, status, _headers = _connect(server)
        assert status == 101
        client.read_event()  # session.created
        client.send_append(b"\x01\x02\x03\x04")
        client.send_response_create()
        event = client.read_event()
        while event["type"] != "response.done":
            event = client.read_event()
        client.close()

    seen_types: set[str] = set()
    for opcode, payload in server.received_frames:
        if opcode != wire.OPCODE_TEXT:
            continue
        decoded = wire.decode_event(payload)
        if decoded is not None:
            seen_types.add(decoded["type"])
    assert seen_types == {wire.APPEND_EVENT_TYPE, wire.RESPONSE_CREATE_EVENT_TYPE}
