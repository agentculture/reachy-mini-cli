"""The embodiment layer's cognition loop: cues + utterances in, streamed turns out.

This is the layer's MIND. It sits between two things that already exist: the
perception side (:mod:`reachy.embody.cues` mapping the runtime's own exported
events to cue text, and :class:`~reachy.speech.realtime_duplex.
RealtimeDuplexSession` handing over what it heard) and the action side
(:class:`~reachy.embody.tools.EmbodyToolRegistry`, the closed five-tool set).
It contributes exactly one thing of its own: a streaming
``/v1/chat/completions`` turn loop that turns the former into the latter.

Cue-triggered, not polled
-------------------------
:class:`~reachy.speech.agent_turn.AgentTurnEngine` is the closest relative and
several seams are cited from it verbatim (the bounded rolling history, the
``run(max_turns=, stop=, before_turn=)`` loop shape, the export-before-dispatch
ordering, the ``max_tool_rounds`` bound). One thing is deliberately NOT
inherited:

* **No permanent failure latch.** That engine mutes its audio sink for the
  process lifetime after a streak of failures. This one runs beside a robot
  that is meant to stay switched on: every failure is a named, counted drop and
  the very next turn tries again. A layer that goes permanently quiet because
  the gateway blipped is indistinguishable from a layer that crashed.

Three input classes, not one queue (issue #143)
-----------------------------------------------
A turn is TRIGGERED — that is what makes "the robot's own rule fired, so it
says something about it" possible at all — but not by everything that arrives.
Measured live on 2026-08-02 with the bus bridged into the feed: **187 cues in
~40 s produced 23 turns and 19 queue-full drops**, and not one of those turns
was prompted by something the robot DECIDED. The mix was 145 "speech from the
left/ahead/right" plus 44 "loud sound", zero rule fires. The layer already has
its own ears (a ``/v1/realtime`` duplex session with server-side VAD), so
those cues told it nothing it did not already hear — they simply arrived at
tick rate instead of utterance rate. So intake splits three ways:

============  ==========================================  =================
class         events                                      effect
============  ==========================================  =================
heard         an utterance from the duplex session        runs a turn, if
                                                          ATTENTION admits it
**alert**     a rule FIRE                                 runs a turn
**context**   ``sense`` / ``intent`` / ``motion``, and a   parked, drained
              rule SUPPRESSION                            by the next turn
============  ==========================================  =================

The "if attention admits it" qualifier is issue #148, and it is the one clause
of this policy that is about WHO rather than about WHAT. The ear stays ungated
— the duplex session surfaces every voice in the room — but a turn is woken
only by an utterance that named the robot, or by one arriving while a
conversation the name opened is still live. The whole rule lives in
:mod:`reachy.embody.attention`; what matters here is that it gates the HEARD
class alone. An alert is the robot's own reflex firing: attention gates the
ear, never the robot's reactions, so a rule fire runs a turn from cold.

The context half is where :class:`~reachy.speech.agent_turn.AgentTurnEngine`'s
snapshot-only buffer is CITED rather than imported: cues that arrive during a
turn accumulate for the next one and cause none of their own. What is added on
top is COALESCING — 145 near-identical lines must reach a turn as one fact
carrying a count, not as 145 strings — keyed on the rendered cue text, which
is exactly the identity the closed cue vocabulary already expresses (see
:mod:`reachy.runtime_cues`: a fixed phrase per perception, so equal text means
the same fact happened again).

A second coalescing key, because free text has no fixed phrase (issue #154)
-----------------------------------------------------------------------------
Text-identity coalescing is exactly right for the closed cue vocabulary and
exactly wrong for anything else: "a kitchen with someone at the counter" and
"a kitchen, a person near the counter" describe the same fact and share no
key, so a caller feeding free-form perception text (the senses lane's
:meth:`ask`, polled roughly every 20 s by the clip-asking caller) through
:meth:`submit_cue` fills :data:`DEFAULT_MAX_CONTEXT` with near-duplicate
sightings inside minutes and starts refusing genuine runtime facts — the
cheapest, most repetitive signal evicting the most valuable one.
:meth:`submit_perception` is the escape hatch, not a retuning of the same
dict: a SEPARATELY bounded park keyed on SOURCE rather than text, where a new
description REPLACES the one already there instead of adding beside it — a
:class:`PerceptionSlot` describes a STATE ("what the camera currently
shows"), not a growing log of past sightings, so one slot per source is
correct even after an hour of updates. The replacement is never silent: the
slot's own ``count`` keeps incrementing on every update, so it still adds its
share to the turn's ``coalesced-from`` total, and its rendering marks an
update differently from a repeat (``"... (updated x180)"`` against a cue's
``"... (x145)"``) so a reader of the journal or export feed can tell which
happened.

A state PERSISTS, and a structured artifact has a shape (issue #155 c7, task
t13)
------------------------------------------------------------------------------
Two things t3 deliberately left open, both closed here. First, the shape:
:meth:`submit_perception` now takes a :class:`PerceptionSnapshot` — the
observation summary, salient entities, a confidence, a capture time and a
frame reference issue #155 names — rather than bare text; the producer
(:class:`~reachy.cli._commands.agent._ClipAsker`) builds one from the senses
model's answer and degrades to a summary-only snapshot, never a crash, when
that answer does not parse as the requested structure. A bare string is still
accepted (wrapped into a summary-only snapshot at intake) so every existing
caller keeps working unchanged.

Second, the lifetime: "one slot, replaced on every call" is the coalescing
key, not the whole story, and t3's own :meth:`_drain_context` DRAINED that
slot exactly like every cue — read once, gone. A :class:`PerceptionSlot`
describes a STATE, and a state does not stop being true the instant one turn
looks at it: a person asking "what can you see?" between two 20 s clip polls
used to get nothing, because the last answer had already been read and
thrown away by whatever turn happened to run first. :meth:`_live_perception`
is the fix — a slot now PERSISTS across turns until SUPERSEDED (a later
:meth:`submit_perception` call for the same source) or STALE (its
snapshot's ``captured_at`` is older than
:attr:`Limits.perception_stale_after_s`, checked on every read against
:attr:`_now` — the SAME clock the alert interval and the attention gate
already share, never a second one). This is why perception is a SEPARATE
park from the closed cue vocabulary in the first place: changing ONE park's
lifetime semantics without touching the other would not be possible if they
shared a dict, and the closed-vocabulary park's drain-every-turn behaviour is
exactly as load-bearing as it always was (see the section above) — a cue
describes something that HAPPENED, and a happening does not stay true.

Freshness reuses the clip discipline instead of inventing a second one. The
production ``captured_at`` is the runtime clip's own monotonic ``ts``
(``reachy/behavior/clip_rider.py``), carried straight through by
``_ClipAsker`` rather than re-stamped at submission time — so a snapshot that
sat unread is judged by the true age of the FRAME it describes, not by how
recently the layer happened to hear about it. :data:`DEFAULT_PERCEPTION_
STALE_AFTER_S` mirrors :data:`reachy.cli._commands.agent.DEFAULT_CLIP_
STALE_AFTER_S` (30.0) BY VALUE, not by import — the same reason
:data:`DEFAULT_ATTENTION_WINDOW_S` is independently defined on each side of
the composition-root boundary — because the two really are the SAME rule
evaluated at two different moments (once at ask time, in ``_ClipAsker.
poll_once``; again at read time, here), and a caller that widened one without
the other would have quietly invented a second staleness policy.

Alert containment, because the flood has a front door too
----------------------------------------------------------
``reachy/behavior/rules.py`` permits ``cooldown_s = 0`` and several rules can
fire in one tick, so "only alerts trigger" alone would let the same flood back
in wearing the one class that is allowed through. Two bounds close it, and
both are about turns rather than about cues:

* alerts arriving while a turn is pending or running COALESCE into the ONE
  turn that drains them next — the trigger buffer is drained whole, so ten
  fires inside one turn window cost a second turn, never ten;
* :data:`DEFAULT_MIN_ALERT_INTERVAL_S` bounds how often an alert may TRIGGER.
  Inside the interval an alert is DEFERRED, never dropped: it stays pending
  and rides the next turn that runs. An utterance is exempt — a person talking
  outranks a rate limit — and the first alert after quiet is never delayed,
  because the interval is measured from the last alert-triggered turn.

Both bounds are observable by construction: every turn's senselog line and its
exported ``thinking`` block carry ``triggers=T context=N coalesced-from=M``. A
silent coalescer is indistinguishable from a dropper.

Every LLM call streams (spec claim c6), and the reason is measured
------------------------------------------------------------------
Both lanes — the tool-bearing turn (:meth:`run_turn`, the ``worker`` model) and
the tool-less perception question (:meth:`ask`, the ``senses`` model) — go
through :func:`reachy.speech.llm.stream_turn` with ``stream=true``. Non-streaming
was not a style choice to reject: with thinking enabled, our own gateway took
**43.2 s** to the first content delta while the largest gap BETWEEN chunks was
**0.124 s** (``docs/evidence/2026-08-01-cited-findings-from-embodiment-
sibling.md``). A total deadline that survives the former is uselessly long for
detecting the latter.

Which is exactly why honesty condition h6 — "a stalled stream resolves as a
named timeout drop, never a hang" — is armed on **inter-chunk idle**, never on
total elapsed. The mechanism is that ``urlopen``'s timeout becomes the SOCKET
timeout, so it applies per read: a stream that keeps producing is never killed
however long the whole turn takes, and one that goes quiet is named
(:data:`REASON_STREAM_IDLE`) within one idle budget. Arm it on total elapsed and
every long think dies looking like a broken model.
:data:`DEFAULT_IDLE_TIMEOUT_S` is generous for one reason: the FIRST read also
covers time-to-first-token, and the gateway lazy-loads the worker model.

The model is a per-request field, from process env only
-------------------------------------------------------
:class:`EmbodyModels` resolves ``worker`` and ``senses`` from
:data:`ENV_WORKER_MODEL` / :data:`ENV_SENSES_MODEL` (defaulting to the ROLE
names, which lobes' ``resolve_model`` accepts), and the chosen name travels as
the request body's ``model`` field — one per call. It reads no file and writes
no variable, and that is a requirement rather than an implementation detail: an
``environment.d`` drop-in would re-point the RUNTIME's engagement classifier
too, silently changing the reflex robot while configuring the layer.
``tests/test_embody_engine.py`` proves both halves, the second by AST.

Nested windows onto ONE history, and Qwen's summary (issue #154 decision c30)
-------------------------------------------------------------------------------
:attr:`Limits.history_maxlen` (``n``) and :attr:`Limits.senses_history_maxlen`
(``m``) bound the SAME conversation ``deque`` — never two histories. The
worker (:meth:`run_turn`, via :meth:`_build_messages`) replays its FULL
``n``-turn window; Gemma (:meth:`ask`, via :meth:`_senses_window`) replays only
its last ``m`` turns, taken as a plain tail slice of that one deque — a STRICT
SUFFIX of what the worker sees, never a second, independently-maintained copy.
Constructing a :class:`Limits` with ``senses_history_maxlen > history_maxlen``
is refused (:meth:`Limits.__post_init__`), fail-closed: asking Gemma to see
MORE turns than the shared history even keeps for Qwen is a configuration
error, not a request this module can satisfy by clamping.

Why one history rather than two: a second, independently-maintained history
for Gemma would drift from the worker's, and the two models would then
disagree about what was said — the worst failure mode a robot with one voice
can have (operator decision, issue #154).

The measured cost of the ``m`` window is small, not zero
(``docs/evidence/2026-08-02-t1-media-chunk-budget.md``, task t1): twenty turns
of ordinary spoken exchange cost **401 prompt tokens** against **2 399** for
one clip ask — about +16% on a clip question, a correction to the "nearly
free" the initial scope pass assumed (that comparison was in BYTES, where the
clip really is 827× larger; in TOKENS, what actually fills the window, it is
6×). ``m=20`` nested inside ``n=60`` (:data:`DEFAULT_SENSES_HISTORY_MAXLEN` /
:data:`DEFAULT_HISTORY_MAXLEN`) is sized against that measurement, not a round
number.

Everything older than Gemma's ``m``-turn window is covered by ONE
Qwen-maintained summary (:meth:`update_summary`), never regenerated per lane
and never computed by this module — Qwen owns WRITING it
(:class:`reachy.embody.summary.SummaryProducer`, the one production caller of
:meth:`update_summary` and :meth:`mark_summary_stale`, reading the turns
:meth:`backlog` reports); this module owns only the plumbing: storing it,
bounding it
(:attr:`Limits.summary_max_chars`, refused rather than truncated when
over-length, the same fail-closed idiom :data:`reachy.behavior.rules.
MAX_SAY_CHARS` uses — "a compaction that can grow without limit is a slow leak
with extra steps", issue #154), and surfacing it to :meth:`ask`. If Qwen's
maintenance pass fails (the worker LLM is unreachable, or the text it returns
is itself refused), Gemma's context keeps whatever summary text it last had
but PREFIXES it with :data:`STALE_SUMMARY_MARKER`, and the failure is a named,
counted drop (:data:`REASON_SUMMARY_STALE`) — never a silent narrowing of
Gemma's memory down to just the last ``m`` turns (spec claim c45, honesty
h30). The marker clears on the next :meth:`update_summary` call that
succeeds.

The THIRD reader: the floor's history is what the layer put there (c27)
--------------------------------------------------------------------------
Decision **c27** settled who owns the conversation record. The layer does: it
already receives every utterance and every reply text over the duplex wire, so
lobes' server-side history becomes a PROJECTION of the deque above rather than
a second account of the same conversation — the two-histories drift issue #154
warned about, arriving one level down. Two methods produce those projections,
both returning :class:`FloorItem`\\ s the composition root joins to
:class:`reachy.speech.realtime_duplex.ConversationItem`:

* :meth:`floor_reseed` — what a NEW session must be told, consulted by the
  duplex client inside ``session.created`` handling and BEFORE it arms (spec
  claim c40; a session close wipes the floor's ephemeral history, so a
  reconnect that armed first would answer out of an empty one). It is Gemma's
  ``m``-window as curated HISTORY turns plus Qwen's summary as ONE ephemeral
  CONTEXT item, taken through :meth:`_senses_window` and
  :meth:`_history_messages` — the same views the two lanes already read, never
  a third derivation and never a cached copy.
* :meth:`floor_correction` — what the room ACTUALLY heard of a reply a human
  cut off, which the floor cannot see for itself (spec claim c39).

Both are bounded by bounds that already exist
(:attr:`Limits.senses_history_maxlen`, :attr:`Limits.summary_max_chars`), and
both are refused wholesale by a gateway that announced no conversation-item
support — one named drop, the connect-time ``system_prompt`` context task t9
wired, and the phase-1 overstatement documented rather than papered over.

Cognition scopes: what the background mind may put in front of the voice
--------------------------------------------------------------------------
Qwen influences the conversation only through explicit, inspectable typed
events (spec claim c2), and :mod:`reachy.embody.scope` is the type: a compact,
attributed, expiring :class:`~reachy.embody.scope.CognitionScope` — a goal, the
facts that matter, a suggested next step, a priority, an expiry in turns and a
speakable flag. **Never raw model reasoning** (claim c8). :meth:`submit_scope`
parks one, latest-wins on ``(kind, goal)``; :meth:`ask` — the FOREGROUND lane —
renders the live ones into one system message beside Qwen's summary, and Gemma
keeps the wording and the decision to speak.

Two properties are structural rather than conventional, and both mirror
guarantees this module already carries elsewhere. A scope is **context, never a
trigger**: :meth:`submit_scope` has no class parameter, exactly as
:meth:`submit_perception` has none, so the robot cannot wake itself up to act
on its own background thought. And a scope **expires in TURNS**, filtered on
read like :attr:`wanted_to_say`, so a stale scope cannot shape a later turn —
the turn counter is the only clock this record has.

:meth:`note_interjection` is where the sibling family meets this one: an
ADMITTED :class:`reachy.embody.interjection.Interjection` becomes a
``speakable`` scope (:func:`reachy.embody.scope.scope_from_interjection`).
Its ``alert`` flag is the ONE difference between the two admission routes — an
interjection that arrived over the wire is worth waking the mind for (t5's
``ADMITTED_CUE_CLASS``), while the worker's own tool call is not: a mind woken
by its own proposal is a mind talking to itself, the same defect
:meth:`note_spoken` avoids one buffer over.

Said, unsaid, and the sentence the room never got (issue #151, spec c34)
-------------------------------------------------------------------------
:meth:`~EmbodyTurnEngine.note_spoken` records what the layer's mouth said. When
a human talks over an audibly speaking robot, that is no longer the whole
truth: the reply was cut mid-sentence, and
:meth:`~EmbodyTurnEngine.note_interrupted_reply` is where the two halves are
recorded honestly — the MEASURED prefix as spoken, the remainder as a
:class:`reachy.embody.interjection.WantedToSay` artifact parked as CONTEXT for
the next turn to judge. The measurement itself is not this module's: it is
taken at the sink by :class:`reachy.speech.realtime_duplex.
RealtimeDuplexSession`, and arrives here as a plain structural type
(:class:`_SpokenSplitLike`) so the mind never imports the wire.

Two properties of that record are load-bearing and are stated where they are
enforced (see the method): a cut NEVER triggers a turn, and the layer's record
makes no claim that the SERVER's own history matches it — a client-local cut is
invisible to the floor in phase 1, so the server still holds the full reply.

The export contract
-------------------
Per turn the engine emits, through the shared
:class:`~reachy.export.exporter.ExportHook` (``docs/export-schema.md``):

* ``message`` — one per voice tool call (``speak`` / ``harmonics``), emitted
  BEFORE dispatch, and one per :meth:`note_spoken`. As the schema says of
  ``agent attach``'s publish-only seams, the block names the utterance the mind
  CHOSE, not a speaker that moved. Since task t12 that distinction carries more
  weight, not less: a voice tool call is now a PROPOSAL the interjection policy
  may refuse, so the block records what the background mind wanted said and the
  same turn's ``thinking`` block carries the verdict verbatim. Emitting it
  before dispatch is what keeps "what it wanted" and "what it was allowed"
  visible as two facts rather than one.
* ``emotion`` — when the model's own reply text carries an emoji, resolved to a
  pose through the hook's ``pose_resolver`` (the shipped expressions catalog).
  The layer's action set has no ``apply_pose`` tool, so this is the one place an
  expression can come from; it is a plain codepoint scan
  (:func:`first_emoji`), NOT a resurrection of the retired ``*emoji*`` marker
  grammar — the text is neither consumed nor rewritten.
* ``thinking`` — exactly one per turn, last, carrying every perception line the
  turn read (its triggers first, then the drained context) and the raw turn
  text, which OPENS with this turn's ``[triggers=… context=… coalesced-from=…]``
  drain counts and continues with the model's streamed ``reasoning`` (see
  :data:`reachy.speech.llm.REASONING_DELTA_KEYS` — the gateway sends
  ``reasoning``, not the documented ``reasoning_content``), its content, every
  tool call, every tool RESULT including refusals, and any named drop. That last
  part is what puts the red-team refusals on the feed.

Import boundary
---------------
Like the rest of ``reachy/embody/``: no ``reachy_mini``, no ``reachy.daemon``,
no subprocess, no shell (``tests/test_embody_redteam.py`` walks this package by
AST). The audio devices belong to :mod:`reachy.embody.media` and the socket to
:mod:`reachy.speech.realtime_duplex`; this module owns no I/O but the one HTTP
lane, and reaches even that through an injectable ``turn_fn``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from reachy import senselog
from reachy.cli._errors import CliError
from reachy.embody.attention import DEFAULT_ATTENTION_WINDOW_S, LABEL_COLD, AttentionGate
from reachy.embody.cues import ClassifiedCue, CueClass
from reachy.embody.interjection import ADMITTED_CUE_CLASS, WantedToSay, make_wanted_to_say
from reachy.embody.scope import CognitionScope, scope_from_interjection
from reachy.embody.tools import HARMONICS, SPEAK
from reachy.export.events import EmotionEvent, MessageEvent, ThinkingEvent
from reachy.export.exporter import ExportHook
from reachy.speech import llm as _llm
from reachy.speech.realtime_wire import (
    ITEM_DISPOSITION_CONTEXT,
    ITEM_DISPOSITION_HISTORY,
    ITEM_ROLE_SYSTEM,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Senselog identity                                                           #
# --------------------------------------------------------------------------- #

#: ``[SENSE stage=turn source=embody event=<id>]`` — the same ``turn`` stage
#: :class:`~reachy.speech.agent_turn.AgentTurnEngine` logs under, with the layer's
#: own source so one journal can be split by which mind thought what.
STAGE = "turn"
SOURCE = "embody"

# --------------------------------------------------------------------------- #
# Model roles                                                                 #
# --------------------------------------------------------------------------- #

#: The tool-bearing conversational lane (qwen on thor, per the spec).
ROLE_WORKER = "worker"
#: The cheap perception lane (gemma), used by :meth:`EmbodyTurnEngine.ask`.
ROLE_SENSES = "senses"
#: Every role this module knows. A name outside it is refused, never guessed.
ROLES: tuple[str, ...] = (ROLE_WORKER, ROLE_SENSES)

#: Process-scoped overrides. Deliberately NOT ``REACHY_OPENAI_MODEL_ID``: that
#: one is read by the runtime's engagement classifier as well, so pointing it at
#: the layer's worker model would change the reflex robot's behaviour.
ENV_WORKER_MODEL = "REACHY_EMBODY_WORKER_MODEL"
ENV_SENSES_MODEL = "REACHY_EMBODY_SENSES_MODEL"
#: Process-scoped override for :attr:`Limits.attention_window_s` (issue #150).
#: Same scoping as :data:`ENV_WORKER_MODEL`/:data:`ENV_SENSES_MODEL`, and the
#: same reason: see :func:`resolve_attention_window_s`.
ENV_ATTENTION_WINDOW_S = "REACHY_EMBODY_ATTENTION_WINDOW"

# --------------------------------------------------------------------------- #
# Named drop reasons — every failure names one, never a silent no-op          #
# --------------------------------------------------------------------------- #

#: No delta arrived within the inter-chunk idle budget (honesty condition h6).
REASON_STREAM_IDLE = "stream-idle-timeout"
#: The endpoint refused, was unreachable, or answered non-2xx.
REASON_ENDPOINT_UNREACHABLE = "llm-endpoint-unreachable"
#: The stream failed some other way (a reset, an unexpected fault in the turn).
REASON_STREAM_FAILED = "stream-failed"
#: The model kept calling tools past :data:`DEFAULT_MAX_TOOL_ROUNDS`.
REASON_TOOL_ROUNDS_EXHAUSTED = "tool-rounds-exhausted"
#: The pending-TRIGGER buffer was full; the newest trigger was refused.
REASON_INPUT_QUEUE_FULL = "input-queue-full"
#: The context park already holds :data:`DEFAULT_MAX_CONTEXT` DISTINCT cue
#: facts and a new one arrived. A repeat of an already-parked cue can never
#: reach this: it coalesces, so a flood of one exact fact cannot fill the
#: park. Free-text perception (:meth:`EmbodyTurnEngine.submit_perception`)
#: cannot reach this reason at all — see :data:`REASON_PERCEPTION_SOURCES_FULL`,
#: its own, separately bounded, park.
REASON_CONTEXT_PARK_FULL = "context-park-full"
#: :meth:`EmbodyTurnEngine.submit_perception`'s own bound: the latest-wins
#: park already holds :data:`DEFAULT_MAX_PERCEPTION_SOURCES` distinct SOURCES
#: and a new one arrived. An existing source's later text can never reach
#: this: it REPLACES the slot in place, so any number of updates from one
#: source — however many DIFFERENT descriptions — can never fill this bound;
#: only a genuinely new source can.
REASON_PERCEPTION_SOURCES_FULL = "perception-sources-full"
#: :meth:`EmbodyTurnEngine._live_perception` evicted a slot on READ: its
#: snapshot's ``captured_at`` is older than :attr:`Limits.
#: perception_stale_after_s` (task t13, spec c7). Never counted against
#: :attr:`EmbodyTurnEngine.dropped_inputs` — this is a slot going stale, not a
#: capacity refusal, the same distinction :data:`REASON_SUMMARY_STALE` draws
#: against the input-queue reasons above it.
REASON_PERCEPTION_STALE = "perception-stale"
#: :meth:`EmbodyTurnEngine.submit_scope`'s own bound: the scope park already
#: holds :data:`DEFAULT_MAX_SCOPES` distinct ``(kind, goal)`` concerns and a new
#: goal arrived. A restatement of an already-parked GOAL can never reach this —
#: it replaces the slot in place — so only a genuinely new concern can, and an
#: EXPIRED slot is freed first (see :meth:`~EmbodyTurnEngine.submit_scope`).
REASON_SCOPE_PARK_FULL = "scope-park-full"
#: A blank cue/utterance was submitted.
REASON_EMPTY_INPUT = "empty-input"
#: A turn produced no text, no reasoning and no tool call.
REASON_SILENT_TURN = "silent-turn"
#: An utterance arrived while attention was COLD and named nobody (issue #148).
#: Imported from :mod:`reachy.embody.attention` rather than retyped: the gate's
#: label IS the drop reason, exactly as the runtime's engagement labels are.
REASON_NOT_ADDRESSED_COLD = LABEL_COLD
#: :meth:`EmbodyTurnEngine.mark_summary_stale` was called: Qwen's rolling
#: summary maintenance pass failed this cycle (the worker LLM unreachable, or
#: the text it produced was itself refused). Named and counted rather than a
#: silent narrowing of Gemma's memory to just the last ``m`` turns (spec claim
#: c45, honesty h30) — see the module docstring's "Nested windows" section.
REASON_SUMMARY_STALE = "summary-stale"
#: :meth:`EmbodyTurnEngine.update_summary` was handed text longer than
#: :attr:`Limits.summary_max_chars`. REFUSED, never truncated — the same
#: fail-closed idiom :data:`reachy.behavior.rules.MAX_SAY_CHARS` uses, so the
#: cut point is never "wherever this module happened to stop reading" instead
#: of a decision Qwen's producer (task t12) can see and act on.
REASON_SUMMARY_TOO_LONG = "summary-too-long"

#: Every reason this module can emit, in one place so the journal, the export
#: feed, the operator docs and the tests share ONE vocabulary.
DROP_REASONS: tuple[str, ...] = (
    REASON_STREAM_IDLE,
    REASON_ENDPOINT_UNREACHABLE,
    REASON_STREAM_FAILED,
    REASON_TOOL_ROUNDS_EXHAUSTED,
    REASON_INPUT_QUEUE_FULL,
    REASON_CONTEXT_PARK_FULL,
    REASON_PERCEPTION_SOURCES_FULL,
    REASON_PERCEPTION_STALE,
    REASON_SCOPE_PARK_FULL,
    REASON_EMPTY_INPUT,
    REASON_SILENT_TURN,
    REASON_NOT_ADDRESSED_COLD,
    REASON_SUMMARY_STALE,
    REASON_SUMMARY_TOO_LONG,
)

# --------------------------------------------------------------------------- #
# Defaults                                                                    #
# --------------------------------------------------------------------------- #

#: The inter-chunk idle budget, in seconds. It bounds ONE read, so it must also
#: cover time-to-first-token — the gateway lazy-loads the worker model, and the
#: probe measured 43.2 s to the first content delta with thinking on. A stall
#: therefore costs one budget of silence and then names itself; a long think
#: costs nothing at all, because every later chunk resets the clock.
DEFAULT_IDLE_TIMEOUT_S = 90.0
#: Sampling temperature for both lanes.
DEFAULT_TEMPERATURE = 0.7
#: Rounds one turn may take before the tool loop is force-stopped (cited from
#: :data:`reachy.speech.agent_turn.DEFAULT_MAX_TOOL_ROUNDS`).
DEFAULT_MAX_TOOL_ROUNDS = 6
#: Prior (perception, reply) pairs the WORKER (Qwen) lane replays in full —
#: ``n`` in issue #154 decision c30's "m nested in n" nested-window shape.
#: This is the single shared history's own bound: the ``deque`` in
#: :attr:`EmbodyTurnEngine._history` never holds more than this many turns,
#: and :data:`DEFAULT_SENSES_HISTORY_MAXLEN` is sliced from its tail. 60 turns
#: of ordinary spoken exchange is inexpensive next to a clip ask (see the
#: module docstring's "Nested windows" section for the measured numbers) —
#: this is no longer the 6-entry discipline :data:`reachy.speech.agent_turn.
#: DEFAULT_HISTORY_MAXLEN` / :data:`reachy.speech.engagement.
#: DEFAULT_HISTORY_MAXLEN` use, and does not need to be: this lane's window is
#: text-only, the media clip is what actually costs tokens.
DEFAULT_HISTORY_MAXLEN = 60
#: Prior turns Gemma (the ``senses`` lane) replays — ``m`` in issue #154
#: decision c30. A STRICT SUFFIX of :data:`DEFAULT_HISTORY_MAXLEN`'s ``n``
#: turns, taken from the tail of the SAME deque, never a second history of its
#: own. Sized against the measured cost (``docs/evidence/
#: 2026-08-02-t1-media-chunk-budget.md``): 20 turns of text cost 401 prompt
#: tokens against 2 399 for one clip, roughly +16% on a clip ask — cheap
#: enough to afford, not free enough to make unbounded. Refused, never
#: silently clamped, if it exceeds ``n`` (:meth:`Limits.__post_init__`).
DEFAULT_SENSES_HISTORY_MAXLEN = 20
#: Upper bound, in characters, on the Qwen-maintained rolling summary of
#: everything older than Gemma's ``m``-turn window (issue #154 decision c30).
#: "A compaction that can grow without limit is a slow leak with extra steps"
#: (issue #154) — this is that bound. :meth:`EmbodyTurnEngine.update_summary`
#: REFUSES text over this length rather than truncating it, the same
#: fail-closed idiom :data:`reachy.behavior.rules.MAX_SAY_CHARS` uses. Sized
#: generously against the same measurement that sized ``m``: the 20-turn text
#: window alone costs ~401 prompt tokens, and t1's evidence records that "a
#: few hundred tokens of summary is small beside the media it accompanies and
#: comparable to the turn window itself" — roughly 500 tokens, ~4 chars/token.
DEFAULT_SUMMARY_MAX_CHARS = 2000
#: Pending TRIGGERS (utterances + alerts) held between turns. Bounded: a
#: runtime feed that outruns cognition must drop the NEWEST by name, never grow
#: without bound.
DEFAULT_MAX_PENDING = 32
#: DISTINCT facts the context park holds. Small on purpose: the cue vocabulary
#: is closed and the measured 40 s flood was six facts arriving 187 times, so a
#: park that needs more than this is describing a robot in a genuinely novel
#: situation, not a busy one.
DEFAULT_MAX_CONTEXT = 24
#: The SOURCE name a caller's free-text perception is parked under when it
#: does not name one — see :meth:`EmbodyTurnEngine.submit_perception`.
#: Distinct sources get distinct slots; the same source's later text REPLACES
#: rather than adds beside its predecessor.
DEFAULT_PERCEPTION_SOURCE = "vision"
#: DISTINCT perception SOURCES the latest-wins park holds at once — a bound
#: that exists to fail closed rather than because a real deployment needs it:
#: today's only caller (the clip-asking senses lane, issue #139's ``_ClipAsker``)
#: is one source, so this only ever bites a caller that names a new source on
#: every call, which is a bug, not a busy robot.
DEFAULT_MAX_PERCEPTION_SOURCES = 4
#: How long a persisted :class:`PerceptionSnapshot` stays LIVE before
#: :meth:`EmbodyTurnEngine._live_perception` evicts it as stale (task t13,
#: issue #153/#155 spec claim c7). Mirrors :data:`reachy.cli._commands.agent.
#: DEFAULT_CLIP_STALE_AFTER_S` (30.0) BY VALUE, not by import — the same
#: reason :data:`DEFAULT_ATTENTION_WINDOW_S` is independently defined on each
#: side of the composition-root boundary. The number is deliberately the
#: SAME one, not a fresh guess: a snapshot's ``captured_at`` carries the
#: clip's own monotonic ``ts``, which ``_ClipAsker.poll_once`` already
#: refuses to ask about once it is this old, so re-checking it here — later,
#: against the SAME clock family (``time.monotonic``, a host-wide counter
#: comparable across processes on one host, never wall time) — is the
#: identical staleness rule evaluated a second time, not a new one invented
#: for persistence.
DEFAULT_PERCEPTION_STALE_AFTER_S = 30.0
#: DISTINCT ``(kind, goal)`` concerns the cognition-scope park holds at once
#: (:meth:`EmbodyTurnEngine.submit_scope`). Small on purpose, and for the same
#: reason the artifact expires in turns: a foreground voice steered by five
#: simultaneous background concerns is not being helped, and the whole point of
#: a scope is that it is COMPACT. A restatement of a parked goal replaces it
#: rather than adding beside it, so this only bites on genuinely distinct
#: concerns.
DEFAULT_MAX_SCOPES = 3
#: Seconds between ALERT-triggered turns. The first alert after quiet is never
#: delayed; this only bites on a burst, where the fires it holds back are
#: deferred into the next turn rather than dropped. Sized against the measured
#: defect: 23 turns in 40 s (~34/min) becomes at most 12/min from alerts, while
#: a single reflex the robot should react to still gets an immediate turn.
DEFAULT_MIN_ALERT_INTERVAL_S = 5.0
#: Recent already-spoken replies carried into the next turn's context.
DEFAULT_SPOKEN_MAXLEN = 4
#: :class:`reachy.embody.interjection.WantedToSay` artifacts the canonical
#: record keeps at once (:meth:`EmbodyTurnEngine.note_interrupted_reply`).
#: Small, and for the same reason the artifact expires in TURNS: a robot
#: holding the tail ends of four old sentences is not remembering, it is
#: hoarding. The artifact reaches the MODEL through the context park like any
#: other fact — this deque is the layer's own record of what it kept, which is
#: what makes attribution and expiry inspectable rather than implied.
DEFAULT_WANTED_TO_SAY_MAXLEN = 4
#: Minimum gap between turns in :meth:`EmbodyTurnEngine.run`.
DEFAULT_TURN_INTERVAL = 0.5

#: Tool calls exported as ``message`` blocks. Imported from the action set, never
#: retyped, so a rename cannot leave the export mapping pointing at a dead name.
DEFAULT_VOICE_TOOLS: frozenset[str] = frozenset({SPEAK, HARMONICS})

DEFAULT_EMBODY_SYSTEM_PROMPT = (
    "You are Reachy Mini, a small expressive desk robot, present in the room with "
    "the people you can hear. You perceive two kinds of thing: what people say "
    "near you, and what your own body just did on its own — its reflex rules "
    "firing, a hand petting your head, a face appearing. Your spoken conversation "
    "is already handled: you do not need to reply in words to everything you hear. "
    "What you decide here is what to DO. You act only through your tools: goto "
    "(move your head, antennas or body), run_behavior (run one of your movement "
    "sets), speak and harmonics (PROPOSE something to say out loud, or a chirp — "
    "the voice that actually talks to the room decides the wording and whether to "
    "say it, and may refuse), and create_rule (teach yourself a new standing "
    "reaction that keeps firing on its own afterwards). When nothing is worth "
    "doing, do nothing and call no tools. "
    "Keep any speech to one or two short, natural first-person sentences. Never "
    "narrate raw sensor readings. If you want to show an expression, put a single "
    "emoji in your reply text."
)

#: Prefixed onto Gemma's last known summary (or stands alone if there was
#: never one) when :meth:`EmbodyTurnEngine.mark_summary_stale` has been called
#: since the last successful :meth:`~EmbodyTurnEngine.update_summary` — spec
#: claim c45, honesty h30. Named so a reader of the journal, the export feed,
#: or the model's own prompt recognises it on sight; see the module
#: docstring's "Nested windows" section for why this exists instead of
#: silently narrowing Gemma's memory to just the last ``m`` turns.
STALE_SUMMARY_MARKER = (
    "[The summary of the conversation before this window could not be "
    "refreshed and may be out of date.]"
)

#: Heads the system message :meth:`EmbodyTurnEngine._scope_message` builds from
#: the live :class:`reachy.embody.scope.CognitionScope`\\ s (spec claim c8).
#: Every word of it is load-bearing for claim c2: the background mind is
#: introduced as a colleague with suggestions, never as an instruction, and the
#: last clause says outright where the decision lives — because a prompt that
#: reads like an order is how a background mind quietly becomes the speaker.
SCOPE_PREAMBLE = (
    "Your background mind is working on the following. Use any of it that helps "
    "you answer well; the wording, and whether to say anything at all, are yours."
)

#: Opens every item :meth:`EmbodyTurnEngine.floor_correction` builds (spec claim
#: c39). A correction APPENDS to the floor's history rather than rewriting the
#: turn already in it — the schema has no operation for that — so it has to
#: announce itself in words the reading model, the journal and a test can all
#: recognise. Kept as a constant for the usual reason: a phrase restated in a
#: test is a phrase that drifts.
FLOOR_CORRECTION_PREFIX = "Correction:"
#: The correction when the room heard SOME of the reply, ``{said}`` filled with
#: the measured prefix. Deliberately says "was cut off" rather than naming the
#: interrupter: the layer knows a cut happened, not who caused it.
FLOOR_CORRECTION_PARTIAL = (
    FLOOR_CORRECTION_PREFIX + ' my previous reply was cut off. Only "{said}" was '
    "actually spoken aloud; the rest of it was never heard."
)
#: The correction when the cut landed before a single word reached the room.
FLOOR_CORRECTION_NOTHING = (
    FLOOR_CORRECTION_PREFIX + " my previous reply was cut off before any of it "
    "was spoken aloud; none of it was heard."
)

# --------------------------------------------------------------------------- #
# Input kinds                                                                 #
# --------------------------------------------------------------------------- #

#: A runtime perception the robot's own reflexes DECIDED — a rule fire. The one
#: cue class that triggers, because it is the one the layer cannot learn any
#: other way (see :class:`reachy.embody.cues.CueClass`).
KIND_ALERT = "alert"
#: Something a person said. The layer HEARS everyone (spec claim c4, pinned in
#: the wire); whether what it heard wakes the mind is
#: :mod:`reachy.embody.attention`'s decision, taken in
#: :meth:`EmbodyTurnEngine.submit_utterance`.
KIND_UTTERANCE = "utterance"


@dataclass(frozen=True)
class Input:
    """One pending TRIGGER: what kind it was, and the text a turn will read."""

    kind: str
    text: str

    def render(self) -> str:
        """The line this input contributes to a turn's perception list."""
        if self.kind == KIND_UTTERANCE:
            return f'heard: "{self.text}"'
        return self.text


