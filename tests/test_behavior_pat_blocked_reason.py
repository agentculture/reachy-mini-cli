"""Tests for ``PatState.blocked_reason`` — named blocked causes (issue #168, task t3).

``PatState.availability == "blocked"`` used to conflate three different gates
(the stillness gate not yet re-earned, an ownership edge, an observation-clock
gap) plus a fourth path (a missing/malformed commanded pose) — a live
measurement of "N/N samples blocked" could not say WHICH gate was closing. This
module pins the four labels :mod:`reachy.behavior.pat_sense` now threads
through the gap machinery, the "latest cause wins" behavior chosen for a gap
that persists across ticks or across edges within one tick, that every
transition back to "available" clears the reason (never leaked via a stale
``replace()``), and that the additive field survives the export round-trip.

Two of the four causes (``"ownership"`` / ``"clock-gap"``) are set inside
``_apply_observation_edges``, which does not return early — the very next gate
``_process`` checks (the stillness gate, re-armed by the same edge) closes
again immediately and overwrites the reason with ``"stillness"`` before the
tick ends (see ``_blocked_edge``'s docstring). So those two labels are never
the FINAL reason a full ``driver(ctx)`` tick settles on; they are pinned by
calling ``_apply_observation_edges`` directly, the way the rest of this test
suite already inspects private driver state (``driver._stillness_blocked`` in
``tests/test_behavior_pat_sense.py``) rather than only through the public
``driver(ctx)`` surface.

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
    """The ownership edge itself, isolated at its own construction site.

    Driven directly through ``_apply_observation_edges`` (see the module
    docstring): a full tick's stillness re-arm would immediately overwrite this
    label with ``"stillness"``, so this pins the CAUSE the edge attaches at the
    moment it fires, independent of whatever gate closes next.
    """
    driver = PatSenseDriver(reader=_Reader(), still_hold_s=0.0, warmup_s=0.0)
    # Establish a baseline owner first (an edge only fires on a CHANGE).
    driver._apply_observation_edges(_ctx(now=T0, owner=BASE_OWNER), T0)
    assert driver.peek_state().blocked_reason is None  # no edge yet

    driver._apply_observation_edges(_ctx(now=T0 + DT, owner=GESTURE_OWNER), T0 + DT)

    state = driver.peek_state()
    assert state.availability == "blocked"
    assert state.blocked_reason == "ownership"


def test_clock_gap_blocked_reason_is_clock_gap() -> None:
    """The observation-clock-gap edge, isolated the same way as ownership above."""
    driver = PatSenseDriver(
        reader=_Reader(), still_hold_s=0.0, warmup_s=0.0, max_observation_gap_s=0.2
    )
    driver._apply_observation_edges(_ctx(now=T0), T0)  # establish the clock baseline
    assert driver.peek_state().blocked_reason is None

    # Same owner, but a jump far past max_observation_gap_s.
    driver._apply_observation_edges(_ctx(now=T0 + 10.0), T0 + 10.0)

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
