"""LLM engagement engine — is this utterance *addressed to the robot*?

This module is the decision core of issue #55.  When the robot hears a
transcribed utterance it must decide whether to engage cognition (the speaker
is talking **to** the robot) or stay quiet (two people are talking to *each
other*).  The crucial distinction this module enforces is **addressed-to-me**,
not **could-I-help**:

    A spoken sentence like "could you grab me a coffee" between two humans is
    helpable, but it is NOT addressed to the robot.  The robot must not butt
    into human-to-human conversation just because it has something to offer.
    Engage only when the speaker is talking TO the robot — by name, or as a
    clear continuation of a conversation already underway with it.

The decision is layered, cheapest-first:

1. **Name fast-path.**  If the utterance plausibly *names* the robot — the
   canonical "reachy"/"robot" **or** a common STT mishearing ("richie",
   "reachie", "robbot") caught by :func:`reachy.speech.name_match.is_name_match`
   — it is addressed to the robot by definition.  Engage immediately, with **no
   LLM call**.

2. **LLM classifier.**  Otherwise a single-shot "is this aimed at me?" classifier
   (:class:`EngagementClassifier`, backed by :func:`reachy.speech.llm.complete`)
   judges the utterance against the recent conversation.  A positive verdict
   engages; a negative one drops.

3. **Degrade.**  If the classifier is unavailable (network error / timeout /
   unparseable response) the decision is the :data:`Decision.DEGRADE` sentinel.
   ``decide_engagement`` deliberately runs **no fallback heuristic of its own**
   — DEGRADE is the caller's signal to apply whatever cheap heuristic it owns
   (e.g. the word-count + conversation-window rule).  Keeping the fallback out
   of this module keeps the policy in one place (the caller) and this module a
   pure classifier.

``decide_engagement`` makes **at most one** classifier call per invocation, and
**zero** when the name fast-path already decides ENGAGE.

--------------------------------------------------------------------------
The conversation state (:class:`ConversationGate`) — issue #105
--------------------------------------------------------------------------
``decide_engagement`` is deliberately **stateless**: it judges one utterance
against a context that somebody else maintains.  Every caller maintained that
context the same way, and the way was wrong::

    if engaged:
        self._history.append(text)

Only ACCEPTED utterances ever entered the history that the *next* classifier
call is told is "the recent conversation".  There was no decay and no negative
evidence, so **the only evidence the gate could accumulate was evidence that a
conversation is happening** — a one-way ratchet in which a single false accept
plants a six-turn mid-conversation context and every accept re-seeds it.

Measured on the deployed robot over 45 minutes, operator present, the robot's
name never spoken:

===========================  =====
verdict                      count
===========================  =====
``dropped``                    199
``context`` (classifier YES)    36
``name`` (fast path)             3
===========================  =====

The 199 drops prove the classifier works.  The 39 accepts were *all* wrong, and
the ``context`` ones were exactly the short continuations a mid-conversation
reading accepts: "No.", "Okay.", "Right.", "Yeah.", "Hold up." — each firing an
audible chirp into a conversation the robot was not part of.  The gate was not
broken; it was **leaky, and the leak self-amplified**.

:class:`ConversationGate` owns that state and gives it three ways to lose
confidence.  Two of them are *structural* — they change control flow so the
classifier is not consulted at all — which is the point: a merely advisory fix
(e.g. also feeding DROP verdicts into the prompt as negative evidence) leaves
the decision with a model that already said YES 36 times out of 36, and cannot
be proven by a test.  These can:

1. **A warm window, opened only by a name.**  Context-only engagement requires
   a conversation that is currently *live*, and only a NAME mention can open one
   from cold (any subsequent accept extends it).  Past ``warm_window_s`` of
   quiet the conversation closes and a nameless utterance is dropped **with no
   classifier call**.  This is what makes the leak un-amplifiable: in the
   measured session no name was ever spoken, so the gate would have stayed cold
   for all 45 minutes and none of the 36 context accepts could have occurred.
   It also just enforces what this module's own contract always claimed — "by
   name, or a clear continuation of a conversation already underway with it" —
   which was previously left entirely to the classifier's judgement.
2. **A short-utterance rule.**  A context-only engagement requires at least
   ``min_context_words`` words.  Every false context accept measured was one or
   two words; a backchannel carries almost no addressing signal, and
   backchannels in a room are overwhelmingly human-to-human.  A NAME match is
   exempt, so a bare "Reachy!" still engages.  The cost is stated plainly: a
   genuine two-word reply ("yes please") mid-conversation is missed.  That is
   the right trade here because nothing in this architecture is *waiting* for a
   reply — there is no dialogue state machine — so a missed short reply costs
   one turn, while an admitted backchannel costs an unprompted chirp, and the
   latter was measured at ~36 per 45 minutes.
3. **History decay.**  Turns older than the warm window are not "recent" and are
   dropped from the context, so a conversation reopened an hour later does not
   hand the classifier the previous one's turns.

Both thresholds deliberately reuse the values the callers' own DEGRADE-path
heuristic already used (a 20 s conversation window, a 3-word floor).  That
heuristic always had both a decay and a word floor; the defect was that the
classifier path had neither.  This brings the two paths into agreement rather
than inventing a third set of numbers.
"""

