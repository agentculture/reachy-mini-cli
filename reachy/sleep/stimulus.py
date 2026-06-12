"""Qualifying-stimulation classifier for the sleep / wake-threshold engine.

A *stimulus* is a sensor event that should reset the robot's idle (sleep) timer
and keep it "awake".  Four event kinds qualify:

- **DoA shift** — the sound Direction-of-Arrival angle moved; a new sound source
  appeared or the source relocated.  The caller computes the shift by comparing
  the current :attr:`~reachy.behavior.sense.Sense.doa_angle` with the previous
  tick's angle and passes the result as the ``doa_shift`` bool.
- **speech_detected** — the daemon flagged active speech in the current
  :class:`~reachy.behavior.sense.Sense` snapshot.
- **snap** — the :class:`~reachy.motion.snap.SnapDetector` fired for this chunk
  (loud transient: clap, knock, snap).  The caller passes the detector's ``feed()``
  return value as the ``snap`` bool.
- **pat** — the :class:`~reachy.motion.pat.PatDetector` returned a level1/level2
  event this tick.  The caller passes ``True`` when that happens.

Self-mute exclusion
-------------------
While ``now < mute_until`` the sample is inside the self-mute window that
:mod:`reachy.cli._commands.think` stamps after each playback clip.  Inside this
window the robot's own voice is on the shared USB audio device and any acoustic
cue (DoA shift, speech flag, snap) is almost certainly self-generated.  The
function returns ``False`` for *any* event kind during this window — the robot
cannot keep itself awake by speaking.

Boundary: ``now == mute_until`` is treated as *expired* (not suppressed), matching
the ``now < mute_until`` check in ``think``'s ``_guarded_feed``.

audio_wake flag
---------------
When ``audio_wake=False`` the three acoustic stimuli (``doa_shift``,
``sense.speech_detected``, ``snap``) are silently ignored — only ``pat`` can
return ``True``.  Use this when the microphone array is disabled or the operator
wants the robot to wake only on physical touch.  The self-mute guard still applies
first regardless of ``audio_wake``.  The default ``audio_wake=True`` preserves
the existing behavior byte-for-byte.

Dependencies: stdlib + numpy only (numpy imported transitively via the ``Sense``
import chain, but this module itself uses only stdlib).  No transport, no SDK.

Public API
----------
.. function:: is_stimulus(sense, *, doa_shift, snap, pat, now, mute_until, audio_wake) -> bool

    The single public entry point.  All qualifier flags are keyword-only after
    ``sense`` to prevent positional mis-ordering at the call site.
"""

from __future__ import annotations

from reachy.behavior.sense import Sense


def is_stimulus(
    sense: Sense,
    *,
    doa_shift: bool,
    snap: bool,
    pat: bool,
    now: float,
    mute_until: float,
    audio_wake: bool = True,
) -> bool:
    """Return ``True`` when *sense* + event flags represent a qualifying stimulus.

    Parameters
    ----------
    sense:
        The current sensor snapshot (see :class:`~reachy.behavior.sense.Sense`).
        ``sense.speech_detected`` is checked directly; ``sense.doa_angle`` itself
        is not evaluated here — the *caller* compares successive angles and
        passes the result as ``doa_shift``.
    doa_shift:
        ``True`` when the DoA angle has moved by more than the caller's deadband
        since the previous sample — i.e. a new sound direction was detected.
    snap:
        ``True`` when :meth:`~reachy.motion.snap.SnapDetector.feed` fired for
        the current audio chunk (loud transient).
    pat:
        ``True`` when :class:`~reachy.motion.pat.PatDetector` returned a
        level1/level2 event this tick.
    now:
        Current monotonic time (seconds).  Typically ``time.monotonic()``.
    mute_until:
        End of the self-mute window (monotonic seconds) stamped by the speech
        playback guard.  Pass ``0.0`` when no mute is active.  While
        ``now < mute_until`` the function returns ``False`` regardless of events.
    audio_wake:
        When ``True`` (default) all four event kinds are considered; preserves
        existing behavior.  When ``False`` the three acoustic stimuli
        (``doa_shift``, ``sense.speech_detected``, ``snap``) are ignored and
        only ``pat`` can trigger a ``True`` return — use this when the
        microphone array is off or the operator wants touch-only wake.

    Returns
    -------
    bool
        ``True`` iff the sample contains at least one qualifying event AND the
        sample was not captured inside the self-mute window.
    """
    # Self-mute guard: suppress everything while the robot's own voice could be
    # on the mic.  Boundary (now == mute_until) is treated as expired.
    if now < mute_until:
        return False

    if audio_wake:
        # Any one of the four qualifying event kinds is sufficient.
        return doa_shift or sense.speech_detected or snap or pat
    else:
        # Acoustic stimuli are disabled; only physical touch (pat) qualifies.
        return pat
