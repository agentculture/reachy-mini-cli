"""An offline, in-process, loopback-socket fake for the lobes ``/v1/realtime`` wire.

Task t2 of the ``realtime-hearing-over-the-lobes-wire`` plan
(agentculture/reachy-mini-cli#115, lobes-cli#151/#149). This is TEST
INFRASTRUCTURE: task t3 imports :class:`FakeRealtimeServer` to drive
``reachy/speech/realtime.py`` (the session client, built next) through every
happy and failure path documented in the wire contract, with **no network, no
live fleet, and no new dependency** — stdlib only (``socket``, ``threading``,
``json``, ``base64``, ``time``, ``uuid``, ``struct``, ``urllib.parse``).

It reuses :mod:`reachy.speech.realtime_wire` (task t1, wave 1) for every wire
primitive rather than reimplementing RFC 6455 framing: :func:`~reachy.speech.
realtime_wire.compute_accept_key` for the handshake, :func:`~reachy.speech.
realtime_wire.build_frame` (called with ``mask=False`` — **a server MUST send
unmasked frames**, RFC 6455 SS5.1) to emit events, and :func:`~reachy.speech.
realtime_wire.read_frame` (which auto-unmasks client-to-server frames) to
receive them.

Scenario catalog
-----------------
One :class:`Scenario` member selects the server's entire behaviour for one
connection (pass it as ``FakeRealtimeServer(scenario=...)``):

- ``HAPPY_PATH`` (default) — accepts the handshake, then emits
  ``session.created`` -> ``input_audio_buffer.speech_started`` ->
  ``input_audio_buffer.speech_stopped`` ->
  ``conversation.item.input_audio_transcription.completed`` (with a
  configurable ``transcript`` string), then a graceful WebSocket close. It
  never sends a PING, so it doubles as the "server never pings" baseline a
  test can use to assert the ABSENCE of keepalive traffic — see
  ``PING_EXPECT_PONG`` below for the complementary "server pings and expects a
  pong" mode; there is no separate "never ping" scenario because that would be
  a no-op duplicate of this one.
- ``UNAUTHORIZED`` — refuses the handshake with ``401`` unconditionally
  (regardless of what ``Authorization`` header, if any, the client sent).
  Models "no bearer" and "bad bearer" with one selector; see
  ``require_bearer_token`` below for the complementary realistic mode where a
  SPECIFIC token is required and only a mismatching/missing one 401s.
- ``CLOSE_MID_STREAM`` — emits ``session.created``, waits (bounded by
  ``wait_timeout``) until it has received at least ``close_after_frames``
  frames from the client, then drops the TCP connection with **no** WebSocket
  CLOSE frame — an abrupt disconnect, not a clean shutdown.
- ``PING_EXPECT_PONG`` — emits ``session.created``, sends one PING frame,
  waits (bounded by ``pong_wait_s``) for a PONG to come back (recorded via
  :attr:`FakeRealtimeServer.pong_count` / :meth:`wait_for_pong`), then
  continues with the rest of the happy-path sequence regardless of whether the
  pong arrived — the scenario's job is to make the wait observable, not to
  fail the connection over it.
- ``MALFORMED_JSON`` — emits ``session.created``, then one TEXT frame whose
  payload is deliberately invalid JSON, then closes gracefully.
- ``ERROR_VAD_UNAVAILABLE`` — emits ``session.created``, then a named
  ``error`` event with ``code="vad_unavailable"``, then closes gracefully.
- ``ERROR_STT_FORWARD_FAILED`` — emits ``session.created`` ->
  ``speech_started`` -> ``speech_stopped``, then a named ``error`` event with
  ``code="stt_forward_failed"`` (mirroring the real point in the flow where a
  committed turn's forward to STT can fail), then closes gracefully.
- ``RESPONSE_HAPPY_PATH`` (embodiment-layer plan, task t3) — emits
  ``session.created``, then WAITS (bounded by ``wait_timeout``, proceeding
  regardless once it elapses — same "make the wait observable, not fail the
  connection" posture as ``PING_EXPECT_PONG``) for the client's
  ``response.create`` arming frame, then emits ``response.created`` ->
  ``response.text.done`` -> one ``response.audio.delta`` per
  ``response_chunk_bytes``-sized slice of ``response_audio`` (multiple
  chunks by default, so a test can prove the reassembled audio is
  CONTIGUOUS) -> ``response.done``, then a graceful close.
- ``RESPONSE_INTERRUPTED`` — the same arm-and-wait as ``RESPONSE_HAPPY_PATH``,
  then ``response.created`` -> ``response.text.done`` -> exactly ONE
  (partial) ``response.audio.delta`` -> ``response.interrupted``
  (``truncated=True``) instead of ``response.done`` — models a barge-in
  cutting a reply short.
- ``RESPONSE_AUDIO_DELTA_MALFORMED`` — the same arm-and-wait, then
  ``response.created`` -> ``response.text.done`` -> ONE
  ``response.audio.delta`` whose ``"delta"`` field is deliberately not valid
  base64 (a well-formed JSON envelope, exactly like ``MALFORMED_JSON`` is a
  well-formed frame but not valid JSON — the malformedness lives one level
  deeper, in the field content, mirroring how ``malformed_append_count``
  above already covers the inbound direction), then a graceful close.

Two failure modes are deliberately NOT separate ``Scenario`` members, because
they are properties of the REQUEST, not a server mood the harness picks:

- **HTTP 426** ("plain GET, no upgrade") fires for ANY scenario the instant a
  connection's request lacks a genuine WebSocket upgrade (mirrors
  ``lobes.gateway._realtime.is_websocket_upgrade`` byte for byte: both
  ``Upgrade: websocket`` and an ``upgrade`` token in ``Connection`` are
  required). Drive it by opening a raw socket and sending a plain
  ``GET /v1/realtime HTTP/1.1`` with no ``Upgrade``/``Connection`` headers —
  no scenario selection needed.
- **HTTP 401 for a specific expected token** is ``require_bearer_token=...``
  (a constructor argument, checked before every scenario dispatch including
  ``UNAUTHORIZED``, which short-circuits it): connect with a missing or wrong
  ``Authorization`` header and get 401; connect with the right one and the
  chosen ``scenario`` proceeds normally. With neither ``require_bearer_token``
  set nor ``scenario=UNAUTHORIZED``, no bearer is required at all — mirroring
  the real gateway's documented default (``GATEWAY_API_KEY`` unset -> the
  ``Authorization`` header is never even read).

Both refusal bodies are hand-mirrored from the real gateway
(``lobes.gateway.server``): 401 is the OpenAI-shaped
``{"error": {"message": ..., "code": "invalid_api_key"}}`` with a
``WWW-Authenticate: Bearer`` header; 426 names the fix in its message text.

Event shapes — confirmed vs. genuinely guessed
------------------------------------------------
The event field names/types below are **hand-mirrored from the real server's
own schema**, not guessed: ``lobes-cli``'s ``lobes/realtime/_session.py``
(``EventType``, the frozen ``*Event`` dataclasses, and ``event_to_dict``) and
``lobes/realtime/protocol.py`` (``gen_event_id``/``gen_session_id``/
``gen_item_id`` -> ``"event_" | "sess_" | "item_"`` + a 24-hex-char uuid4
suffix; ``timestamp_ms() = int(time.monotonic() * 1000)``). This is a hand
PORT in the same "cite, don't import" spirit as ``realtime_wire.py`` — a
snapshot that can drift if the real schema changes, not a live sync. Every
event carries ``type``/``session_id``/``event_id``/``timestamp_ms`` plus:

- ``session.created``: nested ``config`` (``input_audio_format``,
  ``input_sample_rate`` — echoed from the connect URL's own
  ``?input_sample_rate=`` query param, exactly as the real session config rides
  the connect URL per ``realtime_wire``'s own docstring —, ``channels``,
  ``turn_detection``, ``aec_mode``, ``system_prompt``).
- ``input_audio_buffer.speech_started`` / ``..._stopped``: ``item_id``,
  ``at_ms`` (an audio-stream-time integer), and (stopped only) ``reason``.
- ``conversation.item.input_audio_transcription.completed``: ``item_id``,
  ``text``.
- ``error``: ``code``, ``message``, ``item_id`` (``None`` unless tied to one).
- ``response.created``: ``response_id``, ``item_id`` (``None`` unless the
  reply answers one specific transcribed turn).
- ``response.text.done``: ``response_id``, ``text`` (the full reply, whole —
  the donor's generate call is not streamed onto this wire).
- ``response.audio.delta``: ``response_id``, ``delta`` (one already
  base64-encoded PCM16 chunk — mirrors ``Session.emit_audio_delta``'s simpler
  session-schema shape, NOT ``_wire.py``'s richer standalone
  ``serialize_audio_delta`` shape, which that module's own docstring says the
  live route does not actually send).
- ``response.done``: ``response_id`` only.
- ``response.interrupted``: ``response_id``, ``truncated`` (always ``True``
  here — a barge-in is the only trigger this harness models).

**What is genuinely made up, not mirrored, and why it's safe:** the *numeric*
``at_ms`` values and the *default* ``reason="silence"`` are placeholders —
this offline harness never runs real VAD, so there is no real audio-stream
clock to report. The exact ID *format* (``event_<24 hex>`` etc.) is included
for realism but a wire consumer MUST NOT depend on it: IDs are opaque by
contract, and if that turns out wrong the fix is in this file's event builders
only, never in the session client under test. Reconcile against a live run at
``docs/evidence/`` before tightening any assertion that depends on exact
numeric ``at_ms``/id-format values.

Threading and cleanup
----------------------
One accept thread per :class:`FakeRealtimeServer`, plus two threads per
accepted connection (a scripted "sender" — the accept thread's own handler —
and a "reader" that records every inbound frame and answers a client PING
with a PONG). Every socket operation is timeout-bounded
(``io_timeout``/``accept_timeout``/``wait_timeout``/``pong_wait_s``), so
nothing blocks the suite indefinitely; :meth:`stop` force-closes every live
socket (unblocking anything parked in ``recv``) and joins every thread with a
bounded timeout. Binds ``127.0.0.1:0`` (an ephemeral port) so parallel
``pytest -n auto`` workers never collide; use as a context manager
(``with FakeRealtimeServer(...) as server: ...``) so cleanup always runs, or
wrap it in a pytest fixture that ``yield``s inside a ``with`` block.

Observability (thread-safe reads, populated by the reader thread)
--------------------------------------------------------------------
- ``received_frames`` — ``list[tuple[int, bytes]]`` of every ``(opcode,
  payload)`` received, in arrival order (includes PING/PONG/CLOSE).
- ``received_opcodes`` — the same list's opcodes only, e.g. for asserting
  "only TEXT frames arrived, zero BINARY".
- ``append_payloads`` — ``list[bytes]``, the base64-decoded PCM for every
  well-formed ``input_audio_buffer.append`` TEXT frame received.
- ``malformed_append_count`` — count of ``input_audio_buffer.append`` events
  whose ``audio`` field failed to base64-decode (or was missing/non-string).
- ``pong_count`` / ``wait_for_pong(timeout)`` — how many PONG frames arrived,
  and a blocking wait for the next one.
- ``response_create_count`` / ``wait_for_response_create(timeout)`` — how many
  well-formed ``response.create`` frames arrived, and a blocking wait for the
  next one (embodiment-layer plan, task t3) — the arm-and-wait scenarios above
  poll this the same way ``CLOSE_MID_STREAM`` polls ``received_frames``.
- ``ping_sent_count``, ``connections_accepted``, ``handshake_headers``
  (lowercased dict from the most recently accepted handshake), ``request_path``,
  ``sent_events`` (every event this server sent, for debugging), ``refusals``
  (``list[tuple[int, str]]`` of ``(status, reason)`` for each non-101 response),
  ``last_response_id`` (the most recently generated ``resp_...`` id, mirroring
  ``last_session_id``/``last_item_id``).
"""

