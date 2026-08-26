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
   (:data:`LOCK_INHIBITS` — ``feel-alive`` and ``orient-to-sound``, the two
   behaviors that would otherwise keep dragging the head off the face).

A lock cannot outlive its mind, nor be held forever
----------------------------------------------------
A lock is an INDEFINITE claim on the head taken on a mind's behalf, so the two
ways it could become a wedged robot are closed in :meth:`FaceLockDriver.on_tick`:
``mind_online()`` reading ``False`` for ``mind_offline_grace_s`` releases it
(``reason: "mind-offline"`` — nobody is left to call ``release_face``), and
``max_hold_s`` releases it regardless (``reason: "max-hold"``). Losing the FACE
is deliberately NOT one of them: that is reported and the lock persists. Every
release — including the explicit one, ``reason: "requested"`` — runs the one
:meth:`FaceLockDriver._release` path, so no ending can undo less than another.
``mind_online`` defaults to ``None`` (unknown), which never releases.

Inhibition is LATER-WINS
-------------------------
``set_inhibition`` REPLACES the whole inhibited set (see
:mod:`reachy.behavior.intents`), so a caller that replaces it WHILE locked has
made a deliberate, later statement about what is inhibited. The lock therefore
forgets its own additions at that moment (:meth:`FaceLockDriver.notice_inhibition_replaced`)
and ``release_face`` leaves the newer set exactly as it stands. With no
intervening call, release removes precisely the names the lock ADDED — never a
name the snapshot already carried — restoring the snapshot.

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

import math
from typing import Callable, Iterable

from reachy.behavior.model import Behavior, Contribution, Lifetime, neutral_head
from reachy.cli._errors import EXIT_USER_ERROR, CliError

#: The two command kinds (the ``op`` field) this module answers to — following
#: :mod:`reachy.behavior.intents`'s bare-verb naming (``run_behavior``, ...).
LOCK_FACE = "lock_face"
RELEASE_FACE = "release_face"

#: The library entry the lock admits.
FACE_LOCK_BEHAVIOR = "face-lock"

#: What the lock adds to the inhibited set: the two behaviors that would
#: otherwise keep commanding the head (and so keep pulling it off the face).
LOCK_INHIBITS = ("feel-alive", "orient-to-sound")

#: A ``face_bbox`` older than this is not a face to lock onto. Matches the
#: producer's own TTL (``reachy.behavior.face_sense``), so the two agree.
MAX_FACE_AGE_S = 1.5

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

#: The three named causes a :data:`EVENT_LOCK_RELEASED` carries in ``detail.reason``.
REASON_REQUESTED = "requested"
REASON_MIND_OFFLINE = "mind-offline"
REASON_MAX_HOLD = "max-hold"

#: How long the face must be absent/stale before the ONE ``face-lost`` report.
#: Well above vision's own TTL (:data:`MAX_FACE_AGE_S`): a dropped frame is not
#: a lost face, and this event is meant to be rare enough to mean something.
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
#: Equal to the clamps by default, so the mapping is linear across the frame and
#: only saturates at the edge; raise it for a twitchier lock, and the clamp still
#: binds.
YAW_GAIN_DEG = 20.0
PITCH_GAIN_DEG = 12.0

#: How fast the commanded angle chases its target, in deg/s — specified in
#: TIME, never per tick, so the lock behaves identically at any tick rate
#: (the cadence-invariance lesson of issue #168).
SLEW_DEG_S = 120.0

