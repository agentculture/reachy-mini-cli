"""An in-process fake of a bus-subscribe client, for the embody cue-intake seam.

Mirrors ``tests/fake_events_client.py``'s role for the nervous-system PUBLISH
leg, but for the SUBSCRIBE direction :mod:`reachy.embody.cues` declares
(:class:`reachy.embody.cues.BusSubscriber`). No such capability exists in the
installed ``events-cli`` client today (see ``reachy/embody/cues.py``'s module
docstring for the verified gap) — this fake is what proves the intake logic
that WOULD run once a real subscribe-capable client exists, without any
network, thread, or vendor dependency.

``push()`` calls the registered callback synchronously, so a test can drive
messages without a background thread: the fake models exactly the surface
:class:`~reachy.embody.cues.BusSubscriber` requires and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable


class FakeBusSubscriber:
    """The whole required surface, and nothing else.

    Args:
        autoconnect: whether :meth:`connect` immediately reports a live
            session; ``False`` models a client whose ``connect()`` succeeds
            (no exception) but never actually gets a session.
        raise_on_connect: an exception :meth:`connect` should raise, or
            ``None``.
        raise_on_subscribe: an exception :meth:`subscribe` should raise, or
            ``None``.
    """

    def __init__(
        self,
        *,
        autoconnect: bool = True,
        raise_on_connect: Exception | None = None,
        raise_on_subscribe: Exception | None = None,
    ) -> None:
        self._connected = False
        self._autoconnect = autoconnect
        self.raise_on_connect = raise_on_connect
        self.raise_on_subscribe = raise_on_subscribe
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.subscribed_topic_filters: list[str] = []
        self._callback: Callable[[str, str], None] | None = None

    # -- the required surface ------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self.connect_calls += 1
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        if self._autoconnect:
            self._connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def subscribe(self, topic_filter: str, on_message: Callable[[str, str], None]) -> None:
        if self.raise_on_subscribe is not None:
            raise self.raise_on_subscribe
        self.subscribed_topic_filters.append(topic_filter)
        self._callback = on_message

    # -- test controls ---------------------------------------------------

    def push(self, topic: str, payload: str) -> None:
        """Simulate one incoming bus message, delivered synchronously."""
        assert self._callback is not None, "subscribe() was never called"
        self._callback(topic, payload)
