"""Tests for ``PatState.blocked_reason`` — named blocked causes (issue #168, task t3).

``PatState.availability == "blocked"`` used to conflate three different gates
(the stillness gate not yet re-earned, an ownership edge, an observation-clock
gap) plus a fourth path (a missing/malformed commanded pose) — a live
measurement of "N/N samples blocked" could not say WHICH gate was closing. This
module pins the four labels :mod:`reachy.behavior.pat_sense` now threads
through the gap machinery, that every transition back to "available" clears
the reason (never leaked via a stale ``replace()``), and that the additive
field survives the export round-trip.

**First cause wins within a tick (review t3).** Two of the four causes
(``"ownership"`` / ``"clock-gap"``) are set inside ``_apply_observation_edges``,
which does not return early, and both call ``_blocked_edge`` — which re-arms
the stillness gate before returning. So the very next gate ``_process`` checks
(the stillness gate) closes again immediately in the SAME tick and would try
to assign ``"stillness"`` right behind the edge's own reason. An earlier build
let that second assignment win (`"latest cause wins"`), which meant a full
``driver(ctx)`` tick could NEVER observe ``"ownership"`` or ``"clock-gap"`` on
public state — every edge tick settled on ``"stillness"`` instead, masking the
root cause a consumer most needs. ``_begin_gap`` now latches the reason once
per tick (reset in ``_process``), so the FIRST cause assigned in a tick wins
and later assignments in that same tick — including the stillness rearm's own
— keep it. The tests below therefore drive full ``driver(ctx)`` ticks (never
``_apply_observation_edges`` directly): each edge scenario asserts the edge's
own cause survives its own tick, and that the FOLLOWING tick — with no new
edge, while the gate is still re-earning its hold — legitimately reports
``"stillness"`` on its own merits.

Deterministic throughout: an injected fake reader, a hand-built
``TickContext``-shaped fake, and an explicit ``now`` per tick. No robot, SDK,
daemon, or network anywhere.
"""

from __future__ import annotations

import typing
from types import SimpleNamespace

from reachy.behavior.pat_sense import PatSenseDriver
from reachy.behavior.sense import PatAvailability, Sense
from reachy.export.runtime import SenseSnapshotDriver

BASE_OWNER = "feel-alive-1"
GESTURE_OWNER = "thoughtful-3"
T0 = 10.0
DT = 0.1


# --------------------------------------------------------------------------- #
# Fakes / helpers (mirrors tests/test_behavior_pat_sense.py)                  #
# --------------------------------------------------------------------------- #


