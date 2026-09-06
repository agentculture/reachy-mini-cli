"""Face lock — the ``lock_face`` / ``release_face`` intent kinds and the looping
``face-lock`` behavior they admit.

Locking onto a face is a DEDICATED intent kind, not something a mind composes
out of ``run_behavior`` + ``set_inhibition``: the LOCK STATE lives here, in the
runtime, so "am I looking at someone right now?" has exactly one answer no
matter which agent turn asked, and so releasing is one call that undoes
everything the lock did.

What ``lock_face`` does, in one atomic step
--------------------------------------------
1. Refuses fail-closed when there is no fresh :attr:`reachy.behavior.sense.Sense.face_bbox`
   (absent, or older than :data:`MAX_FACE_AGE_S`) — ``{"ok": false, "op":
   "lock_face", "error": "no face known"}``, admitting NOTHING. A lock with no
   face to lock onto would be a head frozen at neutral, indistinguishable from
   a wedged runtime.
2. Admits ONE looping, indefinite ``face-lock`` behavior (see
   :func:`make_face_lock`) that maps the current bbox centre to a head yaw/pitch
   target every tick, under its OWN clamp (``max_yaw`` 20 deg / ``max_pitch``
   12 deg by default — :mod:`reachy.behavior.goto_intent`'s head envelope, cited
   rather than re-derived). It holds its last target while the bbox is
   momentarily absent: a face lock does NOT end because a frame missed a
   detection. A face gone for :data:`FACE_LOST_AFTER_S` is REPORTED once, as
   ``motion.face-lost``, and the lock still persists (see below).
3. SNAPSHOTS the currently inhibited set and adds its own
   (:data:`LOCK_INHIBITS` — ``orient-to-sound``, the one behavior arbitration
   alone cannot keep off the face).

Why only ONE name, and why the lock claims the body too (issue #183)
---------------------------------------------------------------------
The lock used to inhibit ``feel-alive`` as well, which stilled the ANTENNAS for
the whole hold — the base layer is ONE behavior, so inhibiting it to protect
the head took the sway with it. Arbitration is per CHANNEL by (class priority,
recency) with abstention, and ``face-lock`` is ``STOPPABLE`` above the
``PASSIVE`` base layer, so the lock wins any channel it CLAIMS without evicting
anything. It therefore claims ``head`` AND ``body_yaw`` (see the library entry)
and contributes a constant, HELD ``body_yaw`` — the value the engine was
already streaming when the lock was taken — because ``feel-alive``'s slow body
wander (amplitude 6 deg at energy 1.0) rotates the whole head assembly, and the
camera with it, off the face. ``antennas`` is left unclaimed, so the base layer
keeps that channel and the antennas keep swaying under a lock.

``orient-to-sound`` STAYS inhibited: it is ``STOPPABLE`` like the lock, so a
later admission would win the head on the recency tie-break. Arbitration alone
cannot keep a same-class behavior off the face — only the inhibition can.

A lock cannot outlive its mind, nor be held forever
----------------------------------------------------
A lock is an INDEFINITE claim on the head taken on a mind's behalf, so the two
ways it could become a wedged robot are closed in :meth:`FaceLockDriver.on_tick`:
``mind_online()`` reading ``False`` for ``mind_offline_grace_s`` releases it
(``reason: "mind-offline"`` — nobody is left to call ``release_face``), and
``max_hold_s`` releases it regardless (``reason: "max-hold"``). A third ending
needs no timer at all: if the behavior leaves the active set without this driver
asking — ``behavior stop face-lock``, or a ``stop all`` from any surface — the
gaze is already gone, so the lock state follows it (``reason: "evicted"``)
rather than holding inhibitions for a head it no longer drives. Losing the FACE
is deliberately NOT one of them: that is reported and the lock persists. Every
release — including the explicit one, ``reason: "requested"`` — runs the one
:meth:`FaceLockDriver._release` path, so no ending can undo less than another.
``mind_online`` defaults to ``None`` (unknown), which never releases.

Inhibition is LATER-WINS
-------------------------
``set_inhibition`` REPLACES the whole inhibited set (see
:mod:`reachy.behavior.intents`), so a caller that replaces it WHILE locked has
made a deliberate, later statement about what IT holds — and
:meth:`FaceLockDriver.notice_inhibition_replaced` takes that statement as the
caller's set, so ``release_face`` never restores an older one behind the
caller's back.

Ownership is RECOMPUTED on every replacement, never frozen at acquisition. The
live set is re-asserted as ``new_set | LOCK_INHIBITS``: while the lock is held,
``orient-to-sound`` is inhibited no matter what the caller
wrote — including a name that was ALREADY operator-inhibited when the lock was
taken (so it was never "added") and that the replacement drops, which an
acquisition-time ownership set would have let start dragging the head off the
face under a still-held lock. What the lock then OWNS — and hands back on
release — is every ``LOCK_INHIBITS`` name the caller did not keep, plus, when
the replacement carries EVERY name the lock currently holds, those names too: a
set echoing all of ours back is a mind re-writing what it read (``stay_silent``
merging ``speak`` into ``state.json``'s list), not a statement about our claim,
and adopting it as operator-held would leave the presence loop inhibited after
release and the robot inert (observed live, 2026-08-26). Keeping only SOME of
the lock's names cannot be that echo, so those are the caller's own and survive
release. Release removes precisely the lock's CURRENT contribution — never a
name the caller holds.

Import boundary
-----------------
Like :mod:`reachy.behavior.goto_intent`, this module must never import
:mod:`reachy.behavior.control` or :mod:`reachy.behavior.intents`: registering
the two kinds into a live :class:`~reachy.behavior.control.KindRegistry` is
composition's job (``_compose_run_seam`` in ``reachy/cli/_commands/behavior.py``),
and the inhibited set is reached through two injected callables, not an import.
:mod:`reachy.behavior.library` is imported LAZILY inside
:meth:`FaceLockDriver.__init__` for the opposite reason: ``library`` imports
:func:`make_face_lock` from here at module scope (exactly as it does for
``feel-alive`` / ``orient-to-sound`` / ``pet-reaction``), so a module-scope
import back would be a cycle. ``tests/test_behavior_face_lock.py`` asserts the
boundary holds.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Callable, Iterable

from reachy.behavior.model import Behavior, Contribution, Lifetime, neutral_head
from reachy.cli._errors import EXIT_USER_ERROR, CliError

#: The two command kinds (the ``op`` field) this module answers to — following
#: :mod:`reachy.behavior.intents`'s bare-verb naming (``run_behavior``, ...).
LOCK_FACE = "lock_face"
RELEASE_FACE = "release_face"

logger = logging.getLogger(__name__)

#: The library entry the lock admits.
FACE_LOCK_BEHAVIOR = "face-lock"

#: What the lock adds to the inhibited set. ONE name since #183: the base layer
#: is handled by arbitration instead (the lock claims ``head`` + ``body_yaw``
#: and leaves ``antennas`` to ``feel-alive``), but ``orient-to-sound`` is the
#: lock's own contention class, so a later admission of it would win the head
#: on the recency tie-break. The tuple shape is kept for exactly that reason —
#: the set is "whatever arbitration cannot handle", not "one behavior".
LOCK_INHIBITS = ("orient-to-sound",)

#: A ``face_bbox`` older than this is not a face to lock onto. Matches the
#: producer's own TTL (``reachy.behavior.face_sense``), so the two agree.
MAX_FACE_AGE_S = 3.0
#: Why 3.0 and not the 1.5 it shipped with: since ``FaceObservation.captured_at``
#: a reading's age honestly INCLUDES the detection's own latency, and the
#: deployed Wireless detects every 1.0 s (``REACHY_FACE_DETECT_INTERVAL``) on
#: a CM4 — so a reading was 1.0 s + latency old by the time the next one
#: landed, past 1.5 s, and the lock ignored every reading after the first one
#: it admitted on: live (2026-09-06) it aimed once and then held that pose for
#: 40 s while the person moved. A late reading is not a WRONG reading — the
#: base-angle ring anchors the aim at the frame's capture time — so the bound
#: is ``FACE_LOST_AFTER_S``, the point at which absence is reported anyway.

#: The raw event every release emits, whatever ended the lock. ``motion.`` is the
#: prefix :mod:`reachy.export.runtime` maps onto ``MotionEvent``; both this action
#: and :data:`FACE_LOST_ACTION` are registered in its ``_RAW_MOTION_ACTIONS``
#: table, which is what puts them on the stdout feed AND on
#: ``reachy/events/motion/<action>`` (an unregistered action is dropped from both).
LOCK_RELEASED_ACTION = "lock-released"
EVENT_LOCK_RELEASED = f"motion.{LOCK_RELEASED_ACTION}"

#: The raw event a locked driver emits ONCE when the face has been gone too long.
#: A report, not an ending: the lock persists (see :meth:`FaceLockDriver.on_tick`).
FACE_LOST_ACTION = "face-lost"
EVENT_FACE_LOST = f"motion.{FACE_LOST_ACTION}"

#: The four named causes a :data:`EVENT_LOCK_RELEASED` carries in ``detail.reason``.
REASON_REQUESTED = "requested"
REASON_MIND_OFFLINE = "mind-offline"
REASON_MAX_HOLD = "max-hold"
#: The lock's behavior left the active set without this driver asking — a
#: ``behavior stop face-lock`` or a ``stop all`` from any other surface. The
#: gaze is already gone, so the LOCK STATE must follow it rather than sit there
#: claiming a head it no longer holds (and holding inhibitions for it).
REASON_EVICTED = "evicted"

#: How long the face must be absent/stale before the ONE ``face-lost`` report.
#: Well above vision's own TTL (:data:`MAX_FACE_AGE_S`): a dropped frame is not
#: a lost face, and this event is meant to be rare enough to mean something.
#:
#: Re-derived for #181/#179 against the LONGEST legitimate gap between two
#: detections while a lock is actively re-aiming, and it still holds at 3.0 s:
#: one detect interval (``face_sense.DEFAULT_DETECT_INTERVAL`` 0.5 s, 1.0 s as
#: deployed on the Wireless) + the post-motion settle #179 adds to the face
#: worker (<= 0.5 s) + the worst in-clamp slew the incremental aim can command
#: (the full 40 deg yaw envelope at :data:`SLEW_DEG_S` = 0.33 s) = 1.83 s. One
#: whole missed cycle on top of that is 2.83 s — inside 3.0, with ~0.2 s of
#: margin in the worst case and ~1.3 s in the shipped 0.5 s-interval case. So
#: the value is UNCHANGED; what changed is that it now has a derivation.
FACE_LOST_AFTER_S = 3.0

#: How long ``mind_online()`` must read ``False`` CONTINUOUSLY before the lock
#: releases itself. A lock is a standing claim taken on a mind's behalf; when the
#: mind is gone there is nobody left to release it, so it releases itself. The
#: grace exists so a harness restart (seconds) does not drop the head off a face.
MIND_OFFLINE_GRACE_S = 10.0

#: The longest a lock may be held at all — 30 minutes. Not a safety limit (the
#: behavior's clamp is that); a liveness limit, so a forgotten lock is bounded.
MAX_HOLD_S = 1800.0

#: Top-level fields a ``lock_face`` command may carry, beyond the spool envelope.
_ALLOWED_FIELDS = frozenset({"cmd_id", "op", "params"})

_ID_PREFIX = "face-lock"

# --------------------------------------------------------------------------- #
# The behavior — a self-clamping continuous gaze                              #
# --------------------------------------------------------------------------- #

#: Head yaw clamp (deg). Cited from :data:`reachy.behavior.goto_intent.HEAD_YAW_LIMIT_DEG`.
MAX_YAW_DEG = 20.0

#: Head pitch clamp (deg). Cited from :data:`reachy.behavior.goto_intent.HEAD_PITCH_LIMIT_DEG`.
MAX_PITCH_DEG = 12.0

#: Degrees commanded for a face at the very edge of the frame, BEFORE the clamp.
#: The OPEN-LOOP mapping the one-shot glance :mod:`reachy.behavior.gaze` still
#: uses (``plan_look_at_face``): a single glance has no loop to close, so it maps
#: the offset straight onto an absolute angle.
#:
#: The face LOCK no longer uses these (issue #181): an absolute map settles at
#: ``2*gain/FOV`` of the true bearing — ~0.31 on the Wireless camera — because
#: the offset shrinks as the head turns toward it. The lock's aim is now
#: INCREMENTAL, off :data:`HFOV_DEG` / :data:`VFOV_DEG` / :data:`DAMPING`.
YAW_GAIN_DEG = 20.0
PITCH_GAIN_DEG = 12.0

#: The camera's field of view in degrees, horizontal and vertical — what turns a
#: normalised bbox offset into a real ANGLE, and the whole reason the lock can
#: aim incrementally at all.
#:
#: Measured on the Reachy Mini Wireless from ``GET /api/camera/specs``'s
#: intrinsics (fx ~2002, cx ~1906 at 3840 px wide): ``2*atan(cx/fx)`` ~ 87 deg
#: horizontal, ~57 deg vertical. Overridable per lock as the ``fov_h`` / ``fov_v``
#: params, because a different camera is a different number — this module is a
#: leaf that imports no transport, so nothing reads ``/api/camera/specs`` HERE
#: (composition may resolve it once and inject the default; see the spec's
#: scope boundary for #181).
HFOV_DEG = 87.0
VFOV_DEG = 57.0

#: How much of the measured angular error one detection closes, 0..1. At 0.7 a
#: face 30 deg off-axis is within 2.7 deg after two detections and 0.8 deg after
#: three, and the error NEVER changes sign — the loop cannot overshoot, because
#: every step is a fraction of a measured error. 1.0 (aim exactly at the face) is
#: reachable through the ``damping`` param and is the twitchy end; the shipped
#: default keeps a margin for a stale or mis-centred bbox (decision c24, #181).
DAMPING = 0.7

#: How fast the commanded angle chases its target, in deg/s — specified in
#: TIME, never per tick, so the lock behaves identically at any tick rate
#: (the cadence-invariance lesson of issue #168).
SLEW_DEG_S = 120.0

#: Anti-windup for the incremental aim. Each new detection nudges the target by
#: the FOV-scaled bbox offset; if that offset never SHRINKS — a face beyond the
#: head's reach, or a false detection latched on a fixed edge (a printed face on
#: a wall) — the nudges accumulate and march the target to the clamp, where it
#: pins. So an OUTWARD push (one that moves the target further from neutral) is
#: only accepted while the measured offset is still shrinking by at least this
#: much (normalised frame units); a stalled offset freezes the target where it
#: is rather than driving on to the corner. Live on the Wireless (2026-09-06)
#: the un-guarded loop ran the head to its clamp and held there, staring at an
#: empty kitchen, for 80 s. Convergence itself is unaffected: a working lock's
#: offset shrinks ~(1-damping) each cycle, far above this floor.
CONVERGE_EPS = 0.03

#: How long the face may be genuinely ABSENT (no fresh bbox) before the lock
#: eases the gaze back toward neutral instead of holding a possibly-runaway
#: pose forever. A short absence is still HELD — vision drops frames, and a
#: person who steps out for a moment has not asked to be un-looked-at — so this
#: matches :data:`FACE_LOST_AFTER_S`. The lock is NOT released here: it stays
#: locked and re-aims the instant a face returns; only the HEAD relaxes, so a
#: lock that lost its face stops pointing at a wall.
RECOVER_AFTER_S = 3.0

#: How fast (deg/s) the gaze eases toward neutral once :data:`RECOVER_AFTER_S`
#: of true absence has passed. Gentle — this is a graceful relax, not a snap.
RECOVER_RATE_DEG_S = 30.0

#: Longest gap between two calls the slew integrates over. A resumed/stalled
#: process must not teleport the head.
_MAX_DT_S = 0.25

#: How many past ``(t, yaw, pitch)`` samples the base-pose ring keeps. The ring
#: answers ONE question — "what was I commanding when this frame was taken?" —
#: so it only has to reach back as far as a bbox may be old, i.e. the ``max_age``
#: param (:data:`MAX_FACE_AGE_S`, 1.5 s) plus slack. 256 samples is ~5 s at the
#: 50 Hz design cadence. A faster tick shortens the window rather than growing
#: the ring; the lookup then degrades to the OLDEST sample, which is the honest
#: answer ("this is as far back as I remember") and still much better than the
#: current pose.
_RING_MAXLEN = 256

#: How far the capture time must advance before an unchanged bbox counts as a
#: NEW observation. ``face_age_s`` is measured on the sense provider's clock and
#: ``t_local`` on the engine's, so a republished (stale) reading's derived
#: capture time jitters by microseconds between the two; 50 ms swallows that
#: while staying far below any real detection interval (0.5-1.0 s).
_NEW_OBSERVATION_EPS_S = 0.05

#: Degrees below which a "toward neutral" move is not counted as one — keeps
#: a re-application of the same clamped target from reading as progress and so
#: keeping a pinned runaway alive.
_RECOVER_ANGLE_EPS = 0.25


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _finite(value: object, fallback: float) -> float:
    """``float(value)`` when it is a finite real number, else *fallback*."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    number = float(value)
    return number if math.isfinite(number) else fallback


