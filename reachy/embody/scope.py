"""What the background mind is allowed to put in front of the foreground voice.

The two-tempo architecture (issue #155) gives Reachy one voice and two minds.
**Gemma** is the foreground interlocutor: it hears, it answers, it owns the
wording and the decision to speak. **Qwen** follows the conversation in the
background, reasons over longer horizons, operates tools — and influences the
conversation only through explicit, inspectable typed events. This module is the
type of that influence: a **cognition scope**.

A scope is deliberately NOT reasoning
--------------------------------------
The obvious way to let a background mind help a foreground one is to hand over
its thinking. That is exactly what spec claim c8 forbids, and the reason is
practical rather than fastidious: raw reasoning is long, unbounded, written for
nobody, and — with ``enable_thinking`` on — measured at 9-18 SECONDS before the
first token (``docs/evidence/2026-08-02-probe-thinking-vs-reasoning-deltas.md``).
Injected into a realtime prompt it costs the tokens, buys the wrong thing, and
leaks a model's private draft into the sentence a human hears.

So a scope is a **compact artifact with a fixed shape**, and every field is
there because the foreground can act on it::

    {"type": "cognition.scope", "source": "qwen",
     "goal": "Clarify what object the user is referring to",
     "relevant_facts": ["The latest image contains two visible objects",
                        "The user previously referred to the left object"],
     "suggested_next_step": "Ask whether they mean the left object",
     "priority": "normal", "expires_after_turns": 2, "speakable": false}

Gemma may use a scope to shape its next response. It **remains responsible for
the wording and the decision to speak** — a scope suggests, it does not
dictate, and :meth:`CognitionScope.render` phrases it that way on purpose.

Four properties, each because its absence is a specific failure
----------------------------------------------------------------
* **attributed** (:attr:`CognitionScope.source`) — a suggestion nobody can trace
  is one nobody can withdraw, rate-bound or explain to an operator afterwards.
  Blank provenance is refused (:data:`REFUSAL_UNATTRIBUTED`), never defaulted.
* **bounded** — per field AND in total (:data:`REFUSAL_TOO_LARGE`). The per-field
  caps do not add up to the whole: five legal facts beside a legal goal and a
  legal next step are a paragraph, and "compact one field at a time" is not
  compact. Every bound is fail-closed — REFUSED, never truncated — the same
  idiom :data:`reachy.behavior.rules.MAX_SAY_CHARS` uses, because a truncated
  scope is a scope that misstates what the background mind meant.
* **expiring** (:attr:`CognitionScope.expires_after_turns`, counted in TURNS
  like :class:`reachy.embody.interjection.WantedToSay`) — a conversation moves
  on, and a stale scope shaping a later turn is worse than no scope at all.
* **context, never a trigger** — nothing here can wake the mind. The artifact
  has no class-carrying field and
  :meth:`reachy.embody.engine.EmbodyTurnEngine.submit_scope` has no parameter
  that could make one; the robot never wakes itself up to act on its own
  background thought.

Coalescing keys on kind + goal, never on free text (issue #154's lesson)
--------------------------------------------------------------------------
Issue #154 was free-form perception text filling a text-keyed park with
near-duplicates until genuine runtime facts were refused. A scope carries free
text too, so it never keys on any of it: :meth:`CognitionScope.key` is
``(kind, normalized goal)``. Two scopes pursuing the same GOAL are the same
standing concern restated — the later one REPLACES the earlier, because a goal
describes a STATE of the background mind's attention, not a log of past
thoughts. The facts and the suggested next step, being free text, are never
part of the key.

An interjection is the SPEAKABLE face of a scope
-------------------------------------------------
:func:`scope_from_interjection` is where the two families meet (spec assumption,
2026-08-02): an admitted :class:`reachy.embody.interjection.Interjection` — the
worker's own ``speak`` tool call, a mesh peer's typed event — becomes a scope
with ``speakable=True`` whose suggested next step is the proposed sentence. The
proposal reaches Gemma as a suggestion it may re-word or decline. Qwen still
never owns the mouth; c8's raw-reasoning ban applies to the content unchanged.

The goal of such a scope NAMES ITS SOURCE, which is what keeps the key
per-source: two background minds proposing different sentences are two standing
concerns, and one must not clobber the other.

Import boundary
----------------
Like the rest of ``reachy/embody/``: no ``reachy_mini``, no
:mod:`reachy.daemon`, no ``subprocess``, no shell. Two more, both asserted by
``tests/test_embody_scope.py``: **no model** (:mod:`reachy.speech.llm` is
absent — this module is a data type and its bounds, it calls nothing) and **no
mouth** (the synthesis/playback stack is absent — a scope is text in front of a
mind, never sound in a room).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from reachy import senselog

# --------------------------------------------------------------------------- #
# Senselog identity                                                           #
# --------------------------------------------------------------------------- #

#: ``[SENSE stage=scope source=<who proposed it> event=<id>] …`` — the
#: ``source`` field is the scope's own PROVENANCE, so one journal line answers
#: both "what was refused" and "whose thought it was".
STAGE = "scope"

# --------------------------------------------------------------------------- #
# The wire discriminator + the closed vocabularies                            #
# --------------------------------------------------------------------------- #

#: The artifact's ``type`` on the wire and on the export feed (issue #155's own
#: schema). Named here rather than typed as a bare string at each use site, the
#: same discipline :mod:`reachy.runtime_cues` keeps for the layer's two cue
#: families and :mod:`reachy.embody.tools` keeps for its command kinds.
SCOPE_TYPE = "cognition.scope"

#: Who a scope comes from when a caller does not say. The background mind is
#: the only producer today; a blank source is still REFUSED rather than
#: defaulted (see :func:`make_scope`), because "unattributed" and "the usual
#: producer" are different facts.
DEFAULT_SCOPE_SOURCE = "qwen"

#: The closed priority vocabulary. A free-text priority is a priority nothing
#: can compare, so an unknown one is refused rather than passed through.
PRIORITIES: tuple[str, ...] = ("low", "normal", "high")
DEFAULT_PRIORITY = "normal"

# --------------------------------------------------------------------------- #
# Defaults — the shipped values ARE the closed state                          #
# --------------------------------------------------------------------------- #

#: Turns a scope stays readable, counted AFTER the turn that created it.
#:
#: Two rather than one for the reason
#: :data:`reachy.embody.interjection.DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS` gives:
#: a background thought is usually followed by the human's own utterance and
#: then the robot's reply to it, so a one-turn life would often expire exactly
#: one beat before the moment it was kept for. Beyond that it is stale — a
#: foreground voice steered by a concern three turns old is not being helped,
#: it is being haunted.
DEFAULT_EXPIRES_AFTER_TURNS = 2

#: The longest life a caller may ask for. Refused above this, not clamped: a
#: producer asking for a fifty-turn scope has misunderstood what a scope is,
#: and silently shortening it would hide that.
DEFAULT_MAX_EXPIRES_AFTER_TURNS = 10

#: Characters the goal may run to. A goal is one clause — "clarify what object
#: the user means" — not a paragraph.
DEFAULT_MAX_GOAL_CHARS = 160
#: Facts one scope may carry. Five is "what the foreground actually needs to
#: know"; a mind that needs more is describing the conversation, not scoping it.
DEFAULT_MAX_FACTS = 5
#: Characters one fact may run to.
DEFAULT_MAX_FACT_CHARS = 160
#: Characters the suggested next step may run to. Shares its order of magnitude
#: with the other two on purpose — a next step the foreground cannot read at a
#: glance is not a next step.
DEFAULT_MAX_NEXT_STEP_CHARS = 200
#: The whole RENDERED artifact's cap — the one bound that actually makes a
#: scope compact, since the per-field caps do not add up to it. Sized so a full
#: park of :data:`reachy.embody.engine.DEFAULT_MAX_SCOPES` scopes stays a small
#: fraction of the ~2 399 prompt tokens one clip ask costs
#: (``docs/evidence/2026-08-02-t1-media-chunk-budget.md``).
DEFAULT_MAX_TOTAL_CHARS = 600

# --------------------------------------------------------------------------- #
# Named refusals — the label IS the drop reason, never a paraphrase           #
# --------------------------------------------------------------------------- #

#: The scope had no goal. A scope IS its goal; the rest is supporting material.
REFUSAL_EMPTY = "scope-empty"
#: The scope named no source. Provenance is required, never defaulted.
REFUSAL_UNATTRIBUTED = "scope-unattributed"
#: The goal was over :data:`DEFAULT_MAX_GOAL_CHARS`.
REFUSAL_GOAL_TOO_LONG = "scope-goal-too-long"
#: One relevant fact was over :data:`DEFAULT_MAX_FACT_CHARS`.
REFUSAL_FACT_TOO_LONG = "scope-fact-too-long"
#: More than :data:`DEFAULT_MAX_FACTS` relevant facts were offered.
REFUSAL_TOO_MANY_FACTS = "scope-too-many-facts"
#: The suggested next step was over :data:`DEFAULT_MAX_NEXT_STEP_CHARS`.
REFUSAL_NEXT_STEP_TOO_LONG = "scope-next-step-too-long"
#: The priority was outside the closed :data:`PRIORITIES` vocabulary.
REFUSAL_UNKNOWN_PRIORITY = "scope-unknown-priority"
#: The requested life was over :data:`DEFAULT_MAX_EXPIRES_AFTER_TURNS`.
REFUSAL_EXPIRY_TOO_LONG = "scope-expiry-too-long"
#: Every field fit and the whole still did not — see
#: :attr:`ScopeLimits.max_total_chars`.
REFUSAL_TOO_LARGE = "scope-too-large"
#: A wire payload was not a usable scope (not a dict, wrong ``type``, or no
#: goal). Named rather than raised: malformed input on a local-trust feed is a
#: normal outcome, not an exception.
REFUSAL_MALFORMED = "scope-malformed"

#: Every refusal this module can produce. Exported so the journal, the export
#: feed, the operator docs and the tests share ONE vocabulary — the discipline
#: :data:`reachy.embody.tools.REFUSALS` and
#: :data:`reachy.embody.interjection.REFUSALS` already keep.
REFUSALS: frozenset[str] = frozenset(
    {
        REFUSAL_EMPTY,
        REFUSAL_UNATTRIBUTED,
        REFUSAL_GOAL_TOO_LONG,
        REFUSAL_FACT_TOO_LONG,
        REFUSAL_TOO_MANY_FACTS,
        REFUSAL_NEXT_STEP_TOO_LONG,
        REFUSAL_UNKNOWN_PRIORITY,
        REFUSAL_EXPIRY_TOO_LONG,
        REFUSAL_TOO_LARGE,
        REFUSAL_MALFORMED,
    }
)


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _as_float(value: object) -> float:
    """A real number from wire data, or ``0.0``. ``bool`` is not a timestamp."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _as_int(value: object, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return int(value)