from __future__ import annotations

import enum
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from reachy.speech.name_match import DEFAULT_THRESHOLD, SHIPPED_NAMES, is_name_match

#: Canonical names the robot answers to — an ALIAS of
#: :data:`reachy.speech.name_match.SHIPPED_NAMES`, which is the ONE place the
#: shipped pair is spelled.  The alias is kept (rather than the import being
#: used directly at every site) because it is part of this module's public
#: surface: ``reachy.embody.attention.DEFAULT_NAMES`` is pinned equal to it by
#: test, and callers pass it explicitly.
DEFAULT_NAMES: tuple[str, ...] = SHIPPED_NAMES

#: What a ``names`` parameter accepts anywhere in this module: a plain sequence
#: (the shipped pair, or an operator's configured list) OR a zero-arg callable
#: returning one.  The callable form is what makes the names LIVE — the robot's
#: names can change while the runtime is up, and the next utterance is judged
#: against the new set with nothing rebuilt.
NamesLike = Sequence[str] | Callable[[], Sequence[str]]

#: Tight, bounded default timeout for a single classifier call (seconds).  A
#: classifier sits in the perception hot-loop, so a slow/dead endpoint must fail
#: fast and degrade rather than stall the loop.  Shorter than ``llm.complete``'s
#: own 10 s default.
DEFAULT_CLASSIFIER_TIMEOUT: float = 5.0

#: How long a conversation stays live after the last accepted turn (seconds).
#: Past this, a nameless utterance is dropped with NO classifier call and only a
#: fresh name can reopen the conversation.  Mirrors the callers' own
#: ``engage_window_s`` heuristic default, so both paths share one definition of
#: "the conversation is still going".
DEFAULT_WARM_WINDOW_S: float = 20.0

#: Minimum word count for a CONTEXT-only engagement.  A name match is exempt.
#: Mirrors the callers' ``min_words`` heuristic default.  Every false context
#: engagement measured live ("No.", "Okay.", "Right.", "Yeah.", "Hold up.") is
#: below this floor.
DEFAULT_MIN_CONTEXT_WORDS: int = 3

#: How many recent accepted turns are handed to the classifier as context.
DEFAULT_HISTORY_MAXLEN: int = 6

#: Word tokeniser for the short-utterance rule — the same pattern the
#: transcribe pipeline uses (letters plus intra-word apostrophes).
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

#: Verdict labels.  Every outcome is NAMED, and each drop names its own reason,
#: so a journal line is never a silent no-op.  All three drop labels share the
#: ``not-addressed`` prefix, so the existing grep still finds every drop.
LABEL_NAME = "name"
LABEL_CONTEXT = "context"
LABEL_DROPPED = "not-addressed"
LABEL_SHORT = "not-addressed-short"
LABEL_COLD = "not-addressed-cold"
LABEL_DEGRADE = "degrade"

#: System prompt TEMPLATE for the engagement classifier — rendered against the
#: names the robot currently answers to (``{names}``).
#:
#: Parked as a tunable follow-up (issue #55): the exact wording is a single
#: module-level constant so it can be tuned in one place without touching the
#: control flow.  The contract it must keep is the addressed-vs-helpable
#: distinction and the strict ``YES``/``NO`` answer shape that
#: :meth:`EngagementClassifier._parse` depends on.
#:
#: There is deliberately NO name literal in the template (issue #177).  A
#: hardcoded "Reachy" was a second, silent copy of the robot's identity: an
#: operator who renames the robot would still have had the classifier told the
#: old name, so a configured name would engage on the fast path and then be
#: judged by a classifier that had never heard of it.
ENGAGEMENT_PROMPT_TEMPLATE: str = (
    "You decide whether a spoken utterance is addressed to a small desk robot "
    "that answers to the names: {names}, given the recent conversation. Engage "
    "only if the speaker is talking TO the robot or clearly continuing a "
    "conversation with it — NOT if two people are talking to each other, even "
    "about something the robot could help with. Being helpable is not the same "
    "as being addressed: do not engage just because the robot could assist. "
    "Answer with exactly YES or NO."
)


