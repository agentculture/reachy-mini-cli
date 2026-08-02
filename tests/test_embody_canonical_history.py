"""ONE canonical history, and the three things it feeds (decision c27, task t11).

Decision **c27** made the LAYER the curator of the conversation record: it
already receives every utterance and every reply text over the duplex wire, so
lobes' own server-side history becomes *what the layer put there* rather than a
second account of the same conversation. The failure that decision exists to
prevent is the one issue #154 named — two independently-maintained histories
drift, and then the two models disagree about what was said, which is the worst
thing that can happen to a robot with one voice.

This module pins the three acceptance criteria of task t11:

1. **One source of truth.** The engine's ONE ``_history`` deque feeds Gemma's
   ``m``-window (a strict suffix), Qwen's ``n``-window, and — new here — the
   content of the floor re-seed. Pinned three ways: behaviourally (a turn
   appended once shows up in all three), structurally (the module declares
   exactly one turn deque), and by AST over
   :meth:`~reachy.embody.engine.EmbodyTurnEngine.floor_reseed` itself, which
   may read only the projections both lanes already share.
2. **Context items and history turns stay distinct.** Verified against
   ``tests/fake_realtime_server.py``'s implementation of the lobes-cli#170
   schema — the summary rides as an EPHEMERAL context item, the ``m``-window
   as curated HISTORY turns, and the harness sorts them by ``disposition`` so a
   client that never made the distinction cannot pass. Honesty h23 asks for
   exactly this, BEFORE the layer sends its first item live.
3. **The correction-after-cut.** Spec claim c39 recorded a phase-1 limitation:
   a client-local cut is invisible to the floor, so the server records the FULL
   reply as heard and OVERSTATES it. With items the layer can finally say what
   the room actually got — as a HISTORY-disposition item, because a correction
   that evaporated after one generate call would let the overstatement come
   straight back.

What this module does NOT claim is as important as what it does. The correction
APPENDS; nothing here rewrites the turn the floor already stored, because the
schema has no operation for that. Where the gateway announced no item support
at all, the overstatement simply remains — one named, latched drop from the
session and the connect-time ``system_prompt`` context task t9 wired, exactly
the c44/h29 degrade. Neither state is dressed up as a fix.

The composition half — that the seams are actually WIRED into
``_compose_embody_seam``, which is where two earlier capabilities in this arc
shipped with no caller — lives in ``tests/test_agent_embody.py`` beside the
rest of the composition pins.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from reachy.embody import engine as engine_mod
from reachy.embody.engine import STALE_SUMMARY_MARKER, EmbodyModels, EmbodyTurnEngine, FloorItem
from reachy.speech import realtime_duplex as duplex
from reachy.speech import realtime_wire as wire
from reachy.speech.llm import TurnResult
from reachy.speech.realtime_duplex import ConversationItem, RealtimeDuplexSession
from tests.fake_realtime_server import FakeRealtimeServer, Scenario

_ENGINE_SOURCE = Path(engine_mod.__file__)
_TIMEOUT = 10.0
_RATE = 16000


# --------------------------------------------------------------------------- #
# Doubles                                                                     #
# --------------------------------------------------------------------------- #


class _ScriptedTurn:
    """A ``turn_fn`` double returning one canned turn and recording every call."""

    def __init__(self, content: str = "ok") -> None:
        self._content = content
        self.calls: list[dict] = []

    def __call__(self, messages: list[dict], **kwargs) -> TurnResult:
        self.calls.append({"messages": copy.deepcopy(messages), "kwargs": kwargs})
        return TurnResult(content=self._content, tool_calls=[], finish_reason="stop")

    def last_messages(self) -> list[dict]:
        return self.calls[-1]["messages"]


class _Registry:
    """An :class:`~reachy.embody.tools.EmbodyToolRegistry`-shaped double."""

    def tools(self) -> list[dict]:
        return []

    def dispatch(self, name, arguments_json=None, tool_call_id=None) -> dict:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps({"ok": True})}


@dataclass(frozen=True)
class _StubSplit:
    """The structural shape :meth:`EmbodyTurnEngine.floor_correction` reads.

    The real object is :class:`reachy.speech.realtime_duplex.SpokenSplit`; the
    engine types it as a Protocol so it never imports the WebSocket client, and
    this double keeps these unit checks honest about that boundary.
    """

    response_id: str
    text: str
    said: str
    unsaid: str


_LIMIT_FIELDS = {field.name for field in dataclasses.fields(engine_mod.Limits)}

_CUT_TEXT = "one two three four five six"
_CUT_SAID = "one two three"
_CUT_UNSAID = "four five six"


def _build(**kwargs) -> EmbodyTurnEngine:
    """An engine with faked collaborators and its attention window already open."""
    kwargs.setdefault("registry", _Registry())
    kwargs.setdefault("turn_fn", _ScriptedTurn())
    kwargs.setdefault("models", EmbodyModels(worker="worker", senses="senses"))
    limit_kwargs = {name: kwargs.pop(name) for name in list(kwargs) if name in _LIMIT_FIELDS}
    if limit_kwargs:
        kwargs.setdefault("limits", engine_mod.Limits(**limit_kwargs))
    engine = EmbodyTurnEngine(**kwargs)
    engine.attention.note_addressed()
    return engine


def _talk(engine: EmbodyTurnEngine, turns: int, *, first: int = 0) -> None:
    for index in range(first, first + turns):
        engine.submit_utterance(f"turn {index}")
        engine.run_turn()


def _split(
    said: str = _CUT_SAID,
    unsaid: str = _CUT_UNSAID,
    *,
    text: str = _CUT_TEXT,
    response_id: str = "resp_cut",
) -> _StubSplit:
    return _StubSplit(response_id=response_id, text=text, said=said, unsaid=unsaid)


def _history_items(items: list[FloorItem]) -> list[FloorItem]:
    return [item for item in items if item.disposition == wire.ITEM_DISPOSITION_HISTORY]


def _context_items(items: list[FloorItem]) -> list[FloorItem]:
    return [item for item in items if item.disposition == wire.ITEM_DISPOSITION_CONTEXT]


def _method_ast(name: str) -> ast.FunctionDef:
    tree = ast.parse(_ENGINE_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from {_ENGINE_SOURCE.name}")


def _self_attributes(node: ast.AST) -> set[str]:
    """Every ``self.<name>`` *node* touches, in any syntactic position."""
    found: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "self"
        ):
            found.add(child.attr)
    return found


def _wait_until(predicate, timeout: float = _TIMEOUT, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _count_reason(caplog: pytest.LogCaptureFixture, reason: str) -> int:
    needle = f"reason={reason}"
    return sum(1 for record in caplog.records if needle in record.getMessage())


# =========================================================================== #
# Criterion 1 — ONE source of truth, three projections                       #
# =========================================================================== #


def test_c27_the_floor_reseed_is_a_projection_of_the_deque_both_lanes_read() -> None:
    """The headline claim, stated behaviourally: one append, three projections move.

    A second, independently-maintained record would satisfy any one of these
    assertions on its own. Only asking all three about the SAME new turn — the
    worker's full replay, Gemma's tail slice, and what a reconnect would tell
    the floor — can tell one history from two that happen to agree today.
    """
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, history_maxlen=5, senses_history_maxlen=3)
    _talk(engine, 4)

    before = [item.text for item in _history_items(engine.floor_reseed())]
    engine.submit_utterance("a brand new sentence")
    engine.run_turn()
    after = [item.text for item in _history_items(engine.floor_reseed())]

    assert after != before, "the re-seed did not follow the canonical history"
    assert any("a brand new sentence" in text for text in after)

    # And the same turn reached both lanes, off the same deque.
    worker_users = [m["content"] for m in turn.last_messages() if m["role"] == "user"]
    assert any("a brand new sentence" in text for text in worker_users)
    engine.ask("what's going on?")
    senses_users = [m["content"] for m in turn.last_messages() if m["role"] == "user"]
    assert any("a brand new sentence" in text for text in senses_users)


def test_c27_the_reseed_history_turns_are_exactly_gemmas_m_window() -> None:
    """The re-seed carries Gemma's window, not Qwen's — the floor speaks AS Gemma.

    Pinned as an EQUALITY against the messages ``ask()`` itself replays, so the
    two cannot drift into "nearly the same" without failing.
    """
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, history_maxlen=6, senses_history_maxlen=2)
    _talk(engine, 5)

    engine.ask("what's going on?")
    gemma = [
        (message["role"], message["content"])
        for message in turn.last_messages()
        if message["role"] in ("user", "assistant")
    ][:-1]

    projected = [(item.role, item.text) for item in _history_items(engine.floor_reseed())]
    assert projected == gemma
    assert len(projected) == 2 * 2, "m=2 turns, each a user + an assistant message"


def test_c27_gemmas_reseed_window_stays_a_strict_suffix_of_qwens() -> None:
    """m <= n over ONE deque (spec c3/decision c30) survives the third reader."""
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, history_maxlen=6, senses_history_maxlen=2)
    _talk(engine, 5)

    projected = [item.text for item in _history_items(engine.floor_reseed())]
    qwen = [message["content"] for message in turn.last_messages() if message["role"] == "user"]

    # Every projected user line is one of Qwen's, and they are its tail.
    projected_users = [text for text in projected if text in qwen]
    assert projected_users == qwen[-2:]


def test_c27_the_engine_declares_exactly_one_conversation_deque() -> None:
    """Structural half of "no second independently-maintained history".

    The behavioural test above can only prove that today's projections agree.
    This one proves there is nothing else to disagree WITH: exactly one
    ``deque[tuple[str, str]]`` is declared in the whole module, and it is
    ``_history``. A future re-seed cache added "for speed" fails here first.
    """
    source = _ENGINE_SOURCE.read_text(encoding="utf-8")
    declared = [
        ast.unparse(node.target)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AnnAssign)
        and "deque[tuple[str, str]]" in ast.unparse(node.annotation)
    ]
    assert declared == ["self._history"], declared


def test_c27_floor_reseed_reads_only_the_projections_both_lanes_already_share() -> None:
    """AST: the re-seed may compose the shared views, never re-derive them.

    ``_senses_window`` is Gemma's window and ``_history_messages`` is the ONE
    renderer both lanes use, so a re-seed built from them cannot render a
    stored turn a third way. Reading ``_history`` directly here would be the
    first step towards exactly that.
    """
    touched = _self_attributes(_method_ast("floor_reseed"))
    assert "_senses_window" in touched
    assert "_history_messages" in touched
    assert "_summary_message" in touched
    assert "_history" not in touched, "the re-seed re-derived the window instead of sharing it"


def test_c27_the_reseed_restates_no_bound_of_its_own() -> None:
    """Never a second copy of a number: the payload is bounded by what already bounds it.

    ``senses_history_maxlen`` caps the turns and ``summary_max_chars`` caps the
    summary — both enforced where they live. A numeric literal appearing here
    would be a third bound nobody maintains.
    """
    literals = [
        node.value
        for node in ast.walk(_method_ast("floor_reseed"))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ]
    assert literals == [], literals


def test_c30_the_summary_rides_the_reseed_as_one_ephemeral_context_item() -> None:
    """The summary is not a turn anybody took, so it is not projected as one."""
    engine = _build()
    engine.update_summary("earlier: the operator asked about the weather")

    items = engine.floor_reseed()
    context = _context_items(items)
    assert len(context) == 1
    assert context[0].role == wire.ITEM_ROLE_SYSTEM
    assert "asked about the weather" in context[0].text
    assert items[0] is context[0], "the summary must precede the turns it summarises"


def test_c45_a_stale_summary_marker_rides_the_reseed_unchanged() -> None:
    """A reconnect must not quietly drop the "this may be out of date" caveat."""
    engine = _build()
    engine.update_summary("earlier: the kitchen was quiet")
    engine.mark_summary_stale("worker unreachable")

    context = _context_items(engine.floor_reseed())
    assert len(context) == 1
    assert context[0].text.startswith(STALE_SUMMARY_MARKER)
    assert "kitchen was quiet" in context[0].text


def test_an_engine_with_nothing_to_say_re_seeds_nothing() -> None:
    """No summary and no turns is not a failure — it is a fresh conversation."""
    assert _build().floor_reseed() == []


def test_the_reseed_payload_is_bounded_by_the_windows_that_already_bound_the_lanes() -> None:
    """h2's growth test, one projection over: 100+ turns do not grow the seed."""
    engine = _build(history_maxlen=60, senses_history_maxlen=20)
    engine.update_summary("earlier: a long conversation about the weather")
    _talk(engine, 60)
    after_60 = len(engine.floor_reseed())

    _talk(engine, 100, first=60)
    after_160 = engine.floor_reseed()

    assert len(after_160) == after_60
    # One summary context item + m turns x (user + assistant).
    assert len(after_160) == 1 + 2 * 20
    assert len(_history_items(after_160)) == 2 * 20