# --------------------------------------------------------------------------- #
# Bounds, in ONE frozen home                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScopeLimits:
    """Everything a scope is allowed to be, in one object.

    The repo's rule: bounds live in a frozen ``Limits``-style dataclass whose
    ``DEFAULT_*`` constants carry the reasoning
    (:class:`reachy.embody.engine.Limits` and
    :class:`reachy.embody.interjection.InterjectionLimits` are the siblings).
    This class does not re-explain each number — every field carries its
    constant forward unchanged, so a refactor cannot silently move one.
    """

    max_goal_chars: int = DEFAULT_MAX_GOAL_CHARS
    max_facts: int = DEFAULT_MAX_FACTS
    max_fact_chars: int = DEFAULT_MAX_FACT_CHARS
    max_next_step_chars: int = DEFAULT_MAX_NEXT_STEP_CHARS
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS
    max_expires_after_turns: int = DEFAULT_MAX_EXPIRES_AFTER_TURNS


# --------------------------------------------------------------------------- #
# The artifact                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CognitionScope:
    """One compact, attributed, expiring thinking scope from a background mind.

    Frozen, and built through :func:`make_scope` rather than directly in
    production code: the constructor holds the SHAPE, that function holds the
    BOUNDS, and separating them is what lets a test build a deliberately
    over-large scope to prove the bounds bite.

    :meth:`as_event` and :meth:`from_event` are inverse, which is what "typed
    and inspectable" has to mean to be worth anything: the object on the export
    feed is not a rendering of the artifact, it IS the artifact, and a consumer
    can reconstruct it.
    """

    goal: str
    source: str = DEFAULT_SCOPE_SOURCE
    relevant_facts: tuple[str, ...] = ()
    suggested_next_step: str = ""
    priority: str = DEFAULT_PRIORITY
    expires_after_turns: int = DEFAULT_EXPIRES_AFTER_TURNS
    #: Whether the background mind is proposing that something be SAID. Even
    #: then the foreground keeps the wording and the decision — see
    #: :func:`scope_from_interjection` and :meth:`render`.
    speakable: bool = False
    #: The artifact family. A field rather than a constant so a later family
    #: can share :meth:`key`'s shape without a second key format; serialized as
    #: the spec's ``type``.
    kind: str = SCOPE_TYPE
    #: The turn index this was created at — expiry is relative to this.
    created_turn: int = 0
    id: str = field(default_factory=_new_id)
    ts: float = 0.0

    def key(self) -> tuple[str, str]:
        """The coalescing key: ``(kind, normalized goal)`` — never any free text.

        Issue #154's lesson, one family over. Two scopes pursuing the same goal
        are the same standing concern restated, so the later REPLACES the
        earlier; the facts and the suggested next step never enter the key,
        because two wordings of one idea would otherwise be two entries and a
        park keyed on free text fills with near-duplicates.
        """
        return (self.kind, self.goal.strip().casefold())

    def is_expired(self, turn: int) -> bool:
        """Whether *turn* is past this scope's life.

        :attr:`expires_after_turns` counts turns AFTER the one that created it,
        so ``1`` means "the turn it was made on and the next one". Being
        readable by the NEXT turn is the whole point, which is why
        :func:`make_scope` floors the value at 1.
        """
        return int(turn) > self.created_turn + self.expires_after_turns

    def render(self) -> str:
        """The prompt fragment the foreground lane reads.

        Phrased as a SUGGESTION throughout, because that is what it is: spec
        claim c2 leaves the wording and the decision to speak with the
        foreground voice, and a prompt that reads like an instruction is how a
        background mind quietly becomes the speaker.
        """
        lines = [f"- goal ({self.source}, {self.priority} priority): {self.goal}"]
        lines += [f"    relevant: {fact}" for fact in self.relevant_facts]
        if self.suggested_next_step:
            verb = "may say something like" if self.speakable else "suggested next step"
            lines.append(f"    {verb}: {self.suggested_next_step}")
        return "\n".join(lines)

    def as_event(self) -> dict:
        """The wire/feed shape — issue #155's ``cognition.scope`` schema.

        The spec's seven content fields plus the three the layer needs to make
        the artifact inspectable afterwards (its id, the turn it was made on,
        and the producer's clock reading). There is deliberately no field here
        that could carry model reasoning, and ``tests/test_embody_scope.py``
        walks the type to prove it.
        """
        return {
            "type": self.kind,
            "id": self.id,
            "source": self.source,
            "goal": self.goal,
            "relevant_facts": list(self.relevant_facts),
            "suggested_next_step": self.suggested_next_step,
            "priority": self.priority,
            "expires_after_turns": self.expires_after_turns,
            "speakable": self.speakable,
            "created_turn": self.created_turn,
            "ts": self.ts,
        }

    @classmethod
    def from_event(cls, event: object) -> "CognitionScope | None":
        """Rebuild one from a wire payload, or ``None`` when it is not a usable one.

        Never raises: this parses input that reaches the layer over a
        local-trust feed. It reads ONLY the fields it knows, which is the
        structural half of c8's raw-reasoning ban — a payload carrying
        ``reasoning`` (or any other extra) does not have it stripped later, it
        never enters the artifact at all.

        Bounds are NOT applied here: this is the shape, and
        :func:`scope_from_event` is the bounded front door a caller should use.
        """
        if not isinstance(event, dict) or event.get("type") != SCOPE_TYPE:
            return None
        goal = event.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return None
        raw_facts = event.get("relevant_facts")
        facts = (
            tuple(str(fact) for fact in raw_facts if isinstance(fact, str) and fact.strip())
            if isinstance(raw_facts, Sequence) and not isinstance(raw_facts, (str, bytes))
            else ()
        )
        source = event.get("source")
        next_step = event.get("suggested_next_step")
        priority = event.get("priority")
        raw_id = event.get("id")
        return cls(
            goal=goal,
            source=source if isinstance(source, str) and source.strip() else "",
            relevant_facts=facts,
            suggested_next_step=next_step if isinstance(next_step, str) else "",
            priority=priority if isinstance(priority, str) else DEFAULT_PRIORITY,
            expires_after_turns=_as_int(
                event.get("expires_after_turns"), DEFAULT_EXPIRES_AFTER_TURNS
            ),
            speakable=bool(event.get("speakable")),
            created_turn=_as_int(event.get("created_turn"), 0),
            id=raw_id if isinstance(raw_id, str) and raw_id else _new_id(),
            ts=_as_float(event.get("ts")),
        )