@dataclass
class Parked:
    """One CONTEXT fact in the park, and how many times it has been perceived.

    Mutable, unlike :class:`Input`, because coalescing IS a mutation of the
    entry already there: the whole point is that the 145th "speech from the
    left" costs one increment rather than a 145th list slot. Every mutation
    happens under the engine's intake lock and is O(1).
    """

    text: str
    count: int = 1

    def render(self) -> str:
        """``"speech from the left (x145)"`` — or the bare fact when seen once.

        A single sighting reads as a fact, not a tally: ``(x1)`` on every quiet
        line would be noise in the one place the model is meant to skim.
        """
        return self.text if self.count == 1 else f"{self.text} (x{self.count})"


@dataclass(frozen=True)
class PerceptionSnapshot:
    """One structured perception observation (issue #155, spec claim c7).

    What the clip-asking senses lane
    (:class:`~reachy.cli._commands.agent._ClipAsker`) produces INSTEAD of free
    prose, closing issues #153/#154: a compact, typed artifact rather than
    uncontrolled prompt text — the five fields issue #155 names for it.

    ``summary`` is the one field every caller can always fill, even when the
    senses model ignores the requested JSON shape (see
    :func:`reachy.cli._commands.agent.parse_perception_answer`'s degrade
    path, which falls back to the model's raw answer text) — the rest are a
    best-effort extra, never a requirement for the observation to reach the
    robot's context at all.

    ``captured_at`` is a MONOTONIC timestamp (``time.monotonic()``, never
    wall time — see the module docstring's freshness note) naming WHEN THE
    FRAME WAS CAPTURED, not when this snapshot was built or submitted: the
    production caller carries the runtime clip's own ``ts``
    (``reachy/behavior/clip_rider.py``) straight through, so a snapshot that
    sits unread in the park is judged by the true age of what it describes,
    never by how recently the layer happened to hear about it. ``None`` means
    "the caller did not know" — :meth:`EmbodyTurnEngine.submit_perception`
    stamps its own clock at intake in that case, exactly as it already does
    for a bare string.

    ``frame_ref`` is the clip path the observation was drawn from — kept for
    attribution/inspection (the journal, the export feed, a future consumer
    that wants the actual frame), never rendered into the compact prose a
    turn's context carries (see :meth:`render`).
    """

    summary: str
    entities: tuple[str, ...] = ()
    confidence: float | None = None
    captured_at: float | None = None
    frame_ref: str | None = None

    def render(self) -> str:
        """The compact one-line prose a turn's context shows for this snapshot.

        ``frame_ref`` is deliberately absent: a file path narrated into the
        model's prompt would cost tokens for no benefit the model can act on,
        so it stays a structured field for attribution instead of prose.
        """
        parts = [self.summary]
        if self.entities:
            parts.append("entities: " + ", ".join(self.entities))
        if self.confidence is not None:
            parts.append(f"confidence={self.confidence:.2f}")
        return "; ".join(parts)


