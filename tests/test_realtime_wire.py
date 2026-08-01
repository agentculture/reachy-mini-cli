"""Offline unit tests for :mod:`reachy.speech.realtime_wire` (task t1).

Every test here is pure — no socket, no thread, no live server — mirroring
``lobes-cli``'s ``tests/test_realtime_smoke_helpers.py``, the donor's own test
family for the code this module ports. See the module docstring for the wire
contract and the provenance note.
"""

from __future__ import annotations

import ast
import base64
import io
import json
from pathlib import Path

import pytest

from reachy.speech import realtime_wire as wire

_MODULE_PATH = Path(wire.__file__)

#: Every stdlib module this file is allowed to import at module scope — the
#: literal "stdlib only" contract from CLAUDE.md's hard constraints (h12): a
#: new base runtime dependency needs an explicit decision, and this module
#: adds none.
_ALLOWED_TOP_LEVEL_IMPORTS = {"__future__", "base64", "hashlib", "json", "os", "struct", "typing"}


def _reader_from(data: bytes):
    """A ``recv_exact``-shaped callable over static bytes: short-reads at EOF,
    never raises — the same contract a real socket helper follows."""
    buf = io.BytesIO(data)

    def _recv(n: int) -> bytes:
        return buf.read(n)

    return _recv


def _length_marker(payload_len: int) -> int:
    """The RFC 6455 length-prefix byte (low 7 bits) build_frame must choose."""
    if payload_len < 126:
        return payload_len
    if payload_len < 65536:
        return 126
    return 127


# --- import boundary: pure module, stdlib only, no socket ---------------------


def test_module_never_imports_socket_or_a_third_party_ws_library() -> None:
    src = _MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "import socket",
        "from socket",
        "import websocket",
        "from websocket",
        "import websockets",
        "from websockets",
        "import aiohttp",
        "from aiohttp",
        "import requests",
        "from requests",
    )
    offenders = [line for line in src.splitlines() if line.strip().startswith(forbidden)]
    assert not offenders, f"realtime_wire.py imports a forbidden module: {offenders}"


def test_module_top_level_imports_are_all_stdlib_and_pinned() -> None:
    """AST-level pin (stronger than a text grep): every top-level import name
    resolves to something in :data:`_ALLOWED_TOP_LEVEL_IMPORTS`. ``urllib`` is
    the one multi-component import (``from urllib.parse import ...``) and its
    top-level package name is ``urllib``."""
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module.split(".")[0])
    allowed = _ALLOWED_TOP_LEVEL_IMPORTS | {"urllib"}
    assert found <= allowed, f"unexpected top-level import(s): {found - allowed}"


def test_module_never_calls_a_network_primitive() -> None:
    """Belt-and-suspenders: no ``urlopen``/``create_connection``/``sendall``/
    ``recv`` call anywhere in the CODE — this module does bytes-in/bytes-out
    only. (A bare ``socket.`` substring check would false-positive on this
    module's own docstring, which cites ``socket.socket`` in prose; the
    stronger AST import pin above already proves ``socket`` is never
    imported, so no code path could call it even if the substring appeared.)
    """
    src = _MODULE_PATH.read_text(encoding="utf-8")
    for needle in ("urlopen(", "create_connection(", "sendall(", ".recv("):
        assert needle not in src, f"unexpected I/O primitive {needle!r} found in realtime_wire.py"


# --- Sec-WebSocket-Key / Sec-WebSocket-Accept ---------------------------------


def test_compute_accept_key_matches_the_rfc6455_worked_example() -> None:
    # RFC 6455 SS1.3's own worked example — the canonical correctness check,
    # not just a round trip against our own code.
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    assert wire.compute_accept_key(key) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_make_sec_websocket_key_is_16_random_bytes_base64_encoded() -> None:
    key = wire.make_sec_websocket_key()
    decoded = base64.b64decode(key)
    assert len(decoded) == 16
    # Two independent calls must not collide (a hardcoded nonce or broken RNG
    # would break the RFC's replay-protection intent for the handshake).
    assert wire.make_sec_websocket_key() != key


def test_verify_accept_key_true_for_the_correct_pair() -> None:
    key = wire.make_sec_websocket_key()
    accept = wire.compute_accept_key(key)
    assert wire.verify_accept_key(key, accept) is True