# --------------------------------------------------------------------------- #
# The bounded front doors                                                     #
# --------------------------------------------------------------------------- #


def _refuse(source: str, reason: str) -> None:
    senselog.drop(STAGE, source or "unattributed", _new_id(), reason)


def make_scope(
    goal: str,
    *,
    source: str = DEFAULT_SCOPE_SOURCE,
    relevant_facts: Sequence[str] = (),
    suggested_next_step: str = "",
    priority: str = DEFAULT_PRIORITY,
    expires_after_turns: int = DEFAULT_EXPIRES_AFTER_TURNS,
    speakable: bool = False,
    kind: str = SCOPE_TYPE,
    turn: int = 0,
    limits: ScopeLimits | None = None,
    now: float = 0.0,
) -> CognitionScope | None:
    """Build a bounded, attributed scope — or refuse it by name.

    Returns ``None`` on a refusal, after exactly one named
    :func:`reachy.senselog.drop`. Two things it deliberately does NOT do:

    * **it never truncates.** Every over-length field is refused, because a
      trimmed goal or a cut-off suggestion misstates what the background mind
      meant, and the foreground would read the remainder as complete.
    * **it never raises.** Producers call this from a worker thread and from a
      tool handler; an exception there costs the layer a turn over a
      bookkeeping detail.

    The one value that IS clamped is the life: an expiry below one turn is
    raised to one, mirroring :func:`reachy.embody.interjection.
    make_wanted_to_say`. An artifact that expires before anything can read it
    is silently useless rather than loudly wrong, and being readable by the
    next turn is the entire point.
    """
    bounds = limits if limits is not None else ScopeLimits()
    cleaned_goal = (goal or "").strip()
    cleaned_source = (source or "").strip()
    facts = tuple(fact.strip() for fact in relevant_facts if isinstance(fact, str) and fact.strip())
    next_step = (suggested_next_step or "").strip()

    if not cleaned_goal:
        _refuse(cleaned_source, REFUSAL_EMPTY)
        return None
    if not cleaned_source:
        _refuse("", REFUSAL_UNATTRIBUTED)
        return None
    if len(cleaned_goal) > bounds.max_goal_chars:
        _refuse(cleaned_source, REFUSAL_GOAL_TOO_LONG)
        return None
    if len(facts) > bounds.max_facts:
        _refuse(cleaned_source, REFUSAL_TOO_MANY_FACTS)
        return None
    if any(len(fact) > bounds.max_fact_chars for fact in facts):
        _refuse(cleaned_source, REFUSAL_FACT_TOO_LONG)
        return None
    if len(next_step) > bounds.max_next_step_chars:
        _refuse(cleaned_source, REFUSAL_NEXT_STEP_TOO_LONG)
        return None
    if priority not in PRIORITIES:
        _refuse(cleaned_source, REFUSAL_UNKNOWN_PRIORITY)
        return None
    if int(expires_after_turns) > bounds.max_expires_after_turns:
        _refuse(cleaned_source, REFUSAL_EXPIRY_TOO_LONG)
        return None

    built = CognitionScope(
        goal=cleaned_goal,
        source=cleaned_source,
        relevant_facts=facts,
        suggested_next_step=next_step,
        priority=priority,
        expires_after_turns=max(1, int(expires_after_turns)),
        speakable=bool(speakable),
        kind=kind,
        created_turn=int(turn),
        ts=float(now),
    )
    if len(built.render()) > bounds.max_total_chars:
        _refuse(cleaned_source, REFUSAL_TOO_LARGE)
        return None
    return built


