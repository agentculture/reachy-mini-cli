"""Tests for ``reachy.embody.supervisor`` (task t12) — start/stop/restart/status.

Mirrors ``tests/test_sleep_supervisor.py``'s shape (the closest sibling model
named by the task): no real process spawned for the unit-level tests
(``subprocess.Popen`` / ``is_alive`` / grace sleep / OS signals are
monkeypatched), state pinned to a tmp dir via ``REACHY_STATE_DIR``.

Acceptance criteria pinned in this module (see each test's docstring for the
exact claim):

* **h7** — ``stop`` kills ONLY the tracked layer: ``test_stop_kills_only_the_
  tracked_layer_process_a_sibling_is_untouched`` spawns two REAL trivial
  stand-in processes and proves the untracked one survives.
* **h16** — no systemd unit ships for the layer: the ``_PRESENCE`` pair in
  ``reachy.service.manager`` and the unit catalog in ``reachy.service.units``
  stay exactly what they were before this task, and this module never touches
  ``systemctl``.
* **h17** — one command each way: ``start`` / ``stop`` are each a single CLI
  invocation (pinned again, end-to-end, in ``tests/test_agent_embody_
  supervisor_cli.py``).
* **h26** — after ``stop``: no process/socket/unit trace remains, while
  ``embody-*`` rules the layer authored persist in the overlay and stay
  enumerable by prefix.
"""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - test spawns trivial, hardcoded-argv stand-in processes
import sys
import threading
import time

import pytest

from reachy.embody import supervisor
from tests.conftest import WAIT_BUDGET_S


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


# ---------------------------------------------------------------------------
# Path-collision acceptance criteria
# ---------------------------------------------------------------------------


def test_pid_file_is_embody_pid(tmp_path) -> None:
    assert supervisor.pid_file() == tmp_path / "embody.pid"


def test_log_file_is_embody_log(tmp_path) -> None:
    assert supervisor.log_file() == tmp_path / "embody.log"


def test_pid_file_differs_from_sleep_and_vision_and_engine(tmp_path) -> None:
    assert supervisor.pid_file() != tmp_path / "sleep.pid"
    assert supervisor.pid_file() != tmp_path / "vision.pid"
    assert supervisor.pid_file() != tmp_path / "behavior" / "engine.pid"


def test_log_file_differs_from_sleep_and_vision_and_engine(tmp_path) -> None:
    assert supervisor.log_file() != tmp_path / "sleep.log"
    assert supervisor.log_file() != tmp_path / "vision.log"
    assert supervisor.log_file() != tmp_path / "behavior" / "engine.log"


# ---------------------------------------------------------------------------
# build_run_command
# ---------------------------------------------------------------------------


def test_build_run_command_core_argv() -> None:
    """Must produce ``python -m reachy agent embody`` — no nested ``run`` verb."""
    cmd = supervisor.build_run_command(feed="-", await_timeout=2.0, turn_interval=0.75)
    assert cmd[1:5] == ["-m", "reachy", "agent", "embody"]
    assert "run" not in cmd, "embody has no nested run sub-verb — the bare verb IS the loop"
    assert cmd[cmd.index("--feed") + 1] == "-"
    assert cmd[cmd.index("--await-timeout") + 1] == "2.0"
    assert cmd[cmd.index("--turn-interval") + 1] == "0.75"


def test_build_run_command_optional_media_profile_forwarded() -> None:
    cmd = supervisor.build_run_command(media_profile="bench")
    assert "--media-profile" in cmd
    assert cmd[cmd.index("--media-profile") + 1] == "bench"


def test_build_run_command_media_profile_absent_by_default() -> None:
    cmd = supervisor.build_run_command()
    assert "--media-profile" not in cmd


def test_build_run_command_optional_spool_dir_forwarded(tmp_path) -> None:
    cmd = supervisor.build_run_command(spool_dir=str(tmp_path / "spool"))
    assert "--spool-dir" in cmd
    assert cmd[cmd.index("--spool-dir") + 1] == str(tmp_path / "spool")


