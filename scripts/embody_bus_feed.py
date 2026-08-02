"""Bridge the runtime's MQTT event bus into the embodiment layer's --feed FIFO.

Why this exists: `agent embody --feed` wants the runtime's JSONL export, but the
deployed runtime runs under systemd with no `--export`, and wiring one would
mean a FIFO the runtime BLOCKS on if nothing is reading. The runtime already
publishes the same lines to MQTT unconditionally, and the bus payload is
byte-identical to a feed line — so this subscribes there instead and writes them
on. The runtime is untouched and cannot be stalled by the layer going away.

Opens the FIFO O_RDWR on purpose: that never blocks on open regardless of start
order, and keeps the pipe from EOF-ing if the layer restarts. It never reads,
so it does not compete for bytes with the real consumer.

By default it forwards the runtime's DECISIONS (rule / intent / motion) and
drops the `sense` snapshot stream. That is not a rate hack, it is what the layer
actually needs: it has its OWN ears on a realtime session, so runtime sense cues
("speech from the left", "loud sound ahead") duplicate what it already hears
while arriving at tick rate. Measured unfiltered: 187 cues and 23 turns in ~40 s,
with 19 `input-queue-full` drops and not one rule fire in the mix — the flood was
entirely sense. What the layer cannot learn on its own is what the runtime
DECIDED, which is exactly what rule/intent/motion carry.

Set REACHY_BUS_FEED_SOURCES to override (comma-separated, `*` for everything).

Stop it and the layer simply stops receiving cues; it keeps hearing and
speaking over its own realtime session.
"""

from __future__ import annotations

import os
import sys

import paho.mqtt.client as mqtt

FIFO = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.path.expanduser("~/.local/state/reachy/embody-feed.fifo")
)
#: Which `reachy/events/<source>/...` streams to forward. `sense` is excluded by
#: default — see the module docstring.
SOURCES = tuple(
    s.strip()
    for s in os.environ.get("REACHY_BUS_FEED_SOURCES", "rule,intent,motion").split(",")
    if s.strip()
)
BROKER = os.environ.get("REACHY_MQTT_URL", "localhost:1883")
host, _, port = BROKER.partition(":")

if not os.path.exists(FIFO):
    os.mkfifo(FIFO, 0o600)
fd = os.open(FIFO, os.O_RDWR | os.O_NONBLOCK)
sent = dropped = 0


def _on_connect(client, _u, _f, _rc, _p=None):
    if "*" in SOURCES:
        client.subscribe("reachy/events/#")
    else:
        for source in SOURCES:
            client.subscribe(f"reachy/events/{source}/#")
    print(f"[bus-feed] forwarding {list(SOURCES)} -> {FIFO}", flush=True)


def _on_message(_c, _u, msg):
    global sent, dropped
    try:
        os.write(fd, msg.payload + b"\n")
        sent += 1
    except BlockingIOError:
        dropped += 1  # nobody draining: drop rather than stall the bus thread
    except OSError as err:
        dropped += 1
        print(f"[bus-feed] write failed: {err}", flush=True)
    if (sent + dropped) % 500 == 0:
        print(f"[bus-feed] sent={sent} dropped={dropped}", flush=True)


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = _on_connect
client.on_message = _on_message
client.connect(host or "localhost", int(port or 1883), 30)
client.loop_forever()
