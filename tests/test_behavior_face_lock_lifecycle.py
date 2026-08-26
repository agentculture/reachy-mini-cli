"""The face lock's LIFECYCLE: it is reported when lost, and it cannot outlive its mind.

t4 gave the lock its state and its two kind handlers. t5 gives it the three
endings a standing, indefinite claim on the head needs — because a lock that
only ``release_face`` can end is a lock that survives the mind that took it:

1. **Face lost is REPORTED, never fatal.** While locked and the face has been
   absent/stale for ``face_lost_after_s`` (default 3 s), the driver emits
   exactly ONE ``motion.face-lost`` (carrying ``absent_s``) — not one per tick —
   and RE-ARMS when the face comes back. The lock persists throughout: vision
   drops frames, and a person who steps out of frame for four seconds has not
   asked to be unlocked.
2. **A lock cannot outlive its mind.** ``mind_online()`` returning ``False``
   continuously for ``mind_offline_grace_s`` (default 10 s) releases the lock
   with reason ``mind-offline``. ``None`` is UNKNOWN and never releases —
   the conservative default, because the runtime today has no live liveness
   source and "I cannot tell" must not look like "the mind is gone".
3. **A lock cannot be held forever.** ``max_hold_s`` (default 30 min) releases
   with reason ``max-hold``. An explicit ``release_face`` carries ``requested``.

Every release path is t4's release: the behavior is evicted, the inhibitions are
restored under the LATER-WINS rule, and ONE ``motion.lock-released`` is emitted —
now carrying the ``reason`` that ended it.

Deterministic throughout: the clock is injected tick by tick, and the ``ctx`` is
the same duck-typed recorder ``tests/test_behavior_face_lock.py`` uses. No
robot, daemon, network, clock-sleeping or LLM anywhere in this file.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

import pytest

from reachy.behavior.face_lock import (
    EVENT_FACE_LOST,
    EVENT_LOCK_RELEASED,
    FACE_LOCK_BEHAVIOR,
    FACE_LOST_ACTION,
    LOCK_FACE,
    LOCK_INHIBITS,
    LOCK_RELEASED_ACTION,
    MAX_FACE_AGE_S,
    REASON_EVICTED,
    REASON_MAX_HOLD,
    REASON_MIND_OFFLINE,
    REASON_REQUESTED,
    RELEASE_FACE,
    FaceLockDriver,
)
from reachy.behavior.intents import (
    DECLARE_GOAL,
    RUN_BEHAVIOR,
    SET_INHIBITION,
    IntentDriver,
)
from reachy.behavior.sense import EMPTY_SENSE, Sense
from reachy.cli._commands import behavior as behavior_cmd
from reachy.cli._errors import CliError
from reachy.export.runtime import MotionEvent, to_runtime_event

# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingCtx:
    """A duck-typed TickContext (the sibling file's, verbatim in shape)."""

    now: float = 0.0
    tick: int = 0
    sense: Sense = EMPTY_SENSE
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)
    events: list = field(default_factory=list)
    _active: set = field(default_factory=set)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        self._active.add(behavior.name)
        return {"ok": True, "op": "add", "id": behavior.id, "name": behavior.name}

    def evict(self, target: str) -> dict:
        self.evicts.append(target)
        self._active.discard(FACE_LOCK_BEHAVIOR)
        return {"ok": True, "op": "stop", "target": target}

    def active_names(self) -> set:
        return set(self._active)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))


def _face(cx: float = 0.5, cy: float = 0.5, size: float = 0.2, age: float = 0.0) -> Sense:
    return Sense(face_bbox=(cx - size / 2.0, cy - size / 2.0, size, size), face_age_s=age)


def _wire(intents: IntentDriver | None = None, **kwargs):
    """Compose driver + registry the way ``_compose_run_seam`` does."""
    if intents is None:
        intents = IntentDriver()
    driver = FaceLockDriver(
        inhibitions_getter=lambda: intents.inhibitions,
        inhibitions_setter=intents.set_inhibitions,
        **kwargs,
    )
    driver.register_into(intents.registry)
    intents.inhibition_observer = driver.notice_inhibition_replaced
    return driver, intents.registry, intents


def _lock(registry, ctx) -> dict:
    return registry.dispatch({"op": LOCK_FACE}, ctx)