class FaceLockGaze:
    """The ``face-lock`` contribution function: hold the gaze on the seen face.

    Callable as ``fn(t_local, params, sense) -> Contribution``, so it plugs into
    :class:`reachy.behavior.model.Behavior` like any other sensor-driven library
    entry (:class:`reachy.behavior.orient.OrientToSound` is the sibling, and the
    reference for a self-clamping continuous behavior).

    The aim is INCREMENTAL and closed-loop (issue #181). ``sense.face_bbox``'s
    centre — normalised ``0..1``, origin top-left — is an ANGULAR ERROR once the
    camera's field of view is known::

        target_yaw   = base_yaw   - (cx - 0.5) * fov_h * damping
        target_pitch = base_pitch - (cy - 0.5) * fov_v * damping

    where ``base_*`` is the angle this gaze was COMMANDING WHEN THE FRAME WAS
    TAKEN, not the one it commands now — see :meth:`_base_at`. The commanded
    angle then eases toward the target at :data:`SLEW_DEG_S`. The sign
    convention is the repo's and is unchanged: ``+yaw`` is LEFT
    (:func:`reachy.behavior.sense.doa_angle_to_yaw`), so a face at large ``x``
    (the robot's right) yields a negative yaw; ``+pitch`` is up
    (``thoughtful``'s "upward/forward tilt"), so a face at small ``y`` yields a
    positive pitch.

    What this replaces, and why: the previous target was ABSOLUTE
    (``-(cx-0.5) * 2 * gain``), which is a proportional loop with gain
    ``2*gain/FOV`` — ~0.31 on the Wireless camera — so the head settled about a
    third of the way to the face and never arrived. The incremental form closes
    ``damping`` of the MEASURED error per detection instead, so it converges
    regardless of the gain constants, and it cannot overshoot for
    ``0 < damping <= 1`` because every step is a fraction of an error it just
    measured.

    Only a NEW observation moves the target. A bbox is republished on every tick
    between detections (the producer holds it for its TTL), and re-applying an
    increment from ONE reading 50 times would walk the head off the face. A
    reading is new when its CAPTURE time — ``t_local - sense.face_age_s`` —
    advances: a held reading's age grows exactly as fast as the clock, so its
    capture time is constant. See :meth:`_is_new_observation`.

    The clamp is its OWN, applied to both the target and the command, so no
    ``params`` value and no out-of-frame bbox can drive the head past it.

    Absence is HELD, not released: a tick with no (or a stale) bbox keeps the
    last target. Vision drops frames, and a lock that ended on the first missed
    detection would be unusable. Only ``release_face`` (or, later, a face-lost
    event) ends the lock.

    Never raises. A hostile snapshot, a malformed ``params`` dict or a rewinding
    clock degrades to "hold what we had" for the tick.
    """

    def __init__(self) -> None:
        self._yaw = 0.0
        self._pitch = 0.0
        #: The body yaw this gaze HOLDS for the life of the lock (issue #183).
        #: Set once, at lock time, from the pose the engine last streamed; 0.0
        #: (the neutral body yaw) when nobody handed one over — a gaze built
        #: directly in a test, or a lock taken before any tick composed a pose.
        self._body_yaw = 0.0
        self._target_yaw = 0.0
        self._target_pitch = 0.0
        self._last_t: float | None = None
        #: (t_local, commanded yaw, commanded pitch) for the recent past.
        self._ring: deque[tuple[float, float, float]] = deque(maxlen=_RING_MAXLEN)
        #: Capture time of the last observation actually APPLIED, and its bbox.
        self._last_capture: float | None = None
        self._last_bbox: tuple[float, float, float, float] | None = None
        #: Normalised offset magnitude of the last accepted observation, and the
        #: local time a fresh face was last SEEN — the anti-windup guard and the
        #: recover-to-neutral timer, respectively.
        self._last_offset: float | None = None
        self._last_seen_t: float | None = None

    # -- introspection (tests, and any future status view) ------------------ #

    @property
    def yaw(self) -> float:
        """The head yaw offset currently being commanded, in degrees."""
        return self._yaw

    @property
    def pitch(self) -> float:
        """The head pitch offset currently being commanded, in degrees."""
        return self._pitch

    @property
    def body_yaw(self) -> float:
        """The body yaw being held for the life of the lock, in degrees."""
        return self._body_yaw

    def hold_body_yaw(self, value: object) -> float:
        """Hold *value* on the ``body_yaw`` channel for the life of this gaze.

        Called ONCE by :meth:`FaceLockDriver.lock` with the body yaw the engine
        streamed on the tick before the lock took the channel, so the lock
        freezes the body where it already was instead of snapping it to
        neutral. Never raises: a missing, non-numeric or non-finite reading
        degrades to 0.0, the neutral body yaw — the same honest default
        :meth:`reachy.behavior.pose_feed.LastPoseHolder.as_start_pose_provider`
        falls back to when no pose has been stashed yet.
        """
        self._body_yaw = _finite(value, 0.0)
        return self._body_yaw

    @property
    def target_yaw(self) -> float:
        """The head yaw offset currently being eased toward, in degrees."""
        return self._target_yaw

    @property
    def target_pitch(self) -> float:
        """The head pitch offset currently being eased toward, in degrees."""
        return self._target_pitch

    # -- the contribution function ------------------------------------------ #

    def __call__(self, t_local: float, params: dict, sense) -> Contribution:
        now = _finite(t_local, 0.0)
        # First, because a rewinding clock invalidates the ring this tick reads.
        dt = self._dt(now)
        max_yaw = abs(_finite((params or {}).get("max_yaw"), MAX_YAW_DEG))
        max_pitch = abs(_finite((params or {}).get("max_pitch"), MAX_PITCH_DEG))
        bbox = _normalised_bbox(getattr(sense, "face_bbox", None))
        centre = _bbox_centre(bbox)
        age = getattr(sense, "face_age_s", None)
        if centre is not None and not _is_stale(age, params):
            self._last_seen_t = now  # a fresh face resets the recover timer
            capture = self._capture_time(now, age)
            if self._is_new_observation(capture, bbox):
                fov_h = abs(_finite((params or {}).get("fov_h"), HFOV_DEG))
                fov_v = abs(_finite((params or {}).get("fov_v"), VFOV_DEG))
                # Clamped into 0..1 here as well as declared on the Param: the
                # library validator is the real gate, but this leaf takes its
                # params from a dict and a NEGATIVE damping would steer AWAY
                # from the face — the same belt-and-braces `abs()` the clamps
                # and the slew already apply.
                damping = min(1.0, max(0.0, _finite((params or {}).get("damping"), DAMPING)))
                base_yaw, base_pitch = self._base_at(capture)
                cx, cy = centre
                new_yaw = _clamp(base_yaw - (cx - 0.5) * fov_h * damping, max_yaw)
                new_pitch = _clamp(base_pitch - (cy - 0.5) * fov_v * damping, max_pitch)
                admitted = self._admit_target(new_yaw, new_pitch, cx, cy)
                if admitted:
                    self._target_yaw = new_yaw
                    self._target_pitch = new_pitch
                logger.info(
                    "[SENSE stage=intent source=lock_face event=aim] "
                    "cx=%.3f cy=%.3f admitted=%s -> yaw=%.1f pitch=%.1f",
                    cx,
                    cy,
                    admitted,
                    self._target_yaw,
                    self._target_pitch,
                )
                self._last_capture = capture
                self._last_bbox = bbox
        else:
            # Absence breaks the convergence chain: the next real detection
            # starts fresh, so a face that returns is never judged against the
            # offset from before it vanished (which would reject the first
            # re-aim as "not converging").
            self._last_offset = None
            if self._recovering(now):
                # No fresh face for RECOVER_AFTER_S: ease the gaze back toward
                # neutral so a lock that lost its face (or a guard-frozen
                # runaway that then lost it) stops pointing at a wall. Still
                # LOCKED — a returning face re-aims at once; only the head
                # relaxes.
                ease = RECOVER_RATE_DEG_S * dt
                self._target_yaw = _approach(self._target_yaw, 0.0, ease)
                self._target_pitch = _approach(self._target_pitch, 0.0, ease)
            # else: a brief absence is HELD — absence is not a release.

        # Re-clamp the held target too: `params` may have tightened since.
        self._target_yaw = _clamp(self._target_yaw, max_yaw)
        self._target_pitch = _clamp(self._target_pitch, max_pitch)

        step = abs(_finite((params or {}).get("slew"), SLEW_DEG_S)) * dt
        self._yaw = _clamp(_approach(self._yaw, self._target_yaw, step), max_yaw)
        self._pitch = _clamp(_approach(self._pitch, self._target_pitch, step), max_pitch)
        self._ring.append((now, self._yaw, self._pitch))
        # `body_yaw` is HELD, never planned: the lock claims the channel only to
        # keep `feel-alive`'s slow wander from rotating the camera off the face
        # (#183). Contributing it every tick (rather than abstaining) is what
        # makes the claim effective — an abstaining claimant falls through to
        # the base layer in `arbitrate()`.
        return Contribution(head=_head(yaw=self._yaw, pitch=self._pitch), body_yaw=self._body_yaw)

    # -- the capture-time machinery ----------------------------------------- #

    def _capture_time(self, now: float, age: object) -> float | None:
        """When the frame behind this bbox was taken, on the LOCAL clock.

        ``None`` when the snapshot carries no ``face_age_s`` at all — an older
        or partial provider. The caller then falls back to "the bbox changed",
        which is weaker but never wrong in the dangerous direction.
        """
        if age is None:
            return None
        return now - max(0.0, _finite(age, 0.0))

    def _is_new_observation(
        self, capture: float | None, bbox: tuple[float, float, float, float] | None
    ) -> bool:
        """Whether this reading is a DETECTION we have not already acted on.

        The rule, and why it is this one:

        * With a capture time, a reading is new when that time ADVANCED. A bbox
          republished between detections carries a growing ``face_age_s``, so
          its capture time stands still — the single robust signal, and it works
          even for a motionless face whose two consecutive detections are
          bit-identical. The advance must clear :data:`_NEW_OBSERVATION_EPS_S`
          UNLESS the bbox itself changed, which absorbs the microsecond skew
          between the provider's clock and ours without making a genuinely
          fast detector (a new bbox every tick) wait for the epsilon.
        * With no capture time, a changed bbox is the only evidence available.
        """
        if capture is None:
            return bbox != self._last_bbox
        if self._last_capture is None:
            return True
        advance = capture - self._last_capture
        if advance <= 0.0:
            return False
        return advance > _NEW_OBSERVATION_EPS_S or bbox != self._last_bbox

    def _admit_target(self, new_yaw: float, new_pitch: float, cx: float, cy: float) -> bool:
        """Whether to accept this observation's target — the anti-windup guard.

        The FIRST observation of a lock always wins (there is nothing yet to
        converge from). After that, an OUTWARD push — one that would drive the
        target further from neutral on the axis that dominates the offset — is
        accepted only while the measured offset is still SHRINKING by at least
        :data:`CONVERGE_EPS`. A push that moves the target TOWARD neutral is
        always fine (the face crossed centre, or moved closer in). This is what
        stops a fixed, unreachable offset from marching the head to its clamp
        and pinning there (live, 2026-09-06). ``_last_offset`` is updated every
        accepted OR rejected call, so convergence is always measured against the
        most recent reading.
        """
        offset = math.hypot(cx - 0.5, cy - 0.5)
        last = self._last_offset
        self._last_offset = offset
        if last is None or self._last_capture is None:
            return True  # first real observation
        toward_neutral = (
            abs(new_yaw) < abs(self._target_yaw) - _RECOVER_ANGLE_EPS
            or abs(new_pitch) < abs(self._target_pitch) - _RECOVER_ANGLE_EPS
        )
        if toward_neutral:
            return True
        return offset < last - CONVERGE_EPS

    def _recovering(self, now: float) -> bool:
        """Whether enough true absence has passed to ease back toward neutral."""
        seen = self._last_seen_t
        return seen is not None and (now - seen) > RECOVER_AFTER_S

    def _base_at(self, capture: float | None) -> tuple[float, float]:
        """The angle this gaze was COMMANDING at *capture*, from the ring.

        The most recent sample at or before the capture time — the command
        actually in force when the frame was taken. With nothing that old
        (a ring shorter than the detection latency, or a fresh lock) the OLDEST
        sample is used; with an empty ring, the current command. Using the
        CURRENT command instead would double-count every degree slewed since
        the frame was taken, which turns a converging loop into an overshooting
        one exactly while the head is moving.
        """
        if capture is None or not self._ring:
            return (self._yaw, self._pitch)
        for sample_t, yaw, pitch in reversed(self._ring):
            if sample_t <= capture:
                return (yaw, pitch)
        _oldest_t, yaw, pitch = self._ring[0]
        return (yaw, pitch)

    def _dt(self, t_local: float) -> float:
        """Seconds since the previous call, bounded and never negative.

        A REWINDING clock (a restarted timeline, a test driving the same gaze
        twice from t=0) invalidates the ring and the last capture time: every
        remembered timestamp now sits in the future. Both are dropped, so the
        next reading is taken as new and measured against the current command.
        """
        now = _finite(t_local, 0.0)
        previous = self._last_t
        self._last_t = now
        if previous is None:
            return 0.0
        if now < previous:
            self._ring.clear()
            self._last_capture = None
            self._last_bbox = None
            return 0.0
        return max(0.0, min(_MAX_DT_S, now - previous))


