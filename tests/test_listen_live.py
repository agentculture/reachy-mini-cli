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


# ---------------------------------------------------------------------------
# t6 — `--transcribe`: fold STT words into live cognition
#
# `listen run --live --transcribe` composes the already-built TranscribeHook so
# nearby speech is transcribed and the WORDS flow into the SAME EventBuffer the
# ThinkHook's CognitionEngine consumes. Off by default; byte-identical when off.
# ---------------------------------------------------------------------------


from reachy.cli._errors import EXIT_USER_ERROR, CliError  # noqa: E402
from reachy.motion.listen_transcribe import TranscribeHook  # noqa: E402


class _NoMediaTransport:
    """An http-style transport with NO ``media_session`` (no mic audio source).

    ``--transcribe`` (like ``--export``) requires the sdk media session; this
    transport must be rejected with a clean exit-1 user error.
    """

    name = "http"

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        return {"uuid": "fake"}

    def doa(self, *, timeout=None):  # noqa: ARG002
        return {"angle": np.pi / 2, "speech_detected": False}


def _run_capture(monkeypatch, argv, *, transport=None):
    """Run ``reachy <argv>`` (main catches CliError → rc); return (rc, stdout, stderr)."""
    if transport is not None:
        monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _a: transport)
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


def test_transcribe_without_live_is_clean_exit_1_before_transport(monkeypatch) -> None:
    """``--transcribe`` without ``--live`` is a clean exit-1 user error.

    The transcribe path only has a cognition buffer to feed inside the folded
    live loop, so a bare ``--transcribe`` is rejected — and rejected BEFORE
    ``get_transport`` (mirrors the ``--export`` ordering), so the combo error
    fires regardless of whether the sdk extra is installed. ``get_transport`` is
    patched to a tripwire to prove it is never reached.
    """
    called = {"transport": False}

    def _tripwire(_args):
        called["transport"] = True
        raise AssertionError("get_transport must not be reached")

    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", _tripwire)

    rc, _out, err = _run_capture(
        monkeypatch, ["listen", "run", "--transcribe", "--transport", "sdk", "--max-ticks", "1"]
    )

    assert rc == EXIT_USER_ERROR
    assert "--transcribe needs --live" in err
    assert "hint:" in err
    assert called["transport"] is False, "validation must run before get_transport"


def test_transcribe_resolver_requires_live_unit() -> None:
    """The resolver helper raises the documented exit-1 CliError without ``--live``."""
    import argparse

    args = argparse.Namespace(transcribe=True, live=False)
    with pytest.raises(CliError) as ei:
        listen_mod._resolve_transcribe(args)
    assert ei.value.code == EXIT_USER_ERROR
    assert "--transcribe" in ei.value.message and "--live" in ei.value.message

    # Off → resolver returns False (no error).
    args_off = argparse.Namespace(transcribe=False, live=False)
    assert listen_mod._resolve_transcribe(args_off) is False
    # On + live → resolver returns True.
    args_on = argparse.Namespace(transcribe=True, live=True)
    assert listen_mod._resolve_transcribe(args_on) is True


def test_transcribe_requires_sdk_transport(monkeypatch) -> None:
    """``--transcribe --live`` on a transport without ``media_session`` is exit-1.

    The http profile has no mic audio to transcribe; the transport check errors
    cleanly (mirrors ``_require_export_transport``).
    """
    transport = _NoMediaTransport()
    rc, _out, err = _run_capture(
        monkeypatch,
        ["listen", "run", "--live", "--transcribe", "--transport", "http", "--max-ticks", "1"],
        transport=transport,
    )

    assert rc == EXIT_USER_ERROR
    assert "--transcribe requires the sdk transport" in err
    assert "hint:" in err


def test_transcribe_transport_resolver_rejects_no_media() -> None:
    """The transport guard rejects a transport lacking ``media_session``."""
    with pytest.raises(CliError) as ei:
        listen_mod._require_transcribe_transport(True, _NoMediaTransport())
    assert ei.value.code == EXIT_USER_ERROR

    # Off, or a transport WITH media_session → no error.
    listen_mod._require_transcribe_transport(False, _NoMediaTransport())  # no raise
    listen_mod._require_transcribe_transport(True, _LiveSdkTransport())  # no raise