def resolve_names(names: NamesLike) -> tuple[str, ...]:
    """Resolve a :data:`NamesLike` into a concrete tuple of names, at USE time.

    A callable is invoked here and now — that is the whole point of the
    provider form: swapping what the provider returns takes effect on the very
    next utterance, with no gate or classifier rebuilt.

    Never raises.  A provider that raises, returns a non-sequence, or yields
    nothing usable degrades to :data:`~reachy.speech.name_match.SHIPPED_NAMES`
    rather than to the empty tuple: an empty name set would silently take away
    the ONLY thing that can open a cold conversation (the name fast-path), so a
    misconfigured provider would leave a robot that can never be addressed
    again — indistinguishable, from the room, from a wedged runtime.
    """
    value: Any = names
    if callable(value):
        try:
            value = value()
        except Exception:  # a names provider must never break a decision
            return SHIPPED_NAMES
    if isinstance(value, str) or not isinstance(value, Sequence):
        return SHIPPED_NAMES
    resolved = tuple(str(name) for name in value if isinstance(name, str) and name.strip())
    return resolved or SHIPPED_NAMES


def render_engagement_prompt(names: NamesLike) -> str:
    """Render the classifier's system prompt naming EVERY configured name.

    ``names`` may be a sequence or a provider (see :data:`NamesLike`); it is
    resolved at call time, so rendering per call is what keeps the prompt in
    step with a live rename.
    """
    return ENGAGEMENT_PROMPT_TEMPLATE.format(names=", ".join(resolve_names(names)))


#: The classifier prompt as rendered for the SHIPPED names.  Kept as a
#: module-level constant for backward compatibility (callers imported it, and
#: it is the readable reference form); a classifier given other names renders
#: its own.
ENGAGEMENT_SYSTEM_PROMPT: str = render_engagement_prompt(SHIPPED_NAMES)


class Decision(enum.Enum):
    """Three-valued engagement decision.

    Members
    -------
    ENGAGE
        The utterance is addressed to the robot — feed it to cognition.
    DROP
        The utterance is ambient (human-to-human / not addressed) — ignore it.
    DEGRADE
        The classifier was unavailable (raised / timed out / unparseable).  This
        is a sentinel, **not** a decision: the caller should fall back to its own
        cheap heuristic.  ``decide_engagement`` never runs that heuristic itself.
    """

    ENGAGE = "engage"
    DROP = "drop"
    DEGRADE = "degrade"


