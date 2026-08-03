"""Who else may speak through the robot's mouth — and how the layer says no.

The two-tempo architecture (issue #155) gives Reachy one voice and two minds.
The operator talks with **Gemma**, the foreground interlocutor: it hears, it
answers, it owns turn-taking. **Qwen** is background cognition — it follows the
conversation, reasons over longer horizons and operates tools, and it never
owns the mouth. That invariant is what keeps the robot's speech coherent, and
it is not negotiable.

What IS negotiable, and is the whole subject of this module, is that a
background mind sometimes has something worth saying *now*. So a source may be
AUTHORIZED — opt-in, never the default — to throw an **interjection** into the
conversation. The interjection travels as a typed, inspectable event carrying
its own provenance; the foreground voice renders it into speech. Qwen still
never speaks. It proposes, and something else decides how to say it.

By a later operator decision the same door is open to any non-audio external
system — a mesh peer, an API caller — under the SAME policy: explicit opt-in,
default OFF, identical attention semantics. That is why nothing here knows
which route an interjection arrived by. One decision point means one place to
get default-deny right; a per-route policy would be four places to get it
wrong.

What this module bounds, and what it does not
----------------------------------------------
**It bounds cost and manners. It does not bound blast radius.** Say that
plainly, because the layer already has a module people reach for when they want
a security story and it is not one either: ``tests/test_embody_redteam.py``
records why attention cannot carry a containment claim — it is a two-state
machine anyone in the room can open by saying "reachy" out loud, so a claim
resting on it is a claim that the attacker will not say "reachy". The same is
true here one family over. Containment rests where it always did: on the closed
five-tool action set and the fail-closed validators that already shipped.

What an interjection widens is **who may put text in front of the mind**, and
how often. It carries nothing executable — text plus provenance, and the
red-team suite pins that this module reaches no tool surface at all.

The policy, cheapest-first
---------------------------
:meth:`InterjectionPolicy.admit` runs six checks in this order, and every
outcome is NAMED — the label is used verbatim as the :func:`reachy.senselog.drop`
reason and as the ``refusal`` field of the tool result, so the journal, the
export feed and the model all see the same word:

=====  ==============================  =========================================
order  check                           refusal
=====  ==============================  =========================================
1      the text is not blank           :data:`REFUSAL_EMPTY`
2      authorization is not OFF        :data:`REFUSAL_UNAUTHORIZED`
3      the source is allow-listed      :data:`REFUSAL_SOURCE_DENIED`
4      the text is within the say cap  :data:`REFUSAL_TOO_LONG`
5      attention permits it            :data:`REFUSAL_COLD`
6      the source's rate budget        :data:`REFUSAL_RATE_LIMITED`
=====  ==============================  =========================================

Two orderings there are load-bearing rather than tidy. The source check
precedes the rate check, so the rate table is only ever keyed by allow-listed
names — a flood of forged source names is refused at step 3 and allocates
nothing, which bounds this module's MEMORY by the same default-deny that bounds
its manners. And the rate budget is spent only on an ADMISSION, so an
interjection refused for arriving while the room was quiet does not cost its
source the chance to say the same thing when the conversation reopens.

Warm, proactive, and the asymmetry that must not be re-derived
----------------------------------------------------------------
Authorization is three states, not a boolean, because "may interject" and "may
interject UNINVITED" are different permissions:

============  =============================================================
level         what it permits
============  =============================================================
``OFF``       nothing. The shipped default.
``WARM``      an interjection while :mod:`reachy.embody.attention` is warm —
              i.e. while a human is already in conversation with the robot.
``PROACTIVE`` the above, plus interjecting into a cold room.
============  =============================================================

An interjection **never opens the attention window**, at either level. This
mirrors, exactly, the extend-never-open asymmetry
:meth:`reachy.embody.attention.AttentionGate.note_spoken` already carries, and
the reason is the same and is worth restating rather than re-deriving: against
the gateway deployed today the duplex session is armed once and the SERVER
answers every committed utterance out loud (per-utterance arming is wired and
fails closed — see that module's docstring), so the robot's own voice fires
``note_spoken`` for replies to chatter the gate has just refused. A voice that
could open attention would be a robot
waking itself up — ``reachy/speech/engagement.py``'s one-way-ratchet defect
(199 correct drops, 39 accepts, *all wrong*) arriving through a door the
runtime never had. An interjection is the layer speaking uninvited, which is
the case where that would be worst, so :meth:`InterjectionPolicy.note_spoken`
delegates to the gate's own seam and inherits the guarantee rather than
re-implementing it.

An ADMITTED interjection rides the **alert** lane
(:data:`ADMITTED_CUE_CLASS`), and that is the precedent the spec cites: a rule
fire triggers a turn from cold and opens nothing. Attention gates the EAR,
never the robot's own reactions.

The wanted-to-say artifact
---------------------------
The second type here is the other side of interjection: when a HUMAN interjects
over an audibly speaking robot, the reply is cut mid-sentence. The said portion
is recorded as spoken; the remainder is not discarded silently and is never
recorded as said — it becomes a :class:`WantedToSay` artifact, attributed to
the response it came from, bounded, and expiring in TURNS the way a cognition
scope does.

It is **CONTEXT only, structurally** — never a trigger. The robot never wakes
itself up to finish an old sentence, which is the same no-self-wake asymmetry
one more time. That is not enforced by a convention: the artifact has no
class-carrying field, its :meth:`WantedToSay.as_cue` is hard-wired to
:data:`WANTED_TO_SAY_CUE_CLASS`, and ``tests/test_embody_interjection.py``
walks this module's AST to prove the artifact's own surface never so much as
mentions the alert lane.

This module validates nothing of its own
-----------------------------------------
The one bound it needs that already has an owner — how long an utterance may
be — is IMPORTED from :data:`reachy.behavior.rules.MAX_SAY_CHARS`, never
restated, exactly as :mod:`reachy.embody.tools` imports it "because a rule is
not its only caller". A second copy of a bound is a second number to drift, and
a bound living only in the layer is one an operator using the CLI does not get.
Everything else here (the levels, the source list, the rate budget, the expiry)
is a policy no downstream validator has ever heard of, so this module is its
only possible owner, and all of it lives in ONE frozen
:class:`InterjectionLimits` whose shipped values are the closed state.

Import boundary
----------------
Like the rest of ``reachy/embody/``: no ``reachy_mini``, no
:mod:`reachy.daemon`, no ``subprocess``, no shell. Two more, specific to this
module and both asserted by test:

* **no synthesis and no playback.** It decides who may speak; it has no mouth
  of its own, so :mod:`reachy.speech.tts` / :mod:`~reachy.speech.playback` /
  :mod:`~reachy.speech.voice` / :mod:`~reachy.speech.realtime_duplex` and
  :mod:`reachy.embody.media` are all absent from its import closure.
* **no** :mod:`reachy.speech.engagement`. Attention arrives as an injected
  ``is_warm()`` answer, and the runtime's engagement gate additionally carries
  the single-shot LLM classifier whose importer set
  ``tests/test_zero_llm_boundary.py`` pins BY EQUALITY. Reaching for it is a
  separate decision and must not ride along on an interjection feature.
"""