def test_transcribe_off_keeps_sample_audio_none_and_no_hook(monkeypatch) -> None:
    """``--transcribe`` OFF: no TranscribeHook, ``SenseSample.audio`` stays None.

    The byte-identical-when-off guarantee: without ``--transcribe`` the live loop
    builds no TranscribeHook and the shared per-tick sample carries no raw audio
    chunk (so the off path cannot POST a single byte to STT).
    """
    built = {"transcribe": 0}
    real_init = TranscribeHook.__init__

    def _count(self, *a, **k):
        built["transcribe"] += 1
        return real_init(self, *a, **k)

    monkeypatch.setattr(TranscribeHook, "__init__", _count)

    # Spy the live hook list to assert no TranscribeHook is composed.
    captured = _spy_chain(monkeypatch)
    transport = _LiveSdkTransport()
    rc = _run_live_cli(monkeypatch, transport, max_ticks=5, extra_args=["--idle-energy", "0"])
    assert rc == 0

    assert built["transcribe"] == 0, "live without --transcribe must build NO TranscribeHook"
    hooks = captured["hooks"]
    assert not any(isinstance(h, TranscribeHook) for h in hooks), hooks

    # And the shared per-tick sample carries no raw audio chunk when off.
    holder = listen_mod.SampleHolder()
    audio_rms = {"rms": 0.0, "audio": None}

    def fake_audio(t):
        audio_rms["rms"] = 0.3
        return (False, True)

    class _FakeSense:
        doa_angle = np.pi / 2
        speech_detected = True

    sense_tap, audio_tap = listen_mod._build_sample_tap(
        holder, lambda t: _FakeSense(), fake_audio, audio_rms, transcribe=False
    )
    audio_tap(0.0)
    sense_tap(0.0)
    assert holder.latest.audio is None, "off-mode must leave SenseSample.audio = None"


def test_transcribe_off_makes_zero_stt_posts(monkeypatch) -> None:
    """``--transcribe`` OFF: zero STT POSTs happen — the loop is observably unchanged.

    Patch the real :class:`~reachy.speech.stt.Transcriber.transcribe` to count
    calls; with ``--transcribe`` absent it must never be invoked.
    """
    posts = {"n": 0}

    def _count_transcribe(self, audio):  # noqa: ANN001
        posts["n"] += 1
        return None

    monkeypatch.setattr("reachy.speech.stt.Transcriber.transcribe", _count_transcribe, raising=True)

    transport = _LiveSdkTransport()
    rc = _run_live_cli(monkeypatch, transport, max_ticks=6, extra_args=["--idle-energy", "0"])
    assert rc == 0
    assert posts["n"] == 0, "off-mode must make zero STT transcription calls"


def test_transcribe_stashes_raw_chunk_once_per_tick(monkeypatch) -> None:
    """``--transcribe`` ON: the raw chunk lands on ``SenseSample.audio`` with ONE read.

    The same single ``get_audio_sample()`` read that feeds RMS/snap also retains
    the raw float32 chunk; the sense tap places it on ``SenseSample.audio``. There
    must be NO second ``get_audio_sample()`` call.
    """
    holder = listen_mod.SampleHolder()
    chunk = np.full(256, 0.4, dtype=np.float32)
    audio_rms = {"rms": 0.0, "audio": None}
    reads = {"n": 0}

    def fake_audio(t):
        reads["n"] += 1  # stands in for the ONE get_audio_sample() read
        audio_rms["rms"] = 0.5
        audio_rms["audio"] = chunk  # _audio stashes the raw chunk alongside rms
        return (False, True)

    class _FakeSense:
        doa_angle = np.pi / 2
        speech_detected = True

    sense_tap, audio_tap = listen_mod._build_sample_tap(
        holder, lambda t: _FakeSense(), fake_audio, audio_rms, transcribe=True
    )
    audio_tap(0.0)
    sense_tap(0.0)

    assert reads["n"] == 1, "the tick must read the mic chunk exactly once"
    assert holder.latest.audio is chunk, "the raw chunk must land on SenseSample.audio"
    assert holder.latest.rms == 0.5


def test_transcribe_audio_stashed_via_full_loop(monkeypatch) -> None:
    """End-to-end: ``--live --transcribe`` retains the loop's raw chunk on the sample.

    Drive the real CLI loop; the fake session's ``get_audio_sample`` returns one
    known chunk and counts its calls. After the bounded run the shared sample
    carries that exact chunk, and the session was read once per tick (no second
    read for STT).
    """

    class _CountingSession(_Session):
        def __init__(self):
            super().__init__()
            self.audio_reads = 0
            self._chunk = np.full(512, 0.001, dtype=np.float32)

        def get_audio_sample(self):
            self.audio_reads += 1
            return self._chunk

    class _CountingTransport(_LiveSdkTransport):
        def __init__(self):
            super().__init__()
            self._session = _CountingSession()

    transport = _CountingTransport()
    rc = _run_live_cli(
        monkeypatch, transport, max_ticks=4, extra_args=["--idle-energy", "0", "--transcribe"]
    )
    assert rc == 0
    sess = transport._session
    # One audio read per tick (max_ticks ticks); no second read for transcription.
    assert sess.audio_reads == 4, f"expected one read per tick, got {sess.audio_reads}"