def _head(**offsets: float) -> dict[str, float]:
    head = neutral_head()
    head.update(offsets)
    return head


def _approach(value: float, target: float, step: float) -> float:
    """Move *value* toward *target* by at most *step* (a slew, never an overshoot)."""
    delta = target - value
    if abs(delta) <= step:
        return target
    return value + math.copysign(step, delta)


def _normalised_bbox(bbox: object) -> tuple[float, float, float, float] | None:
    """A hostile ``face_bbox`` as a finite 4-tuple of floats, or ``None``.

    A tuple (never the caller's list) so it can be compared and remembered as
    the "same detection" key without aliasing the snapshot.
    """
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        return None
    return (x, y, w, h)


def _bbox_centre(bbox: object) -> tuple[float, float] | None:
    """The ``(cx, cy)`` centre of a normalised ``(x, y, w, h)``, or ``None``."""
    box = _normalised_bbox(bbox)
    if box is None:
        return None
    x, y, w, h = box
    return (x + w / 2.0, y + h / 2.0)


def _is_stale(age: object, params: dict | None) -> bool:
    """Whether a ``face_age_s`` reading is too old to steer the gaze."""
    if age is None:
        return False  # no age reading: trust the producer's own TTL
    max_age = abs(_finite((params or {}).get("max_age"), MAX_FACE_AGE_S))
    return _finite(age, 0.0) > max_age


