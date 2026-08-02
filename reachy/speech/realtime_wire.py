"""Hand-rolled RFC 6455 WebSocket framing + the OpenAI-Realtime base64 event codec.

**Pure functions only.** This module builds bytes and parses bytes; it never
opens a socket, never imports :mod:`socket`, and never does I/O of any kind.
The socket-owning session client (:mod:`reachy.speech.realtime`, task t3)
imports these primitives and supplies its own transport (a real
``socket.socket``, or a fake in tests) through a small ``recv_exact``-shaped
callable — exactly the seam :func:`read_frame` takes.

Provenance — cite, don't import
--------------------------------
Ported by hand from ``lobes-cli``'s
``scripts/realtime-smoke.py`` (``make_sec_websocket_key``,
``compute_accept_key``, ``build_handshake_request``, ``parse_response_head``,
``mask_payload``, ``build_frame``, ``read_frame``, ``build_append_event``),
whose pure pieces are themselves unit-tested offline in that repo's
``tests/test_realtime_smoke_helpers.py``. We hand-roll rather than depend on
``websockets``/``websocket-client`` for the exact reason that donor script's
own docstring states::

    Issue #149 exists to move ``server_vad`` endpointing OFF the
    ``reachy-mini-cli`` robot client and ONTO the server — the whole point is
    to keep heavyweight deps (torch, an OpenAI SDK) out of a robot's
    dependency tree and its CI. A smoke test that itself needs
    ``websocket-client`` or ``websockets`` would undercut that motivation.

That reasoning applies with equal force to the actual runtime client this
module backs, not just its smoke test — a robot CLI (base deps: ``numpy`` +
``harmonics-cli`` only, see ``pyproject.toml``) gains nothing from a
third-party WebSocket dependency when RFC 6455's client-side subset is this
small. This is a hand PORT, not a live sync: the donor is read and adapted
to this repo's conventions (``from __future__ import annotations``, type
hints, 100-char lines), not imported.

Wire contract (agentculture/reachy-mini-cli#115)
--------------------------------------------------
- Audio out is JSON **TEXT** frames only, one per PCM chunk:
  ``{"type": "input_audio_buffer.append", "audio": "<base64 PCM16 mono LE>"}``.
  **No binary audio frames anywhere** — see :func:`build_append_event`.
- Connect at ``wss://<gateway>/v1/realtime?input_sample_rate=16000`` — session
  config rides query params on the connect URL, not a follow-up message. See
  :func:`derive_realtime_ws_url`.
- Auth is an ``Authorization: Bearer <key>`` header on the handshake — pass it
  via :func:`build_handshake_request`'s ``extra_headers``.
- Inbound JSON text-frame events this wire produces: ``session.created``,
  ``input_audio_buffer.speech_started``, ``input_audio_buffer.speech_stopped``,
  ``conversation.item.input_audio_transcription.completed``, named
  ``error`` events (``vad_unavailable``, ``stt_forward_failed``), and — once a
  session is ARMED (see below) — the ``response.*`` family:
  ``response.created``, ``response.text.done``, ``response.audio.delta``
  (base64 PCM16, chunked), ``response.done``, ``response.interrupted``.
  :func:`decode_event` decodes any of these generically; it is the session
  client's job (not this module's) to branch on ``type``.
- **Arming the duplex half** (embodiment-layer plan, task t3): sending
  :func:`build_response_create_event`'s ``response.create`` frame is the ONE
  opt-in trigger for the ``response.*`` family — the donor server
  (``lobes-cli``'s ``lobes/realtime/_conversation.py`` ``ConversationBridge``)
  starts DISARMED, so a session that never sends it gets exactly the #115
  ears-only sequence above and nothing else.
- **Pushing context into the floor's generate call** (foreground-Gemma plan,
  task t10, decision **c28**): :func:`build_conversation_item_create_event`'s
  ``conversation.item.create`` is the FOURTH and last outbound frame kind, and
  it is a DELIBERATE widening of a pinned surface rather than an
  implementation detail — the layer curates the canonical conversation history
  (decision c27) and needs a per-turn channel for cognition scopes, perception
  snapshots and rolling summary updates. It is also **PROVISIONAL**: upstream
  parked conversation-item parity explicitly, and the ask is
  agentculture/lobes-cli#170 item 2. See that function's docstring for the
  schema and what happens if upstream answers differently.

So the complete outbound family is session config (query params, not a frame
at all), :func:`build_append_event`, :func:`build_response_create_event` and
:func:`build_conversation_item_create_event` — four kinds, pinned by AST scan
in ``tests/test_realtime_wire.py`` (h13/h20).

Stdlib only: ``base64``, ``hashlib``, ``json``, ``os``, ``struct``,
``urllib.parse`` — the dependency lists in ``pyproject.toml`` are untouched by
this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from typing import Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

#: RFC 6455 §4.2.2 — fixed by spec, XORed into the Sec-WebSocket-Accept hash.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

#: The one route this wire ever targets.
REALTIME_PATH = "/v1/realtime"

#: The one audio-out event type this wire sends (never a binary frame).
APPEND_EVENT_TYPE = "input_audio_buffer.append"

#: The one arming frame this wire sends to opt a session into the duplex
#: half (embodiment-layer plan, task t3) — see :func:`build_response_create_event`.
RESPONSE_CREATE_EVENT_TYPE = "response.create"

#: The one CONTEXT frame this wire sends (foreground-Gemma plan, task t10,
#: decision c28) — see :func:`build_conversation_item_create_event`. Its
#: schema below is PROVISIONAL: upstream has parked conversation-item parity
#: (``lobes/realtime/_conversation.py``'s own note: ``response.create`` was
#: adopted "for its SHAPE only — full parity (session.update semantics, the
#: conversation-item schema, tool calls over the session) is an explicitly
#: parked follow-up"), and our ask is agentculture/lobes-cli#170 item 2.
CONVERSATION_ITEM_CREATE_EVENT_TYPE = "conversation.item.create"

#: The item OBJECT's own type, and the content-part type inside it. Both are
#: OpenAI-Realtime's names, adopted for their shape — they are types of an
#: object *inside* one event, never event kinds of their own, which is the
#: distinction the outbound-family AST pin makes explicitly.
ITEM_TYPE_MESSAGE = "message"
ITEM_CONTENT_INPUT_TEXT = "input_text"

#: The roles an injected item may carry, mirroring the two roles the floor
#: itself appends (``lobes/realtime/_conversation.py``:489/523 user,
#: :647/:680 assistant) plus ``system`` for out-of-band context.
ITEM_ROLE_SYSTEM = "system"
ITEM_ROLE_USER = "user"
ITEM_ROLE_ASSISTANT = "assistant"
ITEM_ROLES = (ITEM_ROLE_SYSTEM, ITEM_ROLE_USER, ITEM_ROLE_ASSISTANT)

#: The one key in this schema that is OURS rather than OpenAI's, and the whole
#: reason the schema is worth filing upstream: an item is either **ephemeral
#: CONTEXT** (participates in the next generate call, never lands in the
#: session's history) or a **HISTORY turn** (the client's curated record,
#: re-seeded after a reconnect). Without that distinction, items injected
#: beside the floor's own auto-appends duplicate and drift — the two-histories
#: failure the whole arc exists to avoid, one level down.
ITEM_DISPOSITION_CONTEXT = "context"
ITEM_DISPOSITION_HISTORY = "history"
ITEM_DISPOSITIONS = (ITEM_DISPOSITION_CONTEXT, ITEM_DISPOSITION_HISTORY)


class FrameReadError(Exception):
    """The frame stream ended (EOF) or was malformed before a full frame arrived."""


# ---------------------------------------------------------------------------
# Handshake: Sec-WebSocket-Key / Sec-WebSocket-Accept (RFC 6455 SS4.1/4.2.2)
# ---------------------------------------------------------------------------


def make_sec_websocket_key() -> str:
    """A fresh, random base64-encoded 16-byte nonce (RFC 6455 SS4.1)."""
    return base64.b64encode(os.urandom(16)).decode("ascii")


def compute_accept_key(sec_websocket_key: str) -> str:
    """RFC 6455 SS4.2.2: ``base64(sha1(key + the fixed WebSocket GUID))``.

    SHA-1 here is the protocol-mandated handshake check, not a security
    boundary — RFC 6455 requires exactly this algorithm.
    """
    digest = hashlib.sha1(  # nosec B324 - RFC 6455-mandated, not a security use
        (sec_websocket_key + WS_GUID).encode("ascii")
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_accept_key(sec_websocket_key: str, accept_key: str) -> bool:
    """``True`` iff *accept_key* is the correct Sec-WebSocket-Accept for *sec_websocket_key*.

    A caller (the session client, t3) uses this to decide whether to trust a
    101 handshake response — never trust an accept key without checking it.
    """
    return accept_key == compute_accept_key(sec_websocket_key)


def build_handshake_request(
    host: str, path: str, key: str, extra_headers: dict[str, str] | None = None
) -> bytes:
    """Serialise the WebSocket opening handshake (RFC 6455 SS4.1) as raw bytes."""
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for name, value in (extra_headers or {}).items():
        lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def parse_response_head(head: bytes) -> tuple[int, dict[str, str]]:
    """Parse a raw HTTP response head into ``(status_code, lowercased_headers)``.

    ``status_code`` is ``0`` when the status line cannot be parsed at all —
    never raises on malformed input.
    """
    text = head.decode("latin-1", errors="replace")
    lines = [line for line in text.split("\r\n") if line]
    status = 0
    if lines:
        parts = lines[0].split(None, 2)  # ["HTTP/1.1", "101", "Switching Protocols"]
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers


# ---------------------------------------------------------------------------
# Frame build / read (RFC 6455 SS5.2)
# ---------------------------------------------------------------------------


def mask_payload(payload: bytes, mask_key: bytes) -> bytes:
    """XOR-mask (or unmask — the operation is its own inverse) *payload*."""
    if len(mask_key) != 4:
        raise ValueError("mask_key must be exactly 4 bytes")
    return bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))


def build_frame(opcode: int, payload: bytes = b"", *, mask: bool = True) -> bytes:
    """One complete, unfragmented RFC 6455 SS5.2 frame (FIN always set).

    ``mask=True`` is the default and the only mode a client may legally use:
    every client-to-server frame MUST be masked per RFC 6455 SS5.1. The three
    payload-length encodings are chosen automatically per SS5.2: a 7-bit
    length inline (< 126 bytes), a 16-bit extended length (marker 126, up to
    65535 bytes), or a 64-bit extended length (marker 127, anything larger).
    """
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))  # FIN bit set, reserved bits clear
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if mask:
        mask_key = os.urandom(4)
        header += mask_key
        payload = mask_payload(payload, mask_key)
    return bytes(header) + payload


def read_frame(recv_exact: Callable[[int], bytes]) -> tuple[bool, int, bytes]:
    """Read one frame using ``recv_exact(n) -> bytes``.

    ``recv_exact`` is any one-int-argument callable that returns exactly ``n``
    bytes, or fewer at EOF — this function never assumes a real socket, which
    is what makes it testable against a plain ``io.BytesIO``-backed reader fed
    pre-built frames. Raises :class:`FrameReadError` (never anything else) on
    EOF or truncation at any point in the header/length/mask/payload sequence.
    """
    first_two = recv_exact(2)
    if len(first_two) < 2:
        raise FrameReadError("connection closed before a frame header arrived")
    b0, b1 = first_two[0], first_two[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        ext = recv_exact(2)
        if len(ext) < 2:
            raise FrameReadError("connection closed while reading the 16-bit extended length")
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = recv_exact(8)
        if len(ext) < 8:
            raise FrameReadError("connection closed while reading the 64-bit extended length")
        length = struct.unpack("!Q", ext)[0]
    mask_key = None
    if masked:
        mask_key = recv_exact(4)
        if len(mask_key) < 4:
            raise FrameReadError("connection closed while reading the mask key")
    payload = recv_exact(length) if length else b""
    if length and len(payload) < length:
        raise FrameReadError("connection closed before the full payload arrived")
    if masked and mask_key is not None:
        payload = mask_payload(payload, mask_key)
    return fin, opcode, payload


# ---------------------------------------------------------------------------
# Base64 append-event codec (audio-in) + the response.create arming frame
# (audio-out opt-in). Issue #115 shipped ears-only, where this section built
# only the append side; the embodiment-layer plan's task t3 adds the one
# outbound frame — response.create — that arms a session for the response.*
# family (response.created / response.text.done / response.audio.delta /
# response.done / response.interrupted). Decoding an inbound response.* event
# needs no new function: :func:`decode_event` below already handles any event
# type generically, exactly as it always has.
# ---------------------------------------------------------------------------


def build_append_event(pcm: bytes) -> str:
    """Wrap one PCM16 mono LE chunk as an ``input_audio_buffer.append`` JSON TEXT event.

    Returns the JSON **text** ready to send verbatim as a TEXT frame payload
    (see :data:`OPCODE_TEXT` / :func:`build_frame`) — this wire never sends a
    binary audio frame. An empty ``pcm`` is valid (encodes to ``""``), never
    an error.
    """
    return json.dumps({"type": APPEND_EVENT_TYPE, "audio": base64.b64encode(pcm).decode("ascii")})


def build_response_create_event() -> str:
    """Wrap the ``response.create`` arming frame as JSON TEXT ready to send.

    Alongside session config (query params — see :func:`derive_realtime_ws_url`,
    never a frame), :func:`build_append_event` and
    :func:`build_conversation_item_create_event`, this is one of the four
    outbound frame kinds this wire ever builds: the h13/h20 boundary
    (``tests/test_realtime_wire.py``'s
    ``test_the_wire_modules_outbound_frame_type_family_is_exactly_three_members``)
    pins that no FIFTH kind is ever added here. Tool calls never travel over
    this socket — see the embodiment-layer spec's scope/boundaries.

    Carries no body: the donor server's own opt-in check (``lobes-cli``'s
    ``lobes/realtime/_conversation.py`` ``is_response_create``) reads only
    ``type``, so this function takes no arguments. Safe to send more than
    once — arming is idempotent on the server side, and this function itself
    has no state to make idempotent.
    """
    return json.dumps({"type": RESPONSE_CREATE_EVENT_TYPE})


def build_conversation_item_create_event(text: str, *, role: str, disposition: str) -> str:
    """Wrap ONE conversation item as a ``conversation.item.create`` JSON TEXT event.

    The FOURTH outbound frame kind, and the one the layer's mouth-knows-the-mind
    arc leans on (decision **c28**, issue #153): the embodiment layer curates
    the canonical conversation history (decision c27) and pushes per-turn
    context into the floor's generate call through this channel — cognition
    scopes, perception snapshots, rolling summary updates, and the m-window
    re-seed a reconnect needs (claim c40).

    **Schema, and how much of it is ours.** The envelope is OpenAI-Realtime's
    own::

        {"type": "conversation.item.create",
         "item": {"type": "message", "role": "system",
                  "disposition": "context",
                  "content": [{"type": "input_text", "text": "..."}]}}

    ``item.type`` / ``item.role`` / ``item.content[]`` of ``input_text`` parts
    are adopted for their SHAPE, exactly as upstream adopted
    ``response.create``'s. ``disposition`` is OURS: it distinguishes an
    ephemeral CONTEXT item (participates in the next generate call, never lands
    in the session's own history) from a HISTORY turn (the client-curated
    record). That key exists because the floor already auto-appends both roles
    (``lobes/realtime/_conversation.py``:489/523 user, :647/:680 assistant), so
    items injected beside those auto-appends would duplicate and drift — and it
    is filed as the constraint on agentculture/lobes-cli#170 item 2.

    **PROVISIONAL, and it fails closed one level up.** Upstream has not shipped
    conversation-item parity; nothing has answered #170 item 2 yet, so this is
    the shape this repo committed to rather than a shape anyone agreed on. It
    is safe because no frame is ever sent to a gateway that did not announce
    support: :func:`reachy.speech.realtime_duplex.announces_conversation_items`
    is the one predicate that decides, and it answers ``True`` only for an
    explicit affirmative. If upstream answers with a different schema, this
    function and that predicate are the two places that change.

    Raises :class:`ValueError` for an unknown *role* or *disposition* — refused,
    never coerced, because guessing a disposition is exactly the duplicate-and-
    drift failure the key exists to prevent. *text* is carried VERBATIM and
    bounded by nothing here: whoever produced it already owns its bound
    (``reachy.embody.scope.ScopeLimits`` for a scope,
    ``reachy.embody.engine.Limits.summary_max_chars`` for a summary), and a
    second copy of a bound is a second number to drift.
    """
    if role not in ITEM_ROLES:
        raise ValueError(f"role must be one of {ITEM_ROLES}, got {role!r}")
    if disposition not in ITEM_DISPOSITIONS:
        raise ValueError(f"disposition must be one of {ITEM_DISPOSITIONS}, got {disposition!r}")
    return json.dumps(
        {
            "type": CONVERSATION_ITEM_CREATE_EVENT_TYPE,
            "item": {
                "type": ITEM_TYPE_MESSAGE,
                "role": role,
                "disposition": disposition,
                "content": [{"type": ITEM_CONTENT_INPUT_TEXT, "text": text}],
            },
        }
    )


def decode_event(payload: bytes | str) -> dict | None:
    """Decode one inbound TEXT-frame payload into an event dict, or ``None`` if malformed.

    Never raises. Every one of the following is "malformed" and yields
    ``None`` rather than an exception: invalid UTF-8 bytes, text that is not
    valid JSON, JSON whose top-level value is not an object (e.g. an array,
    string, or number), and an object with a missing/non-string/empty
    ``"type"`` field. This function only validates the wire *shape* — a
    session client is responsible for branching on ``type`` and handling each
    of ``session.created`` / ``input_audio_buffer.speech_started`` /
    ``input_audio_buffer.speech_stopped`` /
    ``conversation.item.input_audio_transcription.completed`` / ``error`` /
    and, once armed (:func:`build_response_create_event`), ``response.created``
    / ``response.text.done`` / ``response.audio.delta`` / ``response.done`` /
    ``response.interrupted``. This function stops at the envelope: a
    ``response.audio.delta`` whose ``"delta"`` field is present but not valid
    base64 still decodes here (the shape is fine) — a caller that wants the
    PCM bytes back must base64-decode that field itself and handle a
    ``ValueError``, exactly as the append side's own ``"audio"`` field is
    decoded ad hoc by its caller, never inside this function.
    """
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        text = payload
    try:
        parsed = json.loads(text)
    except ValueError:  # json.JSONDecodeError is a ValueError subclass
        return None
    if not isinstance(parsed, dict):
        return None
    event_type = parsed.get("type")
    if not isinstance(event_type, str) or not event_type:
        return None
    return parsed


# ---------------------------------------------------------------------------
# Connect-URL derivation
# ---------------------------------------------------------------------------


def derive_realtime_ws_url(
    base_url: str, input_sample_rate: int, *, path: str = REALTIME_PATH
) -> str:
    """Map an ``http(s)://host[:port]`` gateway base URL to its ``/v1/realtime`` connect URL.

    ``http://host:port`` -> ``ws://host:port<path>?input_sample_rate=<rate>``;
    ``https://`` maps to ``wss://`` the same way. The host/port are preserved
    exactly as given in *base_url* — a port is carried through unchanged, and
    an *absent* port is never defaulted to 80/443. Raises :class:`ValueError`
    for any scheme other than ``http``/``https``: there is no sane ws(s)
    mapping for anything else.
    """
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"base_url must be http:// or https://, got {base_url!r}")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urlencode({"input_sample_rate": int(input_sample_rate)})
    return urlunsplit((scheme, parsed.netloc, path, query, ""))