def test_transcribe_hook_shares_thinkhook_buffer(monkeypatch) -> None:
    """``--transcribe`` ON: the TranscribeHook's buffer IS the ThinkHook engine's buffer.

    The crux of t6: words transcribed by the TranscribeHook must flow into the
    SAME :class:`~reachy.speech.events.EventBuffer` the CognitionEngine consumes.
    Capture the buffer passed to ThinkHook and the buffer passed to TranscribeHook
    and assert they are the one same object.
    """
    captured: dict[str, object] = {}
    real_think_init = ThinkHook.__init__
    real_tr_init = TranscribeHook.__init__

    def _think_init(self, sample_provider, **kw):
        captured["think_buffer"] = kw.get("buffer")
        return real_think_init(self, sample_provider, **kw)

    def _tr_init(self, sample_provider, **kw):
        captured["transcribe_buffer"] = kw.get("buffer")
        captured["transcribe_provider"] = sample_provider
        return real_tr_init(self, sample_provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)
    monkeypatch.setattr(TranscribeHook, "__init__", _tr_init)

    captured_chain = _spy_chain(monkeypatch)
    transport = _LiveSdkTransport()
    rc = _run_live_cli(
        monkeypatch, transport, max_ticks=3, extra_args=["--idle-energy", "0", "--transcribe"]
    )
    assert rc == 0

    # A TranscribeHook is in the composed live chain.
    hooks = captured_chain["hooks"]
    assert any(isinstance(h, TranscribeHook) for h in hooks), hooks

    assert captured.get("think_buffer") is not None, "ThinkHook must receive a buffer"
    assert (
        captured["transcribe_buffer"] is captured["think_buffer"]
    ), "TranscribeHook must feed the SAME buffer the ThinkHook engine consumes"


def test_transcribe_feeds_words_into_shared_cognition_buffer(monkeypatch) -> None:
    """A transcript fed by the TranscribeHook lands as a cue in the shared buffer.

    With a fake Transcriber returning a fixed phrase, after the loop the shared
    buffer (the one the cognition engine snapshots) carries a ``heard someone
    say`` cue — proving the words reach cognition.
    """
    fixed = {"text": "hello there robot"}  # names the robot → passes the engagement gate

    def _fake_once(self, audio):  # noqa: ANN001 — the hook transcribes the whole utterance
        return fixed["text"]

    monkeypatch.setattr("reachy.speech.stt.Transcriber.transcribe_once", _fake_once)

    captured: dict[str, object] = {}
    real_tr_init = TranscribeHook.__init__

    def _tr_init(self, sample_provider, **kw):
        # The hook now buffers a whole utterance and flushes on a pause / max length.
        # Force a flush on the very first speech tick (max_utterance_s=0) with no
        # minimum-duration floor, so the end-to-end path fires within the mocked ticks.
        kw.setdefault("max_utterance_s", 0.0)
        kw.setdefault("min_utterance_s", 0.0)
        captured["buffer"] = kw.get("buffer")
        return real_tr_init(self, sample_provider, **kw)

    monkeypatch.setattr(TranscribeHook, "__init__", _tr_init)

    # The fake session reports speech so the sample carries speech=True + audio; with
    # max_utterance_s=0 the first speech tick accumulates one chunk and flushes it.
    class _SpeechSession(_Session):
        def doa(self, *, timeout=None):  # noqa: ARG002
            return {"angle": np.pi / 2, "speech_detected": True}

    class _SpeechTransport(_LiveSdkTransport):
        def __init__(self):
            super().__init__()
            self._session = _SpeechSession()

    transport = _SpeechTransport()
    rc = _run_live_cli(
        monkeypatch, transport, max_ticks=4, extra_args=["--idle-energy", "0", "--transcribe"]
    )
    assert rc == 0

    buffer = captured["buffer"]
    assert buffer is not None
    cues = buffer.snapshot()
    texts = [c.text for c in cues]
    assert any("hello there robot" in t for t in texts), texts


