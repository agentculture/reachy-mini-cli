"""The events-cli binding: does the vendor's real client drive our publisher?

``tests/test_export_mqtt.py`` proves :class:`~reachy.export.mqtt.NervousPublisher`
against a fake shaped like the surface this repo DECLARES. That is the right
test for the publisher and the wrong test for the binding: a fake built from our
own protocol agrees with us by construction, so it cannot notice that the
shipped ``events_cli.EventClient`` names things differently
(``is_connected``/``close``, and a constructor-time Last Will).

This module tests the other side — that
:mod:`reachy.export.events_client` turns the REAL vendor shape into the declared
one, and that our own fail-closed probe accepts the result. The end-to-end case
at the bottom runs the actual vendor class with no broker running, which is the
check that fails loudly if events-cli ever changes its API again.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from reachy.export import events_client as EC
from reachy.export import mqtt as M

# --------------------------------------------------------------------------- #
# A double shaped like the VENDOR (not like our protocol)                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Will:
    """Mirrors ``events_cli.Will`` — resolved from the factory's own module."""

    topic: str
    payload: str = ""
    qos: int = 0
    retain: bool = False


@dataclass(frozen=True)
class PublishResult:
    ok: bool
    connected: bool
    reason: str


class VendorDouble:
    """The vendor's surface exactly: ``is_connected``, ``close``, will-at-init."""

    def __init__(self, host, port, *, connect=True, will=None, **kwargs):
        self.host = host
        self.port = port
        self.will = will
        self.kwargs = kwargs
        self.published: list[tuple] = []
        self.closed = 0
        self.raise_on_close: Exception | None = None
        self.publish_ok = True
        self._connected = bool(connect)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        # The vendor exposes this too — the double mirrors its surface exactly,
        # so the "raw class fails our probe" test names only the REAL gaps.
        self._connected = True

    def publish(self, topic, payload, *, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return PublishResult(ok=self.publish_ok, connected=self._connected, reason="ok")

    def close(self) -> None:
        self.closed += 1
        self._connected = False
        if self.raise_on_close is not None:
            raise self.raise_on_close


class _Factory(list):
    """A vendor factory that records what it built, scoped to ONE test.

    Replaces a class-level ``VendorDouble.instances`` registry: that was shared
    mutable state needing an autouse fixture to reset it between tests, and one
    forgotten reset would have coupled tests invisibly. A list per test cannot.
    """

    def __call__(self, host, port, **kwargs):
        made = VendorDouble(host, port, **kwargs)
        self.append(made)
        return made

    # `_build_will` resolves the vendor's `Will` from the factory's module, so
    # the callable must report this module rather than `list`'s.
    __module__ = __name__


def _adapter(url: str = "localhost:1883") -> tuple[EC.EventsCliClient, _Factory]:
    """The adapter under test, plus the doubles its factory builds."""
    factory = _Factory()
    return EC.EventsCliClient(url, factory=factory), factory


# --------------------------------------------------------------------------- #
# 1. The probe that guards the whole leg                                      #
# --------------------------------------------------------------------------- #


def test_the_adapter_satisfies_our_own_required_client_surface():
    """The test that would have caught the vendor mismatch.

    ``missing_client_members`` is what the publisher runs at ``start()``; a
    non-empty result disables the bus with ``reason=client-incompatible``.
    Handing it the RAW vendor class fails that probe — handing it the adapter
    must not.
    """
    adapter, _built = _adapter()
    assert M.missing_client_members(adapter) == ()


def test_the_raw_vendor_class_does_not_satisfy_it_which_is_why_the_adapter_exists():
    """Pins the reason this module exists, so deleting it fails loudly."""
    raw = VendorDouble("localhost", 1883)
    missing = M.missing_client_members(raw)
    assert set(missing) == {"connected", "will_set", "disconnect"}


# --------------------------------------------------------------------------- #
# 2. URL parsing                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("localhost:1883", ("localhost", 1883)),
        ("10.0.0.9:1884", ("10.0.0.9", 1884)),
        ("mqtt://broker.local:1883", ("broker.local", 1883)),
        ("tcp://127.0.0.1:1885/", ("127.0.0.1", 1885)),
        ("broker.local", ("broker.local", 1883)),
        ("broker.local:not-a-port", ("broker.local", 1883)),
        ("broker.local:0", ("broker.local", 1883)),
        ("broker.local:99999", ("broker.local", 1883)),
    ],
)
def test_broker_urls_parse_into_host_and_port(url, expected):
    assert EC.parse_broker_url(url) == expected