@dataclass
class PerceptionSlot(Parked):
    """One latest-wins CONTEXT slot for structured perception (issue #154/#155).

    A subclass of :class:`Parked`, not a second copy: it reuses ``text`` /
    ``count`` and the same list :meth:`EmbodyTurnEngine._drain_context` reads,
    so the turn-building and accounting code needs no branch for which kind of
    entry it is holding. What differs is the KEY it lives under and how a
    later update behaves — both live in
    :meth:`EmbodyTurnEngine.submit_perception`, not here — and how it renders.
    Since task t13 the slot also carries the full structured
    :attr:`snapshot` (:class:`PerceptionSnapshot`); ``text`` is kept in sync
    with ``snapshot.summary`` on every update so the generic :class:`Parked`
    accounting (``coalesced-from``, the plain-string legacy path) needs no
    branch of its own.

    Unlike :class:`Parked`'s exact-text coalescing (the SAME fact reported
    again), a slot coalesces on the IDENTITY OF ITS SOURCE, never on text: a
    new description from that source REPLACES the old one, because "what the
    camera currently shows" is a STATE, not a growing list of past sightings.
    ``count`` still increments on every replacement, so the slot keeps adding
    its share to the turn's ``coalesced-from`` total — a silent coalescer is
    indistinguishable from a dropper, and that holds for a replacement exactly
    as it does for a repeat. The render marks the two apart on purpose:
    ``"... (updated x180)"`` here, never a bare ``"... (x180)"``, so a reader
    can tell "the 180th version of this fact" apart from "this exact fact
    recurred 180 times."

    Also unlike :class:`Parked`, a slot does not live only until the next
    turn drains it: :meth:`EmbodyTurnEngine._live_perception` keeps it PARKED
    across turns until it is superseded (a later
    :meth:`~EmbodyTurnEngine.submit_perception` for the same source) or STALE
    (:meth:`is_stale`) — see the module docstring's "A state PERSISTS"
    section for why that lifetime differs from the closed cue vocabulary's.
    """

    snapshot: PerceptionSnapshot | None = None

    def render(self) -> str:
        """``"<latest snapshot> (updated x180)"`` — or the bare rendering on the first sighting."""
        body = self.snapshot.render() if self.snapshot is not None else self.text
        return body if self.count == 1 else f"{body} (updated x{self.count})"

    def is_stale(self, now: float, *, stale_after_s: float) -> bool:
        """Whether this slot's snapshot has gone stale (task t13, spec c7).

        ``stale_after_s <= 0`` disables the bound entirely — the same
        zero-disables convention :attr:`Limits.min_alert_interval_s` /
        :attr:`Limits.attention_window_s` already use, so a park meant to
        never expire (an operator choice, or a test with no clock to worry
        about) is never tripped by a default nobody asked for. A slot with no
        snapshot, or a snapshot with no known capture time, never expires
        either: there is nothing to judge its age against, and that should
        not happen once :meth:`~EmbodyTurnEngine.submit_perception` always
        builds one — this stays defensive rather than assumed.
        """
        if self.snapshot is None or self.snapshot.captured_at is None:
            return False
        if stale_after_s <= 0.0:
            return False
        return (now - self.snapshot.captured_at) > stale_after_s