from __future__ import annotations

import enum
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from reachy import runtime_cues, senselog
from reachy.behavior.rules import MAX_SAY_CHARS
from reachy.embody.cues import ClassifiedCue, CueClass

# --------------------------------------------------------------------------- #
# Senselog identity                                                           #
# --------------------------------------------------------------------------- #

#: ``[SENSE stage=interjection source=<who> event=<id>] …`` — the ``source``
#: field is the interjection's own PROVENANCE, so one journal line answers both
#: "what happened" and "who asked for it".
STAGE = "interjection"

# --------------------------------------------------------------------------- #
# Authorization                                                               #
# --------------------------------------------------------------------------- #


class Authorization(enum.Enum):
    """How much an authorized source may interrupt. Three states, not a boolean.

    ``OFF`` is the shipped default and is enforced, not documented: with it, no
    route reaches speech and every attempt is a named drop.
    """

    #: Nothing may interject. The shipped default.
    OFF = "off"
    #: An authorized source may interject while attention is WARM — i.e. while a
    #: human is already in conversation with the robot.
    WARM = "warm"
    #: The above, plus interjecting into a cold room. A separate, explicit
    #: permission because "may join a conversation" and "may start one" are
    #: different things to grant.
    PROACTIVE = "proactive"


# --------------------------------------------------------------------------- #
# Defaults — the shipped values ARE the closed state                          #
# --------------------------------------------------------------------------- #