def test_verify_accept_key_false_for_a_mismatched_pair() -> None:
    key = wire.make_sec_websocket_key()
    wrong_accept = wire.compute_accept_key(wire.make_sec_websocket_key())
    assert wire.verify_accept_key(key, wrong_accept) is False


# --- handshake request / response head ----------------------------------------


def test_build_handshake_request_carries_every_mandatory_header() -> None:
    req = wire.build_handshake_request(
        "gateway:8000",
        "/v1/realtime?input_sample_rate=16000",
        "dGhlIHNhbXBsZSBub25jZQ==",
        extra_headers={"Authorization": "Bearer secret"},
    )
    text = req.decode("latin-1")
    assert text.startswith("GET /v1/realtime?input_sample_rate=16000 HTTP/1.1\r\n")
    assert "Host: gateway:8000\r\n" in text
    assert "Upgrade: websocket\r\n" in text
    assert "Connection: Upgrade\r\n" in text
    assert "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n" in text
    assert "Sec-WebSocket-Version: 13\r\n" in text
    assert "Authorization: Bearer secret\r\n" in text
    assert text.endswith("\r\n\r\n")


def test_build_handshake_request_omits_authorization_when_no_api_key() -> None:
    req = wire.build_handshake_request("gateway:8000", "/v1/realtime", "key==", extra_headers=None)
    assert b"Authorization" not in req


def test_handshake_round_trip_verifies_the_real_accept_key() -> None:
    """Criterion 1: handshake builder + response parser + accept-key
    verification, chained end to end, exactly as a session client would use
    them (build request -> [server replies] -> parse response -> verify)."""
    key = wire.make_sec_websocket_key()
    request = wire.build_handshake_request(
        "gateway:8000", "/v1/realtime?input_sample_rate=16000", key
    )
    assert f"Sec-WebSocket-Key: {key}".encode("latin-1") in request

    # Simulate the server's correct 101 response.
    accept = wire.compute_accept_key(key)
    response_head = (
        f"HTTP/1.1 101 Switching Protocols\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        f"\r\n"
    ).encode("latin-1")

    status, headers = wire.parse_response_head(response_head)
    assert status == 101
    assert wire.verify_accept_key(key, headers["sec-websocket-accept"]) is True


def test_parse_response_head_extracts_status_and_lowercases_header_names() -> None:
    head = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n"
        b"\r\n"
    )
    status, headers = wire.parse_response_head(head)
    assert status == 101
    assert headers["sec-websocket-accept"] == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    assert headers["upgrade"] == "websocket"


def test_parse_response_head_handles_a_refusal_status() -> None:
    head = b"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\n\r\n"
    status, headers = wire.parse_response_head(head)
    assert status == 401
    assert headers["content-type"] == "application/json"


def test_parse_response_head_is_zero_on_unparseable_status_line() -> None:
    status, _headers = wire.parse_response_head(b"not even http\r\n\r\n")
    assert status == 0


def test_parse_response_head_is_zero_on_empty_input() -> None:
    status, headers = wire.parse_response_head(b"")
    assert status == 0
    assert headers == {}


# --- masking -------------------------------------------------------------------


def test_mask_payload_is_its_own_inverse() -> None:
    payload = b"the quick brown fox"
    mask_key = b"\x01\x02\x03\x04"
    masked = wire.mask_payload(payload, mask_key)
    assert masked != payload  # a real mask actually changes the bytes
    assert wire.mask_payload(masked, mask_key) == payload


def test_mask_payload_rejects_a_mask_key_of_the_wrong_length() -> None:
    with pytest.raises(ValueError):
        wire.mask_payload(b"data", b"\x00\x00\x00")


# --- frame build / read round trips: TEXT, PING/PONG, CLOSE --------------------