def test_the_reseed_summary_is_bounded_by_the_engines_own_summary_cap() -> None:
    """The one bound that could grow without the window noticing, and it cannot."""
    engine = _build()
    over_long = "x" * (engine.summary_max_chars + 1)
    assert engine.update_summary(over_long) is False
    assert engine.floor_reseed() == [], "a refused summary must not reach the floor"


def test_every_reseed_item_is_a_legal_frame_on_the_real_wire() -> None:
    """The projection is only worth anything if the wire accepts it verbatim.

    ``build_conversation_item_create_event`` raises on an unknown role or
    disposition, so this is the cheapest possible proof that the engine's own
    vocabulary and the codec's have not drifted apart.
    """
    engine = _build()
    engine.update_summary("earlier: something happened")
    _talk(engine, 3)

    for item in engine.floor_reseed():
        payload = json.loads(
            wire.build_conversation_item_create_event(
                item.text, role=item.role, disposition=item.disposition
            )
        )
        assert payload["item"]["disposition"] in wire.ITEM_DISPOSITIONS
        assert ConversationItem(role=item.role, text=item.text, disposition=item.disposition).valid


# =========================================================================== #
# Criterion 2 — context items and history turns stay distinct (c38 / h23)     #
# =========================================================================== #


def _duplex(server: FakeRealtimeServer | None = None, **kwargs) -> RealtimeDuplexSession:
    if server is not None:
        kwargs.setdefault("url", server.url)
    kwargs.setdefault("sample_rate", _RATE)
    kwargs.setdefault("read_audio", lambda: None)
    kwargs.setdefault("limits", duplex.Limits(backoff_initial_s=0.02, backoff_max_s=0.05))
    return RealtimeDuplexSession(**kwargs)