def scope_from_event(
    event: object, *, turn: int, limits: ScopeLimits | None = None, now: float = 0.0
) -> CognitionScope | None:
    """Parse and BOUND one scope that arrived as a typed payload.

    The front door a caller reading a feed should use:
    :meth:`CognitionScope.from_event` gives the shape,
    :func:`make_scope` gives the bounds, and a payload that fails either is one
    named drop rather than a raise. ``created_turn`` is taken from *turn* — the
    RECEIVING layer's own clock — never from the payload, so a producer cannot
    hand itself a scope that outlives its welcome by claiming to be from the
    future.
    """
    parsed = CognitionScope.from_event(event)
    if parsed is None:
        _refuse(_event_source(event), REFUSAL_MALFORMED)
        return None
    return make_scope(
        parsed.goal,
        source=parsed.source,
        relevant_facts=parsed.relevant_facts,
        suggested_next_step=parsed.suggested_next_step,
        priority=parsed.priority,
        expires_after_turns=parsed.expires_after_turns,
        speakable=parsed.speakable,
        kind=parsed.kind,
        turn=turn,
        limits=limits,
        now=now,
    )


#: How the goal of an interjection-derived scope is phrased. It NAMES THE
#: SOURCE, which is what keeps :meth:`CognitionScope.key` per-source: two
#: background minds proposing different sentences are two standing concerns and
#: neither may clobber the other. The wording is a decision to take, never an
#: instruction to obey — spec claim c2 leaves the mouth with the foreground.
INTERJECTION_GOAL_TEMPLATE = "Decide whether to say what {source} suggested"