def make_face_lock() -> Callable[[float, dict, object], Contribution]:
    """Return one fresh, stateful ``face-lock`` contribution function.

    The zero-argument factory shape :class:`reachy.behavior.library.LibraryEntry`
    calls per behavior instance (``make_fn``), so every lock gets its own slew
    state. Construct :class:`FaceLockGaze` directly in a test.
    """
    return FaceLockGaze()


def _lock_channels() -> list[str]:
    """The ``face-lock`` library entry's claimed channels, sorted; ``["head"]`` if unknown.

    Imported lazily (the library imports this module for the entry's
    ``make_fn``), and fail-open to the head alone so a feed line is never lost
    to an import-order accident.
    """
    try:
        from reachy.behavior import library as lib

        return sorted(lib.get(FACE_LOCK_BEHAVIOR).channels)
    except Exception:  # pragma: no cover - defensive: the feed line still lands
        return ["head"]


# --------------------------------------------------------------------------- #
# The driver — the lock STATE and the two kind handlers                       #
# --------------------------------------------------------------------------- #


class FaceLockDriver:
    """Where the lock state lives, the two kind handlers, and the lock's lifecycle.

    A ``TickBus``-shaped driver (``driver(ctx)`` -> :meth:`on_tick`, exactly as
    :class:`reachy.behavior.intents.IntentDriver` is shaped): the admitted
    ``face-lock`` behavior IS the gaze loop, but the LOCK itself needs a tick to
    notice the three things that end or report on it. It holds the three facts a
    release needs — whether we are locked, which behavior id to evict, and which
    inhibition names WE added — and registers ``lock_face`` / ``release_face``
    into whatever :class:`~reachy.behavior.control.KindRegistry` composition
    hands it (:meth:`register_into`), exactly as ``goto`` is registered.

    The lifecycle, all three of it (see :meth:`on_tick`)
    -----------------------------------------------------
    * **Face lost is REPORTED, never fatal.** ``face_lost_after_s`` of an absent
      or stale bbox emits ONE :data:`EVENT_FACE_LOST` and re-arms when the face
      returns. The lock persists: a person who steps out of frame has not asked
      to be let go, and vision drops frames.
    * **A lock cannot outlive its mind.** ``mind_online()`` reading ``False``
      for ``mind_offline_grace_s`` releases with :data:`REASON_MIND_OFFLINE`.
      The lock is a standing claim taken on the mind's behalf; with the mind
      gone there is nobody left to call ``release_face``. ``None`` is UNKNOWN
      and NEVER releases — see the seam note below.
    * **A lock cannot be held forever.** ``max_hold_s`` releases with
      :data:`REASON_MAX_HOLD`.

    Every one of those releases is the SAME release ``release_face`` performs
    (evict, restore inhibitions under the later-wins rule, emit one
    :data:`EVENT_LOCK_RELEASED`) — now carrying ``detail.reason``.

    The ``mind_online`` seam
    -------------------------
    ``mind_online`` is an injected ``() -> bool | None`` callable, NOT an import
    and not a broker subscription: this runtime publishes its nervous system
    (``reachy/events/*``, retained ``reachy/state/*``) and subscribes to nothing,
    so there is no live "is the harness up" reading here to consume yet. The
    default is therefore ``None`` — unknown — and unknown never releases. A
    probe that raises is unknown too. When a mind-liveness source does land
    (a retained ``reachy/state/nova/online`` subscriber being the obvious one),
    wiring it is one lambda at the composition site; nothing in this module
    changes.

    The inhibited set is reached through two injected callables
    (``inhibitions_getter`` / ``inhibitions_setter``, satisfied by
    :attr:`reachy.behavior.intents.IntentDriver.inhibitions` and
    :meth:`~reachy.behavior.intents.IntentDriver.set_inhibitions`) so this module
    never imports ``intents``, and a test can inject a bare pair.
    """

    def __init__(
        self,
        *,
        inhibitions_getter: Callable[[], Iterable[str]] | None = None,
        inhibitions_setter: Callable[[Iterable[str]], object] | None = None,
        mind_online: Callable[[], bool | None] | None = None,
        face_lost_after_s: float = FACE_LOST_AFTER_S,
        mind_offline_grace_s: float = MIND_OFFLINE_GRACE_S,
        max_hold_s: float = MAX_HOLD_S,
        lib=None,
    ) -> None:
        if lib is None:
            # Local by necessity, not by style: `library` imports `make_face_lock`
            # from this module at module scope, so importing it back at module
            # scope would be a cycle (see the module docstring).
            from reachy.behavior import library as lib
        self._lib = lib
        self._get_inhibitions = inhibitions_getter
        self._set_inhibitions = inhibitions_setter
        self._mind_online = mind_online
        self._face_lost_after_s = abs(_finite(face_lost_after_s, FACE_LOST_AFTER_S))
        self._mind_offline_grace_s = abs(_finite(mind_offline_grace_s, MIND_OFFLINE_GRACE_S))
        self._max_hold_s = abs(_finite(max_hold_s, MAX_HOLD_S))
        self._locked = False
        self._behavior_id: str | None = None
        self._added: frozenset[str] = frozenset()
        self._seq = 0
        self._locked_at: float | None = None
        self._face_seen_at: float | None = None
        self._face_lost_reported = False
        self._mind_offline_since: float | None = None

    # -- read-only introspection ------------------------------------------- #

    @property
    def face_lost_after_s(self) -> float:
        """Seconds of an absent/stale face before the ONE ``face-lost`` report."""
        return self._face_lost_after_s

    @property
    def mind_offline_grace_s(self) -> float:
        """Seconds of a continuously offline mind before a ``mind-offline`` release."""
        return self._mind_offline_grace_s

    @property
    def max_hold_s(self) -> float:
        """The longest one lock may be held before a ``max-hold`` release."""
        return self._max_hold_s

    @property
    def locked(self) -> bool:
        """Whether a face lock is currently held."""
        return self._locked

    @property
    def behavior_id(self) -> str | None:
        """The admitted ``face-lock`` behavior's id while locked, else ``None``."""
        return self._behavior_id

    @property
    def added_inhibitions(self) -> frozenset[str]:
        """The inhibition names THIS lock added and will remove on release."""
        return self._added

    # -- registration (composition's one wiring step) ----------------------- #

    def register_into(self, registry):
        """Register both kinds into *registry* (duck-typed ``KindRegistry``)."""
        registry.register(LOCK_FACE, self.lock)
        registry.register(RELEASE_FACE, self.release)
        return registry

    # -- the later-wins seam ------------------------------------------------- #

    def notice_inhibition_replaced(self, names: Iterable[str] | None = None) -> None:
        """A ``set_inhibition`` replaced the whole set: RECOMPUTE what this lock owns.

        Wired to :attr:`reachy.behavior.intents.IntentDriver.inhibition_observer`
        at composition. Ownership is recomputed on EVERY replacement, never
        frozen at acquisition: the live set is re-asserted as
        ``new_set | LOCK_INHIBITS`` (so a replacement can never leave a
        head-owning behavior running under a held lock, even for a name that was
        already operator-inhibited when the lock was taken and therefore was
        never "added"), and the ownership this release will hand back becomes:

        * every :data:`LOCK_INHIBITS` name the caller did NOT keep — the lock
          re-claims those, and removes them again on release;
        * plus, when the replacement carries EVERY name the lock currently holds,
          those same names — a set that echoes back all of ours is a mind
          re-writing what it read (``stay_silent`` merging ``speak`` into
          ``state.json``'s list), not a statement about our claim, and adopting
          it as operator-held would leave the presence loop inhibited after
          release: an inert robot (observed live, 2026-08-26).

        Keeping only SOME of the lock's names cannot be that echo, so those are
        the caller's own and survive release.
        """
        if not self._locked:
            self._added = frozenset()
            return
        new_set = frozenset(names or ())
        owned = frozenset(LOCK_INHIBITS) - new_set
        if self._added and self._added <= new_set:
            owned = owned | self._added
        self._added = owned
        if self._set_inhibitions is not None and not frozenset(LOCK_INHIBITS) <= new_set:
            self._set_inhibitions(new_set | frozenset(LOCK_INHIBITS))

    # -- the tick seam ------------------------------------------------------- #

    def __call__(self, ctx) -> None:
        """Usable directly as one entry of the engine's ``TickBus`` driver list."""
        self.on_tick(ctx)

    def on_tick(self, ctx, now: float | None = None) -> None:
        """Report a lost face; end a lock that outlived its mind or its clock.

        Cheap and total: a no-op while unlocked (beyond clearing the transient
        timers), and it never raises — a hostile ``ctx``, a rewinding clock or a
        ``mind_online`` probe that explodes each degrade to "do nothing this
        tick", because a lifecycle watchdog that can break the tick is worse
        than the lock it was meant to bound.

        *now* defaults to ``ctx.now``, so the driver is equally usable as a bare
        ``TickBus`` rider and as an explicitly-clocked object in a test.
        """
        moment = _finite(now if now is not None else getattr(ctx, "now", 0.0), 0.0)
        if not self._locked:
            self._mind_offline_since = None
            return
        if self._was_evicted(ctx):
            self._release(ctx, REASON_EVICTED)
            return
        self._watch_face(ctx, moment)
        if self._mind_is_gone(moment):
            self._release(ctx, REASON_MIND_OFFLINE)
            return
        if self._held_too_long(moment):
            self._release(ctx, REASON_MAX_HOLD)

    def _watch_face(self, ctx, now: float) -> None:
        """Emit ONE ``face-lost`` per disappearance; re-arm when the face returns."""
        if self._face_is_lockable(ctx):
            self._face_seen_at = now
            self._face_lost_reported = False
            return
        if self._face_seen_at is None:
            self._face_seen_at = now
            return
        absent_s = max(0.0, now - self._face_seen_at)
        if absent_s >= self._face_lost_after_s and not self._face_lost_reported:
            self._face_lost_reported = True
            self._emit(
                ctx,
                EVENT_FACE_LOST,
                {"id": self._behavior_id, "absent_s": absent_s},
            )

    def _was_evicted(self, ctx) -> bool:
        """Whether the admitted behavior has left the active set behind our back.

        UNKNOWN is never "gone": a ``ctx`` with no ``active_names`` (an older
        seam, a partial test double) and a probe that raises both read as "still
        there", because a watchdog that guesses would drop a live lock off a
        face on any seam it does not recognise. The name — not the id — is what
        is checked: this driver is the ONLY admitter of ``face-lock`` (a
        ``declare_goal`` naming it is refused in
        :mod:`reachy.behavior.intents`), so the name is unambiguous here and
        survives an engine that re-ids on re-admission.
        """
        if self._behavior_id is None:
            return False
        active_names = getattr(ctx, "active_names", None)
        if not callable(active_names):
            return False
        try:
            names = active_names()
        except Exception:  # a hostile ctx is UNKNOWN, never "evicted"
            return False
        try:
            return FACE_LOCK_BEHAVIOR not in names
        except TypeError:  # not a container -> UNKNOWN
            return False

    def _mind_is_gone(self, now: float) -> bool:
        """Whether the mind has read offline for the WHOLE grace period."""
        if self._mind_online is None:
            return False
        try:
            reading = self._mind_online()
        except Exception:  # an unreachable probe is UNKNOWN, never "offline"
            reading = None
        if reading is None or reading:
            self._mind_offline_since = None
            return False
        if self._mind_offline_since is None:
            self._mind_offline_since = now
            return False
        return max(0.0, now - self._mind_offline_since) >= self._mind_offline_grace_s

    def _held_too_long(self, now: float) -> bool:
        if self._locked_at is None:
            return False
        return max(0.0, now - self._locked_at) >= self._max_hold_s

    # -- kind handlers ------------------------------------------------------- #

    def lock(self, payload: dict, ctx) -> dict:
        """Handle ``lock_face``: admit the behavior and take the inhibitions."""
        _reject_unknown_fields(payload)
        if self._locked:
            return {"ok": True, "op": LOCK_FACE, "locked": True, "note": "already locked"}
        entry = self._lib.get(FACE_LOCK_BEHAVIOR)
        params = self._lib.resolve_params(entry, payload.get("params"))
        if not self._face_is_lockable(ctx):
            return {"ok": False, "op": LOCK_FACE, "error": "no face known"}

        self._seq += 1
        behavior_id = f"{_ID_PREFIX}:lock:{self._seq}"
        beh = Behavior(
            id=behavior_id,
            name=FACE_LOCK_BEHAVIOR,
            channels=entry.channels,
            stop_class=entry.default_class,
            # Indefinite on purpose, and exempt from the bounded-lifetime
            # invariant for the same reason `declare_goal` is: the standing
            # record that undoes it is the lock itself, released by name.
            lifetime=Lifetime(looping=True, duration=None),
            params=dict(params),
            fn=self._build_gaze(entry, ctx),
            wants_sense=entry.wants_sense,
        )
        result = ctx.admit(beh)
        self._locked = True
        self._behavior_id = behavior_id
        # Every lifecycle timer starts HERE, at this lock's own moment — never at
        # process start, and never carrying anything over from a previous lock.
        now = _finite(getattr(ctx, "now", 0.0), 0.0)
        self._locked_at = now
        self._face_seen_at = now
        self._face_lost_reported = False
        self._mind_offline_since = None
        self._take_inhibitions()
        return {
            "ok": True,
            "op": LOCK_FACE,
            "locked": True,
            "id": behavior_id,
            "inhibited": sorted(self._added),
            "admitted": result,
        }

    def release(self, payload: dict, ctx) -> dict:
        """Handle ``release_face``: evict, restore inhibitions, emit ONE event."""
        _reject_unknown_fields(payload, kind=RELEASE_FACE)
        if not self._locked:
            return {"ok": True, "op": RELEASE_FACE, "released": False, "note": "not locked"}
        behavior_id, restored = self._release(ctx, REASON_REQUESTED)
        return {
            "ok": True,
            "op": RELEASE_FACE,
            "released": True,
            "id": behavior_id,
            "reason": REASON_REQUESTED,
            "inhibitions": restored,
        }

    # -- internals ----------------------------------------------------------- #

    def _release(self, ctx, reason: str) -> tuple[str | None, list[str]]:
        """THE release, whatever asked for it: evict, restore, emit ONE event.

        One path for every reason, so a lifecycle release can never differ from
        an explicit one in what it undoes.
        """
        behavior_id = self._behavior_id
        self._locked = False
        self._behavior_id = None
        self._locked_at = None
        self._face_seen_at = None
        self._face_lost_reported = False
        self._mind_offline_since = None
        if behavior_id is not None:
            ctx.evict(behavior_id)
        restored = self._restore_inhibitions()
        self._emit(ctx, EVENT_LOCK_RELEASED, {"id": behavior_id, "reason": reason})
        return behavior_id, restored

    def _build_gaze(self, entry, ctx):
        """One fresh gaze, holding the body yaw the engine streamed most recently.

        ``ctx.pose`` is the complete pose the engine composed and sent THIS tick
        — populated after streaming, before the seam runs (see
        :class:`reachy.behavior.engine.TickContext`) — so at lock time it is the
        body yaw commanded on the tick BEFORE the lock takes the channel. That
        is the same live-pose seam :mod:`reachy.behavior.pose_feed` adapts for
        ``GotoLane``'s start pose; reading it straight off ``ctx`` needs no
        composition change and no second stash.

        A ctx with no ``pose`` (the duck-typed ctx a test or a non-engine caller
        passes, or a lock taken before any tick composed one) holds 0.0 — the
        neutral body yaw, and the same fallback ``as_start_pose_provider``
        makes. Never raises.
        """
        fn = entry.build_fn()
        hold = getattr(fn, "hold_body_yaw", None)
        if not callable(hold):  # a foreign/monkeypatched factory — nothing to hold
            return fn
        pose = getattr(ctx, "pose", None)
        body_yaw = pose.get("body_yaw") if isinstance(pose, dict) else None
        hold(body_yaw)
        return fn

    def _face_is_lockable(self, ctx) -> bool:
        sense = getattr(ctx, "sense", None)
        if _bbox_centre(getattr(sense, "face_bbox", None)) is None:
            return False
        return not _is_stale(getattr(sense, "face_age_s", None), None)

    def _take_inhibitions(self) -> None:
        """Snapshot the inhibited set and add ours — remembering only what we added."""
        if self._get_inhibitions is None or self._set_inhibitions is None:
            self._added = frozenset()
            return
        snapshot = frozenset(self._get_inhibitions() or ())
        self._added = frozenset(LOCK_INHIBITS) - snapshot
        if self._added:
            self._set_inhibitions(snapshot | self._added)

    def _restore_inhibitions(self) -> list[str]:
        """Remove exactly what we added (nothing, after a later ``set_inhibition``)."""
        if self._get_inhibitions is None or self._set_inhibitions is None:
            return []
        current = frozenset(self._get_inhibitions() or ())
        if self._added:
            current = current - self._added
            self._set_inhibitions(current)
        self._added = frozenset()
        return sorted(current)

    def _emit(self, ctx, event_type: str, detail: dict) -> None:
        """Publish one ``motion.*`` event through ``ctx.emit``, if there is one.

        Both lifecycle events share this shape, and both actions are registered
        in :data:`reachy.export.runtime._RAW_MOTION_ACTIONS` — which is what puts
        them on the stdout feed and on ``reachy/events/motion/<action>``.
        """
        emit = getattr(ctx, "emit", None)
        if emit is None:
            return
        emit(
            {
                "type": event_type,
                "ts": getattr(ctx, "now", 0.0),
                "tick": getattr(ctx, "tick", 0),
                "behavior": FACE_LOCK_BEHAVIOR,
                # The channels the lock CLAIMS (#183: head + body_yaw, so the
                # base layer keeps the antennas) — read from the library entry,
                # never restated, so the feed cannot under-report the claim.
                "channels": _lock_channels(),
                "detail": dict(detail),
            }
        )


