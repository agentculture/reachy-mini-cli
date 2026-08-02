"""Two-state attention: what it takes to wake the layer's mind, and to keep it awake.

The layer's EAR is ungated — the duplex session surfaces every voice in the
room, including the ones the runtime's own engagement gate would drop, and
``tests/test_realtime_duplex.py`` pins that three ways. This module is the
decision that sits *after* it: an utterance arriving is not the same as an
utterance worth waking a thinking mind for. Measured on a real conversation on
2026-08-02, **6 operator utterances produced 49 turns**; task t7 closed the
runtime-cue half of that flood, and this closes the other half.

The model is deliberately the smallest thing that answers the operator's
request — "'reachy' wakes it up, but then it has a time period from each of its
answers where it still listens" (issue #148):

============  ==========================================  ====================
state         what is admitted                            how it ends
============  ==========================================  ====================
**cold**      only an utterance that NAMES the robot      —
**warm**      any utterance                               nothing admitted and
                                                          nothing spoken for
                                                          ``window_s``
============  ==========================================  ====================

Cited from the runtime's gate, and where it deliberately stops
---------------------------------------------------------------
:mod:`reachy.speech.engagement` is the runtime hearing leg's version of this
and its rules 1-3 are exactly this shape — a name fast-path that opens the
conversation, a warm window only a name can open from cold, and a NAMED label
per outcome used verbatim as the ``senselog.drop`` reason. Two of its parts are
knowingly left behind:

* **its LLM classifier.** This gate reaches only
  :func:`reachy.speech.name_match.is_name_match`, which is pure ``difflib`` +
  ``re`` — no model, no network, no timeout to degrade from. Importing
  ``engagement`` would put an LLM edge in the layer and would also widen the
  importer set ``tests/test_zero_llm_boundary.py`` pins BY EQUALITY; that is a
  separate decision and must not ride along on a wake-word feature. The layer
  already has a mind one call away — a second model judging whether to wake the
  first one is a cost this does not need.
* **its short-utterance rule.** That rule exists because the runtime's gate
  admits nameless utterances into a warm conversation *on a classifier's word*;
  here a warm window is opened only by a name the human deliberately said, and
  a two-word reply ("go on") is exactly the thing a spoken conversation is made
  of.

The name matcher's own guards are load-bearing and are NOT re-derived here:
``is_name_match`` scores ``difflib_ratio × length_ratio`` behind four
structural guards including a Soundex phonetic guard (issue #104) that closed a
family of everyday ``r``-words — ``really``, ``reality``, ``ready``, ``reason``,
``record``, ``room``, ``route``, ``robust`` — which the orthographic guards
alone let through. A new collision belongs in
``tests/test_name_match.py``'s ``_COLLISION_TABLE``, never in a stoplist here.

Why the rules are structural, and why speech cannot OPEN the window
--------------------------------------------------------------------
``engagement.py`` records what happens when this state is advisory rather than
control flow: an accept-only history was a **one-way ratchet** — a single false
accept planted a six-turn context and every accept re-seeded it — measured live
at 199 correct drops and **39 accepts, all wrong**, against a model that had
said YES 36 times out of 36. So every rule here changes control flow and is
provable by a test.

That matters more here than there, because this layer has a second ratchet door
the runtime never had: its duplex session is armed once and the SERVER answers
every committed utterance out loud ("every committed turn on this session gets
a spoken reply" — ``docs/evidence/2026-08-02-t14-live-acceptance.md``). So
:meth:`AttentionGate.note_spoken` fires for replies to the very chatter this
gate refused. If speaking could open attention, a robot in a talkative room
would hold its own ear open with its own voice. Hence the asymmetry, which is
the one thing to keep intact if this file is ever edited:

    a NAME opens. Being heard while warm, and speaking while warm, EXTEND.
    Nothing else opens.

Threading
---------
Not thread-safe, and does not need to be, for the same reason
:class:`~reachy.speech.engagement.ConversationGate` is not: both callers (the
duplex session's ``on_utterance`` and ``on_response`` taps) run on that
session's single worker thread, and the whole mutable state is one float, whose
store is atomic under the GIL. A reader on another thread sees a slightly stale
deadline at worst, never a torn one.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from reachy.speech.name_match import DEFAULT_THRESHOLD, is_name_match

# --------------------------------------------------------------------------- #
# Defaults                                                                    #
# --------------------------------------------------------------------------- #

#: Names the robot answers to. Duplicated from
#: :data:`reachy.speech.engagement.DEFAULT_NAMES` rather than imported — that
#: module carries the LLM classifier this gate deliberately does not reach —
#: and ``tests/test_embody_attention.py`` pins the two tuples equal so the
#: duplication cannot drift into a robot that answers to different names
#: depending on which of its two ears heard you.
DEFAULT_NAMES: tuple[str, ...] = ("reachy", "robot")

#: How long attention stays open after the last thing heard or said, in seconds.
#:
#: Longer than the runtime's 20 s ``engage_window_s``, and the difference is not
#: a preference — the two windows cover different things. The runtime's window
#: spans TEXT arriving from a continuous transcript stream; this one has to span
#: a whole SPOKEN exchange, and most of that exchange is time the transcript
#: path never pays for:
#:
#: * the robot's own turn — measured live at up to **six** streamed rounds for
#:   a single rule fire (``docs/evidence/2026-08-02-t14-live-acceptance.md``),
#:   each round a full completion plus its tool dispatches;
#: * the answer itself — one measured spoken reply was **2.8 s** of audio, which
#:   is a seventh of a 20 s window spent before the human has even heard it;
#: * the human listening to that answer, thinking, and speaking a reply out
#:   loud.
#:
#: 45 s is a bit over 2x the runtime's window: long enough that a natural pause
#: mid-exchange ("hang on, let me think") does not silently end the
#: conversation, short enough that a room going quiet is back to name-only
#: inside a minute, which is what "for a while" meant in the request. The cost
#: of each direction is asymmetric and worth stating: too short and the robot
#: ignores someone who is plainly talking to it (the complaint this fixes); too
#: long and it answers a conversation it is not part of, which is bounded by
#: the fact that it only ever costs turns while somebody is actually speaking.
DEFAULT_ATTENTION_WINDOW_S: float = 45.0

# --------------------------------------------------------------------------- #
# Named outcomes — the label IS the senselog reason, never a paraphrase        #
# --------------------------------------------------------------------------- #

#: Admitted because the utterance named the robot. Opens the window from cold.
LABEL_NAME = "name"
#: Admitted because a conversation is still live. The same word
#: ``engagement.py`` uses for the same idea (there, a classifier's judgement
#: that the utterance continues the conversation; here, the window itself),
#: so one journal grep finds continuation turns on both hearing paths.
LABEL_CONTEXT = "context"
#: Refused: nothing named the robot and no conversation is live. Shares the
#: ``not-addressed`` prefix with the runtime's three drop labels, so the grep an
#: operator already knows finds this one too.
LABEL_COLD = "not-addressed-cold"

#: Every outcome this module can name, in one place — the same discipline
#: :mod:`reachy.embody.engine` keeps for its own drop reasons.
ATTENTION_LABELS: tuple[str, ...] = (LABEL_NAME, LABEL_CONTEXT, LABEL_COLD)


@dataclass(frozen=True)
class AttentionVerdict:
    """One :meth:`AttentionGate.decide` outcome.

    Attributes:
        admitted: whether the utterance may wake a turn.
        label: the NAMED outcome, used verbatim as the ``senselog`` reason.
        opened: whether this verdict took attention from cold to warm. The
            caller logs that transition and nothing else, because it is the one
            an operator is ever asked about ("why is it ignoring me?" / "why is
            it answering everything?").
    """

    admitted: bool
    label: str
    opened: bool = False


class AttentionGate:
    """The two-state gate itself: cold until named, warm until quiet.

    Args:
        window_s: how long attention stays open after the last admitted
            utterance or spoken answer. ``0`` means name-only forever — the
            same convention :attr:`reachy.embody.engine.Limits.
            min_alert_interval_s` uses for ``0``, and a useful setting for a
            room where the robot should answer only when addressed by name.
        names: the canonical names, passed straight to
            :func:`~reachy.speech.name_match.is_name_match`.
        name_threshold: the matcher's fuzzy-similarity threshold. Left at the
            matcher's own default; it was measured against a real accept/reject
            table and is not this module's number to retune.
        clock: the monotonic clock. Injected, like every other cadence in this
            codebase, so a 45 s window is testable without waiting 45 s — the
            composition passes the engine's own ``now_fn`` so the layer has ONE
            clock.
    """

    def __init__(
        self,
        *,
        window_s: float = DEFAULT_ATTENTION_WINDOW_S,
        names: Sequence[str] = DEFAULT_NAMES,
        name_threshold: float = DEFAULT_THRESHOLD,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_s = max(0.0, float(window_s))
        self._names = tuple(names)
        self._name_threshold = float(name_threshold)
        self._clock = clock
        # -inf, never 0.0: an injected clock may start anywhere, and a gate that
        # began life spuriously warm because the clock reads 0.0 would admit the
        # first ambient utterance of the process.
        self._warm_until = float("-inf")

    # ------------------------------------------------------------------ #
    # Queries                                                            #
    # ------------------------------------------------------------------ #

    @property
    def window_s(self) -> float:
        """The configured window, in seconds."""
        return self._window_s

    def is_warm(self, now: float | None = None) -> bool:
        """Whether a conversation is currently live (a nameless utterance lands)."""
        return self._resolve(now) < self._warm_until

    # ------------------------------------------------------------------ #
    # The decision                                                       #
    # ------------------------------------------------------------------ #

    def decide(self, text: str, now: float | None = None) -> AttentionVerdict:
        """Judge one heard utterance. Never raises, never calls anything remote.

        Cheapest-first, and both rules are control flow rather than advice:

        1. the utterance NAMES the robot -> admit, and open (or extend) the
           window;
        2. the window is open -> admit, and extend it;
        3. otherwise -> refuse, named :data:`LABEL_COLD`, and change **no**
           state. A refused utterance must not extend anything, or ambient
           chatter would hold the ear open by being refused often enough.
        """
        moment = self._resolve(now)
        was_warm = moment < self._warm_until

        if is_name_match(text, self._names, self._name_threshold):
            self._warm_until = moment + self._window_s
            return AttentionVerdict(True, LABEL_NAME, opened=not was_warm)
        if was_warm:
            self._warm_until = moment + self._window_s
            return AttentionVerdict(True, LABEL_CONTEXT)
        return AttentionVerdict(False, LABEL_COLD)

    def note_addressed(self, now: float | None = None) -> None:
        """Open (or extend) the window explicitly.

        :meth:`decide` calls this path itself on both admitting rules. A caller
        uses it to hand the gate an accept it made on its own — the seam the
        runtime's :meth:`~reachy.speech.engagement.ConversationGate.note_engaged`
        keeps for the same reason.
        """
        self._warm_until = self._resolve(now) + self._window_s

    def note_spoken(self, now: float | None = None) -> bool:
        """The layer answered out loud. EXTENDS a live window; never opens one.

        Returns whether it actually extended anything, so the caller can tell a
        conversation being kept alive from the robot talking into a room it is
        not part of. The asymmetry is the whole point — see the module
        docstring's second ratchet door: the duplex server replies to ambient
        utterances this gate has already refused, so a voice that could open
        attention would be a robot waking itself up.
        """
        moment = self._resolve(now)
        if moment >= self._warm_until:
            return False
        self._warm_until = moment + self._window_s
        return True

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _resolve(self, now: float | None) -> float:
        """This decision's clock reading — the caller's, or the injected clock."""
        if isinstance(now, (int, float)) and not isinstance(now, bool):
            return float(now)
        return float(self._clock())
