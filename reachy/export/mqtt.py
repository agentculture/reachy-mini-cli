"""The nervous-system publisher: the runtime feed on an event bus.

This is the leg that lets EXTERNAL services — first the reTerminal panel, later
tau and daria — subscribe to Reachy's senses without touching the SDK. The
runtime already owns every sense inside ONE process (the single-SDK-owner
model); this module fans that one owner's events out, so a consumer never
contends for the media session and a dead consumer can never backpressure the
50 Hz tick.

The client is somebody else's
-----------------------------
The broker and its client belong to the sibling **events-cli** project
(``agentculture/events-cli#3``). This repo deliberately ships **no MQTT library,
no wire code and no new dependency**: it declares the narrow client surface it
needs (:class:`EventClient`), publishes through an INJECTED instance, and a
later composition step binds the real import in one line once that wheel exists.
Everything here is exercised against a fake (``tests/fake_events_client.py``).

The contract required of events-cli — hold this shape:

- ``publish(topic, payload, *, qos, retain)`` is an **O(1) enqueue** callable
  from a latency-sensitive thread: network I/O happens on the client's own
  background machinery, never the caller's thread, and it never raises when the
  broker is unreachable.
- ``connected`` is a cheap, non-blocking liveness read.
- ``will_set(topic, payload, *, qos, retain)`` configures the **Last Will**, and
  must be honoured when called before :meth:`connect`.
- ``connect()`` / ``disconnect()`` start and stop that background machinery.
- Optional: ``set_on_connect(callback)`` fires on every successful (re)connect.
  When it is absent the publisher falls back to edge-detecting ``connected``.

The topic map
-------------
Two trees under one root (``reachy`` by default, injectable):

- ``reachy/events/{source}/{type}`` — one message per runtime-feed event, NOT
  retained, QoS 0. ``{source}`` is the feed's block type (``sense`` / ``rule`` /
  ``intent`` / ``motion``) and ``{type}`` is that block's action
  (``fire``/``suppress``, ``declare``/``update``/``clear``/``applied``/
  ``blocked``, ``admit``/``evict``/``goto``); a ``sense`` snapshot has no action
  and publishes as ``sense/snapshot``. The payload is
  :func:`~reachy.export.runtime.runtime_to_jsonl`'s output **verbatim** — the
  same serializer, and therefore the same wire contract
  (``docs/export-schema.md``), as the stdout ``--export -`` feed. One
  serializer, two transports: the two surfaces cannot drift.
- ``reachy/state/{key}`` — one RETAINED message per top-level key of the
  engine's ``state.json`` payload, QoS 0, so a late subscriber immediately sees
  current state. The publisher never derives state itself: :meth:`
  NervousPublisher.state_writer` wraps the ONE existing writer, so the bus is a
  transport for the same truth, not a second source of it.
- ``reachy/state/online`` — RETAINED availability, ``true`` while the publisher
  holds a session, with a **Last Will** of ``false`` so an ungraceful death
  (``kill -9``, a lost link) flips it without the runtime's cooperation.
  Without this a dead runtime leaves permanently-stale retained state that
  reads as live. ``online`` is publisher-owned and reserved: a state key of
  that name is refused rather than allowed to shadow it.

**Media never travels this bus.** Events carry only TEXT REFERENCES to media —
a file location, or a memory-link handle; frames and audio move out-of-band and
the bus only announces where they are. The guarantee is structural, not a
convention: the runtime event model declares no binary field, and serialization
is :func:`json.dumps` with no ``default=``, so a ``bytes`` value cannot be
encoded — it resolves to a named drop instead. :func:`is_text_reference_only` is
the declared predicate that states the rule for consumers and tests.

Degradation
-----------
Ported from ``reachy_nova``'s ``nova_mqtt.py`` (an unreachable broker makes every
publish a no-op and the app runs unaffected) and tightened with this repo's
senselog discipline: an absent, incompatible, unreachable or raising client
resolves to ONE named ``[SENSE stage=nervous source=mqtt ...] dropped
reason=<reason>`` line and no-op publishes — never an exception on the caller's
thread, and never a silent no-op. Each distinct reason is reported once; the
latch clears on a reconnect, so a second outage is reported again.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable
from typing import Protocol

from reachy import senselog
from reachy.export.runtime import RUNTIME_BLOCKS, RuntimeConsumer, runtime_to_jsonl

# --------------------------------------------------------------------------- #
# Senselog identity                                                           #
# --------------------------------------------------------------------------- #

#: The senselog ``stage`` every line from this module carries.
STAGE = "nervous"
#: The senselog ``source`` every line from this module carries.
SOURCE = "mqtt"

# --------------------------------------------------------------------------- #
# Topic map                                                                   #
# --------------------------------------------------------------------------- #

#: Default root of both topic trees.
DEFAULT_TOPIC_ROOT = "reachy"
#: The retained availability key under ``<root>/state/``. Publisher-owned.
ONLINE_KEY = "online"
#: Retained availability payloads (compact JSON booleans).
ONLINE_PAYLOAD = "true"
OFFLINE_PAYLOAD = "false"
#: The ``{type}`` segment for a perception snapshot, which carries no action.
SENSE_EVENT_TYPE = "snapshot"
#: At-most-once for every topic: a dropped event under load matches the
#: drop-don't-block ethos, and retained state is self-healing by retention.
QOS = 0

# --------------------------------------------------------------------------- #
# Broker configuration (read here, applied by the composition site)           #
# --------------------------------------------------------------------------- #

#: Env var naming the broker the composed client should reach.
BROKER_URL_ENV = "REACHY_MQTT_URL"
#: Loopback default — the broker binds localhost; remote consumers are an
#: explicit, documented opt-in, never the default.
DEFAULT_BROKER_URL = "localhost:1883"

# --------------------------------------------------------------------------- #
# Named drop reasons — every drop names one of these, verbatim and greppable  #
# --------------------------------------------------------------------------- #

REASON_NO_CLIENT = "no-client"
REASON_CLIENT_INCOMPATIBLE = "client-incompatible"
REASON_CONNECT_FAILED = "connect-failed"
REASON_BROKER_UNREACHABLE = "broker-unreachable"
REASON_PUBLISH_FAILED = "publish-failed"
REASON_UNSERIALIZABLE = "unserializable-payload"
REASON_RESERVED_STATE_KEY = "state-key-reserved"
REASON_BAD_STATE = "state-not-a-mapping"
REASON_UNKNOWN_EVENT = "unknown-event-type"
REASON_DISCONNECT_FAILED = "disconnect-failed"


# --------------------------------------------------------------------------- #
# The client seam                                                             #
# --------------------------------------------------------------------------- #


class EventClient(Protocol):
    """The narrow client surface this repo requires of ``events-cli``.

    Structural typing only — nothing here imports or subclasses anything from
    that project. See the module docstring for the behavioural half of the
    contract (O(1) enqueue, never raises on an unreachable broker).
    """

    @property
    def connected(self) -> bool:
        """Cheap, non-blocking: is a broker session live right now?"""

    def will_set(self, topic: str, payload: str, *, qos: int = 0, retain: bool = True) -> None:
        """Configure the Last Will, honoured when called before :meth:`connect`."""

    def connect(self) -> None:
        """Start the background transport. Should not block on the network."""

    def disconnect(self) -> None:
        """Stop the background transport (graceful: no Last Will is delivered)."""

    def publish(self, topic: str, payload: str, *, qos: int = 0, retain: bool = False) -> None:
        """Enqueue one message. O(1); no network I/O on the calling thread."""


#: Members the injected client MUST expose; a client missing any of them
#: degrades to one named drop instead of crashing the runtime.
REQUIRED_CLIENT_MEMBERS: tuple[str, ...] = (
    "connected",
    "will_set",
    "connect",
    "disconnect",
    "publish",
)

#: Members used when present. ``set_on_connect`` lets a reconnect be noticed the
#: moment it happens; without it the publisher edge-detects ``connected`` on the
#: next publish, which is a lull later but never wrong.
OPTIONAL_CLIENT_MEMBERS: tuple[str, ...] = ("set_on_connect",)


def _exposes(client: object, name: str) -> bool:
    """Does *client* expose *name*, without a raising accessor counting as yes?

    ``connected`` is a property, so probing it runs client code. A probe that
    raises means the client cannot be driven — the honest reading is "missing",
    not an exception escaping into the caller.
    """
    try:
        getattr(client, name)
    except Exception:  # noqa: BLE001 - a sick accessor is a missing member
        return False
    return True


def missing_client_members(client: object) -> tuple[str, ...]:
    """Names from :data:`REQUIRED_CLIENT_MEMBERS` that *client* does not expose."""
    return tuple(name for name in REQUIRED_CLIENT_MEMBERS if not _exposes(client, name))


def broker_url(env: dict | None = None) -> str:
    """The broker URL the composition site should hand its client.

    Reads :data:`BROKER_URL_ENV`, defaulting to :data:`DEFAULT_BROKER_URL`. This
    module never opens a connection itself — the value is config *for the
    client*, kept here so the topic map and its endpoint stay in one place.
    """
    source = os.environ if env is None else env
    return source.get(BROKER_URL_ENV) or DEFAULT_BROKER_URL


# --------------------------------------------------------------------------- #
# The no-media boundary                                                       #
# --------------------------------------------------------------------------- #

#: A ``data:`` URI naming an inline media blob.
_DATA_URI_RE = re.compile(r"^\s*data:(image|audio|video|application/octet-stream)", re.IGNORECASE)
#: A long uninterrupted base64 run — a blob, not a reference. The threshold is
#: far above any plausible identifier/handle/path this feed carries.
_BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{256,}={0,2}")
#: Topic segments must never carry an MQTT separator or wildcard.
_UNSAFE_SEGMENT_RE = re.compile(r"[^0-9A-Za-z._-]")


def is_text_reference_only(payload: object) -> bool:
    """Is *payload* free of inline binary and base64 media?

    The declared statement of the bus's hard product boundary: events carry only
    TEXT REFERENCES to media (a file path, a URL, a memory-link handle). A
    ``bytes``-like value, a ``data:`` media URI or a long base64 run is inline
    media and fails. Used by the schema tests and available to consumers as an
    executable statement of the rule; it is NOT called on the publish path,
    where the guarantee is already structural (:func:`json.dumps` with no
    ``default=`` cannot encode binary at all).
    """
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return False
    if isinstance(payload, str):
        return not _DATA_URI_RE.match(payload) and not _BASE64_BLOB_RE.search(payload)
    if isinstance(payload, dict):
        return all(
            is_text_reference_only(key) and is_text_reference_only(value)
            for key, value in payload.items()
        )
    if isinstance(payload, (list, tuple)):
        return all(is_text_reference_only(item) for item in payload)
    return True


def _segment(value: object) -> str:
    """Coerce *value* into one safe MQTT topic segment.

    Wildcards (``+``/``#``), separators (``/``) and control characters can never
    reach a topic — a malformed event must not be able to publish outside its
    own subtree. An empty result becomes ``unknown`` rather than an empty level.
    """
    text = _UNSAFE_SEGMENT_RE.sub("-", str(value).strip())
    return text or "unknown"


def _event_id() -> str:
    return uuid.uuid4().hex[:8]


def _detail(reason: str, extra: str = "") -> str:
    """``senselog.drop`` detail: the bare reason token first, context after."""
    return f"{reason} {extra}".strip()


# --------------------------------------------------------------------------- #
# The publisher                                                               #
# --------------------------------------------------------------------------- #


class NervousPublisher:
    """Publish the runtime feed and standing state through an injected client.

    Shaped as a **sink** (``emit(event)``, like
    :class:`~reachy.export.exporter.JsonlExporter`) so
    :class:`~reachy.export.runtime.RuntimeConsumer` drives it unchanged — that
    is what makes the broker payloads and the stdout NDJSON feed the same bytes
    by construction. :meth:`as_tick_consumer` returns the ready-made
    ``TickBus``-shaped consumer.

    Every public method is total: it returns normally for any input and any
    client state, reporting faults as named senselog drops. Nothing here may
    raise into the 50 Hz tick thread.

    Args:
        client: an object satisfying :class:`EventClient`, or ``None`` (the
            no-broker profile — one named drop, then silent no-ops).
        root: the topic-tree root; ``reachy`` by default.
    """

    def __init__(
        self, client: EventClient | None = None, *, root: str = DEFAULT_TOPIC_ROOT
    ) -> None:
        self._client = client
        clean_root = str(root).strip().strip("/") or DEFAULT_TOPIC_ROOT
        self._events_root = f"{clean_root}/events"
        self._state_root = f"{clean_root}/state"
        self._online_topic = f"{self._state_root}/{ONLINE_KEY}"
        # ``_lock`` guards the connection edge and the retained-state cache: the
        # tick thread publishes while the client's own thread may fire
        # ``set_on_connect``. Uncontended acquisition is O(1) and nanoseconds —
        # it never becomes network wait, which is the property that matters.
        self._lock = threading.Lock()
        self._online = False
        self._connected_once = False
        self._disabled = False
        self._started = False
        self._stopped = False
        self._reported: set[str] = set()
        self._last_state: dict | None = None
        #: Observability counters (also what the O(1) tests read).
        self.published = 0
        self.failed_publishes = 0

    # -- introspection -------------------------------------------------------

    @property
    def online_topic(self) -> str:
        """The retained availability topic this publisher owns."""
        return self._online_topic

    @property
    def events_root(self) -> str:
        return self._events_root

    @property
    def state_root(self) -> str:
        return self._state_root

    @property
    def connected(self) -> bool:
        """Last-known liveness of the injected client's broker session."""
        return self._online

    @property
    def degraded(self) -> bool:
        """True when publishes are no-ops (no client, or no live session)."""
        return self._disabled or not self._online

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        """Configure the Last Will, connect, and announce availability.

        Returns whether a live session resulted. A missing/incompatible client
        or a raising ``connect()`` is a NORMAL no-broker outcome: one named
        drop, publishing disabled for the process, and the caller carries on.
        """
        if self._started:
            return self._online
        self._started = True

        if self._client is None:
            self._disable(REASON_NO_CLIENT, "(nothing injected; publishing disabled)")
            return False

        missing = missing_client_members(self._client)
        if missing:
            self._disable(REASON_CLIENT_INCOMPATIBLE, f"missing={','.join(missing)}")
            return False

        try:
            # The Last Will MUST be registered before the session opens, or the
            # broker never learns it and an ungraceful death goes unnoticed.
            self._client.will_set(self._online_topic, OFFLINE_PAYLOAD, qos=QOS, retain=True)
            on_connect = getattr(self._client, "set_on_connect", None)
            if callable(on_connect):
                on_connect(self._note_client_connected)
            self._client.connect()
        except Exception as err:  # noqa: BLE001 - a dead broker must not kill the runtime
            self._disable(
                REASON_CONNECT_FAILED,
                f"url={broker_url()} ({type(err).__name__}: {err})",
            )
            return False

        live = self._sync_connection()
        if not live:
            # A client that accepted connect() but has no session yet is the
            # normal broker-not-up-yet outcome — but it must never be a SILENT
            # no-op. Name it once; the latch clears the moment a session lands.
            self._drop(REASON_BROKER_UNREACHABLE, f"(no session after connect to {broker_url()})")
        return live

    def stop(self) -> None:
        """Flip availability false and close the session — idempotent, total."""
        if self._stopped or self._disabled or self._client is None:
            return
        self._stopped = True
        if self._online:
            self._publish_raw(self._online_topic, OFFLINE_PAYLOAD, retain=True)
        try:
            self._client.disconnect()
        except Exception as err:  # noqa: BLE001 - shutdown must never raise
            self._drop(REASON_DISCONNECT_FAILED, f"({type(err).__name__}: {err})")
        with self._lock:
            self._online = False
        senselog.stage(STAGE, SOURCE, _event_id(), "publisher stopped")

    # -- the sink surface ----------------------------------------------------

    def emit(self, event: object) -> None:
        """Publish one runtime event on ``<root>/events/{source}/{type}``.

        Sink-shaped, so :class:`~reachy.export.runtime.RuntimeConsumer` can hand
        events straight through. Not retained: the event stream is a stream.
        """
        if not self._sync_connection():
            return
        topic = self._event_topic(event)
        if topic is None:
            self._drop(REASON_UNKNOWN_EVENT, f"t={getattr(event, 't', None)!r}")
            return
        try:
            payload = runtime_to_jsonl(event)
        except (TypeError, ValueError) as err:
            # The no-media boundary lands here: bytes are not JSON-encodable.
            self._drop(REASON_UNSERIALIZABLE, f"topic={topic} ({type(err).__name__})")
            return
        self._publish_raw(topic, payload, retain=False)

    def as_tick_consumer(self) -> Callable[[dict], None]:
        """A ``TickBus``-shaped ``consumer(event: dict)`` feeding this publisher.

        Reuses :class:`~reachy.export.runtime.RuntimeConsumer` verbatim, so the
        raw-``ctx.emit``-dict mapping is identical to the stdout feed's — an
        unrecognised event maps to nothing on BOTH surfaces, always.
        """
        return RuntimeConsumer(self)

    # -- standing state ------------------------------------------------------

    def publish_state(self, state: dict) -> None:
        """Mirror the engine's ``state.json`` payload onto retained topics.

        One retained message per top-level key. *state* must be the payload the
        ONE existing builder produced — this method never derives state, and
        :meth:`state_writer` is the wiring that makes that structural.
        """
        if not isinstance(state, dict):
            self._drop(REASON_BAD_STATE, f"type={type(state).__name__}")
            return
        snapshot = dict(state)
        with self._lock:
            # Cached so a reconnect can republish CURRENT state, not a stale one.
            self._last_state = snapshot
        if not self._sync_connection():
            return
        self._publish_state_payload(snapshot)

    def state_writer(self, write_state: Callable[[dict], None]) -> Callable[[dict], None]:
        """Wrap the engine's state writer so the bus mirrors the same payload.

        The disk write happens FIRST and unconditionally — the mirror is purely
        additive, so a dead bus never costs the runtime its state file. Because
        both surfaces receive the identical object, they cannot drift; the
        equality is asserted by test.
        """

        def _write(state: dict) -> None:
            write_state(state)
            self.publish_state(state)

        return _write

    # -- internals -----------------------------------------------------------

    def _event_topic(self, event: object) -> str | None:
        block = getattr(event, "t", None)
        if not isinstance(block, str) or block not in RUNTIME_BLOCKS:
            return None
        if block == "sense":
            kind = SENSE_EVENT_TYPE
        else:
            kind = _segment(getattr(event, "action", ""))
        return f"{self._events_root}/{_segment(block)}/{kind}"

    def _publish_state_payload(self, state: dict) -> None:
        for key, value in state.items():
            segment = _segment(key)
            if segment == ONLINE_KEY:
                self._drop(REASON_RESERVED_STATE_KEY, f"key={key!r}")
                continue
            try:
                payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError) as err:
                self._drop(REASON_UNSERIALIZABLE, f"key={key!r} ({type(err).__name__})")
                continue
            self._publish_raw(f"{self._state_root}/{segment}", payload, retain=True)

    def _publish_raw(self, topic: str, payload: str, *, retain: bool) -> None:
        """The ONE call into the client. O(1) by the contract required of it."""
        client = self._client
        if client is None or self._disabled:
            return
        try:
            client.publish(topic, payload, qos=QOS, retain=retain)
        except Exception as err:  # noqa: BLE001 - a publish must never reach the tick
            self.failed_publishes += 1
            self._drop(
                REASON_PUBLISH_FAILED,
                f"topic={topic} ({type(err).__name__}: {err})",
            )
            return
        self.published += 1

    def _note_client_connected(self) -> None:
        """The optional ``set_on_connect`` seam — may run on the client's thread."""
        self._sync_connection()

    def _sync_connection(self) -> bool:
        """Edge-detect the session; returns whether publishing is live.

        Called at the head of every publish path (a cheap boolean read), so a
        reconnect is noticed even from a client that offers no callback.
        """
        if self._disabled or self._client is None:
            return False
        try:
            live = bool(self._client.connected)
        except Exception as err:  # noqa: BLE001 - a sick client is named, not fatal
            self._drop(
                REASON_BROKER_UNREACHABLE,
                f"(connected probe raised {type(err).__name__}: {err})",
            )
            return False
        with self._lock:
            if live == self._online:
                return live
            self._online = live
            if not live:
                self._drop_locked(REASON_BROKER_UNREACHABLE, "(session lost)")
                return False
            first = not self._connected_once
            self._connected_once = True
            # A fresh session earns a fresh report budget: a SECOND outage must
            # be named again rather than swallowed by the first one's latch.
            self._reported.clear()
            cached = self._last_state
        senselog.stage(
            STAGE,
            SOURCE,
            _event_id(),
            "connected" if first else "reconnected — republishing retained state",
        )
        self._publish_raw(self._online_topic, ONLINE_PAYLOAD, retain=True)
        if cached is not None:
            self._publish_state_payload(cached)
        return True

    def _disable(self, reason: str, extra: str = "") -> None:
        """Hard-off: report once, then every publish path is a pure no-op."""
        self._drop(reason, extra)
        self._disabled = True

    def _drop(self, reason: str, extra: str = "") -> None:
        """Emit ONE named senselog drop per distinct reason per session.

        The line always BEGINS with the bare reason token, so
        ``grep 'dropped reason=broker-unreachable'`` keeps working however much
        context a call site appends after it.
        """
        with self._lock:
            if reason in self._reported:
                return
            self._reported.add(reason)
        senselog.drop(STAGE, SOURCE, _event_id(), _detail(reason, extra))

    def _drop_locked(self, reason: str, extra: str = "") -> None:
        """:meth:`_drop` for a caller that already holds ``_lock``."""
        if reason in self._reported:
            return
        self._reported.add(reason)
        senselog.drop(STAGE, SOURCE, _event_id(), _detail(reason, extra))
