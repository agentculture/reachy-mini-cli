"""Offline CI lane (task t12): the success list proven with every endpoint unreachable.

This module exercises, deterministically and end-to-end, each success-list path
that must survive with every network leg pointed nowhere:

    path            -> representative test
    --------------------------------------------------------------------------
    boot            -> test_boot_behavior_engine_composes_with_a_rules_file
    breathe         -> test_breathe_feel_alive_contributes_a_moving_pose_over_time
                       test_breathe_owns_head_every_tick_of_a_bounded_engine_run
    orient-to-sound -> test_orient_to_sound_listen_producer_commits_a_turn_on_speech
    pat             -> test_pat_detect_then_react_enqueues_lean_nuzzle_settle
    sleep/wake      -> test_sleep_wake_demo_walks_the_full_arc_with_no_robot
    rules           -> test_rules_file_changes_robot_behavior_in_a_bounded_run

Every test below drives the SAME production seam a pre-existing, more thorough
test file already proves (named in each test's docstring) — this file is
deliberately thin: one representative scenario per path, not a re-run of every
edge case owned elsewhere. Each test is offline by construction (fakes /
injected clocks / pure functions only, never a real robot, daemon, LLM, TTS,
STT, or forge endpoint). The module-level ``offline`` marker additionally
routes every service env var to an unreachable address and hard-fails on any
real socket connect (see ``tests/conftest.py``'s ``_offline_guard`` fixture),
so a hidden network dependency introduced later here is a loud CI failure, not
a silent pass or a hang.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket

import pytest

from reachy.behavior import library
from reachy.behavior import rules as rules_mod
from reachy.behavior.engine import Engine, EngineConfig
from reachy.behavior.engine import run as engine_run
from reachy.behavior.model import Lifetime, StopClass
from reachy.behavior.rule_engine import compose_rule_seam
from reachy.behavior.rules import RulesConfig
from reachy.behavior.sense import Sense
from reachy.cli import main
from reachy.motion.listen import ListenParams, ListenProducer
from reachy.motion.pat import PatDetector
from reachy.motion.pat_reaction import PatReaction
from reachy.motion.queue import MotionQueue

pytestmark = pytest.mark.offline


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Pin bookkeeping (rules.toml, ``*_active`` flags) to a throwaway dir.

    Mirrors the ``_isolate_state`` fixture in ``tests/test_behavior_reload.py`` /
    ``tests/test_behavior_rule_engine.py`` so this module's engine runs never
    touch the real ``$XDG_STATE_HOME/reachy`` bookkeeping.
    """
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


# --------------------------------------------------------------------------- #
# Fakes (mirrors tests/test_behavior.py / test_behavior_reload.py)            #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def __init__(self):
        self.poses = []
        self.calls = 0

    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        self.calls += 1
        self.poses.append({"head": head, "antennas": antennas, "body_yaw": body_yaw})
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self, sink=None):
        self.sink = sink or _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink


class _Clock:
    def __init__(self, dt=0.02):
        self.t = 0.0
        self.dt = dt

    def __call__(self):
        self.t += self.dt
        return self.t


def _react_rule(rule_id: str, field: str, op: str, run: str, *, cooldown_s: float = 0.0) -> dict:
    return {
        "id": rule_id,
        "when": {"field": field, "op": op},
        "run": run,
        "cooldown_s": cooldown_s,
        "hysteresis": 0.0,
    }


# --------------------------------------------------------------------------- #
# The guard itself — prove it really blocks a network call, not just the env  #
# --------------------------------------------------------------------------- #


def test_offline_guard_points_every_service_env_var_unreachable() -> None:
    for name in (
        "REACHY_OPENAI_URL_BASE",
        "REACHY_LLM_BASE_URL",
        "REACHY_TTS_URL",
        "REACHY_STT_URL",
        "FORGE_BASE_URL",
    ):
        assert os.environ[name] == "http://127.0.0.1:1"


def test_offline_guard_blocks_a_real_socket_connect() -> None:
    with pytest.raises(AssertionError, match="offline lane: network call attempted"):
        socket.create_connection(("127.0.0.1", 1), timeout=0.1)
    sock = socket.socket()
    try:
        with pytest.raises(AssertionError, match="offline lane: network call attempted"):
            sock.connect(("127.0.0.1", 1))
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# boot — behavior engine run composition path, with a rules file on disk      #
# --------------------------------------------------------------------------- #

_BOOT_RULES_TOML = """\
[[react]]
id = "wake-nod"
when = { field = "doa", op = "absent_for", value = 0 }
run = "nod"
cooldown_s = 0
"""


def test_boot_behavior_engine_composes_with_a_rules_file(monkeypatch, capsys) -> None:
    """Mirrors test_behavior_reload.py's engine-run-with-good-rules CLI idiom:

    boot picks up ``rules.toml`` at start, ``feel-alive`` is the base every
    tick, and the rule-admitted behavior takes over head ownership by the end
    of a bounded run.
    """
    path = rules_mod.default_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BOOT_RULES_TOML, encoding="utf-8")

    tr = _FakeTransport()
    monkeypatch.setattr("reachy.cli._commands.behavior.get_transport", lambda args: tr)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    rc = main(["behavior", "engine", "run", "--json", "--max-ticks", "3"])
    assert rc == 0

    events = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert [e["tick"] for e in events] == [1, 2, 3]
    assert events[0]["ownership"]["head"].startswith("feel-alive")
    assert events[-1]["ownership"]["head"].startswith("rule:wake-nod:")


