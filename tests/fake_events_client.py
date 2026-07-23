"""An in-process fake of the ``events-cli`` client, for the nervous-system seam.

The broker and its client belong to the sibling ``events-cli`` project
(``agentculture/events-cli#3``); this repo holds only the narrow
:class:`reachy.export.mqtt.EventClient` protocol and publishes through it. Until
that wheel ships — and afterwards, so the suite stays hermetic — every test of
:mod:`reachy.export.mqtt` runs against this fake.

It is deliberately socket-free, thread-free and dependency-free: ``publish`` is
an O(1) append, which is exactly the contract the real client owes the tick
thread (network I/O on its own background machinery, never the caller's).
Mirrors ``tests/fake_realtime_server.py``'s role for the hearing leg.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Published:
    """One message the fake accepted, with its full per-publish policy."""

    topic: str
    payload: str
    qos: int
    retain: bool


class FakeEventsClient:
    """The whole required surface, and nothing else.

    Args:
        autoconnect: whether :meth:`connect` immediately reports a live session
            (the usual case); ``False`` models a client that connects later on
            its own background machinery, so a test can drive
            :meth:`go_online` explicitly.
    """

    def __init__(self, *, autoconnect: bool = True) -> None:
        self.published: list[Published] = []
        self.will: Published | None = None
        self.calls: list[str] = []
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.raise_on_connect: Exception | None = None
        self.raise_on_publish: Exception | None = None
        self._connected = False
        self._autoconnect = autoconnect
        self._on_connect = None

    # -- the required surface ------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def will_set(self, topic: str, payload: str, *, qos: int = 0, retain: bool = True) -> None:
        self.calls.append("will_set")
        self.will = Published(topic, payload, qos, retain)

    def connect(self) -> None:
        self.calls.append("connect")
        self.connect_calls += 1
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        if self._autoconnect:
            self.go_online()

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.disconnect_calls += 1
        self._connected = False

    def publish(self, topic: str, payload: str, *, qos: int = 0, retain: bool = False) -> None:
        self.calls.append("publish")
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        assert isinstance(payload, str), "the seam must hand the client TEXT, never bytes"
        self.published.append(Published(topic, payload, qos, retain))

    # -- the optional surface ------------------------------------------------

    def set_on_connect(self, callback) -> None:
        self.calls.append("set_on_connect")
        self._on_connect = callback

    # -- test controls -------------------------------------------------------

    def go_online(self) -> None:
        """Report a live session, firing the registered connect callback."""
        self._connected = True
        if self._on_connect is not None:
            self._on_connect()

    def go_offline(self) -> None:
        """Report a dead session without any notification (the polled path)."""
        self._connected = False

    def topics(self) -> list[str]:
        return [p.topic for p in self.published]

    def by_topic(self, topic: str) -> list[Published]:
        return [p for p in self.published if p.topic == topic]


class BlockingTrapClient(FakeEventsClient):
    """A fake whose blocking-shaped methods explode if the seam ever calls them.

    The publisher runs on the 50 Hz tick thread; a ``flush``/``loop``/
    ``wait_for_publish`` call there would turn every publish into a latency
    hazard. Nothing in the seam may reach for one, so reaching for one fails
    loudly.
    """

    def flush(self, *_a, **_kw):  # pragma: no cover - must never be called
        raise AssertionError("the seam called a blocking client method: flush()")

    def loop(self, *_a, **_kw):  # pragma: no cover - must never be called
        raise AssertionError("the seam called a blocking client method: loop()")

    def wait_for_publish(self, *_a, **_kw):  # pragma: no cover - must never be called
        raise AssertionError("the seam called a blocking client method: wait_for_publish()")

    def loop_forever(self, *_a, **_kw):  # pragma: no cover - must never be called
        raise AssertionError("the seam called a blocking client method: loop_forever()")
