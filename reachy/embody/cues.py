"""Turn the runtime's own exported events into cues the embodiment layer can think about.

The layer perceives the robot's own reflex life exactly the way ``agent
attach`` perceives the deterministic runtime (see
``reachy/cli/_commands/agent.py``'s ``_CUE_MAPPERS`` — the shape this module
follows): the runtime feed's four block types (``sense`` / ``rule`` /
``intent`` / ``motion``, :data:`docs/export-schema.md`'s Runtime Event Feed)
each map to zero or more short first-person perception-cue strings. **A rule
fire/suppress is the headline input, not an afterthought**: when the robot's
own scratch/pat/react rule fires, this is what lets the layer react IN VOICE
to its own reflexes (the spec's before/after story: "a scratch draws a spoken
response") — so :func:`cues_for_runtime_event`'s ``rule`` mapper never returns
an empty cue for a fire, and is exercised first in this module's test table.

Two intake routes feed the SAME mapping (:func:`cues_for_line` /
:func:`cues_for_runtime_event`), so nothing downstream cares which one
produced a line — the wire shape is identical either way
(``docs/export-schema.md``: "the payload is ``runtime_to_jsonl(event)``'s
output, verbatim... one serializer, two transports"):

* **primary — the MQTT bus.** :func:`open_runtime_lines` resolves a
  :class:`BusSubscriber` (see below) and, if one connects and subscribes,
  drains ``reachy/events/#`` messages as they arrive.
* **fallback — tailing the NDJSON feed** (``behavior engine run --export
  -``), the exact shape ``agent attach --feed`` already reads.

Nothing here changes what the runtime publishes. This module is a pure
CONSUMER of two already-shipped, already-tested wire contracts (the bus topic
map in ``reachy/export/mqtt.py`` and the NDJSON feed in
``reachy/export/runtime.py``) — it holds no import edge to either module,
by design: the fallback route only ever needs to read TEXT LINES matching the
documented schema, and the bus route only ever needs to read TEXT PAYLOADS off
a topic, so this module treats both exactly the same way a hand-rolled reader
following ``docs/export-schema.md`` would, with no Python import from
``reachy.export`` required (mirroring that document's own promise to an
external consumer).

The reported gap — events-cli is publish-only today
-----------------------------------------------------
The repo's decision is that a bus client is *always* the events-cli
package, adapted through one narrow declared surface, never a raw MQTT
library (see ``reachy/export/events_client.py``'s module docstring for the
adapter discipline this module follows for the PUBLISH leg). For the
SUBSCRIBE direction that discipline has nothing to adapt: the installed
``events-cli>=0.9`` ships exactly one client class, ``EventClient``, and
it exposes no ``subscribe`` / ``on_message`` surface anywhere — not in the
Python API, not in the ``events`` CLI (verified by grepping the installed
distribution; ``test_the_installed_events_cli_client_has_no_subscribe_capability``
is the live canary that will start failing the day this changes). So unlike
the publish leg, this module does **not** import events-cli's client module
at all — there is no vendor shape here to bind to yet. :class:`BusSubscriber`
is the seam a future events-cli release would satisfy (or a test fake
satisfies today);
:func:`resolve_bus_subscriber` is where that binding would be wired once it
exists. Until then this is a REPORTED gap, not a patched one: every call
degrades to one named drop and the feed-tail fallback, exactly per this
task's acceptance contract, and no workaround lives in this file or in the
runtime.

Named drops, never a silent no-op
----------------------------------
Every degrade path — an unrecognised/malformed runtime line, a missing bus
subscriber, an incompatible one, a failed connect/subscribe, a session that
never materializes — resolves to exactly one
``[SENSE stage=cue source=<runtime|bus> event=<id>] dropped reason=<reason>``
line via :mod:`reachy.senselog`, greppable and named from the fixed
vocabulary declared below (mirrors ``reachy/export/mqtt.py``'s discipline).
"""

from __future__ import annotations