from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import time
import uuid
from enum import Enum
from urllib.parse import parse_qs, urlsplit

from reachy.speech import realtime_wire as wire

DEFAULT_HOST = "127.0.0.1"
DEFAULT_TRANSCRIPT = "The quick brown fox jumps over the lazy dog."
#: The scripted spoken reply's text (embodiment-layer plan, task t3).
DEFAULT_RESPONSE_TEXT = "This is a scripted spoken reply."
#: The scripted reply's PCM16 bytes — every byte value once, so a truncated or
#: reordered reassembly is easy to catch by eye as well as by assertion.
DEFAULT_RESPONSE_AUDIO = bytes(range(24))
#: Splits DEFAULT_RESPONSE_AUDIO into 3 deltas by default — plural on purpose,
#: so "the reassembled audio is contiguous" is an assertion about ORDER, not
#: just about there being exactly one chunk.
DEFAULT_RESPONSE_CHUNK_BYTES = 8

_DEFAULT_ACCEPT_TIMEOUT = 5.0
_DEFAULT_IO_TIMEOUT = 0.2
_DEFAULT_WAIT_TIMEOUT = 5.0
_DEFAULT_PONG_WAIT_S = 2.0
_MAX_HEAD_BYTES = 64 * 1024


class Scenario(str, Enum):
    """Selects one connection's entire scripted behaviour. See the module docstring."""

    HAPPY_PATH = "happy_path"
    UNAUTHORIZED = "unauthorized"
    CLOSE_MID_STREAM = "close_mid_stream"
    PING_EXPECT_PONG = "ping_expect_pong"
    MALFORMED_JSON = "malformed_json"
    ERROR_VAD_UNAVAILABLE = "error_vad_unavailable"
    ERROR_STT_FORWARD_FAILED = "error_stt_forward_failed"
    RESPONSE_HAPPY_PATH = "response_happy_path"
    RESPONSE_INTERRUPTED = "response_interrupted"
    RESPONSE_AUDIO_DELTA_MALFORMED = "response_audio_delta_malformed"


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _timestamp_ms() -> int:
    return int(time.monotonic() * 1000)


