"""An offline, in-process HTTP fake for an OpenAI-compatible ``/v1/chat/completions``.

Test infrastructure for the embodiment layer's turn engine (task t10) and for
:mod:`reachy.speech.llm`'s streaming leg. It is the HTTP sibling of
``tests/fake_realtime_server.py``: stdlib only (``http.server``, ``threading``,
``json``), bound to an ephemeral loopback port, one process, no network, no new
dependency, safe under ``pytest -n auto``.

Why a real socket rather than a monkeypatched ``urlopen``
---------------------------------------------------------
The two properties this arc has to prove about streaming cannot be observed
through a stubbed transport, because both are properties of the SOCKET:

* **deltas are consumed incrementally.** A fake that hands back a whole body
  cannot tell a line-by-line reader from one that buffered everything first.
  Here the server writes chunk *i*, then calls the script's ``on_chunk`` hook
  before writing chunk *i+1* — so a test can BLOCK the server until the client
  has actually surfaced the previous delta, and a buffering client deadlocks
  its own proof instead of passing it.
* **the read deadline is per-read, not total** (the measured finding in
  ``docs/evidence/2026-08-01-cited-findings-from-embodiment-sibling.md``: the
  largest inter-chunk gap was 0.124 s while the first content delta took
  43.2 s). ``Script.chunk_delay_s`` paces real writes on a real socket, so a
  drop armed on total elapsed and one armed on inter-chunk idle behave
  DIFFERENTLY here — which is the whole point of honesty condition h6.

Usage::

    with FakeChatServer(script=Script(chunks=[content_chunk("hi"), finish()])) as server:
        result = llm.stream_turn(messages, base_url=server.base_url, model="worker")
    assert server.requests[0]["stream"] is True

A ``script_fn`` may be passed instead, receiving the decoded request payload and
returning the :class:`Script` for that request — that is how a test scripts a
multi-round tool loop (round 1 answers with a tool call, round 2 with text).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: The chat-completions path an OpenAI-compatible client posts to.
CHAT_PATH = "/v1/chat/completions"


# --------------------------------------------------------------------------- #
# SSE chunk builders — the wire shapes the real gateway emits                 #
# --------------------------------------------------------------------------- #


def _chunk(delta: dict, *, finish_reason: str | None = None) -> dict:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def role_chunk() -> dict:
    """The opening ``delta.role`` chunk every OpenAI-compatible stream sends."""
    return _chunk({"role": "assistant"})


def content_chunk(text: str) -> dict:
    """One ``delta.content`` fragment."""
    return _chunk({"content": text})


def reasoning_chunk(text: str, *, key: str = "reasoning") -> dict:
    """One reasoning fragment under *key*.

    The default is ``reasoning`` — the name our own gateway actually sends,
    reproduced at ``localhost:8001`` with ``model=cortex`` over 73 chunks
    (``docs/evidence/2026-08-01-cited-findings-from-embodiment-sibling.md``).
    vLLM DOCUMENTS ``reasoning_content``; pass ``key="reasoning_content"`` to
    model a server that follows the documentation instead.
    """
    return _chunk({key: text})


def tool_call_chunk(
    *, index: int = 0, call_id: str | None = None, name: str | None = None, arguments: str = ""
) -> dict:
    """One ``delta.tool_calls[i]`` fragment (id + name early, arguments dripped)."""
    fragment: dict = {"index": index}
    if call_id is not None:
        fragment["id"] = call_id
    function: dict = {}
    if name is not None:
        function["name"] = name
    if arguments:
        function["arguments"] = arguments
    if function:
        fragment["function"] = function
    return _chunk({"tool_calls": [fragment]})


def finish(reason: str = "stop") -> dict:
    """The terminal chunk carrying ``finish_reason``."""
    return _chunk({}, finish_reason=reason)


# --------------------------------------------------------------------------- #
# Script                                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class Script:
    """What the server does for ONE request.

    Attributes:
        chunks: the SSE chunk dicts, written in order as ``data: <json>`` lines.
        status: the HTTP status (a non-2xx skips the SSE body entirely).
        body: a non-SSE body — used for ``stream=false`` requests and errors.
        chunk_delay_s: seconds slept BEFORE each chunk (a paced stream).
        stall_after: write this many chunks, then stop writing and hold the
            connection open until :meth:`FakeChatServer.release` (or the
            handler's own bounded wait) — the stalled-stream case for h6.
        stall_timeout_s: the handler's own bound on that hold, so a test that
            forgets to release cannot hang the suite.
        on_chunk: called with the index of the chunk about to be written, ON
            THE SERVER THREAD. The incremental-consumption gate.
        send_done: append the ``data: [DONE]`` sentinel after the last chunk.
    """

    chunks: list[dict] = field(default_factory=list)
    status: int = 200
    body: str | None = None
    chunk_delay_s: float = 0.0
    stall_after: int | None = None
    stall_timeout_s: float = 30.0
    on_chunk: Callable[[int], None] | None = None
    send_done: bool = True


def json_body(payload: dict, *, status: int = 200) -> Script:
    """A plain JSON (non-SSE) response — the ``stream=false`` shape."""
    return Script(status=status, body=json.dumps(payload))


# --------------------------------------------------------------------------- #
# Server                                                                      #
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0: no Content-Length is required and the client reads to EOF, which
    # is exactly how an SSE body of unknown length terminates here.
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args: object) -> None:  # noqa: D102 - silence the test log
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        owner: "FakeChatServer" = self.server.owner  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {"_undecodable": raw.decode("utf-8", "replace")}
        owner.record(self.path, dict(self.headers), payload)

        script = owner.script_for(payload)
        if script.body is not None or script.status != 200:
            self._respond_body(script)
            return
        self._respond_sse(script, owner)

    def _respond_body(self, script: Script) -> None:
        body = (script.body or "").encode("utf-8")
        self.send_response(script.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    def _respond_sse(self, script: Script, owner: "FakeChatServer") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for index, chunk in enumerate(script.chunks):
            if script.stall_after is not None and index >= script.stall_after:
                owner.stalled.set()
                owner.release.wait(script.stall_timeout_s)
                return
            if script.chunk_delay_s:
                owner.release.wait(script.chunk_delay_s)
            if script.on_chunk is not None:
                script.on_chunk(index)
            if not self._write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8")):
                return
        if script.send_done:
            self._write(b"data: [DONE]\n\n")

    def _write(self, data: bytes) -> bool:
        """Write *data*, reporting whether the client is still there."""
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
            return False
        return True


class FakeChatServer:
    """A loopback ``/v1/chat/completions`` server driven by one or many scripts.

    Args:
        script: the :class:`Script` used for every request.
        script_fn: called with each decoded request payload, returning that
            request's :class:`Script`. Wins over *script* when given — this is
            how a multi-round tool loop is scripted.

    Attributes:
        requests: one decoded request payload per POST, in arrival order.
        headers: the matching request headers, in arrival order.
        paths: the matching request paths, in arrival order.
        stalled: set when a script's ``stall_after`` point is reached.
        release: set it to end a stall (or shorten a paced write) early.
    """

    def __init__(
        self,
        *,
        script: Script | None = None,
        script_fn: Callable[[dict], Script] | None = None,
    ) -> None:
        self._script = script if script is not None else Script(chunks=[finish()])
        self._script_fn = script_fn
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.paths: list[str] = []
        self.stalled = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.owner = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-sse-server", daemon=True
        )

    # -- lifecycle ------------------------------------------------------- #

    def start(self) -> "FakeChatServer":
        self._thread.start()
        return self

    def close(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    def __enter__(self) -> "FakeChatServer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- accessors ------------------------------------------------------- #

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        """The ``base_url`` an ``llm`` caller passes (no ``/v1`` suffix)."""
        return f"http://127.0.0.1:{self.port}"

    # -- handler callbacks (server thread) -------------------------------- #

    def record(self, path: str, headers: dict, payload: dict) -> None:
        with self._lock:
            self.paths.append(path)
            self.headers.append(headers)
            self.requests.append(payload)

    def script_for(self, payload: dict) -> Script:
        if self._script_fn is not None:
            return self._script_fn(payload)
        return self._script