import json
import math
import queue
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol, TextIO

from reachy import senselog

# --------------------------------------------------------------------------- #
# Senselog identity                                                           #
# --------------------------------------------------------------------------- #

#: The senselog ``stage`` every line from this module carries.
STAGE = "cue"
#: ``source`` for cue-mapping events (an unrecognised/malformed runtime line).
SOURCE_RUNTIME = "runtime"
#: ``source`` for bus-intake events (subscribe attempt succeeded/failed).
SOURCE_BUS = "bus"

# --------------------------------------------------------------------------- #
# Named drop reasons — every drop names one of these, verbatim and greppable  #
# --------------------------------------------------------------------------- #

REASON_UNKNOWN_LINE_TYPE = "unknown-line-type"
REASON_MALFORMED_LINE = "malformed-line"
REASON_NO_SUBSCRIBER = "no-bus-subscriber"
REASON_SUBSCRIBER_INCOMPATIBLE = "bus-subscriber-incompatible"
REASON_CONNECT_FAILED = "bus-connect-failed"
REASON_BROKER_UNREACHABLE = "bus-broker-unreachable"
REASON_SUBSCRIBE_FAILED = "bus-subscribe-failed"


def _eid() -> str:
    return uuid.uuid4().hex[:8]


def _detail(reason: str, extra: str = "") -> str:
    """``senselog.drop`` detail: the bare reason token first, context after."""
    return f"{reason} {extra}".strip()


def _drop(source: str, reason: str, extra: str = "") -> None:
    senselog.drop(STAGE, source, _eid(), _detail(reason, extra))


# ---------------------------------------------------------------------------
# Sound-direction band + loudness threshold + pat phrasing — cited from
# reachy.cli._commands.agent's _CUE_MAPPERS (cite-don't-import: that module is
# a CLI command module this package must never be imported BY, so the
# vocabulary is restated here rather than shared by import), which itself
# mirrors reachy.speech.events' DoA convention (0 = left, pi/2 = front, pi =
# right; ~15 degree "ahead" band) and its loud-sound floor.
# ---------------------------------------------------------------------------

_AHEAD_BAND_RAD: float = 0.26
_LOUD_RMS_THRESHOLD: float = 0.02

_PAT_KIND_PHRASE: dict[str, str] = {"scratch": "scratch", "side_pat": "sideways nudge"}
_PAT_LEVEL_INTENSITY: dict[str, str] = {"level1": "gentle", "level2": "firm"}


def _direction_word(doa: object) -> str | None:
    """Map a DoA angle (radians) to ``"left"`` / ``"ahead"`` / ``"right"``, or ``None``."""
    if doa is None:
        return None
    try:
        angle = float(doa)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    front = math.pi / 2.0
    if angle < front - _AHEAD_BAND_RAD:
        return "left"
    if angle > front + _AHEAD_BAND_RAD:
        return "right"
    return "ahead"


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Runtime-event -> perception-cue mapping (one function per line type)
# ---------------------------------------------------------------------------


def _rule_cues(event: dict) -> list[str]:
    """Cues for a ``rule`` runtime event — the headline react-in-voice input.

    A ``fire`` NEVER maps to an empty cue: the robot's own react/inhibit
    decision is always worth narrating, whether or not it names a behavior.
    """
    rule = str(event.get("rule") or "a rule")
    action = event.get("action")
    if action == "fire":
        behavior = event.get("behavior")
        disable = event.get("disable") or []
        if behavior:
            return [f"a behavior rule fired ({rule}): now doing {behavior}"]
        if disable:
            joined = ", ".join(str(d) for d in disable)
            return [f"a behavior rule fired ({rule}): stopping {joined}"]
        return [f"a behavior rule fired ({rule})"]
    if action == "suppress":
        return [f"a behavior rule held off ({rule})"]
    return []


