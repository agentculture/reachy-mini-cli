"""Offline tests for :mod:`reachy.speech.realtime` (task t3).

Every test drives the REAL :class:`~reachy.speech.realtime.RealtimeTranscriber`
over a real loopback socket against :class:`tests.fake_realtime_server.
FakeRealtimeServer` (task t2) — no mock of the wire, no live fleet, no new
dependency. The one branch the harness cannot script (an inbound ``response.*``
event, which the ears-only server never emits) is exercised through the
client's private dispatcher, and says so where it happens.

Two harness properties shape how these tests are written, and both are the
reason for the ``_client`` helper's "construct, do not start" contract:

* The scripted server sends its whole event sequence **and a graceful CLOSE**
  within a millisecond of the handshake, so ``client.connected`` is a window
  far too short to poll for. Every wait therefore keys on a CUMULATIVE counter
  (``client.sessions``, ``server.append_payloads``, ``client.utterances``),
  never on an instantaneous state.
* Audio submitted before :meth:`~reachy.speech.realtime.RealtimeTranscriber.
  start` is queued and flushed by the first session, which is what makes the
  append assertions deterministic — combined with a generous ``stale_after_s``
  so a loaded ``pytest -n auto`` box can never age a chunk out mid-connect.

Timeouts everywhere: every wait is bounded by :data:`_TIMEOUT`, so a regression
that wedges the worker fails the test instead of hanging the suite.
"""

from __future__ import annotations

import dataclasses
import logging
import socket
import threading
import time

import numpy as np
import pytest

from reachy.cli._errors import CliError
from reachy.speech import realtime_wire as wire
from reachy.speech.realtime import (
    NO_KEY_SENTINEL,
    OPENAI_API_KEY_ENV,
    OPENAI_URL_BASE_ENV,
    REALTIME_API_KEY_ENV,
    REALTIME_URL_ENV,
    REASON_CONNECT_FAILED,
    REASON_EMPTY_TRANSCRIPT,
    REASON_HANDSHAKE_REFUSED,
    REASON_MALFORMED_EVENT,
    REASON_QUEUE_FULL,
    REASON_SESSION_DOWN,
    REASON_STREAM_CLOSED,
    REASON_STT_FORWARD_FAILED,
    REASON_UTTERANCE_QUEUE_FULL,
    REASON_VAD_UNAVAILABLE,
    TRANSCRIPTION_COMPLETED,
    RealtimeTranscriber,
    Utterance,
    connect_url,
    resolve_realtime_api_key,
    resolve_realtime_base_url,
)
from tests.fake_realtime_server import FakeRealtimeServer, Scenario

_TIMEOUT = 5.0
_RATE = 16000
#: Long enough that no scheduling delay can age a pre-queued chunk out.
_NEVER_STALE = 120.0

_ENV_VARS = (REALTIME_URL_ENV, REALTIME_API_KEY_ENV, OPENAI_URL_BASE_ENV, OPENAI_API_KEY_ENV)


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


def _client(server: FakeRealtimeServer | None = None, **kwargs) -> RealtimeTranscriber:
    """A CONSTRUCTED (not started) client, pointed at *server* unless given a url."""
    if server is not None:
        kwargs.setdefault("url", server.url)
    kwargs.setdefault("sample_rate", _RATE)
    kwargs.setdefault("backoff_initial_s", 0.02)
    kwargs.setdefault("backoff_max_s", 0.05)
    kwargs.setdefault("stale_after_s", _NEVER_STALE)
    return RealtimeTranscriber(**kwargs)


def _established(client: RealtimeTranscriber, sessions: int = 1) -> bool:
    """Wait on the CUMULATIVE session counter, never on the instantaneous state."""
    return _wait_until(lambda: client.sessions >= sessions)


def _take(client: RealtimeTranscriber, timeout: float = _TIMEOUT) -> Utterance | None:
    holder: list[Utterance] = []

    def _got() -> bool:
        utterance = client.take_utterance()
        if utterance is not None:
            holder.append(utterance)
            return True
        return False

    _wait_until(_got, timeout=timeout)
    return holder[0] if holder else None