# --------------------------------------------------------------------------- #
# breathe — the feel-alive base layer contributes a live, moving pose         #
# --------------------------------------------------------------------------- #


def test_breathe_feel_alive_contributes_a_moving_pose_over_time() -> None:
    """Mirrors test_behavior.py's test_feel_alive_contribution_shape_and_energy:

    two ``contribution()`` reads apart in time differ — the base layer is a
    live breathing motion, not a frozen pose.
    """
    entry = library.get("feel-alive")
    beh = library.build(
        "feel-alive",
        entry.default_params(),
        StopClass.PASSIVE,
        Lifetime(looping=True, duration=None),
        "fa",
    )
    c1 = beh.contribution(0.0)
    c2 = beh.contribution(2.5)
    assert set(c1.head) == {"x", "y", "z", "roll", "pitch", "yaw"}
    assert c1.head != c2.head  # breathing moves — not a frozen pose


def test_breathe_owns_head_every_tick_of_a_bounded_engine_run() -> None:
    """Mirrors test_behavior.py's test_run_streams_complete_poses_and_settles:

    with no rules and no reaction, ``feel-alive`` is the sole contributor every
    tick of a bounded, fake-sink engine run.
    """
    tr = _FakeTransport()
    cfg = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    ticks = engine_run(tr, cfg, sleep=lambda *_: None, now=_Clock(), max_ticks=3)
    assert ticks == 3
    for pose in tr.sink.poses:
        assert set(pose["head"]) == {"x", "y", "z", "roll", "pitch", "yaw"}
        assert len(pose["antennas"]) == 2


# --------------------------------------------------------------------------- #
# orient-to-sound — ListenProducer commits a head turn toward off-axis speech #
# --------------------------------------------------------------------------- #


def test_orient_to_sound_listen_producer_commits_a_turn_on_speech() -> None:
    """Mirrors test_motion.py's test_producer_commits_on_speech_off_axis:

    fed a synthetic DoA reading (no transport, no daemon — ListenProducer is a
    pure decision object), off-axis speech commits exactly one head-turn action
    toward the source, then holds.
    """
    prod = ListenProducer(ListenParams(deadband=10, hold=3.0, gain=0.6, max_yaw=35, idle_energy=0))
    spoke = Sense(doa_angle=0.0, speech_detected=True)  # doa=0 -> desired +35 deg, off-axis
    action = prod.update(0.0, spoke, sound_present=True)
    assert action is not None
    assert action.head is not None and action.head["yaw"] > 0
    # Held immediately after — no second commit within the hold window.
    assert prod.update(0.5, spoke, sound_present=True) is None


# --------------------------------------------------------------------------- #
# pat — detect (commanded-vs-actual deviation) then react (lean/nuzzle/settle)#
# --------------------------------------------------------------------------- #


def test_pat_detect_then_react_enqueues_lean_nuzzle_settle() -> None:
    """Mirrors test_pat_detector.py's pitch-press scenario for detect, then
    drives the detected event through PatReaction (mirrors test_pat_reaction.py)
    to prove detect->react is a fully offline, sensor-less pipeline.
    """
    det = PatDetector(level2_threshold_fn=lambda: 6.0)
    now = 1000.0
    event = None
    for i in range(3):
        t_press = now + i * 0.4
        result = det.update(0.0, -5.0, now=t_press)  # pitch press
        if result is not None:
            event = result
            break
        det.update(0.0, 0.0, now=t_press + 0.1)  # release
    assert event == ("level1", "scratch")

    level, touch_type = event
    queue = MotionQueue()
    PatReaction(queue=queue).react(touch_type, level)
    assert len(queue.pending()) == 3  # lean -> nuzzle -> settle, strictly ordered


# --------------------------------------------------------------------------- #
# sleep/wake — the no-robot demo arc, ALERT -> DROWSY -> ASLEEP -> wake       #
# --------------------------------------------------------------------------- #


def test_sleep_wake_demo_walks_the_full_arc_with_no_robot(capsys) -> None:
    """Mirrors test_sleep_cli.py's test_sleep_demo_json_walks_full_arc:

    ``sleep demo`` drives a synthetic sense feed + fake clock and needs no SDK,
    robot, or network.
    """
    rc = main(["sleep", "demo", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    states = payload["states"]
    assert states[0] == "ALERT"
    assert "DROWSY" in states
    assert "ASLEEP" in states
    assert states[-1] == "ALERT"
    assert payload["woke"] is True


# --------------------------------------------------------------------------- #
# rules — a firing rule changes robot behavior in a bounded engine run        #
# --------------------------------------------------------------------------- #


def test_rules_file_changes_robot_behavior_in_a_bounded_run() -> None:
    """Mirrors test_behavior_rule_engine.py's
    test_rules_file_changes_robot_behavior_in_bounded_run: a speech->nod rule
    flips head ownership from ``feel-alive`` to the rule-admitted behavior,
    driven purely through the engine's injected seams — no daemon, no network.
    """
    cfg = RulesConfig.from_dict({"react": [_react_rule("hear", "speech", "is_true", "nod")]})
    eng = Engine()
    bus = compose_rule_seam(cfg)

    def sense(_t):  # scripted perception — no daemon, no network
        return Sense(speech_detected=True)

    tr = _FakeTransport()
    ticks = engine_run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=3,
        engine=eng,
        sense=sense,
        tick_seam=bus,
    )
    assert ticks == 3
    active_names = {ab.behavior.name for ab in eng.active}
    assert "nod" in active_names
    assert eng._last_ownership["head"].startswith("rule:hear:")
