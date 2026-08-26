"""Is the MIND up? The runtime's first bus SUBSCRIBER.

Everything the runtime has done on the event bus until now has been outbound:
:class:`~reachy.export.mqtt.NervousPublisher` publishes senses, decisions and
retained state and listens to nothing. That asymmetry is why
:class:`~reachy.behavior.face_lock.FaceLockDriver` shipped with
``mind_online=None`` — a documented seam with no live reading behind it, so
"a lock cannot outlive its mind" was bounded only by ``max_hold_s`` (30
minutes). This module is the reading.

It is deliberately tiny: ONE retained topic, ONE tri-state answer.

The topic
---------
:data:`MIND_STATE_TOPIC` is ``nova/harness/state`` — the harness's OWN retained
availability topic, in Nova's namespace rather than the runtime's, because the
two trees never mix. It is declared and published by ``reachy_nova``'s
``reachy_nova/harness/bus.py`` (``HARNESS_STATE_TOPIC``, line 144), published
retained on every successful connect (line 615) and registered as that client's
Last Will (line 552) so an ungraceful death flips it without the harness's
cooperation — which is exactly the case a face lock has to survive.

The payload is that module's ``harness_state_payload``:
``{"status": "online" | "offline", "ts": <float>}``. A bare JSON boolean is
understood too, so the reading survives the topic being moved to the runtime's
own ``reachy/state/nova/online`` shape (which is how ``reachy_nova``'s
``docs/architecture.md`` §4 describes the mind namespace) without a code change
on this side.

The tri-state
-------------
:meth:`MindPresence.online` answers ``True`` / ``False`` / ``None``, and the
``None`` is the load-bearing one: it means *we have no idea*, which is what a
broker that is not up, a harness that has never announced itself, or a runtime
composed with no client at all all honestly amount to. ``FaceLockDriver``
treats unknown as "do not release" — a lock must never drop off a face because
the BUS was sick.

Degradation
-----------
Same discipline as the publisher: an absent client, a client without the
optional subscription members, a ``subscribe`` that raises, and a payload that
does not parse each resolve to ONE named
``[SENSE stage=nervous source=mind ...] dropped reason=<reason>`` line and an
unknown reading — never an exception, and never a silent no-op. The message
callback runs on the CLIENT's thread and the reading is a plain attribute
assignment (atomic under CPython, one writer), so the tick thread's
:meth:`online` read takes no lock and can never block.
"""

from __future__ import annotations

import json

from reachy import senselog
from reachy.export.mqtt import QOS

#: The senselog ``stage`` every line from this module carries — the publisher's
#: stage, because this is the same nervous system read backwards.
STAGE = "nervous"
#: The senselog ``source`` every line from this module carries.
SOURCE = "mind"

#: The retained topic the harness actually publishes its availability on.
#: See the module docstring for the provenance (file + lines).
MIND_STATE_TOPIC = "nova/harness/state"

#: The two ``status`` values the harness's payload carries.
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"

#: Client members this subscriber needs BEYOND the publisher's required set.
#: They are OPTIONAL on :class:`~reachy.export.mqtt.EventClient` on purpose: a
#: client that can only publish is a perfectly good nervous system, it simply
#: cannot answer this question.
REQUIRED_SUBSCRIBE_MEMBERS: tuple[str, ...] = ("subscribe", "set_on_message")

#: Named drop reasons — greppable, verbatim.
REASON_NO_CLIENT = "no-client"
REASON_CLIENT_INCOMPATIBLE = "client-incompatible"
REASON_SUBSCRIBE_FAILED = "subscribe-failed"
REASON_BAD_PAYLOAD = "bad-payload"


def _missing_members(client: object) -> tuple[str, ...]:
    """Names from :data:`REQUIRED_SUBSCRIBE_MEMBERS` *client* does not expose."""
    missing = []
    for name in REQUIRED_SUBSCRIBE_MEMBERS:
        try:
            member = getattr(client, name, None)
        except Exception:  # a sick accessor is a missing member
            member = None
        if not callable(member):
            missing.append(name)
    return tuple(missing)


def parse_presence(payload: object) -> bool | None:
    """Read one retained payload as ``True`` / ``False`` / ``None`` (unreadable).

    Accepts the harness's ``{"status": "online"|"offline", ...}`` object, a bare
    JSON boolean (``reachy/state/online``'s shape), and the two bare status
    words. Anything else is ``None``, which the caller reports as one named drop
    and which never overwrites a reading we already trust.
    """
    if isinstance(payload, (bytes, bytearray, memoryview)):
        try:
            payload = bytes(payload).decode("utf-8")
        except Exception:
            return None
    if isinstance(payload, bool):
        return payload
    if not isinstance(payload, str):
        return None
    text = payload.strip()
    if not text:
        return None
    try:
        body = json.loads(text)
    except Exception:
        body = text
    if isinstance(body, bool):
        return body
    if isinstance(body, dict):
        body = body.get("status")
    if isinstance(body, str):
        status = body.strip().lower()
        if status == STATUS_ONLINE:
            return True
        if status == STATUS_OFFLINE:
            return False
    return None