class EngagementClassifier:
    """Single-shot LLM classifier: "is this utterance aimed at the robot?".

    Wraps :func:`reachy.speech.llm.complete` behind an injectable
    ``complete_fn`` seam (so tests pass a fake) and turns its free-text answer
    into a boolean.  The classifier judges **addressed-to-the-robot**, not
    **helpable** — see the module docstring and :data:`ENGAGEMENT_SYSTEM_PROMPT`.

    The call is non-streaming and bounded by a tight default *timeout*
    (:data:`DEFAULT_CLASSIFIER_TIMEOUT`) so a slow endpoint surfaces quickly.

    This class does **not** swallow transport errors: if ``complete_fn`` raises
    (network / timeout) or the response is unparseable, :meth:`judge` lets it
    propagate.  :func:`decide_engagement` is what maps such a failure onto
    :data:`Decision.DEGRADE` — mirroring ``llm.complete``'s own raise-don't-
    swallow policy.
    """

    def __init__(
        self,
        *,
        complete_fn: Callable[..., str] | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_CLASSIFIER_TIMEOUT,
        names: NamesLike = SHIPPED_NAMES,
        system_prompt: str | None = None,
    ) -> None:
        """Build a classifier.

        Parameters
        ----------
        complete_fn:
            The single-shot completion callable.  Defaults to
            :func:`reachy.speech.llm.complete`; tests inject a fake.  Called as
            ``complete_fn(messages, model=..., base_url=..., api_key=...,
            timeout=...)`` and expected to return the assistant text.
        model, base_url, api_key:
            Optional LLM connection overrides, threaded straight through to
            ``complete_fn`` (which resolves the ``REACHY_OPENAI_*`` env when they
            are ``None``).
        timeout:
            Per-call timeout in seconds.  Defaults to a tight, bounded value so
            the perception loop degrades instead of hanging.
        names:
            The names the robot answers to — a sequence, or a zero-arg provider
            (see :data:`NamesLike`).  Used ONLY to render the system prompt, and
            resolved on every call, so a provider swap reaches the very next
            judgement without this object being rebuilt.
        system_prompt:
            An explicit classifier instruction, which WINS over the rendered
            one for the object's whole life.  ``None`` (the default) renders
            :data:`ENGAGEMENT_PROMPT_TEMPLATE` against *names*.
        """
        if complete_fn is None:
            # Resolved HERE, not as a default argument, so importing this module
            # does not pull the LLM client into every process that only wants
            # ``ConversationGate`` / ``is_name_match``.  ``_build_parser()`` reaches
            # this module through ``behavior.transcript_sense``, so a module-scope
            # ``from reachy.speech import llm`` put an LLM client in the import
            # path of EVERY ``reachy`` invocation — ``say run``, ``daemon status``,
            # even ``--help``.  Found by t24's import-boundary suite.
            from reachy.speech import llm

            complete_fn = llm.complete
        self._complete_fn = complete_fn
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout
        self._names = names
        self._explicit_prompt = system_prompt

    @property
    def system_prompt(self) -> str:
        """The instruction this call will use: the explicit one, else rendered.

        Rendering per call is the cheapest way to keep the prompt honest about a
        live rename — it is one ``str.format`` against a tuple, off the tick
        thread, immediately before a network round-trip.
        """
        if self._explicit_prompt is not None:
            return self._explicit_prompt
        return render_engagement_prompt(self._names)

    def judge(self, text: str, context: Sequence[str]) -> bool:
        """Return ``True`` iff *text* is addressed to the robot.

        Builds a system + user message list, calls ``complete_fn`` once, and
        parses the answer (``True`` iff it starts with "YES", leniently).

        Parameters
        ----------
        text:
            The new utterance to judge.
        context:
            Recent accepted turns, oldest-first.  How many turns to pass is the
            caller's choice (parked as a follow-up); any sequence is accepted,
            including empty.

        Raises
        ------
        Exception
            Whatever ``complete_fn`` raises (network / timeout) propagates
            unchanged; the caller decides the error policy.
        """
        messages = self._build_messages(text, context)
        answer = self._complete_fn(
            messages,
            model=self._model,
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._timeout,
        )
        return self._parse(answer)

    def _build_messages(self, text: str, context: Sequence[str]) -> list[dict]:
        """Assemble the system + user message list for the classifier call."""
        if context:
            context_block = "\n".join(f"- {turn}" for turn in context)
            recent = f"Recent conversation (oldest first):\n{context_block}\n\n"
        else:
            recent = "Recent conversation: (none)\n\n"
        user = (
            f"{recent}"
            f'New utterance: "{text}"\n\n'
            "Is this new utterance addressed to the robot? Answer YES or NO."
        )
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _parse(answer: str) -> bool:
        """Parse a classifier answer into a boolean.

        Lenient: strips surrounding whitespace/quotes, uppercases, and returns
        ``True`` iff the answer *starts with* "YES".  Anything else — "NO", an
        empty string, or an explanation that does not lead with YES — is
        ``False``.  A non-string answer raises ``ValueError`` so
        :func:`decide_engagement` maps it to DEGRADE rather than guessing.
        """
        if not isinstance(answer, str):
            raise ValueError(f"classifier answer was not a string: {answer!r}")
        normalised = answer.strip().strip("\"'.,!? \t\n").upper()
        return normalised.startswith("YES")


