"""End-to-end acceptance tests for the two-tier ``listen`` success signals.

These tests drive the full ``reachy listen run --json`` pipeline with a stubbed
SDK transport (no hardware, no daemon) and assert the three spec-defined success
signals:

1. **Faint off-axis sound** (above floor but NOT a snap, no speech, off-axis DoA)
   → Tier-1 **antenna-only** action: ``antennas`` set, ``head is None``, no
   ``"yaw"`` in the emitted JSON.

2. **Clap / loud snap** (quiet→loud RMS transition, no speech, off-axis DoA)
   → Tier-2 **turn toward source**: ``"yaw"`` present in at least one emitted
   JSON action.

3. **Speech** (``speech_detected=True``, off-axis DoA, any RMS)
   → Tier-2 **head→body turn** toward source: ``"yaw"`` present in at least one
   emitted JSON action.

The fake SDK transport models the SDK path (has a ``media_session()``
contextmanager) and records every ``move_goto`` call so we can inspect both the
JSON output and the raw goto calls.
"""

from __future__ import annotations

import contextlib
import json

import numpy as np

from reachy.cli import main

# ---------------------------------------------------------------------------
# Quiet / loud sample helpers
# ---------------------------------------------------------------------------

_QUIET = np.full(512, 0.001, dtype=np.float32)  # rms ≈ 0.001 — below min_rms=0.02
_LOUD = np.full(512, 0.5, dtype=np.float32)  # rms = 0.5 — clears ratio×avg gate


# ---------------------------------------------------------------------------
# Fake media sessions
# ---------------------------------------------------------------------------


class _FaintOffAxisSession:
    """Steady quiet-but-above-floor signal from the left; no speech; no snap.

    We use rms=0.03 so ``sound_present`` is True (> min_rms=0.02) but never
    loud enough to trigger the SnapDetector (0.03 / 0.03_avg < ratio=5).
    The session returns the *same* sample forever — the RMS never spikes.
    """

    _SAMPLE = np.full(512, 0.03, dtype=np.float32)  # just above min_rms=0.02

    def doa(self, *, timeout=None):  # noqa: ARG002
        # angle=0.0 rad → left, off-axis; no speech.
        return {"angle": 0.0, "speech_detected": False}

    def get_audio_sample(self):
        return self._SAMPLE

    @property
    def samplerate(self):
        return 16000

    @property
    def channels(self):
        return 1


class _SnapOffAxisSession:
    """Quiet baseline for the first 10 calls, then a loud spike — triggers SnapDetector.

    The DoaPoller polls the session's ``doa()`` method; we keep speech_detected=False
    so the Tier-2 action is purely snap-driven.

    Algorithm need: SnapDetector needs ≥5 history entries, then fires when
    rms > ratio(5.0) × rolling_avg AND rms > min_rms(0.02) AND prev_chunk_low.
    """

    def __init__(self):
        self._call = 0

    def doa(self, *, timeout=None):  # noqa: ARG002
        return {"angle": 0.0, "speech_detected": False}  # left, no speech

    def get_audio_sample(self):
        self._call += 1
        # First 10: quiet baseline so SnapDetector builds rolling_avg ≈ 0.001.
        if self._call <= 10:
            return _QUIET
        # Loud spike: rms=0.5 >> 5.0 × 0.001 = 0.005; fires once (edge-triggered).
        return _LOUD

    @property
    def samplerate(self):
        return 16000

    @property
    def channels(self):
        return 1


class _SpeechOffAxisSession:
    """Steady quiet-but-present audio + speech_detected=True from the left."""

    _SAMPLE = np.full(512, 0.03, dtype=np.float32)

    def doa(self, *, timeout=None):  # noqa: ARG002
        return {"angle": 0.0, "speech_detected": True}

    def get_audio_sample(self):
        return self._SAMPLE

    @property
    def samplerate(self):
        return 16000

    @property
    def channels(self):
        return 1


# ---------------------------------------------------------------------------
# Fake SDK transport
# ---------------------------------------------------------------------------


class _FakeSdkTransport:
    """Minimal SDK transport stub: records gotos and exposes media_session()."""

    name = "sdk-acceptance"

    def __init__(self, session):
        self.gotos: list[dict] = []
        self._session = session

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        self.gotos.append(
            {
                "head": head,
                "antennas": antennas,
                "body_yaw": body_yaw,
                "duration": duration,
            }
        )
        return {"uuid": "fake"}

    @contextlib.contextmanager
    def media_session(self):
        yield self._session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_listen(monkeypatch, session, *, max_ticks=40, deadband=0, extra_args=None):
    """Run ``reachy listen run --json`` against a fake SDK transport.

    Returns (rc, actions) where *actions* is the list of parsed JSON dicts
    emitted by the loop (the ``_on_action`` callback path).
    """
    tr = _FakeSdkTransport(session)
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: tr)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    argv = [
        "listen",
        "run",
        "--json",
        "--transport",
        "sdk",
        "--deadband",
        str(deadband),
        "--max-ticks",
        str(max_ticks),
    ]
    if extra_args:
        argv.extend(extra_args)

    import io
    import sys

    # capsys is not available as a helper arg here; we monkeypatch stdout directly.
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = main(argv)
    finally:
        sys.stdout = old_stdout

    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    actions = []
    for ln in lines:
        try:
            obj = json.loads(ln)
            if "action" in obj:
                actions.append(obj)
        except json.JSONDecodeError:
            pass
    return rc, actions, tr


# ---------------------------------------------------------------------------
# Acceptance test 1 — Faint off-axis sound → Tier-1 antenna-only
# ---------------------------------------------------------------------------


