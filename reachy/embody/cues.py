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

The per-type mapping functions themselves (``rule``/``sense``/``intent``/
``motion``), plus the DoA band / loudness floor / pat-phrasing vocabulary they
share with ``agent attach``'s ``_CUE_MAPPERS``, now live in
:mod:`reachy.runtime_cues` — SonarCloud flagged the two modules' copies as
duplicated blocks on PR #140. This module still defines its OWN ``_sense_cues``
(layering a ``frame_available`` cue the shared core does not carry) and its
own ``cues_for_runtime_event``/``CUE_MAPPERS`` dispatch (which names a
:mod:`reachy.senselog` drop for an unrecognised/malformed event — see "Named
drops" below — where ``agent attach``'s dispatch stays silent); see
:mod:`reachy.runtime_cues`'s own docstring for the full account of what moved
and what deliberately did not.

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

Because both routes hand their lines to the same :func:`cues_for_line` /
:func:`classified_cues_for_line`, a bus-delivered event and a feed-tailed
event of identical content classify identically by construction — neither
function knows or cares which transport produced the line it is looking at.

Cue classification — alert vs context (issue #143)
-----------------------------------------------------
:func:`cues_for_runtime_event` / :func:`cues_for_line` stay exactly as they
were: bare ``list[str]``, because :mod:`reachy.runtime_cues`'s shared shape
and ``agent attach``'s own caller must stay untouched (boundary claim c20).
This module additionally exposes a classified counterpart —
:func:`classified_cues_for_runtime_event` / :func:`classified_cues_for_line`,
returning :class:`ClassifiedCue` — for a caller that needs to know WHICH of
#143's two admission lanes a cue belongs to. A rule FIRE is the one
``CueClass.ALERT``; every other recognised event (a sense snapshot, an intent
change, a motion admit/evict) and a rule SUPPRESSION are
``CueClass.CONTEXT``. The class is decided from the event's TYPE (and, for
``rule``, its fire/suppress ``action``) at the same dispatch point
:data:`CUE_MAPPERS` already uses — never re-derived from a mapper's rendered
cue TEXT, which by the time it exists has already lost the distinction that
mattered. This module only decides and carries the class; giving ALERT and
CONTEXT different admission behaviour in the turn engine is issue #143b, a
later task, not this one.

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

import enum
import queue
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from reachy import runtime_cues, senselog

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
# Runtime-event -> perception-cue mapping (one function per line type)
# ---------------------------------------------------------------------------
#
# The DoA band / loudness floor / pat-phrasing constants, ``direction_word``,
# ``rule_cues``, the ``sense_cues`` core, ``intent_cues`` and ``motion_cues``
# all live in :mod:`reachy.runtime_cues` now — SonarCloud flagged this
# module's copies as duplicated against ``reachy.cli._commands.agent``'s
# ``_CUE_MAPPERS`` on PR #140. Previously the vocabulary was RESTATED here
# rather than imported, because the only place it lived was
# ``reachy.cli._commands.agent`` — a CLI command module this package must
# never be imported BY. That constraint still holds; what changed is that the
# vocabulary now lives in a THIRD, neutral module neither side already
# forbade importing (see ``reachy.runtime_cues``'s own docstring for exactly
# why it sits outside ``reachy.cli``/``reachy.embody``/``reachy.speech``), so
# both callers cite one owner instead of each carrying its own copy.
#
# ``_sense_cues`` below is the one function this module still defines itself:
# it layers a ``frame_available`` cue on top of the shared
# :func:`reachy.runtime_cues.sense_cues` core, which ``agent attach`` does
# NOT do — see ``reachy.runtime_cues``'s docstring for why that is reported as
# likely accidental drift on the ``agent attach`` side rather than folded away
# here.


def _sense_cues(event: dict) -> list[str]:
    """Cues for a ``sense`` runtime event (pat, face, rms, speech, doa, frame_available).

    Delegates the speech/loud-sound, pat and face core to
    :func:`reachy.runtime_cues.sense_cues` (identical to ``agent attach``'s
    mapper) and layers ``frame_available`` on top — the one extension this
    layer's cue vocabulary has that ``agent attach``'s does not.
    """
    cues = list(runtime_cues.sense_cues(event))

    # Positive-only, mirroring every other sub-field above: a camera view
    # being unavailable is the common (no [vision] extra, or the daemon not
    # up yet) case and would otherwise republish a "no camera" cue on every
    # sense-snapshot change forever — noise, not perception. Only the
    # positive "I can see something now" transition is worth narrating.
    if event.get("frame_available"):
        cues.append("a camera frame is available")

    return cues


#: Every runtime line type this module recognises, mapped to its cue function.
#: This IS the table :mod:`tests.test_embody_cues`' table test pins. ``rule``
#: is listed first — not load-bearing for dict lookup, but it keeps the
#: "headline react-in-voice input" framing from the module docstring visible
#: at the point of use: a ``fire`` NEVER maps to an empty cue (see
#: :func:`reachy.runtime_cues.rule_cues`), so the robot's own react/inhibit
#: decision is always worth narrating, whether or not it names a behavior.
CUE_MAPPERS: dict[str, Callable[[dict], list[str]]] = {
    "rule": runtime_cues.rule_cues,
    "sense": _sense_cues,
    "intent": runtime_cues.intent_cues,
    "motion": runtime_cues.motion_cues,
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
    """Parse one JSONL runtime-feed line into an event dict, or ``None`` for junk/blank.

    Delegates to the shared :func:`reachy.runtime_cues.parse_runtime_line` —
    identical logic to ``agent attach``'s ``_parse_runtime_line``.
    """
    return runtime_cues.parse_runtime_line(line)


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
# Cue classification — alert vs context (issue #143)
# ---------------------------------------------------------------------------
#
# The layer's own duplex ears already hear everything a person says, so an
# utterance always triggers a turn — that decision lives in engine.py, not
# here. What a runtime cue adds on top is different in kind: most of it (a
# sense snapshot, a standing-intent change, a motion admit/evict, a rule the
# engine held off) is safe to let accumulate and be picked up on the NEXT
# turn rather than interrupt one. Exactly one kind is not: a rule FIRE, the
# one thing this layer cannot learn any other way (the robot's own reflex
# just did something, in voice or in motion) — the same "headline input"
# :func:`reachy.runtime_cues.rule_cues` already never returns an empty cue
# for. Task #143b (a later task, not this module) is what gives ALERT and
# CONTEXT different admission policies in the turn engine; this module's job
# ends at deciding, and carrying, which class each cue belongs to.
#
# The class is decided from the EVENT's type (and, for ``rule``, its
# fire/suppress ``action``) — never re-derived from a mapper's rendered cue
# TEXT. By the time a mapper has produced a string, the fact that mattered
# (what kind of runtime decision this was) is already what produced that
# string, so classifying straight from the event is both cheaper and stays
# correct even if the wording changes later.


class CueClass(enum.Enum):
    """Which of #143's two admission lanes a cue belongs to (t7 implements both).

    * ``ALERT`` — a rule fire. The one class worth interrupting a turn for.
    * ``CONTEXT`` — everything else this module recognises: a sense snapshot,
      an intent change, a motion admit/evict, and a rule SUPPRESSION. Safe to
      accumulate and be read on the next turn; never a trigger on its own.
    """

    ALERT = "alert"
    CONTEXT = "context"


@dataclass(frozen=True)
class ClassifiedCue:
    """One perception-cue string plus the :class:`CueClass` its source event carries.

    :func:`cues_for_runtime_event` / :func:`cues_for_line` still return bare
    ``list[str]`` — unchanged, because :mod:`reachy.runtime_cues`'s shared
    shape and ``agent attach``'s caller must stay untouched (boundary claim
    c20). This is the ADDITIVE, classified counterpart a caller that needs
    the class (the turn engine's composition) reaches for instead.
    """

    text: str
    cue_class: CueClass


def _rule_cue_class(event: dict) -> CueClass:
    """A rule ``fire`` is ALERT; a ``suppress`` (or any other action) is CONTEXT."""
    return CueClass.ALERT if event.get("action") == "fire" else CueClass.CONTEXT


def _context_cue_class(_event: dict) -> CueClass:
    """``sense`` / ``intent`` / ``motion`` are always CONTEXT."""
    return CueClass.CONTEXT


#: One classifier per recognised line type, keyed exactly like
#: :data:`CUE_MAPPERS` — that dispatch table already knows each event's type,
#: so classification happens right where the mapper is chosen, not re-derived
#: later from whatever text the mapper happened to produce.
CUE_CLASSIFIERS: dict[str, Callable[[dict], CueClass]] = {
    "rule": _rule_cue_class,
    "sense": _context_cue_class,
    "intent": _context_cue_class,
    "motion": _context_cue_class,
}


def classify_runtime_event(event: dict) -> CueClass:
    """The :class:`CueClass` for one recognised runtime event.

    Looks up :data:`CUE_CLASSIFIERS` by the event's ``t``. A pure lookup —
    no I/O, no :mod:`reachy.senselog` side effect — because
    :func:`cues_for_runtime_event` already owns the recognition/drop
    observability; this function only ever runs on an event that mapper
    already accepted (see :func:`classified_cues_for_runtime_event`). An
    event whose type :data:`CUE_MAPPERS` does not recognise defaults to
    CONTEXT here rather than raising, matching that function's own
    fail-quiet posture for a mapper miss.
    """
    classifier = CUE_CLASSIFIERS.get(event.get("t"), _context_cue_class)
    return classifier(event)


def classified_cues_for_runtime_event(event: object) -> list[ClassifiedCue]:
    """Like :func:`cues_for_runtime_event`, but each cue carries its :class:`CueClass`.

    Delegates entirely to :func:`cues_for_runtime_event` for recognition,
    text and the senselog drop/stage side effects — this function adds
    nothing but the classification and produces no cue text of its own. All
    cues from one event share one class, because the class is a property of
    the runtime DECISION the event describes, not of the individual wording a
    mapper happened to produce for it.
    """
    if not isinstance(event, dict):
        return []
    texts = cues_for_runtime_event(event)
    if not texts:
        return []
    cue_class = classify_runtime_event(event)
    return [ClassifiedCue(text=text, cue_class=cue_class) for text in texts]


def classified_cues_for_line(line: str) -> list[ClassifiedCue]:
    """Parse one runtime-feed line into classified cues (blank lines yield none).

    The classified counterpart to :func:`cues_for_line`, sharing the same
    :func:`parse_runtime_line` parse step. Both intake routes
    (:func:`open_runtime_lines`'s bus branch and its feed-tail fallback) hand
    their lines to this SAME function, so a bus-delivered event and a
    feed-tailed event of identical content classify identically — the class
    depends only on the parsed event, never on which transport carried the
    line.
    """
    event = parse_runtime_line(line)
    if event is None:
        return []
    return classified_cues_for_runtime_event(event)


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
