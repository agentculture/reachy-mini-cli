"""Tests for folding ``think``'s cognition trigger into the ``listen`` loop.

``listen`` already owns the one in-process SDK media session and derives a single
per-tick :class:`~reachy.motion.sense_sample.SenseSample` (DoA / RMS / speech).
:class:`~reachy.motion.listen_think.ThinkHook` is the per-tick ``on_tick`` hook
that drives ``think``'s :class:`~reachy.speech.cognition.CognitionEngine` from
*that* shared sample — it never opens a second media session (which would
contend for the single-consumer SDK client and throttle to ~1 Hz, see the
single-SDK-owner model in ``CLAUDE.md`` and the #43 ``PatHook`` fold-in).

These tests exercise the seam directly with fakes — no robot, no daemon, no
network, no real LLM, no real threads (the cognition worker is driven through an
injected synchronous spawner) and no real sleeps. The ``think_active.flag`` is
pinned into a throwaway state dir.

Coverage (mirrors the acceptance criteria):

1. The hook consumes the loop's shared sample via an injected ``SampleProvider``;
   a ``None`` sample is a silent no-op (no engine call, no flag).
2. A sample carrying a speech cue drives the (injected) cognition engine — the
   engine's buffer receives the cue — and the ``think_active.flag`` is raised
   while cognition produces, then cleared.
3. The hook mirrors ``PatHook``'s structure: the ``on_tick`` signature, silent
   degradation on errors (a faulty provider/engine never kills the loop), a
   ``close()`` that clears the flag, and full determinism via injected fakes.
4. The hook never blocks the loop on the LLM: ``__call__`` only feeds cues +
   updates the flag and returns promptly; cognition runs off the tick thread on a
   start-once background worker. The hook NEVER imports/opens a media session.
"""

from __future__ import annotations

import threading

import pytest

import reachy.speech.cognition_signal as cs
from reachy.motion.listen_think import ThinkHook
from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SenseSample

# ---------------------------------------------------------------------------
# Isolation: pin the think-active flag into a throwaway state dir
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin bookkeeping into a throwaway dir; never touch the real state dir."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    cs.clear()
    yield
    cs.clear()


# ---------------------------------------------------------------------------
# Fakes: a cognition engine + a synchronous worker spawner
# ---------------------------------------------------------------------------


class _FakeEngine:
    """A stand-in for :class:`CognitionEngine`.

    Captures everything fed to its buffer, and exposes a ``run`` whose
    ``before_turn`` hook is pumped on each iteration so the test can observe the
    cues the hook fed. ``run`` is driven by a ``stop`` predicate and runs as many
    bounded iterations as the test allows — exactly the ``CognitionEngine.run``
    surface ``ThinkHook`` relies on.
    """

    def __init__(self) -> None:
        self.buffer = _RecordingBuffer()
        self.run_calls = 0
        self.turns_run = 0
        self.run_started = threading.Event()
        self.run_finished = threading.Event()
        # When True, ``run`` blocks (simulating a long LLM turn) until released.
        self.block_until = threading.Event()
        self.block = False

    def run(self, *, max_turns=None, stop=None, before_turn=None):  # noqa: ARG002
        self.run_calls += 1
        self.run_started.set()
        # Pump a handful of bounded iterations, calling before_turn each time so
        # the hook drains its sample buffer into the engine buffer.
        for _ in range(64):
            if stop is not None and stop():
                break
            if before_turn is not None:
                before_turn()
            self.turns_run += 1
            if self.block and not self.block_until.is_set():
                self.block_until.wait(timeout=1.0)
        self.run_finished.set()
        return self.turns_run


class _RecordingBuffer:
    """A minimal :class:`EventBuffer` look-alike recording fed DoA cues."""

    def __init__(self) -> None:
        self.doa_feeds: list[dict] = []
        self.vision_feeds: list[dict] = []

    def feed_doa(self, angle_rad, rms, is_speech) -> None:
        self.doa_feeds.append({"angle_rad": angle_rad, "rms": rms, "is_speech": is_speech})

    def feed_vision(self, motion_direction, brightness_delta) -> None:  # pragma: no cover
        self.vision_feeds.append(
            {"motion_direction": motion_direction, "brightness_delta": brightness_delta}
        )

    def snapshot(self):  # pragma: no cover - not used by the hook directly
        return []


class _SyncSpawn:
    """A spawner that runs the worker target inline (deterministic, no threads).

    Mirrors ``threading.Thread(target=...).start()`` enough for the hook: it is
    called with ``target`` (and optional ``name``) and runs it immediately, so the
    cognition ``run`` loop executes synchronously inside ``__call__``. Records the
    spawned callables so a test can assert "started exactly once".
    """

    def __init__(self) -> None:
        self.spawned: list = []

    def __call__(self, target, *, name=None):  # noqa: ARG002
        self.spawned.append(target)
        target()
        return _FakeHandle()


