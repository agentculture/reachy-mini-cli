#!/usr/bin/env python3
"""t1 probe (embodiment-layer plan): can two /v1/realtime sessions coexist?

Opens TWO simultaneous WebSocket sessions against the deployed lobes gateway's
``/v1/realtime`` route (default ``http://localhost:8001``, resolved exactly as
the production code resolves it — see ``reachy.speech.realtime``):

- **Session A** — transcription-only, ears-only. This is the literal
  production client the runtime already uses in ``TranscriptSenseDriver``
  (``reachy.speech.realtime.RealtimeTranscriber``): it never sends
  ``response.create`` and silently ignores any ``response.*`` event that
  arrives (counted in ``ignored_events``). Reused verbatim, not re-derived.
- **Session B** — armed conversational. No production client for this exists
  yet (that is task t3/t9's job); this script hand-rolls a minimal client
  using the same pure-function wire primitives
  (``reachy.speech.realtime_wire``) that both the production client and
  ``tests/fake_realtime_server.py`` already use. It sends one
  ``response.create`` shortly after connecting (arming is session-level and
  idempotent per lobes-cli's ``_conversation.py``), then logs every
  ``response.*`` event verbatim.

Both sessions are fed **real recorded speech** (not a synthetic tone — a weak
VAD probe) from ``/usr/share/sounds/speech-dispatcher/dummy-message.wav``
(16 kHz mono PCM16, ~29.4 s, part of the ``speech-dispatcher`` OS package).
Session A gets it forwards; Session B gets the SAME samples time-reversed.
Reversing preserves the amplitude envelope (so VAD onset/offset behaviour
should be similar) while making the two streams trivially distinguishable in
content, so a cross-talk bug (session A's socket receiving session B's
transcript, or vice versa) would be visible as a forward-sounding transcript
turning up on the reversed-audio session or vice versa.

Every event either session sends or receives is appended to one shared,
timestamped (``t=`` seconds since the probe's t0) log list, and dumped as
NDJSON at the end. Both a Python ``logging`` handler (capturing this repo's
``reachy.sense`` senselog lines and ``reachy.speech.realtime``'s own debug
line for an ignored response.* event) and the two clients' own counters feed
the log, so the evidence doc can cite either the structured events or the
human-readable trace.

No library code changes. No new dependency (stdlib + numpy, already a base
dep). Safe to re-run: it never mutates repo state, only prints/writes under
``docs/evidence/``.

Usage::

    uv run python scripts/probe_concurrent_realtime.py [--duration 75] [--out-dir docs/evidence]
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import queue
import select
import socket
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

from reachy.speech import realtime as rt
from reachy.speech import realtime_wire as wire

REAL_SPEECH_WAV = Path("/usr/share/sounds/speech-dispatcher/dummy-message.wav")
CHUNK_MS = 20
DEFAULT_DURATION_S = 75.0
DEFAULT_ARM_DELAY_S = 2.0


# --------------------------------------------------------------------------- #
# Shared event log                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class EventLog:
    t0: float
    entries: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, session: str, kind: str, **fields: object) -> None:
        entry = {"t": round(time.monotonic() - self.t0, 3), "session": session, "type": kind}
        entry.update(fields)
        with self.lock:
            self.entries.append(entry)
        shown = {k: v for k, v in fields.items() if k not in ("config",)}
        print(f"[{entry['t']:7.3f}s] {session}: {kind} {shown}")

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.entries)


class _LogCapture(logging.Handler):
    """Feeds matching stdlib log records (senselog + realtime debug) into the EventLog."""

    def __init__(self, log: EventLog, session: str, name_filter: str) -> None:
        super().__init__(level=logging.DEBUG)
        self._log = log
        self._session = session
        self._name_filter = name_filter

    def emit(self, record: logging.LogRecord) -> None:
        if self._name_filter not in record.name:
            return
        self._log.add(
            self._session,
            "log",
            logger=record.name,
            level=record.levelname,
            message=record.getMessage(),
        )


# --------------------------------------------------------------------------- #
# Audio: real recorded speech, forwards for A, reversed for B                 #
# --------------------------------------------------------------------------- #


def load_speech_pcm16(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wf:
        assert wf.getsampwidth() == 2, f"expected 16-bit PCM, got {wf.getsampwidth()*8}-bit"
        assert wf.getnchannels() == 1, f"expected mono, got {wf.getnchannels()} channels"
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    return raw, sr


def reversed_pcm16(pcm: bytes) -> bytes:
    samples = np.frombuffer(pcm, dtype="<i2")
    return samples[::-1].copy().tobytes()


# --------------------------------------------------------------------------- #
# Feeder: paces PCM16 bytes into a submit_audio()-shaped callable in ~real time
# --------------------------------------------------------------------------- #


def feed_paced(
    submit: "callable[[bytes], object]",
    pcm: bytes,
    sample_rate: int,
    duration_s: float,
    stop_event: threading.Event,
) -> int:
    """Loop *pcm* in CHUNK_MS chunks, real-time paced, for duration_s. Returns chunks sent."""
    chunk_bytes = int(sample_rate * CHUNK_MS / 1000) * 2
    t0 = time.monotonic()
    pos = 0
    sent = 0
    next_deadline = t0
    while (time.monotonic() - t0) < duration_s and not stop_event.is_set():
        if pos + chunk_bytes > len(pcm):
            pos = 0
        chunk = pcm[pos : pos + chunk_bytes]
        pos += chunk_bytes
        submit(chunk)
        sent += 1
        next_deadline += CHUNK_MS / 1000.0
        sleep_for = next_deadline - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
    return sent


# --------------------------------------------------------------------------- #
# Session B: hand-rolled armed-conversational client (no production class     #
# exists yet — t3/t9 build one; this is scratch-probe code only).             #
# --------------------------------------------------------------------------- #


class ArmedProbeSession:
    """Minimal /v1/realtime client that arms with response.create and logs everything."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        sample_rate: int,
        log: EventLog,
        arm_delay_s: float = DEFAULT_ARM_DELAY_S,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.sample_rate = sample_rate
        self.log = log
        self.arm_delay_s = arm_delay_s

        self.session_id: str | None = None
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.armed = False
        self._audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=256)
        self.stats: collections.Counter = collections.Counter()
        self._thread = threading.Thread(target=self._run, name="probe-session-b", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit_audio(self, pcm: bytes) -> None:
        try:
            self._audio_q.put_nowait(pcm)
        except queue.Full:
            self.stats["audio_dropped"] += 1

    def stop(self) -> None:
        self.closed.set()
        self._thread.join(timeout=5.0)

    # -- worker ------------------------------------------------------------ #

    def _run(self) -> None:
        url = rt.connect_url(self.base_url, self.sample_rate)
        parts = urlsplit(url)
        host = parts.hostname or "localhost"
        port = parts.port or 80
        path = parts.path or wire.REALTIME_PATH
        if parts.query:
            path = f"{path}?{parts.query}"
        key = wire.make_sec_websocket_key()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None

        buf = bytearray()
        try:
            sock = socket.create_connection((host, port), timeout=5.0)
        except OSError as err:
            self.log.add("B", "connect_failed", reason=str(err))
            return
        sock.settimeout(0.2)
        try:
            sock.sendall(wire.build_handshake_request(parts.netloc, path, key, headers))
            deadline = time.monotonic() + 5.0
            while b"\r\n\r\n" not in buf:
                if time.monotonic() > deadline:
                    self.log.add("B", "connect_failed", reason="handshake-timeout")
                    sock.close()
                    return
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    self.log.add("B", "connect_failed", reason="eof-during-handshake")
                    sock.close()
                    return
                buf.extend(chunk)
        except OSError as err:
            self.log.add("B", "connect_failed", reason=str(err))
            sock.close()
            return

        idx = buf.index(b"\r\n\r\n") + 4
        head = bytes(buf[:idx])
        del buf[:idx]
        status, resp_headers = wire.parse_response_head(head)
        if status != 101:
            self.log.add("B", "handshake_refused", status=status)
            sock.close()
            return
        if not wire.verify_accept_key(key, resp_headers.get("sec-websocket-accept", "")):
            self.log.add("B", "handshake_refused", reason="bad-accept-key")
            sock.close()
            return

        self.log.add("B", "connected", url=url)
        self.connected.set()
        t_connected = time.monotonic()

        def recv_exact(n: int) -> bytes:
            deadline_inner = time.monotonic() + 5.0
            while len(buf) < n:
                if time.monotonic() > deadline_inner or self.closed.is_set():
                    break
                try:
                    chunk = sock.recv(max(4096, n))
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
            take = min(n, len(buf))
            data = bytes(buf[:take])
            del buf[:take]
            return data

        armed_sent = False
        try:
            while not self.closed.is_set():
                sent = 0
                while sent < 32:
                    try:
                        pcm = self._audio_q.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        sock.sendall(
                            wire.build_frame(
                                wire.OPCODE_TEXT, wire.build_append_event(pcm).encode("utf-8")
                            )
                        )
                    except OSError as err:
                        self.log.add("B", "stream_closed", reason=f"send failed: {err}")
                        return
                    sent += 1
                if not armed_sent and (time.monotonic() - t_connected) >= self.arm_delay_s:
                    sock.sendall(
                        wire.build_frame(
                            wire.OPCODE_TEXT,
                            json.dumps({"type": "response.create"}).encode("utf-8"),
                        )
                    )
                    armed_sent = True
                    self.armed = True
                    self.log.add("B", "sent_response.create")
                try:
                    ready, _, _ = select.select([sock], [], [], 0.01)
                except OSError:
                    break
                if ready or buf:
                    try:
                        _fin, opcode, payload = wire.read_frame(recv_exact)
                    except wire.FrameReadError as err:
                        self.log.add("B", "stream_closed", reason=str(err))
                        break
                    if self._handle_frame(sock, opcode, payload):
                        break
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self.log.add("B", "socket_closed")

    def _handle_frame(self, sock: socket.socket, opcode: int, payload: bytes) -> bool:
        """Returns True if the session ended (caller should stop the loop)."""
        if opcode == wire.OPCODE_TEXT:
            event = wire.decode_event(payload)
            if event is None:
                self.stats["malformed"] += 1
                self.log.add("B", "malformed_event", size=len(payload))
                return False
            kind = event.get("type", "?")
            self.stats[kind] += 1
            if kind == "session.created":
                self.session_id = event.get("session_id")
                self.log.add(
                    "B", "session.created", session_id=self.session_id, config=event.get("config")
                )
            elif kind == "response.audio.delta":
                delta = event.get("delta", "") or ""
                self.log.add(
                    "B",
                    "response.audio.delta",
                    response_id=event.get("response_id"),
                    b64_chars=len(delta),
                )
            elif kind == "response.text.done":
                self.log.add(
                    "B",
                    "response.text.done",
                    response_id=event.get("response_id"),
                    text=event.get("text"),
                )
            elif kind == "error":
                self.log.add("B", "error", code=event.get("code"), message=event.get("message"))
            else:
                extra = {k: v for k, v in event.items() if k != "type"}
                self.log.add("B", kind, **extra)
            return False
        if opcode == wire.OPCODE_PING:
            try:
                sock.sendall(wire.build_frame(wire.OPCODE_PONG, payload))
            except OSError:
                return True
            return False
        if opcode == wire.OPCODE_PONG:
            return False
        if opcode == wire.OPCODE_CLOSE:
            self.log.add("B", "server_close")
            self.closed.set()
            return True
        self.log.add("B", "unknown_opcode", opcode=opcode)
        return False


# --------------------------------------------------------------------------- #
# Main probe                                                                  #
# --------------------------------------------------------------------------- #


def run_probe(duration_s: float, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    pcm_forward, sr = load_speech_pcm16(REAL_SPEECH_WAV)
    pcm_reversed = reversed_pcm16(pcm_forward)
    print(
        f"Loaded {REAL_SPEECH_WAV} : {len(pcm_forward)} bytes @ {sr} Hz "
        f"({len(pcm_forward)/2/sr:.2f}s)"
    )

    t0 = time.monotonic()
    log = EventLog(t0=t0)

    # Capture senselog ("reachy.sense") + this module's debug lines onto the log,
    # tagged per session so an ears-only violation on A is unmistakable.
    logging.getLogger().setLevel(logging.DEBUG)
    cap_a = _LogCapture(log, "A", "reachy.")
    logging.getLogger("reachy.sense").addHandler(cap_a)
    logging.getLogger("reachy.speech.realtime").addHandler(cap_a)
    logging.getLogger("reachy.sense").setLevel(logging.DEBUG)
    logging.getLogger("reachy.speech.realtime").setLevel(logging.DEBUG)

    base_url = rt.resolve_realtime_base_url()
    api_key = rt.resolve_realtime_api_key()
    print(f"base_url={base_url!r} api_key={'<set>' if api_key else None!r}")

    # --- Session A: the REAL production ears-only client ------------------ #
    utterances_a: list[dict] = []

    def on_utterance(u: rt.Utterance) -> None:
        entry = {
            "text": u.text,
            "session_id": u.session_id,
            "item_id": u.item_id,
            "t": round(time.monotonic() - t0, 3),
        }
        utterances_a.append(entry)
        log.add("A", "utterance", **entry)

    client_a = rt.RealtimeTranscriber(sample_rate=sr, on_utterance=on_utterance)
    client_a.start()

    # --- Session B: hand-rolled armed-conversational probe client --------- #
    session_b = ArmedProbeSession(
        base_url=base_url,
        api_key=api_key,
        sample_rate=sr,
        log=log,
        arm_delay_s=DEFAULT_ARM_DELAY_S,
    )
    session_b.start()

    stop_event = threading.Event()
    thread_a = threading.Thread(
        target=lambda: feed_paced(client_a.submit_audio, pcm_forward, sr, duration_s, stop_event),
        name="feed-a",
        daemon=True,
    )
    thread_b = threading.Thread(
        target=lambda: feed_paced(session_b.submit_audio, pcm_reversed, sr, duration_s, stop_event),
        name="feed-b",
        daemon=True,
    )
    log.add("meta", "probe_start", duration_s=duration_s, wav=str(REAL_SPEECH_WAV), sample_rate=sr)
    thread_a.start()
    thread_b.start()
    thread_a.join()
    thread_b.join()

    # Drain trailing events for a couple more seconds (a response mid-flight
    # when feeding stops should still finish).
    drain_deadline = time.monotonic() + 5.0
    while time.monotonic() < drain_deadline:
        while True:
            u = client_a.take_utterance()
            if u is None:
                break
            entry = {
                "text": u.text,
                "session_id": u.session_id,
                "item_id": u.item_id,
                "t": round(time.monotonic() - t0, 3),
            }
            utterances_a.append(entry)
            log.add("A", "utterance", **entry)
        time.sleep(0.1)

    summary = {
        "duration_s": duration_s,
        "elapsed_s": round(time.monotonic() - t0, 3),
        "session_a": {
            "connected": client_a.connected,
            "sessions": client_a.sessions,
            "connect_failures": client_a.connect_failures,
            "submitted": client_a.submitted,
            "sent": client_a.sent,
            "dropped": client_a.dropped,
            "utterances": client_a.utterances,
            "ignored_events": client_a.ignored_events,
            "session_down": client_a.session_down,
            "utterance_texts": [u["text"] for u in utterances_a],
        },
        "session_b": {
            "connected": session_b.connected.is_set(),
            "session_id": session_b.session_id,
            "armed": session_b.armed,
            "event_counts": dict(session_b.stats),
        },
    }

    client_a.close()
    session_b.stop()
    log.add("meta", "probe_end", **{k: v for k, v in summary.items() if k != "session_a"})

    logging.getLogger("reachy.sense").removeHandler(cap_a)
    logging.getLogger("reachy.speech.realtime").removeHandler(cap_a)

    events_path = out_dir / "2026-08-01-probe-concurrent-realtime-sessions.events.jsonl"
    with events_path.open("w") as fh:
        for entry in log.snapshot():
            fh.write(json.dumps(entry, default=str) + "\n")
    summary_path = out_dir / "2026-08-01-probe-concurrent-realtime-sessions.summary.json"
    with summary_path.open("w") as fh:
        json.dump(summary, fh, indent=2, default=str)

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nEvent log:   {events_path}")
    print(f"Summary:     {summary_path}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_S,
        help="seconds to feed both sessions (default: %(default)s)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("docs/evidence"),
        help="directory to write the event log + summary JSON",
    )
    args = parser.parse_args()
    run_probe(args.duration, args.out_dir)


if __name__ == "__main__":
    main()