def test_a_malformed_url_never_raises_at_composition_time():
    """A typo in REACHY_MQTT_URL degrades the bus; it must not stop the robot."""
    assert EC.parse_broker_url("") == ("localhost", EC.DEFAULT_PORT)


# --------------------------------------------------------------------------- #
# 3. The two-step protocol becomes a one-step constructor                     #
# --------------------------------------------------------------------------- #


def test_nothing_is_constructed_until_connect():
    """Composition must not touch the network — the client is built by connect()."""
    adapter, built = _adapter()
    assert built == []
    assert adapter.connected is False


def test_will_set_before_connect_reaches_the_vendor_constructor():
    """The whole trick: our two-step protocol maps onto their constructor arg."""
    adapter, built = _adapter()
    adapter.will_set("reachy/state/online", "false", qos=0, retain=True)
    adapter.connect()
    vendor = built[-1]
    assert vendor.will == Will(topic="reachy/state/online", payload="false", qos=0, retain=True)
    assert (vendor.host, vendor.port) == ("localhost", 1883)


def test_connect_is_idempotent():
    adapter, built = _adapter()
    adapter.connect()
    adapter.connect()
    assert len(built) == 1


def test_will_set_after_connect_is_refused_not_silently_dropped(caplog):
    """It cannot take effect — the broker learns the will as the session opens."""
    adapter, built = _adapter()
    adapter.connect()
    with caplog.at_level("WARNING"):
        adapter.will_set("reachy/state/online", "false")
    assert "will_set after connect" in caplog.text


def test_connect_without_the_vendor_raises_into_the_publishers_named_drop(monkeypatch):
    """``start()`` wraps this into ``connect-failed`` — never into the tick."""
    # Force the "vendor absent" branch without touching sys.modules.
    monkeypatch.setattr(EC, "VENDOR_IMPORT", ("reachy_no_such_events_pkg", "EventClient"))
    adapter = EC.EventsCliClient("localhost:1883", factory=None)
    with pytest.raises(RuntimeError):
        adapter.connect()


# --------------------------------------------------------------------------- #
# 4. Liveness, publishing, shutdown                                           #
# --------------------------------------------------------------------------- #


def test_connected_maps_onto_the_vendors_is_connected():
    adapter, built = _adapter()
    adapter.connect()
    assert adapter.connected is True
    built[-1]._connected = False
    assert adapter.connected is False


def test_publish_delegates_with_qos_and_retain_intact():
    adapter, built = _adapter()
    adapter.connect()
    adapter.publish("reachy/state/pose", '{"x":1}', qos=0, retain=True)
    assert built[-1].published == [("reachy/state/pose", '{"x":1}', 0, True)]


def test_publish_before_connect_is_a_no_op_not_a_crash():
    _adapter()[0].publish("reachy/events/sense/snapshot", "{}")  # must not raise


def test_a_not_ok_publish_result_never_raises_into_the_caller():
    """QoS 0 under a dropped session: the vendor reports, we do not escalate."""
    adapter, built = _adapter()
    adapter.connect()
    built[-1].publish_ok = False
    adapter.publish("reachy/events/sense/snapshot", "{}")  # must not raise


def test_disconnect_closes_the_vendor_session():
    adapter, built = _adapter()
    adapter.connect()
    adapter.disconnect()
    assert built[-1].closed == 1
    assert adapter.connected is False


def test_a_raising_close_is_swallowed_at_shutdown():
    adapter, built = _adapter()
    adapter.connect()
    built[-1].raise_on_close = RuntimeError("socket already gone")
    adapter.disconnect()  # must not raise


def test_disconnect_without_connect_is_a_no_op():
    _adapter()[0].disconnect()  # must not raise


# --------------------------------------------------------------------------- #
# 5. Against the REAL vendor, with no broker running                          #
# --------------------------------------------------------------------------- #

real_events_cli = pytest.importorskip("events_cli")


def test_the_real_vendor_class_is_where_we_say_it_is():
    module_name, attr = EC.VENDOR_IMPORT
    assert module_name == "events_cli"
    assert getattr(real_events_cli, attr, None) is not None


def test_the_real_client_satisfies_the_declared_surface_through_the_adapter():
    """The binding, end to end, on a port with nothing listening.

    Port 1 is reserved and never a broker, so this exercises the real paho
    machinery in its broker-down state without depending on a live broker —
    the suite stays hermetic and safe under ``pytest -n auto``.
    """
    adapter = EC.EventsCliClient("127.0.0.1:1")
    assert M.missing_client_members(adapter) == ()
    adapter.will_set("reachy/state/online", "false", qos=0, retain=True)
    adapter.connect()
    try:
        # Never connected, so publishing is a documented no-op — and crucially
        # it does not raise, which is the contract the 50 Hz tick depends on.
        adapter.publish("reachy/events/sense/snapshot", "{}")
        assert adapter.connected is False
    finally:
        adapter.disconnect()