def _sense_cues(event: dict) -> list[str]:
    """Cues for a ``sense`` runtime event (pat, face, rms, speech, doa, frame_available)."""
    cues: list[str] = []
    direction = _direction_word(event.get("doa"))
    rms = event.get("rms")
    if event.get("speech"):
        cues.append(f"speech from the {direction}" if direction else "speech nearby")
    elif _is_number(rms) and rms >= _LOUD_RMS_THRESHOLD:
        cues.append(f"loud sound {direction}" if direction else "loud sound nearby")

    pat = event.get("pat")
    if isinstance(pat, (list, tuple)) and len(pat) == 2:
        phrase = _PAT_KIND_PHRASE.get(pat[0])
        intensity = _PAT_LEVEL_INTENSITY.get(pat[1])
        if phrase and intensity:
            cues.append(f"felt a {intensity} {phrase} on the head")

    face = event.get("face")
    if isinstance(face, str) and face.strip():
        cues.append(f"saw {face.strip()}")

    # Positive-only, mirroring every other sub-field above: a camera view
    # being unavailable is the common (no [vision] extra, or the daemon not
    # up yet) case and would otherwise republish a "no camera" cue on every
    # sense-snapshot change forever — noise, not perception. Only the
    # positive "I can see something now" transition is worth narrating.
    if event.get("frame_available"):
        cues.append("a camera frame is available")

    return cues


def _intent_cues(event: dict) -> list[str]:
    """Cues for an ``intent`` runtime event (declare / update / clear).

    ``applied`` / ``blocked`` are the IntentDriver's own status emissions —
    recognised, but deliberately silent here (the declare/update/clear the
    status describes already produced its own cue moments earlier).
    """
    action = event.get("action")
    name = str(event.get("name") or "").strip()
    if action == "clear":
        return ["a standing intent was cleared"]
    if action in ("declare", "update"):
        verb = "set" if action == "declare" else "updated"
        return [
            f"a standing intent was {verb}: {name}" if name else f"a standing intent was {verb}"
        ]
    return []


def _motion_cues(event: dict) -> list[str]:
    """Cues for a ``motion`` runtime event (admit / evict).

    A low-level ``goto`` is not surfaced — same reasoning as ``agent attach``:
    it would flood turns with keyframe-level noise instead of the higher-level
    behavior admissions that actually matter to a conversation.
    """
    action = event.get("action")
    label = str(event.get("behavior") or "a body behavior")
    if action == "admit":
        return [f"started moving: {label}"]
    if action == "evict":
        return [f"stopped moving: {label}"]
    return []


#: Every runtime line type this module recognises, mapped to its cue function.
#: This IS the table :mod:`tests.test_embody_cues`' table test pins.
CUE_MAPPERS: dict[str, Callable[[dict], list[str]]] = {
    "rule": _rule_cues,
    "sense": _sense_cues,
    "intent": _intent_cues,
    "motion": _motion_cues,
}


def cues_for_runtime_event(event: object) -> list[str]:
    """Map one runtime-feed event to zero or more perception-cue strings.

    Dispatches on the event's ``t`` discriminator against :data:`CUE_MAPPERS`.
    A non-dict event, or a dict whose ``t`` is not one of the four recognised
    line types, is skipped with exactly ONE named :mod:`reachy.senselog` drop
    (never a silent no-op) and yields no cue. A RECOGNISED type that happens to
    produce zero cues this time (an ``intent.applied`` status, a ``motion.goto``,
    an unrecognised rule action, a quiet sense snapshot) is a normal outcome,
    not an error, and is not logged as a drop.
    """
    if not isinstance(event, dict):
        _drop(SOURCE_RUNTIME, REASON_MALFORMED_LINE, f"type={type(event).__name__}")
        return []
    line_type = event.get("t")
    mapper = CUE_MAPPERS.get(line_type)
    if mapper is None:
        _drop(SOURCE_RUNTIME, REASON_UNKNOWN_LINE_TYPE, f"t={line_type!r}")
        return []
    texts = mapper(event)
    for text in texts:
        senselog.stage(STAGE, SOURCE_RUNTIME, _eid(), text)
    return texts