def _bearer_matches(expected: str, header_value: str | None) -> bool:
    """``True`` iff *header_value* is ``"Bearer <expected>"`` (scheme case-insensitive)."""
    if not header_value:
        return False
    parts = header_value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return parts[1] == expected


def _is_websocket_upgrade(headers: dict[str, str]) -> bool:
    """Mirrors ``lobes.gateway._realtime.is_websocket_upgrade``: both halves required."""
    upgrade = headers.get("upgrade", "").strip().lower()
    connection = headers.get("connection", "").lower()
    tokens = {token.strip() for token in connection.split(",")}
    return upgrade == "websocket" and "upgrade" in tokens


def _parse_request_head(head: bytes) -> tuple[str, dict[str, str]]:
    """Parse a raw HTTP request head into ``(request_line, lowercased_headers)``."""
    text = head.decode("latin-1", errors="replace")
    lines = [line for line in text.split("\r\n") if line]
    request_line = lines[0] if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return request_line, headers


class _ConnReader:
    """A byte-buffered reader over one connection socket.

    Serves both the raw HTTP handshake head (:meth:`read_until`) and, after
    the handshake, the WebSocket frame stream (:meth:`recv_exact`, the exact
    shape :func:`reachy.speech.realtime_wire.read_frame` requires) — the same
    buffer carries over so a client that pipelines its first frame right
    behind the handshake in one TCP segment never loses those bytes.
    """

    def __init__(self, conn: socket.socket, stop_event: threading.Event) -> None:
        self._conn = conn
        self._stop_event = stop_event
        self._buf = bytearray()

    def read_until(self, marker: bytes, deadline: float) -> bytes | None:
        while marker not in self._buf:
            if time.monotonic() > deadline:
                return None
            try:
                chunk = self._conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            self._buf.extend(chunk)
            if len(self._buf) > _MAX_HEAD_BYTES:
                return None
        idx = self._buf.index(marker) + len(marker)
        head = bytes(self._buf[:idx])
        del self._buf[:idx]
        return head

    def recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            if self._stop_event.is_set():
                break
            try:
                chunk = self._conn.recv(max(4096, n))
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            self._buf.extend(chunk)
        take = min(n, len(self._buf))
        data = bytes(self._buf[:take])
        del self._buf[:take]
        return data


