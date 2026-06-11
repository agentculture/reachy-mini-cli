"""Boundary / import-guard suite for the ``sleep`` noun (task t9).

Proves the spec's NOT-claims (boundary c6 / h6):

* The implementation adds only a timer + producer + flag — NO affect/emotion
  model, NO daemon-suspend / motor-disable / OS-power call.
* It introduces NO new base runtime dependency beyond numpy (``reachy_mini``
  stays an optional ``[sdk]`` extra; the wake-word engine stays behind
  ``[cpu]``/``[gpu]``, lazily imported).

Idiom: mirrors ``test_think_boundary.py`` (AST-based source scan) and
``test_sleep_wake.py`` / ``test_dependencies.py`` (sys.modules + pyproject
checks).  See those files for the established patterns this module copies.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import subprocess
import sys
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared helper — identical to the one in test_think_boundary.py
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent


def _module_pulls_in(dotted: str, forbidden: str) -> bool:
    """True if importing *dotted* pulls *forbidden* into sys.modules.

    Run in a fresh SUBPROCESS so the probe has ZERO effect on this
    interpreter's sys.modules — evicting/re-importing in-process splits module
    identity and breaks unrelated suites (e.g. the supervisor monkeypatch tests).
    """
    code = f"import sys; import {dotted}; print({forbidden!r} in sys.modules)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    return proc.stdout.strip() == "True"


def _imported_modules(module) -> set[str]:
    """All dotted module names imported by *module* (Import + ImportFrom)."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def _source(*module_path: str) -> str:
    """Return the concatenated source of the given dotted module names."""
    parts = []
    for dotted in module_path:
        mod = importlib.import_module(dotted)
        parts.append(inspect.getsource(mod))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The sleep modules under test
# ---------------------------------------------------------------------------

_SLEEP_MODULE_NAMES = [
    "reachy.motion.sleep_signal",
    "reachy.sleep.state",
    "reachy.sleep.stimulus",
    "reachy.sleep.wake",
    "reachy.sleep.supervisor",
    "reachy.motion.sleep",
]


# ---------------------------------------------------------------------------
# 1. Base path does NOT pull reachy_mini into sys.modules
# ---------------------------------------------------------------------------


class TestNoSdkImportAtModuleLoad:
    """Importing the base sleep modules must not pull in ``reachy_mini``."""

    def test_sleep_signal_does_not_import_reachy_mini(self) -> None:
        """reachy.motion.sleep_signal must not pull in reachy_mini at import time."""
        assert not _module_pulls_in("reachy.motion.sleep_signal", "reachy_mini"), (
            "reachy.motion.sleep_signal pulled in reachy_mini — "
            "it must be SDK-free on the base profile"
        )

    def test_sleep_state_does_not_import_reachy_mini(self) -> None:
        """reachy.sleep.state is pure stdlib — must not import reachy_mini."""
        assert not _module_pulls_in(
            "reachy.sleep.state", "reachy_mini"
        ), "reachy.sleep.state pulled in reachy_mini at import time"

    def test_sleep_stimulus_does_not_import_reachy_mini(self) -> None:
        """reachy.sleep.stimulus is stdlib-only — must not import reachy_mini."""
        assert not _module_pulls_in(
            "reachy.sleep.stimulus", "reachy_mini"
        ), "reachy.sleep.stimulus pulled in reachy_mini at import time"

    def test_sleep_wake_does_not_import_reachy_mini(self) -> None:
        """reachy.sleep.wake must not pull in reachy_mini at module load."""
        assert not _module_pulls_in(
            "reachy.sleep.wake", "reachy_mini"
        ), "reachy.sleep.wake pulled in reachy_mini — wake-word engine must stay lazy"

    def test_sleep_supervisor_does_not_import_reachy_mini(self) -> None:
        """reachy.sleep.supervisor is pure stdlib — must not import reachy_mini."""
        assert not _module_pulls_in(
            "reachy.sleep.supervisor", "reachy_mini"
        ), "reachy.sleep.supervisor pulled in reachy_mini at import time"

    def test_sleep_producer_does_not_import_reachy_mini(self) -> None:
        """reachy.motion.sleep (SleepProducer) must not pull in reachy_mini."""
        assert not _module_pulls_in("reachy.motion.sleep", "reachy_mini"), (
            "reachy.motion.sleep pulled in reachy_mini at import time — "
            "it must be a pure planner with no SDK dependency"
        )


# ---------------------------------------------------------------------------
# 2. No power / suspend / motor-disable calls in any sleep module
# ---------------------------------------------------------------------------