def parse_runtime_line(line: str) -> dict | None:
    """Parse one JSONL runtime-feed line into an event dict, or ``None`` for junk/blank."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def cues_for_line(line: str) -> list[str]:
    """Parse one runtime-feed line and map it to cue text (blank lines yield none).

    The one entry point a caller iterating :func:`open_runtime_lines` needs —
    composes :func:`parse_runtime_line` and :func:`cues_for_runtime_event` so
    the two intake routes and the mapping stay a single seam.
    """
    event = parse_runtime_line(line)
    if event is None:
        return []
    return cues_for_runtime_event(event)


# ---------------------------------------------------------------------------
# Bus intake (primary) — an injectable seam, never a raw MQTT library
# ---------------------------------------------------------------------------

#: The wildcard subscription covering every runtime-feed event
#: (``docs/export-schema.md``'s "Reading the bus" sketch: ``reachy/events/#``).
DEFAULT_TOPIC_FILTER = "reachy/events/#"


class BusSubscriber(Protocol):
    """The narrow client surface this module requires to subscribe to the bus.

    Structural typing only — nothing here imports or subclasses anything from
    any vendor. Mirrors ``reachy/export/mqtt.py``'s ``EventClient`` Protocol
    for the publish leg, one direction over: this module has no ``publish``
    concern, and its one extra requirement is ``subscribe`` itself.
    """

    @property
    def connected(self) -> bool:
        """Cheap, non-blocking: is a broker session live right now?"""

    def connect(self) -> None:
        """Start the background transport. Should not block on the network."""

    def disconnect(self) -> None:
        """Stop the background transport."""

    def subscribe(self, topic_filter: str, on_message: Callable[[str, str], None]) -> None:
        """Register *on_message* to be called ``(topic, payload)`` for each match."""


#: Members a subscriber MUST expose; one missing any of them degrades to one
#: named drop instead of crashing the intake.
REQUIRED_SUBSCRIBER_MEMBERS: tuple[str, ...] = ("connected", "connect", "disconnect", "subscribe")


def _exposes(client: object, name: str) -> bool:
    try:
        getattr(client, name)
    except Exception:  # noqa: BLE001 - a sick accessor is a missing member
        return False
    return True


def missing_subscriber_members(client: object) -> tuple[str, ...]:
    """Names from :data:`REQUIRED_SUBSCRIBER_MEMBERS` that *client* does not expose."""
    return tuple(name for name in REQUIRED_SUBSCRIBER_MEMBERS if not _exposes(client, name))


def resolve_bus_subscriber(
    *, factory: Callable[[], BusSubscriber | None] | None = None
) -> BusSubscriber | None:
    """Resolve a bus-subscribe client, or ``None`` when subscribing is not available.

    ``factory`` is the declared injection seam: a test hands in a
    :class:`~tests.fake_bus_subscriber.FakeBusSubscriber`; a future
    composition site would hand in a real adapter the day events-cli grows
    subscribe support. Without an injected factory this ALWAYS resolves to
    ``None`` — see the module docstring's "the reported gap" section: there is
    no vendor shape to bind here today, so unlike
    :mod:`reachy.export.events_client` this function never imports the
    events-cli package itself. Never touches a socket either way.
    """
    if factory is not None:
        return factory()
    return None


def _try_bus_intake(subscriber: BusSubscriber, topic_filter: str) -> Iterator[str] | None:
    """Attempt the primary route; ``None`` means "fall back", always after one named drop."""
    missing = missing_subscriber_members(subscriber)
    if missing:
        _drop(SOURCE_BUS, REASON_SUBSCRIBER_INCOMPATIBLE, f"missing={','.join(missing)}")
        return None

    try:
        subscriber.connect()
    except Exception as err:  # noqa: BLE001 - a dead broker must not stop the layer
        _drop(SOURCE_BUS, REASON_CONNECT_FAILED, f"({type(err).__name__}: {err})")
        return None

    try:
        live = bool(subscriber.connected)
    except Exception as err:  # noqa: BLE001 - a sick client is named, not fatal
        _drop(SOURCE_BUS, REASON_BROKER_UNREACHABLE, f"(connected probe raised {err})")
        return None
    if not live:
        _drop(SOURCE_BUS, REASON_BROKER_UNREACHABLE, "(no session after connect)")
        return None

    pending: queue.Queue[str] = queue.Queue()

    def _on_message(_topic: str, payload: str) -> None:
        pending.put(payload)

    try:
        subscriber.subscribe(topic_filter, _on_message)
    except Exception as err:  # noqa: BLE001 - a refused subscribe must not stop the layer
        _drop(SOURCE_BUS, REASON_SUBSCRIBE_FAILED, f"({type(err).__name__}: {err})")
        return None

    senselog.stage(STAGE, SOURCE_BUS, _eid(), f"subscribed topic_filter={topic_filter!r}")
    return _drain(pending)


def _drain(pending: "queue.Queue[str]") -> Iterator[str]:
    while True:
        yield pending.get()


# ---------------------------------------------------------------------------
# Feed-tail intake (fallback) — the same shape ``agent attach --feed`` reads
# ---------------------------------------------------------------------------


def _tail_feed(feed: str | Path, *, stdin: TextIO | None = None) -> Iterator[str]:
    """Yield NDJSON lines from *feed* (``"-"`` = stdin, else a path/FIFO).

    A FIFO/pipe streams line-by-line as data arrives; a regular file is read
    once to EOF. Never spawns the runtime — it only reads the feed the runtime
    (``behavior engine run --export -``) writes. An unreadable path raises
    naturally (this module is a library, not a CLI verb — the composition
    site, not this module, owns turning that into a structured CLI error).
    """
    feed_str = str(feed)
    if feed_str == "-":
        source = stdin if stdin is not None else sys.stdin
        yield from source
        return
    with Path(feed_str).open("r", encoding="utf-8") as handle:
        yield from handle


# ---------------------------------------------------------------------------
# The one intake entry point: bus first, feed-tail fallback
# ---------------------------------------------------------------------------


def open_runtime_lines(
    *,
    feed: str | Path = "-",
    stdin: TextIO | None = None,
    topic_filter: str = DEFAULT_TOPIC_FILTER,
    subscriber_factory: Callable[[], BusSubscriber | None] | None = None,
) -> Iterator[str]:
    """Yield runtime-feed JSONL lines from the bus, or the feed-tail fallback.

    Two intake routes, one mapping (:func:`cues_for_line` consumes either
    output identically — see the module docstring). The route decision itself
    runs EAGERLY (before this function returns), so a caller can assert on the
    resulting :mod:`reachy.senselog` drop without first consuming the
    iterator; consumption of whichever route wins stays lazy.

    * A bus subscriber is resolved via :func:`resolve_bus_subscriber`
      (``subscriber_factory`` overrides the default resolver — the seam tests
      and a future composition site use). ``None`` — the current, always-true
      outcome with no injected factory — is one named ``no-bus-subscriber``
      drop, then the fallback.
    * An injected subscriber that is missing a required member, whose
      ``connect()`` raises, that never reaches a live session, or whose
      ``subscribe()`` raises is each exactly one named drop, then the
      fallback. A successful subscribe drains messages off an internal queue
      forever; the feed is never opened in that case.
    * The fallback tails *feed* (``"-"`` = stdin, default) exactly as
      ``agent attach --feed`` does.
    """
    subscriber = resolve_bus_subscriber(factory=subscriber_factory)
    if subscriber is None:
        _drop(SOURCE_BUS, REASON_NO_SUBSCRIBER, "(no bus-subscribe capability available)")
    else:
        bus_lines = _try_bus_intake(subscriber, topic_filter)
        if bus_lines is not None:
            return bus_lines
    return _tail_feed(feed, stdin=stdin)