def _tick(driver, ctx, now: float, sense: Sense | None = None) -> None:
    """One engine tick at *now*, mirroring ``IntentDriver.on_tick``'s seam shape."""
    if sense is not None:
        ctx.sense = sense
    ctx.now = now
    ctx.tick += 1
    driver.on_tick(ctx, now)


def _events(ctx, type_: str) -> list[dict]:
    return [e for e in ctx.events if e.get("type") == type_]


# --------------------------------------------------------------------------- #
# 1. Face lost is REPORTED once, re-armed on return, and never releases       #
# --------------------------------------------------------------------------- #


def test_a_face_absent_past_the_threshold_emits_exactly_one_face_lost() -> None:
    driver, registry, _ = _wire(face_lost_after_s=3.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)

    for i in range(1, 300):  # 6 s at 50 Hz with no face at all
        _tick(driver, ctx, now=i * 0.02, sense=EMPTY_SENSE)

    lost = _events(ctx, EVENT_FACE_LOST)
    assert len(lost) == 1
    assert lost[0]["detail"]["absent_s"] >= 3.0
    assert lost[0]["behavior"] == FACE_LOCK_BEHAVIOR
    # REPORTED, not fatal: the lock is still held and nothing was evicted.
    assert driver.locked is True
    assert ctx.evicts == []
    assert _events(ctx, EVENT_LOCK_RELEASED) == []


def test_a_merely_stale_face_counts_as_absent_for_the_face_lost_timer() -> None:
    driver, registry, _ = _wire(face_lost_after_s=3.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)

    stale = _face(age=MAX_FACE_AGE_S + 1.0)
    for i in range(1, 300):
        _tick(driver, ctx, now=i * 0.02, sense=stale)

    assert len(_events(ctx, EVENT_FACE_LOST)) == 1
    assert driver.locked is True


def test_a_face_returning_re_arms_the_report_for_the_next_loss() -> None:
    driver, registry, _ = _wire(face_lost_after_s=1.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)

    now = 0.0
    for _ in range(100):  # 2 s absent -> one report
        now += 0.02
        _tick(driver, ctx, now=now, sense=EMPTY_SENSE)
    assert len(_events(ctx, EVENT_FACE_LOST)) == 1

    for _ in range(10):  # the face comes back
        now += 0.02
        _tick(driver, ctx, now=now, sense=_face())
    assert len(_events(ctx, EVENT_FACE_LOST)) == 1

    for _ in range(100):  # ... and goes again -> a SECOND report
        now += 0.02
        _tick(driver, ctx, now=now, sense=EMPTY_SENSE)
    assert len(_events(ctx, EVENT_FACE_LOST)) == 2


def test_a_face_absent_under_the_threshold_reports_nothing() -> None:
    driver, registry, _ = _wire(face_lost_after_s=3.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)

    for i in range(1, 100):  # 2 s < 3 s
        _tick(driver, ctx, now=i * 0.02, sense=EMPTY_SENSE)

    assert _events(ctx, EVENT_FACE_LOST) == []


def test_ticking_while_unlocked_emits_nothing() -> None:
    driver, _registry, _ = _wire(face_lost_after_s=0.5, max_hold_s=1.0)
    ctx = _RecordingCtx(sense=EMPTY_SENSE)
    for i in range(1, 500):
        _tick(driver, ctx, now=i * 0.02)
    assert ctx.events == []
    assert ctx.evicts == []


# --------------------------------------------------------------------------- #
# 2. Mind offline — the lock cannot outlive its mind                          #
# --------------------------------------------------------------------------- #


def test_a_mind_offline_for_the_grace_period_releases_with_reason_mind_offline() -> None:
    online = {"v": True}
    driver, registry, intents = _wire(mind_online=lambda: online["v"], mind_offline_grace_s=10.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)
    assert set(intents.inhibitions) >= set(LOCK_INHIBITS)

    now = 0.0
    for _ in range(50):  # 1 s of a live mind
        now += 0.02
        _tick(driver, ctx, now=now, sense=_face())
    assert driver.locked is True

    online["v"] = False
    for _ in range(400):  # 8 s offline — still inside the grace
        now += 0.02
        _tick(driver, ctx, now=now, sense=_face())
    assert driver.locked is True

    for _ in range(200):  # past 10 s offline
        now += 0.02
        _tick(driver, ctx, now=now, sense=_face())

    assert driver.locked is False
    released = _events(ctx, EVENT_LOCK_RELEASED)
    assert len(released) == 1
    assert released[0]["detail"]["reason"] == REASON_MIND_OFFLINE
    assert ctx.evicts == [released[0]["detail"]["id"]]
    # t4's later-wins restore still holds: what the lock added is gone again.
    assert set(intents.inhibitions).isdisjoint(LOCK_INHIBITS)


