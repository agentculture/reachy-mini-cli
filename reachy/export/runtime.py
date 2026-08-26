"""Event model for the behavior engine's OWN runtime-event JSONL export feed.

This is a **separate wire contract** from the cognition feed
(:mod:`reachy.export.events` — ``thinking``/``message``/``emotion``, produced by
``agent attach --export -``). Decision c27 (the
``symbolic-runtime-70`` spec): an attached agent publishes its OWN cognition feed
through that existing family; THIS feed is the deterministic runtime's — the
50 Hz :mod:`reachy.behavior.engine` loop and its rule evaluator
(:mod:`reachy.behavior.rule_engine`) — and carries ONLY runtime events:
perception snapshots, rule decisions, sustained intents, and motion admissions.
No event type defined here can represent an LLM call — that is what makes a
rules-driven run's zero-token property verifiable straight from the feed: every
line's ``t`` is a member of :data:`RUNTIME_BLOCKS`, and that set has no overlap
with :data:`reachy.export.blocks.BLOCKS` (see
``test_runtime_blocks_disjoint_from_cognition_blocks``).

Four block types, each ``{t, ts, tick, ...payload}``:

- ``"sense"`` — a perception snapshot, published when it changes (see
  :class:`SenseSnapshotDriver`). Its legacy ``pat`` value remains
  ``[touch_type, level]``; event-stable interaction details travel in the
  additive parallel ``pat_state`` object.
- ``"rule"`` — a rule fire/suppress decision, a passthrough of the ``rule.fire`` /
  ``rule.suppress`` events :mod:`reachy.behavior.rule_engine` already publishes
  through ``ctx.emit`` (its module docstring is the source-of-truth for that raw
  shape: ``{type, rule, kind, field, op, reason, ts, tick}`` plus
  ``behavior``/``disable`` on a fire).
- ``"intent"`` — a sustained symbolic goal flowing through the intent-tools
  spool; the live producer is :class:`reachy.behavior.intents.IntentDriver`'s
  ``intent.applied``/``intent.blocked`` status emissions.
- ``"motion"`` — a behavior admission/eviction, a goto, or a face-lock
  lifecycle event; the goto lane's ``goto.admitted``/``goto.done``/
  ``goto.cancelled`` lifecycle maps here as ``action="goto"`` with
  ``detail.phase``, and the face lock's ``motion.face-lost`` /
  ``motion.lock-released`` map here verbatim (``detail.absent_s`` and
  ``detail.reason`` respectively).

This module never imports :mod:`reachy.behavior.rule_engine` (or any
``reachy.behavior`` module) — it only *interprets* the documented ``type``
string convention of the raw event dicts a tick driver publishes via
``ctx.emit``, so ``reachy/export/`` stays independent of the engine package it
describes; the composition site (``reachy/cli/_commands/behavior.py``) is what
imports both and wires them together.

Public API
----------
:class:`SenseEvent`, :class:`RuleEvent`, :class:`IntentEvent`, :class:`MotionEvent`
    Frozen dataclasses, one per block type.
:data:`RuntimeEvent`
    Union alias of the four.
:data:`RUNTIME_BLOCKS`
    The four block-type strings, in declaration order.
:func:`runtime_to_jsonl`
    Serialize any :data:`RuntimeEvent` to a compact JSON line (mirrors
    :func:`reachy.export.events.to_jsonl`'s shape/contract).
:func:`parse_runtime_blocks`
    ``--export-blocks`` CSV parser for this feed (mirrors
    :func:`reachy.export.blocks.parse_blocks`).
:func:`to_runtime_event`
    Map a raw ``ctx.emit`` dict to a :data:`RuntimeEvent`, or ``None`` for an
    unrecognised shape (forward-compatible; never raises).
:class:`RuntimeConsumer`
    A :class:`~reachy.behavior.rule_engine.TickBus`-shaped event consumer
    (``consumer(event: dict) -> None``) adapting raw events into
    :data:`RuntimeEvent` objects and forwarding them to an injected sink (e.g. a
    :class:`~reachy.export.exporter.JsonlExporter`).
:class:`SenseSnapshotDriver`
    A :class:`~reachy.behavior.rule_engine.TickBus`-shaped per-tick driver
    (``driver(ctx) -> None``) publishing a ``"sense"`` event whenever
    ``ctx.sense`` changes.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import ClassVar, Union

from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.export.blocks import Selection

# ---------------------------------------------------------------------------
# The raw ``type`` string convention published via ctx.emit (documented, not
# imported — see the module docstring for why this stays a string contract).
# ---------------------------------------------------------------------------

_RAW_TYPE_SENSE = "sense"
_RAW_TYPE_RULE_FIRE = "rule.fire"
_RAW_TYPE_RULE_SUPPRESS = "rule.suppress"
_RAW_INTENT_ACTIONS = frozenset({"declare", "update", "clear"})
#: The IntentDriver's live status emissions (``intent.applied`` / ``intent.blocked``).
_RAW_INTENT_STATUS_ACTIONS = frozenset({"applied", "blocked"})
#: ``face-lost`` / ``lock-released`` are the face lock's lifecycle emissions
#: (:mod:`reachy.behavior.face_lock`): a lock reports a face it has stopped
#: seeing, and names the ``detail.reason`` that finally ended it. Registered
#: here because registration IS the gate — an action absent from this table
#: is dropped from the stdout feed and from the bus alike.
_RAW_MOTION_ACTIONS = frozenset({"admit", "evict", "goto", "face-lost", "lock-released"})
#: The GotoLane's per-goto lifecycle emissions (``goto.admitted`` / ``goto.done`` /
#: ``goto.cancelled``) — surfaced as ``motion`` blocks with ``action="goto"``.
_RAW_GOTO_PHASES = frozenset({"admitted", "done", "cancelled"})

_PAT_AVAILABILITIES = frozenset({"available", "blocked", "unavailable"})
_PAT_TOUCH_TYPES = frozenset({"scratch", "side_pat"})
_PAT_LEVELS = frozenset({"level1", "level2"})
_PAT_PHASES = frozenset(
    {"idle", "receptive", "contentment", "warning", "released", "enough", "cooldown"}
)
#: Issue #168 — the named causes a "blocked" pat reading may carry. Always
#: normalized to ``None`` when ``availability`` is not exactly ``"blocked"``,
#: mirroring :class:`reachy.behavior.sense.PatState`'s own contract.
_PAT_BLOCKED_REASONS = frozenset({"stillness", "ownership", "clock-gap", "no-command"})

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SenseEvent:
    """A perception snapshot published when it differs from the last one.

    Mirrors :class:`reachy.behavior.sense.Sense`'s fields (``doa``/``speech``/
    ``rms``/``pat``/``pat_state``/``face``/``frame_available``); ``pat`` stays
    the legacy 2-element list (``[kind, level]``) or ``null``. ``pat_state`` is
    an additive parallel object and is omitted when parsing an older raw shape
    that did not carry it. Unknown object keys are ignored for forward
    compatibility.
    """

    t: ClassVar[str] = "sense"

    doa: float | None
    speech: bool
    rms: float | None
    pat: list | None
    face: str | None
    frame_available: bool
    ts: float = 0.0
    tick: int = 0
    pat_state: dict | None = None


@dataclass(frozen=True)
class RuleEvent:
    """A rule engine decision — a passthrough of ``rule.fire`` / ``rule.suppress``.

    ``action`` is ``"fire"`` or ``"suppress"``. ``behavior`` is the admitted
    library behavior name on a react fire (``None`` otherwise); ``disable`` is
    the list of evicted behavior names on an inhibit fire (empty otherwise).
    """

    t: ClassVar[str] = "rule"

    action: str
    rule: str
    kind: str
    field: str
    op: str
    reason: str
    behavior: str | None = None
    disable: list = field(default_factory=list)
    ts: float = 0.0
    tick: int = 0


@dataclass(frozen=True)
class IntentEvent:
    """A sustained symbolic goal declared/updated/cleared through the intent tools.

    ``action`` is ``"declare"`` / ``"update"`` / ``"clear"``; ``payload`` carries
    whatever declarative data the intent tool attached (e.g. mode/params).
    """

    t: ClassVar[str] = "intent"

    action: str
    name: str
    payload: dict = field(default_factory=dict)
    ts: float = 0.0
    tick: int = 0


@dataclass(frozen=True)
class MotionEvent:
    """A behavior admission/eviction, goto, or lock lifecycle event.

    The engine's active-set churn. ``action`` is ``"admit"`` / ``"evict"`` /
    ``"goto"`` / ``"face-lost"`` / ``"lock-released"``; ``channels`` names the
    claimed/released channels; ``detail`` carries action-specific extras (a
    goto's target pose, a face-lost's ``absent_s``, a release's ``reason``).
    """

    t: ClassVar[str] = "motion"

    action: str
    behavior: str | None = None
    channels: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    ts: float = 0.0
    tick: int = 0


#: Union type alias for use in type annotations and ``isinstance`` checks.
RuntimeEvent = Union[SenseEvent, RuleEvent, IntentEvent, MotionEvent]

#: Canonical exportable block-type strings for this feed, in declaration order.
RUNTIME_BLOCKS: tuple[str, ...] = ("sense", "rule", "intent", "motion")

_VALID = frozenset(RUNTIME_BLOCKS)
_VALID_HINT = ", ".join(RUNTIME_BLOCKS)


# ---------------------------------------------------------------------------
# JSONL serializer
# ---------------------------------------------------------------------------


def runtime_to_jsonl(event: RuntimeEvent) -> str:
    """Serialize a :data:`RuntimeEvent` to a compact single-line JSON string.

    Mirrors :func:`reachy.export.events.to_jsonl`'s contract exactly: ``t`` and
    ``ts`` come first, no trailing newline, ``ensure_ascii=False``, compact
    separators.
    """
    if isinstance(event, SenseEvent):
        payload: dict = {
            "t": event.t,
            "ts": event.ts,
            "tick": event.tick,
            "doa": event.doa,
            "speech": event.speech,
            "rms": event.rms,
            "pat": event.pat,
            "face": event.face,
            "frame_available": event.frame_available,
        }
        pat_state = _pat_state_payload(event.pat_state)
        if pat_state is not None:
            payload["pat_state"] = pat_state
    elif isinstance(event, RuleEvent):
        payload = {
            "t": event.t,
            "ts": event.ts,
            "tick": event.tick,
            "action": event.action,
            "rule": event.rule,
            "kind": event.kind,
            "field": event.field,
            "op": event.op,
            "reason": event.reason,
            "behavior": event.behavior,
            "disable": list(event.disable),
        }
    elif isinstance(event, IntentEvent):
        payload = {
            "t": event.t,
            "ts": event.ts,
            "tick": event.tick,
            "action": event.action,
            "name": event.name,
            "payload": dict(event.payload),
        }
    else:  # MotionEvent
        payload = {
            "t": event.t,
            "ts": event.ts,
            "tick": event.tick,
            "action": event.action,
            "behavior": event.behavior,
            "channels": list(event.channels),
            "detail": dict(event.detail),
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# --export-blocks parser
# ---------------------------------------------------------------------------


def parse_runtime_blocks(csv: str) -> Selection:
    """Parse a comma-separated list of runtime block-type names into a :class:`Selection`.

    Mirrors :func:`reachy.export.blocks.parse_blocks` exactly, validated against
    :data:`RUNTIME_BLOCKS` instead of the cognition feed's block set.
    """
    tokens = [t.strip() for t in csv.split(",")]
    tokens = [t for t in tokens if t]

    if not tokens:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--export-blocks requires at least one block type",
            remediation=f"valid block types: {_VALID_HINT}",
        )

    unknown = [t for t in tokens if t not in _VALID]
    if unknown:
        bad = ", ".join(repr(u) for u in unknown)
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown block type(s): {bad}",
            remediation=f"valid block types: {_VALID_HINT}",
        )

    return Selection(tokens)


# ---------------------------------------------------------------------------
# Raw ctx.emit dict -> RuntimeEvent mapping
# ---------------------------------------------------------------------------


def _rule_event(event: dict, *, action: str) -> RuleEvent:
    disable = event.get("disable") or []
    return RuleEvent(
        action=action,
        rule=str(event.get("rule", "")),
        kind=str(event.get("kind", "")),
        field=str(event.get("field", "")),
        op=str(event.get("op", "")),
        reason=str(event.get("reason", "")),
        behavior=event.get("behavior"),
        disable=list(disable),
        ts=_safe_float(event.get("ts")),
        tick=_safe_int(event.get("tick")),
    )


def _optional_finite_float(value: object) -> float | None:
    """Return a finite float or ``None`` for malformed wire values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _vocab_str(value: object, vocabulary, default: str | None) -> str | None:
    """*value* when it is a member of *vocabulary*, else *default*.

    The wire's enum discipline in one place: every closed string field of the
    pat-state payload degrades to its conservative default rather than letting
    an unknown or malformed value poison the legacy sense event.
    """
    return value if isinstance(value, str) and value in vocabulary else default


def _pat_field_reader(value: object):
    """A never-raising field reader over a dict or PatState-like value.

    Returns ``None`` for a value that is neither (the payload is dropped);
    production values are duck-typed while raw runtime events use dicts, so
    the export layer stays independent of :mod:`reachy.behavior`.
    """
    if isinstance(value, dict):
        raw_read = value.get
    elif hasattr(value, "availability"):

        def raw_read(name, default=None):
            return getattr(value, name, default)

    else:
        return None

    def read(name, default=None):
        try:
            return raw_read(name, default)
        except Exception:  # one bad field must not drop legacy sense
            return default

    return read


def _pat_state_payload(value: object) -> dict | None:
    """Normalize a PatState-like value to its stable additive wire object.

    Unknown keys are ignored and malformed individual fields fall back
    conservatively (see :func:`_vocab_str` / :func:`_pat_field_reader`), so a
    future or damaged pat-state value cannot poison the legacy sense event.
    """
    if value is None:
        return None
    read = _pat_field_reader(value)
    if read is None:
        return None
    availability = _vocab_str(
        read("availability", "unavailable"), _PAT_AVAILABILITIES, "unavailable"
    )
    # Outside "blocked" a reason is meaningless (the field is always None
    # there, per PatState's own contract); a malformed reason inside
    # "blocked" degrades to None rather than poisoning the whole payload.
    blocked_reason = None
    if availability == "blocked":
        blocked_reason = _vocab_str(read("blocked_reason"), _PAT_BLOCKED_REASONS, None)
    contact = read("contact", False)

    return {
        "availability": availability,
        "contact": contact if isinstance(contact, bool) else False,
        "touch_type": _vocab_str(read("touch_type"), _PAT_TOUCH_TYPES, None),
        "level": _vocab_str(read("level"), _PAT_LEVELS, None),
        "yaw_deg": _optional_finite_float(read("yaw_deg")),
        "phase": _vocab_str(read("phase", "idle"), _PAT_PHASES, "idle"),
        "phase_started_at": _optional_finite_float(read("phase_started_at")),
        "last_press_at": _optional_finite_float(read("last_press_at")),
        "blocked_reason": blocked_reason,
    }


def _sense_event(event: dict) -> SenseEvent:
    pat = event.get("pat")
    return SenseEvent(
        doa=event.get("doa"),
        speech=bool(event.get("speech", False)),
        rms=event.get("rms"),
        pat=list(pat) if pat is not None else None,
        face=event.get("face"),
        frame_available=bool(event.get("frame_available", False)),
        ts=_safe_float(event.get("ts")),
        tick=_safe_int(event.get("tick")),
        pat_state=_pat_state_payload(event.get("pat_state")),
    )


def _intent_event(event: dict, *, action: str) -> IntentEvent:
    return IntentEvent(
        action=action,
        name=str(event.get("name", "")),
        payload=dict(event.get("payload") or {}),
        ts=_safe_float(event.get("ts")),
        tick=_safe_int(event.get("tick")),
    )


def _safe_float(value: object, default: float = 0.0) -> float:
    """Coerce to float, or *default* — ``to_runtime_event`` promises never to raise."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _safe_int(value: object, default: int = 0) -> int:
    """Coerce to int, or *default* — ``to_runtime_event`` promises never to raise."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _intent_status_event(event: dict, *, action: str) -> IntentEvent:
    """An ``intent.applied``/``intent.blocked`` status emission from the IntentDriver."""
    payload = {k: event[k] for k in ("kind", "cmd_id", "result", "reason", "goal") if event.get(k)}
    return IntentEvent(
        action=action,
        name=str(event.get("kind") or event.get("goal") or ""),
        payload=payload,
        ts=_safe_float(event.get("ts")),
        tick=_safe_int(event.get("tick")),
    )


def _goto_event(event: dict, *, phase: str) -> MotionEvent:
    """A GotoLane lifecycle emission, surfaced as a ``motion`` block."""
    detail = {"phase": phase}
    for k in ("id", "reason", "channel", "owner", "duration"):
        if event.get(k) is not None:
            detail[k] = event[k]
    return MotionEvent(
        action="goto",
        behavior=event.get("label") or event.get("id"),
        channels=list(event.get("channels") or []),
        detail=detail,
        ts=_safe_float(event.get("ts")),
        tick=_safe_int(event.get("tick")),
    )


def _motion_event(event: dict, *, action: str) -> MotionEvent:
    return MotionEvent(
        action=action,
        behavior=event.get("behavior"),
        channels=list(event.get("channels") or []),
        detail=dict(event.get("detail") or {}),
        ts=_safe_float(event.get("ts")),
        tick=_safe_int(event.get("tick")),
    )


def to_runtime_event(event: dict) -> RuntimeEvent | None:
    """Map a raw ``ctx.emit`` dict to a :data:`RuntimeEvent`, or ``None``.

    Dispatches on the ``type`` string convention documented in the module
    docstring. An unrecognised ``type`` (a future producer this version does not
    know about, or an unrelated event some other driver publishes on the same
    seam) returns ``None`` — forward-compatible, never raises so one malformed or
    unexpected event can never break the ``ctx.emit`` fan-out.
    """
    kind = event.get("type")
    if not isinstance(kind, str):
        return None
    if kind == _RAW_TYPE_RULE_FIRE:
        return _rule_event(event, action="fire")
    if kind == _RAW_TYPE_RULE_SUPPRESS:
        return _rule_event(event, action="suppress")
    if kind == _RAW_TYPE_SENSE:
        return _sense_event(event)
    if kind.startswith("intent."):
        action = kind.split(".", 1)[1]
        if action in _RAW_INTENT_ACTIONS:
            return _intent_event(event, action=action)
        if action in _RAW_INTENT_STATUS_ACTIONS:
            return _intent_status_event(event, action=action)
        return None
    if kind.startswith("motion."):
        action = kind.split(".", 1)[1]
        if action in _RAW_MOTION_ACTIONS:
            return _motion_event(event, action=action)
        return None
    if kind.startswith("goto."):
        phase = kind.split(".", 1)[1]
        if phase in _RAW_GOTO_PHASES:
            return _goto_event(event, phase=phase)
        return None
    return None


# ---------------------------------------------------------------------------
# TickBus consumer — adapts raw dicts into RuntimeEvent objects
# ---------------------------------------------------------------------------


class RuntimeConsumer:
    """Adapt raw ``ctx.emit`` dicts into :data:`RuntimeEvent` objects for a sink.

    Usable directly as a :class:`reachy.behavior.rule_engine.TickBus` consumer
    (``consumer(event: dict) -> None``). *sink* is anything exposing
    ``emit(event) -> None`` — typically a
    :class:`~reachy.export.exporter.JsonlExporter` constructed with
    ``serialize=runtime_to_jsonl``. Unrecognised events are dropped silently
    (via :func:`to_runtime_event`); a malformed event dict is swallowed
    defensively so one bad upstream publish can never break the fan-out (the
    engine's :class:`~reachy.behavior.rule_engine.TickBus` already isolates a
    raising consumer, but this adds the same guarantee for direct/non-bus use).
    """

    def __init__(self, sink) -> None:
        self._sink = sink

    def __call__(self, event: dict) -> None:
        try:
            mapped = to_runtime_event(event)
        except Exception:  # a malformed event must never break the fan-out
            return
        if mapped is not None:
            self._sink.emit(mapped)


# ---------------------------------------------------------------------------
# TickBus driver — publishes a "sense" event when perception changes
# ---------------------------------------------------------------------------


class SenseSnapshotDriver:
    """Publish a ``"sense"`` runtime event whenever ``ctx.sense`` changes.

    Usable directly as a :class:`reachy.behavior.rule_engine.TickBus` driver
    (``driver(ctx) -> None``). Compares each tick's ``ctx.sense`` against the
    last-published snapshot (frozen-dataclass equality) and emits only on a
    change — always on the first tick, to establish a baseline — so a 50 Hz loop
    does not flood the feed with an identical reading every 20 ms. Never raises:
    ``ctx.sense`` is a plain dataclass read, and ``ctx.emit`` is the engine's own
    fault-isolated fan-out.
    """

    def __init__(self) -> None:
        self._last = None
        self._started = False

    def __call__(self, ctx) -> None:
        sense = ctx.sense
        if self._started and sense == self._last:
            return
        self._started = True
        self._last = sense
        pat = getattr(sense, "pat_event", None)
        pat_state = _pat_state_payload(getattr(sense, "pat_state", None))
        ctx.emit(
            {
                "type": _RAW_TYPE_SENSE,
                "doa": getattr(sense, "doa_angle", None),
                "speech": bool(getattr(sense, "speech_detected", False)),
                "rms": getattr(sense, "rms", None),
                "pat": list(pat) if pat is not None else None,
                "pat_state": pat_state,
                "face": getattr(sense, "face", None),
                "frame_available": bool(getattr(sense, "frame_available", False)),
                "ts": ctx.now,
                "tick": ctx.tick,
            }
        )