def _pcm16(samples: int = 160) -> np.ndarray:
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
    """An ephemeral port that is reserved then released — connects are refused."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _dead_url() -> str:
    return f"ws://127.0.0.1:{_dead_port()}/v1/realtime"


class _PinnedPortServer(FakeRealtimeServer):
    """A :class:`FakeRealtimeServer` bound to a KNOWN port.

    The stock harness always binds ``:0`` (ephemeral, parallel-safe), which is
    exactly right for every other test — but the session-down latch test needs
    ONE address that first refuses connections and later accepts them, which is
    impossible when the accepting server picks its own port after the fact.
    Only :meth:`start`'s bind differs.
    """

    def __init__(self, *args, port: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pinned_port = port

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, self._pinned_port))
        sock.listen(8)
        sock.settimeout(self._io_timeout)
        self._sock = sock
        self._port = self._pinned_port
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="fake-realtime-accept", daemon=True
        )
        self._accept_thread.start()


@pytest.fixture()
def sense_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="reachy.sense")
    return caplog


# --------------------------------------------------------------------------- #
# happy path                                                                   #
# --------------------------------------------------------------------------- #


def test_happy_path_yields_exactly_one_utterance_carrying_the_transcript() -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH, transcript="hello robot") as server:
        # A long backoff: the happy-path script ends in a graceful close, and a
        # prompt reconnect would replay the whole scenario and deliver a SECOND
        # transcript — a property of the harness, not of the client.
        client = _client(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        try:
            client.submit_audio(_pcm16())
            utterance = _take(client)
            assert utterance is not None
            assert utterance.text == "hello robot"
            assert isinstance(utterance.t, float) and utterance.t > 0.0
            assert utterance.item_id == server.last_item_id
            assert utterance.session_id == server.last_session_id
            assert client.take_utterance() is None
            assert client.utterances == 1
        finally:
            client.close()


def test_only_text_frames_are_ever_sent_and_appends_carry_base64_pcm16() -> None:
    chunk = _pcm16()
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _client(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        try:
            assert client.submit_audio(chunk) is True
            assert _take(client) is not None
            assert _wait_until(lambda: len(server.append_payloads) >= 1)
        finally:
            client.close()

    assert wire.OPCODE_BINARY not in server.received_opcodes
    assert wire.OPCODE_TEXT in server.received_opcodes
    assert server.append_payloads == [_expected_bytes(chunk)]
    assert server.malformed_append_count == 0


def test_raw_pcm16_bytes_and_int16_arrays_are_accepted_verbatim() -> None:
    raw = b"\x01\x02\x03\x04\x05\x06"
    ints = np.array([0, 1, -1, 32767], dtype=np.int16)
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _client(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        try:
            assert client.submit_audio(raw) is True
            assert client.submit_audio(ints) is True
            assert _take(client) is not None
            assert _wait_until(lambda: len(server.append_payloads) >= 2)
        finally:
            client.close()

    assert server.append_payloads == [raw, ints.astype("<i2").tobytes()]


def test_the_session_never_sends_response_create() -> None:
    """Ears-only (#115 non-goal): nothing this client writes asks for a reply."""
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _client(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        try:
            client.submit_audio(_pcm16())
            assert _take(client) is not None
            assert _wait_until(lambda: len(server.append_payloads) >= 1)
        finally:
            client.close()

    types = []
    for opcode, payload in server.received_frames:
        if opcode != wire.OPCODE_TEXT:
            continue
        event = wire.decode_event(payload)
        if event is not None:
            types.append(event["type"])
    assert types  # something really was sent
    assert "response.create" not in types
    assert all(not name.startswith("response.") for name in types)


def test_an_inbound_response_event_is_ignored_without_error() -> None:
    """The ears-only server never emits ``response.*``, so this drives the
    dispatcher directly — the one branch the harness cannot script."""
    client = _client(url=_dead_url())
    try:
        client._dispatch_event({"type": "response.audio.delta", "delta": "irrelevant"})
        client._dispatch_event({"type": "some.unknown.event"})
    finally:
        client.close()
    assert client.ignored_events == 2
    assert client.take_utterance() is None


def test_the_sample_rate_rides_the_connect_url_and_is_not_hardcoded() -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _client(server, sample_rate=48000, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _established(client)
        finally:
            client.close()
    assert server.last_input_sample_rate == 48000
    assert server.request_path == "/v1/realtime?input_sample_rate=48000"


def test_set_sample_rate_reconnects_so_the_session_config_follows_the_mic() -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _client(server, sample_rate=_RATE)
        client.start()
        try:
            assert _established(client)
            assert _wait_until(lambda: server.last_input_sample_rate == _RATE)
            client.set_sample_rate(24000)
            assert _wait_until(lambda: server.last_input_sample_rate == 24000)
        finally:
            client.close()


def test_set_sample_rate_on_a_LIVE_session_reconnects_with_no_session_down_drop(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """The rate rides the connect URL, so a late-learned mic rate (t5) costs a
    new session — but an INTENTIONAL one: nothing failed, so nothing is named
    a drop and no backoff is served.

    ``CLOSE_MID_STREAM`` with a frame target it will never reach is the one
    scenario that holds a session OPEN, which is what makes the live-session
    branch reachable at all.
    """
    # wait_timeout is how long each held session lingers after the client has
    # left it, so it is kept short: it is pure test wall-clock, and the
    # reconnect it must outlast takes milliseconds.
    with FakeRealtimeServer(
        Scenario.CLOSE_MID_STREAM, close_after_frames=10_000, wait_timeout=1.0
    ) as server:
        client = _client(server, sample_rate=_RATE)
        client.start()
        try:
            assert _established(client, 1)
            assert _wait_until(lambda: client.connected)
            client.set_sample_rate(24000)
            assert client.sample_rate == 24000
            assert _established(client, 2)
            assert _wait_until(lambda: server.last_input_sample_rate == 24000)
        finally:
            client.close()

    assert _count_reason(sense_log, REASON_SESSION_DOWN) == 0
    assert client.connect_failures == 0


# --------------------------------------------------------------------------- #
# failure scenarios — each a NAMED drop, none an exception                     #
# --------------------------------------------------------------------------- #


def test_unauthorized_handshake_is_a_named_refusal_and_never_raises(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    with FakeRealtimeServer(Scenario.UNAUTHORIZED) as server:
        client = _client(server)
        client.start()
        try:
            assert _wait_until(lambda: client.connect_failures >= 2)
            # The caller's thread keeps working throughout: no raise, no block.
            assert client.submit_audio(_pcm16()) in (True, False)
            assert client.take_utterance() is None
        finally:
            client.close()

    assert _count_reason(sense_log, REASON_HANDSHAKE_REFUSED) == 1
    assert any("401" in message for message in _messages(sense_log))
    assert _count_reason(sense_log, REASON_SESSION_DOWN) == 1
    assert server.refusals[0] == (401, "unauthorized")


def test_a_non_upgrade_request_is_refused_426_and_named(
    monkeypatch: pytest.MonkeyPatch, sense_log: pytest.LogCaptureFixture
) -> None:
    """426 is a property of the REQUEST, so the only way to watch the client
    handle it is to make its handshake a plain GET for one test."""

    def _plain_get(host: str, path: str, key: str, extra_headers=None) -> bytes:
        return f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode("latin-1")

    monkeypatch.setattr(wire, "build_handshake_request", _plain_get)

    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _client(server)
        client.start()
        try:
            assert _wait_until(lambda: client.connect_failures >= 1)
        finally:
            client.close()

    assert _count_reason(sense_log, REASON_HANDSHAKE_REFUSED) == 1
    assert any("426" in message for message in _messages(sense_log))
    assert server.refusals and server.refusals[0] == (426, "upgrade_required")


def test_close_mid_stream_is_a_named_drop_and_a_backoff_reconnect_follows(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    with FakeRealtimeServer(Scenario.CLOSE_MID_STREAM, close_after_frames=1) as server:
        client = _client(server)
        try:
            deadline = time.monotonic() + _TIMEOUT
            while time.monotonic() < deadline and server.connections_accepted < 2:
                client.submit_audio(_pcm16())
                time.sleep(0.02)
            assert server.connections_accepted >= 2
        finally:
            client.close()

    assert _count_reason(sense_log, REASON_STREAM_CLOSED) >= 1
    assert _count_reason(sense_log, REASON_SESSION_DOWN) >= 1


def test_malformed_json_event_is_a_named_drop_and_never_raises(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    with FakeRealtimeServer(Scenario.MALFORMED_JSON) as server:
        client = _client(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: _count_reason(sense_log, REASON_MALFORMED_EVENT) >= 1)
            assert client.take_utterance() is None
        finally:
            client.close()
    assert server.connections_accepted >= 1


def test_vad_unavailable_is_its_own_named_drop(sense_log: pytest.LogCaptureFixture) -> None:
    with FakeRealtimeServer(Scenario.ERROR_VAD_UNAVAILABLE) as server:
        client = _client(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: _count_reason(sense_log, REASON_VAD_UNAVAILABLE) >= 1)
        finally:
            client.close()
    assert _count_reason(sense_log, REASON_STT_FORWARD_FAILED) == 0
    assert server.connections_accepted >= 1


def test_stt_forward_failed_is_its_own_named_drop(sense_log: pytest.LogCaptureFixture) -> None:
    with FakeRealtimeServer(Scenario.ERROR_STT_FORWARD_FAILED) as server:
        client = _client(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _wait_until(lambda: _count_reason(sense_log, REASON_STT_FORWARD_FAILED) >= 1)
        finally:
            client.close()
    assert _count_reason(sense_log, REASON_VAD_UNAVAILABLE) == 0


def test_the_two_server_error_codes_are_distinct_reason_strings() -> None:
    assert REASON_VAD_UNAVAILABLE != REASON_STT_FORWARD_FAILED
    assert REASON_VAD_UNAVAILABLE == "vad-unavailable"
    assert REASON_STT_FORWARD_FAILED == "stt-forward-failed"


# --------------------------------------------------------------------------- #
# the session-down LATCH (#99 discipline)                                      #
# --------------------------------------------------------------------------- #


def test_session_down_logs_once_on_entry_then_hearing_resumes_without_a_restart(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """Refuse-then-accept on ONE address: many failed attempts, ONE drop line."""
    port = _dead_port()
    client = _client(
        url=f"ws://127.0.0.1:{port}/v1/realtime", backoff_initial_s=0.01, backoff_max_s=0.02
    )
    client.start()
    try:
        # Phase 1 — nothing is listening. Let the worker fail repeatedly.
        assert _wait_until(lambda: client.connect_failures >= 4)
        assert client.session_down is True
        # The whole point: N failures, ONE session-down line (and one cause).
        assert _count_reason(sense_log, REASON_SESSION_DOWN) == 1
        assert _count_reason(sense_log, REASON_CONNECT_FAILED) == 1

        # Phase 2 — the same address starts answering. Hearing resumes with no
        # restart of the client and no new object.
        with _PinnedPortServer(Scenario.HAPPY_PATH, transcript="back again", port=port):
            utterance = _take(client)
            assert utterance is not None
            assert utterance.text == "back again"
    finally:
        client.close()

    assert client.sessions >= 1


def test_a_full_audio_queue_drops_once_not_once_per_chunk(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """A queue that cannot drain (nothing is listening) must not flood the log."""
    client = _client(url=_dead_url(), audio_maxsize=2, backoff_initial_s=0.05, backoff_max_s=0.05)
    try:
        accepted = [client.submit_audio(_pcm16()) for _ in range(8)]
    finally:
        client.close()

    assert accepted[:2] == [True, True]
    assert accepted[2:] == [False] * 6
    assert _count_reason(sense_log, REASON_QUEUE_FULL) == 1
    assert client.dropped == 6


# --------------------------------------------------------------------------- #
# keepalive                                                                    #
# --------------------------------------------------------------------------- #


def test_a_server_ping_is_answered_with_a_pong() -> None:
    with FakeRealtimeServer(Scenario.PING_EXPECT_PONG, pong_wait_s=_TIMEOUT) as server:
        client = _client(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert server.wait_for_pong(timeout=_TIMEOUT) is True
            assert _take(client) is not None
        finally:
            client.close()

    assert server.ping_sent_count >= 1
    assert server.pong_count >= 1
    assert client.pongs_sent >= 1


# --------------------------------------------------------------------------- #
# configuration                                                                #
# --------------------------------------------------------------------------- #


def test_openai_env_alone_targets_the_gateway_realtime_route_with_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH, require_bearer_token="sk-gateway") as server:
        monkeypatch.setenv(OPENAI_URL_BASE_ENV, f"http://{server.host}:{server.port}")
        monkeypatch.setenv(OPENAI_API_KEY_ENV, "sk-gateway")
        client = _client(url=None, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _established(client)
        finally:
            client.close()

    assert server.request_path == f"/v1/realtime?input_sample_rate={_RATE}"
    assert server.handshake_headers is not None
    assert server.handshake_headers["authorization"] == "Bearer sk-gateway"
    assert server.connections_accepted == 1


def test_realtime_url_env_overrides_the_openai_base(monkeypatch: pytest.MonkeyPatch) -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        monkeypatch.setenv(OPENAI_URL_BASE_ENV, f"http://127.0.0.1:{_dead_port()}")
        monkeypatch.setenv(REALTIME_URL_ENV, server.url)
        client = _client(url=None, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _established(client)
        finally:
            client.close()
    assert server.connections_accepted == 1


def test_realtime_api_key_env_overrides_the_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH, require_bearer_token="sk-realtime") as server:
        monkeypatch.setenv(OPENAI_API_KEY_ENV, "sk-openai")
        monkeypatch.setenv(REALTIME_API_KEY_ENV, "sk-realtime")
        client = _client(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _established(client)
        finally:
            client.close()
    assert server.handshake_headers["authorization"] == "Bearer sk-realtime"


def test_explicit_arguments_beat_every_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REALTIME_URL_ENV, _dead_url())
    monkeypatch.setenv(REALTIME_API_KEY_ENV, "sk-env")
    with FakeRealtimeServer(Scenario.HAPPY_PATH, require_bearer_token="sk-explicit") as server:
        client = _client(server, api_key="sk-explicit", backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _established(client)
        finally:
            client.close()
    assert server.handshake_headers["authorization"] == "Bearer sk-explicit"


def test_no_key_anywhere_sends_no_authorization_header() -> None:
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _client(server, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            assert _established(client)
        finally:
            client.close()
    assert "authorization" not in (server.handshake_headers or {})


def test_resolve_realtime_base_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_realtime_base_url("ws://box:8001/v1/realtime") == "ws://box:8001/v1/realtime"
    # An http(s) value is read as a GATEWAY BASE and mapped onto the route.
    assert resolve_realtime_base_url("https://box") == "wss://box/v1/realtime"
    monkeypatch.setenv(OPENAI_URL_BASE_ENV, "http://gateway:8001")
    assert resolve_realtime_base_url() == "ws://gateway:8001/v1/realtime"
    monkeypatch.setenv(REALTIME_URL_ENV, "ws://other:9/v1/realtime")
    assert resolve_realtime_base_url() == "ws://other:9/v1/realtime"


def test_resolve_realtime_base_url_defaults_to_the_local_gateway() -> None:
    assert resolve_realtime_base_url() == "ws://localhost:8001/v1/realtime"


def test_an_unusable_url_scheme_is_a_clean_setup_error() -> None:
    with pytest.raises(CliError):
        resolve_realtime_base_url("ftp://box/realtime")
    with pytest.raises(CliError):
        RealtimeTranscriber(url="ftp://box/realtime", sample_rate=_RATE)


def test_resolve_realtime_api_key_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_realtime_api_key() is None
    monkeypatch.setenv(OPENAI_API_KEY_ENV, "sk-openai")
    assert resolve_realtime_api_key() == "sk-openai"
    monkeypatch.setenv(REALTIME_API_KEY_ENV, "sk-realtime")
    assert resolve_realtime_api_key() == "sk-realtime"
    assert resolve_realtime_api_key("sk-explicit") == "sk-explicit"
    # Presence, not truthiness: an explicitly EMPTY realtime key means "no auth".
    monkeypatch.setenv(REALTIME_API_KEY_ENV, "")
    assert resolve_realtime_api_key() is None


def test_the_empty_sentinel_never_becomes_a_bearer_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EMPTY`` is the repo-wide "no key" placeholder, on every source of the key.

    Local OpenAI-compatible servers use the literal ``EMPTY`` for an
    unauthenticated endpoint, and ``speech/llm.py``, ``speech/tts.py``,
    ``stash/embeddings.py`` and ``forge/client.py`` all already honour it. The
    realtime client shares ``REACHY_OPENAI_API_KEY`` with those, so failing to
    honour it here would put a literal ``Authorization: Bearer EMPTY`` on the
    handshake of any box configured that way.
    """
    monkeypatch.setenv(OPENAI_API_KEY_ENV, NO_KEY_SENTINEL)
    assert resolve_realtime_api_key() is None
    monkeypatch.setenv(REALTIME_API_KEY_ENV, NO_KEY_SENTINEL)
    assert resolve_realtime_api_key() is None
    assert resolve_realtime_api_key(NO_KEY_SENTINEL) is None


def test_no_authorization_header_is_sent_for_the_empty_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: an ``EMPTY``-keyed box completes a handshake with NO auth header."""
    monkeypatch.delenv(REALTIME_URL_ENV, raising=False)
    monkeypatch.setenv(OPENAI_API_KEY_ENV, NO_KEY_SENTINEL)
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        with RealtimeTranscriber(sample_rate=16000, url=server.url) as client:
            _wait_until(lambda: client.sessions >= 1)
        headers = {k.lower(): v for k, v in (server.handshake_headers or {}).items()}
        assert "authorization" not in headers


def test_connect_url_sets_the_sample_rate_query_without_losing_the_path() -> None:
    assert connect_url("ws://box:8001/v1/realtime", 16000) == (
        "ws://box:8001/v1/realtime?input_sample_rate=16000"
    )
    # An already-present value is replaced, never duplicated.
    assert connect_url("ws://box/v1/realtime?input_sample_rate=8000", 24000) == (
        "ws://box/v1/realtime?input_sample_rate=24000"
    )


# --------------------------------------------------------------------------- #
# lifecycle                                                                    #
# --------------------------------------------------------------------------- #


def test_close_is_idempotent_and_joins_the_worker_leaving_no_threads() -> None:
    before = threading.active_count()
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        client = _client(server)
        client.submit_audio(_pcm16())
        assert _take(client) is not None
        client.close()
        client.close()  # idempotent
        assert client.worker is not None
        assert client.worker.is_alive() is False
    assert _wait_until(lambda: threading.active_count() <= before, timeout=2.0)


def test_close_before_start_is_a_clean_noop() -> None:
    client = _client(url=_dead_url())
    client.close()
    client.close()
    assert client.worker is None
    assert client.submit_audio(_pcm16()) is False


def test_the_context_manager_starts_and_closes_the_worker() -> None:
    before = threading.active_count()
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        with RealtimeTranscriber(url=server.url, sample_rate=_RATE) as client:
            assert _established(client)
        assert client.worker is not None and client.worker.is_alive() is False
    assert _wait_until(lambda: threading.active_count() <= before, timeout=2.0)


def test_start_is_idempotent() -> None:
    client = _client(url=_dead_url())
    try:
        client.start()
        worker = client.worker
        client.start()
        assert client.worker is worker
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# the tick-thread contract                                                     #
# --------------------------------------------------------------------------- #


def test_submit_audio_never_raises_on_anything_the_mic_can_hand_over() -> None:
    client = _client(url=_dead_url())
    try:
        assert client.submit_audio(None) is False
        assert client.submit_audio(np.zeros(0, dtype=np.float32)) is False
        assert client.submit_audio(b"") is False
        assert client.submit_audio("not audio at all") is False
        assert client.submit_audio(object()) is False
        # (N, 2) stereo is coerced to mono, never interleaved into 2N samples.
        stereo = np.zeros((32, 2), dtype=np.float32)
        assert client.submit_audio(stereo) is True
        assert client.last_chunk_bytes == 64
    finally:
        client.close()


def test_submit_audio_is_bounded_work_on_the_calling_thread() -> None:
    """No socket, no lock held across I/O: a full queue returns promptly."""
    client = _client(url=_dead_url(), audio_maxsize=4)
    try:
        start = time.monotonic()
        for _ in range(200):
            client.submit_audio(_pcm16())
        elapsed = time.monotonic() - start
    finally:
        client.close()
    assert elapsed < 1.0


def test_take_utterance_returns_none_when_nothing_is_ready() -> None:
    client = _client(url=_dead_url())
    try:
        assert client.take_utterance() is None
    finally:
        client.close()


def test_the_utterance_callback_receives_the_same_utterance_as_the_queue() -> None:
    seen: list[Utterance] = []
    with FakeRealtimeServer(Scenario.HAPPY_PATH, transcript="ping") as server:
        client = _client(server, on_utterance=seen.append, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            utterance = _take(client)
        finally:
            client.close()

    assert utterance is not None and utterance.text == "ping"
    assert seen == [utterance]


def test_a_raising_utterance_callback_never_stops_the_session() -> None:
    def _boom(_utterance: Utterance) -> None:
        raise RuntimeError("callback fault")

    with FakeRealtimeServer(Scenario.HAPPY_PATH, transcript="still here") as server:
        client = _client(server, on_utterance=_boom, backoff_initial_s=5.0, backoff_max_s=5.0)
        client.start()
        try:
            utterance = _take(client)
        finally:
            client.close()
    assert utterance is not None and utterance.text == "still here"


def test_a_transcription_with_no_usable_text_is_a_named_drop(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    client = _client(url=_dead_url())
    try:
        client._dispatch_event({"type": TRANSCRIPTION_COMPLETED, "text": "   "})
        assert client.take_utterance() is None
    finally:
        client.close()
    assert _count_reason(sense_log, REASON_EMPTY_TRANSCRIPT) == 1
    assert client.utterances == 0


def test_a_full_utterance_queue_evicts_the_oldest_transcript(
    sense_log: pytest.LogCaptureFixture,
) -> None:
    """A stale transcript is worth less than a fresh one."""
    client = _client(url=_dead_url(), utterance_maxsize=1)
    try:
        client._dispatch_event({"type": TRANSCRIPTION_COMPLETED, "text": "first"})
        client._dispatch_event({"type": TRANSCRIPTION_COMPLETED, "text": "second"})
        newest = client.take_utterance()
        assert newest is not None and newest.text == "second"
        assert client.take_utterance() is None
    finally:
        client.close()
    assert _count_reason(sense_log, REASON_UTTERANCE_QUEUE_FULL) == 1


def test_the_openai_transcript_field_name_is_honoured_as_well_as_lobes_text() -> None:
    client = _client(url=_dead_url())
    try:
        client._dispatch_event({"type": TRANSCRIPTION_COMPLETED, "transcript": "words"})
        utterance = client.take_utterance()
    finally:
        client.close()
    assert utterance is not None and utterance.text == "words"


def test_audio_queued_long_before_a_session_is_discarded_as_stale() -> None:
    """A reconnect must not replay old sound into a server-side VAD.

    The chunk is queued while NOTHING is listening, and the server is brought
    up on that same (pinned) port afterwards, so it is unambiguously standing
    backlog by the time a session exists. Pointing the client at a LIVE server
    and submitting looked simpler but left the ordering to chance:
    ``submit_audio`` calls ``start()`` BEFORE its own ``put_nowait``, so under
    a loaded ``pytest -n auto`` the worker could connect and run
    ``_discard_stale_audio`` against a still-empty queue, after which the chunk
    was sent as ordinary live audio and this assertion failed. Observed in the
    wild (~1 run in 8) once the box was busy enough; the client was never at
    fault.
    """
    port = _dead_port()
    client = _client(
        url=f"ws://127.0.0.1:{port}/v1/realtime",
        stale_after_s=0.0,
        backoff_initial_s=0.01,
        backoff_max_s=0.02,
    )
    try:
        assert client.submit_audio(_pcm16()) is True
        assert _wait_until(lambda: client.connect_failures >= 1)
        with _PinnedPortServer(Scenario.HAPPY_PATH, port=port) as server:
            assert _take(client) is not None  # the session ran to completion
            assert server.append_payloads == []
    finally:
        client.close()


def test_the_utterance_record_is_frozen() -> None:
    utterance = Utterance(text="hi", t=1.0, item_id="item_1", session_id="sess_1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        utterance.text = "no"  # type: ignore[misc]