def test_a_mind_that_comes_back_inside_the_grace_resets_the_countdown() -> None:
    online = {"v": True}
    driver, registry, _ = _wire(mind_online=lambda: online["v"], mind_offline_grace_s=10.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)

    now = 0.0
    online["v"] = False
    for _ in range(450):  # 9 s offline
        now += 0.02
        _tick(driver, ctx, now=now, sense=_face())
    assert driver.locked is True

    online["v"] = True
    now += 0.02
    _tick(driver, ctx, now=now, sense=_face())

    online["v"] = False
    for _ in range(450):  # 9 s offline AGAIN — the countdown restarted
        now += 0.02
        _tick(driver, ctx, now=now, sense=_face())
    assert driver.locked is True


def test_an_unknown_mind_never_releases_the_lock() -> None:
    """``None`` means "I cannot tell" — the conservative default, never a release."""
    for probe in (lambda: None, None):
        driver, registry, _ = _wire(mind_online=probe, mind_offline_grace_s=1.0)
        ctx = _RecordingCtx(sense=_face())
        _lock(registry, ctx)
        for i in range(1, 1000):  # 20 s — twenty grace periods
            _tick(driver, ctx, now=i * 0.02, sense=_face())
        assert driver.locked is True
        assert _events(ctx, EVENT_LOCK_RELEASED) == []


def test_a_raising_mind_probe_is_unknown_not_offline() -> None:
    def boom() -> bool:
        raise RuntimeError("no transport")

    driver, registry, _ = _wire(mind_online=boom, mind_offline_grace_s=1.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)
    for i in range(1, 500):
        _tick(driver, ctx, now=i * 0.02, sense=_face())
    assert driver.locked is True


# --------------------------------------------------------------------------- #
# 3. Max hold — and the reason every release carries                          #
# --------------------------------------------------------------------------- #


def test_the_default_max_hold_is_thirty_minutes() -> None:
    driver = FaceLockDriver()
    assert driver.max_hold_s == 1800.0
    assert driver.face_lost_after_s == 3.0
    assert driver.mind_offline_grace_s == 10.0


def test_a_lock_held_past_max_hold_releases_with_reason_max_hold() -> None:
    driver, registry, intents = _wire(max_hold_s=60.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)

    now = 0.0
    for _ in range(1000):  # 20 s
        now += 0.02
        _tick(driver, ctx, now=now, sense=_face())
    assert driver.locked is True

    _tick(driver, ctx, now=61.0, sense=_face())
    assert driver.locked is False
    released = _events(ctx, EVENT_LOCK_RELEASED)
    assert len(released) == 1
    assert released[0]["detail"]["reason"] == REASON_MAX_HOLD
    assert set(intents.inhibitions).isdisjoint(LOCK_INHIBITS)

    # And the timer does not keep firing on an unlocked driver.
    _tick(driver, ctx, now=200.0, sense=_face())
    assert len(_events(ctx, EVENT_LOCK_RELEASED)) == 1


def test_the_max_hold_clock_starts_at_lock_time_not_at_process_start() -> None:
    driver, registry, _ = _wire(max_hold_s=60.0)
    ctx = _RecordingCtx(sense=_face(), now=5000.0)
    _lock(registry, ctx)
    _tick(driver, ctx, now=5050.0, sense=_face())
    assert driver.locked is True
    _tick(driver, ctx, now=5061.0, sense=_face())
    assert driver.locked is False


def test_an_explicit_release_carries_reason_requested() -> None:
    driver, registry, _ = _wire()
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)
    result = registry.dispatch({"op": RELEASE_FACE}, ctx)

    assert result["ok"] is True
    assert result["reason"] == REASON_REQUESTED
    released = _events(ctx, EVENT_LOCK_RELEASED)
    assert len(released) == 1
    assert released[0]["detail"]["reason"] == REASON_REQUESTED
    assert driver.locked is False