class _FakeHandle:
    """A thread-handle stand-in: join() is a no-op (sync spawn already finished)."""

    def join(self, timeout=None) -> None:  # noqa: ARG002
        return None


def _make_hook(provider, **kwargs):
    """Build a ThinkHook with a fake engine + synchronous spawner unless overridden."""
    engine = kwargs.pop("engine", None) or _FakeEngine()
    spawn = kwargs.pop("spawn", None) or _SyncSpawn()
    hook = ThinkHook(provider, engine=engine, spawn=spawn, **kwargs)
    return hook, engine, spawn


# ---------------------------------------------------------------------------
# 1. None sample → silent no-op
# ---------------------------------------------------------------------------


def test_none_sample_is_silent_no_op() -> None:
    """A provider returning ``None`` means no cues fed, no flag, no engine run."""
    hook, engine, spawn = _make_hook(lambda: None)

    queue = MotionQueue()
    for i in range(5):
        hook(object(), queue, 0.1 * i, {"pitch": 0.0, "yaw": 0.0})

    assert engine.buffer.doa_feeds == [], "a None sample must feed no cues"
    assert engine.run_calls == 0, "a None sample must not start cognition"
    assert spawn.spawned == [], "no worker spawned on an empty tick stream"
    assert cs.is_active() is False, "no flag raised when there is nothing to think about"
    hook.close()
    assert cs.is_active() is False


# ---------------------------------------------------------------------------
# 2. A speech sample drives the engine and raises/clears the flag
# ---------------------------------------------------------------------------


def test_speech_sample_feeds_engine_and_raises_flag() -> None:
    """A sample with a speech cue feeds the engine buffer and raises the flag."""
    sample = SenseSample(rms=0.08, doa=10.0, speech=True, ts=1.0)
    hook, engine, spawn = _make_hook(lambda: sample)

    queue = MotionQueue()
    hook(object(), queue, 0.1, {"pitch": 0.0, "yaw": 0.0})

    # The sample's cues reached the engine's event buffer (speech True).
    assert engine.buffer.doa_feeds, "the speech sample must be fed to the engine buffer"
    feed = engine.buffer.doa_feeds[-1]
    assert feed["is_speech"] is True
    assert feed["rms"] == pytest.approx(0.08)
    assert feed["angle_rad"] is not None, "a non-None doa must reach the buffer"

    # Cognition started exactly once (start-once worker).
    assert engine.run_calls == 1
    assert len(spawn.spawned) == 1, "the cognition worker must be spawned exactly once"

    # A second tick must NOT spawn a second worker.
    hook(object(), queue, 0.2, {"pitch": 0.0, "yaw": 0.0})
    assert len(spawn.spawned) == 1, "the worker is start-once; a second tick reuses it"

    # The flag tracks cognition activity: with the sync spawner the run loop has
    # already drained, so close() clears it cleanly.
    hook.close()
    assert cs.is_active() is False, "close() must clear the think-active flag"


def test_flag_is_raised_while_cognition_runs() -> None:
    """While the cognition worker is in its run loop, the flag is up."""
    engine = _FakeEngine()
    engine.block = True  # the run loop parks until released

    sample = SenseSample(rms=0.05, doa=0.0, speech=True, ts=0.0)
    spawn_threads: list[threading.Thread] = []

    def _thread_spawn(target, *, name=None):
        th = threading.Thread(target=target, name=name, daemon=True)
        spawn_threads.append(th)
        th.start()
        return th

    hook = ThinkHook(lambda: sample, engine=engine, spawn=_thread_spawn)
    queue = MotionQueue()
    hook(object(), queue, 0.1, {"pitch": 0.0, "yaw": 0.0})

    # The worker thread is parked inside run(); the flag must be raised.
    assert engine.run_started.wait(timeout=2.0), "cognition run did not start"
    assert cs.is_active() is True, "the flag must be up while cognition runs"

    # Release the worker and close — the flag must come back down.
    engine.block_until.set()
    hook.close()
    assert engine.run_finished.wait(timeout=2.0)
    assert cs.is_active() is False, "the flag must clear once cognition stops"


# ---------------------------------------------------------------------------
# 3. on_tick signature + silent degradation + determinism seams
# ---------------------------------------------------------------------------


def test_on_tick_signature_matches_pat_hook() -> None:
    """ThinkHook.__call__ accepts (transport, queue, t, commanded_head)."""
    hook, _engine, _spawn = _make_hook(lambda: None)
    queue = MotionQueue()
    # Positional, exactly like HookChain forwards to PatHook.
    hook(object(), queue, 0.5, {"pitch": 1.0, "yaw": 2.0})
    # commanded_head is optional (the seam may omit it) — must not raise.
    hook(object(), queue, 0.6)


