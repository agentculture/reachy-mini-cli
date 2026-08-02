"""Per-sense AVAILABILITY, published into the runtime's standing ``state.json``.

Issue #120: the deployed robot's box lacked the ``[vision]`` extra, so
:func:`reachy.behavior.face_sense.build_face_recognition` logged ONE latched
warning at startup and the ``face`` / ``frame_available`` cues were never
populated again. Six hours of journal held zero face events — and nothing in it
distinguished *"no face has been in view"* from *"this sense has been dead since
boot"*. The once-only warning is the correct LOG policy (repeating it 50x/s
would be strictly worse); the defect is that it was the ONLY copy of the fact.

So the fact is now also a VALUE, standing in ``state.json`` for as long as the
runtime lives::

    "senses": {
      "face":            {"available": false, "reason": "vision-extra-absent"},
      "frame_available": {"available": true,  "reason": null},
      ...
    }

Every entry obeys the same discipline :mod:`reachy.senselog` enforces for
drops — **a dead sense always names its reason** — mechanically, via
:class:`SenseAvailability`'s own validation: ``available=False`` without a
non-blank reason is a construction error, and so is a reason on an available
sense.

--------------------------------------------------------------------------
Placement: a seam rider, not ``Engine.state()``
--------------------------------------------------------------------------
``Engine.state()`` builds ``{updated, compose_hz, active, ownership, doa}``
from what the engine itself owns — the active set, channel arbitration, the
last :class:`~reachy.behavior.sense.Sense`. Which sense PROVIDERS a composition
happened to wire, and whether each one's extra is installed, is
composition-time knowledge the engine has no access to and deliberately no
opinion about: ``reachy/behavior/engine.py`` imports neither ``face_sense`` nor
the media client, and teaching it to would invert the dependency the whole
sense stack is built on (composition wires providers INTO the engine, never the
other way round).

The engine already left the door open for exactly this: it publishes its state
snapshot **before** running the tick seam, with a comment saying a seam rider
that augments ``state.json`` is then the tick's FINAL writer. This module walks
through that door, the same way
:class:`reachy.behavior.intents.IntentDriver` does for its ``"intents"`` view —
one more additive top-level key, merged by read-modify-write, engine untouched.

--------------------------------------------------------------------------
Write policy: check every tick, write only on change
--------------------------------------------------------------------------
The engine's periodic heartbeat write CLOBBERS the whole file (it writes the
un-augmented base shape — it has no idea this module exists), so publishing
once at boot would leave the block alive for a fraction of a second. The rider
therefore re-checks every tick. It does not re-*write* every tick: availability
is structural, so in the steady state each tick costs one small JSON read and
no write, and a write happens only when the file was clobbered or a verdict
genuinely changed. Both are O(1) and neither touches the network, the SDK or a
lock.

--------------------------------------------------------------------------
What "available" means here — and what it deliberately does not
--------------------------------------------------------------------------
This block reports **structural** availability: is this sense composed, and is
the software it needs installed? It is deliberately NOT a liveness readout. A
media client that has not warmed up yet, a hearing session inside its reconnect
backoff, or a camera that produced no frame this instant are all *transient* and
already observable elsewhere (``senselog`` drops, ``frame_available`` itself,
the engine heartbeat). Folding them in here would make the block flap and would
destroy the one thing it exists to say: *this sense will not report anything for
the whole life of this process, and here is why.*

The probe map is injected, so a future task can add a genuinely standing
dynamic verdict (a latched ``session-down``, say) without touching this module.

Stdlib only, plus in-package imports. No ``reachy_mini``, no ``cv2``, no
network — the probes are import-spec checks and composition booleans.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.util import find_spec as _find_spec
from pathlib import Path
from typing import Callable, Iterable, Mapping

from reachy import senselog
from reachy.behavior import control as control_mod
from reachy.behavior import face_sense

# The ONE declared source of truth for which optional providers the current
# composition wires (``behavior rules check`` lints against it through
# ``FED_SENSE_FIELDS``). Imported rather than restated so the availability block
# can never drift from it — a provider added there without a probe here fails
# ``SenseAvailabilityDriver``'s coverage check and its dedicated test.
from reachy.behavior.sense import _COMPOSED_PROVIDER_FIELDS

logger = logging.getLogger(__name__)

#: The senselog stage every availability transition is reported under.
STAGE = "availability"

#: The additive top-level ``state.json`` key this rider owns.
STATE_KEY = "senses"

#: Every sense name the block must cover: the declared composed-provider fields
#: plus ``pat``.
#:
#: ``pat`` and ``pat_event`` are the SAME verdict under two vocabularies and
#: both are carried on purpose — ``pat_event`` is the
#: :class:`~reachy.behavior.sense.SenseProviders` attribute name, ``pat`` is the
#: ``rules.toml`` predicate field it feeds
#: (``sense._PROVIDER_PREDICATE_FIELDS["pat_event"] == "pat"``). A consumer
#: holding either name finds an answer instead of a missing key.
AVAILABILITY_SENSES: frozenset[str] = frozenset(_COMPOSED_PROVIDER_FIELDS) | {"pat"}

#: NAMED reason: the ``[sdk]`` extra (``reachy_mini``) is not installed, so the
#: held media client and pose reader are permanently quiet — no mic, no camera
#: frames, no pose read-back. The HTTP remote profile's normal state.
SDK_EXTRA_ABSENT = "sdk-extra-absent"

#: NAMED reason: the pat stack was not composed at all (``REACHY_PAT_SENSE`` off).
#: An operator switch, not a missing dependency — a different fix, so a
#: different name.
PAT_SENSE_DISABLED = "pat-sense-disabled"

#: NAMED reason: the probe itself misbehaved (raised, or returned something that
#: is not a :class:`SenseAvailability`). Reported as UNAVAILABLE rather than
#: assumed fine: an unanswerable question is not a yes.
PROBE_ERROR = "probe-error"


@dataclass(frozen=True)
class SenseAvailability:
    """One sense's structural verdict — and, when dead, the reason by name.

    The invariant is enforced here rather than at the call sites so it cannot be
    forgotten: an unavailable sense MUST carry a non-blank reason, and an
    available one must NOT carry a reason at all (a lingering reason on a
    recovered sense is the same lie in the other direction).
    """

    available: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.available and self.reason is not None:
            raise ValueError(
                "an available sense must not carry a reason "
                f"(got reason={self.reason!r}); pass reason=None"
            )
        if not self.available and not (self.reason or "").strip():
            raise ValueError(
                "an unavailable sense must NAME its reason — a dead sense is "
                "never a silent no-op (see reachy.senselog's drop discipline)"
            )

    def as_dict(self) -> dict:
        """The wire shape published under :data:`STATE_KEY`."""
        return {"available": self.available, "reason": self.reason}


#: The single available verdict — frozen, so one shared instance is safe.
AVAILABLE = SenseAvailability(available=True, reason=None)


def unavailable(reason: str) -> SenseAvailability:
    """A dead-sense verdict carrying *reason*."""
    return SenseAvailability(available=False, reason=reason)


def sdk_unavailable_reason(find_spec: Callable[[str], object] | None = None) -> str | None:
    """:data:`SDK_EXTRA_ABSENT` when ``reachy_mini`` is absent, else ``None``.

    The ``[sdk]``-side sibling of
    :func:`reachy.behavior.face_sense.vision_unavailable_reason`, and injectable
    the same way: ``None`` resolves the module-level probe **at call time**, so
    monkeypatching ``sense_availability._find_spec`` works; a default ARGUMENT
    would bind at definition time and ignore it.

    A spec check, never an import: this must stay free of the SDK's
    pycairo/gstreamer transitive stack, which is exactly what is missing on the
    boxes this reason describes.
    """
    probe = _find_spec if find_spec is None else find_spec
    return SDK_EXTRA_ABSENT if probe("reachy_mini") is None else None


def runtime_probes(
    *,
    pat_composed: bool,
    face_recognizer_ready: bool,
    sdk_reason: Callable[[], str | None] | None = None,
    face_reason: Callable[[], str | None] | None = None,
) -> dict[str, Callable[[], SenseAvailability]]:
    """The probe map for ``behavior engine run``'s composed sense stack.

    Lives here rather than in the composition root so the call site stays three
    lines and the *reasoning* about which sense needs what is reviewable in one
    place. Every value is a zero-arg callable evaluated per tick (cheap: a
    cached import-spec lookup or a captured boolean), which is also what leaves
    room for a future standing dynamic verdict.

    Who needs what, and why:

    * ``pat`` / ``pat_event`` — the proprioceptive pose read-back, so the
      ``[sdk]`` extra; and the stack must actually have been composed.
    * ``rms`` / ``rms_ratio`` / ``transcript`` — all three ride the ONE mic tap,
      so all three need the ``[sdk]`` extra. (Hearing additionally needs a
      reachable lobes session, but a down session is transient and latched by
      the transcriber itself — see the module docstring on why this block does
      not report liveness.)
    * ``frame_available`` — camera frames come through the held media client:
      the ``[sdk]`` extra, and NOT ``[vision]`` (the frame-validity leg is
      numpy-only).
    * ``face`` — needs BOTH: ``[vision]`` to recognise anyone and ``[sdk]`` to
      have a frame to recognise them in. ``[vision]`` is reported FIRST because
      that is the one the #120 box was actually missing, and because it stays
      the answer even on a box that also lacks the SDK — the remediation ladder
      reads in dependency order rather than jumping about.
    * ``self_moving`` — derived from ``ctx.pose`` alone. No extra, no hardware,
      no network: it is never structurally dead, on any box.

    :param pat_composed: whether the composition built a ``PatSenseDriver``.
    :param face_recognizer_ready: whether ``build_face_recognition()`` returned a
        pair (passed in rather than re-probed, so this reports what the process
        ACTUALLY built).
    """
    sdk = sdk_unavailable_reason if sdk_reason is None else sdk_reason

    def _default_face_reason() -> str | None:
        return face_sense.face_recognition_unavailable_reason(face_recognizer_ready)

    face = _default_face_reason if face_reason is None else face_reason

    def _from(*reasons: Callable[[], str | None]) -> SenseAvailability:
        for probe in reasons:
            reason = probe()
            if reason is not None:
                return unavailable(reason)
        return AVAILABLE

    def _pat() -> SenseAvailability:
        if not pat_composed:
            return unavailable(PAT_SENSE_DISABLED)
        return _from(sdk)

    def _media() -> SenseAvailability:
        return _from(sdk)

    def _face() -> SenseAvailability:
        return _from(face, sdk)

    return {
        "pat": _pat,
        "pat_event": _pat,
        "rms": _media,
        "rms_ratio": _media,
        "transcript": _media,
        "frame_available": _media,
        "face": _face,
        "self_moving": lambda: AVAILABLE,
    }


class SenseAvailabilityDriver:
    """A ``TickBus`` rider publishing :data:`STATE_KEY` into the main ``state.json``.

    Register :meth:`__call__` on the engine's ``tick_seam``. It reads nothing off
    ``ctx`` — the verdicts are structural — and never raises out of a tick: a
    misbehaving probe becomes a :data:`PROBE_ERROR` entry, and a failing state
    write is logged once per occurrence and dropped. A sense tap that can crash
    the 50 Hz loop is a worse defect than the one it reports.

    Parameters
    ----------
    probes:
        ``{sense name -> () -> SenseAvailability}``, covering exactly
        :data:`AVAILABILITY_SENSES`. Both a missing name and an unknown one are
        refused at CONSTRUCTION (a composition-time programming error, caught by
        a test rather than by a consumer finding a hole in the block months
        later) — this is what makes ``_COMPOSED_PROVIDER_FIELDS`` the single
        source of truth in practice and not just in prose.
    main_control:
        The MAIN (unnamespaced) :class:`~reachy.behavior.control.CommandSpool` —
        the same file the engine's heartbeat writes. Defaults to a fresh one, as
        :class:`~reachy.behavior.intents.IntentDriver` does.
    root:
        State-dir override for the default spool (tests).
    """

    def __init__(
        self,
        probes: Mapping[str, Callable[[], SenseAvailability]],
        *,
        main_control: control_mod.CommandSpool | None = None,
        root: Path | None = None,
        senses: Iterable[str] = AVAILABILITY_SENSES,
    ) -> None:
        declared = frozenset(senses)
        missing = sorted(declared - set(probes))
        if missing:
            raise ValueError(
                f"no availability probe for {missing}; every declared sense must "
                "report, so a structurally dead one can never be a missing key"
            )
        unknown = sorted(set(probes) - declared)
        if unknown:
            raise ValueError(
                f"availability probe for unknown sense(s) {unknown}; the block's "
                f"vocabulary is {sorted(declared)}"
            )
        self._probes = dict(probes)
        self._main = main_control or control_mod.CommandSpool(root=root)
        self._published: dict | None = None

    # -- the block ---------------------------------------------------------- #

    def block(self) -> dict:
        """The current availability block: ``{sense: {available, reason}}``.

        Public because it is also the natural source for any other standing
        mirror of runtime state (a retained broker topic, a status readout) —
        one builder, so two surfaces can never disagree.
        """
        return {name: self._probe(name).as_dict() for name in sorted(self._probes)}

    def _probe(self, name: str) -> SenseAvailability:
        try:
            verdict = self._probes[name]()
        except Exception:  # an unanswerable question is not a yes
            logger.warning("availability probe for %r raised; reporting dead", name, exc_info=True)
            return unavailable(PROBE_ERROR)
        if not isinstance(verdict, SenseAvailability):
            logger.warning("availability probe for %r returned %r; reporting dead", name, verdict)
            return unavailable(PROBE_ERROR)
        return verdict

    # -- TickBus driver ------------------------------------------------------ #

    def __call__(self, ctx=None) -> None:  # ctx unused, kept for the seam shape
        """One tick: republish the block if the file no longer carries it."""
        try:
            self._publish()
        except Exception:  # a sense tap must never crash the loop
            logger.warning("sense availability publish raised; skipping this tick", exc_info=True)

    def _publish(self) -> None:
        block = self.block()
        current = self._main.read_state()
        if not isinstance(current, dict):
            current = {}
        if current.get(STATE_KEY) == block:
            return
        merged = dict(current)
        merged[STATE_KEY] = block
        self._main.write_state(merged)
        self._report(block)

    def _report(self, block: dict) -> None:
        """One senselog line per CHANGED verdict — bounded, because changes are rare.

        Skipped for a pure republish (the engine heartbeat clobbered the key and
        the rider put it back): that is bookkeeping, not news.
        """
        previous = self._published
        self._published = dict(block)
        if previous == block:
            return
        for name in sorted(block):
            entry = block[name]
            if previous is not None and previous.get(name) == entry:
                continue
            if entry["available"]:
                senselog.stage(STAGE, name, STATE_KEY, "sense available")
            else:
                senselog.drop(STAGE, name, STATE_KEY, entry["reason"])


__all__ = [
    "AVAILABILITY_SENSES",
    "AVAILABLE",
    "PAT_SENSE_DISABLED",
    "PROBE_ERROR",
    "SDK_EXTRA_ABSENT",
    "STAGE",
    "STATE_KEY",
    "SenseAvailability",
    "SenseAvailabilityDriver",
    "runtime_probes",
    "sdk_unavailable_reason",
    "unavailable",
]
