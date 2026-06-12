"""Tests for reachy.sleep.wake — two-tier wake detection.

Tier-1: speech_detected flag + SnapDetector (always on, no extra deps).
Tier-2: wake-word engine (lazy-loaded, optional [cpu]/[gpu] extra).

Tests confirm:
1. wake() fires on speech_detected even without a wake-word engine.
2. wake() fires on a snap even without a wake-word engine.
3. wake() never raises when the wake-word engine is absent.
4. The wake-word engine import lives inside a function/method and is NOT pulled in
   at module import time (import boundary).
5. pyproject.toml declares [cpu] and [gpu] optional-dependency extras.
"""

from __future__ import annotations

import importlib
import sys

import numpy as np

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sense(speech: bool = False):
    """Return a minimal Sense-like object."""
    from reachy.behavior.sense import Sense

    return Sense(doa_angle=None, speech_detected=speech)


def _silent_chunk(n: int = 512) -> np.ndarray:
    """Return a low-level (non-zero) float32 audio chunk.

    A tiny nonzero RMS is needed so SnapDetector's rolling average stays above
    the 1e-6 floor, allowing the ratio comparison to work when a loud chunk follows.
    """
    return np.full(n, 0.001, dtype=np.float32)


def _snap_chunk(n: int = 512) -> np.ndarray:
    """Return a loud transient float32 audio chunk (triggers SnapDetector)."""
    return np.ones(n, dtype=np.float32)


# ---------------------------------------------------------------------------
# 1. Tier-1 fires on speech_detected (no engine)
# ---------------------------------------------------------------------------


class TestTier1Speech:
    """wake() fires when Sense.speech_detected is True (engine not needed)."""

    def test_speech_triggers_wake(self):
        from reachy.sleep.wake import WakeDetector

        det = WakeDetector(wake_word_enabled=False)
        sense = _make_sense(speech=True)
        assert det.update(sense, _silent_chunk()) is True

    def test_no_speech_no_snap_no_wake(self):
        from reachy.sleep.wake import WakeDetector

        det = WakeDetector(wake_word_enabled=False)
        sense = _make_sense(speech=False)
        # Feed enough silent chunks that the snap detector has a history
        for _ in range(10):
            result = det.update(sense, _silent_chunk())
        assert result is False

    def test_speech_flag_cleared_each_call(self):
        """A second call with no speech must NOT re-fire."""
        from reachy.sleep.wake import WakeDetector

        det = WakeDetector(wake_word_enabled=False)
        sense_on = _make_sense(speech=True)
        sense_off = _make_sense(speech=False)
        assert det.update(sense_on, _silent_chunk()) is True
        assert det.update(sense_off, _silent_chunk()) is False


# ---------------------------------------------------------------------------
# 2. Tier-1 fires on snap (no engine)
# ---------------------------------------------------------------------------


class TestTier1Snap:
    """wake() fires on a loud transient detected by SnapDetector."""

    def test_snap_triggers_wake(self):
        from reachy.sleep.wake import WakeDetector

        det = WakeDetector(wake_word_enabled=False)
        sense = _make_sense(speech=False)
        # Prime the rolling window with quiet chunks first
        for _ in range(30):
            det.update(sense, _silent_chunk())
        # Feed a loud snap
        fired = det.update(sense, _snap_chunk())
        assert fired is True

    def test_snap_requires_quiet_baseline(self):
        """Snap must NOT fire on a chunk that's loud from the start with no baseline."""
        from reachy.sleep.wake import WakeDetector

        det = WakeDetector(wake_word_enabled=False)
        sense = _make_sense(speech=False)
        # Only a few chunks — snap detector needs ≥5 samples; with <5 it must not fire
        for _ in range(3):
            result = det.update(sense, _snap_chunk())
        assert result is False

    def test_reset_rebuilds_snap_without_private_access(self):
        """reset() reconstructs the SnapDetector from WakeDetector's own retained
        config — not by reaching into SnapDetector private attributes (regression:
        coupled to ``_ratio`` / ``_min_rms`` / ``_history`` internals)."""
        from reachy.sleep.wake import WakeDetector

        det = WakeDetector(
            wake_word_enabled=False, snap_ratio=4.0, snap_min_rms=0.03, snap_history=17
        )
        # Config is retained on the detector itself, so reset() needs no SnapDetector internals.
        assert det._snap_ratio == 4.0
        assert det._snap_min_rms == 0.03
        assert det._snap_history == 17

        det.reset()  # must not raise even if SnapDetector internals are renamed

        # After reset the detector still works: quiet baseline then a loud snap fires.
        sense = _make_sense(speech=False)
        for _ in range(30):
            det.update(sense, _silent_chunk())
        assert det.update(sense, _snap_chunk()) is True