#: Default OFF (spec claim c22). An operator turns interjection on with a
#: deliberate act; nothing about installing or starting the layer does it.
DEFAULT_AUTHORIZATION = Authorization.OFF

#: Default-deny per source (spec claim c42). Empty rather than "the worker" on
#: purpose: naming a LEVEL is not the same as naming a SOURCE, and an operator
#: who enables interjection has still not said whose.
DEFAULT_SOURCES: tuple[str, ...] = ()

#: Interjections one source may land per :data:`DEFAULT_RATE_WINDOW_S`.
#:
#: Three is a conversational number rather than a resource one. The bound this
#: exists to prevent is not CPU — the layer's other bounds cover that — it is a
#: background mind that keeps butting in, which is a manners failure and shows
#: up long before any queue fills. Three interjections a minute is roughly "at
#: most one per exchange" at the conversational pace measured live
#: (``docs/evidence/2026-08-02-t14-live-acceptance.md``), and a source with
#: something genuinely urgent still gets its first one immediately: the budget
#: is spent, never pre-paid.
DEFAULT_MAX_PER_WINDOW = 3

#: The sliding window the budget above is measured over, in seconds. ``0``
#: disables the rate bound entirely, the same convention
#: :attr:`reachy.embody.engine.Limits.min_alert_interval_s` uses for ``0``.
DEFAULT_RATE_WINDOW_S = 60.0

#: How many turns a :class:`WantedToSay` artifact stays readable.
#:
#: It must be at least 1, or the artifact would expire before the next turn
#: could read it — and being readable by the next turn is the entire point (the
#: model decides whether the unsaid remainder is still worth saying, rather than
#: the robot deciding to finish its sentence). Two rather than one because a
#: cut is usually followed by the human's own utterance and then the robot's
#: reply to it, so a one-turn life would often expire exactly one beat before
#: the moment it was kept for. Beyond that it is stale: a robot completing a
#: sentence from three turns ago is not remembering, it is perseverating.
DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS = 2

# --------------------------------------------------------------------------- #
# Named outcomes — the label IS the drop reason, never a paraphrase           #
# --------------------------------------------------------------------------- #

#: Admitted while a conversation was live.
LABEL_WARM = "interjection-warm"
#: Admitted into a cold room, under :attr:`Authorization.PROACTIVE`.
LABEL_PROACTIVE = "interjection-proactive"

#: Every outcome that ADMITS. Kept apart from :data:`REFUSALS` so a caller can
#: tell the two apart without string matching, and pinned disjoint by test.
ADMIT_LABELS: tuple[str, ...] = (LABEL_WARM, LABEL_PROACTIVE)

#: Interjection is not authorized at all — the shipped default. This is what an
#: attempt by ANY route resolves to before anything else is even considered
#: (spec honesty condition h12).
REFUSAL_UNAUTHORIZED = "interjection-unauthorized"
#: The source is not on the allow-list. Default-deny: an unknown name is a
#: refused name (spec claim c42).
REFUSAL_SOURCE_DENIED = "interjection-source-denied"
#: The room is cold and the level is only :attr:`Authorization.WARM`.
REFUSAL_COLD = "interjection-cold"
#: This source has spent its budget for the current window.
REFUSAL_RATE_LIMITED = "interjection-rate-limited"
#: The proposed text was blank.
REFUSAL_EMPTY = "interjection-empty"
#: The proposed text was over :data:`reachy.behavior.rules.MAX_SAY_CHARS` — the
#: same cap a rule's ``say`` field and the layer's voice tools carry.
REFUSAL_TOO_LONG = "interjection-too-long"
#: A wire event was not a usable interjection (not a dict, wrong ``t``, or
#: missing its text or its provenance). Named rather than raised: a malformed
#: event on a local-trust feed is a normal outcome, not an exception.
REFUSAL_MALFORMED = "interjection-malformed"
#: The unsaid remainder handed to :func:`make_wanted_to_say` was blank.
REFUSAL_WANTED_TO_SAY_EMPTY = "wanted-to-say-empty"
#: The unsaid remainder was over the say cap. Refused, never truncated — see
#: :func:`make_wanted_to_say`.
REFUSAL_WANTED_TO_SAY_TOO_LONG = "wanted-to-say-too-long"

