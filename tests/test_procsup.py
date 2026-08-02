"""``reachy.procsup`` — the one owner of the tracked-background-process mechanics.

Two things are pinned here, and they are the same thing seen from two sides.

**The shape.** ``sleep`` / ``vision`` / the ``behavior`` engine / the ``embody``
layer each used to carry a verbatim copy of the PID-file, wait, identity and
stop machinery. They now cite :mod:`reachy.procsup` instead, so the mechanics
have ONE definition — while every genuine per-noun difference (filenames, argv,
daemon-health preflight, result fields, wording) stays in the noun's own
supervisor.

**The bug that duplication caused — issue #136.** ``_is_our_process`` exists for
exactly one reason: to stop ``stop`` from SIGKILLing an unrelated process that
recycled the tracked PID. Three of the four supervisors implemented it by
flattening ``/proc/<pid>/cmdline`` and testing for SUBSTRINGS, which also scans
the interpreter path and every argument — so a guard whose whole job is process
identity could be satisfied by a DIRECTORY NAME. The canonical example, straight
from the issue::

    /home/spark/git/reachy-mini-cli/.venv/bin/python3 -c "import time; time.sleep(60)"

``reachy`` comes from the checkout path, ``sleep`` from the code, and
``sleep``'s guard read that as "this is my loop". The fix — already shipped in
``reachy.embody.supervisor`` and now the shared implementation — splits the
NUL-separated cmdline and matches EXACT argv tokens.

Every ``stop``-spares-a-bystander test below spawns a REAL process, per the
repo's hard rule using only a trivial, hardcoded-argv stand-in
(``sys.executable -c "import time; time.sleep(...)"``) — never a real robot
verb, never anything that touches a service. Waiting is always on the actual
condition (process gone / still alive), never on a fixed sleep; the budget is
``tests.conftest.WAIT_BUDGET_S``.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - test spawns trivial, hardcoded-argv stand-in processes
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from reachy import procsup
from reachy.behavior import supervisor as behavior_supervisor
from reachy.embody import supervisor as embody_supervisor
from reachy.sleep import supervisor as sleep_supervisor
from reachy.vision import supervisor as vision_supervisor
from tests.conftest import WAIT_BUDGET_S

#: The stand-in body: harmless, hardcoded, touches nothing real.
_STANDIN_CODE = "import time; time.sleep(120)"


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


# --------------------------------------------------------------------------- #
# The four supervisors, described by what actually differs between them        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Noun:
    """One supervised noun: its module, its identity tokens, and two stand-ins."""

    name: str
    module: object
    #: The exact argv tokens its real spawn line carries.
    tokens: tuple[str, ...]
    #: An extra argv element for the IMPOSTOR that embeds every token as a
    #: SUBSTRING but never as a token of its own — standing in for the
    #: interpreter path on a real checkout (``…/reachy-mini-cli/.venv/bin/python``
    #: already supplies ``reachy`` for free) so the test is deterministic
    #: wherever it runs.
    impostor_arg: str


_NOUNS = (
    _Noun(
        name="sleep",
        module=sleep_supervisor,
        tokens=("reachy", "sleep"),
        impostor_arg="--launched-from-a-reachy-mini-cli-checkout",
    ),
    _Noun(
        name="vision",
        module=vision_supervisor,
        tokens=("reachy", "vision"),
        impostor_arg="--reachy-mini-cli-supervision-helper",
    ),
    _Noun(
        name="behavior",
        module=behavior_supervisor,
        tokens=("behavior", "engine"),
        impostor_arg="--behavioral-engineering-notes",
    ),
    _Noun(
        name="embody",
        module=embody_supervisor,
        tokens=("reachy", "embody"),
        impostor_arg="--not-a-reachy-embody-process",
    ),
)

_NOUN_BY_NAME = {noun.name: noun for noun in _NOUNS}


@pytest.fixture(params=[noun.name for noun in _NOUNS])
def noun(request) -> _Noun:
    return _NOUN_BY_NAME[request.param]


# --------------------------------------------------------------------------- #
# Helpers — real processes, real waiting, no sleeps as assertions              #
# --------------------------------------------------------------------------- #


def _cmdline_tokens(pid: int) -> set[str]:
    """The live argv of *pid* as a token set, or empty if it cannot be read."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return set()
    return {token.decode("utf-8", "replace") for token in raw.split(b"\x00") if token}


