"""The ONE place that knows what ``events-cli``'s client actually looks like.

:mod:`reachy.export.mqtt` declares the narrow surface this repo *requires* of a
bus client (:class:`~reachy.export.mqtt.EventClient`) and publishes through it,
naming nothing from any vendor. This module is the other half of that split: it
binds that declared requirement to the real ``events_cli.EventClient`` shipped
by the sibling project, and it is the only module in the repo that imports it.

Why an adapter rather than a one-line binding
---------------------------------------------
The plan assumed the vendor would land on our declared names. It didn't, and
the differences are real rather than cosmetic:

===========================  ==================================================
we require                   ``events_cli.EventClient`` ships
===========================  ==================================================
``connected``                ``is_connected``
``disconnect()``             ``close()``
``will_set(...)`` before     the Last Will is a **constructor** argument
``connect()``                (``will=``/``availability_topic=``); paho requires
                             it registered before the session opens, so there
                             is no post-construction setter to call
===========================  ==================================================

Both shapes are defensible — theirs makes the will-before-connect ordering
structurally unskippable, which is strictly safer. Adapting is therefore the
honest move: our publisher keeps the surface its tests and
``docs/export-schema.md`` describe, the vendor keeps the API it designed, and
the coupling lives in one small file that moves as a unit if either side
changes again. Without this, our own fail-closed probe
(:func:`~reachy.export.mqtt.missing_client_members`) would correctly report
``dropped reason=client-incompatible`` and disable the bus — a named, safe
no-op, but a silent one to anybody expecting the bus to work.

The deferred construction
-------------------------
:meth:`EventsCliClient.connect` is where the vendor client is *built*, not just
started, because that is the only point at which the Last Will is known. This
is the adapter's whole trick: it turns our two-step
``will_set()`` → ``connect()`` protocol into the vendor's one-step
constructor, and it means a vendor that cannot be constructed at all (paho
missing, a nonsensical host) raises inside ``connect()``, which
:meth:`~reachy.export.mqtt.NervousPublisher.start` already wraps into the named
``connect-failed`` drop. Nothing new can reach the tick thread.

Publishing stays O(1) on the caller's thread: the vendor documents
``publish()`` as a pure enqueue onto its own background loop thread and returns
a ``PublishResult`` instead of raising, which is exactly the contract
``reachy/export/mqtt.py`` requires of whatever it is handed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The vendor module + class this repo binds to. Kept here, beside the code
#: that knows the shape, rather than at the composition site.
VENDOR_IMPORT: tuple[str, str] = ("events_cli", "EventClient")

#: Fallback when a broker URL names a host but no port.
DEFAULT_PORT = 1883


def parse_broker_url(url: str, *, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """Split a broker URL into ``(host, port)`` for the vendor constructor.

    :func:`~reachy.export.mqtt.broker_url` yields a bare ``host:port`` pair
    (``localhost:1883``), but an operator setting ``REACHY_MQTT_URL`` by hand
    may well write a scheme or omit the port, so all three forms are accepted:
    ``mqtt://host:1883``, ``host:1883`` and ``host``. A non-numeric or
    out-of-range port falls back to *default_port* rather than raising — this
    runs at composition time, where a typo must degrade the bus, never stop the
    robot from booting.
    """
    text = str(url).strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.strip("/")
    host, _, port_text = text.rpartition(":")
    if not host:  # no colon at all — the whole string is the host
        return (text or "localhost"), default_port
    try:
        port = int(port_text)
    except ValueError:
        logger.warning("events: unparseable port in %r; using %d", url, default_port)
        return host, default_port
    if not 0 < port < 65536:
        logger.warning("events: out-of-range port in %r; using %d", url, default_port)
        return host, default_port
    return host, port


class EventsCliClient:
    """Present ``events_cli.EventClient`` as the surface ``mqtt.py`` requires.

    Constructed with just the broker URL, so it drops straight into the
    composition site's existing ``factory(url)`` seam. The vendor client is
    built later, by :meth:`connect`, once the Last Will is known.

    Args:
        url: ``host:port`` (or ``mqtt://host:port``, or a bare host).
        factory: the vendor class, injectable for tests. Defaults to resolving
            :data:`VENDOR_IMPORT` lazily — importing this module never imports
            the vendor, so a box without events-cli installed is unaffected.
    """

    def __init__(self, url: str, *, factory: Callable[..., Any] | None = None) -> None:
        self._host, self._port = parse_broker_url(url)
        self._factory = factory
        self._client: Any | None = None
        self._will: tuple[str, str, int, bool] | None = None
        #: A message callback registered before :meth:`connect` built the vendor
        #: client, applied by that call. Ordering must not decide whether the
        #: mind's retained state is ever heard.
        self._pending_on_message: Callable[..., None] | None = None

    # -- the surface reachy.export.mqtt.EventClient declares -----------------

    @property
    def connected(self) -> bool:
        """Cheap, non-blocking liveness read; ``False`` before :meth:`connect`."""
        client = self._client
        if client is None:
            return False
        try:
            return bool(client.is_connected)
        except Exception:  # a state read must never raise
            return False

    def will_set(self, topic: str, payload: str, *, qos: int = 0, retain: bool = True) -> None:
        """Record the Last Will. Applied by :meth:`connect`, which builds the client.

        Calling this after :meth:`connect` cannot take effect — the vendor
        registers the will with the broker as the session opens — so it is
        logged rather than silently accepted.
        """
        if self._client is not None:
            logger.warning("events: will_set after connect is ignored (topic=%s)", topic)
            return
        self._will = (topic, payload, qos, retain)

    def connect(self) -> None:
        """Build the vendor client with the recorded will and start its loop.

        Raises only what the vendor raises at construction (a missing paho, a
        nonsensical host/port). :meth:`~reachy.export.mqtt.NervousPublisher.start`
        turns that into the named ``connect-failed`` drop, so the runtime is
        never harmed by a broker that is not there.
        """
        if self._client is not None:
            return
        factory = self._factory if self._factory is not None else _resolve_vendor()
        if factory is None:
            raise RuntimeError(f"{VENDOR_IMPORT[0]}.{VENDOR_IMPORT[1]} is not importable")
        kwargs: dict[str, Any] = {}
        if self._will is not None:
            topic, payload, qos, retain = self._will
            kwargs["will"] = _build_will(factory, topic, payload, qos, retain)
        # connect=True: the vendor's constructor runs connect_async + loop_start,
        # neither of which blocks on the network, so this stays safe to call from
        # composition without a timeout.
        self._client = factory(self._host, self._port, connect=True, **kwargs)
        if self._pending_on_message is not None:
            callback, self._pending_on_message = self._pending_on_message, None
            self.set_on_message(callback)

    def disconnect(self) -> None:
        """Close the vendor session. Total: a failure is logged, never raised."""
        client = self._client
        if client is None:
            return
        self._client = None
        try:
            client.close()
        except Exception as err:  # shutdown must never raise
            logger.warning(
                "events: closing the bus client failed (%s: %s)", type(err).__name__, err
            )

    def publish(self, topic: str, payload: str, *, qos: int = 0, retain: bool = False) -> None:
        """Enqueue one message. O(1) on this thread, by the vendor's contract.

        The vendor returns a ``PublishResult`` rather than raising; a
        not-``ok`` result is a dropped QoS-0 message, which the publisher's own
        connection gate has usually already accounted for. It is logged at debug
        level only — this runs at tick rate, so it must never become a log
        stream of its own.
        """
        client = self._client
        if client is None:
            return
        result = client.publish(topic, payload, qos=qos, retain=retain)
        ok = getattr(result, "ok", True)
        if not ok:
            logger.debug(
                "events: publish to %s not accepted (%s)", topic, getattr(result, "reason", "?")
            )

    # -- the OPTIONAL subscribe half (t17) -----------------------------------

    def subscribe(self, topic: str, *, qos: int = 0) -> None:
        """Subscribe *topic* on the vendor client. Total: never raises here.

        The vendor's own subscribe signature is not pinned by this repo (the
        wheel is the sibling project's), so both the keyword and the positional
        QoS shapes are tried before giving up. A vendor with no subscribe at all
        is not a fault — it is a publish-only client, and
        :class:`reachy.export.mind_presence.MindPresence` reports the gap as one
        named drop and reads UNKNOWN forever, which never releases a face lock.
        """
        client = self._client
        if client is None:
            return
        subscribe = getattr(client, "subscribe", None)
        if not callable(subscribe):
            logger.debug("events: the bus client exposes no subscribe(); %s unread", topic)
            return
        try:
            subscribe(topic, qos=qos)
        except TypeError:
            subscribe(topic)

    def set_on_message(self, callback: Callable[..., None]) -> None:
        """Register *callback* for inbound messages, however the vendor spells it.

        ``set_on_message(cb)`` if the vendor offers it, else a plain
        ``on_message`` attribute (paho's convention, which the vendor wraps).
        Neither present means inbound messages are simply never delivered — see
        :meth:`subscribe` on why that is a degradation, not a failure.
        """
        client = self._client
        if client is None:
            self._pending_on_message = callback
            return
        setter = getattr(client, "set_on_message", None)
        if callable(setter):
            setter(callback)
            return
        try:
            client.on_message = callback
        except Exception as err:  # a read-only attribute, a slotted class, ...
            logger.debug(
                "events: cannot register a message callback (%s: %s)", type(err).__name__, err
            )


def _build_will(factory: Any, topic: str, payload: str, qos: int, retain: bool) -> Any:
    """Build the vendor's ``Will`` value object for *factory*'s package.

    Resolved from the factory's own module so an injected test double can ship
    its own ``Will`` without this module reaching for the real one.
    """
    module = _module_of(factory)
    will_type = getattr(module, "Will", None)
    if will_type is None:
        raise RuntimeError("the events client package exposes no 'Will' type")
    return will_type(topic=topic, payload=payload, qos=qos, retain=retain)


def _module_of(factory: Any) -> Any:
    import importlib

    return importlib.import_module(factory.__module__)


def _resolve_vendor() -> Any | None:
    """Import the vendor class lazily; ``None`` when the package is absent."""
    import importlib

    module_name, attr = VENDOR_IMPORT
    try:
        module = importlib.import_module(module_name)
    except Exception as err:  # an absent optional package is normal
        logger.debug("events: %s unavailable (%s: %s)", module_name, type(err).__name__, err)
        return None
    return getattr(module, attr, None)


def client_factory() -> Callable[[str], EventsCliClient] | None:
    """A ``factory(url)`` the composition site can call, or ``None``.

    ``None`` means the vendor package is not installed — the normal no-broker
    profile, which :class:`~reachy.export.mqtt.NervousPublisher` reports as one
    named ``no-client`` drop.
    """
    if _resolve_vendor() is None:
        return None
    return EventsCliClient