def test_a_relock_after_a_lifecycle_release_starts_every_timer_fresh() -> None:
    driver, registry, _ = _wire(max_hold_s=60.0, face_lost_after_s=1.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)
    _tick(driver, ctx, now=61.0, sense=_face())
    assert driver.locked is False

    ctx.now = 61.0
    _lock(registry, ctx)
    assert driver.locked is True
    _tick(driver, ctx, now=100.0, sense=_face())
    assert driver.locked is True  # 39 s into the SECOND hold, not 100 s
    assert _events(ctx, EVENT_FACE_LOST) == []


def test_a_later_set_inhibition_still_wins_over_a_lifecycle_release() -> None:
    """t4's later-wins rule is the release path's rule, whoever triggered it."""
    driver, registry, intents = _wire(max_hold_s=10.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["nod", "feel-alive"]}, ctx)

    _tick(driver, ctx, now=11.0, sense=_face())
    assert driver.locked is False
    # The later statement stands untouched — release restored nothing behind it.
    assert set(intents.inhibitions) == {"nod", "feel-alive"}


# --------------------------------------------------------------------------- #
# 4. Seam shape — the driver is tick-bus shaped, like IntentDriver            #
# --------------------------------------------------------------------------- #


def test_the_driver_is_callable_as_a_tick_seam_rider() -> None:
    driver, registry, _ = _wire(max_hold_s=10.0)
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)
    ctx.now = 11.0
    driver(ctx)  # the TickBus calls `d(ctx)` — `now` comes off the ctx
    assert driver.locked is False


def test_the_two_lifecycle_actions_are_named_for_the_export_feed() -> None:
    assert FACE_LOST_ACTION == "face-lost"
    assert LOCK_RELEASED_ACTION == "lock-released"
    assert EVENT_FACE_LOST == "motion.face-lost"
    assert EVENT_LOCK_RELEASED == "motion.lock-released"


def test_both_lifecycle_actions_are_registered_with_the_runtime_exporter() -> None:
    """Registration IS the gate: an unregistered action reaches NEITHER feed."""
    for event_type, action in (
        (EVENT_FACE_LOST, FACE_LOST_ACTION),
        (EVENT_LOCK_RELEASED, LOCK_RELEASED_ACTION),
    ):
        mapped = to_runtime_event(
            {"type": event_type, "behavior": FACE_LOCK_BEHAVIOR, "channels": ["head"]}
        )
        assert isinstance(mapped, MotionEvent)
        assert mapped.action == action


def test_the_composition_site_rides_the_lock_driver_on_the_tick_bus() -> None:
    """``_compose_run_seam`` must put the driver in the ONE ``TickBus`` list.

    A source-level check for the same reason the sibling file AST-checks the
    import boundary: the wiring is the whole feature (an unticked driver reports
    nothing and releases nothing), and the alternative — booting the real CLI —
    would prove it far more slowly and no more precisely.
    """
    source = inspect.getsource(behavior_cmd._compose_run_seam)
    head, sep, tail = source.partition("drivers = [")
    assert sep, "the drivers list moved — re-point this test"
    drivers_block = tail.split("]", 1)[0]
    assert "face_lock_driver" in drivers_block
    # ... and it is constructed there too, with the mind seam left explicit.
    assert "FaceLockDriver(" in head


# --------------------------------------------------------------------------- #
# 4. An externally EVICTED lock is not a locked lock (PR #172 review)          #
# --------------------------------------------------------------------------- #


def test_an_external_stop_of_the_behavior_releases_the_lock_state() -> None:
    """``behavior stop face-lock`` removes the gaze; the state must follow it.

    The driver watched face, mind and clock only, so an eviction it did not ask
    for left ``locked`` true and the inhibitions installed: the head was free
    again while ``lock_face`` still answered ``already locked`` and
    ``feel-alive``/``orient-to-sound`` stayed inhibited until the 30-minute
    watchdog. Eviction is a fourth ending, and it runs the SAME release path.
    """
    driver, registry, intents = _wire()
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)
    assert driver.locked is True
    assert set(LOCK_INHIBITS) <= set(intents.inhibitions)

    # Somebody else stopped it — `behavior stop face-lock`, or `stop all`.
    ctx._active.discard(FACE_LOCK_BEHAVIOR)

    _tick(driver, ctx, now=1.0)

    assert driver.locked is False
    assert set(LOCK_INHIBITS) & set(intents.inhibitions) == set()
    released = _events(ctx, EVENT_LOCK_RELEASED)
    assert len(released) == 1
    assert released[0]["detail"]["reason"] == REASON_EVICTED


