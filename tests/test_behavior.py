"""Tests for the ``behavior`` noun group and the ``reachy.behavior`` package.

No real robot, daemon, or background process is involved: the engine runs against
a fake in-memory streaming sink, and the supervisor's subprocess
(``subprocess.Popen``), liveness (``os.kill`` / ``is_alive``), grace sleep, and
HTTP health check are monkeypatched. Every test pins bookkeeping into a throwaway
dir via ``REACHY_STATE_DIR`` so the real state dir is untouched.

The arbitration core, library, and control spool are pure / filesystem-only and
are exercised directly; the engine loop reuses the same injectable
``sleep`` / ``now`` / ``max_ticks`` seams as ``reachy.alive.run_loop``.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from reachy.behavior import control
from reachy.behavior import engine as E
from reachy.behavior import library
from reachy.behavior.arbitration import admit, arbitrate
from reachy.behavior.engine import Engine, EngineConfig
from reachy.behavior.model import CHANNELS, Behavior, Contribution, Lifetime, StopClass
from reachy.cli import main
from reachy.cli._errors import EXIT_ENV_ERROR, CliError


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


def _beh(name, cls, channels, *, looping=True, duration=None, bid=None) -> Behavior:
    return Behavior(
        id=bid or name,
        name=name,
        channels=frozenset(channels),
        stop_class=cls,
        lifetime=Lifetime(looping=looping, duration=duration),
        params={},
        fn=lambda t, p: Contribution(),
    )


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #


class _FakeSink:
    """Records every streamed pose; can fail a set number of tick-sends.

    The first call (the engine's connectivity preflight) always succeeds unless
    ``fail_preflight`` — so transient-error tests fail *ticks*, not the preflight.
    """

    def __init__(self, fail_times=0, fail_forever=False, fail_preflight=False):
        self.poses: list[dict] = []
        self.calls = 0
        self._ft = fail_times
        self._ff = fail_forever
        self._fail_preflight = fail_preflight

    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        self.calls += 1
        self.poses.append({"head": head, "antennas": antennas, "body_yaw": body_yaw})
        if self.calls == 1 and not self._fail_preflight:
            return {"status": "ok"}
        if self._ff or self._ft > 0:
            self._ft -= 1
            raise CliError(code=EXIT_ENV_ERROR, message="daemon gone", remediation="start it")
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self, sink=None):
        self.sink = sink or _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink


class _Clock:
    """A monotonic clock that advances a fixed dt each call (deterministic t_local)."""

    def __init__(self, dt=0.02):
        self.t = 0.0
        self.dt = dt

    def __call__(self):
        self.t += self.dt
        return self.t


# --------------------------------------------------------------------------- #
# model + arbitration                                                          #
# --------------------------------------------------------------------------- #


def test_arbitrate_priority_then_recency() -> None:
    old = _beh("a", StopClass.STOPPABLE, ["head"], bid="a")
    new = _beh("b", StopClass.STOPPABLE, ["head"], bid="b")
    owners = arbitrate([old, new])
    assert owners["head"].id == "b"  # same class -> newest wins
    strong = _beh("c", StopClass.UNSTOPPABLE, ["head"], bid="c")
    owners = arbitrate([strong, old, new])
    assert owners["head"].id == "c"  # higher priority beats recency


def test_arbitrate_passive_yields_to_non_passive() -> None:
    base = _beh("feel-alive", StopClass.PASSIVE, CHANNELS, bid="base")
    speak = _beh("speak", StopClass.STOPPABLE, ["head"], bid="speak")
    owners = arbitrate([base, speak])
    assert owners["head"].id == "speak"
    assert owners["antennas"].id == "base"  # passive keeps the unclaimed channels
    assert owners["body_yaw"].id == "base"


def test_arbitrate_empty_channel_is_none() -> None:
    owners = arbitrate([_beh("x", StopClass.STOPPABLE, ["antennas"])])
    assert owners["head"] is None and owners["body_yaw"] is None


def test_admit_stopping_evicts_stoppable_on_shared_channel() -> None:
    sway = _beh("antenna-sway", StopClass.STOPPABLE, ["antennas"], bid="sway")
    body = _beh("body", StopClass.STOPPABLE, ["body_yaw"], bid="body")
    seizer = _beh("seizer", StopClass.STOPPING, ["antennas", "body_yaw"], bid="seizer")
    result = admit(seizer, [sway, body])
    assert {b.id for b in result.evicted} == {"sway", "body"}
    assert result.blocked == []


def test_admit_unstoppable_blocks_new_stopping() -> None:
    held = _beh("thoughtful", StopClass.UNSTOPPABLE, ["head"], bid="held")
    stopper = _beh("stopper", StopClass.STOPPING, ["head"], bid="stopper")
    result = admit(stopper, [held])
    assert result.evicted == []  # cannot evict an unstoppable
    assert result.blocked == ["head"]  # and it does not get the channel either


def test_admit_passive_never_evicts_and_is_not_blocked() -> None:
    speak = _beh("speak", StopClass.STOPPABLE, ["head"], bid="speak")
    base = _beh("feel-alive", StopClass.PASSIVE, CHANNELS, bid="base")
    result = admit(base, [speak])
    assert result.evicted == [] and result.blocked == []


def test_admit_new_stoppable_does_not_evict_existing_stoppable() -> None:
    old = _beh("nod", StopClass.STOPPABLE, ["head"], bid="old")
    new = _beh("shake", StopClass.STOPPABLE, ["head"], bid="new")
    result = admit(new, [old])
    assert result.evicted == []  # only a stopping behavior evicts
    assert result.blocked == []  # newest of equal class owns the channel


def test_is_expired_one_shot_vs_until_stopped() -> None:
    one_shot = _beh("g", StopClass.STOPPABLE, ["head"], looping=False, duration=2.0)
    forever = _beh("s", StopClass.STOPPABLE, ["head"], looping=True, duration=None)
    assert not one_shot.is_expired(1.9) and one_shot.is_expired(2.0)
    assert not forever.is_expired(10_000.0)


def test_lifetime_validation() -> None:
    assert Lifetime(looping=False, duration=None).errors()  # one-shot needs a duration
    assert Lifetime(looping=True, duration=-1).errors()  # negative duration
    assert Lifetime(looping=True, duration=None).errors() == []  # until-stopped is fine


# --------------------------------------------------------------------------- #
# library                                                                      #
# --------------------------------------------------------------------------- #


def test_library_get_unknown_raises() -> None:
    with pytest.raises(CliError):
        library.get("does-not-exist")


def test_resolve_params_merges_and_validates() -> None:
    entry = library.get("antenna-sway")
    params = library.resolve_params(entry, {"amp": "25"})
    assert params["amp"] == 25.0 and params["period"] == entry.params["period"].default
    with pytest.raises(CliError):
        library.resolve_params(entry, {"nope": "1"})
    with pytest.raises(CliError):
        library.resolve_params(entry, {"amp": "not-a-number"})


def test_resolve_class_default_and_unknown() -> None:
    entry = library.get("speak")
    assert library.resolve_class(entry, None) is StopClass.STOPPABLE
    assert library.resolve_class(entry, "stopping") is StopClass.STOPPING
    with pytest.raises(CliError):
        library.resolve_class(entry, "bogus")


def test_resolve_lifetime_rules() -> None:
    looping_entry = library.get("nod")  # looping, no default duration
    # default for a looping behavior is until-stopped
    assert library.resolve_lifetime(
        looping_entry, once=False, loop=False, duration=None
    ) == Lifetime(looping=True, duration=None)
    # --once on a looping behavior needs an explicit duration
    with pytest.raises(CliError):
        library.resolve_lifetime(looping_entry, once=True, loop=False, duration=None)
    # one-shot entry falls back to its default duration
    one_shot = library.get("gaze-hold")
    lt = library.resolve_lifetime(one_shot, once=False, loop=False, duration=None)
    assert lt.looping is False and lt.duration == one_shot.default_duration
    with pytest.raises(CliError):
        library.resolve_lifetime(one_shot, once=True, loop=True, duration=1.0)


def test_feel_alive_contribution_shape_and_energy() -> None:
    entry = library.get("feel-alive")
    beh = library.build(
        "feel-alive",
        entry.default_params(),
        StopClass.PASSIVE,
        Lifetime(looping=True, duration=None),
        "fa",
    )
    c = beh.contribution(1.23)
    assert set(c.head) == {"x", "y", "z", "roll", "pitch", "yaw"}
    assert len(c.antennas) == 2
    still = entry.default_params()
    still["energy"] = 0.0
    beh0 = library.build(
        "feel-alive", still, StopClass.PASSIVE, Lifetime(looping=True, duration=None), "fa0"
    )
    c0 = beh0.contribution(1.23)
    assert c0.head["z"] == 0.0 and c0.antennas == (0.0, 0.0)


# --------------------------------------------------------------------------- #
# Engine: add / stop / compose (unit, no loop)                                 #
# --------------------------------------------------------------------------- #


def test_engine_add_assigns_ids_and_reports() -> None:
    eng = Engine()
    out = eng.add(
        "speak",
        library.get("speak").default_params(),
        StopClass.STOPPABLE,
        Lifetime(looping=True, duration=None),
        now=0.0,
    )
    assert out["ok"] and out["id"] == "speak-1" and out["channels"] == ["head"]
    out2 = eng.add(
        "nod",
        library.get("nod").default_params(),
        StopClass.STOPPABLE,
        Lifetime(looping=True, duration=None),
        now=0.0,
    )
    assert out2["id"] == "nod-2"


def test_engine_stop_all_keeps_base_layer() -> None:
    eng = Engine()
    eng.seed_base_layer(now=0.0, energy=1.0)
    eng.add(
        "speak",
        library.get("speak").default_params(),
        StopClass.STOPPABLE,
        Lifetime(looping=True, duration=None),
        now=0.0,
    )
    out = eng.stop("all")
    assert out["count"] == 1
    names = [ab.behavior.name for ab in eng.active]
    assert names == ["feel-alive"]  # base layer survives 'stop all'


def test_engine_stop_by_name_and_unknown() -> None:
    eng = Engine()
    eng.add(
        "nod",
        library.get("nod").default_params(),
        StopClass.STOPPABLE,
        Lifetime(looping=True, duration=None),
        now=0.0,
    )
    assert eng.stop("nod")["count"] == 1
    out = eng.stop("ghost")
    assert out["count"] == 0 and out["unknown"] is True


def test_engine_channels_override() -> None:
    eng = Engine()
    out = eng.add(
        "antenna-sway",
        library.get("antenna-sway").default_params(),
        StopClass.STOPPING,
        Lifetime(looping=True, duration=None),
        now=0.0,
        channels=["antennas", "body_yaw"],
    )
    assert set(out["channels"]) == {"antennas", "body_yaw"}


def test_compose_tick_drops_expired_and_is_complete() -> None:
    eng = Engine()
    eng.add(
        "gaze-hold",
        library.get("gaze-hold").default_params(),
        StopClass.STOPPABLE,
        Lifetime(looping=False, duration=1.0),
        now=0.0,
    )
    tick = eng.compose_tick(0.5)
    assert tick["ownership"]["head"].startswith("gaze-hold")
    assert set(tick["pose"]["head"]) == {"x", "y", "z", "roll", "pitch", "yaw"}
    tick2 = eng.compose_tick(1.5)  # past its duration
    assert tick2["expired"] and tick2["ownership"]["head"] is None
    assert eng.active == []


def test_apply_bad_command_does_not_raise() -> None:
    eng = Engine()
    assert eng.apply({"op": "frobnicate"}, 0.0)["ok"] is False
    assert (
        eng.apply(
            {
                "op": "add",
                "name": "ghost",
                "class": "stoppable",
                "lifetime": {"looping": True, "duration": None},
            },
            0.0,
        )["ok"]
        is False
    )


# --------------------------------------------------------------------------- #
# Engine: the run loop                                                         #
# --------------------------------------------------------------------------- #


def test_run_streams_complete_poses_and_settles() -> None:
    tr = _FakeTransport()
    cfg = EngineConfig(compose_hz=50, base_layer=True, settle=True)
    ticks = E.run(tr, cfg, sleep=lambda *_: None, now=_Clock(), max_ticks=3)
    assert ticks == 3
    # preflight + 3 ticks + settle == 5 sink calls
    assert tr.sink.calls == 5
    for pose in tr.sink.poses:
        assert set(pose["head"]) == {"x", "y", "z", "roll", "pitch", "yaw"}
        assert len(pose["antennas"]) == 2
    # settle is neutral
    assert tr.sink.poses[-1]["head"]["yaw"] == 0.0 and tr.sink.poses[-1]["body_yaw"] == 0.0


def test_run_no_base_layer_streams_neutral() -> None:
    tr = _FakeTransport()
    cfg = EngineConfig(compose_hz=50, base_layer=False, settle=False)
    E.run(tr, cfg, sleep=lambda *_: None, now=_Clock(), max_ticks=2)
    # nothing owns any channel -> every pose neutral
    for pose in tr.sink.poses:
        assert pose["body_yaw"] == 0.0
        assert all(v == 0.0 for v in pose["head"].values())


def test_run_preflight_failure_propagates() -> None:
    tr = _FakeTransport(_FakeSink(fail_preflight=True, fail_forever=True))
    with pytest.raises(CliError):
        E.run(tr, EngineConfig(settle=False), sleep=lambda *_: None, now=_Clock(), max_ticks=1)


def test_run_tolerates_transient_then_recovers() -> None:
    tr = _FakeTransport(_FakeSink(fail_times=2))  # 2 tick-sends fail, then ok
    cfg = EngineConfig(compose_hz=50, max_errors=5, settle=False)
    ticks = E.run(tr, cfg, sleep=lambda *_: None, now=_Clock(), max_ticks=4)
    assert ticks == 4


def test_run_gives_up_after_max_consecutive_errors() -> None:
    tr = _FakeTransport(_FakeSink(fail_forever=True))
    cfg = EngineConfig(compose_hz=50, max_errors=3, settle=False)
    with pytest.raises(CliError):
        E.run(tr, cfg, sleep=lambda *_: None, now=_Clock(), max_ticks=100)


def test_run_applies_spool_commands_and_publishes_state() -> None:
    class _NoReset(control.CommandSpool):
        def reset(self):  # keep the pre-staged command for this synchronous test
            pass

    spool = _NoReset()
    control.submit(
        "add",
        name="speak",
        params=library.get("speak").default_params(),
        lifetime={"looping": True, "duration": None},
        channels=None,
        **{"class": "stoppable"},
    )
    tr = _FakeTransport()
    E.run(
        tr,
        EngineConfig(base_layer=True),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=3,
        control=spool,
    )
    state = control.read_state()
    assert state["ownership"]["head"].startswith("speak")
    assert state["ownership"]["antennas"].startswith("feel-alive")


# --------------------------------------------------------------------------- #
# Control spool                                                                #
# --------------------------------------------------------------------------- #


def test_spool_submit_drain_roundtrip() -> None:
    cid = control.submit("stop", target="all")
    spool = control.CommandSpool()
    cmds = spool.drain()
    assert len(cmds) == 1 and cmds[0]["cmd_id"] == cid and cmds[0]["target"] == "all"
    assert spool.drain() == []  # drained files are removed


def test_spool_drain_skips_garbage(tmp_path) -> None:
    (control.commands_dir() / "junk.json").write_text("{not json", encoding="utf-8")
    (control.commands_dir() / "ignore.txt").write_text("x", encoding="utf-8")
    control.submit("list")
    cmds = control.CommandSpool().drain()
    assert [c["op"] for c in cmds] == ["list"]  # garbage removed, non-json ignored


def test_spool_result_roundtrip_and_timeout() -> None:
    spool = control.CommandSpool()
    spool.write_result("abc", {"ok": True, "id": "speak-1"})
    assert control.await_result("abc", timeout=0.1)["id"] == "speak-1"
    # consumed: a second await times out
    assert control.await_result("abc", timeout=0, sleep=lambda *_: None) is None


def test_spool_reset_clears(tmp_path) -> None:
    control.submit("list")
    control.CommandSpool().write_state({"x": 1})
    control.CommandSpool().reset()
    assert control.read_state() is None
    assert control.CommandSpool().drain() == []


# --------------------------------------------------------------------------- #
# CLI: list / overview / run / stop / status                                   #
# --------------------------------------------------------------------------- #


def test_behavior_list_json(capsys) -> None:
    assert main(["behavior", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    names = [b["name"] for b in payload["behaviors"]]
    assert "speak" in names and "feel-alive" in names


def test_behavior_overview_text_and_json(capsys) -> None:
    assert main(["behavior", "overview"]) == 0
    assert "# reachy-mini-cli behavior" in capsys.readouterr().out
    assert main(["behavior", "overview", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["subject"] == "reachy-mini-cli behavior"


def test_bare_behavior_prints_overview(capsys) -> None:
    assert main(["behavior"]) == 0
    assert capsys.readouterr().out.strip()


def test_behavior_bad_flag_structured_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["behavior", "status", "--bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err


def test_run_submits_and_reports(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.supervisor.ensure_running",
        lambda **k: {"status": "already-running"},
    )
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.control.await_result",
        lambda cid, **k: {"ok": True, "op": "add", "id": "speak-1", "evicted": [], "blocked": []},
    )
    rc = main(["behavior", "run", "speak", "--duration", "5", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["id"] == "speak-1"
    # a command file was actually written to the spool
    assert any(p.suffix == ".json" for p in control.commands_dir().iterdir())


def test_run_unknown_param_is_user_error(monkeypatch, capsys) -> None:
    # validation happens before the engine is touched
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.supervisor.ensure_running",
        lambda **k: pytest.fail("must validate before ensuring the engine"),
    )
    rc = main(["behavior", "run", "speak", "--set", "wobble=1"])
    assert rc == 1
    assert "unknown parameter" in capsys.readouterr().err


def test_run_no_engine_reports_unconfirmed(capsys) -> None:
    rc = main(["behavior", "run", "nod", "--loop", "--no-ensure-engine", "--await-timeout", "0"])
    assert rc == 0
    assert "did not confirm" in capsys.readouterr().out


def test_stop_submits(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.control.await_result",
        lambda cid, **k: {"ok": True, "op": "stop", "stopped": ["speak-1"], "count": 1},
    )
    assert main(["behavior", "stop", "speak-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_status_without_engine(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.behavior.supervisor.health_ok", lambda *a, **k: False)
    rc = main(["behavior", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["engine"]["process"] == "stopped"
    assert payload["ownership"] == {ch: None for ch in CHANNELS}


# --------------------------------------------------------------------------- #
# CLI: engine foreground + supervisor                                          #
# --------------------------------------------------------------------------- #


def test_engine_run_foreground_json(monkeypatch, capsys) -> None:
    tr = _FakeTransport()
    monkeypatch.setattr("reachy.cli._commands.behavior.get_transport", lambda args: tr)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    rc = main(["behavior", "engine", "run", "--json", "--max-ticks", "2"])
    assert rc == 0
    events = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert [e["tick"] for e in events] == [1, 2]
    assert events[0]["ownership"]["head"].startswith("feel-alive")


def test_status_reads_published_state(monkeypatch, capsys) -> None:
    tr = _FakeTransport()
    monkeypatch.setattr("reachy.cli._commands.behavior.get_transport", lambda args: tr)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    main(["behavior", "engine", "run", "--max-ticks", "2"])
    capsys.readouterr()
    monkeypatch.setattr("reachy.behavior.supervisor.health_ok", lambda *a, **k: True)
    main(["behavior", "status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ownership"]["head"].startswith("feel-alive")


class _FakePopen:
    returncode = None

    def __init__(self, cmd, **kwargs):
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 6262

    def poll(self):
        return self.returncode


def _popen_factory(box):
    def _popen(cmd, **kwargs):
        proc = _FakePopen(cmd, **kwargs)
        box.append(proc)
        return proc

    return _popen


def test_engine_start_spawns(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr("reachy.behavior.supervisor.health_ok", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    rc = main(["behavior", "engine", "start", "--compose-hz", "40", "--energy", "0.5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: started" in out and "pid: 6262" in out
    assert (tmp_path / "behavior" / "engine.pid").read_text().strip() == "6262"
    cmd = procs[0].cmd
    assert cmd[1:6] == ["-m", "reachy", "behavior", "engine", "run"]
    assert cmd[cmd.index("--compose-hz") + 1] == "40.0"


def test_engine_start_refuses_when_daemon_unreachable(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.behavior.supervisor.health_ok", lambda *a, **k: False)

    def _no_spawn(cmd, **kwargs):
        raise AssertionError("must not spawn when the daemon is unreachable")

    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    rc = main(["behavior", "engine", "start"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "daemon start" in err


def test_engine_start_idempotent(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "behavior").mkdir()
    (tmp_path / "behavior" / "engine.pid").write_text("6262")
    monkeypatch.setattr("reachy.behavior.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    assert main(["behavior", "engine", "start"]) == 0
    assert "already-running" in capsys.readouterr().out


def test_engine_stop_sigterm(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "behavior").mkdir()
    (tmp_path / "behavior" / "engine.pid").write_text("6262")
    state = {"alive": True}
    monkeypatch.setattr("reachy.behavior.supervisor.is_alive", lambda pid: state["alive"])
    monkeypatch.setattr("reachy.behavior.supervisor._is_our_process", lambda pid: True)

    def _kill(pid, sig):
        state["alive"] = False

    monkeypatch.setattr("os.kill", _kill)
    rc = main(["behavior", "engine", "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stopped" in out and "SIGTERM" in out
    assert not (tmp_path / "behavior" / "engine.pid").exists()


def test_engine_stop_not_running(capsys) -> None:
    assert main(["behavior", "engine", "stop"]) == 0
    assert "not running" in capsys.readouterr().out


def test_engine_status_running_healthy(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "behavior").mkdir()
    (tmp_path / "behavior" / "engine.pid").write_text("6262")
    monkeypatch.setattr("reachy.behavior.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.behavior.supervisor.health_ok", lambda *a, **k: True)
    assert main(["behavior", "engine", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "running" and payload["daemon"] == "healthy"


def test_engine_stop_refuses_reused_pid(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "behavior").mkdir()
    (tmp_path / "behavior" / "engine.pid").write_text("6262")
    monkeypatch.setattr("reachy.behavior.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.behavior.supervisor._is_our_process", lambda pid: False)
    killed: list = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(sig))
    rc = main(["behavior", "engine", "stop"])
    assert rc == 0
    assert "reused" in capsys.readouterr().out and killed == []


def test_engine_overview(capsys) -> None:
    assert main(["behavior", "engine", "overview", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["subject"] == "reachy-mini-cli behavior engine"


# --------------------------------------------------------------------------- #
# Qodo follow-ups                                                              #
# --------------------------------------------------------------------------- #


def test_interruptible_sleep_never_overshoots() -> None:
    # A 0.3 s gap with a 0.25 s slice must sleep 0.25 + 0.05, never 0.25 + 0.25.
    from reachy.looputil import interruptible_sleep

    slept: list[float] = []
    interruptible_sleep(0.3, {"flag": False}, slept.append, slice_seconds=0.25)
    assert sum(slept) == pytest.approx(0.3)
    assert max(slept) <= 0.25 + 1e-9


def test_run_forwards_engine_flags_to_autostart(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.supervisor.ensure_running",
        lambda **k: seen.update(k) or {"status": "already-running"},
    )
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.control.await_result",
        lambda cid, **k: {"ok": True, "id": "speak-1"},
    )
    rc = main(
        [
            "behavior",
            "run",
            "speak",
            "--duration",
            "5",
            "--no-base-layer",
            "--no-settle",
            "--compose-hz",
            "30",
        ]
    )
    assert rc == 0
    assert seen["base_layer"] is False and seen["settle"] is False
    assert seen["compose_hz"] == 30.0
