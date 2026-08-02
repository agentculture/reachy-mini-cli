"""Bridge the runtime's MQTT event bus into the embodiment layer's --feed FIFO.

Why this exists: `agent embody --feed` wants the runtime's JSONL export, but the
deployed runtime runs under systemd with no `--export`, and wiring one would
mean a FIFO the runtime BLOCKS on if nothing is reading. The runtime already
publishes the same lines to MQTT unconditionally, and the bus payload is
byte-identical to a feed line (see `docs/export-schema.md`) — so this
subscribes there instead and writes them on. The runtime is untouched and
cannot be stalled by the layer going away.

Why this lives in `scripts/`, not `reachy/`: `reachy/embody/cues.py`'s own bus
intake (`resolve_bus_subscriber`) can never bind a real client today —
`events-cli>=0.9` ships no `subscribe` surface at all (verified live), and
this repo's own code names and imports no MQTT library directly
(`test_h10_no_mqtt_library_became_a_direct_dependency` /
`test_h10_no_module_in_this_repo_imports_an_mqtt_library` enforce that for
everything under `reachy/`). This script is what makes the bus route usable
anyway: it speaks `paho-mqtt` directly — already in the resolved dependency
tree as `events-cli`'s own base dep, so nothing new is installed — from
OUTSIDE that boundary, and writes onto the same FIFO the layer's feed-tail
fallback already knows how to read. Nothing it does is package code the
zero-MQTT tests are scoped to.

The FIFO is opened `O_RDWR | O_NONBLOCK` on purpose, and both parts of that
choice are load-bearing (see `open_feed_fifo` and
`docs/operating-reachy.md`'s "The bus bridge" section for the full mechanics
and the live incident this fixes): `O_NONBLOCK` means the open itself never
blocks or fails regardless of which process — this bridge or the layer —
starts first; `O_RDWR` means the bridge's own descriptor counts as a reader
of the FIFO for as long as the bridge lives, which is what keeps the pipe
from EOF-ing on any OTHER reader purely because the layer's own `--feed`
attachment restarts. It does NOT protect against the bridge's own exit —
closing that descriptor is exactly what removes the FIFO's last writer, and
that is precisely what happened during live testing: the bridge exited, the
layer's cue reader hit EOF on its next read, and the layer died alongside a
process it does not even know exists. The fix for that is operational (run
this under a supervisor that restarts it), not a code change to the FIFO
itself. This script never reads from the descriptor it holds, so it never
competes for bytes with the real consumer.

By default it forwards the runtime's DECISIONS (rule / intent / motion) and
drops the `sense` snapshot stream. That is an INTERIM MITIGATION, not the
fix — a bridge process is the wrong layer to own the runtime's trigger
policy, and it does nothing for an operator who feeds the layer from the
runtime's own `--export -` instead of this bridge; issue #143 moves that
policy into `EmbodyTurnEngine` itself. It exists because forwarding `sense`
unfiltered actually flooded the layer: measured, 187 cues and 23 turns in
~40 s, with 19 `input-queue-full` drops and not one rule fire in the mix — the
flood was entirely sense. What the layer cannot learn on its own is what the
runtime DECIDED, which is exactly what rule/intent/motion carry.

Set REACHY_BUS_FEED_SOURCES to override (comma-separated, `*` for
everything). Every filter this produces is scoped under `reachy/events/` —
never `reachy/state/#`, the runtime's RETAINED tree — so a reconnect can
never replay the robot's last-known pose/state into a cue as if it just
happened; see `topic_filters`.

Stop it and the layer simply stops receiving cues; it keeps hearing and
speaking over its own realtime session.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import paho.mqtt.client as mqtt

#: Default FIFO path when no path is given on argv. Deliberately a literal
#: rather than `reachy.daemon.state_dir()` — this script has no import edge
#: to the `reachy` package at all, on either side of the bridge.
DEFAULT_FIFO_PATH = os.path.expanduser("~/.local/state/reachy/embody-feed.fifo")

#: The `REACHY_BUS_FEED_SOURCES` default: forward decisions, drop `sense`. See
#: the module docstring for why this is a mitigation, not the fix.
DEFAULT_SOURCES = "rule,intent,motion"


def resolve_sources(raw: str | None) -> tuple[str, ...]:
    """Parse a `REACHY_BUS_FEED_SOURCES`-shaped comma list.

    ``raw=None`` (the env var unset) resolves to :data:`DEFAULT_SOURCES`.
    Blank entries — a stray comma, surrounding whitespace — are dropped,
    matching ``os.environ.get("REACHY_BUS_FEED_SOURCES",
    DEFAULT_SOURCES).split(",")``'s original behaviour byte for byte.
    """
    text = DEFAULT_SOURCES if raw is None else raw
    return tuple(s.strip() for s in text.split(",") if s.strip())


def topic_filters(sources: tuple[str, ...]) -> tuple[str, ...]:
    """Bus topic filters to subscribe for *sources* — events-only, by construction.

    Every filter this returns starts with ``reachy/events/`` — there is no
    way for this function to name ``reachy/state/#``, the RETAINED tree the
    runtime also publishes, which is what keeps a stale retained pose/state
    value from ever replaying into a cue on reconnect. ``"*"`` anywhere in
    *sources* subscribes the whole events tree in one filter, matching the
    published-events shape exactly (never widening into ``reachy/state``).
    """
    if "*" in sources:
        return ("reachy/events/#",)
    return tuple(f"reachy/events/{source}/#" for source in sources)


def open_feed_fifo(path: str) -> int:
    """Create *path* as a FIFO if needed and open it ``O_RDWR | O_NONBLOCK``.

    See the module docstring for why both flags are load-bearing: the open
    never blocks or fails regardless of start order, and the returned
    descriptor holds a permanent reader-of-record on the FIFO for the life of
    the process, which is what stops the pipe from going writer-less (and any
    other reader from seeing EOF) purely because the layer's own `--feed`
    attachment comes and goes. It does not, and cannot, survive this
    descriptor itself closing — see `docs/operating-reachy.md`'s "The bus
    bridge" section for the live incident that distinction cost.
    """
    if not os.path.exists(path):
        os.mkfifo(path, 0o600)
    return os.open(path, os.O_RDWR | os.O_NONBLOCK)


class BusFeedForwarder:
    """Forwards raw MQTT payloads onto an open FIFO descriptor, verbatim.

    A pipe, not a translator: the runtime already publishes
    ``reachy/events/<source>/<type>`` payloads that are byte-identical to a
    feed line (``docs/export-schema.md``), so :meth:`on_message` never parses
    or re-serializes anything — it writes ``msg.payload + b"\\n"`` and
    nothing else, regardless of whether the payload is even valid JSON.
    """

    def __init__(self, fd: int, sources: tuple[str, ...]) -> None:
        self.fd = fd
        self.sources = sources
        self.sent = 0
        self.dropped = 0

    def on_connect(
        self, client: Any, _userdata: Any, _flags: Any, _rc: Any, _props: Any = None
    ) -> None:
        for topic in topic_filters(self.sources):
            client.subscribe(topic)
        print(f"[bus-feed] forwarding {list(self.sources)} -> fd={self.fd}", flush=True)

    def on_message(self, _client: Any, _userdata: Any, msg: Any) -> None:
        try:
            os.write(self.fd, msg.payload + b"\n")
            self.sent += 1
        except BlockingIOError:
            self.dropped += 1  # nobody draining: drop rather than stall the bus thread
        except OSError as err:
            self.dropped += 1
            print(f"[bus-feed] write failed: {err}", flush=True)
        if (self.sent + self.dropped) % 500 == 0:
            print(f"[bus-feed] sent={self.sent} dropped={self.dropped}", flush=True)


def parse_broker(broker: str) -> tuple[str, int]:
    """Split a ``REACHY_MQTT_URL``-shaped ``host:port`` string into its parts."""
    host, _, port = broker.partition(":")
    return host or "localhost", int(port or 1883)


def main(argv: list[str] | None = None) -> None:
    """Wire a real paho client to a :class:`BusFeedForwarder` and run forever.

    Never imported for its side effects — only ``if __name__ ==
    "__main__"`` below calls this, so importing this module (as the test
    suite does) never opens a socket or a FIFO.
    """
    args = sys.argv[1:] if argv is None else argv
    fifo_path = args[0] if args else DEFAULT_FIFO_PATH
    sources = resolve_sources(os.environ.get("REACHY_BUS_FEED_SOURCES"))
    fd = open_feed_fifo(fifo_path)
    forwarder = BusFeedForwarder(fd, sources)

    host, port = parse_broker(os.environ.get("REACHY_MQTT_URL", "localhost:1883"))
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = forwarder.on_connect
    client.on_message = forwarder.on_message
    client.connect(host, port, 30)
    client.loop_forever()


if __name__ == "__main__":
    main()
