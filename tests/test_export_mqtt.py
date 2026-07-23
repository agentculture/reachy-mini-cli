"""Tests for ``reachy.export.mqtt`` — the nervous-system publisher (task t6).

The publisher is the leg that lets EXTERNAL services subscribe to Reachy's
senses. It is built entirely against an **injected client seam**: the broker and
its client live in the sibling ``events-cli`` project (``agentculture/events-cli#3``),
whose wheel does not exist yet, so this repo ships no MQTT library, no wire code
and no dependency — only the narrow
:class:`~reachy.export.mqtt.EventClient` protocol it requires, and a publisher
that is fully exercised here against :class:`FakeEventsClient`.

Coverage map (the four t6 acceptance criteria):

1. *Events + retained state.* A fake client receives runtime-feed events on
   ``reachy/events/{source}/{type}`` and retained state on
   ``reachy/state/{key}``; payloads are the ``docs/export-schema.md`` vocabulary
   verbatim (the SAME ``runtime_to_jsonl`` serializer the stdout feed uses), and
   the retained state equals the ``state.json`` payload produced by the ONE
   existing builder — an equality test, so the two surfaces cannot drift.
2. *Availability.* The publisher configures the Last Will on the retained
   ``reachy/state/online`` topic BEFORE connecting, flips it true on connect,
   and re-publishes current retained state on every reconnect.
3. *Degrade.* An absent, incompatible, unreachable or raising client resolves to
   ONE named ``senselog`` drop and no-op publishes — never an exception on the
   caller's thread — and a publish at the seam is an O(1) enqueue with no
   network I/O on the calling thread.
4. *No media on the bus.* Lives in ``tests/test_nervous_media_boundary.py``.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
from pathlib import Path

import pytest

from reachy.behavior.control import CommandSpool
from reachy.behavior.engine import Engine, EngineConfig
from reachy.behavior.sense import EMPTY_SENSE
from reachy.export import mqtt as M
from reachy.export.runtime import (
    RUNTIME_BLOCKS,
    MotionEvent,
    RuleEvent,
    SenseEvent,
    runtime_to_jsonl,
    to_runtime_event,
)
from tests.fake_events_client import BlockingTrapClient, FakeEventsClient

SENSE_LOGGER = "reachy.sense"
REPO_ROOT = Path(__file__).parent.parent


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _live_publisher(**kwargs) -> tuple[M.NervousPublisher, FakeEventsClient]:
    client = FakeEventsClient()
    pub = M.NervousPublisher(client, **kwargs)
    pub.start()
    return pub, client


def _raw_sense(**over) -> dict:
    base = {
        "type": "sense",
        "doa": 0.5,
        "speech": True,
        "rms": 0.02,
        "pat": ["scratch", "level1"],
        "face": "ori",
        "frame_available": True,
        "ts": 1718362800.0,
        "tick": 7,
    }
    base.update(over)
    return base


def _drop_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "dropped reason=" in r.getMessage()]


def _stage_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == SENSE_LOGGER and "dropped reason=" not in r.getMessage()
    ]


# =========================================================================== #
# Criterion 1 — events on reachy/events/{source}/{type}, retained state on
#               reachy/state/{key}, payloads = the export-schema vocabulary
# =========================================================================== #


def test_runtime_events_reach_the_bus_on_reachy_events_source_type() -> None:
    """Every runtime block type maps onto ``reachy/events/{source}/{type}``."""
    pub, client = _live_publisher()
    consume = pub.as_tick_consumer()

    consume(_raw_sense())
    consume({"type": "rule.fire", "rule": "greet", "kind": "react", "field": "speech"})
    consume({"type": "rule.suppress", "rule": "greet", "reason": "cooldown"})
    consume({"type": "intent.declare", "name": "stay-alert"})
    consume({"type": "intent.blocked", "kind": "run_behavior"})
    consume({"type": "motion.admit", "behavior": "nod", "channels": ["head"]})
    consume({"type": "goto.admitted", "id": "g1"})

    event_topics = [t for t in client.topics() if t.startswith("reachy/events/")]
    assert event_topics == [
        "reachy/events/sense/snapshot",
        "reachy/events/rule/fire",
        "reachy/events/rule/suppress",
        "reachy/events/intent/declare",
        "reachy/events/intent/blocked",
        "reachy/events/motion/admit",
        "reachy/events/motion/goto",
    ]


def test_event_payload_is_the_export_schema_line_verbatim() -> None:
    """The bus payload is the SAME serializer the stdout runtime feed uses.

    One serializer, two transports — so the broker payload and the NDJSON line
    are structurally incapable of drifting.
    """
    pub, client = _live_publisher()
    raw = _raw_sense()
    pub.as_tick_consumer()(raw)

    published = client.by_topic("reachy/events/sense/snapshot")
    assert len(published) == 1
    assert published[0].payload == runtime_to_jsonl(to_runtime_event(raw))

    obj = json.loads(published[0].payload)
    assert obj["t"] == "sense" and obj["t"] in RUNTIME_BLOCKS
    assert obj["ts"] == 1718362800.0
    assert obj["tick"] == 7
    # The documented sense vocabulary, key for key (docs/export-schema.md).
    assert set(obj) >= {
        "t",
        "ts",
        "tick",
        "doa",
        "speech",
        "rms",
        "pat",
        "face",
        "frame_available",
    }


def test_state_is_published_retained_one_topic_per_key() -> None:
    pub, client = _live_publisher()
    pub.publish_state({"updated": 12.5, "compose_hz": 50.0, "ownership": {"head": None}})

    state_topics = {p.topic for p in client.published if p.topic.startswith("reachy/state/")}
    assert state_topics == {
        "reachy/state/online",
        "reachy/state/updated",
        "reachy/state/compose_hz",
        "reachy/state/ownership",
    }
    for pubd in client.published:
        if pubd.topic.startswith("reachy/state/"):
            assert pubd.retain is True, f"{pubd.topic} must be retained"


def test_retained_state_equals_the_state_json_payload_from_the_one_builder(tmp_path) -> None:
    """The bus mirrors ``state.json``; it never re-derives state.

    The publisher wraps the ONE existing writer (``CommandSpool.write_state``,
    fed by ``Engine.state``), so reassembling the retained topics reproduces the
    file byte-identically. If someone ever gives the bus a second derivation,
    this equality breaks.
    """
    engine = Engine()
    config = EngineConfig()
    engine.seed_base_layer(0.0, config.energy)
    engine.compose_tick(0.02, EMPTY_SENSE)

    spool = CommandSpool(root=tmp_path)
    pub, client = _live_publisher()
    write_state = pub.state_writer(spool.write_state)

    payload = engine.state(0.02, config)
    write_state(payload)

    on_disk = json.loads((tmp_path / "behavior" / "state.json").read_text(encoding="utf-8"))
    from_bus = {
        p.topic.rsplit("/", 1)[-1]: json.loads(p.payload)
        for p in client.published
        if p.topic.startswith("reachy/state/") and p.topic != "reachy/state/online"
    }
    assert from_bus == on_disk
    assert from_bus  # the builder produced something to compare


def test_state_writer_writes_the_file_even_when_the_bus_is_dead(tmp_path) -> None:
    """The mirror is additive: a dead bus never costs the runtime its state file."""
    spool = CommandSpool(root=tmp_path)
    pub = M.NervousPublisher(None)
    pub.start()
    pub.state_writer(spool.write_state)({"updated": 1.0})
    assert json.loads((tmp_path / "behavior" / "state.json").read_text()) == {"updated": 1.0}


def test_events_are_not_retained_and_every_publish_is_qos_zero() -> None:
    pub, client = _live_publisher()
    pub.as_tick_consumer()(_raw_sense())
    pub.publish_state({"updated": 1.0})

    for pubd in client.published:
        assert pubd.qos == 0, f"{pubd.topic} published at QoS {pubd.qos}, expected 0"
    for pubd in client.published:
        if pubd.topic.startswith("reachy/events/"):
            assert pubd.retain is False, "events must not be retained"


def test_unrecognised_raw_events_never_reach_the_bus() -> None:
    """The tick consumer reuses ``to_runtime_event``: unknown shapes map to nothing."""
    pub, client = _live_publisher()
    consume = pub.as_tick_consumer()
    consume({"type": "something.new"})
    consume({"tick": 1})
    consume({})
    assert [t for t in client.topics() if t.startswith("reachy/events/")] == []


def test_topic_segments_are_sanitised(caplog) -> None:
    """A wildcard/separator character can never be smuggled into a topic."""
    pub, client = _live_publisher()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.emit(RuleEvent(action="fire/+#", rule="r", kind="react", field="f", op="o", reason="x"))
    assert client.topics()[-1] == "reachy/events/rule/fire---"


def test_the_topic_root_is_injectable() -> None:
    pub, client = _live_publisher(root="robot9")
    pub.as_tick_consumer()(_raw_sense())
    pub.publish_state({"updated": 1.0})
    assert "robot9/events/sense/snapshot" in client.topics()
    assert "robot9/state/updated" in client.topics()
    assert pub.online_topic == "robot9/state/online"
    assert pub.events_root == "robot9/events"
    assert pub.state_root == "robot9/state"


def test_an_event_outside_the_feed_vocabulary_is_dropped_by_name(caplog) -> None:
    """Only the four documented block types can reach a topic."""
    pub, client = _live_publisher()
    client.published.clear()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.emit(object())

    assert any(M.REASON_UNKNOWN_EVENT in line for line in _drop_lines(caplog))
    assert client.published == []


# =========================================================================== #
# Criterion 2 — retained availability topic, LWT, reconnect republish
# =========================================================================== #


def test_last_will_is_configured_before_connect_on_the_retained_online_topic() -> None:
    client = FakeEventsClient()
    pub = M.NervousPublisher(client)
    pub.start()

    assert client.will is not None
    assert client.will.topic == "reachy/state/online"
    assert json.loads(client.will.payload) is False
    assert client.will.retain is True
    assert client.will.qos == 0
    assert client.calls.index("will_set") < client.calls.index(
        "connect"
    ), "the Last Will must be configured BEFORE connecting, or it is never registered"


def test_online_flips_true_retained_on_connect(caplog) -> None:
    client = FakeEventsClient()
    pub = M.NervousPublisher(client)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.start()

    online = client.by_topic("reachy/state/online")
    assert len(online) == 1
    assert json.loads(online[0].payload) is True
    assert online[0].retain is True
    assert any("connected" in line for line in _stage_lines(caplog))
    assert pub.connected is True


def test_reconnect_republishes_online_and_current_retained_state(caplog) -> None:
    """Without this, a dead runtime leaves permanently-stale retained state."""
    pub, client = _live_publisher()
    pub.publish_state({"updated": 1.0, "compose_hz": 50.0})
    client.published.clear()

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        client.go_offline()
        pub.publish_state({"updated": 2.0, "compose_hz": 50.0})  # noticed: one named drop
        assert client.published == [], "a disconnected client must take no publishes"

        client.go_online()
        pub.emit(
            SenseEvent(doa=None, speech=False, rms=None, pat=None, face=None, frame_available=False)
        )

    republished = {p.topic: json.loads(p.payload) for p in client.published}
    assert republished["reachy/state/online"] is True
    assert republished["reachy/state/updated"] == 2.0, "the CURRENT state, not the stale one"
    assert republished["reachy/state/compose_hz"] == 50.0
    assert any("reconnect" in line for line in _stage_lines(caplog))


def test_reconnect_is_noticed_through_the_clients_own_callback_when_offered() -> None:
    """A client offering ``set_on_connect`` gets a reconnect noticed immediately."""
    client = FakeEventsClient(autoconnect=False)
    pub = M.NervousPublisher(client)
    pub.start()
    assert "set_on_connect" in client.calls
    assert client.published == []

    client.go_online()  # fires the callback with no publish call in between
    assert json.loads(client.by_topic("reachy/state/online")[0].payload) is True


def test_a_session_that_never_establishes_is_named_once_not_silent(caplog) -> None:
    """Broker-not-up-yet is normal — but it may never be a SILENT no-op."""
    client = FakeEventsClient(autoconnect=False)
    pub = M.NervousPublisher(client)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        assert pub.start() is False
        for _ in range(100):
            pub.as_tick_consumer()(_raw_sense())
        pub.publish_state({"updated": 1.0})

    drops = _drop_lines(caplog)
    assert len(drops) == 1
    assert M.REASON_BROKER_UNREACHABLE in drops[0]
    assert client.published == []


def test_a_late_session_clears_the_latch_and_republishes(caplog) -> None:
    """The drop latch is per-session: a later outage is named again."""
    client = FakeEventsClient(autoconnect=False)
    pub = M.NervousPublisher(client)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.start()
        pub.publish_state({"updated": 1.0})  # cached while offline
        client.go_online()
        client.go_offline()
        pub.as_tick_consumer()(_raw_sense())

    drops = _drop_lines(caplog)
    assert len(drops) == 2, f"the second outage must be named again, got {drops}"
    assert all(M.REASON_BROKER_UNREACHABLE in line for line in drops)
    assert json.loads(client.by_topic("reachy/state/updated")[0].payload) == 1.0


def test_graceful_stop_publishes_offline_and_disconnects(caplog) -> None:
    pub, client = _live_publisher()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.stop()

    online = client.by_topic("reachy/state/online")
    assert json.loads(online[-1].payload) is False
    assert online[-1].retain is True
    assert client.disconnect_calls == 1
    assert pub.connected is False


def test_a_raising_disconnect_is_named_not_raised(caplog) -> None:
    class StuckClient(FakeEventsClient):
        def disconnect(self):
            raise OSError("socket already gone")

    client = StuckClient()
    pub = M.NervousPublisher(client)
    pub.start()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.stop()  # must not raise
    assert any(M.REASON_DISCONNECT_FAILED in line for line in _drop_lines(caplog))


def test_start_is_idempotent() -> None:
    pub, client = _live_publisher()
    assert pub.start() is True
    assert client.connect_calls == 1
    assert len(client.by_topic("reachy/state/online")) == 1


def test_stop_is_idempotent_and_safe_without_a_client() -> None:
    M.NervousPublisher(None).stop()  # must not raise
    pub, client = _live_publisher()
    pub.stop()
    pub.stop()
    assert client.disconnect_calls == 1


# =========================================================================== #
# Criterion 3 — degrade to ONE named drop + no-op publishes, O(1), no I/O
# =========================================================================== #


def test_absent_client_yields_exactly_one_named_drop_and_no_exception(caplog) -> None:
    pub = M.NervousPublisher(None)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.start()
        consume = pub.as_tick_consumer()
        for _ in range(200):
            consume(_raw_sense())
            pub.publish_state({"updated": 1.0})
        pub.stop()

    drops = _drop_lines(caplog)
    assert len(drops) == 1, f"expected exactly one named drop, got {drops}"
    assert f"dropped reason={M.REASON_NO_CLIENT}" in drops[0]
    assert "[SENSE stage=nervous source=mqtt event=" in drops[0]
    assert pub.degraded is True


def test_incompatible_client_yields_exactly_one_named_drop(caplog) -> None:
    """The events-cli wheel does not exist yet: a shape mismatch must degrade."""

    class HalfBakedClient:
        def publish(self, *_a, **_kw):  # no connect / will_set / connected
            raise AssertionError("must never be reached")

    pub = M.NervousPublisher(HalfBakedClient())
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.start()
        pub.as_tick_consumer()(_raw_sense())
        pub.publish_state({"updated": 1.0})

    drops = _drop_lines(caplog)
    assert len(drops) == 1
    assert M.REASON_CLIENT_INCOMPATIBLE in drops[0]
    assert "connect" in drops[0] and "will_set" in drops[0], "the drop must name what is missing"


def test_connect_failure_yields_one_named_drop_and_the_runtime_continues(caplog) -> None:
    client = FakeEventsClient()
    client.raise_on_connect = ConnectionRefusedError("no broker on :1883")
    pub = M.NervousPublisher(client)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        assert pub.start() is False
        for _ in range(50):
            pub.as_tick_consumer()(_raw_sense())
        pub.publish_state({"updated": 1.0})
        pub.stop()

    drops = _drop_lines(caplog)
    assert len(drops) == 1
    assert M.REASON_CONNECT_FAILED in drops[0]
    assert client.published == []


def test_a_disconnected_broker_is_named_once_and_publishes_become_noops(caplog) -> None:
    pub, client = _live_publisher()
    client.published.clear()  # drop the connect-time availability publish
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        client.go_offline()
        for _ in range(200):
            pub.as_tick_consumer()(_raw_sense())

    drops = _drop_lines(caplog)
    assert len(drops) == 1
    assert M.REASON_BROKER_UNREACHABLE in drops[0]
    assert client.published == []


def test_a_raising_publish_never_reaches_the_caller_and_is_named_once(caplog) -> None:
    pub, client = _live_publisher()
    client.raise_on_publish = OSError("broker went away mid-publish")

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        for _ in range(100):
            pub.as_tick_consumer()(_raw_sense())  # must not raise
        pub.publish_state({"updated": 1.0})

    drops = _drop_lines(caplog)
    assert len(drops) == 1
    assert M.REASON_PUBLISH_FAILED in drops[0]
    assert pub.failed_publishes >= 100


def test_an_unserializable_payload_is_dropped_by_name_not_raised(caplog) -> None:
    pub, client = _live_publisher()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.publish_state({"updated": 1.0, "junk": object()})

    drops = _drop_lines(caplog)
    assert len(drops) == 1
    assert M.REASON_UNSERIALIZABLE in drops[0]
    assert "reachy/state/updated" in client.topics(), "one bad key must not lose the others"
    assert "reachy/state/junk" not in client.topics()


def test_a_reserved_state_key_cannot_shadow_the_availability_topic(caplog) -> None:
    pub, client = _live_publisher()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.publish_state({"online": "definitely not"})

    assert any(M.REASON_RESERVED_STATE_KEY in line for line in _drop_lines(caplog))
    assert [json.loads(p.payload) for p in client.by_topic("reachy/state/online")] == [True]


def test_a_non_mapping_state_is_dropped_by_name(caplog) -> None:
    pub, _client = _live_publisher()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.publish_state(["not", "a", "mapping"])
    assert any(M.REASON_BAD_STATE in line for line in _drop_lines(caplog))


@pytest.mark.offline
def test_publishing_opens_no_socket_on_the_calling_thread(monkeypatch) -> None:
    """The seam does NO network I/O: the client owns the transport.

    ``socket.socket`` itself is made explosive (the ``offline`` marker already
    blocks connect/create_connection), so any attempt by this module to touch
    the network on the caller's thread is a loud failure rather than a silent
    latency cost on the 50 Hz tick.
    """

    def _boom(*_a, **_kw):
        raise AssertionError("the publisher touched the network on the caller's thread")

    pub, client = _live_publisher()
    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    consume = pub.as_tick_consumer()
    for _ in range(500):
        consume(_raw_sense())
    pub.publish_state({"updated": 1.0})

    assert len(client.published) >= 500


def test_publish_is_one_o1_enqueue_per_event_with_no_growing_buffer() -> None:
    """One ``client.publish`` per event, and the publisher accumulates nothing.

    A growing internal container would make the per-tick cost a function of run
    length — exactly the class of defect the runtime's tick budget cannot carry.
    """
    pub, client = _live_publisher()
    consume = pub.as_tick_consumer()

    def _container_sizes() -> dict:
        return {
            name: len(value)
            for name, value in vars(pub).items()
            if isinstance(value, (list, dict, set, tuple))
        }

    consume(_raw_sense())
    baseline_calls = len(client.published)
    baseline_sizes = _container_sizes()

    for tick in range(1000):
        consume(_raw_sense(tick=tick, rms=tick / 1000.0))

    assert len(client.published) == baseline_calls + 1000, "exactly one enqueue per event"
    assert _container_sizes() == baseline_sizes, "the publisher must hold no per-event state"


def test_the_seam_never_calls_a_blocking_client_method() -> None:
    client = BlockingTrapClient()
    pub = M.NervousPublisher(client)
    pub.start()
    pub.as_tick_consumer()(_raw_sense())
    pub.publish_state({"updated": 1.0})
    pub.stop()
    assert set(client.calls) <= {"will_set", "set_on_connect", "connect", "publish", "disconnect"}


def test_a_client_whose_connected_probe_raises_degrades_by_name(caplog) -> None:
    class SickClient(FakeEventsClient):
        @property
        def connected(self):
            raise RuntimeError("client internals blew up")

    client = SickClient(autoconnect=False)
    pub = M.NervousPublisher(client)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.start()
        pub.as_tick_consumer()(_raw_sense())

    assert _drop_lines(caplog), "a sick client must be named, not silent"
    assert any(M.REASON_CLIENT_INCOMPATIBLE in line for line in _drop_lines(caplog))
    assert client.published == []


def test_a_client_that_turns_sick_mid_run_is_named_once(caplog) -> None:
    """The liveness probe is client code: it may start raising at any tick."""

    class TurnsSickClient(FakeEventsClient):
        sick = False

        @property
        def connected(self):
            if self.sick:
                raise RuntimeError("client internals blew up")
            return self._connected

    client = TurnsSickClient()
    pub = M.NervousPublisher(client)
    pub.start()
    client.published.clear()

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        client.sick = True
        for _ in range(100):
            pub.as_tick_consumer()(_raw_sense())  # must not raise

    drops = _drop_lines(caplog)
    assert len(drops) == 1
    assert M.REASON_BROKER_UNREACHABLE in drops[0]
    assert "connected probe raised" in drops[0]
    assert client.published == []


def test_publishing_from_many_threads_never_raises() -> None:
    """The tick thread and the client's callback thread both reach the seam."""
    pub, client = _live_publisher()
    consume = pub.as_tick_consumer()
    errors: list[BaseException] = []

    def _worker(n: int) -> None:
        try:
            for i in range(200):
                consume(_raw_sense(tick=n * 1000 + i))
                if i % 50 == 0:
                    pub.publish_state({"updated": float(i)})
        except BaseException as err:  # pragma: no cover - a failure is the point
            errors.append(err)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(client.published) >= 800