def _head(pitch: float = 0.0, yaw: float = 0.0) -> dict:
    return {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": pitch, "yaw": yaw}


def _ctx(*, now: float, owner: str | None = BASE_OWNER, pitch: float = 0.0, yaw: float = 0.0):
    """A minimal ``TickContext``-shaped fake: only the fields the driver reads."""
    return SimpleNamespace(
        now=now,
        tick=int(now * 100),
        ownership={"head": owner, "antennas": owner, "body_yaw": owner},
        pose={"head": _head(pitch, yaw), "antennas": (0.0, 0.0), "body_yaw": 0.0},
    )


class _Reader:
    """A fake actual-pose reader: ``__call__`` returns whatever ``value`` holds."""

    def __init__(self, value: tuple[float, float] | None = (0.0, 0.0)) -> None:
        self.value = value

    def __call__(self) -> tuple[float, float] | None:
        return self.value


def _drive(driver: PatSenseDriver, reader: _Reader, actual, now: float, **ctx_kw) -> None:
    reader.value = actual
    driver(_ctx(now=now, **ctx_kw))


# --------------------------------------------------------------------------- #
# One label per cause                                                        #
# --------------------------------------------------------------------------- #


def test_stillness_gate_blocked_reason_is_stillness() -> None:
    """A moving commanded pose closes the stillness gate every tick (#80)."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, still_hold_s=0.5, warmup_s=0.0)

    for i in range(5):
        _drive(driver, reader, (0.0, 0.0), T0 + i * DT, pitch=float(i))
        state = driver.peek_state()
        assert state.availability == "blocked"
        assert state.blocked_reason == "stillness"


def test_missing_commanded_pose_blocked_reason_is_no_command() -> None:
    """A tick whose ``ctx.pose`` is missing/malformed never reaches the reader."""
    driver = PatSenseDriver(reader=_Reader(), still_hold_s=0.0, warmup_s=0.0)

    driver(SimpleNamespace(now=T0, ownership={"head": BASE_OWNER}))  # no .pose at all

    state = driver.peek_state()
    assert state.availability == "blocked"
    assert state.blocked_reason == "no-command"


def test_reader_returning_none_is_unavailable_with_no_reason() -> None:
    """The reader-``None`` path is unambiguous on its own — it names no cause."""
    reader = _Reader(value=None)
    driver = PatSenseDriver(reader=reader, still_hold_s=0.0, warmup_s=0.0)

    driver(_ctx(now=T0))

    state = driver.peek_state()
    assert state.availability == "unavailable"
    assert state.blocked_reason is None


def test_ownership_edge_blocked_reason_is_ownership() -> None:
    """A full tick where ownership changes reports "ownership", not "stillness".

    The stillness gate is ENABLED (``still_hold_s=0.2``), so the edge's own
    ``_rearm_stillness_hold()`` call makes ``_stillness_open`` also try to
    close the gate in this SAME tick — the exact scenario that used to let
    ``"stillness"`` mask the edge. First-cause-wins keeps "ownership".

    The FOLLOWING tick then reports "stillness" on its own merits: no new
    edge fires, but the gate has not yet re-earned its ``still_hold_s`` quiet
    window, so it is still the tick's own cause of being blocked.
    """
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, still_hold_s=0.2, warmup_s=0.0)

    # Warm the stillness gate open on a held-still commanded pose.
    t = T0
    for _ in range(10):  # 10 * 0.05 = 0.5 s, comfortably past still_hold_s=0.2
        _drive(driver, reader, (0.0, 0.0), t, owner=BASE_OWNER)
        t += 0.05
    assert driver.peek_state().availability == "available"

    # The tick where ownership actually changes: the edge AND the stillness
    # rearm it triggers both fire in this one tick.
    t += 0.05
    _drive(driver, reader, (0.0, 0.0), t, owner=GESTURE_OWNER)
    edge_state = driver.peek_state()
    assert edge_state.availability == "blocked"
    assert edge_state.blocked_reason == "ownership"

    # The very next tick: no new edge, but the re-armed gate has not yet
    # re-earned its hold -> its own, uncontested cause.
    t += 0.05
    _drive(driver, reader, (0.0, 0.0), t, owner=GESTURE_OWNER)
    next_state = driver.peek_state()
    assert next_state.availability == "blocked"
    assert next_state.blocked_reason == "stillness"


def test_clock_gap_blocked_reason_is_clock_gap() -> None:
    """The same full-tick shape as the ownership test above, for a clock jump."""
    reader = _Reader()
    driver = PatSenseDriver(
        reader=reader, still_hold_s=0.2, warmup_s=0.0, max_observation_gap_s=0.2
    )

    t = T0
    for _ in range(10):
        _drive(driver, reader, (0.0, 0.0), t)
        t += 0.05
    assert driver.peek_state().availability == "available"

    # A clock jump far past max_observation_gap_s, and its own stillness
    # rearm, both land in this one tick.
    t_gap = t + 10.0
    _drive(driver, reader, (0.0, 0.0), t_gap)
    edge_state = driver.peek_state()
    assert edge_state.availability == "blocked"
    assert edge_state.blocked_reason == "clock-gap"

    # The very next tick: no new gap, but the gate is still re-earning its hold.
    t_gap += 0.05
    _drive(driver, reader, (0.0, 0.0), t_gap)
    next_state = driver.peek_state()
    assert next_state.availability == "blocked"
    assert next_state.blocked_reason == "stillness"


def test_simultaneous_edges_the_first_checked_cause_wins() -> None:
    """A clock gap AND an ownership change landing in one tick: order decides.

    ``_apply_observation_edges`` checks the clock gap before ownership, so
    when both edges fire in the same tick, "clock-gap" — not "ownership" and
    not the stillness rearm either edge triggers — is what a consumer reads.
    """
    reader = _Reader()
    driver = PatSenseDriver(
        reader=reader, still_hold_s=0.2, warmup_s=0.0, max_observation_gap_s=0.2
    )

    t = T0
    for _ in range(10):
        _drive(driver, reader, (0.0, 0.0), t, owner=BASE_OWNER)
        t += 0.05
    assert driver.peek_state().availability == "available"

    t_gap = t + 10.0
    _drive(driver, reader, (0.0, 0.0), t_gap, owner=GESTURE_OWNER)

    state = driver.peek_state()
    assert state.availability == "blocked"
    assert state.blocked_reason == "clock-gap"


def test_available_state_has_no_blocked_reason() -> None:
    """An ordinary settled-and-observed tick clears any reason."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, still_hold_s=0.0, warmup_s=0.0)

    _drive(driver, reader, (0.0, 0.0), T0)

    state = driver.peek_state()
    assert state.availability == "available"
    assert state.blocked_reason is None


# --------------------------------------------------------------------------- #
# No stale reason survives a blocked -> available -> blocked sequence         #
# --------------------------------------------------------------------------- #


def test_blocked_available_blocked_never_leaks_a_stale_reason() -> None:
    """no-command -> available -> stillness: each stage's reason is its own.

    A naive ``replace()`` that forgets to clear ``blocked_reason`` on the
    recovery edge would let "no-command" survive into the later "available"
    and "stillness" stages; this pins that it does not.
    """
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, still_hold_s=0.2, warmup_s=0.0)

    # Stage 1: no commanded pose at all.
    driver(SimpleNamespace(now=T0, ownership={"head": BASE_OWNER}))
    stage1 = driver.peek_state()
    assert stage1.availability == "blocked"
    assert stage1.blocked_reason == "no-command"

    # Stage 2: a valid, held-still commanded pose long enough to reopen the
    # stillness gate the no-command path also armed, and reach "available".
    t = T0 + DT
    for _ in range(20):  # 20 * 0.05 = 1.0 s, comfortably past still_hold_s=0.2
        _drive(driver, reader, (0.0, 0.0), t)
        t += 0.05
    stage2 = driver.peek_state()
    assert stage2.availability == "available"
    assert stage2.blocked_reason is None

    # Stage 3: the commanded pose moves again -> a fresh, DIFFERENT cause.
    _drive(driver, reader, (0.0, 0.0), t, pitch=5.0)
    stage3 = driver.peek_state()
    assert stage3.availability == "blocked"
    assert stage3.blocked_reason == "stillness"  # not the stale "no-command"