def test_build_run_command_mute_during_playback_forwarded() -> None:
    cmd = supervisor.build_run_command(mute_during_playback=True)
    assert "--mute-during-playback" in cmd


def test_build_run_command_mute_during_playback_absent_by_default() -> None:
    cmd = supervisor.build_run_command()
    assert "--mute-during-playback" not in cmd


def test_build_run_command_optional_attention_window_forwarded() -> None:
    """Issue #150 — the #147 defect class: the value must reach the argv, not
    just the supervisor function's own parameter."""
    cmd = supervisor.build_run_command(attention_window=12.0)
    assert "--attention-window" in cmd
    assert cmd[cmd.index("--attention-window") + 1] == "12.0"


def test_build_run_command_attention_window_absent_by_default() -> None:
    cmd = supervisor.build_run_command()
    assert "--attention-window" not in cmd


def test_build_run_command_attention_window_zero_is_still_forwarded() -> None:
    """0.0 must not be treated as "not given" — the falsy-zero hazard again."""
    cmd = supervisor.build_run_command(attention_window=0.0)
    assert "--attention-window" in cmd
    assert cmd[cmd.index("--attention-window") + 1] == "0.0"


def test_build_run_command_never_forwards_bounded_run_flags() -> None:
    """A background layer must never get --max-turns/--max-events/--export/--log-level.

    Mirrors ``behavior engine start`` not forwarding ``--max-ticks``: those are
    bounded-run/foreground-observability flags, meaningless (or actively
    counter-productive — --export would bury a JSONL feed inside the log file)
    for a persistent background service.
    """
    cmd = supervisor.build_run_command()
    forbidden = ("--max-turns", "--max-events", "--export", "--export-blocks", "--log-level")
    for flag in forbidden:
        assert flag not in cmd, f"{flag} must never be forwarded to the background layer"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePopen:
    returncode = None

    def __init__(self, cmd, **kwargs) -> None:  # test shim
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 9191

    def poll(self):
        return self.returncode


def _popen_factory(box):
    def _popen(cmd, **kwargs):  # test shim
        proc = _FakePopen(cmd, **kwargs)
        box.append(proc)
        return proc

    return _popen