def test_a_publisher_on_a_dead_broker_degrades_to_one_named_drop(caplog):
    """The whole leg, real client, no broker: named degradation, no exception."""
    adapter = EC.EventsCliClient("127.0.0.1:1")
    publisher = M.NervousPublisher(adapter)
    with caplog.at_level("INFO"):
        assert publisher.start() is False
    publisher.emit(object())
    publisher.publish_state({"pose": {"x": 1}})
    publisher.stop()
    assert M.REASON_BROKER_UNREACHABLE in caplog.text
    assert M.REASON_CLIENT_INCOMPATIBLE not in caplog.text


# --------------------------------------------------------------------------- #
# The adapter must not ADVERTISE a capability the vendor does not have         #
# (PR #172 review)                                                             #
# --------------------------------------------------------------------------- #


class _PublishOnlyVendor:
    """The vendor as it actually ships today: publish, connect, close. No more."""

    def __init__(self, host, port, *, connect=False, **kwargs) -> None:
        self.host, self.port = host, port
        self.is_connected = False
        self.published: list[tuple] = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return type("R", (), {"ok": True})()

    def close(self) -> None:
        self.is_connected = False


class _SubscribingVendor(_PublishOnlyVendor):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.subscriptions: list[tuple] = []
        self.on_message = None

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))


def test_the_installed_vendor_client_still_has_no_subscribe_capability():
    """The live canary, restated where the adapter can act on it.

    ``events_cli.EventClient`` ships publish/connect/close only. The day
    upstream adds ``subscribe`` this test starts failing — and the adapter
    below should then start reporting the capability as PRESENT.
    """
    vendor = pytest.importorskip("events_cli")
    assert not hasattr(vendor.EventClient, "subscribe")


def test_an_adapter_over_a_publish_only_vendor_reports_no_subscribe_support():
    adapter = EC.EventsCliClient("127.0.0.1:1", factory=_PublishOnlyVendor)
    adapter.connect()
    try:
        assert adapter.supports_subscribe() is False
    finally:
        adapter.disconnect()


def test_an_adapter_over_a_subscribing_vendor_reports_support():
    adapter = EC.EventsCliClient("127.0.0.1:1", factory=_SubscribingVendor)
    adapter.connect()
    try:
        assert adapter.supports_subscribe() is True
    finally:
        adapter.disconnect()


def test_capability_is_read_from_the_class_before_the_client_is_built():
    """`MindPresence.start()` must get a truthful answer whenever it asks."""
    assert EC.EventsCliClient("h:1", factory=_PublishOnlyVendor).supports_subscribe() is False
    assert EC.EventsCliClient("h:1", factory=_SubscribingVendor).supports_subscribe() is True


def test_mind_presence_over_a_publish_only_vendor_names_client_incompatible(caplog):
    """The whole point: no inbound path must never look like a live subscription.

    The adapter always DEFINES `subscribe`/`set_on_message`, so a probe of the
    adapter's own attributes passes while the vendor underneath has nowhere to
    put the subscription. Presence then reported success, logged "watching
    ... for the mind", and read UNKNOWN forever with no drop naming why — which
    is exactly the invisible degradation the drop vocabulary exists to prevent.
    """
    from reachy.export import mind_presence as MP

    adapter = EC.EventsCliClient("127.0.0.1:1", factory=_PublishOnlyVendor)
    adapter.connect()
    presence = MP.MindPresence(adapter)

    with caplog.at_level("INFO"):
        started = presence.start()

    try:
        assert started is False
        assert presence.subscribed is False
        assert presence.online() is None
        assert MP.REASON_CLIENT_INCOMPATIBLE in caplog.text
    finally:
        adapter.disconnect()


def test_mind_presence_over_a_subscribing_vendor_still_subscribes():
    from reachy.export import mind_presence as MP

    adapter = EC.EventsCliClient("127.0.0.1:1", factory=_SubscribingVendor)
    adapter.connect()
    presence = MP.MindPresence(adapter)
    try:
        assert presence.start() is True
        assert adapter._client.subscriptions == [(MP.MIND_STATE_TOPIC, M.QOS)]
    finally:
        adapter.disconnect()