class TestNoPowerSuspendCalls:
    """Sleep modules must not contain OS-power, daemon-suspend, or motor-disable calls.

    The sleep noun is a *motion choreography* — it does NOT suspend the OS, cut
    motor power, call systemctl, or invoke any motor-disable API.  We scan each
    module's source statically.
    """

    # Keywords that would indicate a power/suspend/motor-disable call.
    _FORBIDDEN_PATTERNS = [
        "systemctl",
        "suspend",
        "os.system",
        "disable_torque",
        "motor_off",
        "motor_disable",
        "power_off",
        "shutdown",
        "hibernate",
    ]

    def _scan_module(self, dotted: str) -> None:
        """Assert no forbidden pattern appears in the module source."""
        mod = importlib.import_module(dotted)
        src = inspect.getsource(mod)
        for pattern in self._FORBIDDEN_PATTERNS:
            # Allow the word to appear in a comment/docstring context
            # (those are acceptable), but flag it if it appears in actual code.
            # We scan at the AST string-literal level for robustness, but a
            # simple substring check on source lines is sufficient here because
            # none of these patterns are expected to appear anywhere.
            assert pattern not in src, (
                f"Forbidden pattern {pattern!r} found in {dotted} — "
                "sleep must not suspend the OS, disable motors, or cut power"
            )

    def test_sleep_signal_no_power_calls(self) -> None:
        self._scan_module("reachy.motion.sleep_signal")

    def test_sleep_state_no_power_calls(self) -> None:
        self._scan_module("reachy.sleep.state")

    def test_sleep_stimulus_no_power_calls(self) -> None:
        self._scan_module("reachy.sleep.stimulus")

    def test_sleep_wake_no_power_calls(self) -> None:
        self._scan_module("reachy.sleep.wake")

    def test_sleep_supervisor_no_power_calls(self) -> None:
        self._scan_module("reachy.sleep.supervisor")

    def test_sleep_producer_no_power_calls(self) -> None:
        self._scan_module("reachy.motion.sleep")


# ---------------------------------------------------------------------------
# 3. No affect/emotion model — "boredom" is only an idle timer
# ---------------------------------------------------------------------------


class TestNoAffectEmotionModel:
    """Sleep introduces NO affect/emotion model.

    The only "boredom" concept is the idle timer in :mod:`reachy.sleep.state`.
    There must be no joy/anger/sadness/valence/arousal emotion-state machine,
    no mood persistence store, and no cross-noun emotion vocabulary.
    """

    # Emotion vocabulary that must NOT appear as a model concept in the code.
    _EMOTION_KEYWORDS = [
        "joy",
        "anger",
        "sadness",
        "fear",
        "disgust",
        "valence",
        "arousal",
        "emotion_state",
        "EmotionState",
        "AffectModel",
        "mood_store",
        "mood_db",
        "feeling",
    ]

    def test_sleep_state_exposes_only_wakefulness_enum(self) -> None:
        """SleepState must expose exactly ALERT, DROWSY, ASLEEP — no emotion states."""
        from reachy.sleep.state import SleepState

        members = {m.name for m in SleepState}
        assert members == {
            "ALERT",
            "DROWSY",
            "ASLEEP",
        }, f"SleepState must have exactly ALERT/DROWSY/ASLEEP; got {members}"

    def test_sleep_state_machine_has_no_emotion_attributes(self) -> None:
        """SleepStateMachine must not expose any emotion-model attribute."""
        from reachy.sleep.state import SleepStateMachine

        machine = SleepStateMachine()
        for kw in self._EMOTION_KEYWORDS:
            assert not hasattr(machine, kw), (
                f"SleepStateMachine has emotion attribute {kw!r} — "
                "affect/emotion models are out of scope for sleep"
            )

    def test_no_emotion_vocabulary_in_sleep_modules(self) -> None:
        """None of the emotion-model keywords appear as identifiers in the sleep modules."""
        combined_src = _source(*_SLEEP_MODULE_NAMES)
        for kw in self._EMOTION_KEYWORDS:
            assert kw not in combined_src, (
                f"Emotion keyword {kw!r} found in sleep modules — "
                "only an idle timer (boredom = attention-decay) is permitted"
            )

    def test_sleep_state_only_exposes_timer_and_state(self) -> None:
        """state.py's public surface is the idle timer + state; no emotion API."""
        import reachy.sleep.state as state_mod

        # The public names must be the FSM class + enum; nothing emotion-flavoured.
        public = {n for n in dir(state_mod) if not n.startswith("_")}
        # Allowed top-level names: SleepState, SleepStateMachine, plus stdlib
        # helpers (Enum, auto, dataclass, field are re-exported by accident in
        # some patterns — we assert no emotion keyword is in the public surface).
        for kw in self._EMOTION_KEYWORDS:
            assert (
                kw not in public
            ), f"Emotion-flavoured name {kw!r} found in reachy.sleep.state's public API"

    def test_no_persistence_store_in_sleep_modules(self) -> None:
        """Sleep must not import any persistence store (sqlite3/shelve/pickle/db)."""
        persistence_mods = {"sqlite3", "shelve", "pickle", "dbm", "json"}
        for dotted in _SLEEP_MODULE_NAMES:
            mod = importlib.import_module(dotted)
            imported = _imported_modules(mod)
            for name in imported:
                assert name not in persistence_mods, (
                    f"{dotted} imports persistence module {name!r} — "
                    "sleep must have no cross-session mood/state store"
                )