def test_a_re_lock_after_an_eviction_is_admitted_not_answered_already_locked() -> None:
    driver, registry, _ = _wire()
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)
    ctx._active.discard(FACE_LOCK_BEHAVIOR)
    _tick(driver, ctx, now=1.0)

    again = _lock(registry, ctx)

    assert again["ok"] is True
    assert again.get("note") != "already locked"
    assert len(ctx.admits) == 2


def test_an_evicted_release_emits_exactly_one_event_not_one_per_tick() -> None:
    driver, registry, _ = _wire()
    ctx = _RecordingCtx(sense=_face())
    _lock(registry, ctx)
    ctx._active.discard(FACE_LOCK_BEHAVIOR)

    for tick, now in enumerate((1.0, 1.02, 1.04, 1.06), start=1):
        _tick(driver, ctx, now=now)

    assert len(_events(ctx, EVENT_LOCK_RELEASED)) == 1


def test_a_ctx_without_active_names_never_releases_the_lock() -> None:
    """Total, like every other seam here: an absent probe is UNKNOWN, not gone."""

    @dataclass
    class _NoNamesCtx(_RecordingCtx):
        active_names = None  # type: ignore[assignment]

    driver, registry, _ = _wire()
    ctx = _NoNamesCtx(sense=_face())
    _lock(registry, ctx)

    _tick(driver, ctx, now=1.0)

    assert driver.locked is True


def test_eviction_is_a_named_reason_on_the_lock_released_event() -> None:
    assert REASON_EVICTED == "evicted"


# --------------------------------------------------------------------------- #
# 5. Only `lock_face` may admit `face-lock` (PR #172 review)                   #
# --------------------------------------------------------------------------- #


def test_declare_goal_refuses_the_lifecycle_owned_face_lock() -> None:
    """A goal is INDEFINITE by construction — exactly what the lock kind owns.

    ``_apply_declare_goal`` accepts any library entry and gives it a standing,
    unbounded lifetime with no fresh-face check, no inhibitions, no mind
    presence and no max-hold; ``release_face`` then answers ``not locked``. That
    is the unmanaged standing gaze ``lock_face`` exists to prevent, reachable
    from the public intent surface.
    """
    _driver, registry, _ = _wire()
    ctx = _RecordingCtx(sense=_face())

    result = registry.dispatch({"op": DECLARE_GOAL, "goal": FACE_LOCK_BEHAVIOR}, ctx)

    assert result["ok"] is False
    assert LOCK_FACE in result["error"]
    assert ctx.admits == []


def test_run_behavior_refuses_the_lifecycle_owned_face_lock() -> None:
    """The same refusal on the bounded surface, so ONE admitter owns the name.

    A bounded ``run_behavior face-lock`` would not strand a standing claim, but
    it would put a SECOND behavior of that name on the active set — which is
    what the driver's eviction watchdog reads to decide the lock is gone.
    """
    _driver, registry, _ = _wire()
    ctx = _RecordingCtx(sense=_face())

    result = registry.dispatch(
        {
            "op": RUN_BEHAVIOR,
            "name": FACE_LOCK_BEHAVIOR,
            "lifetime": {"looping": True, "duration": 5.0},
        },
        ctx,
    )

    assert result["ok"] is False
    assert LOCK_FACE in result["error"]
    assert ctx.admits == []


def test_a_react_rule_naming_face_lock_is_refused_at_load() -> None:
    """Fail-closed at LOAD, like the unbounded-lifetime refusal beside it."""
    from reachy.behavior.rules import RulesConfig

    with pytest.raises(CliError) as excinfo:
        RulesConfig.from_dict(
            {
                "react": [
                    {
                        "id": "sneak-a-lock",
                        "when": [{"field": "face", "op": "present"}],
                        "run": FACE_LOCK_BEHAVIOR,
                        "duration_s": 5.0,
                    }
                ]
            }
        )

    assert FACE_LOCK_BEHAVIOR in str(excinfo.value.message)
    assert LOCK_FACE in str(excinfo.value.remediation)


def test_the_lock_kind_itself_still_admits_the_entry() -> None:
    """The refusal is about the GENERIC surfaces, not the entry being unusable."""
    driver, registry, _ = _wire()
    ctx = _RecordingCtx(sense=_face())

    result = _lock(registry, ctx)

    assert result["ok"] is True
    assert driver.locked is True
    assert [b.name for b in ctx.admits] == [FACE_LOCK_BEHAVIOR]