def scope_from_interjection(
    interjection: object,
    *,
    turn: int,
    limits: ScopeLimits | None = None,
    now: float = 0.0,
) -> CognitionScope | None:
    """The speakable face of a scope: an ADMITTED interjection, as background context.

    *interjection* is duck-typed (:class:`reachy.embody.interjection.
    Interjection` in production) so this module keeps no import edge to the
    policy — the dependency runs the other way, and the composition root joins
    them.

    The result is ``speakable=True`` and carries the proposed sentence as its
    suggested next step. That is the whole mechanism behind "Qwen never owns the
    mouth": what reaches the foreground is a suggestion attributed to whoever
    made it, and Gemma decides the wording and whether to say anything at all.
    An over-long proposal is refused here exactly as any other over-long scope
    field is — the policy has already applied
    :data:`reachy.behavior.rules.MAX_SAY_CHARS`, and this bound is about the
    PROMPT rather than about the utterance.
    """
    text = (getattr(interjection, "text", "") or "").strip()
    source = (getattr(interjection, "source", "") or "").strip()
    if not text:
        _refuse(source, REFUSAL_EMPTY)
        return None
    return make_scope(
        INTERJECTION_GOAL_TEMPLATE.format(source=source or DEFAULT_SCOPE_SOURCE),
        source=source or DEFAULT_SCOPE_SOURCE,
        suggested_next_step=text,
        speakable=True,
        priority=DEFAULT_PRIORITY,
        turn=turn,
        limits=limits,
        now=now,
    )


def _event_source(event: object) -> str:
    """Best-effort provenance for a malformed payload, for the drop line only."""
    if isinstance(event, dict):
        source = event.get("source")
        if isinstance(source, str) and source.strip():
            return source.strip()
    return "unattributed"