# =========================================================================== #
# The hard constraint: no MQTT library in this repo
# =========================================================================== #


def test_the_publisher_module_imports_no_mqtt_library() -> None:
    """The transport is events-cli's; this repo holds only the seam."""
    source = (REPO_ROOT / "reachy" / "export" / "mqtt.py").read_text(encoding="utf-8")
    forbidden = re.compile(
        r"^\s*(import|from)\s+(paho|gmqtt|asyncio_mqtt|aiomqtt|amqtt|hbmqtt|socket|ssl)\b",
        re.MULTILINE,
    )
    hits = [line.strip() for line in source.splitlines() if forbidden.match(line)]
    assert hits == [], f"an MQTT/transport library leaked into the seam module: {hits}"


def test_broker_url_reads_the_env_with_a_loopback_default(monkeypatch) -> None:
    monkeypatch.delenv(M.BROKER_URL_ENV, raising=False)
    assert M.broker_url() == M.DEFAULT_BROKER_URL == "localhost:1883"
    monkeypatch.setenv(M.BROKER_URL_ENV, "mqtt://10.0.0.5:1883")
    assert M.broker_url() == "mqtt://10.0.0.5:1883"


def test_the_declared_client_protocol_is_the_whole_requirement() -> None:
    """The Protocol is the contract handed back to events-cli#3 — pin it."""
    required = set(M.REQUIRED_CLIENT_MEMBERS)
    assert required == {"connect", "disconnect", "publish", "will_set", "connected"}
    assert M.OPTIONAL_CLIENT_MEMBERS == ("set_on_connect",)
    # A fake implementing only the required set is enough to run the publisher.
    assert M.missing_client_members(FakeEventsClient()) == ()


def test_a_publisher_used_as_a_runtime_sink_matches_the_exporter_shape() -> None:
    """``NervousPublisher`` is sink-shaped (``emit``), so ``RuntimeConsumer`` drives it."""
    pub, client = _live_publisher()
    pub.emit(MotionEvent(action="admit", behavior="nod", channels=["head"], ts=1.0, tick=3))
    assert client.topics()[-1] == "reachy/events/motion/admit"
    assert json.loads(client.published[-1].payload)["behavior"] == "nod"