def test_faulty_provider_degrades_silently() -> None:
    """A provider that raises must not propagate out of the tick (loop survives)."""

    def _boom():
        raise RuntimeError("sensor blew up")

    hook, engine, _spawn = _make_hook(_boom)
    queue = MotionQueue()
    # Must NOT raise — the loop must never die from a hook fault.
    hook(object(), queue, 0.1, {"pitch": 0.0, "yaw": 0.0})
    assert engine.run_calls == 0, "a faulty provider must not start cognition"
    assert cs.is_active() is False


def test_faulty_engine_feed_degrades_silently() -> None:
    """An engine whose buffer.feed_doa raises is swallowed; the tick returns."""

    class _BadBuffer(_RecordingBuffer):
        def feed_doa(self, *a, **k):
            raise RuntimeError("buffer fault")

    engine = _FakeEngine()
    engine.buffer = _BadBuffer()
    sample = SenseSample(rms=0.05, doa=0.0, speech=True, ts=0.0)
    hook, _e, _s = _make_hook(lambda: sample, engine=engine)
    queue = MotionQueue()
    # The feed fault must not escape the tick.
    hook(object(), queue, 0.1, {"pitch": 0.0, "yaw": 0.0})


def test_close_is_idempotent_and_clears_flag() -> None:
    """close() clears the flag and is safe to call repeatedly / when never active."""
    hook, _engine, _spawn = _make_hook(lambda: None)
    # Never fired — close must be a safe no-op and leave the flag clear.
    hook.close()
    hook.close()
    assert cs.is_active() is False


# ---------------------------------------------------------------------------
# 4. The hook never opens a media session (single-SDK-owner invariant)
# ---------------------------------------------------------------------------


def test_hook_never_opens_a_media_session() -> None:
    """The hook reads cues ONLY via the provider — never transport.media_session."""

    class _ExplodingTransport:
        name = "sdk"

        def media_session(self):  # pragma: no cover - must never be called
            raise AssertionError("ThinkHook must NOT open a media session")

        def head_pose(self):  # pragma: no cover
            raise AssertionError("ThinkHook must not read head_pose either")

    sample = SenseSample(rms=0.05, doa=0.0, speech=True, ts=0.0)
    hook, engine, _spawn = _make_hook(lambda: sample)
    queue = MotionQueue()
    # Passing a transport whose media_session explodes proves the hook never calls it.
    hook(_ExplodingTransport(), queue, 0.1, {"pitch": 0.0, "yaw": 0.0})
    assert engine.run_calls == 1


def test_module_does_not_import_reachy_mini_or_media_session() -> None:
    """Static guard: the module's *code* must not call media_session / build ReachyMini.

    Prose (docstrings/comments) is allowed to *name* these to explain what the hook
    deliberately does NOT do, so we strip docstrings + comments and assert the
    executable AST contains no ``ReachyMini`` name and no ``.media_session`` access.
    """
    import ast
    import inspect

    import reachy.motion.listen_think as mod

    tree = ast.parse(inspect.getsource(mod))
    # Drop module/class/function docstrings so prose mentions don't trip the guard.
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {a.attr for a in ast.walk(tree) if isinstance(a, ast.Attribute)}
    aliases = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "media_session" not in attrs, "ThinkHook must not call media_session"
    assert "ReachyMini" not in names, "ThinkHook must not reference a ReachyMini client"
    assert not any("reachy_mini" in a for a in aliases), "ThinkHook must not import reachy_mini"


def test_call_returns_promptly_without_blocking_on_cognition() -> None:
    """``__call__`` must return even when the cognition run loop blocks (off-thread).

    With a real thread spawner and a blocking engine run, the tick must still
    return promptly — proving cognition runs off the tick thread.
    """
    engine = _FakeEngine()
    engine.block = True

    def _thread_spawn(target, *, name=None):
        th = threading.Thread(target=target, name=name, daemon=True)
        th.start()
        return th

    sample = SenseSample(rms=0.05, doa=0.0, speech=True, ts=0.0)
    hook = ThinkHook(lambda: sample, engine=engine, spawn=_thread_spawn)
    queue = MotionQueue()

    done = threading.Event()

    def _tick():
        hook(object(), queue, 0.1, {"pitch": 0.0, "yaw": 0.0})
        done.set()

    t = threading.Thread(target=_tick, daemon=True)
    t.start()
    # If __call__ blocked on the cognition run loop, this would time out.
    assert done.wait(timeout=2.0), "__call__ must not block on the cognition worker"

    engine.block_until.set()
    hook.close()
