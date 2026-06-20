"""Tests for ``listen run --live`` — compose all four sense hooks into one loop.

``--live`` is the "live mode" the boot service runs: it folds the existing
:class:`~reachy.motion.listen_pat.PatHook` together with the three sibling fold-in
hooks — :class:`~reachy.motion.listen_think.ThinkHook`,
:class:`~reachy.motion.listen_vision.VisionHook`, and
:class:`~reachy.motion.listen_sleep.SleepHook` — into ONE
:func:`reachy.motion.server.run` loop via a single
:class:`~reachy.motion.listen_hooks.HookChain`. All four hooks share the loop's
*one* SDK media session and its *one* :class:`~reachy.motion.queue.MotionQueue`,
arbitrated by the established idle-interrupt priority ``sleep > pat > think``
(vision rides last; it competes for nothing the flags arbitrate).

The crux is the **shared sample**: the loop already computes one DoA + RMS +
speech reading per tick (to drive the antenna lean / Tier-2 turn). ``--live``
exposes that per-tick value as a shared
:class:`~reachy.motion.sense_sample.SenseSample` through a small holder, and the
audio hooks (think, sleep) read it through a provider — *none* opens its own
audio session. Exactly one media session is opened for the whole loop.

Coverage (mirrors the acceptance criteria):

1. ``--live`` builds a HookChain of all four hooks in ``sleep > pat > think``
   order, and EXACTLY ONE media session is opened for the whole loop (no hook
   opens a second).
2. A bounded ``--live --ticks N`` drives all four hooks — each hook's
   ``__call__`` is invoked, fed the loop's shared per-tick sample.
3. Default ``listen run`` (no ``--live``) is behaviourally UNCHANGED — same
   single-PatHook behaviour as today (regression).

No robot, no daemon, no network, no real sleeps.
"""

from __future__ import annotations

import contextlib
import io
import sys

import numpy as np
import pytest

import reachy.cli._commands.listen as listen_mod
import reachy.motion.pat_signal as ps
import reachy.motion.sleep_signal as ss
import reachy.speech.cognition_signal as cs
from reachy.cli import main
from reachy.motion.listen_hooks import HookChain
from reachy.motion.listen_pat import PatHook
from reachy.motion.listen_sleep import SleepHook
from reachy.motion.listen_think import ThinkHook
from reachy.motion.listen_vision import VisionHook
from reachy.motion.sense_sample import SenseSample

# ---------------------------------------------------------------------------
# Isolation: pin every *_active flag into a throwaway state dir
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin bookkeeping into a throwaway dir; never touch the real state dir."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    for sig in (ps, ss, cs):
        sig.clear()
    yield
    for sig in (ps, ss, cs):
        sig.clear()


# ---------------------------------------------------------------------------
# Fake sdk media session + transport (mirror tests/test_listen_pat.py)
# ---------------------------------------------------------------------------


class _Session:
    """The ONE open client for the loop: audio + DoA + pose + move + frame all
    ride this object.

    Opening a second session — or a fresh per-call client for pose/move/frame —
    is exactly the contention/fd-leak failure ``--live`` must avoid (issue #51),
    so the tests assert it is opened EXACTLY once and the per-call path is never
    hit.
    """

    _SAMPLE = np.full(512, 0.001, dtype=np.float32)  # below min_rms → no snap

    def __init__(self):
        self.gotos: list[dict] = []

    def doa(self, *, timeout=None):  # noqa: ARG002
        return {"angle": np.pi / 2, "speech_detected": False}  # front, no speech

    def get_audio_sample(self):
        return self._SAMPLE

    def head_pose(self) -> tuple[float, float]:
        return (0.0, 0.0)  # flat: no pat

    def get_frame(self):
        return None  # no camera frame → vision is a quiet no-op

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        self.gotos.append({"head": head, "duration": duration})
        return {"uuid": "fake"}

    @property
    def samplerate(self):
        return 16000

    @property
    def channels(self):
        return 1


