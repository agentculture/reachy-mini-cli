"""Fold sleep's decay-to-sleep + wake state machine into the ``listen`` loop.

``sleep`` used to run as its own noun against its own single-consumer SDK media
session.  But the hardware has exactly ONE single-consumer media subsystem, so a
second reader contends and throttles to ~1 Hz — the same constraint that drove
the #43 ``PatHook`` fold-in (see the single-SDK-owner model in ``CLAUDE.md``).
Running ``listen`` and ``sleep`` as two processes is therefore impossible.

This module resolves that by providing :class:`SleepHook` — a per-tick hook with
the *same* ``(transport, queue, t, commanded_head)`` signature as
:class:`~reachy.motion.listen_pat.PatHook`, installed on
:func:`reachy.motion.server.run`'s ``on_tick`` seam.  It runs *inside* ``listen``'s
loop and consumes the loop's **shared** per-tick
:class:`~reachy.motion.sense_sample.SenseSample` (via an injected
:data:`~reachy.motion.sense_sample.SampleProvider`) rather than opening a second
media session.  The hook is pure *glue*: it reuses the sleep engines verbatim —

* :class:`~reachy.sleep.state.SleepStateMachine` — the injected-clock idle timer
  that walks ALERT → DROWSY → ASLEEP;
* :func:`~reachy.sleep.stimulus.is_stimulus` — the qualifying-stimulation
  classifier (speech / DoA shift / snap / pat, with the self-mute exclusion);
* :class:`~reachy.sleep.wake.WakeDetector` — Tier-1 speech/snap wake (Tier-2
  wake-word stays off in the fold-in);
* :class:`~reachy.motion.pat.PatDetector` — pat-based wake measured against the
  loop's *commanded* head pose, exactly as ``PatHook`` does (``actual −
  commanded``), so ``listen``'s own motion reads as zero deviation.

On every tick it: reads the shared sample, classifies stimulation, then either
resets the idle timer to ALERT (on a stimulus) or decays it; and reflects the
resulting state into the ``sleep_active`` flag — raised while DROWSY/ASLEEP (the
**strongest** idle interrupt: ``sleep`` > ``pat`` > ``think``), cleared the moment
the robot is no longer drowsy.  The flag is always cleared on the way out (see
:meth:`SleepHook.close`), even if the loop is interrupted mid-sleep.  A missing
sample, a ``head_pose`` read-back error, or any transport hiccup degrades to "no
stimulation / no pat" — never a raised exception that would kill the loop.

``now`` comes straight from the loop's clock (handed in as ``t``), so the hook
inherits the loop's determinism with no extra clock seam.  Stdlib + numpy only;
no ``ReachyMini`` import, so it loads without the ``[sdk]`` extra.
"""

from __future__ import annotations

import numpy as np

from reachy.behavior.sense import Sense
from reachy.motion import sleep_signal
from reachy.motion.pat import PatDetector
from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SampleProvider, SenseSample
from reachy.sleep.state import SleepState, SleepStateMachine
from reachy.sleep.stimulus import is_stimulus
from reachy.sleep.wake import WakeDetector

#: The pre-first-action commanded head pose ``listen`` rests at before it has
#: dispatched any move (mirrors :data:`reachy.motion.listen_pat._NEUTRAL_HEAD`).
_NEUTRAL_HEAD: dict[str, float] = {"pitch": 0.0, "yaw": 0.0}

#: DoA deadband (degrees): a DoA angle move smaller than this is not a "shift".
#: ``SenseSample.doa`` is carried in degrees, so the deadband is in degrees too
#: (the standalone sleep loop uses a radian deadband on ``doa_angle``; this is
#: the degrees-domain equivalent for the shared sample).
_DOA_DEADBAND_DEG: float = 11.5

#: Number of samples in the synthetic audio chunk fed to the snap/wake detector.
#: The detectors only use ``sqrt(mean(audio**2))`` (== the constant RMS we fill),
#: so any non-trivial length works; this matches a typical mic chunk size.
_CHUNK_LEN: int = 512


def _sample_to_sense(sample: SenseSample) -> Sense:
    """Map the loop's shared :class:`SenseSample` onto a :class:`Sense`.

    ``is_stimulus`` / :class:`WakeDetector` consume a :class:`Sense`; the shared
    sample carries the same cues under different names.  ``doa`` (degrees) is
    forwarded as ``doa_angle`` and ``speech`` as ``speech_detected``.  The DoA
    *shift* is computed separately by the hook (it needs the previous angle), so
    the angle value itself is only carried for that comparison.
    """
    return Sense(doa_angle=sample.doa, speech_detected=sample.speech)


