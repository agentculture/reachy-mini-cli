"""Tests for the sparse, marker-driven :class:`ExpressionProducer` (t5).

The producer is the motion-integration core for ``think``'s expressive movement:
an LLM expression marker (``*🤔*``) maps to **exactly one** calm, low-amplitude
:class:`~reachy.motion.queue.MotionAction` pushed onto the *existing* serial
goto/minjerk motion queue.  Stillness is the thinking posture, so a move is
emitted **only** on an expression marker — never per sentence — and each move is
deliberately small so it stands out against the stillness.

All tests are pure: the producer takes a real (in-memory) :class:`MotionQueue`
and a real :class:`Catalog`; no robot, daemon, or transport is involved.
"""

from __future__ import annotations

from reachy.motion.expression import EXPRESSION_DURATION, ExpressionProducer
from reachy.motion.queue import EXPRESSION_KEY, IDLE_KEY, MotionAction, MotionQueue
from reachy.speech.expressions import Catalog, get_pose
from reachy.speech.markers import MarkerEvent, SpeechEvent, parse

# --------------------------------------------------------------------------- #
# AC1 — a marker maps to EXACTLY ONE MotionAction on the existing queue        #
# --------------------------------------------------------------------------- #


def test_marker_enqueues_exactly_one_motion_action() -> None:
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    prod.express("🤔")
    assert len(q) == 1, "a single marker must enqueue exactly one MotionAction"
    assert isinstance(q.peek(), MotionAction)


def test_express_returns_the_enqueued_action() -> None:
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    action = prod.express("🤔")
    assert action is q.peek(), "express() returns the same action it enqueued"


def test_action_carries_the_catalog_pose_for_the_emoji() -> None:
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    a = prod.express("🤔")
    pose = get_pose("🤔")
    # Head dict mapping: ExpressionPose.as_head_dict() → MotionAction.head, verbatim.
    assert a.head == pose.as_head_dict()
    # Antennas tuple mapping: (right, left), verbatim.
    assert a.antennas == pose.as_antennas_tuple()
    # body_yaw scalar mapping, verbatim.
    assert a.body_yaw == pose.body_yaw


def test_uses_the_existing_motion_path_minjerk_goto() -> None:
    # Reuses the existing serial goto/minjerk path — a plain MotionAction with the
    # standard "minjerk" interpolation. No new motion channel, no transport.move_* call.
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    a = prod.express("😮")
    assert a.interpolation == "minjerk"


def test_unknown_emoji_falls_back_to_neutral_pose() -> None:
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    a = prod.express("\U0001f984")  # a unicorn — not in the catalog
    neutral = get_pose("\U0001f984")  # → neutral all-zeros
    assert a.head == neutral.as_head_dict()
    assert a.antennas == (0.0, 0.0)
    assert a.body_yaw == 0.0


# --------------------------------------------------------------------------- #
# AC2 — sparse: ≤ N moves for N markers; speech produces NO motion             #
# --------------------------------------------------------------------------- #


def test_speech_events_produce_no_motion() -> None:
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    n = prod.consume([SpeechEvent(text="I wonder what that was."), SpeechEvent(text="Hmm.")])
    assert n == 0, "speech events must never enqueue a move"
    assert len(q) == 0


def test_consume_emits_one_move_per_marker_not_per_sentence() -> None:
    # A realistic marked stream: two markers, several sentences. The robot moves on the
    # markers ONLY — sentences are silent (stillness is the thinking posture).
    stream = '*🤔* "I wonder what that sound was." "It came from the left." *👂* "There it is."'
    events = parse(stream)
    markers = [e for e in events if isinstance(e, MarkerEvent)]
    speeches = [e for e in events if isinstance(e, SpeechEvent)]
    assert len(markers) == 2 and len(speeches) == 3  # guard the fixture

    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    moves = prod.consume(events)
    # AT MOST N moves for N markers (here the markers differ, so exactly N) — and crucially
    # FAR fewer than the sentence count: motion is driven by markers, not speech.
    assert moves == len(markers)
    assert moves < len(events), "not one move per event/sentence"


def test_consume_is_sparse_under_repeated_markers_at_most_n() -> None:
    # Five markers fed before any drains → at most 5 expression moves, never more.
    # (Identical markers queued before either executes coalesce to the latest under
    # EXPRESSION_KEY, so the count is ≤ N — the sparse / rate-limited property.)
    events = [MarkerEvent(emoji="🤔") for _ in range(5)]
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    moves = prod.consume(events)
    assert moves == 5  # express() was called once per marker (each returns one action)
    assert len(q) <= 5, "pending moves never exceed the marker count (coalescing keeps it sparse)"


def test_repeated_markers_coalesce_to_a_single_pending_move() -> None:
    # The queue's EXPRESSION_KEY coalescing means a burst of markers queued before the
    # executor drains collapses to ONE pending move — sparse against stillness.
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    for _ in range(4):
        prod.express("🤔")
    assert len(q) == 1, "bursted markers coalesce to a single pending expression move"


# --------------------------------------------------------------------------- #
# AC3 — calm, low-amplitude: catalog poses unscaled, modest duration           #
# --------------------------------------------------------------------------- #


def test_pose_is_not_amplified() -> None:
    # The catalog poses are already calm/low-amplitude; the producer must use them AS-IS
    # (no scaling up). Every axis equals the catalog value exactly.
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    a = prod.express("🎉")  # the largest-amplitude catalog entry
    pose = get_pose("🎉")
    assert a.head == pose.as_head_dict()
    assert a.antennas == pose.as_antennas_tuple()
    assert a.body_yaw == pose.body_yaw


def test_duration_is_calm_and_default() -> None:
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    a = prod.express("🙂")
    # A modest, deliberate default duration (on the same calm scale as listen's min_dur).
    assert a.duration == EXPRESSION_DURATION
    assert 1.0 <= EXPRESSION_DURATION <= 3.0, "duration is calm — not snappy, not a crawl"


def test_duration_override_is_respected() -> None:
    q = MotionQueue()
    prod = ExpressionProducer(queue=q, duration=2.0)
    a = prod.express("🙂")
    assert a.duration == 2.0


# --------------------------------------------------------------------------- #
# coalesce-key contract — expression supersedes idle, queues alongside reacts  #
# --------------------------------------------------------------------------- #


def test_expression_action_uses_the_expression_coalesce_key() -> None:
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    a = prod.express("🤔")
    assert a.coalesce_key == EXPRESSION_KEY


def test_expression_supersedes_a_pending_idle_pose() -> None:
    # A deliberate expression gesture must preempt background idle motion.
    q = MotionQueue()
    q.submit(MotionAction(label="idle-1", head={"yaw": 0.0}, coalesce_key=IDLE_KEY))
    prod = ExpressionProducer(queue=q)
    prod.express("🤔")
    assert [a.coalesce_key for a in q.pending()] == [EXPRESSION_KEY]


# --------------------------------------------------------------------------- #
# construction — custom catalog injection (how t7 will drive it)              #
# --------------------------------------------------------------------------- #


def test_accepts_an_injected_catalog() -> None:
    q = MotionQueue()
    cat = Catalog()
    prod = ExpressionProducer(queue=q, catalog=cat)
    a = prod.express("👂")
    assert a.head == cat.get("👂").as_head_dict()


def test_on_marker_is_an_alias_for_express() -> None:
    q = MotionQueue()
    prod = ExpressionProducer(queue=q)
    a = prod.on_marker(MarkerEvent(emoji="😮"))
    assert a is q.peek() and a.coalesce_key == EXPRESSION_KEY