class _LiveSdkTransport:
    """A fake sdk transport. ``head_pose``/``move_goto``/``get_frame`` here stand
    in for the LEAKY per-call-client path (each opens a fresh ``ReachyMini``); the
    loop must route those through the open session instead, so they bump
    ``base_calls`` and the issue-#51 tests assert it stays 0. ``media_opens``
    counts sessions opened — the loop must open exactly one.
    """

    name = "sdk-live"

    def __init__(self):
        self.media_opens = 0
        self.base_calls = 0
        self._session = _Session()

    @property
    def gotos(self):  # moves now ride the one open session
        return self._session.gotos

    def head_pose(self) -> tuple[float, float]:
        self.base_calls += 1
        return (0.0, 0.0)

    def get_frame(self):
        self.base_calls += 1
        return None

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        self.base_calls += 1
        return {"uuid": "fake"}

    @contextlib.contextmanager
    def media_session(self):
        self.media_opens += 1
        yield self._session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_live_cli(monkeypatch, transport, *, max_ticks, extra_args=None):
    """Run ``reachy listen run --live --json`` against *transport*; return rc."""
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: transport)

    argv = [
        "listen",
        "run",
        "--live",
        "--json",
        "--transport",
        "sdk",
        "--deadband",
        "0",
        "--max-ticks",
        str(max_ticks),
    ]
    if extra_args:
        argv.extend(extra_args)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = main(argv)
    finally:
        sys.stdout = old
    return rc


def _spy_chain(monkeypatch):
    """Capture the HookChain the live loop builds (its hook list, in order)."""
    captured: dict[str, object] = {}
    real_init = HookChain.__init__

    def _spy(self, hooks):
        captured["hooks"] = list(hooks)
        captured["chain"] = self
        real_init(self, hooks)

    monkeypatch.setattr(HookChain, "__init__", _spy)
    return captured


# ---------------------------------------------------------------------------
# 1. --live builds the four-hook chain in priority order; ONE media session
# ---------------------------------------------------------------------------


def test_live_builds_four_hook_chain_in_priority_order(monkeypatch) -> None:
    """``--live`` composes a HookChain of all four hooks, sleep > pat > think order.

    The chain must contain a SleepHook, a PatHook, a ThinkHook, and a VisionHook.
    The flag-arbitrated three (sleep, pat, think) must appear in that descending
    priority order — sleep before pat before think — so the documented
    ``sleep > pat > think`` interrupt order holds when they run each tick.
    """
    captured = _spy_chain(monkeypatch)
    transport = _LiveSdkTransport()

    rc = _run_live_cli(monkeypatch, transport, max_ticks=3, extra_args=["--idle-energy", "0"])
    assert rc == 0

    hooks = captured["hooks"]
    types = [type(h) for h in hooks]
    assert SleepHook in types, types
    assert PatHook in types, types
    assert ThinkHook in types, types
    assert VisionHook in types, types

    # sleep > pat > think (the flag-arbitrated priority order).
    i_sleep = next(i for i, h in enumerate(hooks) if isinstance(h, SleepHook))
    i_pat = next(i for i, h in enumerate(hooks) if isinstance(h, PatHook))
    i_think = next(i for i, h in enumerate(hooks) if isinstance(h, ThinkHook))
    assert i_sleep < i_pat < i_think, f"expected sleep<pat<think, got {types}"


def test_live_opens_exactly_one_media_session(monkeypatch) -> None:
    """The whole live loop opens the single-consumer SDK media session EXACTLY once.

    This is the single-SDK-owner invariant: the loop opens one session and every
    hook rides the shared sample/transport — no hook opens a second consumer
    (which would throttle the lot to ~1 Hz). With four hooks composed, the open
    count must still be 1.
    """
    transport = _LiveSdkTransport()
    rc = _run_live_cli(monkeypatch, transport, max_ticks=5, extra_args=["--idle-energy", "0"])
    assert rc == 0
    assert transport.media_opens == 1, (
        f"expected exactly one media session for the whole live loop, "
        f"got {transport.media_opens}"
    )


# ---------------------------------------------------------------------------
# 2. --live --ticks N drives all four hooks (each __call__ invoked) from the
#    shared per-tick sample
# ---------------------------------------------------------------------------


def test_live_invokes_all_four_hooks(monkeypatch) -> None:
    """A bounded ``--live --ticks N`` calls every hook's ``__call__`` each tick.

    Spy on each hook class's ``__call__`` and assert all four are invoked at least
    once over the bounded run — proving the HookChain fans the loop's single
    ``on_tick`` seam out to all four sense behaviours in one loop.
    """
    invoked: dict[str, int] = {"sleep": 0, "pat": 0, "think": 0, "vision": 0}

    def _wrap(cls, key):
        real = cls.__call__

        def _call(self, *a, **k):
            invoked[key] += 1
            return real(self, *a, **k)

        monkeypatch.setattr(cls, "__call__", _call)

    _wrap(SleepHook, "sleep")
    _wrap(PatHook, "pat")
    _wrap(ThinkHook, "think")
    _wrap(VisionHook, "vision")

    transport = _LiveSdkTransport()
    rc = _run_live_cli(monkeypatch, transport, max_ticks=6, extra_args=["--idle-energy", "0"])
    assert rc == 0

    for key, n in invoked.items():
        assert n >= 1, f"hook {key!r} was never invoked (counts={invoked})"