def test_transcribe_self_mute_wired_to_play_audio(monkeypatch) -> None:
    """The play_audio wrapper stamps the mute window the TranscribeHook reads.

    Wiring proof (c10): the cognition engine's ``play_audio`` is wrapped so each
    played clip stamps a shared ``mute["until"]``, and the TranscribeHook is given
    a ``mute_until`` callable reading that same window. We capture both and prove
    that invoking the wrapped play_audio moves the mute deadline the hook reads.
    """
    captured: dict[str, object] = {}
    real_engine_init = None

    import reachy.speech.cognition as cog_mod

    real_engine_init = cog_mod.CognitionEngine.__init__

    def _engine_init(self, **kw):
        captured["play_audio"] = kw.get("play_audio")
        return real_engine_init(self, **kw)

    monkeypatch.setattr(cog_mod.CognitionEngine, "__init__", _engine_init)

    real_tr_init = TranscribeHook.__init__

    def _tr_init(self, sample_provider, **kw):
        captured["mute_until"] = kw.get("mute_until")
        return real_tr_init(self, sample_provider, **kw)

    monkeypatch.setattr(TranscribeHook, "__init__", _tr_init)

    # The play_audio wrapper imports reachy.speech.playback.play_audio lazily; stub
    # it to a no-op so invoking the wrapper does not touch the (absent) SDK.
    monkeypatch.setattr("reachy.speech.playback.play_audio", lambda *a, **k: None)

    # Freeze the clock the wrapper stamps with so the assertion is deterministic.
    monkeypatch.setattr("time.monotonic", lambda: 100.0)

    transport = _LiveSdkTransport()
    rc = _run_live_cli(
        monkeypatch, transport, max_ticks=2, extra_args=["--idle-energy", "0", "--transcribe"]
    )
    assert rc == 0

    play = captured.get("play_audio")
    mute_until = captured.get("mute_until")
    assert callable(play), "the cognition engine must receive a wrapped play_audio"
    assert callable(mute_until), "the TranscribeHook must receive a mute_until callable"

    # Before any clip, the mute window is in the past (not muted).
    assert mute_until() <= 100.0
    # Playing a clip stamps the shared window forward (default ~2.5s mute-after).
    play(b"\x00\x00")
    assert mute_until() > 100.0, "play_audio must stamp the mute window the hook reads"


# ---------------------------------------------------------------------------
# t7 — wire the engagement classifier + the motion-ladder engaged signal,
# ONLY under `--transcribe`.
#
# Under `--transcribe`: an EngagementClassifier is built (sharing cognition's
# REACHY_OPENAI_* endpoint, i.e. NO connection overrides) and injected into the
# TranscribeHook, and `on_engage` is wired to the loop's ListenProducer.set_engaged
# so the gate's ENGAGE decision turns the head. Without `--transcribe`: NO
# classifier is built and the off path stays byte-identical (criterion 1).
# ---------------------------------------------------------------------------

from reachy.motion.listen import ListenProducer  # noqa: E402
from reachy.speech.engagement import EngagementClassifier  # noqa: E402


def test_transcribe_builds_classifier_and_wires_on_engage(monkeypatch) -> None:
    """``--transcribe`` ON: a classifier is built and ``on_engage`` is the producer's seam.

    Capture (a) every ``EngagementClassifier`` constructed and the connection kwargs
    it was built with, and (b) the ``classifier`` / ``on_engage`` passed to the
    TranscribeHook. Prove: exactly one classifier is built with NO endpoint overrides
    (so it resolves the same ``REACHY_OPENAI_*`` env cognition uses), and ``on_engage``
    is the loop ``ListenProducer``'s bound ``set_engaged``.
    """
    built: list[dict] = []
    real_cls_init = EngagementClassifier.__init__

    def _cls_init(self, **kw):
        built.append(dict(kw))
        return real_cls_init(self, **kw)

    monkeypatch.setattr(EngagementClassifier, "__init__", _cls_init)

    captured: dict[str, object] = {}
    real_tr_init = TranscribeHook.__init__

    def _tr_init(self, sample_provider, **kw):
        captured["classifier"] = kw.get("classifier")
        captured["on_engage"] = kw.get("on_engage")
        return real_tr_init(self, sample_provider, **kw)

    monkeypatch.setattr(TranscribeHook, "__init__", _tr_init)

    # Track set_engaged on the real producer so we can match the bound method identity.
    engaged_calls = {"n": 0}
    real_set_engaged = ListenProducer.set_engaged

    def _spy_engaged(self):
        engaged_calls["n"] += 1
        return real_set_engaged(self)

    monkeypatch.setattr(ListenProducer, "set_engaged", _spy_engaged)

    transport = _LiveSdkTransport()
    rc = _run_live_cli(
        monkeypatch, transport, max_ticks=3, extra_args=["--idle-energy", "0", "--transcribe"]
    )
    assert rc == 0

    # Exactly one classifier was built, with NO connection overrides → it resolves the
    # SAME REACHY_OPENAI_* endpoint the cognition engine resolves (shared config).
    assert len(built) == 1, f"expected one EngagementClassifier built, got {len(built)}"
    kw = built[0]
    for k in ("base_url", "model", "api_key"):
        assert kw.get(k) is None, f"classifier must not override {k} (share cognition's endpoint)"

    # The hook received that classifier and a callable on_engage...
    assert isinstance(captured.get("classifier"), EngagementClassifier)
    on_engage = captured.get("on_engage")
    assert callable(on_engage), "TranscribeHook must receive a callable on_engage"

    # ...and on_engage IS the producer's bound set_engaged: invoking it bumps the spy.
    before = engaged_calls["n"]
    on_engage()
    assert engaged_calls["n"] == before + 1, "on_engage must be the producer's set_engaged"