def _reject_unknown_fields(payload: dict, *, kind: str = LOCK_FACE) -> None:
    unknown = sorted(set(payload or {}) - _ALLOWED_FIELDS)
    if unknown:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{kind}: unknown field(s) {', '.join(repr(k) for k in unknown)}",
            remediation=f"allowed: {', '.join(sorted(_ALLOWED_FIELDS - {'cmd_id', 'op'}))}",
        )


__all__ = [
    "DAMPING",
    "EVENT_FACE_LOST",
    "EVENT_LOCK_RELEASED",
    "FACE_LOCK_BEHAVIOR",
    "FACE_LOST_ACTION",
    "FACE_LOST_AFTER_S",
    "HFOV_DEG",
    "LOCK_FACE",
    "LOCK_INHIBITS",
    "LOCK_RELEASED_ACTION",
    "MAX_FACE_AGE_S",
    "MAX_HOLD_S",
    "MAX_PITCH_DEG",
    "MAX_YAW_DEG",
    "MIND_OFFLINE_GRACE_S",
    "PITCH_GAIN_DEG",
    "REASON_EVICTED",
    "REASON_MAX_HOLD",
    "REASON_MIND_OFFLINE",
    "REASON_REQUESTED",
    "RELEASE_FACE",
    "SLEW_DEG_S",
    "VFOV_DEG",
    "YAW_GAIN_DEG",
    "FaceLockDriver",
    "FaceLockGaze",
    "make_face_lock",
]