#: Every refusal this module can produce. Exported so the journal, the export
#: feed, the operator docs and the tests share ONE vocabulary — the same
#: discipline :data:`reachy.embody.tools.REFUSALS` keeps.
REFUSALS: frozenset[str] = frozenset(
    {
        REFUSAL_UNAUTHORIZED,
        REFUSAL_SOURCE_DENIED,
        REFUSAL_COLD,
        REFUSAL_RATE_LIMITED,
        REFUSAL_EMPTY,
        REFUSAL_TOO_LONG,
        REFUSAL_MALFORMED,
        REFUSAL_WANTED_TO_SAY_EMPTY,
        REFUSAL_WANTED_TO_SAY_TOO_LONG,
    }
)

# --------------------------------------------------------------------------- #
# The two lanes                                                               #
# --------------------------------------------------------------------------- #

#: The lane an ADMITTED interjection rides: the ALERT class, which triggers a
#: turn from cold and opens nothing (spec claim c22 cites exactly this
#: precedent). Only the policy may promote an interjection here — the wire
#: route classifies both layer families as CONTEXT
#: (:data:`reachy.embody.cues.CUE_CLASSIFIERS`).
ADMITTED_CUE_CLASS: CueClass = CueClass.ALERT

#: The ONLY lane a :class:`WantedToSay` artifact may ride, ever. A constant
#: rather than a parameter: an artifact that could be constructed as a trigger
#: is an artifact that will eventually be constructed as one.
WANTED_TO_SAY_CUE_CLASS: CueClass = CueClass.CONTEXT


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _as_float(value: object) -> float:
    """A real number from wire data, or ``0.0``. ``bool`` is not a timestamp."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


# --------------------------------------------------------------------------- #
# Bounds + authorization, in ONE frozen home                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InterjectionLimits:
    """Everything :class:`InterjectionPolicy` is allowed to permit, in one object.

    The repo's rule is that bounds live in a ``Limits``-style frozen dataclass
    with ``DEFAULT_*`` constants carrying the reasoning
    (:class:`reachy.embody.engine.Limits` is the sibling). Two of these fields
    are not numbers, and they live here anyway, on purpose: honesty condition
    h13 requires the default-OFF state to ship as the shipped default "in
    Limits/config, not as documentation". Putting :attr:`authorization` and
    :attr:`sources` anywhere else would make the closed state a property of how
    a composition root happens to call the constructor. Here, an operator
    reading the layer's configuration sees a robot nobody may speak through and
    a source list nobody is on.
    """

    #: How much an authorized source may interrupt. Ships :attr:`Authorization.OFF`.
    authorization: Authorization = DEFAULT_AUTHORIZATION
    #: The allow-listed source names. Ships EMPTY — default-deny per source.
    sources: tuple[str, ...] = DEFAULT_SOURCES
    #: Admissions one source may spend per :attr:`rate_window_s`. ``0`` admits
    #: nothing (a zero budget is a closed door, not an unbounded one).
    max_per_window: int = DEFAULT_MAX_PER_WINDOW
    #: The sliding window the budget is measured over. ``0`` disables the bound.
    rate_window_s: float = DEFAULT_RATE_WINDOW_S
    #: The say cap, IMPORTED from its one home so there is no second number to
    #: drift. See the module docstring's "validates nothing of its own".
    max_chars: int = MAX_SAY_CHARS
    #: Turns a :class:`WantedToSay` artifact stays readable.
    wanted_to_say_expiry_turns: int = DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS


# --------------------------------------------------------------------------- #
# The typed event family                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Interjection:
    """One proposed interjection: text, and who proposed it.

    Frozen, and provenance is a REQUIRED field rather than optional metadata —
    an interjection whose source is unknown cannot be authorized, cannot be
    rate-bounded, and cannot be explained to an operator afterwards, so it is
    not a thing this module represents.

    :meth:`as_event` and :meth:`from_event` are inverse, which is what "typed
    and inspectable" has to mean to be worth anything: the event on the export
    feed is not a rendering of the decision, it IS the decision, and a consumer
    can reconstruct it.
    """

    text: str
    source: str
    id: str = field(default_factory=_new_id)
    #: The policy clock's reading when the interjection was admitted (monotonic
    #: by default, so it is an ordering, not a wall time). The export hook
    #: stamps its own wall-clock ``ts`` when the block lands on the feed.
    ts: float = 0.0

    def render(self) -> str:
        """The cue line, through the ONE owner of the phrasing."""
        return runtime_cues.interjection_cue(self.text, self.source)

    def as_event(self) -> dict:
        """The wire/feed shape, carrying its ``t`` discriminator and provenance."""
        return {
            "t": runtime_cues.LINE_INTERJECTION,
            "id": self.id,
            "source": self.source,
            "text": self.text,
            "ts": self.ts,
        }

    @classmethod
    def from_event(cls, event: object) -> "Interjection | None":
        """Rebuild one from a wire event, or ``None`` when it is not a usable one.

        Never raises: this parses attacker-reachable input off a local-trust
        feed, so a malformed event is a normal outcome the caller names (see
        :meth:`InterjectionPolicy.admit_event`). Text and source are both
        required — an event missing either is not an under-specified
        interjection, it is not one.
        """
        if not isinstance(event, dict) or event.get("t") != runtime_cues.LINE_INTERJECTION:
            return None
        text = event.get("text")
        source = event.get("source")
        if not isinstance(text, str) or not text.strip():
            return None
        if not isinstance(source, str) or not source.strip():
            return None
        raw_id = event.get("id")
        return cls(
            text=text,
            source=source,
            id=raw_id if isinstance(raw_id, str) and raw_id else _new_id(),
            ts=_as_float(event.get("ts")),
        )


@dataclass(frozen=True)
class InterjectionVerdict:
    """One :meth:`InterjectionPolicy.admit` outcome, in every shape a caller needs.

    Three consumers, one object: the composition needs a cue (or nothing), the
    model needs a tool result it can read, and the journal needs a named reason.
    The last one has already happened by the time this exists — the policy emits
    it — and the first two are :meth:`as_cue` and :meth:`as_result`.

    Attributes:
        admitted: whether this interjection may reach the conversation.
        label: the NAMED outcome — one of :data:`ADMIT_LABELS` or
            :data:`REFUSALS`, used verbatim as the ``senselog`` reason and the
            tool result's ``refusal`` field.
        detail: the human-readable half, in the refusing check's own words.
        interjection: the typed event, present ONLY on an admission. A refusal
            deliberately carries no event: an interjection that exists is one
            that may be published, and a half-existing one is how a refusal
            becomes a leak.
    """

    admitted: bool
    label: str
    detail: str = ""
    interjection: Interjection | None = None

    def as_cue(self) -> ClassifiedCue | None:
        """The ALERT-classed cue for an admitted interjection; ``None`` otherwise.

        A refusal is not a cue. Returning an empty-but-present cue would be the
        exact silent no-op this layer refuses everywhere else.
        """
        if not self.admitted or self.interjection is None:
            return None
        return ClassifiedCue(text=self.interjection.render(), cue_class=ADMITTED_CUE_CLASS)

    def as_result(self) -> dict:
        """The tool-result payload, shaped like :meth:`reachy.embody.tools`' own.

        Every refusal returns as a result AND lands on the export feed — a
        refusal the model cannot see is not a refusal, it is a silence the model
        will read as success and repeat.
        """
        if not self.admitted or self.interjection is None:
            return {"ok": False, "refusal": self.label, "error": self.detail}
        return {"ok": True, "admitted": self.label, "interjection": self.interjection.as_event()}


@dataclass(frozen=True)
class WantedToSay:
    """The measured remainder of a reply a human cut off, kept honestly.

    Three properties, each of which exists because its absence is a specific
    failure:

    * **attributed** (:attr:`response_id`) — without it the remainder is a
      floating sentence nobody can trace to the reply it belonged to;
    * **expiring** (:attr:`expires_in_turns`, counted in TURNS like a cognition
      scope) — without it a stale remainder shapes a conversation that has
      moved on;
    * **context-only** — see :data:`WANTED_TO_SAY_CUE_CLASS`. There is no field
      here that could make it a trigger, and :meth:`as_cue` names the constant.

    It is never recorded as spoken. That is the point: the said portion is what
    was said, and this is what was not.
    """

    text: str
    #: The interrupted response this remainder came from.
    response_id: str
    #: The turn index it was created at — expiry is relative to this.
    created_turn: int
    expires_in_turns: int = DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS
    id: str = field(default_factory=_new_id)
    ts: float = 0.0

    def is_expired(self, turn: int) -> bool:
        """Whether *turn* is past this artifact's life.

        :attr:`expires_in_turns` counts turns AFTER the one that created it, so
        ``1`` means "the turn it was made on and the next one". Readable by the
        NEXT turn is the requirement, and an off-by-one in this direction would
        silently make the artifact useless rather than loudly wrong — which is
        why :func:`make_wanted_to_say` floors the value at 1.
        """
        return int(turn) > self.created_turn + self.expires_in_turns

    def render(self) -> str:
        """The cue line, through the ONE owner of the phrasing."""
        return runtime_cues.wanted_to_say_cue(self.text)

    def as_cue(self) -> ClassifiedCue:
        """The CONTEXT cue — the only lane this artifact has.

        Not a parameter, not a default: the class is
        :data:`WANTED_TO_SAY_CUE_CLASS`, and the trigger lane is not reachable
        from this method or from anything else on this type. The robot never
        wakes itself to finish an old sentence.
        """
        return ClassifiedCue(text=self.render(), cue_class=WANTED_TO_SAY_CUE_CLASS)

    def as_event(self) -> dict:
        """The feed shape, carrying its attribution and its expiry."""
        return {
            "t": runtime_cues.LINE_WANTED_TO_SAY,
            "id": self.id,
            "response_id": self.response_id,
            "text": self.text,
            "created_turn": self.created_turn,
            "expires_in_turns": self.expires_in_turns,
            "ts": self.ts,
        }


def make_wanted_to_say(
    text: str,
    *,
    response_id: str,
    turn: int,
    limits: InterjectionLimits | None = None,
    now: float = 0.0,
) -> WantedToSay | None:
    """Build a bounded, attributed artifact, or refuse it by name.

    Returns ``None`` on a refusal, after exactly one named
    :func:`reachy.senselog.drop`. Two things this deliberately does NOT do:

    * **it never truncates.** An over-long remainder is refused
      (:data:`REFUSAL_WANTED_TO_SAY_TOO_LONG`), because a truncated remainder is
      a false record of what the robot meant to say — worse than no record,
      since the next turn would read it as complete and might say it.
    * **it never raises.** This is called from the playback-cut path, where an
      exception would cost the layer its session over a bookkeeping detail.
    """
    bounds = limits if limits is not None else InterjectionLimits()
    cleaned = (text or "").strip()
    if not cleaned:
        senselog.drop(STAGE, response_id or "unattributed", _new_id(), REFUSAL_WANTED_TO_SAY_EMPTY)
        return None
    if len(cleaned) > bounds.max_chars:
        senselog.drop(
            STAGE,
            response_id or "unattributed",
            _new_id(),
            REFUSAL_WANTED_TO_SAY_TOO_LONG,
        )
        return None
    return WantedToSay(
        text=cleaned,
        response_id=response_id,
        created_turn=int(turn),
        expires_in_turns=max(1, int(bounds.wanted_to_say_expiry_turns)),
        ts=float(now),
    )


# --------------------------------------------------------------------------- #
# Collaborator protocol (documentation; any matching object is accepted)      #
# --------------------------------------------------------------------------- #


class _AttentionLike(Protocol):
    """The two questions this module asks of attention, and nothing more.

    Structurally typed, so the policy depends on the ANSWER ("is a conversation
    live?") rather than on :class:`reachy.embody.attention.AttentionGate`
    itself. :meth:`note_spoken` is delegated rather than reimplemented on
    purpose — its extend-never-open guarantee is the gate's, and a second
    implementation is a second thing to get wrong.
    """

    def is_warm(self, now: float | None = ...) -> bool: ...

    def note_spoken(self, now: float | None = ...) -> bool: ...


# --------------------------------------------------------------------------- #
# The policy                                                                  #
# --------------------------------------------------------------------------- #


class InterjectionPolicy:
    """Decides who may interject, and says no by name. Never speaks, never raises.

    Args:
        limits: the whole configuration — level, sources, rate budget, say cap,
            artifact expiry — in one frozen :class:`InterjectionLimits`. The
            default is the CLOSED state, which is the point: a policy nobody
            configured admits nothing.
        attention: anything answering :class:`_AttentionLike`; in production
            :class:`reachy.embody.attention.AttentionGate`, which the engine
            already owns and shares its clock with. ``None`` means the layer has
            no attention gate, and reads as COLD — fail-closed, so a missing
            collaborator narrows the policy rather than widening it.
        clock: the monotonic clock the rate window is measured on. Injected like
            every other cadence in this codebase, so a 60 s window is testable
            without waiting 60 s.

    Threading: the rate table is mutated under a lock, because unlike
    :class:`~reachy.embody.attention.AttentionGate` (one float, one thread) this
    is a check-then-act on a dict of deques, and interjections arrive from at
    least two threads — the cue reader and the turn loop. The lock is never held
    across a log write or a call into ``attention``.
    """

    def __init__(
        self,
        *,
        limits: InterjectionLimits | None = None,
        attention: _AttentionLike | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits if limits is not None else InterjectionLimits()
        self._attention = attention
        self._clock = clock
        self._sources: frozenset[str] = frozenset(_clean_sources(self._limits.sources))
        self._max_per_window = int(self._limits.max_per_window)
        self._rate_window_s = max(0.0, float(self._limits.rate_window_s))
        self._max_chars = int(self._limits.max_chars)
        # Keyed ONLY by allow-listed sources: the source check runs first, so a
        # flood of forged names never reaches this dict. Default-deny bounds the
        # memory as well as the manners.
        self._recent: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Queries                                                            #
    # ------------------------------------------------------------------ #

    @property
    def limits(self) -> InterjectionLimits:
        """The frozen configuration this policy was built with."""
        return self._limits

    @property
    def authorization(self) -> Authorization:
        """The configured level. :attr:`Authorization.OFF` unless an operator said otherwise."""
        return self._limits.authorization

    @property
    def sources(self) -> tuple[str, ...]:
        """The allow-listed source names, sorted. Empty is the shipped state."""
        return tuple(sorted(self._sources))

    def is_authorized(self, source: str) -> bool:
        """Whether *source* may interject at all — level AND allow-list, ANDed.

        Both halves are required: a level without a source list permits nobody,
        and a source list without a level permits nothing. Neither is a
        sufficient act of authorization on its own, which is deliberate — two
        deliberate acts are harder to perform by accident than one.
        """
        return self.authorization is not Authorization.OFF and source in self._sources

    def tracked_sources(self) -> tuple[str, ...]:
        """Sources currently holding rate state, sorted. Diagnostics + the memory pin."""
        with self._lock:
            return tuple(sorted(self._recent))

    # ------------------------------------------------------------------ #
    # The decision                                                       #
    # ------------------------------------------------------------------ #

    def admit(self, text: str, *, source: str) -> InterjectionVerdict:
        """Judge one proposed interjection. Never raises, never speaks.

        Route-agnostic on purpose — the worker's own tool call, a mesh peer's
        typed event and an external API call differ only in the name that
        arrives as *source*. See the module docstring for the check order and
        which parts of it are load-bearing.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return self._refuse(source, REFUSAL_EMPTY, "an interjection needs something to say")
        if self.authorization is Authorization.OFF:
            return self._refuse(
                source,
                REFUSAL_UNAUTHORIZED,
                "interjection is not authorized on this robot; an operator enables it "
                "explicitly and it is off by default",
            )
        if source not in self._sources:
            allowed = ", ".join(self.sources) or "(no source is authorized)"
            return self._refuse(
                source,
                REFUSAL_SOURCE_DENIED,
                f"source {source!r} is not authorized to interject; authorized: {allowed}",
            )
        if len(cleaned) > self._max_chars:
            return self._refuse(
                source,
                REFUSAL_TOO_LONG,
                f"the interjection is {len(cleaned)} characters, over the {self._max_chars}-"
                "character limit — the same bound a rule's say field carries",
            )

        label = self._attention_label()
        if label is None:
            return self._refuse(
                source,
                REFUSAL_COLD,
                "nobody is in conversation with the robot; the base authorization "
                "interjects only into a live conversation",
            )
        if not self._spend(source):
            return self._refuse(
                source,
                REFUSAL_RATE_LIMITED,
                f"source {source!r} has spent its {self._max_per_window} interjections "
                f"for the last {self._rate_window_s:g}s",
            )

        interjection = Interjection(text=cleaned, source=source, ts=self._clock())
        senselog.stage(
            STAGE, source, interjection.id, f"interjection admitted ({label}) {cleaned!r}"
        )
        return InterjectionVerdict(True, label, interjection=interjection)

    def admit_event(self, event: object) -> InterjectionVerdict:
        """Judge one interjection that arrived as a typed wire event.

        The SAME policy as :meth:`admit` — the wire route gets no shortcut the
        tool route lacks — plus one extra failure the tool route cannot have: an
        event that is not a usable interjection at all.
        """
        parsed = Interjection.from_event(event)
        if parsed is None:
            return self._refuse(
                _event_source(event),
                REFUSAL_MALFORMED,
                "not a usable interjection event: it needs "
                f"t={runtime_cues.LINE_INTERJECTION!r}, a non-empty 'text' and a "
                "non-empty 'source'",
            )
        return self.admit(parsed.text, source=parsed.source)

    def note_spoken(self, text: str = "") -> bool:
        """The layer spoke an interjection. EXTENDS a live window; never opens one.

        Returns whether it extended anything, so a caller can tell a
        conversation being kept alive from the robot talking into a room it is
        not part of. Delegated to
        :meth:`reachy.embody.attention.AttentionGate.note_spoken` rather than
        reimplemented — the asymmetry is that module's guarantee, and this one
        inherits it instead of owning a second copy that could drift open.
        """
        if self._attention is None:
            return False
        spoken = bool(self._attention.note_spoken())
        if text.strip():
            senselog.stage(
                STAGE,
                "self",
                _new_id(),
                f"interjection spoken (attention {'extended' if spoken else 'unchanged'})",
            )
        return spoken

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _attention_label(self) -> str | None:
        """The admit label attention permits, or ``None`` to refuse as cold.

        A missing gate reads as COLD, so :attr:`Authorization.WARM` refuses and
        :attr:`Authorization.PROACTIVE` still admits — fail-closed at the base
        level, and unchanged at the level that explicitly asked for cold.
        """
        warm = self._attention is not None and bool(self._attention.is_warm())
        if warm:
            return LABEL_WARM
        if self.authorization is Authorization.PROACTIVE:
            return LABEL_PROACTIVE
        return None

    def _spend(self, source: str) -> bool:
        """Check-and-consume one unit of *source*'s rate budget.

        Only ever called AFTER every other check has passed, so a refusal for
        any other reason leaves the budget untouched: being refused for arriving
        into a quiet room must not cost a source the chance to say the same
        thing when the conversation reopens.
        """
        if self._max_per_window <= 0:
            return False
        now = float(self._clock())
        with self._lock:
            spent = self._recent.get(source)
            if spent is None:
                spent = self._recent[source] = deque(maxlen=self._max_per_window)
            if len(spent) == self._max_per_window and now - spent[0] < self._rate_window_s:
                return False
            spent.append(now)
        return True

    def _refuse(self, source: str, reason: str, detail: str) -> InterjectionVerdict:
        """Name the refusal on the journal and return it in the caller's shape."""
        senselog.drop(STAGE, source or "unattributed", _new_id(), reason)
        return InterjectionVerdict(False, reason, detail=detail)


def _clean_sources(sources: Sequence[str]) -> tuple[str, ...]:
    """Non-blank source names, whitespace-trimmed.

    A blank entry in the allow-list would authorize the anonymous source — the
    one an event with no provenance would arrive under — which is precisely the
    thing default-deny exists to prevent.
    """
    return tuple(name.strip() for name in sources if isinstance(name, str) and name.strip())


def _event_source(event: object) -> str:
    """Best-effort provenance for a malformed event, for the drop line only."""
    if isinstance(event, dict):
        source = event.get("source")
        if isinstance(source, str) and source.strip():
            return source.strip()
    return "unattributed"
