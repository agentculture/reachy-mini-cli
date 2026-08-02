"""Qwen's rolling summary of everything the foreground voice no longer replays.

Issue #154 decision c30, issue #155 task t12. The layer keeps ONE conversation
history with nested windows: the worker (Qwen) replays its full ``n`` turns,
the foreground voice (Gemma) replays only the last ``m`` — a strict suffix —
and everything older is covered by ONE summary. :mod:`reachy.embody.engine`
built the plumbing (storing it, bounding it, surfacing it, and
:data:`~reachy.embody.engine.STALE_SUMMARY_MARKER` when it cannot be kept
fresh). This module is the thing that WRITES it.

Why the producer lives here and not in the engine
--------------------------------------------------
The engine's own claim is that it reads no file and makes exactly one kind of
HTTP call through an injectable seam. A summary producer is a policy — WHEN to
fold, HOW MUCH to fold, what to ask for — and policies that live inside the
thing they govern cannot be swapped or tested in isolation. So this is a small
object with an injected ``summarize`` callable, a bounded prompt builder, and
one method a caller may poll.

ONE summary, never regenerated per lane (decision c30)
-------------------------------------------------------
There is exactly one production caller of
:meth:`~reachy.embody.engine.EmbodyTurnEngine.update_summary` in the repo, and
``tests/test_embody_summary.py`` pins that by AST. A second producer would be a
second summary, and the two lanes would then disagree about what was said —
the worst failure mode a robot with one voice can have, which is the same
argument that gave the layer one history rather than two.

The trigger is the conversation moving on, not a clock
--------------------------------------------------------
Polling is how the producer is DRIVEN (a background daemon thread, the pattern
:class:`reachy.cli._commands.agent._ClipAsker` already uses beside this layer),
but a poll only spends a gateway call when at least
:attr:`SummaryLimits.min_new_turns` turns have run since the last successful
pass AND there is a backlog to fold. A robot sitting quietly costs nothing; a
busy one refreshes roughly once per that many turns.

The bound that actually matters is the deque's. The shared history holds ``n``
turns and drops the oldest silently when it is full, so a summary that
refreshes more slowly than turns fall off the end loses them for good.
:data:`DEFAULT_MIN_NEW_TURNS` is sized well inside that margin (``n - m`` is 40
turns with the shipped bounds).

Every failure is NAMED, and the memory is never silently narrowed
-------------------------------------------------------------------
A dead worker gateway, an empty answer, an answer the engine refuses as
over-long — each resolves to
:meth:`~reachy.embody.engine.EmbodyTurnEngine.mark_summary_stale`, which keeps
the last known summary, prefixes it with the marker, and names a counted drop
(spec claim c45, honesty h30). Nothing here raises on the caller's thread, and
nothing latches off: a layer that goes permanently quiet because the gateway
blipped is indistinguishable from one that crashed, so the next poll tries
again — the engine's own stance, inherited deliberately.

Import boundary
----------------
Like the rest of ``reachy/embody/``: no ``reachy_mini``, no
:mod:`reachy.daemon`, no ``subprocess``, no shell. It reaches no LLM client of
its own either — the model call arrives as an injected callable, defaulting to
the engine's own ``senses``/``worker`` seam
(:meth:`~reachy.embody.engine.EmbodyTurnEngine.ask` with ``context=False``), so
this module has no endpoint, no timeout and no model name to drift from the
layer's.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from reachy import senselog

logger = logging.getLogger(__name__)

#: ``[SENSE stage=summary source=qwen event=<id>] …``. Distinct from the
#: engine's ``turn`` stage so one journal can be split by which pass did what;
#: the FAILURE line is the engine's own ``summary-stale`` drop, deliberately,
#: because that is the state a reader greps for.
STAGE = "summary"
SOURCE = "qwen"

#: Worker turns that must run before a poll spends a gateway call. Four is
#: roughly "two exchanges" at the conversational pace measured live
#: (``docs/evidence/2026-08-02-t14-live-acceptance.md``) and leaves an order of
#: magnitude of headroom against the 40-turn backlog the shipped ``m``/``n``
#: bounds allow before turns start falling off the deque.
DEFAULT_MIN_NEW_TURNS = 4

#: Seconds between polls when the producer drives itself on its own thread.
#: The trigger is turn COUNT, so this only bounds how late a refresh can be,
#: never how often one happens.
DEFAULT_POLL_INTERVAL_S = 30.0

#: Backlog turns folded into ONE prompt — the most recent this many. Bounded
#: for the reason every prompt in this layer is: the summary is the compaction
#: of an unbounded history, and a compaction pass whose own input is unbounded
#: is a slow leak with extra steps (issue #154). Older turns are already
#: covered by the PREVIOUS summary, which the prompt carries, so this bounds
#: the window of NEW material rather than the memory itself.
DEFAULT_MAX_BACKLOG_TURNS = 20

#: Characters of any one stored turn the prompt reproduces. A single turn's
#: user content is a whole perception block (triggers, background, already-said
#: lines), and a handful of those would dominate the request.
DEFAULT_MAX_TURN_CHARS = 400

#: The system message of the maintenance pass. It names the job and the shape;
#: the character bound is filled in per call from the engine's own
#: :attr:`~reachy.embody.engine.EmbodyTurnEngine.summary_max_chars`, never
#: restated here.
SUMMARY_SYSTEM_PROMPT = (
    "You maintain the running memory of a small desk robot's conversation. You "
    "are given the summary so far and the exchanges that have since scrolled out "
    "of the robot's short-term window. Rewrite the summary so it still covers "
    "everything that matters — who is present, what they asked for, what was "
    "decided, what is still open — in plain prose, past tense, third person. "
    "Keep what is still relevant, drop what is not, and never invent anything. "
    "Answer with the summary text and nothing else."
)


@dataclass(frozen=True)
class SummaryLimits:
    """When a maintenance pass runs, and how much it is allowed to chew on.

    The repo's rule: bounds live in a frozen dataclass whose ``DEFAULT_*``
    constants carry the reasoning (:class:`reachy.embody.engine.Limits` is the
    sibling). Every field carries its constant forward unchanged.
    """

    min_new_turns: int = DEFAULT_MIN_NEW_TURNS
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    max_backlog_turns: int = DEFAULT_MAX_BACKLOG_TURNS
    max_turn_chars: int = DEFAULT_MAX_TURN_CHARS


def _clip(text: str, limit: int) -> str:
    """One stored turn, shortened for the PROMPT only.

    Truncation is legitimate here and nowhere else in this arc: this is the
    request, not the record. Every artifact the layer KEEPS is refused rather
    than trimmed, because a trimmed artifact misstates what was meant; a
    trimmed prompt line only costs the model some detail, and it is marked so
    the model knows it was shortened.
    """
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}…"


def build_summary_prompt(
    *,
    backlog: Sequence[tuple[str, str]],
    previous: str,
    max_chars: int,
    limits: SummaryLimits | None = None,
) -> str:
    """The user message of one maintenance pass. A pure function.

    Carries the previous summary (so the summary ROLLS rather than being
    rewritten from whatever happens to be in the backlog), the most recent
    :attr:`SummaryLimits.max_backlog_turns` exchanges that have left the
    foreground window, and the character bound the engine will hold the answer
    to — asked for explicitly, because a producer that asks for a summary the
    engine then refuses has spent a gateway call to make the summary stale.
    """
    bounds = limits if limits is not None else SummaryLimits()
    recent = list(backlog)[-max(1, int(bounds.max_backlog_turns)) :]
    lines = [
        "Summary so far:",
        previous.strip() or "(nothing summarised yet — this is the first pass)",
        "",
        "Exchanges that have since left the robot's short-term window:",
    ]
    for perceived, replied in recent:
        lines.append(f"- it perceived: {_clip(perceived, bounds.max_turn_chars)}")
        if replied.strip():
            lines.append(f"  it thought: {_clip(replied, bounds.max_turn_chars)}")
    lines += [
        "",
        f"Write the updated summary in at most {int(max_chars)} characters.",
    ]
    return "\n".join(lines)


class SummaryProducer:
    """Folds turns older than the foreground window into ONE rolling summary.

    Args:
        engine: the layer's :class:`~reachy.embody.engine.EmbodyTurnEngine`.
            Duck-typed — this module only ever calls ``backlog()``,
            ``update_summary``/``mark_summary_stale`` and reads ``turns`` /
            ``summary`` / ``summary_max_chars`` — so a test drives it with a
            handful of attributes and no gateway.
        summarize: ``summarize(prompt) -> str``, the model call. Defaults to
            the engine's own streaming seam on the WORKER lane with
            ``context=False``: the summary is Qwen's job, and the pass must not
            be shown the layer's usual context (see
            :meth:`~reachy.embody.engine.EmbodyTurnEngine.ask`).
        limits: when a pass runs and how much it folds.
        sleep: what :meth:`run` sleeps with between polls (default
            :func:`time.sleep`), so a test drives the loop without waiting.
    """

    def __init__(
        self,
        engine: object,
        *,
        summarize: Callable[[str], str] | None = None,
        limits: SummaryLimits | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._engine = engine
        self._summarize = summarize if summarize is not None else self._default_summarize
        self._limits = limits if limits is not None else SummaryLimits()
        self._sleep = sleep if sleep is not None else time.sleep
        self._min_new_turns = max(1, int(self._limits.min_new_turns))
        self._last_turn = int(getattr(engine, "turns", 0) or 0)
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        #: Successful refreshes and failed passes, counted apart: "the worker
        #: is unreachable" and "there was nothing to do" must not look alike on
        #: a status line.
        self.updates = 0
        self.failures = 0

    # ------------------------------------------------------------------ #
    # One pass                                                           #
    # ------------------------------------------------------------------ #

    def poll_once(self) -> bool:
        """Refresh the summary if the conversation has moved on. Never raises.

        Returns whether a maintenance pass was ATTEMPTED — ``False`` means
        there was nothing worth folding yet, which is the ordinary outcome and
        deliberately not a drop: a quiet robot is not a fault.

        A pass that is attempted always ends in one of two named states, never
        in silence: :meth:`~reachy.embody.engine.EmbodyTurnEngine.
        update_summary` accepted the text, or
        :meth:`~reachy.embody.engine.EmbodyTurnEngine.mark_summary_stale` said
        why it did not.
        """
        try:
            backlog = list(self._engine.backlog())
            turns = int(getattr(self._engine, "turns", 0) or 0)
        except Exception as err:  # a sick engine must not kill the producer
            logger.warning("[embody] summary producer could not read the history: %s", err)
            return False
        if not backlog or turns - self._last_turn < self._min_new_turns:
            return False

        self._last_turn = turns
        prompt = build_summary_prompt(
            backlog=backlog,
            previous=str(getattr(self._engine, "summary", "") or ""),
            max_chars=int(getattr(self._engine, "summary_max_chars", 0) or 0),
            limits=self._limits,
        )
        try:
            answer = (self._summarize(prompt) or "").strip()
        except Exception as err:  # every gateway fault is NAMED, never raw
            self._fail(f"{type(err).__name__}: {err}")
            return True
        if not answer:
            self._fail("the worker lane returned no summary text")
            return True
        if not self._engine.update_summary(answer):
            # ``update_summary`` has already named ITS refusal (over-length, or
            # blank); this adds the state that refusal leaves the layer in.
            self._fail("the engine refused the summary text")
            return True

        self.updates += 1
        senselog.stage(
            STAGE,
            SOURCE,
            f"{turns}",
            f"summary refreshed from {len(backlog)} backlog turns ({len(answer)} chars)",
        )
        return True

    def _fail(self, detail: str) -> None:
        self.failures += 1
        self._engine.mark_summary_stale(detail)

    def _default_summarize(self, prompt: str) -> str:
        """The production model call: the WORKER lane, with no layer context."""
        return self._engine.ask(
            prompt, role=_worker_role(), system=SUMMARY_SYSTEM_PROMPT, context=False
        )

    # ------------------------------------------------------------------ #
    # The thin loop                                                      #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the background poll thread. Idempotent.

        Daemon, and off the turn thread on purpose: a maintenance pass is a
        whole gateway round trip, and charging that to the loop that answers a
        person in the room would make the robot pause mid-conversation to tidy
        its memory.
        """
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.run, name=THREAD_NAME, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Ask the loop to finish after the poll it is on. Never blocks."""
        self._stop.set()

    def run(self) -> None:
        """Poll until stopped. Every fault is caught; the loop outlives them."""
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(max(0.0, float(self._limits.poll_interval_s)))


#: The producer's thread name, matching the layer's other background threads
#: (``embody-cue-reader`` / ``embody-clip-asker``) so a stack dump names them.
THREAD_NAME = "embody-summary"


def _worker_role() -> str:
    """The engine's own worker-role name, imported lazily to avoid a cycle."""
    from reachy.embody.engine import ROLE_WORKER

    return ROLE_WORKER