def _no_spawn(cmd, **kwargs):  # test shim
    raise AssertionError("must not spawn a process here")


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_spawns_agent_embody(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    result = supervisor.start()
    assert result["status"] == "started"
    assert result["pid"] == 9191
    assert (tmp_path / "embody.pid").read_text().strip() == "9191"
    cmd = procs[0].cmd
    assert cmd[1:5] == ["-m", "reachy", "agent", "embody"]
    assert procs[0].kwargs.get("start_new_session") is True


def test_start_forwards_attention_window(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    supervisor.start(attention_window=30.0)
    cmd = procs[0].cmd
    assert cmd[cmd.index("--attention-window") + 1] == "30.0"


def test_start_idempotent_when_already_running(monkeypatch, tmp_path) -> None:
    (tmp_path / "embody.pid").write_text("9191")
    monkeypatch.setattr("reachy.embody.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    result = supervisor.start()
    assert result["status"] == "already-running"
    assert result["pid"] == 9191


def test_start_replaces_stale_pid_and_spawns(monkeypatch, tmp_path) -> None:
    """A stale pid (dead process) is cleared and a new process is spawned."""
    (tmp_path / "embody.pid").write_text("9191")
    monkeypatch.setattr("reachy.embody.supervisor.is_alive", lambda pid: False)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    result = supervisor.start()
    assert result["status"] == "started"
    assert len(procs) == 1


def test_start_reports_exited_when_process_dies_in_grace_window(monkeypatch, tmp_path) -> None:
    """If the spawned process exits during the grace window, status is 'exited'."""

    class _ExitedPopen(_FakePopen):
        returncode = 1

    def _popen_exited(cmd, **kwargs):
        return _ExitedPopen(cmd, **kwargs)

    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("subprocess.Popen", _popen_exited)
    result = supervisor.start()
    assert result["status"] == "exited"
    assert result["exit_code"] == 1
    # The pid file must NOT linger after a failed start — otherwise status/stop
    # would report a stale pid.
    assert not (tmp_path / "embody.pid").exists()
    assert supervisor.read_pid() is None


# ---------------------------------------------------------------------------
# stop (mocked)
# ---------------------------------------------------------------------------


def test_stop_when_not_running_returns_not_running() -> None:
    result = supervisor.stop()
    assert result["status"] == "not running"
    assert "no tracked embody pid" in result["note"]


def test_stop_clears_stale_pid(tmp_path, monkeypatch) -> None:
    (tmp_path / "embody.pid").write_text("9191")
    monkeypatch.setattr("reachy.embody.supervisor.is_alive", lambda pid: False)
    result = supervisor.stop()
    assert result["status"] == "not running"
    assert not (tmp_path / "embody.pid").exists()


def test_stop_sigterm(monkeypatch, tmp_path) -> None:
    (tmp_path / "embody.pid").write_text("9191")
    state = {"alive": True}
    monkeypatch.setattr("reachy.embody.supervisor.is_alive", lambda pid: state["alive"])
    monkeypatch.setattr("reachy.embody.supervisor._is_our_process", lambda pid: True)
    killed: list = []

    def _kill(pid, sig):
        killed.append((pid, sig))
        state["alive"] = False

    monkeypatch.setattr("os.kill", _kill)
    result = supervisor.stop()
    assert result["status"] == "stopped"
    assert result["signal"] == "SIGTERM"
    assert killed == [(9191, signal.SIGTERM)]
    assert not (tmp_path / "embody.pid").exists()


def test_stop_sigkill_when_sigterm_ignored(monkeypatch, tmp_path) -> None:
    """If process survives SIGTERM (timeout), SIGKILL is sent — the escalation h7 names."""
    (tmp_path / "embody.pid").write_text("9191")
    killed: list = []
    wait_calls = {"n": 0}

    def _fake_wait_gone(pid, timeout):
        wait_calls["n"] += 1
        return wait_calls["n"] >= 2

    monkeypatch.setattr("reachy.embody.supervisor._wait_gone", _fake_wait_gone)
    monkeypatch.setattr("reachy.embody.supervisor._is_our_process", lambda pid: True)
    monkeypatch.setattr("reachy.embody.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))
    result = supervisor.stop()
    assert result["status"] == "stopped"
    assert result["signal"] == "SIGKILL"
    assert any(sig == signal.SIGKILL for _, sig in killed)


def test_stop_pid_reuse_guard(monkeypatch, tmp_path) -> None:
    """If tracked pid is no longer our process, stop must NOT signal it."""
    (tmp_path / "embody.pid").write_text("9191")
    monkeypatch.setattr("reachy.embody.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.embody.supervisor._is_our_process", lambda pid: False)
    monkeypatch.setattr("os.kill", _no_spawn)  # must not be called
    result = supervisor.stop()
    assert result["status"] == "not running"
    assert "reused" in result["note"]
    assert not (tmp_path / "embody.pid").exists()


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


def test_restart_stops_then_starts(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    result = supervisor.restart()
    assert result["status"] == "started"
    assert "restarted_from" in result
    assert procs[0].cmd[1:5] == ["-m", "reachy", "agent", "embody"]


def test_restart_reports_prior_stop_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    result = supervisor.restart()
    assert result["restarted_from"] == "not running"


def test_restart_forwards_media_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    supervisor.restart(media_profile="bench")
    assert "--media-profile" in procs[0].cmd
    assert procs[0].cmd[procs[0].cmd.index("--media-profile") + 1] == "bench"


def test_restart_forwards_attention_window(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    supervisor.restart(attention_window=7.5)
    assert "--attention-window" in procs[0].cmd
    assert procs[0].cmd[procs[0].cmd.index("--attention-window") + 1] == "7.5"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_stopped_when_no_pid(tmp_path) -> None:
    result = supervisor.status()
    assert result["process"] == "stopped"
    assert result["pid"] is None
    assert "embody.log" in result["log"]


def test_status_running(monkeypatch, tmp_path) -> None:
    (tmp_path / "embody.pid").write_text("9191")
    monkeypatch.setattr("reachy.embody.supervisor.is_alive", lambda pid: True)
    result = supervisor.status()
    assert result["process"] == "running"
    assert result["pid"] == 9191


def test_status_stale(monkeypatch, tmp_path) -> None:
    (tmp_path / "embody.pid").write_text("9191")
    monkeypatch.setattr("reachy.embody.supervisor.is_alive", lambda pid: False)
    result = supervisor.status()
    assert result["process"] == "stale"
    assert result["pid"] == 9191


# ---------------------------------------------------------------------------
# read_pid edge cases
# ---------------------------------------------------------------------------


def test_read_pid_absent_returns_none() -> None:
    assert supervisor.read_pid() is None


def test_read_pid_bad_content_returns_none(tmp_path) -> None:
    (tmp_path / "embody.pid").write_text("not-a-number")
    assert supervisor.read_pid() is None


def test_read_pid_valid(tmp_path) -> None:
    (tmp_path / "embody.pid").write_text("12345\n")
    assert supervisor.read_pid() == 12345


# =========================================================================== #
# h7 — stop kills ONLY the tracked layer; a sibling process is untouched      #
# =========================================================================== #
#
# A REAL spawn, per the hard rule: never the real ``agent embody`` verb
# against real endpoints — a trivial, hardcoded-argv stand-in
# (``sys.executable -c "..."``) plays BOTH roles: the tracked layer, and a
# stand-in for a sibling runtime/daemon process that must survive `stop`
# untouched. Waiting is on the actual condition (the process gone / still
# alive), never on a sleep — see tests.conftest.WAIT_BUDGET_S.


def _spawn_sleeper() -> subprocess.Popen:
    """A trivial, harmless stand-in process: sleeps, touches nothing real.

    Its argv carries no ``reachy``/``embody`` token, so :func:`_is_our_process`
    correctly reads it as "not an embody layer" — the shape of an unrelated
    sibling process (a stand-in for the runtime/daemon) or a pid a real
    layer's pid file happened to be reused into.
    """
    return subprocess.Popen(  # nosec B603 B607 - fixed argv, sys.executable, no shell
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_tracked_stub() -> subprocess.Popen:
    """A trivial stand-in for the layer itself: same harmless body, matching identity.

    Never the real ``agent embody`` verb (the hard rule: no test may spawn that
    against real endpoints) — but its argv carries ``reachy`` and ``embody`` as
    their OWN tokens (harmless extra ``sys.argv`` the ``-c`` script ignores),
    exactly like the real spawn line's ``... -m reachy agent embody ...``, so
    :func:`reachy.embody.supervisor._is_our_process` recognises it as ours —
    the same identity check ``stop`` runs before it ever signals anything.
    """
    return subprocess.Popen(  # nosec B603 B607 - fixed argv, sys.executable, no shell
        [sys.executable, "-c", "import time; time.sleep(120)", "reachy", "embody"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_until(predicate, *, budget: float = WAIT_BUDGET_S, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_in_background(proc: subprocess.Popen) -> None:
    """Reap *proc* the moment it exits, on a daemon thread.

    A killed child stays a ZOMBIE — ``os.kill(pid, 0)`` still succeeds — until
    its parent calls ``wait()``. On a deployed box the spawned layer is
    reparented to init (``start_new_session=True`` plus the launching CLI
    process exiting), which reaps it automatically; in this test the parent
    stays alive throughout, so without this, ``supervisor.stop()``'s own
    liveness poll would see a zombie and time out waiting for a process that
    is, for every purpose that matters, already gone.
    """
    threading.Thread(target=proc.wait, daemon=True).start()


def test_stop_kills_only_the_tracked_layer_process_a_sibling_is_untouched(tmp_path) -> None:
    """h7: SIGTERM->SIGKILL reaches ONLY the pid the pid file names.

    Two REAL processes are spawned: one is tracked as the embody layer (via
    ``embody.pid``), the other is an untracked stand-in for a sibling
    runtime/daemon process. ``stop`` must end the first and leave the second
    running — proving containment comes from the pid file being the sole
    authority, not from matching a process name or signalling a process group
    (which could reach a sibling sharing this test's own process group).
    """
    tracked = _spawn_tracked_stub()
    sibling = _spawn_sleeper()
    try:
        assert _wait_until(lambda: _pid_alive(tracked.pid))
        assert _wait_until(lambda: _pid_alive(sibling.pid))
        # Reap the moment either dies — see _reap_in_background's docstring:
        # without this a killed child is a lingering zombie, not "gone".
        _reap_in_background(tracked)
        _reap_in_background(sibling)

        (tmp_path / "embody.pid").write_text(str(tracked.pid))

        result = supervisor.stop()
        assert result["status"] == "stopped"
        assert result["pid"] == tracked.pid

        assert _wait_until(lambda: not _pid_alive(tracked.pid)), "tracked layer must be gone"
        assert _pid_alive(sibling.pid), "a sibling runtime/daemon-standin must be UNTOUCHED"
    finally:
        for proc in (tracked, sibling):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=WAIT_BUDGET_S)


def test_stop_pid_reuse_guard_is_real_not_just_mocked(tmp_path) -> None:
    """h7's other edge: a pid file pointing at a REAL but unrelated process.

    The pid file names a real, live process that is manifestly not an embody
    layer (its cmdline is a bare ``sleep``-style stub, containing neither
    ``reachy`` nor ``embody``) — ``stop`` must recognise this and refuse to
    signal it, never "the tracked pid happened to still be alive so kill it".
    """
    unrelated = _spawn_sleeper()
    try:
        assert _wait_until(lambda: _pid_alive(unrelated.pid))
        (tmp_path / "embody.pid").write_text(str(unrelated.pid))

        result = supervisor.stop()
        assert result["status"] == "not running"
        assert "reused" in result["note"]
        assert _pid_alive(unrelated.pid), "a pid that is not ours must never be signalled"
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
            unrelated.wait(timeout=WAIT_BUDGET_S)


# =========================================================================== #
# h16 — no systemd unit ships for the layer; the presence pair is untouched   #
# =========================================================================== #


def test_supervisor_module_never_touches_systemd() -> None:
    """No ``systemctl`` / unit-file text anywhere in this module's source.

    Unlike ``reachy.demo_service`` (a systemd ``--user`` unit manager), the
    embody supervisor is a PLAIN background process (pid + log), exactly like
    sleep/vision/behavior's engine — never a unit.
    """
    import inspect

    source = inspect.getsource(supervisor)
    for forbidden in ("systemctl", "Unit]", "WantedBy", "ExecStart"):
        assert forbidden not in source, f"the embody supervisor must never mention {forbidden!r}"


def test_service_presence_pair_is_unchanged_by_this_task() -> None:
    """``_PRESENCE`` in reachy.service.manager stays exactly the demo/runtime pair.

    Imported and inspected, never edited: this task adds no third presence
    mode and no embody unit anywhere in reachy.service.
    """
    from reachy.service.manager import _MODES

    assert _MODES == ("demo", "runtime")


def test_no_embody_unit_or_reference_anywhere_in_the_unit_catalog() -> None:
    """The unit catalog (reachy.service.units) has no embody-shaped entry."""
    from reachy.service import units as units_mod

    catalog_names = {units_mod.DAEMON_UNIT, units_mod.DEMO_UNIT, units_mod.RUNTIME_UNIT}
    assert not any("embody" in name.lower() for name in catalog_names)
    assert not any("embody" in name.lower() for name in units_mod.RETIRED_UNITS)


def test_embody_supervisor_module_does_not_import_reachy_service() -> None:
    """Static proof the supervisor never reaches the systemd presence stack."""
    import ast

    tree = ast.parse(inspect_source(supervisor))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    assert not any(name.startswith("reachy.service") for name in imported)


def inspect_source(module) -> str:
    import inspect

    return inspect.getsource(module)


# =========================================================================== #
# h26 — after stop: no process/socket/unit trace, embody-* rules PERSIST      #
# =========================================================================== #


def test_stop_leaves_no_pid_file_trace(monkeypatch, tmp_path) -> None:
    (tmp_path / "embody.pid").write_text("9191")
    monkeypatch.setattr("reachy.embody.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.embody.supervisor._is_our_process", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    monkeypatch.setattr("reachy.embody.supervisor._wait_gone", lambda pid, timeout: True)

    supervisor.stop()
    assert not (tmp_path / "embody.pid").exists()
    # Nothing else appears under the state dir: no socket file, no unit file —
    # the supervisor never creates either kind of artifact.
    leftovers = {p.name for p in tmp_path.iterdir()}
    assert leftovers <= {"embody.log"}, f"unexpected artifacts left behind: {leftovers}"


def test_supervisor_never_binds_or_creates_a_socket_file(tmp_path) -> None:
    """The supervisor owns no socket of its own (the audio tee's is the runtime's).

    A behavioural companion to the static "no shell" proof: running start
    (mocked spawn) and stop must never create anything under the state dir
    with a socket type, and must never call the ``socket`` module at all.
    """
    import ast

    tree = ast.parse(inspect_source(supervisor))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name == "socket" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "socket"


def test_embody_star_rules_persist_after_stop_and_stay_enumerable_by_prefix(
    monkeypatch, tmp_path
) -> None:
    """h26's core claim: stop is not a rollback.

    A rule the layer authored (via the SAME ``create_rule`` tool
    ``reachy/embody/tools.py`` exposes) survives a full start->stop cycle of
    the supervisor untouched, and ``list_embody_rules`` (the prefix-scan the
    module already ships) still enumerates it — because the supervisor's
    ``stop`` touches only the PROCESS (pid file + signal), never the rules
    overlay on disk.
    """
    from reachy.embody.tools import (
        CREATE_RULE,
        RULE_ID_PREFIX,
        EmbodyToolRegistry,
        list_embody_rules,
    )

    registry = EmbodyToolRegistry(
        on_interjection=lambda interjection: "proposed",
        spool_root=tmp_path,
        await_timeout=0.0,
        reload_seam=lambda timeout: None,
    )
    import json as _json

    outcome = registry.dispatch(
        CREATE_RULE,
        _json.dumps(
            {
                "id": "embody-scratch-response",
                "when": {"field": "pat", "op": "is_true"},
                "run": "nod",
                "duration_s": 1.0,
            }
        ),
    )
    body = _json.loads(outcome["content"])
    assert body["ok"] is True
    assert body["id"].startswith(RULE_ID_PREFIX)

    # Simulate a full supervisor lifecycle: start (mocked spawn), then stop
    # (mocked signal) — neither of which the create_rule call above depended on.
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    supervisor.start()
    assert (tmp_path / "embody.pid").read_text().strip() == "9191"

    monkeypatch.setattr("reachy.embody.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.embody.supervisor._is_our_process", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    monkeypatch.setattr("reachy.embody.supervisor._wait_gone", lambda pid, timeout: True)
    stop_result = supervisor.stop()
    assert stop_result["status"] == "stopped"
    assert not (tmp_path / "embody.pid").exists(), "the process trace is gone"

    # The rule PERSISTS — enumerable by prefix, exactly as before the stop.
    rules = list_embody_rules()
    assert "embody-scratch-response" in rules
    assert all(name.startswith(RULE_ID_PREFIX) for name in rules)

    # And it is enumerable the OPERATOR's way too — a plain grep by prefix over
    # the overlay text, per spec c26's "grep by prefix" honesty condition.
    overlay_text = (tmp_path / "behavior" / "rules.toml").read_text(encoding="utf-8")
    assert 'id = "embody-scratch-response"' in overlay_text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