def decide_engagement(
    text: str,
    context: Sequence[str],
    *,
    classifier: EngagementClassifier,
    names: NamesLike = DEFAULT_NAMES,
    name_threshold: float = DEFAULT_THRESHOLD,
) -> Decision:
    """Decide whether *text* is addressed to the robot.

    Layered, cheapest-first:

    1. **Name fast-path** — if :func:`is_name_match` accepts *text* (exact
       "reachy"/"robot" or a fuzzy STT mishearing like "richie"), return
       :data:`Decision.ENGAGE` immediately, making **no** classifier call.
    2. **Classifier** — otherwise call ``classifier.judge(text, context)`` once.
       A positive verdict → ENGAGE; a negative verdict → DROP.
    3. **Degrade** — if the classifier raises (timeout / network / parse), return
       :data:`Decision.DEGRADE`.  This function runs **no fallback heuristic**;
       DEGRADE is the caller's signal to apply its own.

    The function makes **at most one** classifier call, and **zero** on the name
    fast-path.

    Parameters
    ----------
    text:
        The new utterance to judge.
    context:
        Recent accepted turns (oldest-first), passed straight to the classifier.
    classifier:
        The (injectable) engagement classifier — only consulted off the name
        path.  Tests pass a fake to assert call counts.
    names:
        Canonical names for the fast-path — a sequence or a zero-arg provider
        (:data:`NamesLike`), resolved here.  Defaults to :data:`DEFAULT_NAMES`.
    name_threshold:
        Fuzzy-match threshold handed to :func:`is_name_match`.  Defaults to the
        name-matcher's own :data:`~reachy.speech.name_match.DEFAULT_THRESHOLD`.

    Returns
    -------
    Decision
        ``ENGAGE`` / ``DROP`` / ``DEGRADE`` (see :class:`Decision`).
    """
    # 1. Name fast-path — short-circuit, no classifier call.
    if is_name_match(text, resolve_names(names), name_threshold):
        return Decision.ENGAGE

    # 2. Single classifier call; 3. any failure degrades.
    return _judge_or_degrade(classifier, text, context)


def _judge_or_degrade(
    classifier: EngagementClassifier, text: str, context: Sequence[str]
) -> Decision:
    """One guarded classifier call: YES → ENGAGE, NO → DROP, any raise → DEGRADE.

    Shared by :func:`decide_engagement` and :class:`ConversationGate` so the
    raise-means-DEGRADE policy is written exactly once.
    """
    try:
        verdict = classifier.judge(text, context)
    except Exception:  # any failure means "classifier unavailable"
        return Decision.DEGRADE
    return Decision.ENGAGE if verdict else Decision.DROP


@dataclass(frozen=True)
class GateVerdict:
    """One :meth:`ConversationGate.decide` outcome: a decision plus its NAME.

    The label is the observability contract: callers log it and use it verbatim
    as the ``senselog.drop`` reason, so every drop says *why* it dropped
    (``not-addressed`` / ``not-addressed-short`` / ``not-addressed-cold``) rather
    than collapsing three different rules into one indistinguishable outcome.
    """

    decision: Decision
    label: str