def _spawn(*extra_argv: str) -> subprocess.Popen:
    """Spawn a trivial stand-in and WAIT until its own argv is the live one.

    ``Popen`` returns as soon as the child exists, which is BEFORE ``execve``
    has replaced its image — until then ``/proc/<pid>/cmdline`` still shows the
    parent's (pytest's) argv. Every test here reasons about the child's command
    line, so returning on "the pid exists" is a race, and one that only opens
    under load: it passed serially and failed for ``sleep`` and ``vision`` on
    the first ``pytest -n auto`` run. Waiting on the actual condition — the
    child's last argv element visible in ``/proc`` — closes it. Budget is
    ``tests.conftest.WAIT_BUDGET_S``.
    """
    argv = [sys.executable, "-c", _STANDIN_CODE, *extra_argv]
    proc = subprocess.Popen(  # nosec B603 B607 - fixed argv, sys.executable, no shell
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert _wait_until(
        lambda: argv[-1] in _cmdline_tokens(proc.pid)
    ), f"the stand-in never exec'd its own argv: {argv[-1]!r}"
    return proc


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate, *, budget: float = WAIT_BUDGET_S, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _reap_in_background(proc: subprocess.Popen) -> None:
    """Reap *proc* the moment it exits, on a daemon thread.

    A killed child stays a ZOMBIE — ``os.kill(pid, 0)`` still succeeds — until
    its parent waits on it. On a deployed box the supervised loop is reparented
    to init and reaped automatically; here the parent (pytest) stays alive, so
    without this a stop's own liveness poll would see a zombie and time out.
    """
    threading.Thread(target=proc.wait, daemon=True).start()


def _legacy_substring_match(pid: int, tokens: tuple[str, ...]) -> bool:
    """The predicate issue #136 deleted, kept HERE so the regression stays real.

    This is verbatim what ``sleep`` / ``vision`` / ``behavior`` used to run:
    flatten the NUL-separated cmdline into one string and test for substrings.
    Asserting it says ``True`` for each impostor below is what stops those tests
    from quietly going vacuous — without it, a future change to the stand-in
    argv could make them pass for the boring reason that nothing ever matched.
    """
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    return all(token in cmdline for token in tokens)


def _write_pid(noun: _Noun, pid: int) -> Path:
    path = noun.module.pid_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# has_argv_tokens — the shared guard                                           #
# --------------------------------------------------------------------------- #


def test_has_argv_tokens_matches_exact_argv_elements() -> None:
    proc = _spawn("reachy", "sleep")
    try:
        assert procsup.has_argv_tokens(proc.pid, ("reachy", "sleep"))
    finally:
        proc.kill()
        proc.wait(timeout=WAIT_BUDGET_S)


def test_has_argv_tokens_rejects_a_substring_that_is_not_its_own_token() -> None:
    """The #136 defect in one assertion, against a real process.

    ``--reachy-mini-cli-supervision-helper`` contains both ``reachy`` and
    ``vision``; neither is an argv element, so the guard says "not ours".
    """
    proc = _spawn("--reachy-mini-cli-supervision-helper")
    try:
        assert _legacy_substring_match(proc.pid, ("reachy", "vision")), (
            "the stand-in no longer reproduces the defect — the substring guard "
            "must still have matched it, or this test proves nothing"
        )
        assert not procsup.has_argv_tokens(proc.pid, ("reachy", "vision"))
    finally:
        proc.kill()
        proc.wait(timeout=WAIT_BUDGET_S)


def test_has_argv_tokens_says_not_ours_for_a_dead_pid(tmp_path) -> None:
    proc = _spawn()
    proc.kill()
    proc.wait(timeout=WAIT_BUDGET_S)
    assert not procsup.has_argv_tokens(proc.pid, ("reachy", "sleep"))


def test_has_argv_tokens_trusts_the_pid_file_without_proc(monkeypatch) -> None:
    """No ``/proc`` (non-Linux) means we cannot verify — so we do not refuse."""
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    assert procsup.has_argv_tokens(1, ("reachy", "sleep"))


def test_every_supervisor_identity_is_a_token_set_not_a_substring_scan() -> None:
    """Structural: no supervisor may go back to flattening the cmdline.

    The four modules each declare ``_IDENTITY_TOKENS`` and delegate to
    :func:`reachy.procsup.has_argv_tokens`; none of them may re-derive the
    guard, because a second implementation is exactly how #136 survived a fix.
    """
    import inspect

    for noun in _NOUNS:
        source = inspect.getsource(noun.module)
        assert 'replace(b"\\x00"' not in source, f"{noun.name} flattens cmdline again"
        assert "/proc/" not in source, f"{noun.name} reads /proc directly again"
        assert noun.module._IDENTITY_TOKENS == noun.tokens


# --------------------------------------------------------------------------- #
# #136 — stop() spares a real, unrelated process, in EVERY supervisor          #
# --------------------------------------------------------------------------- #


def test_stop_spares_a_real_unrelated_process_that_the_substring_guard_matched(
    noun: _Noun,
) -> None:
    """The regression, per supervisor: a bystander the OLD guard called ours.

    A REAL process is spawned from this project's own virtualenv interpreter and
    is manifestly not the supervised loop — but its command line contains every
    word the retired substring guard tested for, exactly as an innocent
    ``python -c "…time.sleep(60)"`` from a ``reachy-mini-cli`` checkout did.
    The PID file names it; ``stop`` must refuse to signal it and say so.

    Before the shared exact-token guard this failed in three of the four
    supervisors — ``sleep``, ``vision`` and ``behavior`` each SIGTERMed the
    bystander. ``embody`` already passed (it is where the fix was written);
    it is parametrized here anyway so the invariant is stated once for the
    family rather than four times with one silently different.
    """
    impostor = _spawn(noun.impostor_arg)
    try:
        assert _legacy_substring_match(impostor.pid, noun.tokens), (
            f"{noun.name}: the stand-in no longer reproduces #136 — the retired "
            "substring guard must still have matched it for this to be a regression test"
        )

        pid_path = _write_pid(noun, impostor.pid)
        result = noun.module.stop()

        assert result["status"] == "not running"
        assert "reused" in result["note"]
        assert _pid_alive(impostor.pid), f"{noun.name}: stop signalled an unrelated process"
        # The refusal also clears the misleading pid file rather than leaving it
        # to be re-tried on the next stop.
        assert not pid_path.exists()
    finally:
        if impostor.poll() is None:
            impostor.kill()
            impostor.wait(timeout=WAIT_BUDGET_S)


def test_stop_still_ends_the_real_tracked_process(noun: _Noun) -> None:
    """The other half: the guard must not simply refuse everything.

    A second stand-in carries the noun's identity tokens as its OWN argv
    elements — the shape of the real ``python -m reachy …`` spawn line, never
    the real verb itself — and ``stop`` must end it while the bystander spawned
    alongside survives untouched.
    """
    tracked = _spawn(*noun.tokens)
    bystander = _spawn(noun.impostor_arg)
    try:
        _reap_in_background(tracked)
        _reap_in_background(bystander)

        _write_pid(noun, tracked.pid)
        result = noun.module.stop()

        assert result["status"] == "stopped"
        assert result["pid"] == tracked.pid
        assert _wait_until(lambda: not _pid_alive(tracked.pid)), f"{noun.name}: not stopped"
        assert _pid_alive(bystander.pid), f"{noun.name}: a bystander was signalled"
    finally:
        for proc in (tracked, bystander):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=WAIT_BUDGET_S)