#: Longest gap between two calls the slew integrates over. A resumed/stalled
#: process must not teleport the head.
_MAX_DT_S = 0.25


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

    Every tick it maps ``sense.face_bbox``'s centre — normalised ``0..1``,
    origin top-left — onto a head yaw/pitch TARGET and eases the commanded angle
    toward it at :data:`SLEW_DEG_S`. The sign convention is the repo's:
    ``+yaw`` is LEFT (:func:`reachy.behavior.sense.doa_angle_to_yaw`), so a face
    at large ``x`` (the robot's right) yields a negative yaw; ``+pitch`` is up
    (``thoughtful``'s "upward/forward tilt"), so a face at small ``y`` yields a
    positive pitch.

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
        self._target_yaw = 0.0
        self._target_pitch = 0.0
        self._last_t: float | None = None

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
    def target_yaw(self) -> float:
        """The head yaw offset currently being eased toward, in degrees."""
        return self._target_yaw

    @property
    def target_pitch(self) -> float:
        """The head pitch offset currently being eased toward, in degrees."""
        return self._target_pitch

    # -- the contribution function ------------------------------------------ #

    def __call__(self, t_local: float, params: dict, sense) -> Contribution:
        max_yaw = abs(_finite((params or {}).get("max_yaw"), MAX_YAW_DEG))
        max_pitch = abs(_finite((params or {}).get("max_pitch"), MAX_PITCH_DEG))
        centre = _bbox_centre(getattr(sense, "face_bbox", None))
        age = getattr(sense, "face_age_s", None)
        if centre is not None and not _is_stale(age, params):
            gain_yaw = _finite((params or {}).get("yaw_gain"), YAW_GAIN_DEG)
            gain_pitch = _finite((params or {}).get("pitch_gain"), PITCH_GAIN_DEG)
            cx, cy = centre
            self._target_yaw = _clamp(-(cx - 0.5) * 2.0 * gain_yaw, max_yaw)
            self._target_pitch = _clamp(-(cy - 0.5) * 2.0 * gain_pitch, max_pitch)
        # else: hold the last target — absence is not a release.

        # Re-clamp the held target too: `params` may have tightened since.
        self._target_yaw = _clamp(self._target_yaw, max_yaw)
        self._target_pitch = _clamp(self._target_pitch, max_pitch)

        step = abs(_finite((params or {}).get("slew"), SLEW_DEG_S)) * self._dt(t_local)
        self._yaw = _clamp(_approach(self._yaw, self._target_yaw, step), max_yaw)
        self._pitch = _clamp(_approach(self._pitch, self._target_pitch, step), max_pitch)
        return Contribution(head=_head(yaw=self._yaw, pitch=self._pitch))

    def _dt(self, t_local: float) -> float:
        """Seconds since the previous call, bounded and never negative."""
        now = _finite(t_local, 0.0)
        previous = self._last_t
        self._last_t = now
        if previous is None:
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


def _bbox_centre(bbox: object) -> tuple[float, float] | None:
    """The ``(cx, cy)`` centre of a normalised ``(x, y, w, h)``, or ``None``."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        return None
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

    def notice_inhibition_replaced(self, _names: Iterable[str] | None = None) -> None:
        """A ``set_inhibition`` replaced the whole set: drop our own additions.

        Wired to :attr:`reachy.behavior.intents.IntentDriver.inhibition_observer`
        at composition. The lock stays held — only its CLAIM on the inhibited set
        is surrendered, so the later call wins and ``release_face`` restores
        nothing behind the caller's back.
        """
        self._added = frozenset()

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
            fn=entry.build_fn(),
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

        One path for all four reasons, so a lifecycle release can never differ
        from an explicit one in what it undoes.
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
                "channels": ["head"],
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
    "EVENT_FACE_LOST",
    "EVENT_LOCK_RELEASED",
    "FACE_LOCK_BEHAVIOR",
    "FACE_LOST_ACTION",
    "FACE_LOST_AFTER_S",
    "LOCK_FACE",
    "LOCK_INHIBITS",
    "LOCK_RELEASED_ACTION",
    "MAX_FACE_AGE_S",
    "MAX_HOLD_S",
    "MAX_PITCH_DEG",
    "MAX_YAW_DEG",
    "MIND_OFFLINE_GRACE_S",
    "PITCH_GAIN_DEG",
    "REASON_MAX_HOLD",
    "REASON_MIND_OFFLINE",
    "REASON_REQUESTED",
    "RELEASE_FACE",
    "SLEW_DEG_S",
    "YAW_GAIN_DEG",
    "FaceLockDriver",
    "FaceLockGaze",
    "make_face_lock",
]