def test_transcribe_off_builds_no_classifier(monkeypatch) -> None:
    """``--transcribe`` OFF (live and bare): NO EngagementClassifier is constructed.

    The keystone byte-identical guarantee for criterion 1: nothing new is built
    without ``--transcribe`` — neither in a bare ``listen run`` nor in
    ``listen run --live`` without ``--transcribe``.
    """
    built = {"n": 0}
    real_cls_init = EngagementClassifier.__init__

    def _cls_init(self, **kw):
        built["n"] += 1
        return real_cls_init(self, **kw)

    monkeypatch.setattr(EngagementClassifier, "__init__", _cls_init)

    # 1) live WITHOUT --transcribe → no classifier.
    transport = _LiveSdkTransport()
    rc = _run_live_cli(monkeypatch, transport, max_ticks=4, extra_args=["--idle-energy", "0"])
    assert rc == 0
    assert built["n"] == 0, "live without --transcribe must build NO EngagementClassifier"


def test_transcribe_bare_run_builds_no_classifier(monkeypatch) -> None:
    """A bare ``listen run`` (no ``--live``, no ``--transcribe``) builds no classifier."""
    built = {"n": 0}
    real_cls_init = EngagementClassifier.__init__

    def _cls_init(self, **kw):
        built["n"] += 1
        return real_cls_init(self, **kw)

    monkeypatch.setattr(EngagementClassifier, "__init__", _cls_init)
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: _LiveSdkTransport())

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(
            [
                "listen",
                "run",
                "--json",
                "--transport",
                "sdk",
                "--deadband",
                "0",
                "--max-ticks",
                "3",
                "--idle-energy",
                "0",
            ]
        )
    finally:
        sys.stdout = old
    assert rc == 0
    assert built["n"] == 0, "a bare listen run must build NO EngagementClassifier"


def test_escape_hatch_skips_classifier_build(monkeypatch) -> None:
    """``REACHY_ENGAGE_HEURISTIC`` truthy: ``--transcribe`` builds NO classifier.

    When the escape hatch forces the heuristic, the hook would ignore a classifier
    anyway, so the build is skipped entirely (the wiring helper short-circuits).
    """
    monkeypatch.setenv("REACHY_ENGAGE_HEURISTIC", "1")
    built = {"n": 0}
    real_cls_init = EngagementClassifier.__init__

    def _cls_init(self, **kw):
        built["n"] += 1
        return real_cls_init(self, **kw)

    monkeypatch.setattr(EngagementClassifier, "__init__", _cls_init)

    transport = _LiveSdkTransport()
    rc = _run_live_cli(
        monkeypatch, transport, max_ticks=3, extra_args=["--idle-energy", "0", "--transcribe"]
    )
    assert rc == 0
    assert built["n"] == 0, "the escape hatch must skip the classifier build"


# ---------------------------------------------------------------------------
# t7 — no new base dependency, feature lives inside the --transcribe live loop
# ---------------------------------------------------------------------------


def test_base_dependencies_unchanged_no_new_dep() -> None:
    """The base ``project.dependencies`` is exactly {numpy, harmonics-cli}.

    The engagement classifier reuses the stdlib-``urllib`` ``llm`` client and the
    name match is stdlib-only, so the smart-hearing-engagement feature itself adds
    NO base dependency (criterion 2). ``harmonics-cli`` was added separately as a
    base dep by t2 (deviation d1); this guard still catches any *other* accidental
    requirements creep, e.g. an engine package like reachy-mini.
    """
    import pathlib
    import tomllib

    root = pathlib.Path(__file__).parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text())
    base_deps = data.get("project", {}).get("dependencies", [])

    names = sorted(d.split(">=")[0].split("==")[0].split("[")[0].strip().lower() for d in base_deps)
    assert names == [
        "harmonics-cli",
        "numpy",
    ], f"base dependencies must remain exactly [harmonics-cli, numpy], got {base_deps!r}"