def _seeded_engine() -> EmbodyTurnEngine:
    engine = _build(history_maxlen=6, senses_history_maxlen=2)
    engine.update_summary("earlier: the operator asked about the weather")
    _talk(engine, 2)
    return engine


def _reseed_of(engine: EmbodyTurnEngine):
    def _seed() -> list[ConversationItem]:
        return [
            ConversationItem(role=item.role, text=item.text, disposition=item.disposition)
            for item in engine.floor_reseed()
        ]

    return _seed


def test_h23_the_summary_lands_as_context_and_the_turns_as_history() -> None:
    """The whole ask (lobes-cli#170 item 2), verified against the schema harness.

    The floor ALREADY auto-appends both roles, so an item that landed in history
    when it meant to be ephemeral would duplicate and drift — the two-histories
    failure #154 warned about, arriving one level down. The harness sorts by
    ``disposition`` precisely so a client that never made the distinction cannot
    pass this.
    """
    engine = _seeded_engine()
    with FakeRealtimeServer(
        Scenario.HAPPY_PATH, announce_conversation_items=True, wait_timeout=_TIMEOUT
    ) as server:
        client = _duplex(server, reseed=_reseed_of(engine))
        client.start()
        try:
            assert _wait_until(lambda: len(server.history_items) >= 4)
        finally:
            client.close()

    assert [role for role, _text in server.context_items] == ["system"]
    assert "asked about the weather" in server.context_items[0][1]
    assert [role for role, _text in server.history_items] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    summaries_in_history = [
        text for _role, text in server.history_items if "asked about the weather" in text
    ]
    assert summaries_in_history == [], "the ephemeral summary duplicated into history"