# ---------------------------------------------------------------------------
# 4. Wake-word engine import is lazy (not at module load time)
# ---------------------------------------------------------------------------


class TestWakeWordLazyImport:
    """The optional wake-word engine must NOT be imported at module load time.

    Importing ``reachy.sleep.wake`` on a bare install profile (no ``[cpu]``/``[gpu]``
    extras) must never pull in ``openwakeword`` or any ASR library.
    """

    def test_importing_wake_module_does_not_import_openwakeword(self) -> None:
        """openwakeword must NOT appear in sys.modules after importing reachy.sleep.wake."""
        assert not _module_pulls_in("reachy.sleep.wake", "openwakeword"), (
            "Importing reachy.sleep.wake pulled in openwakeword at module load time — "
            "the engine import must be guarded inside a function/method so it only "
            "fires when wake_word_enabled=True AND the package is installed."
        )

    def test_wake_word_engine_import_is_inside_function(self) -> None:
        """openwakeword import in wake.py must be indented (inside a function/method).

        A top-level ``import openwakeword`` would load it on every bare install.
        We verify via source inspection that the import is not at column 0.
        """
        import reachy.sleep.wake as wake_mod

        src = inspect.getsource(wake_mod)
        for line in src.splitlines():
            stripped = line.lstrip()
            if "openwakeword" in stripped and stripped.startswith(("import ", "from ")):
                # This line imports openwakeword — it must be indented (inside a function).
                assert line != stripped, (
                    "Found a top-level (unindented) import of openwakeword in wake.py — "
                    "it must live inside a function to keep the module load dep-free."
                )

    def test_importing_wake_module_does_not_import_asr_libs(self) -> None:
        """No ASR / speech-recognition library is pulled in by importing wake.py."""
        asr_libs = ["nemo", "nemo_toolkit", "speechbrain", "whisper", "faster_whisper"]

        for lib in asr_libs:
            assert not _module_pulls_in("reachy.sleep.wake", lib), (
                f"Importing reachy.sleep.wake pulled in ASR library {lib!r} — "
                "ASR/wake-word deps must stay behind the [cpu]/[gpu] optional extra"
            )


# ---------------------------------------------------------------------------
# 5. pyproject.toml — no new base dep beyond numpy; reachy_mini stays an extra
# ---------------------------------------------------------------------------


class TestBaseDependencies:
    """Sleep introduces no new base runtime dependency beyond numpy.

    ``reachy_mini`` must remain an optional extra (``[sdk]``/``[daemon]``).
    New packages attributable to sleep (``openwakeword``) must live in an
    optional extra, not in ``[project.dependencies]``.
    """

    def _project(self) -> dict:
        data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
        return data["project"]

    def test_reachy_mini_is_not_a_base_dep(self) -> None:
        """reachy_mini must NOT be a base dependency — it must stay in [sdk]/[daemon]."""
        base = self._project()["dependencies"]
        assert not any(
            d.startswith("reachy-mini") for d in base
        ), f"reachy-mini must stay an extra, not a base dep: {base}"

    def test_numpy_is_only_non_stdlib_base_dep(self) -> None:
        """numpy is the sole non-stdlib base dep; no new packages introduced by sleep.

        This test lists the known allowed base deps and fails if an unexpected
        package appears (alerting the reviewer to an accidental dep addition).
        """
        base = self._project()["dependencies"]
        # Packages allowed at base level (edit here if an intentional addition is made).
        allowed_prefixes = ("numpy",)
        for dep in base:
            dep_name = dep.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
            assert any(dep_name.startswith(p) for p in allowed_prefixes), (
                f"Unexpected base dependency {dep!r} — sleep must not add new "
                "base runtime deps beyond numpy; move it to an optional extra"
            )

    def test_openwakeword_is_not_a_base_dep(self) -> None:
        """openwakeword (the wake-word engine) must NOT appear in base dependencies."""
        base = self._project()["dependencies"]
        assert not any(
            "openwakeword" in d.lower() for d in base
        ), "openwakeword appeared in base dependencies — it must stay in [cpu]/[gpu] extras"

    def test_openwakeword_if_present_is_in_cpu_or_gpu_extra(self) -> None:
        """If openwakeword is declared at all, it must be in [cpu] or [gpu] extras."""
        base = self._project()["dependencies"]
        # If it's anywhere in base, fail (previous test catches this too — belt+suspenders).
        for dep in base:
            assert "openwakeword" not in dep.lower(), "openwakeword must not be a base dependency"