def test_faint_off_axis_sound_antenna_only(monkeypatch) -> None:
    """Signal 1: quiet-but-present off-axis sound → Tier-1 antenna lean only.

    The loop must emit at least one action whose ``"yaw"`` is ``null`` (head=None
    in the raw action), and no action may carry a non-null yaw (i.e. no head turn).
    The transport's recorded gotos that are not preflight/settle must also be
    antenna-only (``head`` is None).
    """
    # head_only_band kept wide (default 30°); with deadband=0 and gain=0.6 and
    # angle=0.0 the raw desired is ~54°, which is beyond head_only_band but we
    # have no speech and no snap, so no Tier-2 fires — antenna-only always.
    rc, actions, tr = _run_listen(
        monkeypatch,
        _FaintOffAxisSession(),
        max_ticks=30,
        deadband=0,
    )

    assert rc == 0, f"cmd_listen_run returned {rc}"
    assert actions, "expected at least one action emitted"

    # No action should carry a non-null yaw.
    yaw_actions = [a for a in actions if a.get("yaw") is not None]
    assert not yaw_actions, (
        f"expected no head-turn actions (yaw must be null) for faint off-axis sound; "
        f"got: {yaw_actions}"
    )

    # At least one action must be an antenna action (yaw is null, action label present).
    antenna_actions = [a for a in actions if a.get("yaw") is None]
    assert antenna_actions, "expected at least one antenna-only (yaw=null) action"

    # The gotos recorded by the transport (skipping preflight/settle at index 0 and -1)
    # that are not the center-preflight should have head=None (antenna-only).
    non_center_gotos = [g for g in tr.gotos if (g.get("head") or {}).get("yaw", 0.0) != 0.0]
    for g in non_center_gotos:
        assert g.get("head") is None, f"Tier-1 goto must have head=None; got {g}"


# ---------------------------------------------------------------------------
# Acceptance test 2 — Clap / loud snap → Tier-2 turn toward source
# ---------------------------------------------------------------------------


def test_clap_snap_triggers_tier2_turn(monkeypatch) -> None:
    """Signal 2: quiet→loud snap transient, no speech, off-axis DoA → Tier-2 turn.

    The loop must emit at least one action with a non-null ``"yaw"`` after the
    snap fires.  We allow enough ticks for: baseline build (≥10 calls) + snap
    detection + serial queue dispatch after the executor clears hold/settle.
    """
    rc, actions, tr = _run_listen(
        monkeypatch,
        _SnapOffAxisSession(),
        # 60 ticks × 0.05 s/tick = 3.0 s simulated; the snap fires after ~10
        # quiet samples, then the executor should dispatch the Tier-2 turn.
        # Speed is the default (18 deg/s); min_dur=1.5 s occupies the executor
        # for at least 1.5 s + settle 0.2 s + hold 3.0 s = ~4.7 s.  We use a
        # fast speed override via extra args so the hold clears within 60 ticks.
        max_ticks=60,
        deadband=0,
        extra_args=["--speed", "1000", "--hold", "0"],
    )

    assert rc == 0, f"cmd_listen_run returned {rc}"

    # At least one emitted action must have a non-null yaw (Tier-2 head turn).
    yaw_actions = [a for a in actions if a.get("yaw") is not None]
    assert yaw_actions, (
        f"expected at least one Tier-2 yaw action after snap; all actions: {actions}; "
        f"gotos recorded: {tr.gotos}"
    )

    # The yaw for a left-side source (angle=0.0) must be positive.
    assert any(
        a["yaw"] > 0 for a in yaw_actions
    ), f"snap on the left should turn head +yaw (left); yaw_actions={yaw_actions}"


# ---------------------------------------------------------------------------
# Acceptance test 3 — Speech → Tier-2 head→body turn toward source
# ---------------------------------------------------------------------------


def test_speech_triggers_tier2_head_body_turn(monkeypatch) -> None:
    """Signal 3: speech_detected=True, off-axis DoA → Tier-2 head (or head+body) turn.

    The loop must emit at least one action with a non-null ``"yaw"``.  The angle
    (0.0 rad) maps to raw_desired ≈ 54° which exceeds the default
    head_only_band=30°, so the escalation path (body+head) fires — but even if
    it doesn't due to param interaction, *some* turn is required.

    We use --hold 0 and --speed 1000 so the move clears quickly; --deadband 0
    ensures the off-axis speech isn't eaten by the deadband filter.
    """
    rc, actions, tr = _run_listen(
        monkeypatch,
        _SpeechOffAxisSession(),
        max_ticks=40,
        deadband=0,
        extra_args=["--speed", "1000", "--hold", "0"],
    )

    assert rc == 0, f"cmd_listen_run returned {rc}"

    # At least one emitted action must carry a non-null yaw (Tier-2).
    yaw_actions = [a for a in actions if a.get("yaw") is not None]
    assert yaw_actions, (
        f"expected at least one Tier-2 yaw action for speech; all actions: {actions}; "
        f"gotos recorded: {tr.gotos}"
    )

    # Yaw must be positive (left-side source, angle=0.0 → +yaw).
    assert any(
        a["yaw"] > 0 for a in yaw_actions
    ), f"speech on the left should produce +yaw turn; yaw_actions={yaw_actions}"

    # Verify that the goto transport call also reflects a Tier-2 move:
    # at least one goto (beyond the preflight/settle) has a non-zero head yaw.
    non_center_head_gotos = [g for g in tr.gotos if (g.get("head") or {}).get("yaw", 0.0) != 0.0]
    assert (
        non_center_head_gotos
    ), f"expected at least one head-turn goto call for speech scenario; gotos={tr.gotos}"