def test_live_audio_hooks_read_the_shared_sample(monkeypatch) -> None:
    """The think + sleep hooks consume the loop's shared SenseSample via a provider.

    Capture the ``sample_provider`` handed to ThinkHook and SleepHook and confirm
    BOTH receive the SAME provider, and that calling it yields the loop's per-tick
    :class:`SenseSample` (not ``None``) — i.e. the audio hooks read the loop's
    already-computed DoA/RMS/speech, never opening their own audio.
    """
    seen_providers: list[object] = []

    real_think_init = ThinkHook.__init__
    real_sleep_init = SleepHook.__init__

    def _think_init(self, sample_provider, **kw):
        seen_providers.append(("think", sample_provider))
        real_think_init(self, sample_provider, **kw)

    def _sleep_init(self, sample_provider, **kw):
        seen_providers.append(("sleep", sample_provider))
        real_sleep_init(self, sample_provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)
    monkeypatch.setattr(SleepHook, "__init__", _sleep_init)

    transport = _LiveSdkTransport()
    rc = _run_live_cli(monkeypatch, transport, max_ticks=5, extra_args=["--idle-energy", "0"])
    assert rc == 0

    providers = {name: prov for name, prov in seen_providers}
    assert "think" in providers and "sleep" in providers, seen_providers
    # Both audio hooks read the SAME shared provider (one sample source).
    assert providers["think"] is providers["sleep"], "think + sleep must share one provider"

    # After the loop ran, the provider yields a real per-tick SenseSample.
    sample = providers["think"]()
    assert isinstance(sample, SenseSample), f"provider yielded {sample!r}, not a SenseSample"


# ---------------------------------------------------------------------------
# 3. Default (no --live) is unchanged: single PatHook, no other hook built
# ---------------------------------------------------------------------------


def test_default_listen_builds_no_live_hooks(monkeypatch) -> None:
    """Default ``listen run`` (no ``--live``) builds NO think/vision/sleep hooks.

    The regression guard: without ``--live`` the loop is exactly as today — only
    the single PatHook is wired, and none of the three live-only hooks is ever
    constructed.
    """
    built: dict[str, int] = {"pat": 0, "think": 0, "vision": 0, "sleep": 0}

    def _count(cls, key):
        real = cls.__init__

        def _init(self, *a, **k):
            built[key] += 1
            return real(self, *a, **k)

        monkeypatch.setattr(cls, "__init__", _init)

    _count(PatHook, "pat")
    _count(ThinkHook, "think")
    _count(VisionHook, "vision")
    _count(SleepHook, "sleep")

    transport = _LiveSdkTransport()
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: transport)

    rc = main(
        ["listen", "run", "--json", "--transport", "sdk", "--deadband", "0", "--max-ticks", "5"]
    )
    assert rc == 0
    assert built["pat"] == 1, "default listen still wires exactly one PatHook"
    assert built["think"] == 0, "default listen must NOT build a ThinkHook"
    assert built["vision"] == 0, "default listen must NOT build a VisionHook"
    assert built["sleep"] == 0, "default listen must NOT build a SleepHook"


def test_default_listen_uses_no_hookchain(monkeypatch) -> None:
    """Default ``listen run`` wires the bare PatHook as ``on_tick`` — no HookChain.

    Today's behaviour is ``on_tick=PatHook(...)`` directly; the regression test
    asserts no HookChain is constructed when ``--live`` is absent, so the default
    path is byte-for-byte the established single-hook one.
    """
    captured = _spy_chain(monkeypatch)
    transport = _LiveSdkTransport()
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: transport)

    rc = main(
        ["listen", "run", "--json", "--transport", "sdk", "--deadband", "0", "--max-ticks", "5"]
    )
    assert rc == 0
    assert "hooks" not in captured, "default listen must not build a HookChain"


