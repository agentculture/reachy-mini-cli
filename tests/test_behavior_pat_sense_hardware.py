"""Hardware-recorded regression tests for the pat sense (issue #80).

These run the REAL :class:`~reachy.behavior.pat_sense.PatSenseDriver` over
commanded-vs-actual head poses **recorded from the physical robot** during a
hands-on calibration session, so the sense's central promises are pinned against
measured plant behaviour rather than a synthetic model:

* ``pat_base_still.csv``  — 30 s, head commanded STILL, nobody touching it.
* ``pat_pat_still.csv``   — 50 s, head commanded STILL, operator petting it
  continuously (natural dog-style petting from every direction).
* ``pat_base_wander.csv`` — 35 s, ``feel-alive`` idle motion, nobody touching it.
* ``pat_pat_wander.csv``  — 45 s, ``feel-alive`` idle motion, operator petting.

Each row is ``t, cmd_pitch, cmd_yaw, a_pitch, a_yaw, a_roll, a_x, a_y, a_z``
(degrees; ``a_x/y/z`` millimetres), sampled at the engine's 50 Hz tick and
downsampled 2x. Only the four columns the driver consumes are used here; the
extra axes are retained because they are what proved the wander case
unsalvageable (see the module docstring of ``pat_sense``).

What the measurements established, and what these tests therefore pin:

* **Still head → pats are trivially detectable.** 12-20x separation on every
  axis; the detector fires repeatedly on petting and never on an untouched head.
* **Wandering head → pats are NOT detectable.** 0.7-2.0x separation on every
  axis, including the ones ``feel-alive`` never commands (roll/x/y are dragged
  ~11x noisier by mechanical coupling). No threshold separates them, so the
  stillness gate — not a threshold — is what makes the sense honest.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from reachy.behavior.pat_sense import ENOUGH_MAX_S, WARNING_AFTER_S, PatSenseDriver
from reachy.motion.pat import PatDetector

pytestmark = pytest.mark.offline

DATA = Path(__file__).parent / "data"


class _Replay:
    """Replays a recording through the driver, counting latched pat events."""

    def __init__(self, name: str) -> None:
        with open(DATA / f"pat_{name}.csv", newline="") as fh:
            self.rows = [{k: float(v) for k, v in row.items()} for row in csv.DictReader(fh)]
        self.reads = 0

    def run(self, **driver_kw) -> int:
        """Feed every sample through a fresh driver; return the event count."""
        row_holder: dict = {}

        def reader():
            self.reads += 1
            return (row_holder["a_pitch"], row_holder["a_yaw"])

        driver = PatSenseDriver(reader=reader, **driver_kw)
        for row in self.rows:
            row_holder.update(row)
            driver(_Ctx(row))
        return driver.events


class _Ctx:
    """A TickContext-shaped view of one recorded sample (base layer owns head)."""

    def __init__(self, row: dict) -> None:
        self.now = row["t"]
        self.tick = 0
        self.ownership = {"head": "feel-alive-1", "antennas": None, "body_yaw": None}
        self.pose = {
            "head": {
                "x": 0.0,
                "y": 0.0,
                "z": 0.0,
                "roll": 0.0,
                "pitch": row["cmd_pitch"],
                "yaw": row["cmd_yaw"],
            },
            "antennas": (0.0, 0.0),
            "body_yaw": 0.0,
        }


# --------------------------------------------------------------------------- #
# Still head — the sense's supported operating point                          #
# --------------------------------------------------------------------------- #


def test_still_untouched_never_fires() -> None:
    """30 s of a still, untouched head must produce ZERO pat events.

    The measured noise floor here is p99 0.07-0.11 deg — far under the 0.5 deg
    press threshold — so this is the no-ghost guarantee in its shipped config.
    """
    assert _Replay("base_still").run(warmup_s=0.0) == 0


@pytest.mark.parametrize("level2_threshold", [4.0, 5.0, 6.0, 7.0, 8.0])
@pytest.mark.parametrize("enough_after", [WARNING_AFTER_S, 10.0, ENOUGH_MAX_S])
def test_still_petting_fires_repeatedly(level2_threshold: float, enough_after: float) -> None:
    """50 s of real petting on a still head must be detected, repeatedly.

    BOTH random seams are injected. There are two, not one, and missing either
    leaves the test flaky:

    * ``PatDetector.level2_threshold_fn`` — default ``random.uniform(4.0, 8.0)``
    * ``PatSenseDriver.enough_after_fn`` — default
      ``random.uniform(WARNING_AFTER_S, ENOUGH_MAX_S)``

    With both free this test failed about 4% of runs (12 of 300 seeded). Pinning
    only the detector's threshold did NOT fix it — the driver's ``enough`` timing
    alone still tipped it under the bar — which is why the parametrization
    crosses both axes rather than pinning a midpoint.

    The measured grid over the full draw ranges is 5-8 events, and the minimum
    of exactly 5 sits at ``level2_threshold`` 7.0-8.0. So the ``>= 5`` bar has
    ZERO margin at the top of the range: that is deliberate and safe only
    because every cell here is now deterministic. If a future change drops any
    cell to 4, this SHOULD go red — that is the regression it exists to catch,
    not a flake to re-tune.
    """
    events = _Replay("pat_still").run(
        warmup_s=0.0,
        detector=PatDetector(level2_threshold_fn=lambda: level2_threshold),
        enough_after_fn=lambda: enough_after,
    )
    assert events >= 5, (
        f"only {events} pats detected in 50 s of continuous petting "
        f"(level2_threshold={level2_threshold}, enough_after={enough_after})"
    )


@pytest.mark.parametrize("press", [0.3, 0.5, 1.0, 2.0])
def test_still_separation_is_threshold_insensitive(press: float) -> None:
    """The still-head margin is wide enough that threshold choice barely matters.

    Every threshold from 0.3 to 2.0 deg gives zero false fires and repeated true
    detections on the recordings — the signature of a genuinely separable signal,
    and the evidence that the shipped 0.5 is a comfortable middle rather than a
    knife edge.
    """
    from reachy.motion.pat import PatDetector

    def detector() -> PatDetector:
        return PatDetector(
            press_threshold=press,
            release_threshold=press * 0.4,
            yaw_press_threshold=press,
            yaw_release_threshold=press * 0.4,
        )

    assert _Replay("base_still").run(detector=detector(), warmup_s=0.0) == 0
    assert _Replay("pat_still").run(detector=detector(), warmup_s=0.0) >= 3


# --------------------------------------------------------------------------- #
# Wandering head — the stillness gate's whole reason to exist                 #
# --------------------------------------------------------------------------- #


def test_wander_untouched_never_fires_with_the_gate() -> None:
    """Idle motion with nobody touching the robot must produce ZERO events.

    This is the ghost class that kept the sense dormant (issue #79): with the
    gate OFF this recording fires repeatedly (see the companion test); the
    stillness gate is what makes it structurally impossible.
    """
    assert _Replay("base_wander").run() == 0


def test_wander_untouched_would_fire_without_the_gate() -> None:
    """Regression guard: the recording really does contain the ghost signal.

    If this stops firing, the fixture no longer reproduces the defect and the
    gate test above has quietly lost its meaning.
    """
    assert _Replay("base_wander").run(still_hold_s=0.0, warmup_s=0.0) > 0


def test_wander_petting_is_gated_out_too() -> None:
    """Honest limitation: a moving head cannot feel pats, so it reports none.

    Measured separation while wandering is 0.7-2.0x on every axis — there is no
    threshold that admits these pats without also admitting the ghosts, so the
    sense declines rather than guessing.
    """
    assert _Replay("pat_wander").run() == 0
