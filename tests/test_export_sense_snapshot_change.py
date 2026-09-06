"""t2 (issue #184): ``SenseSnapshotDriver`` must emit on a CHANGED PAYLOAD.

Today the driver compares ``ctx.sense`` by frozen-dataclass equality
(``reachy/export/runtime.py``'s ``SenseSnapshotDriver.__call__``), but
``Sense``/``PatState`` carry clock-derived fields that advance every tick with
no corresponding key in the emitted payload:

- ``PatState.phase_started_at`` / ``last_press_at`` are rewritten to ``now`` on
  several paths in ``reachy/behavior/pat_sense.py`` even when the touch
  ``phase`` itself has not changed.
- ``Sense.face_age_s`` / ``doa_age_s`` advance every tick while a face is held
  in view or a DoA reading stays fresh — and neither key reaches the emitted
  dict at all (grep 'age' in the emit dict is empty), so a raw dataclass
  compare defeats itself on a continuously-refreshed sense.

The fix: build the emit payload dict FIRST and compare it (minus ``ts``/
``tick``) against the last EMITTED payload, emitting only on a genuine
difference — never the raw ``Sense``/``PatState`` values. The wire shape must
not change: every key documented in ``docs/export-schema.md``'s sense block
still appears, ``pat_state.phase_started_at``/``last_press_at`` included.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from reachy.behavior.sense import EMPTY_SENSE, PatState, Sense
from reachy.export.runtime import SenseSnapshotDriver

REPO_ROOT = Path(__file__).parent.parent


@dataclass
class _Ctx:
    """A minimal duck-typed TickContext exposing exactly what the driver needs."""

    now: float = 0.0
    tick: int = 0
    sense: Sense = EMPTY_SENSE
    events: list = field(default_factory=list)

    def emit(self, event: dict) -> None:
        self.events.append(event)


def _sense_events(ctx: _Ctx) -> list[dict]:
    return [e for e in ctx.events if e.get("type") == "sense"]


def test_first_tick_always_emits() -> None:
    driver = SenseSnapshotDriver()
    ctx = _Ctx(now=0.0, tick=1, sense=EMPTY_SENSE)
    driver(ctx)
    assert len(_sense_events(ctx)) == 1


def test_pat_state_clock_only_change_does_not_re_emit() -> None:
    """Two snapshots differing ONLY in phase_started_at/last_press_at → one event."""
    state = PatState(
        availability="available",
        contact=True,
        touch_type="scratch",
        level="level1",
        phase="receptive",
        phase_started_at=1.0,
        last_press_at=1.2,
    )
    driver = SenseSnapshotDriver()
    ctx = _Ctx(now=1.0, tick=1, sense=Sense(pat_state=state))
    driver(ctx)

    # Same phase, same everything meaningful — but pat_sense.py rewrites both
    # clock anchors to "now" on several paths even with no phase transition.
    ctx.tick = 2
    ctx.now = 1.02
    ctx.sense = Sense(
        pat_state=PatState(
            availability="available",
            contact=True,
            touch_type="scratch",
            level="level1",
            phase="receptive",
            phase_started_at=1.02,
            last_press_at=1.22,
        )
    )
    driver(ctx)

    assert len(_sense_events(ctx)) == 1


def test_held_face_and_live_doa_for_100_ticks_emit_once() -> None:
    """face_age_s / doa_age_s advancing every tick must not defeat the fold."""
    driver = SenseSnapshotDriver()
    ctx = _Ctx(now=0.0, tick=1, sense=EMPTY_SENSE)

    for tick in range(1, 101):
        ctx.tick = tick
        ctx.now = tick / 50.0
        ctx.sense = Sense(
            face="ada",
            face_bbox=(0.4, 0.4, 0.2, 0.2),
            face_age_s=tick / 50.0,  # advances every tick
            doa_angle=1.2,
            doa_age_s=tick / 50.0,  # advances every tick
        )
        driver(ctx)

    assert len(_sense_events(ctx)) == 1


def test_a_real_face_change_emits_again() -> None:
    driver = SenseSnapshotDriver()
    ctx = _Ctx(now=0.0, tick=1, sense=Sense(face="ada", face_age_s=0.02))
    driver(ctx)

    ctx.tick = 2
    ctx.now = 0.04
    ctx.sense = Sense(face="bo", face_age_s=0.02)  # a real, new name — not just age
    driver(ctx)

    assert len(_sense_events(ctx)) == 2


def test_a_real_pat_phase_change_emits_again() -> None:
    state = PatState(
        availability="available",
        contact=True,
        touch_type="scratch",
        level="level1",
        phase="receptive",
        phase_started_at=1.0,
        last_press_at=1.2,
    )
    driver = SenseSnapshotDriver()
    ctx = _Ctx(now=1.0, tick=1, sense=Sense(pat_state=state))
    driver(ctx)

    ctx.tick = 2
    ctx.now = 1.02
    ctx.sense = Sense(
        pat_state=PatState(
            availability="available",
            contact=True,
            touch_type="scratch",
            level="level1",
            phase="contentment",  # a real phase transition
            phase_started_at=1.02,
            last_press_at=1.22,
        )
    )
    driver(ctx)

    assert len(_sense_events(ctx)) == 2


def test_a_real_rms_change_emits_again() -> None:
    driver = SenseSnapshotDriver()
    ctx = _Ctx(now=0.0, tick=1, sense=Sense(rms=0.01))
    driver(ctx)

    ctx.tick = 2
    ctx.now = 0.02
    ctx.sense = Sense(rms=0.9)
    driver(ctx)

    assert len(_sense_events(ctx)) == 2


def _table_keys(schema_text: str, heading: str) -> set[str]:
    """Pull every ``| `key` |`` cell out of the ONE contiguous markdown table
    that starts right after *heading* — reading the documented CONTRACT (the
    key table), not the fenced example line, which is independently known to
    omit a null ``blocked_reason`` and is not this task's concern to repair.
    """
    start = schema_text.index(heading)
    lines = schema_text[start:].splitlines()
    keys: set[str] = set()
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            in_table = True
            m = re.match(r"\|\s*`([a-zA-Z0-9_, `]+)`\s*\|", line)
            if m:
                keys.update(re.findall(r"[a-zA-Z0-9_]+", m.group(1)))
        elif in_table:
            break
    return keys


def test_emitted_payload_keys_match_the_documented_schema() -> None:
    """The wire shape must not change: same top-level keys as before the fix,
    and the same pat_state sub-keys, cited from docs/export-schema.md's own
    key tables so the two can never silently drift apart.
    """
    schema_text = (REPO_ROOT / "docs" / "export-schema.md").read_text(encoding="utf-8")
    documented_top_keys = _table_keys(schema_text, '#### `"sense"` — perception snapshot') - {"t"}
    documented_pat_state_keys = _table_keys(schema_text, "| `availability`     | string")

    driver = SenseSnapshotDriver()
    state = PatState(
        availability="available",
        contact=True,
        touch_type="scratch",
        level="level1",
        yaw_deg=1.0,
        phase="receptive",
        phase_started_at=1.0,
        last_press_at=1.2,
        blocked_reason=None,
    )
    ctx = _Ctx(now=1.0, tick=1, sense=Sense(pat_state=state))
    driver(ctx)

    emitted = ctx.events[0]
    emitted_top_keys = set(emitted.keys()) - {"type"}
    assert emitted_top_keys == documented_top_keys
    assert set(emitted["pat_state"].keys()) == documented_pat_state_keys