@pytest.mark.parametrize(
    "payload_len",
    [0, 10, 125, 126, 5000, 65535, 65536, 70000],
    ids=["empty", "tiny", "7bit-max", "16bit-min", "16bit-mid", "16bit-max", "64bit-min", "64bit"],
)
def test_build_frame_then_read_frame_round_trips_every_length_encoding(payload_len: int) -> None:
    """Criterion 2's property test: one payload per length class (7-bit, the
    16-bit ``126`` marker, the 64-bit ``127`` marker), each built cheaply by
    slicing a small repeating pattern rather than generating fresh random
    bytes at 64KB+ sizes."""
    payload = (bytes(range(256)) * ((payload_len // 256) + 1))[:payload_len]
    frame = wire.build_frame(wire.OPCODE_BINARY, payload, mask=True)

    # The length-prefix byte must match the encoding this size demands.
    assert (frame[1] & 0x7F) == _length_marker(payload_len)
    # Client-to-server masking is always on (RFC 6455 SS5.1) regardless of size.
    assert (frame[1] & 0x80) == 0x80

    fin, opcode, got = wire.read_frame(_reader_from(frame))
    assert fin is True
    assert opcode == wire.OPCODE_BINARY
    assert got == payload


def test_build_frame_then_read_frame_round_trips_an_unmasked_text_frame() -> None:
    # Server->client frames are never masked (RFC 6455 SS5.1) — the reader
    # must handle that just as correctly as a masked one.
    payload = b'{"type": "session.created"}'
    frame = wire.build_frame(wire.OPCODE_TEXT, payload, mask=False)
    assert (frame[1] & 0x80) == 0x00  # not masked
    fin, opcode, got = wire.read_frame(_reader_from(frame))
    assert fin is True
    assert opcode == wire.OPCODE_TEXT
    assert got == payload


def test_build_frame_then_read_frame_round_trips_ping_and_pong() -> None:
    for opcode in (wire.OPCODE_PING, wire.OPCODE_PONG):
        frame = wire.build_frame(opcode, b"keepalive", mask=True)
        fin, got_opcode, payload = wire.read_frame(_reader_from(frame))
        assert fin is True
        assert got_opcode == opcode
        assert payload == b"keepalive"


def test_build_frame_then_read_frame_round_trips_a_close_frame_with_status_code() -> None:
    import struct as _struct

    close_payload = _struct.pack("!H", 1000) + b"normal closure"
    frame = wire.build_frame(wire.OPCODE_CLOSE, close_payload, mask=True)
    fin, opcode, got = wire.read_frame(_reader_from(frame))
    assert fin is True
    assert opcode == wire.OPCODE_CLOSE
    assert got == close_payload


def test_build_frame_empty_payload_round_trips() -> None:
    frame = wire.build_frame(wire.OPCODE_CLOSE, b"", mask=True)
    fin, opcode, got = wire.read_frame(_reader_from(frame))
    assert fin is True
    assert opcode == wire.OPCODE_CLOSE
    assert got == b""


def test_read_frame_raises_on_a_header_truncated_before_two_bytes() -> None:
    reader = _reader_from(b"\x81")
    with pytest.raises(wire.FrameReadError):
        wire.read_frame(reader)


def test_read_frame_raises_on_a_payload_truncated_mid_frame() -> None:
    full = wire.build_frame(wire.OPCODE_TEXT, b"hello world", mask=False)
    truncated = full[:-3]  # header intact, payload cut short
    reader = _reader_from(truncated)
    with pytest.raises(wire.FrameReadError):
        wire.read_frame(reader)


def test_read_frame_raises_on_a_16bit_length_field_truncated() -> None:
    # FIN+opcode byte, then a length byte declaring the 126 marker, then
    # only one of the two extended-length bytes.
    reader = _reader_from(bytes([0x81, 0x7E, 0x00]))
    with pytest.raises(wire.FrameReadError):
        wire.read_frame(reader)


def test_read_frame_raises_on_a_64bit_length_field_truncated() -> None:
    # FIN+opcode byte, then a length byte declaring the 127 marker, then
    # only 3 of the 8 extended-length bytes.
    reader = _reader_from(bytes([0x81, 0x7F, 0x00, 0x00, 0x00]))
    with pytest.raises(wire.FrameReadError):
        wire.read_frame(reader)


def test_read_frame_raises_on_a_mask_key_truncated() -> None:
    # A masked-bit frame (0xFF -> masked, length 5) but only 2 of the 4 mask
    # key bytes follow.
    reader = _reader_from(bytes([0x81, 0x85, 0x01, 0x02]))
    with pytest.raises(wire.FrameReadError):
        wire.read_frame(reader)


def test_read_frame_correctly_parses_the_64bit_length_marker_cheaply() -> None:
    """Exercises the PARSE side of the 64-bit length path without allocating a
    64KB+ payload: the header/length-field bytes are hand-built directly
    (marker 127 + an 8-byte big-endian length), independent of build_frame's
    own size-based encoding choice."""
    import struct as _struct

    payload = b"hi"  # tiny payload, but the header LIES that it's the 64-bit class
    header = bytes([0x81, 0xFF]) + _struct.pack("!Q", len(payload))  # masked bit set
    mask_key = b"\x00\x00\x00\x00"  # a no-op mask so the payload round-trips as-is
    frame = header + mask_key + payload
    fin, opcode, got = wire.read_frame(_reader_from(frame))
    assert fin is True
    assert opcode == wire.OPCODE_TEXT
    assert got == payload


# --- base64 append-event codec --------------------------------------------------


def test_build_append_event_wraps_pcm_as_base64_input_audio_buffer_append() -> None:
    pcm = b"\x01\x02\x03\x04\x05\x06"
    text = wire.build_append_event(pcm)
    event = json.loads(text)
    assert event == {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
    assert base64.b64decode(event["audio"]) == pcm


def test_build_append_event_round_trips_empty_audio() -> None:
    # Zero bytes of audio is a valid (if odd) chunk — never an error.
    text = wire.build_append_event(b"")
    event = json.loads(text)
    assert event["audio"] == ""
    assert base64.b64decode(event["audio"]) == b""


def test_build_append_event_is_valid_json_text_ready_for_a_text_frame() -> None:
    """The literal shape a TEXT frame carries: JSON text, no binary anywhere."""
    pcm = bytes(range(256)) * 4  # 1024 bytes, exercises every byte value
    text = wire.build_append_event(pcm)
    assert isinstance(text, str)
    frame = wire.build_frame(wire.OPCODE_TEXT, text.encode("utf-8"), mask=True)
    fin, opcode, payload = wire.read_frame(_reader_from(frame))
    assert fin is True
    assert opcode == wire.OPCODE_TEXT
    decoded = wire.decode_event(payload)
    assert decoded is not None
    assert base64.b64decode(decoded["audio"]) == pcm


# --- response.create arming frame (embodiment-layer plan, task t3) --------------


def test_build_response_create_event_is_exactly_the_bare_type_object() -> None:
    text = wire.build_response_create_event()
    assert json.loads(text) == {"type": "response.create"}
    assert json.loads(text) == {"type": wire.RESPONSE_CREATE_EVENT_TYPE}


def test_build_response_create_event_takes_no_arguments_and_is_stable() -> None:
    # No body, no per-call state: every call renders identically.
    assert wire.build_response_create_event() == wire.build_response_create_event()


def test_build_response_create_event_is_valid_json_text_ready_for_a_text_frame() -> None:
    """The literal shape a TEXT frame carries, exactly like the append event's
    own round-trip test above — build, frame, read, decode."""
    text = wire.build_response_create_event()
    assert isinstance(text, str)
    frame = wire.build_frame(wire.OPCODE_TEXT, text.encode("utf-8"), mask=True)
    fin, opcode, payload = wire.read_frame(_reader_from(frame))
    assert fin is True
    assert opcode == wire.OPCODE_TEXT
    decoded = wire.decode_event(payload)
    assert decoded is not None
    assert decoded["type"] == wire.RESPONSE_CREATE_EVENT_TYPE


def test_the_wire_modules_outbound_frame_type_family_is_exactly_two_members() -> None:
    """h13 (embodiment-layer spec): the client-side SEND surface is session
    config (query params — never a frame, see :func:`derive_realtime_ws_url`),
    ``input_audio_buffer.append`` and ``response.create`` — no other frame
    type may ever be built here, so tool calls can never travel over this
    socket.

    An AST scan is a stronger pin than calling the two known ``build_*``
    functions and checking their output: it finds every dict literal in the
    module with a ``"type"`` key (resolving a ``Name`` value like
    ``APPEND_EVENT_TYPE`` through the module's own globals) and asserts the
    resulting set is exactly the two allowed constants — so a THIRD outbound
    builder added anywhere in this file, under any name, fails this test
    immediately rather than waiting for someone to remember to update it.
    """
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    module_globals = vars(wire)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "type"):
                continue
            if isinstance(value, ast.Name) and isinstance(module_globals.get(value.id), str):
                found.add(module_globals[value.id])
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.add(value.value)
    assert found == {wire.APPEND_EVENT_TYPE, wire.RESPONSE_CREATE_EVENT_TYPE}


# --- decode_event: never raises on malformed input ------------------------------


def test_decode_event_round_trips_a_well_formed_event() -> None:
    raw = json.dumps({"type": "session.created", "session_id": "abc"})
    decoded = wire.decode_event(raw)
    assert decoded == {"type": "session.created", "session_id": "abc"}


def test_decode_event_accepts_bytes_payload_like_read_frame_returns() -> None:
    raw = json.dumps({"type": "input_audio_buffer.speech_started"}).encode("utf-8")
    decoded = wire.decode_event(raw)
    assert decoded == {"type": "input_audio_buffer.speech_started"}


def test_decode_event_returns_none_for_non_json_text() -> None:
    assert wire.decode_event("not json at all {{{") is None


def test_decode_event_returns_none_for_invalid_utf8_bytes() -> None:
    assert wire.decode_event(b"\xff\xfe\x00\x01") is None


def test_decode_event_returns_none_for_a_missing_type_field() -> None:
    assert wire.decode_event(json.dumps({"session_id": "abc"})) is None


def test_decode_event_returns_none_for_a_non_string_type_field() -> None:
    assert wire.decode_event(json.dumps({"type": 123})) is None


def test_decode_event_returns_none_for_an_empty_type_field() -> None:
    assert wire.decode_event(json.dumps({"type": ""})) is None


@pytest.mark.parametrize("wrong_shape", ["[1, 2, 3]", '"just a string"', "42", "null", "true"])
def test_decode_event_returns_none_for_a_non_object_top_level_json_value(wrong_shape: str) -> None:
    assert wire.decode_event(wrong_shape) is None


def test_decode_event_recognises_every_consumed_event_type_shape() -> None:
    """Sanity check against the exact contract: every event type this wire is
    documented to consume decodes cleanly — including the response.* family
    (embodiment-layer plan, task t3): decode_event is generic by design, so
    these need no special-casing, but the contract is worth pinning by name."""
    for event_type in (
        "session.created",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "conversation.item.input_audio_transcription.completed",
        "error",
        "response.created",
        "response.text.done",
        "response.audio.delta",
        "response.done",
        "response.interrupted",
    ):
        decoded = wire.decode_event(json.dumps({"type": event_type}))
        assert decoded is not None
        assert decoded["type"] == event_type


def test_decode_event_recognises_named_error_codes() -> None:
    for code in ("vad_unavailable", "stt_forward_failed"):
        decoded = wire.decode_event(json.dumps({"type": "error", "code": code}))
        assert decoded is not None
        assert decoded["code"] == code


# --- URL derivation --------------------------------------------------------------


def test_derive_realtime_ws_url_maps_http_to_ws_and_keeps_the_port() -> None:
    url = wire.derive_realtime_ws_url("http://localhost:8001", 16000)
    assert url == "ws://localhost:8001/v1/realtime?input_sample_rate=16000"


def test_derive_realtime_ws_url_maps_https_to_wss_and_keeps_the_port() -> None:
    url = wire.derive_realtime_ws_url("https://gateway.example.ts.net:8443", 24000)
    assert url == "wss://gateway.example.ts.net:8443/v1/realtime?input_sample_rate=24000"


def test_derive_realtime_ws_url_never_defaults_a_missing_port() -> None:
    url = wire.derive_realtime_ws_url("https://gateway.example.ts.net", 16000)
    assert url == "wss://gateway.example.ts.net/v1/realtime?input_sample_rate=16000"
    assert ":443" not in url


def test_derive_realtime_ws_url_rejects_a_non_http_scheme() -> None:
    with pytest.raises(ValueError):
        wire.derive_realtime_ws_url("ftp://gateway.example.ts.net", 16000)


def test_derive_realtime_ws_url_honours_a_custom_path() -> None:
    url = wire.derive_realtime_ws_url("http://localhost:8001", 16000, path="/v1/realtime/other")
    assert url.startswith("ws://localhost:8001/v1/realtime/other?")