def test_live_flag_defaults_off(monkeypatch) -> None:
    """The ``--live`` flag is opt-in: it defaults to False when not passed."""
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    listen_mod.register(sub)
    ns = parser.parse_args(["listen", "run"])
    assert getattr(ns, "live", False) is False
    ns2 = parser.parse_args(["listen", "run", "--live"])
    assert ns2.live is True


# ---------------------------------------------------------------------------
# Issue #51 — pose/move/frame reads ride the ONE open session, never a per-call
# (leaking) client. Covers both the --live loop and the deployed plain listen.
# ---------------------------------------------------------------------------


def _run_plain_listen(monkeypatch, transport, *, max_ticks):
    """Run the DEPLOYED path — plain ``listen run`` (no --live) — against *transport*."""
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: transport)
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        return main(
            [
                "listen",
                "run",
                "--json",
                "--transport",
                "sdk",
                "--deadband",
                "0",
                "--max-ticks",
                str(max_ticks),
            ]
        )
    finally:
        sys.stdout = old


def test_live_pose_move_frame_do_not_leak_per_tick(monkeypatch) -> None:
    """Issue #51: ``--live`` must not open a per-call SDK client per tick.

    The crash-loop was a per-tick fd leak — ``head_pose`` (every tick) and
    ``move_goto`` (per move) each opened *and leaked* a fresh ReachyMini (via the
    SDK's ``GStreamerAudio`` teardown). After the fix those ride the one open
    session, so the base per-call path is hit only a small CONSTANT number of
    times (the one-shot preflight/settle recenters) that does NOT grow with ticks.
    """
    short = _LiveSdkTransport()
    assert _run_live_cli(monkeypatch, short, max_ticks=5) == 0
    long = _LiveSdkTransport()
    assert _run_live_cli(monkeypatch, long, max_ticks=40) == 0

    assert short.media_opens == 1 and long.media_opens == 1
    assert long.base_calls == short.base_calls, (
        "per-call SDK client opens scaled with ticks — the issue #51 per-tick fd "
        f"leak is still present ({short.base_calls} at 5 ticks, {long.base_calls} at 40)"
    )


def test_default_listen_does_not_leak_per_tick(monkeypatch) -> None:
    """The DEPLOYED crash path (plain ``listen run``, no --live) is leak-free too.

    Same tick-invariance proof as the live case: the standard listen service's
    per-tick PatHook ``head_pose`` read and per-move ``move_goto`` now route
    through the one open session, so base per-call opens do not grow with ticks.
    """
    short = _LiveSdkTransport()
    assert _run_plain_listen(monkeypatch, short, max_ticks=5) == 0
    long = _LiveSdkTransport()
    assert _run_plain_listen(monkeypatch, long, max_ticks=40) == 0

    assert short.media_opens == 1 and long.media_opens == 1
    assert long.base_calls == short.base_calls, (
        "plain listen scaled per-call SDK opens with ticks — issue #51 leak present "
        f"({short.base_calls} at 5 ticks, {long.base_calls} at 40)"
    )


def test_sample_tap_reads_audio_once_and_rms_matches_snap_chunk():
    """Qodo PR #50 (comment 4): the --live sample tap must read audio ONCE per tick.

    The mic chunk is read once inside ``_audio`` (which stashes its loudness in
    ``audio_rms``); the sense tap reuses that exact value, so the stored
    ``SenseSample.rms`` reflects the SAME chunk the snap/sound_present decision
    used — no second ``get_audio_sample()`` (which would desync the RMS and drop
    half the audio).
    """
    holder = listen_mod.SampleHolder()
    audio_rms = {"rms": 0.0}
    reads = {"n": 0}

    def fake_audio(t):
        reads["n"] += 1  # stands in for the single get_audio_sample() read
        audio_rms["rms"] = 0.7  # _audio stashes this tick's loudness
        return (True, True)  # snap, sound_present

    class _FakeSense:
        doa_angle = np.pi / 2
        speech_detected = False

    sense_tap, audio_tap = listen_mod._build_sample_tap(
        holder, lambda t: _FakeSense(), fake_audio, audio_rms
    )
    audio_tap(0.0)  # server.run calls the audio tap...
    sense_tap(0.0)  # ...then the sense tap, each tick

    assert reads["n"] == 1, "the tick must read the mic chunk exactly once"
    assert holder.latest.rms == 0.7, "stored RMS must be the chunk audio() actually read"
    assert holder.latest.speech is True  # snap OR speech -> speech True