# --------------------------------------------------------------------------- #
# Model selection                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EmbodyModels:
    """The per-request model name for each lane.

    The defaults are the ROLE names themselves: lobes' ``resolve_model`` accepts
    a role, which is what keeps a gateway-side model promotion from breaking the
    layer (the deployed ``worker`` role has already moved once). An operator who
    wants a specific served id sets :data:`ENV_WORKER_MODEL` /
    :data:`ENV_SENSES_MODEL` in the LAYER PROCESS's environment — never in
    ``environment.d``, which the runtime reads too.
    """

    worker: str = ROLE_WORKER
    senses: str = ROLE_SENSES

    @classmethod
    def resolve(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        worker: str | None = None,
        senses: str | None = None,
    ) -> "EmbodyModels":
        """Resolve from explicit arguments, then *env*, then the role names.

        *env* defaults to ``os.environ`` — the PROCESS environment, read once
        per call and never written. No file is opened here, by design and by
        test.
        """
        source = env if env is not None else os.environ
        return cls(
            worker=worker or source.get(ENV_WORKER_MODEL) or ROLE_WORKER,
            senses=senses or source.get(ENV_SENSES_MODEL) or ROLE_SENSES,
        )

    def model_for(self, role: str) -> str:
        """The model name for *role*; an unknown role is refused, not guessed."""
        if role == ROLE_WORKER:
            return self.worker
        if role == ROLE_SENSES:
            return self.senses
        raise ValueError(f"unknown model role {role!r}; the layer has exactly {ROLES}")


def resolve_attention_window_s(
    explicit: float | None = None, *, env: Mapping[str, str] | None = None
) -> float:
    """Resolve :attr:`Limits.attention_window_s` (issue #150).

    Precedence: *explicit* argument, then :data:`ENV_ATTENTION_WINDOW_S`, then
    :data:`~reachy.embody.attention.DEFAULT_ATTENTION_WINDOW_S`. Mirrors
    :meth:`EmbodyModels.resolve` on purpose, including its reasoning: *env*
    defaults to ``os.environ`` — the PROCESS environment, read once per call
    and never written, and never a file. An operator who wants a different
    window sets :data:`ENV_ATTENTION_WINDOW_S` in the LAYER PROCESS's own
    environment, never in an ``environment.d`` drop-in — that mechanism is
    login-session-wide (applied identically to every unit under the session,
    the runtime's included) rather than scoped to this one process, and the
    layer ships no systemd unit of its own for a drop-in to target anyway
    (:mod:`reachy.embody.supervisor`'s module docstring).

    Unlike :meth:`EmbodyModels.resolve`'s string fields — where ``""`` and
    "not given" are the same thing, so a plain ``or`` chain is safe — ``0`` is
    a legitimate, meaningfully DIFFERENT value here: it means name-only-
    forever, the same convention :attr:`Limits.min_alert_interval_s` already
    uses for ``0``. So *explicit* is checked by identity against ``None``
    rather than by truthiness; a bare ``or`` chain would silently read an
    explicit ``0.0`` as unset and fall through to the environment or the
    default. :class:`~reachy.embody.attention.AttentionGate` itself clamps a
    negative value to ``0.0``; this function does not re-clamp, so a caller
    sees exactly what was configured. An unparseable environment value
    degrades to the default with a logged warning (mirrors
    :func:`reachy.embody.media._env_int`) — never a raise.
    """
    if explicit is not None:
        return float(explicit)
    source = env if env is not None else os.environ
    raw = source.get(ENV_ATTENTION_WINDOW_S)
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("embody: ignoring non-numeric %s=%r", ENV_ATTENTION_WINDOW_S, raw)
    return DEFAULT_ATTENTION_WINDOW_S


# --------------------------------------------------------------------------- #
# Bounds, grouped into one frozen home (issue #141, python:S107)              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Limits:
    """:class:`EmbodyTurnEngine`'s numeric bounds, out of the constructor's kwargs.

    Every field here was a bare keyword parameter on
    :class:`EmbodyTurnEngine` before this task; the constructor's OTHER
    parameters are injected SEAMS (a collaborator, a callable tap, a clock)
    and none of those moved — grouping seams in here too would just relocate
    the S107 complaint rather than fix its actual defect. This class does not
    re-explain each bound: the measured reasoning behind every default lives
    with its ``DEFAULT_*`` constant above (the one documented home this module
    already keeps), and every field here simply carries that same constant
    forward unchanged, so the refactor cannot silently change a number while
    moving it.
    """

    #: The inter-chunk idle budget passed to every streamed call.
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S
    #: Rounds one turn may take before the tool loop is force-stopped.
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    #: Prior (perception, reply) pairs the WORKER (Qwen) lane replays in
    #: full — ``n`` in issue #154 decision c30. The SAME deque backs both
    #: windows onto the shared history; this is its own bound.
    history_maxlen: int = DEFAULT_HISTORY_MAXLEN
    #: Prior turns the SENSES (Gemma) lane replays — ``m`` in decision c30. A
    #: STRICT SUFFIX of ``history_maxlen``'s ``n`` turns, sliced from the tail
    #: of the SAME deque, never a second history of its own. Refused, never
    #: silently clamped, when it exceeds ``history_maxlen``
    #: (:meth:`Limits.__post_init__`).
    senses_history_maxlen: int = DEFAULT_SENSES_HISTORY_MAXLEN
    #: Upper bound, in characters, on the Qwen-maintained rolling summary of
    #: everything older than the senses lane's ``m``-turn window (decision
    #: c30). Refused, never truncated, past this length
    #: (:meth:`EmbodyTurnEngine.update_summary`).
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS
    #: Pending TRIGGERS (utterances + alerts) held between turns.
    max_pending: int = DEFAULT_MAX_PENDING
    #: DISTINCT facts the context park holds.
    max_context: int = DEFAULT_MAX_CONTEXT
    #: DISTINCT perception SOURCES the latest-wins park holds (see
    #: :meth:`EmbodyTurnEngine.submit_perception`) — a bound of its own, on
    #: purpose: free text keyed by SOURCE must never compete with the closed
    #: cue vocabulary keyed by TEXT for the same ``max_context`` budget, which
    #: is exactly the crowding-out defect (issue #154) this pair of bounds
    #: exists to close.
    max_perception_sources: int = DEFAULT_MAX_PERCEPTION_SOURCES
    #: How long a persisted perception snapshot stays live before
    #: :meth:`EmbodyTurnEngine._live_perception` evicts it as stale (task
    #: t13, spec claim c7); ``<= 0`` disables the bound. See
    #: :data:`DEFAULT_PERCEPTION_STALE_AFTER_S` for why this mirrors the clip
    #: asker's own staleness bound by value rather than widening it.
    perception_stale_after_s: float = DEFAULT_PERCEPTION_STALE_AFTER_S
    #: DISTINCT ``(kind, goal)`` concerns the cognition-scope park holds (see
    #: :meth:`EmbodyTurnEngine.submit_scope`) — a THIRD bound of its own, for
    #: the same reason the perception park has one: what the background mind is
    #: working on must never compete with what the robot perceived for the same
    #: budget.
    max_scopes: int = DEFAULT_MAX_SCOPES
    #: Seconds between ALERT-triggered turns; ``0`` disables the bound.
    min_alert_interval_s: float = DEFAULT_MIN_ALERT_INTERVAL_S
    #: Recent already-spoken replies carried into the next turn's context.
    spoken_maxlen: int = DEFAULT_SPOKEN_MAXLEN
    #: Kept remainders of interrupted replies the canonical record holds at
    #: once (:meth:`EmbodyTurnEngine.note_interrupted_reply`).
    wanted_to_say_maxlen: int = DEFAULT_WANTED_TO_SAY_MAXLEN
    #: Minimum gap between turns in :meth:`EmbodyTurnEngine.run`.
    turn_interval: float = DEFAULT_TURN_INTERVAL
    #: How long attention stays open after the last utterance heard or answer
    #: spoken (issue #148); ``0`` means name-only forever. It lives here rather
    #: than as a constructor parameter for the reason this class exists at all:
    #: a loose bound would put the count back over ``python:S107``'s threshold.
    #: The measured argument for the default is on
    #: :data:`reachy.embody.attention.DEFAULT_ATTENTION_WINDOW_S`. An operator
    #: knob reaches this field via :func:`resolve_attention_window_s` (issue
    #: #150) — the composition root resolves the value BEFORE it lands here;
    #: this field itself takes whatever it is handed, unresolved.
    attention_window_s: float = DEFAULT_ATTENTION_WINDOW_S

    def __post_init__(self) -> None:
        """Refuse ``m > n`` fail-closed (issue #154 decision c30, spec claim c3).

        A frozen dataclass still runs ``__post_init__`` on every construction
        (including :func:`dataclasses.replace`), so this is the one place the
        nested-window invariant needs to live — never re-checked, and never
        clamped, downstream. Equality (``m == n``) is explicitly allowed: the
        bound is ``m <= n``, not ``m < n``.
        """
        if self.senses_history_maxlen > self.history_maxlen:
            raise ValueError(
                f"senses_history_maxlen ({self.senses_history_maxlen}) must be <= "
                f"history_maxlen ({self.history_maxlen}): Gemma's window is a "
                "strict suffix of Qwen's over one shared history (issue #154 "
                "decision c30), never wider than it."
            )


@dataclass(frozen=True)
class RequestConfig:
    """The per-call LLM request template, grouped for the SAME reason as :class:`Limits`.

    :class:`Limits` alone — the resource/time bounds issue #141 names by
    example (``max_tool_rounds``, the several timeouts, …) — still leaves this
    engine's constructor at 17 parameters. This project's configured
    ``python:S107`` threshold is **13 authorized parameters** (verified
    against SonarCloud, not assumed), so bounds alone do not clear the rule
    here — measured, not guessed, and the reason this second dataclass exists
    at all. Its six fields are neither seams (none is a callable) nor
    resource/time bounds; they are the plain, per-call shape of every
    streamed request: which system message opens the turn, which endpoint and
    key to call, and how the model samples. Every field keeps the exact
    default it had as a bare parameter.
    """

    #: The system message on every turn.
    system_prompt: str = DEFAULT_EMBODY_SYSTEM_PROMPT
    #: Forwarded to *turn_fn* per call. ``None`` lets
    #: :class:`reachy.speech.llm.LlmConfig` resolve them from the
    #: ``REACHY_OPENAI_*`` environment as usual.
    base_url: str | None = None
    api_key: str | None = None
    #: Sampling controls, forwarded per call.
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int | None = None
    #: Ask the server for streamed reasoning. Off by default, and the default
    #: is a PRODUCT decision about a robot that answers out loud — measured
    #: live against the deployed gateway on 2026-08-02
    #: (``docs/evidence/2026-08-02-probe-thinking-vs-reasoning-deltas.md``)::
    #:
    #:     model    enable_thinking   delta keys        first *content*
    #:     worker   False (shipped)   content, role     0.22 s
    #:     worker   True              + reasoning       9.72 s
    #:     cortex   False (shipped)   content, role     0.27 s
    #:     cortex   True              + reasoning       17.96 s
    #:
    #: So turning this on costs 9-18 SECONDS before the robot says or does
    #: anything. For a layer whose whole point is realtime conversation that
    #: is not a trade worth making, and no amount of tuning elsewhere
    #: recovers it. The consequence is worth stating plainly rather than
    #: discovering: with the shipped default the gateway sends **no
    #: reasoning key at all**, so :attr:`TurnResult.reasoning` is empty and
    #: the exported ``thinking`` block carries cues, reply text, tool calls
    #: and results — but no model reasoning. The reasoning seam is correct
    #: and dormant, NOT broken. Flip this to ``True`` and it fills
    #: immediately.
    enable_thinking: bool = False


@dataclass(frozen=True)
class FloorItem:
    """ONE projection of the canonical history, addressed to the realtime floor.

    Decision **c27**: the layer curates the conversation record and PUSHES
    projections of it to the floor, so lobes' server-side history becomes what
    the layer put there rather than a second account of the same conversation.
    :meth:`EmbodyTurnEngine.floor_reseed` and
    :meth:`EmbodyTurnEngine.floor_correction` are the two producers.

    Structurally identical to
    :class:`reachy.speech.realtime_duplex.ConversationItem`, and deliberately
    NOT that class. The dependency runs one way — the composition root joins
    this module to the WebSocket client, the same arrangement
    :class:`_SpokenSplitLike` already keeps for the value travelling in the
    other direction — so this module can build what the floor needs without
    importing the socket that carries it. The role and disposition VOCABULARY
    is shared by import from :mod:`reachy.speech.realtime_wire` (the pure
    codec) rather than restated, because a second copy of a vocabulary is a
    second thing to drift; the wire re-validates every value on the way out, so
    a drift fails closed at the frame rather than silently mislabelling an item.

    Attributes:
        role: ``system`` / ``user`` / ``assistant`` — the roles the floor
            itself appends.
        text: carried verbatim, bounded by whoever produced it (the ``m``
            window's ``Limits.senses_history_maxlen``, a summary's
            ``Limits.summary_max_chars``).
        disposition: ``context`` for an ephemeral item that informs the next
            generate call and never enters history, ``history`` for a curated
            turn. The distinction is the whole reason the channel needed an
            upstream ask (agentculture/lobes-cli#170 item 2, spec claim c38):
            the floor ALREADY auto-appends both roles, so an item that landed
            in history when it meant to be ephemeral would duplicate and drift
            — the two-histories failure this arc exists to eliminate, arriving
            one level down.
    """

    role: str
    text: str
    disposition: str


# --------------------------------------------------------------------------- #
# Collaborator protocols (documentation; any matching object is accepted)     #
# --------------------------------------------------------------------------- #


class _RegistryLike(Protocol):
    def tools(self) -> list[dict]: ...

    def dispatch(self, name: str, arguments_json=None, tool_call_id=None) -> dict: ...


class _TurnFn(Protocol):
    def __call__(self, messages: list[dict], **kwargs) -> _llm.TurnResult: ...


class _SpokenSplitLike(Protocol):
    """What :meth:`EmbodyTurnEngine.note_interrupted_reply` reads off a cut reply.

    Structurally typed on purpose: the object production hands over is
    :class:`reachy.speech.realtime_duplex.SpokenSplit`, and this module must
    not import the WebSocket client to record what its own mouth did. The
    dependency runs the other way — the composition root joins them.
    """

    response_id: str | None
    text: str
    said: str
    unsaid: str


# --------------------------------------------------------------------------- #
# Emoji scan — the layer's only expression source                             #
# --------------------------------------------------------------------------- #

#: Codepoint ranges treated as an expression emoji: misc symbols + dingbats, and
#: the pictograph planes the shipped catalog's keys live in (🤔 U+1F914 …).
_EMOJI_RANGES: tuple[tuple[int, int], ...] = ((0x2600, 0x27BF), (0x1F000, 0x1FAFF))
#: Joiners and variation selectors are modifiers, never the expression itself.
_EMOJI_SKIP: frozenset[int] = frozenset({0x200D, 0xFE0E, 0xFE0F})


def first_emoji(text: str) -> str | None:
    """The first expression emoji in *text*, or ``None``.

    A plain codepoint scan — not a grammar. The retired ``*emoji*`` marker
    parser had to find delimiters, strip them and split speech out of the
    stream; this only reports whether the model chose to show a face, and leaves
    the text exactly as it was.
    """
    for char in text:
        code = ord(char)
        if code in _EMOJI_SKIP:
            continue
        if any(low <= code <= high for low, high in _EMOJI_RANGES):
            return char
    return None


# --------------------------------------------------------------------------- #
# The engine                                                                  #
# --------------------------------------------------------------------------- #