# --------------------------------------------------------------------------- #
# The Literal contract stays byte-identical (no consumer keyed on it changes) #
# --------------------------------------------------------------------------- #


def test_pat_availability_literal_is_unchanged() -> None:
    assert typing.get_args(PatAvailability) == ("available", "blocked", "unavailable")


# --------------------------------------------------------------------------- #
# Export round-trip                                                          #
# --------------------------------------------------------------------------- #


class _ExportCtx:
    """A minimal ``TickBus``-shaped ``ctx`` exposing what ``SenseSnapshotDriver`` reads."""

    def __init__(self, sense: Sense, *, now: float = 0.0, tick: int = 0) -> None:
        self.sense = sense
        self.now = now
        self.tick = tick
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


def test_exported_snapshot_carries_blocked_reason() -> None:
    """``SenseSnapshotDriver`` renders ``blocked_reason`` beside ``availability``."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, still_hold_s=0.5, warmup_s=0.0)
    _drive(driver, reader, (0.0, 0.0), T0, pitch=1.0)  # a moving pose -> stillness-blocked
    assert driver.peek_state().blocked_reason == "stillness"

    sense = Sense(pat_state=driver.as_state_provider()())
    snapshot_driver = SenseSnapshotDriver()
    ctx = _ExportCtx(sense, now=T0)

    snapshot_driver(ctx)

    assert len(ctx.events) == 1
    exported = ctx.events[0]["pat_state"]
    assert exported["availability"] == "blocked"
    assert exported["blocked_reason"] == "stillness"


def test_exported_snapshot_reason_is_none_when_available() -> None:
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, still_hold_s=0.0, warmup_s=0.0)
    _drive(driver, reader, (0.0, 0.0), T0)
    assert driver.peek_state().availability == "available"

    sense = Sense(pat_state=driver.as_state_provider()())
    snapshot_driver = SenseSnapshotDriver()
    ctx = _ExportCtx(sense, now=T0)

    snapshot_driver(ctx)

    exported = ctx.events[0]["pat_state"]
    assert exported["availability"] == "available"
    assert exported["blocked_reason"] is None