# ---------------------------------------------------------------------------
# 3. Graceful degrade — engine absent, no exception raised
# ---------------------------------------------------------------------------


class TestGracefulDegrade:
    """With wake_word_enabled=True but the engine absent, no exception is raised."""

    def test_no_exception_engine_absent(self, monkeypatch):
        """Simulate engine import failure; wake() must degrade to Tier-1."""
        # Remove the engine from sys.modules and make it un-importable
        engine_name = "openwakeword"
        monkeypatch.setitem(sys.modules, engine_name, None)  # type: ignore[arg-type]

        from reachy.sleep.wake import WakeDetector

        det = WakeDetector(wake_word_enabled=True)
        sense = _make_sense(speech=True)
        # Must not raise, must still fire on speech
        result = det.update(sense, _silent_chunk())
        assert result is True

    def test_no_exception_engine_absent_no_speech(self, monkeypatch):
        """Absent engine + no speech/snap → returns False without raising."""
        engine_name = "openwakeword"
        monkeypatch.setitem(sys.modules, engine_name, None)  # type: ignore[arg-type]

        from reachy.sleep.wake import WakeDetector

        det = WakeDetector(wake_word_enabled=True)
        sense = _make_sense(speech=False)
        for _ in range(10):
            result = det.update(sense, _silent_chunk())
        assert result is False

    def test_wake_word_disabled_never_raises(self):
        """wake_word_enabled=False path always stays clean."""
        from reachy.sleep.wake import WakeDetector

        det = WakeDetector(wake_word_enabled=False)
        for _ in range(5):
            det.update(_make_sense(), _silent_chunk())  # must not raise


# ---------------------------------------------------------------------------
# 4. Import boundary — engine NOT imported at module level
# ---------------------------------------------------------------------------


class TestImportBoundary:
    """The wake-word engine must NOT be imported at module load time."""

    def test_module_import_does_not_import_openwakeword(self):
        """Importing reachy.sleep.wake must not pull in openwakeword."""
        # Reload the module to ensure we test import time, not cached state.
        # First, ensure openwakeword is NOT present in sys.modules at all.
        sys.modules.pop("openwakeword", None)

        # Import (or re-import) the module; the engine must stay absent.
        if "reachy.sleep.wake" in sys.modules:
            del sys.modules["reachy.sleep.wake"]

        importlib.import_module("reachy.sleep.wake")

        # openwakeword should still be absent
        assert "openwakeword" not in sys.modules

    def test_engine_import_inside_method_not_top_level(self):
        """Validate via source inspection that the engine import is guarded."""
        import inspect

        import reachy.sleep.wake as wake_mod

        source = inspect.getsource(wake_mod)
        # The top-level module body must NOT contain a bare `import openwakeword`
        # or `from openwakeword`. We check by splitting source into lines and
        # confirming any openwakeword import is inside an indented block.
        for line in source.splitlines():
            stripped = line.lstrip()
            if "openwakeword" in stripped and stripped.startswith(("import ", "from ")):
                # This import must be indented (inside a function/method/try)
                assert line != stripped, (
                    "Found a top-level import of openwakeword — "
                    "it must be inside a function/method to stay lazy."
                )


# ---------------------------------------------------------------------------
# 5. pyproject.toml declares [cpu] and [gpu] extras
# ---------------------------------------------------------------------------


class TestPyprojectExtras:
    """pyproject.toml must declare [cpu] and [gpu] optional-dependency tables."""

    def test_cpu_and_gpu_extras_declared(self):
        import pathlib

        root = pathlib.Path(__file__).parent.parent
        pyproject = (root / "pyproject.toml").read_text()

        # tomllib is stdlib in Python 3.11+
        import tomllib

        data = tomllib.loads(pyproject)
        optional_deps = data.get("project", {}).get("optional-dependencies", {})

        assert (
            "cpu" in optional_deps
        ), "[cpu] extra not found in [project.optional-dependencies]; add it to pyproject.toml"
        assert (
            "gpu" in optional_deps
        ), "[gpu] extra not found in [project.optional-dependencies]; add it to pyproject.toml"

    def test_cpu_extra_does_not_bleed_into_base(self):
        """The [cpu] extra packages must NOT appear in base dependencies."""
        import pathlib
        import tomllib

        root = pathlib.Path(__file__).parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text())

        base_deps = data.get("project", {}).get("dependencies", [])
        cpu_deps = data.get("project", {}).get("optional-dependencies", {}).get("cpu", [])

        for pkg in cpu_deps:
            pkg_name = pkg.split(">=")[0].split("==")[0].strip()
            for base in base_deps:
                assert (
                    pkg_name.lower() not in base.lower()
                ), f"[cpu] package {pkg_name!r} leaked into base dependencies"