class MindPresence:
    """Track the mind's retained availability through an injected bus client.

    Args:
        client: an object satisfying :class:`~reachy.export.mqtt.EventClient`
            AND its optional :data:`REQUIRED_SUBSCRIBE_MEMBERS`, or ``None``.
            ``None`` is the normal no-broker profile — one named drop, and a
            permanently unknown reading. May also be supplied later through
            :meth:`attach`, which is how composition wires it: the client
            belongs to the publisher, and the publisher is built AFTER the face
            lock that consumes this reading.
        topic: the retained topic to read. Defaults to
            :data:`MIND_STATE_TOPIC`; injectable so a deployment that moves the
            mind's namespace needs no code change.

    Every method is total: it returns normally for any input and any client
    state. Nothing here may raise into the tick thread OR into the client's
    network thread.
    """

    def __init__(self, client: object | None = None, *, topic: str = MIND_STATE_TOPIC) -> None:
        self._client = client
        self._topic = str(topic)
        self._online: bool | None = None
        self._started = False
        self._stopped = False
        self._disabled = False
        self._reported: set[str] = set()
        #: Observability counters (also what the tests read).
        self.messages = 0

    # -- introspection -------------------------------------------------------

    @property
    def topic(self) -> str:
        """The retained topic this presence reads."""
        return self._topic

    @property
    def subscribed(self) -> bool:
        """Whether a live subscription is actually in place."""
        return self._started and not self._disabled and not self._stopped

    def online(self) -> bool | None:
        """The mind's last known availability — ``None`` means UNKNOWN.

        The exact shape :class:`~reachy.behavior.face_lock.FaceLockDriver`'s
        ``mind_online`` seam expects, and safe to call from the tick thread: a
        plain attribute read, no lock, no I/O.
        """
        return self._online

    # -- lifecycle -----------------------------------------------------------

    def attach(self, client: object | None) -> None:
        """Supply the client before :meth:`start`. A no-op afterwards.

        Composition's deferred wiring: the face lock needs ``presence.online``
        as a bound callable long before the publisher (which owns the client)
        exists. Until a client is attached and started the reading is ``None``,
        and unknown never releases a lock — so the window is safe by
        construction, not by timing.
        """
        if self._started:
            return
        self._client = client

    def start(self) -> bool:
        """Subscribe the topic and register the message callback. Idempotent.

        Returns whether a subscription is in place. Every failure path is one
        named drop and a permanently-unknown reading.
        """
        if self._started:
            return self.subscribed
        self._started = True

        if self._client is None:
            self._disable(REASON_NO_CLIENT, "(nothing injected; the mind's state is unknown)")
            return False

        missing = _missing_members(self._client)
        if missing:
            self._disable(REASON_CLIENT_INCOMPATIBLE, f"missing={','.join(missing)}")
            return False

        try:
            self._client.set_on_message(self.on_message)  # type: ignore[attr-defined]
            self._client.subscribe(self._topic, qos=QOS)  # type: ignore[attr-defined]
        except Exception as err:
            self._disable(
                REASON_SUBSCRIBE_FAILED, f"topic={self._topic} ({type(err).__name__}: {err})"
            )
            return False

        senselog.stage(STAGE, SOURCE, "subscribe", f"watching {self._topic} for the mind")
        return True

    def stop(self) -> None:
        """Stop believing new payloads. Idempotent, total, never unsubscribes.

        The client is the PUBLISHER's, and it is torn down by the publisher's
        own ``stop()``; reaching into it here would be a second owner. What
        this does own is the reading, which is frozen where it stood — a
        shutting-down runtime must not have a lock lifecycle decision made for
        it by a late retained replay.
        """
        self._stopped = True

    # -- the client's thread -------------------------------------------------

    def on_message(self, *args: object) -> None:
        """Handle one broker message. Runs on the CLIENT's thread; never raises.

        Tolerant about its call shape on purpose — the vendor client is not in
        this repo (see :mod:`reachy.export.mqtt`'s docstring) and the two
        plausible conventions are ``(topic, payload)`` and one message object
        carrying ``.topic`` / ``.payload``. Both are accepted; anything else is
        ignored rather than allowed to become an exception inside somebody
        else's network loop.
        """
        try:
            topic, payload = self._unpack(args)
            if topic != self._topic or self._stopped:
                return
            self.messages += 1
            reading = parse_presence(payload)
            if reading is None:
                self._drop(REASON_BAD_PAYLOAD, f"topic={topic} payload={payload!r:.80}")
                return
            if reading != self._online:
                senselog.stage(
                    STAGE,
                    SOURCE,
                    "presence",
                    f"mind is {STATUS_ONLINE if reading else STATUS_OFFLINE} "
                    f"(retained {self._topic})",
                )
            self._online = reading
        except Exception:  # a callback that raises would poison the client loop
            self._drop(REASON_BAD_PAYLOAD, "callback raised")

    @staticmethod
    def _unpack(args: tuple) -> tuple[object, object]:
        """``(topic, payload)`` from either supported callback shape."""
        if len(args) >= 2:
            return args[0], args[1]
        if len(args) == 1:
            message = args[0]
            return getattr(message, "topic", None), getattr(message, "payload", None)
        return None, None

    # -- degradation ---------------------------------------------------------

    def _disable(self, reason: str, extra: str = "") -> None:
        self._disabled = True
        self._drop(reason, extra)

    def _drop(self, reason: str, extra: str = "") -> None:
        """One line per DISTINCT reason — a retained topic can replay a flood."""
        if reason in self._reported:
            return
        self._reported.add(reason)
        senselog.drop(STAGE, SOURCE, "presence", f"{reason} {extra}".strip())


__all__ = [
    "MIND_STATE_TOPIC",
    "REASON_BAD_PAYLOAD",
    "REASON_CLIENT_INCOMPATIBLE",
    "REASON_NO_CLIENT",
    "REASON_SUBSCRIBE_FAILED",
    "REQUIRED_SUBSCRIBE_MEMBERS",
    "SOURCE",
    "STAGE",
    "MindPresence",
    "parse_presence",
]