# --------------------------------------------------------------------------- #
# The shape: shared mechanics, preserved per-noun differences                  #
# --------------------------------------------------------------------------- #


def test_stop_wording_stays_per_noun(noun: _Noun) -> None:
    """Sharing the mechanics must not flatten what an operator reads."""
    result = noun.module.stop()
    assert result["status"] == procsup.STATUS_NOT_RUNNING
    assert result["note"] == f"no tracked {noun.module._LABELS.tracked} pid"


def test_the_four_supervisors_word_their_messages_differently() -> None:
    """Guard the guard above: the labels are genuinely distinct, not decoration."""
    tracked = [n.module._LABELS.tracked for n in _NOUNS]
    reused = [n.module._LABELS.reused for n in _NOUNS]
    assert len(set(tracked)) == len(tracked)
    assert len(set(reused)) == len(reused)
    assert behavior_supervisor._LABELS.tracked == "engine"
    assert behavior_supervisor._LABELS.signalled == "behavior engine"
    assert embody_supervisor._LABELS.reused == "an embody layer"


def test_pid_and_log_paths_stay_per_noun(tmp_path) -> None:
    """Each noun keeps its OWN bookkeeping filenames so the loops can coexist."""
    paths = {n.name: n.module.pid_file() for n in _NOUNS}
    assert len(set(paths.values())) == len(paths)
    assert paths["sleep"] == tmp_path / "sleep.pid"
    assert paths["vision"] == tmp_path / "vision.pid"
    assert paths["embody"] == tmp_path / "embody.pid"
    # The engine alone lives under behavior_dir(), not the plain state dir.
    assert paths["behavior"] == tmp_path / "behavior" / "engine.pid"


def test_only_vision_and_the_engine_preflight_the_daemon() -> None:
    """The preserved structural difference, asserted rather than described.

    ``vision`` and the ``behavior`` engine refuse to spawn when the daemon's
    health route is silent; ``sleep`` and ``embody`` deliberately do not — their
    loops self-report instead. Checked at the source level because the two that
    do it are the only two that call :func:`reachy.procsup.require_daemon_health`.
    """
    import inspect

    preflights = {n.name for n in _NOUNS if "require_daemon_health" in inspect.getsource(n.module)}
    assert preflights == {"vision", "behavior"}


def test_the_pid_file_is_cleared_on_a_failed_start_only_where_it_always_was(
    monkeypatch, noun: _Noun
) -> None:
    """``clear_pid_on_exit`` is a preserved per-noun difference, not a default.

    ``sleep`` / ``embody`` clear the pid file they just wrote when the child dies
    inside the grace window; ``vision`` / the engine leave it for the stale-pid
    path. Sharing the spawn must not quietly unify the two.
    """

    class _ExitedPopen:
        returncode = 1

        def __init__(self, cmd, **kwargs) -> None:
            self.pid = 4242

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("subprocess.Popen", _ExitedPopen)
    monkeypatch.setattr(f"{noun.module.__name__}.health_ok", lambda *a, **k: True, raising=False)

    result = noun.module.start()
    assert result["status"] == "exited"
    cleared = not noun.module.pid_file().exists()
    assert cleared is (noun.name in {"sleep", "embody"})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