def test_h23_every_item_the_layer_sends_declares_a_disposition_the_schema_knows() -> None:
    """No item may reach the wire with a guessed or missing disposition."""
    engine = _seeded_engine()
    with FakeRealtimeServer(
        Scenario.HAPPY_PATH, announce_conversation_items=True, wait_timeout=_TIMEOUT
    ) as server:
        client = _duplex(server, reseed=_reseed_of(engine))
        client.start()
        try:
            assert _wait_until(lambda: len(server.items_received) >= 5)
        finally:
            client.close()

    assert server.items_received
    for item in server.items_received:
        assert item["disposition"] in wire.ITEM_DISPOSITIONS
        assert item["role"] in wire.ITEM_ROLES


def test_c44_no_projection_reaches_a_gateway_that_never_announced_the_schema(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """h29's degrade, from the layer's own projection rather than a stub item.

    The default gateway is the one shipping TODAY: conversation-item parity is
    parked upstream. The layer keeps the connect-time ``system_prompt`` context
    task t9 wired, names the degrade once, and sends nothing.
    """
    engine = _seeded_engine()
    with caplog.at_level(logging.INFO, logger="reachy"):
        with FakeRealtimeServer(Scenario.HAPPY_PATH, wait_timeout=_TIMEOUT) as server:
            client = _duplex(server, reseed=_reseed_of(engine))
            client.start()
            try:
                assert _wait_until(lambda: client.items_declined >= 5)
            finally:
                client.close()

    assert server.items_received == []
    assert _count_reason(caplog, duplex.REASON_ITEMS_UNSUPPORTED) == 1


def test_c40_the_canonical_reseed_precedes_the_arm_on_a_reconnect() -> None:
    """c40 with the REAL payload behind it, not a stub pair of items.

    A session close wipes the floor's ephemeral history, so a reconnect that
    armed first would answer out of an empty one. Task t10 pinned the ordering
    against a fixed seam; this pins that the seam production actually uses
    keeps the same place in the sequence.
    """
    engine = _seeded_engine()
    item_type = wire.CONVERSATION_ITEM_CREATE_EVENT_TYPE
    arm_type = wire.RESPONSE_CREATE_EVENT_TYPE
    with FakeRealtimeServer(
        Scenario.DROP_AFTER_ARM, announce_conversation_items=True, wait_timeout=_TIMEOUT
    ) as server:
        client = _duplex(server, reseed=_reseed_of(engine))
        client.start()
        try:
            assert _wait_until(lambda: server.received_event_types.count(arm_type) >= 2)
        finally:
            client.close()

    sent = [kind for kind in server.received_event_types if kind in (item_type, arm_type)]
    first_arm = sent.index(arm_type)
    assert first_arm == 5, "the whole seed (1 summary + 4 turn messages) precedes the arm"
    assert sent[first_arm + 1 : first_arm + 6] == [item_type] * 5
    assert client.reseeds >= 2


# =========================================================================== #
# Criterion 3 — the correction after a cut (c39)                             #
# =========================================================================== #


def test_c39_a_cut_reply_becomes_a_history_disposition_correction() -> None:
    """HISTORY, not context: a correction that evaporated after one generate call
    would let the server's overstatement come straight back on the next turn."""
    engine = _build()
    item = engine.floor_correction(_split())

    assert item is not None
    assert item.disposition == wire.ITEM_DISPOSITION_HISTORY
    assert item.role == wire.ITEM_ROLE_SYSTEM


def test_c39_the_correction_carries_the_measured_said_portion_not_the_whole_reply() -> None:
    """The client is the measured authority for what the room heard (c34)."""
    item = _build().floor_correction(_split())

    assert item is not None
    assert _CUT_SAID in item.text
    assert _CUT_UNSAID not in item.text, "the unheard remainder was reported as heard"


def test_c39_a_reply_the_room_never_heard_at_all_still_corrects_the_floor() -> None:
    """The largest overstatement of the lot, so silence here would be the worst case."""
    item = _build().floor_correction(_split(said="", unsaid=_CUT_TEXT))

    assert item is not None
    assert item.disposition == wire.ITEM_DISPOSITION_HISTORY
    assert _CUT_TEXT not in item.text


def test_a_reply_the_room_heard_whole_needs_no_correction() -> None:
    """Nothing was withheld, so the floor's record is already true."""
    assert _build().floor_correction(_split(said=_CUT_TEXT, unsaid="")) is None


def test_the_correction_names_itself_so_a_reader_can_find_it() -> None:
    """One shared prefix, so the journal, the floor and a test agree what it is."""
    item = _build().floor_correction(_split())
    assert item is not None
    assert item.text.startswith(engine_mod.FLOOR_CORRECTION_PREFIX)


def test_the_correction_is_a_legal_frame_on_the_real_wire() -> None:
    item = _build().floor_correction(_split())
    assert item is not None
    payload = json.loads(
        wire.build_conversation_item_create_event(
            item.text, role=item.role, disposition=item.disposition
        )
    )
    assert payload["item"]["disposition"] == wire.ITEM_DISPOSITION_HISTORY


def test_the_correction_makes_no_claim_that_the_server_record_matches() -> None:
    """c39 honesty h24, asserted on the prose that ships with the code.

    The correction APPENDS; nothing rewrites the turn the floor already stored,
    because the schema has no operation for that. A docstring that claimed
    otherwise would be the overstatement moving from the server's history into
    our own documentation.
    """
    doc = EmbodyTurnEngine.floor_correction.__doc__ or ""
    assert "append" in doc.lower()
    assert "c39" in doc


def test_the_correction_survives_a_cut_recorded_through_the_ordinary_path() -> None:
    """The two halves of one cut: the layer's own record narrows AND the floor is told."""
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn)
    engine.note_spoken(_CUT_TEXT)

    engine.note_interrupted_reply(_split())
    item = engine.floor_correction(_split())

    engine.submit_utterance("sorry, go on")
    engine.run_turn()
    said_section = turn.last_messages()[-1]["content"]
    assert f'"{_CUT_SAID}"' in said_section
    assert f'"{_CUT_TEXT}"' not in said_section
    assert item is not None and _CUT_SAID in item.text


def test_floor_correction_reads_the_split_structurally_and_never_raises() -> None:
    """It runs from a session tap on a worker thread: a shape it cannot read is a
    ``None``, never an exception that would take the session down."""
    engine = _build()
    assert engine.floor_correction(object()) is None