class EmbodyTurnEngine:
    """Streaming, cue-triggered cognition over the layer's closed action set.

    Every collaborator is injected, so the whole engine is exercised with no
    gateway, no robot, no threads and no clock.

    Args:
        registry: the action set. Anything exposing ``tools()`` and
            ``dispatch(name, arguments_json, tool_call_id)`` — in production
            :class:`reachy.embody.tools.EmbodyToolRegistry`, which never raises
            and returns a named refusal instead.
        turn_fn: the streaming turn function, default
            :func:`reachy.speech.llm.stream_turn`. Called as
            ``turn_fn(messages, model=…, tools=…, timeout=…, on_content=…,
            on_reasoning=…, cancel=…, …)``.
        export: the shared :class:`~reachy.export.exporter.ExportHook`. ``None``
            means no export path is entered at all.
        models: :class:`EmbodyModels`; default :meth:`EmbodyModels.resolve`.
        request: the per-call LLM request template — the system prompt, the
            endpoint + key, and the sampling controls (``temperature`` /
            ``max_tokens`` / ``enable_thinking``) — grouped into one frozen
            :class:`RequestConfig`. Grouped for the same S107 reason as
            ``limits`` below (see :class:`RequestConfig`'s docstring for why
            bounds alone do not clear the rule here); every field keeps the
            exact default it had as a bare parameter.
        limits: the engine's numeric bounds — the inter-chunk idle timeout, the
            tool-round cap, the rolling-history / pending-trigger / context-park
            / already-spoken sizes, the alert-containment interval (issue
            #143) and the inter-turn pacing — grouped into one frozen
            :class:`Limits` (issue #141/``python:S107``). Every field keeps
            the exact default it had as a bare parameter; see :class:`Limits`
            for what each one bounds.
        voice_tools: tool names exported as ``message`` blocks.
        on_content / on_reasoning: optional taps fired per delta, on the calling
            thread, as the stream arrives.
        cancel: zero-arg predicate; truthy aborts an in-flight stream after the
            current chunk. A composition root passes the same predicate it gives
            :meth:`run`'s ``stop``.
        now_fn: the monotonic clock the alert interval is measured on
            (default :func:`time.monotonic`). Injected so the containment
            bounds are testable without sleeping.
        sleep: the callable :meth:`run` sleeps with between turns (default
            :func:`time.sleep`), paced by ``limits.turn_interval``.
    """

    def __init__(
        self,
        *,
        registry: _RegistryLike,
        turn_fn: _TurnFn | None = None,
        export: ExportHook | None = None,
        models: EmbodyModels | None = None,
        request: RequestConfig | None = None,
        limits: Limits | None = None,
        voice_tools: frozenset[str] | None = None,
        on_content: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        now_fn: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._registry = registry
        self._turn_fn = turn_fn if turn_fn is not None else _llm.stream_turn
        self._export = export
        self._models = models if models is not None else EmbodyModels.resolve()
        self._request = request if request is not None else RequestConfig()
        self._system_prompt = self._request.system_prompt
        self._base_url = self._request.base_url
        self._api_key = self._request.api_key
        self._temperature = float(self._request.temperature)
        self._max_tokens = self._request.max_tokens
        self._limits = limits if limits is not None else Limits()
        self._idle_timeout_s = max(0.1, float(self._limits.idle_timeout_s))
        self._enable_thinking = bool(self._request.enable_thinking)
        self._max_tool_rounds = max(1, int(self._limits.max_tool_rounds))
        self._voice_tools = voice_tools if voice_tools is not None else DEFAULT_VOICE_TOOLS
        self._on_content = on_content
        self._on_reasoning = on_reasoning
        self._cancel = cancel if cancel is not None else _never
        self._now = now_fn if now_fn is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._turn_interval = float(self._limits.turn_interval)

        self._triggers: deque[Input] = deque(maxlen=None)
        self._max_pending = max(1, int(self._limits.max_pending))
        # Insertion-ordered by construction (``dict``), so the park reads back
        # in the order the robot first noticed each fact — stable across a
        # flood, where a recency ordering would churn every line every tick.
        self._context: dict[str, Parked] = {}
        self._max_context = max(1, int(self._limits.max_context))
        # A SEPARATE park, keyed on SOURCE rather than text (issue #154): see
        # ``submit_perception`` and ``PerceptionSlot``. Kept apart from
        # ``_context`` so free text can never compete with the closed cue
        # vocabulary for the same distinct-fact budget.
        self._perception: dict[str, PerceptionSlot] = {}
        self._max_perception_sources = max(1, int(self._limits.max_perception_sources))
        # Task t13: a persisted slot's own staleness bound, checked against
        # THIS engine's ``_now`` — the one clock the layer already shares
        # (see the ``self._attention`` comment just below).
        self._perception_stale_after_s = max(0.0, float(self._limits.perception_stale_after_s))
        # A THIRD park, keyed on ``(kind, goal)`` and read by the FOREGROUND
        # lane rather than drained by a turn (spec claim c8): see
        # ``submit_scope``. Insertion-ordered, so the prompt reads back in the
        # order the background mind raised each concern.
        self._scopes: dict[tuple[str, str], CognitionScope] = {}
        self._max_scopes = max(1, int(self._limits.max_scopes))
        self._min_alert_interval_s = max(0.0, float(self._limits.min_alert_interval_s))
        # ONE clock for the layer: the gate is a time-based state machine and a
        # second clock would make "the window elapsed" untestable and, under an
        # injected clock, wrong.
        self._attention = AttentionGate(window_s=self._limits.attention_window_s, clock=self._now)
        # -inf, never 0.0: an injected clock may start anywhere, and the FIRST
        # alert after quiet must never be the one the interval delays.
        self._last_alert_turn = float("-inf")
        self._deferral_logged = False
        self._spoken: deque[str] = deque(maxlen=max(0, int(self._limits.spoken_maxlen)))
        # The kept remainders of interrupted replies (spec claim c34). The
        # MODEL reads them through the context park like any other fact; this
        # is the layer's own record, so attribution and expiry are inspectable.
        self._wanted_to_say: deque[WantedToSay] = deque(
            maxlen=max(1, int(self._limits.wanted_to_say_maxlen))
        )
        # ONE shared conversation deque (issue #154 decision c30): the worker
        # (Qwen) replays it in FULL up to ``n``; the senses lane (Gemma) reads
        # only a tail SLICE of it (``_senses_window``) — never a second,
        # independently-maintained history. ``Limits.__post_init__`` already
        # refused ``senses_history_maxlen > history_maxlen`` at construction,
        # so ``n`` alone bounds what this deque can ever hold.
        self._history: deque[tuple[str, str]] = deque(
            maxlen=max(0, int(self._limits.history_maxlen))
        )
        self._senses_history_maxlen = max(0, int(self._limits.senses_history_maxlen))
        self._summary_max_chars = max(0, int(self._limits.summary_max_chars))
        # Qwen's rolling summary of everything older than Gemma's ``m``-turn
        # window, and whether the last attempt to refresh it failed (issue
        # #154 decision c30 / spec claim c45). Both guarded by
        # ``_summary_lock`` — ``update_summary``/``mark_summary_stale`` are
        # meant to be called from task t12's producer, on its own thread,
        # concurrently with ``ask()`` reading them on another.
        self._summary = ""
        self._summary_stale = False
        self._summary_lock = threading.Lock()
        self._last_text = ""
        # One turn at a time; ``ask`` is deliberately outside it.
        self._turn_lock = threading.Lock()
        # Guards the two intake structures ONLY, and is never held across a
        # turn, an LLM call or a log write: two threads submit (the cue reader
        # and the duplex utterance tap) while a third drains under
        # ``_turn_lock``, and both bounds are check-then-act.
        self._intake_lock = threading.Lock()
        # Guards ``self._history`` — the ONE shared deque both windows read
        # from. Appended under this lock at the end of a worker turn (never
        # across the LLM call itself: the append happens after ``_stream``
        # already returned); read under it by both ``_build_messages`` (the
        # worker's full replay) and ``_senses_window`` (Gemma's tail slice),
        # the second of which runs from ``ask()`` — deliberately OUTSIDE
        # ``_turn_lock`` — so a perception question is never serialised behind
        # a running turn's own history read.
        self._history_lock = threading.Lock()

        self.turns = 0
        self.rounds = 0
        self.tool_calls = 0
        self.refusals = 0
        self.stream_timeouts = 0
        self.stream_failures = 0
        self.dropped_inputs = 0
        #: Named, counted occurrences of :meth:`mark_summary_stale` — every
        #: failed summary-maintenance pass, not only the first (spec claim
        #: c45, honesty h30).
        self.summary_stale_count = 0
        #: Replies a cut reached (:meth:`note_interrupted_reply`). Counted as
        #: well as named: "how often is this robot talked over" is a question
        #: about the room that one drop line, long scrolled away, cannot answer.
        self.replies_cut = 0
        # Counted apart from ``dropped_inputs``, which means "a bound was hit":
        # an unaddressed utterance is not a resource failure, it is the gate
        # working, and folding the two would make a busy room look like a sick
        # layer on the summary line.
        self.unaddressed_utterances = 0

    # ------------------------------------------------------------------ #
    # Intake — O(1), safe from any thread, never raises                  #
    # ------------------------------------------------------------------ #

    def submit_cue(self, text: str, *, cue_class: CueClass = CueClass.CONTEXT) -> bool:
        """Offer one runtime perception cue. Returns whether it was accepted.

        The class defaults to :attr:`~reachy.embody.cues.CueClass.CONTEXT`
        because that is the fail-safe direction of the #143 policy: a caller
        that has not thought about which lane a cue belongs to must not be able
        to wake the mind up by accident. An ALERT is always named explicitly.
        """
        if cue_class is CueClass.ALERT:
            return self._offer_trigger(KIND_ALERT, text)
        return self._offer_context(text)

    def submit_cues(self, cues: Iterable[str | ClassifiedCue]) -> int:
        """Offer several cues, routing each by its class. Returns how many landed.

        Accepts what :func:`reachy.embody.cues.classified_cues_for_line`
        returns — the composition root's intake — and, for a caller that has no
        classification to give, bare strings, which park.
        """
        accepted = 0
        for cue in cues:
            if isinstance(cue, ClassifiedCue):
                accepted += self.submit_cue(cue.text, cue_class=cue.cue_class)
            else:
                accepted += self.submit_cue(cue)
        return accepted

    def submit_perception(
        self,
        snapshot: PerceptionSnapshot | str,
        *,
        source: str = DEFAULT_PERCEPTION_SOURCE,
    ) -> bool:
        """Offer one structured perception update as CONTEXT. Latest-wins per *source*.

        This is the escape hatch from :meth:`submit_cue`'s text-identity
        coalescing, for a caller whose content has no fixed phrase to key on —
        today, the senses lane's :meth:`ask` answering a clip question,
        polled by ``_ClipAsker`` roughly every 20 s (issue #139's h9). Two
        renderings of one room never share a key
        (``submit_cue("a kitchen with someone at the counter")`` and
        ``submit_cue("a kitchen, a person near the counter")`` are two
        DIFFERENT dict entries), so routing free text through the cue park
        fills :data:`DEFAULT_MAX_CONTEXT` with near-duplicate sightings within
        minutes and starts refusing genuine runtime facts — issue #154's
        defect. This method never touches that park: *source* keys a wholly
        separate :class:`PerceptionSlot` dict, bounded by
        :data:`DEFAULT_MAX_PERCEPTION_SOURCES` DISTINCT SOURCES rather than
        distinct text, so any number of updates from ONE source — however
        many different descriptions — occupies exactly one slot.

        Accepts a full :class:`PerceptionSnapshot` (task t13, issue #155
        c7 — the shape :class:`~reachy.cli._commands.agent._ClipAsker` now
        produces) or a plain string, wrapped into a summary-only snapshot
        stamped with THIS call's own clock reading — kept for every existing
        caller that has no structure to give. A snapshot whose own
        ``captured_at`` is ``None`` is stamped the same way; one that already
        names a capture time (the clip's own monotonic ``ts``) keeps it,
        because that is what lets :meth:`_live_perception` judge staleness
        against when the FRAME was captured rather than when this call
        happened to run.

        Always CONTEXT, never a TRIGGER, and there is no parameter to make it
        one: perception must never wake the mind on its own (mirrors
        :meth:`submit_cue`'s CONTEXT-by-default rationale, made structural
        rather than merely defaulted here). Like :meth:`_offer_context`, this
        is O(1), safe from any thread, and never raises; a blank summary is a
        named, counted drop exactly as an empty cue is.

        Unlike :meth:`_offer_context`, the returned slot is NOT drained by the
        next turn that shows it — see :meth:`_live_perception` and the module
        docstring's "A state PERSISTS" section for the lifetime this method's
        park now has.
        """
        if isinstance(snapshot, str):
            cleaned = (snapshot or "").strip()
            if not cleaned:
                self._drop(REASON_EMPTY_INPUT, "perception")
                return False
            snapshot = PerceptionSnapshot(summary=cleaned, captured_at=self._now())
        else:
            cleaned = (snapshot.summary or "").strip()
            if not cleaned:
                self._drop(REASON_EMPTY_INPUT, "perception")
                return False
            captured_at = snapshot.captured_at if snapshot.captured_at is not None else self._now()
            if cleaned != snapshot.summary or captured_at != snapshot.captured_at:
                snapshot = replace(snapshot, summary=cleaned, captured_at=captured_at)
        with self._intake_lock:
            slot = self._perception.get(source)
            if slot is not None:
                # Latest-wins: replace the snapshot IN PLACE, but keep
                # counting — the 180th update is still visible in
                # ``coalesced-from``, it just never grows the dict (see
                # ``PerceptionSlot.render``).
                slot.snapshot = snapshot
                slot.text = snapshot.summary
                slot.count += 1
                return True
            distinct = len(self._perception)
            if distinct < self._max_perception_sources:
                self._perception[source] = PerceptionSlot(text=snapshot.summary, snapshot=snapshot)
                return True
        self.dropped_inputs += 1
        self._drop(REASON_PERCEPTION_SOURCES_FULL, f"{distinct} perception sources parked")
        return False

    def submit_scope(self, scope: CognitionScope | None) -> bool:
        """Park one background-mind cognition scope for the FOREGROUND lane.

        The channel spec claim c2 gives Qwen: it influences the conversation
        through explicit, inspectable typed events and never through the mouth.
        A live scope reaches Gemma as one system message in :meth:`ask`
        (:meth:`_scope_message`), and Gemma keeps the wording and the decision
        to speak.

        Latest-wins on :meth:`~reachy.embody.scope.CognitionScope.key` —
        ``(kind, goal)``, never any of the artifact's free text (issue #154's
        lesson, restated in :mod:`reachy.embody.scope`). Two scopes pursuing
        one goal are the same standing concern restated, so the later replaces
        the earlier and the park cannot fill with re-wordings; only a genuinely
        distinct goal consumes a slot, and an EXPIRED slot is freed before the
        bound is tested, so a long conversation never wedges the park closed.

        **Context, never a trigger — and there is no parameter to make it one**,
        exactly as :meth:`submit_perception` has none. The robot does not wake
        itself up to act on its own background thought.

        O(1), safe from any thread, never raises. A ``None`` scope (what
        :func:`reachy.embody.scope.make_scope` returns on a refusal it has
        already named) is a quiet ``False`` rather than a second drop line for
        one event.
        """
        if scope is None:
            return False
        key = scope.key()
        with self._intake_lock:
            live = {
                parked_key: parked
                for parked_key, parked in self._scopes.items()
                if not parked.is_expired(self.turns)
            }
            self._scopes = live
            if key in live or len(live) < self._max_scopes:
                self._scopes[key] = scope
                return True
            distinct = len(live)
        self.dropped_inputs += 1
        self._drop(REASON_SCOPE_PARK_FULL, f"{distinct} scopes live")
        return False

    def note_interjection(
        self, interjection: object, *, alert: bool = False
    ) -> CognitionScope | None:
        """Record an ADMITTED interjection as the speakable face of a scope.

        *interjection* is duck-typed
        (:class:`reachy.embody.interjection.Interjection` in production), and it
        is ALREADY admitted: the policy owns default-deny, the per-source
        allow-list, the say cap and the rate bound, and this method is what
        happens afterwards. The proposal reaches Gemma as a ``speakable`` scope
        attributed to whoever made it — a suggestion Gemma may re-word or
        decline (spec claim c2).

        *alert* is the ONE difference between the two admission routes, and it
        belongs to the caller because only the caller knows which one it is.
        An interjection that arrived over the wire is somebody ELSE's proposal
        and is worth waking the mind for (t5's
        :data:`reachy.embody.interjection.ADMITTED_CUE_CLASS`); the worker's own
        ``speak`` tool call is not, because a mind woken by its own proposal is
        a mind talking to itself — the failure :meth:`note_spoken` avoids one
        buffer over.

        Returns the parked scope, or ``None`` when the proposal was refused as
        a scope (blank, or over a scope bound) — always after one named drop
        from :mod:`reachy.embody.scope`, never a raise: this runs on a tool
        handler's thread and on the cue reader's.
        """
        scope = scope_from_interjection(interjection, turn=self.turns)
        if scope is not None:
            self.submit_scope(scope)
        if alert:
            text = getattr(interjection, "render", lambda: "")()
            if text:
                self.submit_cues([ClassifiedCue(text=text, cue_class=ADMITTED_CUE_CLASS)])
        senselog.stage(
            STAGE,
            SOURCE,
            uuid.uuid4().hex[:8],
            f"interjection noted source={getattr(interjection, 'source', '?')!r} "
            f"alert={alert} scoped={scope is not None}",
        )
        return scope

    @property
    def scopes(self) -> tuple[CognitionScope, ...]:
        """The live cognition scopes, oldest concern first (spec claim c8).

        Expired scopes are filtered out on READ rather than swept on a timer —
        expiry is counted in TURNS, and the turn counter is the only clock this
        park has, exactly as :attr:`wanted_to_say` reasons. A caller therefore
        never sees a stale scope, and the park stays bounded regardless.
        """
        with self._intake_lock:
            live = {
                key: scope
                for key, scope in self._scopes.items()
                if not scope.is_expired(self.turns)
            }
            self._scopes = live
            return tuple(live.values())

    def submit_utterance(self, text: str) -> bool:
        """Offer one heard utterance, subject to ATTENTION (issue #148).

        The layer's ear stays ungated — the duplex session surfaces every voice
        in the room and its own boundary tests pin that — but hearing is not
        the same as being addressed. While attention is cold only an utterance
        that NAMES the robot wakes a turn; while it is warm anything does, and
        every admission extends the window. A refusal is a NAMED drop carrying
        the text it ignored, never a silent no-op: "why is it ignoring me?" has
        to be answerable from the journal.

        The gate deliberately runs BEFORE the pending-trigger bound, and the
        admission stands even if that bound then refuses the utterance: the
        robot was addressed, which is a fact about the room, not about how full
        a queue happened to be.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            self._drop(REASON_EMPTY_INPUT, KIND_UTTERANCE)
            return False
        verdict = self._attention.decide(cleaned)
        if not verdict.admitted:
            self.unaddressed_utterances += 1
            self._drop(verdict.label, f'"{cleaned[:60]}"')
            return False
        if verdict.opened:
            senselog.stage(
                STAGE,
                SOURCE,
                uuid.uuid4().hex[:8],
                f"attention open ({verdict.label}) for {self._attention.window_s:g}s",
            )
        return self._offer_trigger(KIND_UTTERANCE, cleaned)

    def note_spoken(self, text: str) -> None:
        """Record something the layer's MOUTH already said. Does NOT trigger a turn.

        The duplex session answers speech on its own, server-side. Without this
        the thinking mind would have no idea it had already replied and would
        cheerfully call ``speak`` to say it again. It is context, not a trigger —
        a robot that treats its own voice as a perception talks to itself.

        It also EXTENDS attention, so a long answer cannot time the human out
        mid-exchange — but only while attention is already warm. That
        asymmetry is load-bearing: the session is armed once and the server
        replies to every committed utterance, including the ambient ones the
        gate has just refused, so a voice that could OPEN attention would be a
        robot waking itself up (see :mod:`reachy.embody.attention`).
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return
        self._spoken.append(cleaned)
        self._attention.note_spoken()
        if self._export is not None:
            self._export.emit(MessageEvent(text=cleaned, ts=self._export.time_fn()))

    def note_interrupted_reply(self, split: _SpokenSplitLike) -> WantedToSay | None:
        """Record a reply a human cut off: the said half as spoken, the rest as kept.

        The layer's canonical record of an interjection (spec claim c34). A
        human — any external interlocutor, a peer robot or an automated system
        included — talked over the robot mid-sentence, and *split* says what
        the room actually got, measured at the sink by
        :meth:`reachy.speech.realtime_duplex.RealtimeDuplexSession.spoken_split`.
        Two halves, and neither may be skipped:

        * ``split.said`` is recorded as spoken (through :meth:`note_spoken`, so
          it is one path, not two) — **exactly** what was heard, never the
          whole reply. A mind that believes it answered when the room got half
          a sentence goes on to reason from words nobody received;
        * ``split.unsaid`` becomes a :class:`reachy.embody.interjection.
          WantedToSay` artifact — attributed to the reply, bounded, expiring in
          turns, and parked as CONTEXT so the NEXT turn can decide whether it
          is still worth saying. Never a trigger: the robot does not wake
          itself up to finish an old sentence. Never discarded silently either
          — a refused remainder (blank, or over the shared say cap) is one
          named drop from :func:`~reachy.embody.interjection.make_wanted_to_say`.

        **The correction case is the ordinary one.** The wire delivers a reply
        seconds ahead of the speaker, so a human interjecting over the tail
        does it long after ``response.done`` fired and after
        :meth:`note_spoken` recorded the reply whole. Handed the full *text*,
        this replaces that entry with the measured prefix rather than adding a
        second one. It emits no further ``message`` block for the correction:
        the export schema has no correction shape, and inventing one here is
        not this task's to do — the cut is named on the journal and the kept
        remainder reaches the feed with the next turn.

        **What this does NOT claim (phase 1, spec claim c39).** A client-local
        cut is invisible to the floor, so the SERVER's own conversation history
        still holds the full reply and OVERSTATES what the room heard. This
        record is true because the client is the measured authority for it;
        nothing here asserts the two agree, and nothing here tries to correct
        the server (that needs the ``conversation.item.create`` channel, tasks
        t10/t11). The divergence is knowing, bounded and documented.

        Returns the artifact, or ``None`` when there was no remainder to keep
        (the reply finished) or the remainder was refused. Never raises: it is
        called from a session tap on a worker thread.
        """
        said = (getattr(split, "said", "") or "").strip()
        unsaid = (getattr(split, "unsaid", "") or "").strip()
        text = (getattr(split, "text", "") or "").strip()
        response_id = getattr(split, "response_id", "") or ""
        self.replies_cut += 1
        if not self._correct_spoken(text, said) and said:
            self.note_spoken(said)
        artifact = None
        if unsaid:
            artifact = make_wanted_to_say(unsaid, response_id=response_id, turn=self.turns)
        if artifact is not None:
            self._wanted_to_say.append(artifact)
            self.submit_cues([artifact.as_cue()])
        senselog.stage(
            STAGE,
            SOURCE,
            uuid.uuid4().hex[:8],
            f"reply cut id={response_id or '?'} said={len(said)} chars "
            f"unsaid={len(unsaid)} chars kept={artifact is not None}",
        )
        return artifact

    def _correct_spoken(self, text: str, said: str) -> bool:
        """Replace an already-recorded WHOLE reply with the part that was heard.

        Returns whether a record was corrected. Scans the already-said buffer
        for the reply's full text — the entry :meth:`note_spoken` wrote at
        ``response.done``, before the cut landed — and either narrows it to
        *said* or removes it entirely when nothing was heard. The buffer holds
        at most :attr:`Limits.spoken_maxlen` lines, so this is a walk over a
        handful of strings.

        It races one thing, bounded and on purpose: a turn that drained the
        buffer between the reply and the cut has already shown the model the
        coarse record. There is nothing left to narrow then (and the drain is
        what the :class:`IndexError` arm catches), so the correction is
        declined rather than misapplied — the kept remainder still carries the
        truth about what was never said.
        """
        if not text:
            return False
        for index, spoken in enumerate(self._spoken):
            if spoken != text:
                continue
            try:
                if said:
                    self._spoken[index] = said
                else:
                    del self._spoken[index]
            except IndexError:  # pragma: no cover - the turn thread drained it first
                return False
            return True
        return False

    @property
    def wanted_to_say(self) -> tuple[WantedToSay, ...]:
        """The remainders still worth offering, oldest first (spec claim c43).

        Expired artifacts are filtered out on READ rather than swept on a
        timer: expiry is counted in TURNS, and the turn counter is the only
        clock this record has. A caller therefore never sees a stale
        remainder, and the deque stays bounded regardless.
        """
        live = tuple(item for item in self._wanted_to_say if not item.is_expired(self.turns))
        if len(live) != len(self._wanted_to_say):
            self._wanted_to_say.clear()
            self._wanted_to_say.extend(live)
        return live

    @property
    def attention(self) -> AttentionGate:
        """The two-state attention gate (issue #148).

        Exposed rather than injected: it is state the engine owns and shares a
        clock with, and a composition root configures it through
        :attr:`Limits.attention_window_s` like every other bound. A caller that
        knows the robot was addressed some other way opens the window with
        :meth:`~reachy.embody.attention.AttentionGate.note_addressed`.
        """
        return self._attention

    @property
    def pending(self) -> int:
        """How many TRIGGERS are waiting for the next turn.

        Parked context is deliberately not counted: a composition root uses
        this to decide whether the layer still has thinking to do (see
        ``_EmbodyLayer.should_stop``), and context that can never cause a turn
        would keep a finished run spinning forever.
        """
        return len(self._triggers)

    @property
    def parked(self) -> int:
        """How many DISTINCT, LIVE context facts the park is holding.

        Sums both coalescing keys: exact-text cue facts and latest-wins
        perception slots (:meth:`submit_perception`) are two separately
        bounded dicts, but both are CONTEXT, so one count describes the whole
        park to a caller that only cares "is there anything parked". Since
        task t13 the perception half is filtered through
        :meth:`_live_perception`, exactly like :attr:`scopes`/
        :attr:`wanted_to_say` filter their own park on read — a caller asking
        "how much is parked" should never be told about a slot that has
        already gone stale.
        """
        return len(self._context) + len(self._live_perception())

    @property
    def last_text(self) -> str:
        """The final assistant text of the last turn that ran (``""`` if it failed)."""
        return self._last_text

    def _offer_trigger(self, kind: str, text: str) -> bool:
        """Park-free intake for the two classes that RUN a turn. O(1)."""
        cleaned = (text or "").strip()
        if not cleaned:
            self._drop(REASON_EMPTY_INPUT, kind)
            return False
        with self._intake_lock:
            depth = len(self._triggers)
            if depth < self._max_pending:
                self._triggers.append(Input(kind=kind, text=cleaned))
                return True
        self.dropped_inputs += 1
        self._drop(REASON_INPUT_QUEUE_FULL, f"{kind} dropped, {depth} pending")
        return False

    def _offer_context(self, text: str) -> bool:
        """Coalescing intake for the closed cue vocabulary. O(1). Unchanged by issue #154.

        Keyed on the cue TEXT: the vocabulary is closed (one fixed phrase per
        perception, :mod:`reachy.runtime_cues`), so equal text means the same
        fact happened again, and the count is the only thing worth keeping
        about the repeat. This is :meth:`submit_cue`'s ONLY route for a
        CONTEXT cue — free-form text with no fixed phrase to key on belongs in
        :meth:`submit_perception`'s separate latest-wins park instead, never
        here.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            self._drop(REASON_EMPTY_INPUT, "context")
            return False
        with self._intake_lock:
            entry = self._context.get(cleaned)
            if entry is not None:
                entry.count += 1
                return True
            distinct = len(self._context)
            if distinct < self._max_context:
                self._context[cleaned] = Parked(text=cleaned)
                return True
        self.dropped_inputs += 1
        self._drop(REASON_CONTEXT_PARK_FULL, f"{distinct} distinct facts parked")
        return False

    # ------------------------------------------------------------------ #
    # One turn                                                           #
    # ------------------------------------------------------------------ #

    def run_turn(self) -> bool:
        """Run one turn over everything pending. ``False`` when there was nothing.

        A turn runs only when a TRIGGER is waiting — an utterance or an alert.
        Parked context alone is never a reason to think (issue #143); it is
        drained into whatever turn a trigger causes next, and if none ever
        comes it is simply never read, which is the correct outcome for
        ambient background.

        The pending triggers are DRAINED into the turn, so a failure consumes
        them rather than retrying forever against a sick gateway — but the
        failure is always named, on the journal and on the export feed, and the
        perception still enters the rolling history so the next turn knows it
        happened.

        Exactly ONE turn runs at a time (cited from
        :meth:`reachy.speech.agent_turn.AgentTurnEngine.run_turn`): a second
        concurrent call blocks here rather than interleaving two conversations
        into one history. :meth:`ask` is deliberately NOT behind this lock — a
        perception question must not have to wait out a long turn.
        """
        with self._turn_lock:
            if not self._triggers or self._alert_deferred():
                return False
            triggers = self._drain_triggers()
            if not triggers:
                return False
            self._run_turn(triggers, self._drain_context())
            return True

    def _alert_deferred(self) -> bool:
        """Whether the pending triggers are alerts that must wait out the interval.

        The bound is on alert-triggered TURNS, not on alert cues: an alert held
        back here stays pending and rides the next turn that runs, so a burst
        costs latency, never a lost reflex. An utterance among the triggers
        lifts the bound outright — a person talking is not rate-limited — and
        the alerts waiting with it ride that turn too.
        """
        if self._min_alert_interval_s <= 0.0:
            return False
        with self._intake_lock:
            heard = any(item.kind == KIND_UTTERANCE for item in self._triggers)
            waiting = len(self._triggers)
        if heard:
            return False
        waited = self._now() - self._last_alert_turn
        if waited >= self._min_alert_interval_s:
            return False
        if not self._deferral_logged:
            # Once per deferral window: ``run`` re-asks every ``turn_interval``,
            # and a line per ask would bury the turn it is about to describe.
            self._deferral_logged = True
            senselog.stage(
                STAGE,
                SOURCE,
                uuid.uuid4().hex[:8],
                f"alert deferred waiting={waiting} for "
                f"{self._min_alert_interval_s - waited:.1f}s",
            )
        return True

    def _run_turn(self, triggers: list[Input], context: list[Parked]) -> None:
        event = uuid.uuid4().hex[:8]
        counts = (
            f"triggers={len(triggers)} context={len(context)} "
            f"coalesced-from={sum(entry.count for entry in context)}"
        )
        senselog.stage(STAGE, SOURCE, event, f"turn {counts}")
        self.turns += 1
        if any(item.kind == KIND_ALERT for item in triggers):
            self._last_alert_turn = self._now()
        self._deferral_logged = False
        before_refusals, before_rounds = self.refusals, self.rounds
        trigger_lines = [item.render() for item in triggers]
        context_lines = [entry.render() for entry in context]
        cues = trigger_lines + context_lines
        user_content = self._build_user_content(trigger_lines, context_lines, self._drain_spoken())
        conversation = self._build_messages(user_content)
        # Seeded, not appended: the drain counts open the block so a feed reader
        # can see what a turn was built from before reading what it thought.
        raw: list[str] = [f"[{counts}]"]

        result = self._tool_loop(conversation, raw, event)
        self._last_text = (result.content if result is not None else "") or ""
        with self._history_lock:
            self._history.append((user_content, self._last_text))
        if self._export is not None:
            self._export.emit(
                ThinkingEvent(
                    cues=cues,
                    text="\n".join(part for part in raw if part),
                    ts=self._export.time_fn(),
                )
            )
        senselog.stage(
            STAGE,
            SOURCE,
            event,
            f"turn done rounds={self.rounds - before_rounds} "
            f"refusals={self.refusals - before_refusals} chars={len(self._last_text)}",
        )

    def _tool_loop(
        self, conversation: list[dict], raw: list[str], event: str
    ) -> _llm.TurnResult | None:
        """The bounded round loop. Returns the last result, or ``None`` if none ran."""
        result: _llm.TurnResult | None = None
        for round_index in range(self._max_tool_rounds):
            result = self._stream(
                conversation, role=ROLE_WORKER, tools=self._registry.tools(), raw=raw
            )
            if result is None:
                return None
            self.rounds += 1
            self._render_result(result, raw)
            self._emit_expression(result)
            if not result.tool_calls:
                if round_index == 0 and not result.content and not result.reasoning:
                    self._drop(REASON_SILENT_TURN, "no text, no reasoning, no tool call")
                return result
            conversation.append(_assistant_tool_message(result))
            for call in result.tool_calls:
                self._process_tool_call(call, conversation, raw)
        self._drop(REASON_TOOL_ROUNDS_EXHAUSTED, f"stopped after {self._max_tool_rounds} rounds")
        raw.append(f"[drop reason={REASON_TOOL_ROUNDS_EXHAUSTED}]")
        senselog.stage(STAGE, SOURCE, event, "tool loop bound reached")
        return result

    # ------------------------------------------------------------------ #
    # The perception question (the senses lane)                          #
    # ------------------------------------------------------------------ #

    def ask(
        self,
        prompt: str | list[dict],
        *,
        role: str = ROLE_SENSES,
        system: str | None = None,
        context: bool = True,
    ) -> str:
        """Ask one tool-less streaming question and return the answer text.

        This is the ``senses`` lane: a cheap perception question (describe this
        clip, is that a face) whose answer becomes a cue, not an action. It
        publishes no tools — the ONE no-tools request the layer makes, which is
        why lobes-cli#161 (a tool call on a no-tools request returns
        ``content: null``) can cost at most an empty answer here and never a
        lost action. It emits no export block: the feed is about turns.

        *prompt* is forwarded verbatim as the FINAL user message's
        ``content`` — plain text, or an OpenAI-style multimodal content LIST
        (one ``text`` part plus one ``video_url`` data-URI part; see
        :func:`reachy.cli._commands.agent.build_clip_question`, which builds
        that shape and stays OUTSIDE this module on purpose — this engine
        reads no file, per its own model-config claim machine-checked in
        ``tests/test_embody_engine.py``). No branching lives here: the OpenAI
        wire contract already accepts an arbitrary ``content`` list, and
        ``docs/evidence/2026-08-01-probe-video-wire-format.md`` (task t2)
        confirmed the deployed gateway decodes it correctly, streamed — so
        carrying a clip needed no change to this method beyond the type hint.
        The first real caller is the layer's clip poller (issue #139's h9:
        :class:`reachy.cli._commands.agent._ClipAsker`).

        Ahead of *prompt*, this is where Gemma's nested window lands (issue
        #154 decision c30 — see the module docstring's "Nested windows"
        section): the optional *system* message, then Qwen's summary (or its
        staleness marker, :meth:`_summary_message`), then what the background
        mind is working on (:meth:`_scope_message`, spec claim c8), then the
        last ``m`` turns of the ONE shared history (:meth:`_senses_window`)
        replayed exactly as :meth:`_build_messages` replays the worker's own
        turns. None of that touches *prompt* itself — it is still appended
        last, unmodified, which is what keeps this method's "reads no file,
        builds no clip payload, forwards content verbatim" contract true.

        *context* is how the ONE caller that must NOT see any of that asks for
        a clean sheet: the background mind's own summary maintenance pass
        (:class:`reachy.embody.summary.SummaryProducer`). Handed the layer's
        usual context it would be shown :data:`STALE_SUMMARY_MARKER` — the
        sentence saying the summary could not be refreshed — and would fold
        that sentence into the summary it was asked to write. With ``context =
        False`` the call is exactly ``[system?, user]``.
        """
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        if context:
            for extra in (self._summary_message(), self._scope_message()):
                if extra is not None:
                    messages.append(extra)
            messages.extend(self._history_messages(self._senses_window()))
        messages.append({"role": "user", "content": prompt})
        result = self._stream(messages, role=role, tools=None, raw=None)
        return (result.content if result is not None else "") or ""

    def _scope_message(self) -> dict | None:
        """The system-role message carrying the live cognition scopes, or ``None``.

        ``None`` when nothing is parked — an empty "the background mind is
        working on:" heading is noise in the one place the foreground is meant
        to skim. Every rendered scope is attributed and phrased as a suggestion
        (:meth:`reachy.embody.scope.CognitionScope.render`), which is what
        keeps spec claim c2 true at the level of the prompt itself: the
        background mind proposes, and the wording is still Gemma's.

        What can never appear here is the model's own private draft. The
        artifact carries no field for it, and ``tests/test_embody_scope.py``
        walks this method by AST to prove it does not so much as name one.
        """
        live = self.scopes
        if not live:
            return None
        body = "\n".join(scope.render() for scope in live)
        return {"role": "system", "content": f"{SCOPE_PREAMBLE}\n{body}"}

    # ------------------------------------------------------------------ #
    # Qwen's rolling summary of everything older than Gemma's window     #
    # (issue #154 decision c30) — the plumbing; task t12 builds the      #
    # producer that actually calls the LLM.                              #
    # ------------------------------------------------------------------ #

    def update_summary(self, text: str) -> bool:
        """Replace the summary of everything older than Gemma's ``m``-turn window.

        Called by whatever produces the summary — task t12's Qwen-backed
        producer in production, a plain string in a test, since this method
        makes no LLM call of its own and needs none to be exercised. Clears
        the stale marker on success (spec claim c45, honesty h30): a caller
        that successfully refreshed the summary is exactly the event that
        should make the marker go away.

        Fail-closed on length, mirroring :data:`reachy.behavior.rules.
        MAX_SAY_CHARS`'s idiom: text longer than
        :attr:`Limits.summary_max_chars` is REFUSED, never truncated, because
        silently cutting a Qwen-authored summary would put the cut point
        wherever this module happened to stop reading rather than somewhere
        Qwen's own producer chose. A blank summary is refused the same way an
        empty cue is (:data:`REASON_EMPTY_INPUT`) — "nothing to say yet" is
        not the same fact as "the summary is now empty" and a caller with
        nothing new should not call this at all.

        Returns whether the update was accepted.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            self._drop(REASON_EMPTY_INPUT, "summary")
            return False
        if len(cleaned) > self._summary_max_chars:
            self._drop(
                REASON_SUMMARY_TOO_LONG,
                f"{len(cleaned)} chars > {self._summary_max_chars}",
            )
            return False
        with self._summary_lock:
            self._summary = cleaned
            self._summary_stale = False
        return True

    def mark_summary_stale(self, detail: str = "") -> None:
        """Record that Qwen's summary maintenance pass failed this cycle.

        The worker LLM was unreachable, or the text it returned was itself
        refused by :meth:`update_summary` — either way, Gemma's context must
        not silently narrow to just the last ``m`` turns (spec claim c45,
        honesty h30). This method never clears or shrinks the summary already
        held; it only flags it, so :meth:`_summary_message` keeps showing the
        last known text, PREFIXED with :data:`STALE_SUMMARY_MARKER`, until the
        next :meth:`update_summary` call succeeds. Every call is a named,
        counted drop (:attr:`summary_stale_count`) — not only the first — the
        same discipline every other failure in this module follows.
        """
        with self._summary_lock:
            self._summary_stale = True
        self.summary_stale_count += 1
        self._drop(REASON_SUMMARY_STALE, detail)

    @property
    def summary_is_stale(self) -> bool:
        """Whether the last summary-maintenance attempt failed and has not yet been fixed."""
        with self._summary_lock:
            return self._summary_stale

    @property
    def summary(self) -> str:
        """The last summary text :meth:`update_summary` accepted (``""`` if none).

        The producer's own read-back: a ROLLING summary has to start from what
        it already said, or every pass rewrites the distant past from scratch
        and the compaction drifts.
        """
        with self._summary_lock:
            return self._summary

    @property
    def summary_max_chars(self) -> int:
        """The bound :meth:`update_summary` enforces, exposed for its producer.

        A producer that cannot read this bound has to restate it, and a
        restated bound is a second number to drift — the same reasoning that
        makes :mod:`reachy.embody.tools` import
        :data:`reachy.behavior.rules.MAX_SAY_CHARS` rather than repeat it.
        """
        return self._summary_max_chars

    def history(self) -> list[tuple[str, str]]:
        """A snapshot of the ONE shared conversation deque, oldest turn first.

        The whole ``n``-turn window both lanes read
        (:meth:`_build_messages` replays it in full, :meth:`_senses_window`
        takes its tail) — copied under the history lock so a caller iterating
        it can never race the turn thread's append.
        """
        with self._history_lock:
            return list(self._history)

    def backlog(self) -> list[tuple[str, str]]:
        """The turns OLDER than Gemma's ``m``-turn window — the summary's territory.

        The complement of :meth:`_senses_window` over the same deque
        (issue #154 decision c30): what Gemma no longer replays verbatim is
        exactly what Qwen's rolling summary has to carry, so
        :class:`reachy.embody.summary.SummaryProducer` reads this and nothing
        else. Deriving it here rather than in the producer keeps ONE definition
        of "older than the window" — two would eventually disagree about which
        turns are covered, and the ones they disagreed about would be the ones
        silently lost when the deque drops them at ``n``.
        """
        pairs = self.history()
        if self._senses_history_maxlen <= 0:
            return pairs
        return pairs[: max(0, len(pairs) - self._senses_history_maxlen)]

    def _summary_message(self) -> dict | None:
        """The system-role message carrying Qwen's summary, or its staleness marker.

        ``None`` only for an engine that has never had a summary AND has
        never gone stale — nothing to show and nothing to explain is missing.
        Once either has happened this always returns something: an up-to-date
        summary renders as-is, and a stale one is PREFIXED with
        :data:`STALE_SUMMARY_MARKER` rather than replaced by it — the last
        known summary is still useful context even while its freshness cannot
        be vouched for, and dropping it entirely on top of being stale would
        be the exact silent narrowing spec claim c45 refuses.
        """
        with self._summary_lock:
            summary, stale = self._summary, self._summary_stale
        if not summary and not stale:
            return None
        if not stale:
            text = summary
        elif summary:
            text = f"{STALE_SUMMARY_MARKER} Last known summary: {summary}"
        else:
            text = STALE_SUMMARY_MARKER
        return {"role": "system", "content": text}

    # ------------------------------------------------------------------ #
    # The canonical history's projections onto the realtime floor         #
    # (decision c27, task t11)                                            #
    # ------------------------------------------------------------------ #

    def floor_reseed(self) -> list[FloorItem]:
        """Everything a NEW floor session must be told, projected from ONE record.

        The re-seed seam
        (:class:`reachy.speech.realtime_duplex.Reseed`) production hands the
        duplex client. A session close wipes the floor's ephemeral history
        (lobes ``_session.py``'s ``teardown`` empties ``_history`` — close
        "releases it all"), so a reconnect that armed without re-seeding would
        let the gateway answer the next turn out of nothing: Gemma silently
        reset to amnesia, with no line in any log saying so (spec claim c40).
        The ORDERING that prevents it is the wire's, guaranteed structurally
        inside its ``session.created`` handling; the CONTENT is this method's,
        and that split is why the seam exists at all.

        **This is the third reader of the ONE canonical history, not a second
        copy of it.** The turns come from :meth:`_senses_window` — Gemma's own
        ``m``-window, a strict suffix of Qwen's ``n`` over the same deque
        (decision c30) — rendered by :meth:`_history_messages`, the ONE
        renderer both lanes already use. Nothing is re-derived here, so a
        stored turn cannot reach the floor rendered a third way, and a future
        "re-seed cache" would be exactly the second, independently-maintained
        history #154 warned about.

        **The two dispositions, and why each artifact gets the one it does.**

        * Qwen's rolling summary of everything older (:meth:`_summary_message`,
          carrying :data:`STALE_SUMMARY_MARKER` when the last maintenance pass
          failed) rides as ONE ephemeral **context** item. It is not a turn
          anybody took, and it is REGENERATED — appending each new version as a
          history turn would leave the floor holding every superseded one.
        * The ``m``-window rides as curated **history** turns, because that is
          what they are: the conversation the floor lost when the socket
          dropped.

        **Bounded by what already bounds the lanes**, never by a number of its
        own: :attr:`Limits.senses_history_maxlen` caps the turns and
        :attr:`Limits.summary_max_chars` caps the summary, both enforced where
        they live (an over-length summary is refused by
        :meth:`update_summary` and so never reaches here at all).

        Safe from any thread and never raises: it runs on the session's worker
        thread, inside ``session.created`` handling, where an escaping
        exception would take the session down and reconnect straight back into
        the same fault. Returns ``[]`` for a conversation that has not started
        — nothing to re-seed is not a failure.
        """
        items: list[FloorItem] = []
        summary = self._summary_message()
        if summary is not None:
            items.append(
                FloorItem(
                    role=ITEM_ROLE_SYSTEM,
                    text=str(summary["content"]),
                    disposition=ITEM_DISPOSITION_CONTEXT,
                )
            )
        items.extend(
            FloorItem(
                role=str(message["role"]),
                text=str(message["content"]),
                disposition=ITEM_DISPOSITION_HISTORY,
            )
            for message in self._history_messages(self._senses_window())
        )
        return items

    def floor_correction(self, split: _SpokenSplitLike) -> FloorItem | None:
        """The item that tells the floor what the room ACTUALLY heard of a cut reply.

        **Spec claim c39, closing.** A client-local cut is invisible to the
        floor: wire delivery completed at wire speed, so the server sent
        ``response.done`` and appended the WHOLE reply to its own history. Its
        record therefore OVERSTATES what the room heard after every cut, while
        the layer's own record — narrowed by :meth:`note_interrupted_reply` —
        is right, because the client is the measured authority for what its
        sink played. Until task t10 there was no frame that could carry the
        difference. There is one now, and this builds what goes in it.

        **It APPENDS; it does not rewrite.** The schema has no operation for
        editing a turn the floor already stored, so this is a
        ``history``-disposition item that says, in words the reading model can
        act on, that the previous reply was cut and how much of it was spoken.
        Two consequences worth stating rather than discovering: the raw
        overstated turn is still in the floor's history (nothing here claims
        the two records now agree — only that the floor has been TOLD), and the
        disposition is ``history`` rather than ``context`` on purpose, because
        a correction that evaporated after one generate call would let the
        overstatement come straight back on the next turn.

        Where the gateway announced no conversation-item support the push is
        declined by :meth:`reachy.speech.realtime_duplex.RealtimeDuplexSession.
        send_item` — one named, latched drop, the c44/h29 degrade — and the
        overstatement simply remains, documented rather than papered over.

        Returns ``None`` when there is nothing to correct: the reply the room
        heard whole (``unsaid`` empty) leaves the floor's record already true,
        and an object this method cannot read structurally is a ``None`` too
        rather than an exception, since it is called from a session tap on a
        worker thread.

        Its size is bounded by the reply it corrects, which the floor already
        stored — so this adds no growth the conversation did not already have.
        """
        said = (getattr(split, "said", "") or "").strip()
        unsaid = (getattr(split, "unsaid", "") or "").strip()
        if not unsaid:
            return None
        text = FLOOR_CORRECTION_PARTIAL.format(said=said) if said else FLOOR_CORRECTION_NOTHING
        return FloorItem(
            role=ITEM_ROLE_SYSTEM,
            text=text,
            disposition=ITEM_DISPOSITION_HISTORY,
        )

    def _senses_window(self) -> list[tuple[str, str]]:
        """Gemma's window: the last ``senses_history_maxlen`` entries of the ONE shared deque.

        A STRICT SUFFIX, taken by slicing — never a second history collected
        independently (issue #154 decision c30). ``Limits.__post_init__``
        already guarantees ``senses_history_maxlen <= history_maxlen``, so
        this can never ask for more turns than the deque itself retains.
        """
        if self._senses_history_maxlen <= 0:
            return []
        with self._history_lock:
            return list(self._history)[-self._senses_history_maxlen :]

    # ------------------------------------------------------------------ #
    # The one streaming call                                             #
    # ------------------------------------------------------------------ #

    def _stream(
        self,
        messages: list[dict],
        *,
        role: str,
        tools: list[dict] | None,
        raw: list[str] | None,
    ) -> _llm.TurnResult | None:
        """One streamed call. Every failure is a NAMED drop and a ``None``, never a raise.

        ``timeout`` is the socket deadline, which applies PER READ — that is what
        makes :data:`REASON_STREAM_IDLE` an inter-chunk bound rather than a total
        one. See the module docstring; this is honesty condition h6.
        """
        kwargs: dict = {
            "model": self._models.model_for(role),
            "temperature": self._temperature,
            "timeout": self._idle_timeout_s,
            "base_url": self._base_url,
            "api_key": self._api_key,
            "on_content": self._on_content,
            "on_reasoning": self._on_reasoning,
            "enable_thinking": self._enable_thinking,
            "cancel": self._cancel,
        }
        if tools is not None:
            kwargs["tools"] = tools
        if self._max_tokens is not None:
            kwargs["max_tokens"] = self._max_tokens

        try:
            return self._turn_fn(messages, **kwargs)
        except TimeoutError:
            # socket.timeout IS TimeoutError, and it fires per READ: this is a
            # gap BETWEEN chunks, not a slow turn. Must precede the OSError arm.
            self.stream_timeouts += 1
            return self._fail(
                REASON_STREAM_IDLE,
                f"no delta for {self._idle_timeout_s:g}s on the {role} lane",
                raw,
            )
        except CliError as err:
            self.stream_failures += 1
            return self._fail(REASON_ENDPOINT_UNREACHABLE, err.message, raw)
        except OSError as err:
            self.stream_failures += 1
            return self._fail(REASON_STREAM_FAILED, f"{type(err).__name__}: {err}", raw)
        except Exception as err:  # a bad turn must never kill the layer
            self.stream_failures += 1
            logger.warning("[embody] %s turn raised", role, exc_info=True)
            return self._fail(REASON_STREAM_FAILED, f"{type(err).__name__}: {err}", raw)

    def _fail(self, reason: str, detail: str, raw: list[str] | None) -> None:
        self._drop(reason, detail)
        if raw is not None:
            raw.append(f"[drop reason={reason} {detail}]")
        return None

    # ------------------------------------------------------------------ #
    # Messages                                                           #
    # ------------------------------------------------------------------ #

    def _build_user_content(
        self, triggers: list[str], context: list[str], spoken: list[str]
    ) -> str:
        """The turn's perception, with the background kept visibly separate.

        Two sections rather than one list: what made the robot think, then what
        has merely been going on around it. Folded together, a coalesced
        ``"speech from the left (x145)"`` reads to the model exactly like the
        rule fire that actually woke it up.
        """
        lines = ["I just perceived:"]
        lines.extend(f"- {line}" for line in triggers)
        if context:
            lines.append("Meanwhile, in the background:")
            lines.extend(f"- {line}" for line in context)
        if spoken:
            lines.append("I have already said out loud:")
            lines.extend(f'- "{said}"' for said in spoken)
        return "\n".join(lines)

    def _build_messages(self, user_content: str) -> list[dict]:
        """System prompt + the worker's FULL ``n``-turn rolling history + the current perception.

        Reads the ONE shared history the senses lane's :meth:`_senses_window`
        also reads (issue #154 decision c30) — this replays it whole, up to
        ``n``, where that method takes only the tail ``m``.
        """
        messages: list[dict] = [{"role": "system", "content": self._system_prompt}]
        with self._history_lock:
            pairs = list(self._history)
        messages.extend(self._history_messages(pairs))
        messages.append({"role": "user", "content": user_content})
        return messages

    @staticmethod
    def _history_messages(pairs: Iterable[tuple[str, str]]) -> list[dict]:
        """Turn stored ``(user, reply)`` pairs into alternating user/assistant messages.

        Shared by both windows onto the one history (issue #154 decision c30)
        — the worker's full replay in :meth:`_build_messages` and Gemma's tail
        slice in :meth:`ask` — so the two lanes can never render the same
        stored turn two different ways.
        """
        messages: list[dict] = []
        for prior_user, prior_reply in pairs:
            messages.append({"role": "user", "content": prior_user})
            if prior_reply.strip():
                messages.append({"role": "assistant", "content": prior_reply})
        return messages

    def _drain_triggers(self) -> list[Input]:
        """Take every pending trigger at once.

        Draining WHOLE is the alert coalescer: ten rule fires waiting together
        become one turn's perception list, never ten turns.
        """
        with self._intake_lock:
            items = list(self._triggers)
            self._triggers.clear()
        return items

    def _drain_context(self) -> list[Parked]:
        """Take the parked facts this turn will show: DRAIN the cues, PEEK the perception.

        The two coalescing keys have different LIFETIMES since task t13, and
        this method is where that split is made concrete. Exact-text cue
        facts (:attr:`_context`) describe something that HAPPENED — a rule
        fire, a face appearing — and a happening does not stay true, so they
        are drained rather than snapshotted-and-kept: carrying one forward
        would make every later turn re-read the same background, the failure
        :meth:`_drain_spoken` avoids one buffer over. Latest-wins
        :class:`PerceptionSlot`\\ s (:attr:`_perception`) describe a STATE —
        "what the camera currently shows" — and a state stays true until it
        is superseded or stale, so they are only PEEKED here
        (:meth:`_live_perception`, which does the staleness eviction) and
        never cleared by the act of being read: a turn between two 20 s clip
        polls now sees the room exactly as the last poll described it,
        closing issue #153's "asked what it can see, the robot says it
        cannot" — instead of the old behaviour, where only the ONE turn that
        happened to run right after a poll ever saw it.
        """
        with self._intake_lock:
            taken = list(self._context.values())
            self._context.clear()
        return taken + self._live_perception()

    def _live_perception(self) -> list[PerceptionSlot]:
        """The perception park's LIVE slots, evicting any that have gone stale.

        Mirrors :attr:`scopes`/:attr:`wanted_to_say`: expiry is filtered on
        READ rather than swept on a timer, so a caller never sees a stale
        slot and the park stays bounded regardless. The clock and the bound
        are :attr:`_now` and :attr:`_perception_stale_after_s` — the SAME
        clock the alert interval and the attention gate already share, and
        the SAME staleness rule :class:`~reachy.cli._commands.agent.
        _ClipAsker` already applies once at ask time (see
        :data:`DEFAULT_PERCEPTION_STALE_AFTER_S`), just re-checked here at
        read time. Unlike a scope's turn-counted expiry, this is a genuine
        elapsed-seconds check: a camera snapshot's staleness is about how
        long ago the frame was captured, not how many turns have run since.

        An evicted slot is a NAMED drop (:data:`REASON_PERCEPTION_STALE`) —
        the removal from the dict IS the dedup: a source that goes stale
        reports exactly once, because it cannot be found (and re-evicted)
        again until a fresh :meth:`~EmbodyTurnEngine.submit_perception` call
        re-populates it.
        """
        now = self._now()
        evicted: list[tuple[str, float]] = []
        with self._intake_lock:
            fresh: dict[str, PerceptionSlot] = {}
            for source, slot in self._perception.items():
                if slot.is_stale(now, stale_after_s=self._perception_stale_after_s):
                    captured_at = slot.snapshot.captured_at if slot.snapshot is not None else now
                    evicted.append((source, now - captured_at))
                    continue
                fresh[source] = slot
            if evicted:
                self._perception = fresh
            result = list(fresh.values())
        for source, age in evicted:
            self._drop(
                REASON_PERCEPTION_STALE,
                f"source={source} age={age:.1f}s > {self._perception_stale_after_s:g}s",
            )
        return result

    def _drain_spoken(self) -> list[str]:
        """Take the already-spoken lines this turn will carry, emptying the buffer.

        Drained rather than read-then-cleared: a ``note_spoken`` landing between
        the read and the clear would otherwise be swallowed without ever having
        been shown to the model — a lost update that presents as the robot
        repeating itself, which is the exact failure this buffer exists to
        prevent.
        """
        taken: list[str] = []
        while self._spoken:
            taken.append(self._spoken.popleft())
        return taken

    # ------------------------------------------------------------------ #
    # Tool dispatch + export                                             #
    # ------------------------------------------------------------------ #

    def _process_tool_call(
        self, call: _llm.ToolCall, conversation: list[dict], raw: list[str]
    ) -> None:
        """Export the call's block, dispatch it, and feed the RESULT back in.

        The export comes first and independently of the dispatch outcome — cited
        from :meth:`reachy.speech.agent_turn.AgentTurnEngine._process_tool_call`,
        and matching ``docs/export-schema.md``'s "intent, not proof" semantics.
        The result (a refusal included, verbatim, with its name) is appended to
        the conversation, so the model learns in the SAME turn that the validator
        said no, and to the raw text, so the feed shows it too.
        """
        self.tool_calls += 1
        if self._export is not None and call.name in self._voice_tools:
            text = call.arguments.get("text")
            if isinstance(text, str) and text.strip():
                self._export.emit(MessageEvent(text=text, ts=self._export.time_fn()))

        message = self._registry.dispatch(call.name, call.arguments_json, call.id)
        refusal = _refusal_name(message)
        if refusal is not None:
            self.refusals += 1
        raw.append(f"-> {message.get('content')}")
        conversation.append(message)

    def _emit_expression(self, result: _llm.TurnResult) -> None:
        """Emit an ``emotion`` block when the model's own reply shows a face."""
        if self._export is None:
            return
        emoji = first_emoji(result.content or "")
        if emoji is None:
            return
        resolver = self._export.pose_resolver
        pose = resolver(emoji) if resolver is not None else None
        self._export.emit(EmotionEvent(emoji=emoji, pose=pose, ts=self._export.time_fn()))

    @staticmethod
    def _render_result(result: _llm.TurnResult, raw: list[str]) -> None:
        """Append one round's raw text: reasoning, content, then each tool call."""
        if result.reasoning:
            raw.append(result.reasoning)
        if result.content:
            raw.append(result.content)
        for call in result.tool_calls:
            raw.append(f"{call.name}({call.arguments_json})")

    # ------------------------------------------------------------------ #
    # The thin loop                                                      #
    # ------------------------------------------------------------------ #

    def run(
        self,
        *,
        max_turns: int | None = None,
        stop: Callable[[], bool] | None = None,
        before_turn: Callable[[], None] | None = None,
    ) -> int:
        """Run turns until stopped; returns how many RAN. Shape cited from the agent engine.

        Args:
            max_turns: stop after this many turns that actually ran.
            stop: zero-arg predicate checked before each turn.
            before_turn: called at the top of each iteration — how a composition
                root pumps freshly-read cues in before the turn reads them.
        """
        ran = 0
        first = True
        while True:
            if stop is not None and stop():
                break
            if max_turns is not None and ran >= max_turns:
                break
            if before_turn is not None:
                before_turn()
            if not first:
                self._sleep(self._turn_interval)
            first = False
            if self.run_turn():
                ran += 1
            elif before_turn is None and stop is None and max_turns is not None:
                # Nothing produces input and nothing will: stop rather than spin.
                break
        return ran

    # ------------------------------------------------------------------ #
    # Small helpers                                                      #
    # ------------------------------------------------------------------ #

    def _drop(self, reason: str, detail: str = "") -> None:
        senselog.drop(
            STAGE, SOURCE, uuid.uuid4().hex[:8], f"{reason} ({detail})" if detail else reason
        )


def _never() -> bool:
    return False


def _refusal_name(message: dict) -> str | None:
    """The ``refusal`` name in a tool result, or ``None`` when it was performed."""
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict) and payload.get("ok") is False:
        name = payload.get("refusal")
        return name if isinstance(name, str) else "unnamed-refusal"
    return None


def _assistant_tool_message(result: _llm.TurnResult) -> dict:
    """The OpenAI assistant message carrying this round's tool calls.

    Appended before the tool results so the next round sees its own calls paired
    with their outcomes (the OpenAI tool protocol) — cited from
    :func:`reachy.speech.agent_turn._assistant_tool_message`.
    """
    return {
        "role": "assistant",
        "content": result.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in result.tool_calls
        ],
    }