class FakeRealtimeServer:
    """An offline loopback fake of the lobes ``/v1/realtime`` WebSocket route.

    See the module docstring for the full scenario catalog, event-shape
    provenance, and the observability attributes. Binds an ephemeral port on
    :meth:`start` (or context-manager entry) — :attr:`port`/:attr:`url` are
    valid only after that.
    """

    def __init__(
        self,
        scenario: Scenario | str = Scenario.HAPPY_PATH,
        *,
        host: str = DEFAULT_HOST,
        transcript: str = DEFAULT_TRANSCRIPT,
        response_text: str = DEFAULT_RESPONSE_TEXT,
        response_audio: bytes = DEFAULT_RESPONSE_AUDIO,
        response_chunk_bytes: int = DEFAULT_RESPONSE_CHUNK_BYTES,
        require_bearer_token: str | None = None,
        close_after_frames: int = 1,
        ping_payload: bytes = b"keepalive",
        pong_wait_s: float = _DEFAULT_PONG_WAIT_S,
        wait_timeout: float = _DEFAULT_WAIT_TIMEOUT,
        accept_timeout: float = _DEFAULT_ACCEPT_TIMEOUT,
        io_timeout: float = _DEFAULT_IO_TIMEOUT,
    ) -> None:
        self._scenario = Scenario(scenario)
        self._host = host
        self._transcript = transcript
        self._response_text = response_text
        self._response_audio = response_audio
        self._response_chunk_bytes = max(1, int(response_chunk_bytes))
        self._require_bearer_token = require_bearer_token
        self._close_after_frames = close_after_frames
        self._ping_payload = ping_payload
        self._pong_wait_s = pong_wait_s
        self._wait_timeout = wait_timeout
        self._accept_timeout = accept_timeout
        self._io_timeout = io_timeout

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pong_event = threading.Event()
        self._response_create_event = threading.Event()

        self._sock: socket.socket | None = None
        self._port = 0
        self._accept_thread: threading.Thread | None = None
        self._conn_threads: list[threading.Thread] = []
        self._live_sockets: list[socket.socket] = []

        self._received_frames: list[tuple[int, bytes]] = []
        self._append_payloads: list[bytes] = []
        self._malformed_append_count = 0
        self._pong_count = 0
        self._ping_sent_count = 0
        self._response_create_count = 0
        self._connections_accepted = 0
        self._handshake_headers: dict[str, str] | None = None
        self._request_path: str | None = None
        self._sent_events: list[dict] = []
        self._refusals: list[tuple[int, str]] = []
        self._last_session_id: str | None = None
        self._last_item_id: str | None = None
        self._last_response_id: str | None = None
        self._last_input_sample_rate: int | None = None

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, 0))
        sock.listen(8)
        sock.settimeout(self._io_timeout)
        self._sock = sock
        self._port = sock.getsockname()[1]
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="fake-realtime-accept", daemon=True
        )
        self._accept_thread.start()

    def stop(self, timeout: float = _DEFAULT_WAIT_TIMEOUT) -> None:
        """Force-close every live socket and join every thread, bounded by *timeout*."""
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._lock:
            sockets = list(self._live_sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=timeout)
        with self._lock:
            conn_threads = list(self._conn_threads)
        for thread in conn_threads:
            thread.join(timeout=timeout)

    def __enter__(self) -> "FakeRealtimeServer":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # --- addressing -----------------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    @property
    def host(self) -> str:
        return self._host

    @property
    def url(self) -> str:
        """``ws://<host>:<port>/v1/realtime`` — no query string (add your own)."""
        return f"ws://{self._host}:{self._port}{wire.REALTIME_PATH}"

    # --- observability (thread-safe reads) -------------------------------------

    @property
    def received_frames(self) -> list[tuple[int, bytes]]:
        with self._lock:
            return list(self._received_frames)

    @property
    def received_opcodes(self) -> list[int]:
        with self._lock:
            return [opcode for opcode, _payload in self._received_frames]

    @property
    def append_payloads(self) -> list[bytes]:
        with self._lock:
            return list(self._append_payloads)

    @property
    def malformed_append_count(self) -> int:
        with self._lock:
            return self._malformed_append_count

    @property
    def pong_count(self) -> int:
        with self._lock:
            return self._pong_count

    @property
    def response_create_count(self) -> int:
        with self._lock:
            return self._response_create_count

    @property
    def ping_sent_count(self) -> int:
        with self._lock:
            return self._ping_sent_count

    @property
    def connections_accepted(self) -> int:
        with self._lock:
            return self._connections_accepted

    @property
    def handshake_headers(self) -> dict[str, str] | None:
        with self._lock:
            return dict(self._handshake_headers) if self._handshake_headers is not None else None

    @property
    def request_path(self) -> str | None:
        with self._lock:
            return self._request_path

    @property
    def sent_events(self) -> list[dict]:
        with self._lock:
            return list(self._sent_events)

    @property
    def refusals(self) -> list[tuple[int, str]]:
        with self._lock:
            return list(self._refusals)

    @property
    def last_session_id(self) -> str | None:
        with self._lock:
            return self._last_session_id

    @property
    def last_item_id(self) -> str | None:
        with self._lock:
            return self._last_item_id

    @property
    def last_response_id(self) -> str | None:
        with self._lock:
            return self._last_response_id

    @property
    def last_input_sample_rate(self) -> int | None:
        with self._lock:
            return self._last_input_sample_rate

    def wait_for_pong(self, timeout: float | None = None) -> bool:
        """Block until a PONG has arrived (or *timeout* elapses). Returns whether one has."""
        return self._pong_event.wait(timeout=timeout)

    def wait_for_response_create(self, timeout: float | None = None) -> bool:
        """Block until a well-formed ``response.create`` frame has arrived.

        Mirrors :meth:`wait_for_pong`. Returns whether one has (or *timeout*
        elapsed first) — the arm-and-wait ``response_*`` scenarios use the
        same event internally rather than polling.
        """
        return self._response_create_event.wait(timeout=timeout)

    # --- accept loop --------------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop_event.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(self._io_timeout)
            thread = threading.Thread(
                target=self._handle_connection,
                args=(conn,),
                name="fake-realtime-conn",
                daemon=True,
            )
            with self._lock:
                self._conn_threads.append(thread)
                self._live_sockets.append(conn)
            thread.start()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            self._serve_one_session(conn)
        except (OSError, wire.FrameReadError):
            pass
        finally:
            with self._lock:
                if conn in self._live_sockets:
                    self._live_sockets.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    # --- one connection's handshake + scripted body --------------------------------

    def _serve_one_session(self, conn: socket.socket) -> None:
        send_lock = threading.Lock()
        reader = _ConnReader(conn, self._stop_event)
        deadline = time.monotonic() + self._accept_timeout
        head = reader.read_until(b"\r\n\r\n", deadline)
        if head is None:
            return

        request_line, headers = _parse_request_head(head)
        path = request_line.split(" ", 2)[1] if len(request_line.split(" ", 2)) >= 2 else ""
        with self._lock:
            self._request_path = path

        if self._scenario is Scenario.UNAUTHORIZED:
            self._refuse(conn, send_lock, 401, "unauthorized")
            return

        if self._require_bearer_token is not None and not _bearer_matches(
            self._require_bearer_token, headers.get("authorization")
        ):
            self._refuse(conn, send_lock, 401, "unauthorized")
            return

        if not _is_websocket_upgrade(headers):
            self._refuse(conn, send_lock, 426, "upgrade_required")
            return

        key = headers.get("sec-websocket-key")
        if not key:
            self._refuse(conn, send_lock, 400, "missing_sec_websocket_key")
            return

        accept = wire.compute_accept_key(key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("latin-1")
        try:
            conn.sendall(response)
        except OSError:
            return

        with self._lock:
            self._connections_accepted += 1
            self._handshake_headers = dict(headers)

        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        input_sample_rate = int(query.get("input_sample_rate", ["24000"])[0])
        with self._lock:
            self._last_input_sample_rate = input_sample_rate

        reader_done = threading.Event()
        reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(conn, reader, send_lock, reader_done),
            name="fake-realtime-reader",
            daemon=True,
        )
        reader_thread.start()
        try:
            self._run_scenario(conn, send_lock, input_sample_rate)
        finally:
            # No timeout here, deliberately: the sender script above (every
            # scenario) has zero dependency on client input and can finish
            # sending in well under a millisecond, while the reader thread
            # may still be legitimately waiting on a scheduling-delayed
            # client's in-flight frames — under heavy parallel-test load this
            # is not rare. A short bound here would force-close `conn` out
            # from under the reader thread's still-blocked `recv()` on the
            # SAME fd (a genuine cross-thread-close race, not just a slow
            # test — reproduced live: it silently discarded already-sent
            # client frames that simply hadn't been read yet). Waiting here
            # is safe precisely because the reader always terminates on its
            # own once the CLIENT closes its end (EOF -> FrameReadError ->
            # `done.set()`), and the true backstop for a test that forgets to
            # close its client is :meth:`stop`, which force-closes every live
            # socket (unblocking this exact wait) BEFORE it joins threads.
            reader_done.wait()

    def _refuse(
        self, conn: socket.socket, send_lock: threading.Lock, status: int, reason: str
    ) -> None:
        """Send an OpenAI-shaped 401, or a plain 426/400, and record the refusal.

        Mirrors ``lobes.gateway.server``'s own refusal shapes: 401 is
        ``{"error": {"message": ..., "code": "invalid_api_key"}}`` plus a
        ``WWW-Authenticate: Bearer`` header; 426 says what to do about it
        rather than pretending the route doesn't exist.
        """
        if status == 401:
            body = json.dumps(
                {
                    "error": {
                        "message": (
                            "Incorrect API key provided. You can find your API key "
                            "at your fleet's gateway config. Send it as "
                            "'Authorization: Bearer <key>'."
                        ),
                        "type": "invalid_api_key",
                        "code": "invalid_api_key",
                    }
                }
            ).encode("utf-8")
            status_text = "Unauthorized"
        elif status == 426:
            body = json.dumps(
                {
                    "error": {
                        "message": (
                            "/v1/realtime is a WebSocket route — send an "
                            "Upgrade: websocket handshake"
                        )
                    }
                }
            ).encode("utf-8")
            status_text = "Upgrade Required"
        else:
            body = json.dumps({"error": {"message": reason}}).encode("utf-8")
            status_text = "Bad Request"

        lines = [
            f"HTTP/1.1 {status} {status_text}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
        ]
        if status == 401:
            lines.append("WWW-Authenticate: Bearer")
        response = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
        with send_lock:
            try:
                conn.sendall(response)
            except OSError:
                pass
        with self._lock:
            self._refusals.append((status, reason))

    # --- reader thread: records every inbound frame, auto-pongs a client PING -------

    def _reader_loop(
        self,
        conn: socket.socket,
        reader: _ConnReader,
        send_lock: threading.Lock,
        done: threading.Event,
    ) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    _fin, opcode, payload = wire.read_frame(reader.recv_exact)
                except wire.FrameReadError:
                    break
                with self._lock:
                    self._received_frames.append((opcode, payload))
                if opcode == wire.OPCODE_TEXT:
                    self._record_text_frame(payload)
                elif opcode == wire.OPCODE_PING:
                    self._send_frame(conn, send_lock, wire.OPCODE_PONG, payload)
                elif opcode == wire.OPCODE_PONG:
                    with self._lock:
                        self._pong_count += 1
                    self._pong_event.set()
                elif opcode == wire.OPCODE_CLOSE:
                    break
        except OSError:
            pass
        finally:
            done.set()

    def _record_text_frame(self, payload: bytes) -> None:
        event = wire.decode_event(payload)
        if event is None:
            return
        event_type = event.get("type")
        if event_type == wire.APPEND_EVENT_TYPE:
            self._record_append_event(event)
        elif event_type == wire.RESPONSE_CREATE_EVENT_TYPE:
            with self._lock:
                self._response_create_count += 1
            self._response_create_event.set()

    def _record_append_event(self, event: dict) -> None:
        audio_field = event.get("audio")
        decoded: bytes | None = None
        if isinstance(audio_field, str):
            try:
                decoded = base64.b64decode(audio_field, validate=True)
            except ValueError:
                decoded = None
        with self._lock:
            if decoded is not None:
                self._append_payloads.append(decoded)
            else:
                self._malformed_append_count += 1

    # --- send helpers ------------------------------------------------------------

    def _send_frame(
        self, conn: socket.socket, send_lock: threading.Lock, opcode: int, payload: bytes = b""
    ) -> None:
        frame = wire.build_frame(opcode, payload, mask=False)
        with send_lock:
            try:
                conn.sendall(frame)
            except OSError:
                pass

    def _send_event(self, conn: socket.socket, send_lock: threading.Lock, event: dict) -> None:
        with self._lock:
            self._sent_events.append(event)
        self._send_frame(conn, send_lock, wire.OPCODE_TEXT, json.dumps(event).encode("utf-8"))

    def _send_raw_text(self, conn: socket.socket, send_lock: threading.Lock, text: str) -> None:
        self._send_frame(conn, send_lock, wire.OPCODE_TEXT, text.encode("utf-8"))

    def _graceful_close(self, conn: socket.socket, send_lock: threading.Lock) -> None:
        """Send a WS CLOSE frame, then half-close the WRITE side only.

        Deliberately does NOT ``conn.close()`` the whole socket here. Every
        happy/error/malformed scenario's send-script has zero dependency on
        client input, so it can reach this point — and used to fully close
        the fd — within milliseconds of the handshake, while the (fully
        independent) reader thread may still be draining
        ``input_audio_buffer.append`` frames the client sent moments earlier.
        Closing the fd from THIS thread while the reader thread is blocked in
        ``recv()`` on the SAME fd is a real cross-thread-close race (not just
        a slow-scheduling flake): it can abort the connection before those
        already-in-flight bytes are read, and under heavy parallel-test load
        this reproduced as a genuine, reproducible failure (not fixed by
        raising a test's wait timeout — the bytes were gone, not merely
        delayed). Shutting down only the WRITE half leaves the READ half open,
        so the reader thread keeps recording until it sees the client's own
        EOF/close — real closure happens exactly once, in
        ``_handle_connection``'s ``finally``, after ``_serve_one_session``'s
        UNBOUNDED ``reader_done.wait()`` returns (see that call site's own
        docstring-comment for why an unbounded wait there is safe).
        """
        self._send_frame(conn, send_lock, wire.OPCODE_CLOSE, struct.pack("!H", 1000))
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _abrupt_close(self, conn: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass

    # --- event builders (hand-mirrored from lobes-cli's _session.py schema) --------

    def _event_session_created(self, session_id: str, input_sample_rate: int) -> dict:
        return {
            "type": "session.created",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "config": {
                "input_audio_format": "pcm16",
                "input_sample_rate": input_sample_rate,
                "channels": 1,
                "turn_detection": "server_vad",
                "aec_mode": "none",
                "system_prompt": None,
            },
        }

    def _event_speech_started(self, session_id: str, item_id: str, at_ms: int = 0) -> dict:
        return {
            "type": "input_audio_buffer.speech_started",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "item_id": item_id,
            "at_ms": at_ms,
        }

    def _event_speech_stopped(
        self, session_id: str, item_id: str, at_ms: int = 640, reason: str = "silence"
    ) -> dict:
        return {
            "type": "input_audio_buffer.speech_stopped",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "item_id": item_id,
            "at_ms": at_ms,
            "reason": reason,
        }

    def _event_transcription_completed(self, session_id: str, item_id: str) -> dict:
        return {
            "type": "conversation.item.input_audio_transcription.completed",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "item_id": item_id,
            "text": self._transcript,
        }

    def _event_error(
        self, session_id: str, code: str, message: str, item_id: str | None = None
    ) -> dict:
        return {
            "type": "error",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "code": code,
            "message": message,
            "item_id": item_id,
        }

    # --- event builders: the response.* family (embodiment-layer plan, task t3) ----
    # Mirrors lobes-cli's Session.begin_response / complete_response_text /
    # emit_audio_delta / complete_response / interrupt_response — the SESSION
    # SCHEMA shape (_session.py), not the richer standalone shape _wire.py's
    # own serialize_audio_delta builds, which that module's docstring says the
    # live route does not actually send.

    def _event_response_created(
        self, session_id: str, response_id: str, item_id: str | None
    ) -> dict:
        return {
            "type": "response.created",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "response_id": response_id,
            "item_id": item_id,
        }

    def _event_response_text_done(self, session_id: str, response_id: str, text: str) -> dict:
        return {
            "type": "response.text.done",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "response_id": response_id,
            "text": text,
        }

    def _event_response_audio_delta(self, session_id: str, response_id: str, pcm: bytes) -> dict:
        return {
            "type": "response.audio.delta",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "response_id": response_id,
            "delta": base64.b64encode(pcm).decode("ascii"),
        }

    def _event_response_audio_delta_malformed(self, session_id: str, response_id: str) -> dict:
        """A well-formed envelope carrying a deliberately non-base64 ``delta``.

        Mirrors ``MALFORMED_JSON`` one level deeper: the JSON itself decodes
        fine (:func:`~reachy.speech.realtime_wire.decode_event` sees a valid
        object with a ``type``), but the field CONTENT a caller must
        base64-decode does not — the outbound-direction counterpart of
        :attr:`malformed_append_count` above.
        """
        return {
            "type": "response.audio.delta",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "response_id": response_id,
            "delta": "***not valid base64***",
        }

    def _event_response_done(self, session_id: str, response_id: str) -> dict:
        return {
            "type": "response.done",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "response_id": response_id,
        }

    def _event_response_interrupted(
        self, session_id: str, response_id: str, truncated: bool = True
    ) -> dict:
        return {
            "type": "response.interrupted",
            "session_id": session_id,
            "event_id": _gen_id("event"),
            "timestamp_ms": _timestamp_ms(),
            "response_id": response_id,
            "truncated": truncated,
        }

    # --- scenario dispatch ---------------------------------------------------------

    def _run_scenario(
        self, conn: socket.socket, send_lock: threading.Lock, input_sample_rate: int
    ) -> None:
        session_id = _gen_id("sess")
        item_id = _gen_id("item")
        with self._lock:
            self._last_session_id = session_id
            self._last_item_id = item_id

        self._send_event(
            conn, send_lock, self._event_session_created(session_id, input_sample_rate)
        )

        scenario = self._scenario

        if scenario is Scenario.MALFORMED_JSON:
            self._send_raw_text(conn, send_lock, "{this is not valid json,,, ]")
            self._graceful_close(conn, send_lock)
            return

        if scenario is Scenario.ERROR_VAD_UNAVAILABLE:
            self._send_event(
                conn,
                send_lock,
                self._event_error(session_id, "vad_unavailable", "server_vad is unavailable"),
            )
            self._graceful_close(conn, send_lock)
            return

        if scenario is Scenario.PING_EXPECT_PONG:
            self._pong_event.clear()
            self._send_frame(conn, send_lock, wire.OPCODE_PING, self._ping_payload)
            with self._lock:
                self._ping_sent_count += 1
            self._pong_event.wait(timeout=self._pong_wait_s)
            self._send_event(conn, send_lock, self._event_speech_started(session_id, item_id))
            self._send_event(conn, send_lock, self._event_speech_stopped(session_id, item_id))
            self._send_event(
                conn, send_lock, self._event_transcription_completed(session_id, item_id)
            )
            self._graceful_close(conn, send_lock)
            return

        if scenario is Scenario.CLOSE_MID_STREAM:
            deadline = time.monotonic() + self._wait_timeout
            target = self._close_after_frames
            while time.monotonic() < deadline:
                if len(self.received_frames) >= target:
                    break
                time.sleep(0.02)
            self._abrupt_close(conn)
            return

        if scenario is Scenario.ERROR_STT_FORWARD_FAILED:
            self._send_event(conn, send_lock, self._event_speech_started(session_id, item_id))
            self._send_event(conn, send_lock, self._event_speech_stopped(session_id, item_id))
            self._send_event(
                conn,
                send_lock,
                self._event_error(
                    session_id,
                    "stt_forward_failed",
                    "forwarding the committed turn to STT failed",
                    item_id=item_id,
                ),
            )
            self._graceful_close(conn, send_lock)
            return

        if scenario in (
            Scenario.RESPONSE_HAPPY_PATH,
            Scenario.RESPONSE_INTERRUPTED,
            Scenario.RESPONSE_AUDIO_DELTA_MALFORMED,
        ):
            self._run_response_scenario(conn, send_lock, session_id, item_id, scenario)
            return

        # Scenario.HAPPY_PATH (and the default fallthrough for any future member).
        self._send_event(conn, send_lock, self._event_speech_started(session_id, item_id))
        self._send_event(conn, send_lock, self._event_speech_stopped(session_id, item_id))
        self._send_event(conn, send_lock, self._event_transcription_completed(session_id, item_id))
        self._graceful_close(conn, send_lock)

    def _run_response_scenario(
        self,
        conn: socket.socket,
        send_lock: threading.Lock,
        session_id: str,
        item_id: str,
        scenario: Scenario,
    ) -> None:
        """The shared arm-and-wait body for every ``response_*`` scenario
        (embodiment-layer plan, task t3).

        Waits (bounded by ``wait_timeout``) for the client's
        ``response.create`` frame, then proceeds regardless of whether it
        arrived — the same "make the wait observable, never fail the
        connection over it" posture ``PING_EXPECT_PONG`` uses above: a test
        that forgot to arm still gets a deterministic, bounded scenario rather
        than a hang.
        """
        self._response_create_event.clear()
        response_id = _gen_id("resp")
        with self._lock:
            self._last_response_id = response_id
        self._response_create_event.wait(timeout=self._wait_timeout)

        self._send_event(
            conn, send_lock, self._event_response_created(session_id, response_id, item_id)
        )
        self._send_event(
            conn,
            send_lock,
            self._event_response_text_done(session_id, response_id, self._response_text),
        )

        if scenario is Scenario.RESPONSE_AUDIO_DELTA_MALFORMED:
            self._send_event(
                conn,
                send_lock,
                self._event_response_audio_delta_malformed(session_id, response_id),
            )
            self._graceful_close(conn, send_lock)
            return

        chunk_bytes = self._response_chunk_bytes
        chunks = [
            self._response_audio[start : start + chunk_bytes]
            for start in range(0, len(self._response_audio), chunk_bytes)
        ]

        if scenario is Scenario.RESPONSE_INTERRUPTED:
            # Only the FIRST chunk is delivered before the barge-in cuts the
            # reply short — the rest stays undelivered, exactly what
            # truncated=True on the interrupted event means.
            if chunks:
                self._send_event(
                    conn,
                    send_lock,
                    self._event_response_audio_delta(session_id, response_id, chunks[0]),
                )
            self._send_event(
                conn, send_lock, self._event_response_interrupted(session_id, response_id)
            )
            self._graceful_close(conn, send_lock)
            return

        # Scenario.RESPONSE_HAPPY_PATH: every chunk, in order, then response.done.
        for chunk in chunks:
            self._send_event(
                conn, send_lock, self._event_response_audio_delta(session_id, response_id, chunk)
            )
        self._send_event(conn, send_lock, self._event_response_done(session_id, response_id))
        self._graceful_close(conn, send_lock)