class ConversationGate:
    """The stateful engagement gate — the layered ladder plus conversation state.

    Wraps :func:`decide_engagement`'s stateless ladder in the conversation state
    that makes it un-amplifiable (issue #105; the full argument and the measured
    numbers are in the module docstring).  One instance per hearing runtime,
    replacing the ``self._history`` list every caller used to maintain by hand.

    The ladder, cheapest-first — the ordering is preserved, and the two new
    rules make it *cheaper*, not more expensive, because both short-circuit
    before the classifier:

    1. **Name** → ENGAGE, zero classifier calls, and the conversation opens (or
       is extended).  A name outranks every other rule: a bare "Reachy!" engages
       however short, however long the silence before it.
    2. **Too short** → DROP ``not-addressed-short``, zero classifier calls.
    3. **Conversation not live** → DROP ``not-addressed-cold``, zero classifier
       calls.  Only a name can reopen it.
    4. **Classifier** → exactly one call, judged against the non-expired turns.
       ENGAGE extends the conversation; DROP is ``not-addressed``; a raise is
       DEGRADE, and the caller applies its own heuristic then reports an accept
       back via :meth:`note_engaged`.

    Threading
    ---------
    Not thread-safe, and does not need to be: both callers drive it from their
    single background transcript worker, exactly as they drove the list it
    replaces.

    Parameters
    ----------
    classifier:
        The engagement classifier (duck-typed on ``judge(text, context)``).
        ``None`` means "no classifier available", and every non-name utterance
        returns DEGRADE so the caller's heuristic decides — which is what the
        ``REACHY_ENGAGE_HEURISTIC`` escape hatch and an unconfigured endpoint
        both want.
    names, name_threshold:
        Passed to :func:`~reachy.speech.name_match.is_name_match`.  ``names``
        may be a plain sequence or a zero-arg provider (:data:`NamesLike`) and
        is resolved on every :meth:`decide`, so an operator renaming the robot
        while the runtime is up is obeyed by the very next utterance — no gate
        is rebuilt and no conversation state is lost.
    warm_window_s:
        How long the conversation stays live after the last accepted turn, and
        the age past which a turn is dropped from the classifier context.
    min_context_words:
        Word floor for a context-only engagement (a name match is exempt).
    history_maxlen:
        Cap on the turns handed to the classifier.
    clock:
        Monotonic clock, used only when a caller omits ``now``.
    """

    def __init__(
        self,
        *,
        classifier: EngagementClassifier | None = None,
        names: NamesLike = DEFAULT_NAMES,
        name_threshold: float = DEFAULT_THRESHOLD,
        warm_window_s: float = DEFAULT_WARM_WINDOW_S,
        min_context_words: int = DEFAULT_MIN_CONTEXT_WORDS,
        history_maxlen: int = DEFAULT_HISTORY_MAXLEN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._classifier = classifier
        #: The names SOURCE — held unresolved so a provider stays live.
        self._names_source: NamesLike = names
        self._name_threshold = name_threshold
        self._warm_window_s = max(0.0, float(warm_window_s))
        self._min_context_words = max(0, int(min_context_words))
        self._history_maxlen = max(0, int(history_maxlen))
        self._clock = clock
        #: Accepted turns as ``(timestamp, text)``, oldest first.
        self._history: list[tuple[float, str]] = []
        #: Monotonic deadline past which the conversation is no longer live.
        self._warm_until = 0.0

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def _names(self) -> tuple[str, ...]:
        """The names to judge THIS utterance against, resolved now."""
        return resolve_names(self._names_source)

    def is_warm(self, now: float | None = None) -> bool:
        """Whether a conversation is currently live (a context-only turn is admissible)."""
        return self._resolve(now) < self._warm_until

    def turns(self, now: float | None = None) -> tuple[str, ...]:
        """The non-expired accepted turns, oldest first — the classifier's context.

        Expiry is applied here rather than at insert time so the decay is
        driven by the *decision's* clock, which keeps it deterministic under an
        injected clock and correct when the runtime has been idle.
        """
        moment = self._resolve(now)
        self._expire(moment)
        return tuple(text for _ts, text in self._history)

    # ------------------------------------------------------------------
    # The decision
    # ------------------------------------------------------------------

    def decide(self, text: str, now: float | None = None) -> GateVerdict:
        """Run the ladder for one utterance and return its decision and label.

        Never raises: a classifier fault becomes :data:`Decision.DEGRADE`.
        """
        moment = self._resolve(now)
        self._expire(moment)

        # 1. Name — outranks every other rule, costs nothing.
        if is_name_match(text, self._names(), self._name_threshold):
            self.note_engaged(text, moment)
            return GateVerdict(Decision.ENGAGE, LABEL_NAME)

        # 2. Short — a backchannel carries no addressing signal (#105).
        if len(_WORD_RE.findall(text)) < self._min_context_words:
            return GateVerdict(Decision.DROP, LABEL_SHORT)

        # 3. Cold — context-only engagement needs a conversation a name opened.
        if moment >= self._warm_until:
            return GateVerdict(Decision.DROP, LABEL_COLD)

        # 4. One classifier call against the surviving turns.
        if self._classifier is None:
            return GateVerdict(Decision.DEGRADE, LABEL_DEGRADE)
        decision = _judge_or_degrade(self._classifier, text, self.turns(moment))
        if decision is Decision.ENGAGE:
            self.note_engaged(text, moment)
            return GateVerdict(Decision.ENGAGE, LABEL_CONTEXT)
        if decision is Decision.DROP:
            return GateVerdict(Decision.DROP, LABEL_DROPPED)
        return GateVerdict(Decision.DEGRADE, LABEL_DEGRADE)

    def note_engaged(self, text: str, now: float | None = None) -> None:
        """Record an accepted turn: extend the conversation and add it to the context.

        :meth:`decide` calls this itself on its ENGAGE paths.  Callers call it
        for an accept THEY made — the DEGRADE fallback's heuristic — so a run of
        degraded turns cannot leave the gate permanently cold.
        """
        moment = self._resolve(now)
        self._warm_until = moment + self._warm_window_s
        if self._history_maxlen == 0:
            return
        self._history.append((moment, text))
        del self._history[: -self._history_maxlen]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _expire(self, now: float) -> None:
        """Drop turns older than the warm window — they are not "recent"."""
        cutoff = now - self._warm_window_s
        if self._history and self._history[0][0] <= cutoff:
            self._history = [entry for entry in self._history if entry[0] > cutoff]

    def _resolve(self, now: float | None) -> float:
        """This decision's clock reading — the caller's, or the injected clock."""
        if isinstance(now, (int, float)) and not isinstance(now, bool):
            return float(now)
        return float(self._clock())