def _rms_to_chunk(rms: float) -> np.ndarray:
    """Synthesize a constant float32 chunk whose RMS equals ``rms``.

    The ``listen`` loop already computed this tick's loudness (the sample's
    ``rms``); rather than re-reading the raw audio array (which the shared sample
    does not carry — and which would mean a second consume of the single media
    session), we reconstruct an equivalent chunk.  :meth:`SnapDetector.feed` and
    the wake-word leg use only ``sqrt(mean(audio**2))``, which for a constant
    array equals the constant — so a flat array filled with ``rms`` reproduces the
    exact loudness the snap detector needs, with no audio re-read.
    """
    return np.full(_CHUNK_LEN, float(rms), dtype=np.float32)


class SleepHook:
    """A per-tick ``on_tick`` hook running sleep's decay→wake inside ``listen``.

    Construct one with the loop's shared
    :data:`~reachy.motion.sense_sample.SampleProvider`, then pass :meth:`__call__`
    as ``on_tick=`` to :func:`reachy.motion.server.run`.  Call :meth:`close` in the
    loop's ``finally`` so the ``sleep_active`` flag never leaks past the run.

    Parameters
    ----------
    sample_provider:
        Zero-arg callable returning the loop's latest
        :class:`~reachy.motion.sense_sample.SenseSample`, or ``None`` for "no
        fresh sample this tick" (degraded to no-stimulation).  This is the ONLY
        audio source — the hook never opens a media session.
    drowsy_after / asleep_after:
        Idle-timer thresholds (seconds) forwarded to
        :class:`~reachy.sleep.state.SleepStateMachine`.
    audio_wake:
        When ``True`` (default) speech / DoA shift / snap all qualify as
        stimulation; when ``False`` (pat-only) the acoustic cues are ignored and
        only a head pat wakes the robot — mirrors the ``sleep`` noun's
        ``--no-audio-wake``.
    machine / wake_detector / pat_detector:
        Optional pre-built engines (tests inject tuned/deterministic ones); fresh
        defaults are built when omitted.  The engines are reused verbatim — this
        hook adds no new sleep logic.
    pat_cooldown:
        Forwarded to a default :class:`PatDetector` (ignored when ``pat_detector``
        is supplied); tests pass ``0.0`` to fire a pat without an inter-pat gate.
    """

    def __init__(
        self,
        sample_provider: SampleProvider,
        *,
        drowsy_after: float = 75.0,
        asleep_after: float = 150.0,
        audio_wake: bool = True,
        machine: SleepStateMachine | None = None,
        wake_detector: WakeDetector | None = None,
        pat_detector: PatDetector | None = None,
        pat_cooldown: float = 2.0,
    ) -> None:
        self._sample = sample_provider
        self._audio_wake = audio_wake
        self.machine = (
            machine
            if machine is not None
            else SleepStateMachine(drowsy_after=drowsy_after, asleep_after=asleep_after)
        )
        self._wake = (
            wake_detector if wake_detector is not None else WakeDetector(wake_word_enabled=False)
        )
        self._pat = (
            pat_detector if pat_detector is not None else PatDetector(pat_cooldown=pat_cooldown)
        )
        #: Last DoA angle (degrees) seen, for the shift comparison.
        self._prev_doa: float | None = None
        #: Whether this hook currently holds the ``sleep_active`` flag raised.
        self._flag_up = False
        #: Count of wake events fired this run (diagnostics / tests).
        self.woke_events = 0

    # ------------------------------------------------------------------
    # Public read-only snapshot
    # ------------------------------------------------------------------

    @property
    def state(self) -> SleepState:
        """The current :class:`~reachy.sleep.state.SleepState`."""
        return self.machine.state

    # ------------------------------------------------------------------
    # Per-tick entry point
    # ------------------------------------------------------------------

    def __call__(
        self,
        transport: object,
        queue: MotionQueue,
        t: float,
        commanded_head: dict[str, float] | None = None,
    ) -> None:
        """One tick: classify stimulation, decay-or-reset the timer, sync the flag.

        ``queue`` is the live loop queue (kept for the ``on_tick`` contract; sleep
        motion is task t4's :class:`~reachy.motion.sleep.SleepProducer`, not this
        glue).  ``commanded_head`` is the ``{"pitch", "yaw"}`` pose the loop last
        dispatched — the baseline the pat-wake deviation is measured against.
        Never blocks, never raises.
        """
        head = commanded_head or _NEUTRAL_HEAD
        stimulated = self._stimulated(transport, t, head)
        if stimulated:
            self.machine.reset(now=t)
            self._wake.reset()
            self._pat.reset()
            self.woke_events += 1
        else:
            self.machine.update(now=t)
        self._sync_flag()

    # ------------------------------------------------------------------
    # Stimulation classification (pure glue over the reused engines)
    # ------------------------------------------------------------------

    def _stimulated(self, transport: object, now: float, commanded_head: dict[str, float]) -> bool:
        """Return whether this tick carries a qualifying wake stimulus.

        Reads the shared sample, computes the DoA shift, runs
        :func:`~reachy.sleep.stimulus.is_stimulus` (with the pat-wake result), and
        — when audio wake is on and nothing has fired yet — consults the Tier-1
        :class:`WakeDetector` (speech/snap) on the reconstructed chunk.  A missing
        sample degrades to "no stimulation".
        """
        sample = self._read_sample()
        pat = self._pat_wake(transport, now, commanded_head)
        if sample is None:
            # No fresh audio cue this tick — only a pat can wake.
            return pat

        sense = _sample_to_sense(sample)
        doa_shift = self._doa_shifted(sample.doa)
        # Mirror the sleep loop: advance the prev-DoA only on a real reading.
        if sample.doa is not None:
            self._prev_doa = sample.doa

        stimulated = is_stimulus(
            sense,
            doa_shift=doa_shift,
            snap=False,  # snap is folded into the WakeDetector leg below
            pat=pat,
            now=now,
            mute_until=0.0,
            audio_wake=self._audio_wake,
        )
        # Tier-1 speech/snap wake: only when audio wake is on and nothing fired yet.
        if self._audio_wake and not stimulated:
            stimulated = self._wake.update(sense, _rms_to_chunk(sample.rms))
        return stimulated

    def _read_sample(self) -> SenseSample | None:
        """Read the shared sample, degrading any provider error to ``None``."""
        try:
            return self._sample()
        except Exception:  # noqa: BLE001
            return None

    def _doa_shifted(self, curr: float | None) -> bool:
        """True when the DoA angle moved past the deadband since the last reading."""
        return (
            curr is not None
            and self._prev_doa is not None
            and abs(curr - self._prev_doa) > _DOA_DEADBAND_DEG
        )

    def _pat_wake(self, transport: object, now: float, commanded_head: dict[str, float]) -> bool:
        """Feed the pat detector the commanded-vs-actual deviation; return a fire.

        Mirrors :class:`~reachy.motion.listen_pat.PatHook`: read the actual head
        pose back via ``transport.head_pose()`` and measure deviation against the
        loop's *commanded* pose so ``listen``'s own motion reads as zero deviation.
        A :class:`CliError` (or any transport hiccup) is swallowed and treated as
        no deviation — the read-back is taken to equal the commanded pose.
        """
        commanded_pitch = float(commanded_head.get("pitch", 0.0))
        commanded_yaw = float(commanded_head.get("yaw", 0.0))
        head_pose = getattr(transport, "head_pose", None)
        if not callable(head_pose):
            return False
        try:
            actual_pitch, actual_yaw = head_pose()
        except Exception:  # noqa: BLE001
            actual_pitch, actual_yaw = commanded_pitch, commanded_yaw
        event = self._pat.update(commanded_pitch, actual_pitch, commanded_yaw, actual_yaw, now=now)
        return event is not None

    # ------------------------------------------------------------------
    # Flag bookkeeping
    # ------------------------------------------------------------------

    def _sync_flag(self) -> None:
        """Raise the ``sleep_active`` flag while DROWSY/ASLEEP, clear it otherwise.

        The flag is the strongest idle interrupt (``sleep`` > ``pat`` > ``think``),
        so it goes up as soon as the robot starts nodding off (DROWSY) — not only
        when fully ASLEEP — and is cleared the moment it returns to ALERT.
        """
        asleep_or_drowsy = self.machine.state in (SleepState.DROWSY, SleepState.ASLEEP)
        if asleep_or_drowsy and not self._flag_up:
            sleep_signal.write()
            self._flag_up = True
        elif self._flag_up and not asleep_or_drowsy:
            sleep_signal.clear()
            self._flag_up = False

    def close(self) -> None:
        """Clear the ``sleep_active`` flag if this hook still holds it (idempotent).

        Always safe to call: :func:`reachy.motion.sleep_signal.clear` is a no-op
        when the flag is already absent.  The ``listen`` loop calls this in its
        ``finally`` so an interrupt mid-sleep never leaks the flag.
        """
        if self._flag_up or sleep_signal.is_active():
            sleep_signal.clear()
        self._flag_up = False
