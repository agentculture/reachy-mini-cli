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
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence

from reachy.speech import llm
from reachy.speech.name_match import DEFAULT_THRESHOLD, is_name_match

#: Canonical names the robot answers to.  Mirrors the listen/transcribe default.
DEFAULT_NAMES: tuple[str, ...] = ("reachy", "robot")

#: Tight, bounded default timeout for a single classifier call (seconds).  A
#: classifier sits in the perception hot-loop, so a slow/dead endpoint must fail
#: fast and degrade rather than stall the loop.  Shorter than ``llm.complete``'s
#: own 10 s default.
DEFAULT_CLASSIFIER_TIMEOUT: float = 5.0

#: System prompt for the engagement classifier.
#:
#: Parked as a tunable follow-up (issue #55): the exact wording is a single
#: module-level constant so it can be tuned in one place without touching the
#: control flow.  The contract it must keep is the addressed-vs-helpable
#: distinction and the strict ``YES``/``NO`` answer shape that
#: :meth:`EngagementClassifier._parse` depends on.
ENGAGEMENT_SYSTEM_PROMPT: str = (
    "You decide whether a spoken utterance is addressed to a small desk robot "
    "named Reachy, given the recent conversation. Engage only if the speaker is "
    "talking TO the robot or clearly continuing a conversation with it — NOT if "
    "two people are talking to each other, even about something the robot could "
    "help with. Being helpable is not the same as being addressed: do not engage "
    "just because the robot could assist. Answer with exactly YES or NO."
)


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
        complete_fn: Callable[..., str] = llm.complete,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_CLASSIFIER_TIMEOUT,
        system_prompt: str = ENGAGEMENT_SYSTEM_PROMPT,
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
        system_prompt:
            The classifier instruction.  Defaults to
            :data:`ENGAGEMENT_SYSTEM_PROMPT`; override to tune behaviour.
        """
        self._complete_fn = complete_fn
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout
        self._system_prompt = system_prompt

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
            {"role": "system", "content": self._system_prompt},
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
    names: Sequence[str] = DEFAULT_NAMES,
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
        Canonical names for the fast-path.  Defaults to :data:`DEFAULT_NAMES`.
    name_threshold:
        Fuzzy-match threshold handed to :func:`is_name_match`.  Defaults to the
        name-matcher's own :data:`~reachy.speech.name_match.DEFAULT_THRESHOLD`.

    Returns
    -------
    Decision
        ``ENGAGE`` / ``DROP`` / ``DEGRADE`` (see :class:`Decision`).
    """
    # 1. Name fast-path — short-circuit, no classifier call.
    if is_name_match(text, names, name_threshold):
        return Decision.ENGAGE

    # 2. Single classifier call; 3. any failure degrades.
    try:
        verdict = classifier.judge(text, context)
    except Exception:  # noqa: BLE001 — any failure means "classifier unavailable"
        return Decision.DEGRADE
    return Decision.ENGAGE if verdict else Decision.DROP
