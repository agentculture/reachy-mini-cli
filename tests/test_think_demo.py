"""Unit tests for ``reachy think demo`` (t9).

Verifies that the demo verb drives a scripted ``*emoji* "speech"`` stream
correctly through the real pipeline path:

1. Each ``*emoji*`` marker enqueues **exactly one** expression move via
   ``ExpressionProducer`` (inspected through a recording fake transport queue).
2. Only quoted text is forwarded to TTS — emojis, asterisks, and bare prose
   are never synthesized.
3. Events arrive in the order they appear in the script.
4. The JSON result payload contains ``expressed`` and ``spoken`` lists that
   match the script.
5. The ``DEMO_SCRIPT`` constant exercises the full built-in sequence.

All fakes: no live robot, no daemon, no TTS server, no real transport.
"""

from __future__ import annotations

import argparse

from reachy.cli._commands.think import DEMO_SCRIPT, cmd_think_demo
from reachy.motion.queue import EXPRESSION_KEY
from reachy.speech.markers import MarkerEvent, SpeechEvent, parse

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Records move_goto calls; does not touch any hardware."""

    def __init__(self) -> None:
        self.moves: list[dict] = []

    def move_goto(
        self,
        *,
        head=None,
        antennas=None,
        body_yaw=None,
        duration=1.0,
        interpolation="minjerk",
    ) -> None:
        self.moves.append(
            {
                "head": head,
                "antennas": antennas,
                "body_yaw": body_yaw,
                "duration": duration,
                "interpolation": interpolation,
            }
        )


def _make_args(
    *,
    script: str | None = None,
    json_mode: bool = False,
    tts_url: str | None = None,
    voice: str | None = None,
    transport: str = "http",
    base_url: str = "http://localhost:8000",
    timeout: float = 10.0,
) -> argparse.Namespace:
    """Build a minimal Namespace that ``cmd_think_demo`` accepts."""
    return argparse.Namespace(
        script=script,
        json=json_mode,
        tts_url=tts_url,
        voice=voice,
        transport=transport,
        base_url=base_url,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Tests — marker/speech parsing (unit, no fakes needed)
# ---------------------------------------------------------------------------


def test_demo_script_parses_to_three_markers_and_three_phrases():
    """The built-in DEMO_SCRIPT contains exactly 3 markers and 3 speech spans."""
    events = parse(DEMO_SCRIPT)
    markers = [e for e in events if isinstance(e, MarkerEvent)]
    speeches = [e for e in events if isinstance(e, SpeechEvent)]
    assert len(markers) == 3
    assert len(speeches) == 3


def test_demo_script_marker_order():
    """Markers appear in the expected emoji order."""
    events = parse(DEMO_SCRIPT)
    emojis = [e.emoji for e in events if isinstance(e, MarkerEvent)]
    assert emojis == ["🤔", "👂", "🙂"]


def test_demo_script_speech_order():
    """Quoted phrases appear in the expected order."""
    events = parse(DEMO_SCRIPT)
    texts = [e.text for e in events if isinstance(e, SpeechEvent)]
    assert texts[0].startswith("I wonder")
    assert texts[1].startswith("There it is")
    assert texts[2].startswith("Ah")


def test_demo_script_interleaved_order():
    """Markers and speech alternate correctly: marker, speech, marker, speech, …"""
    events = parse(DEMO_SCRIPT)
    kinds = [e.kind for e in events]
    # expect: marker speech marker speech marker speech
    assert kinds == ["marker", "speech", "marker", "speech", "marker", "speech"]


# ---------------------------------------------------------------------------
# Tests — cmd_think_demo drives the pipeline correctly
# ---------------------------------------------------------------------------


def test_demo_enqueues_one_expression_per_marker(monkeypatch):
    """Each ``*emoji*`` marker enqueues exactly one ExpressionProducer move."""
    enqueued_labels: list[str] = []
    played: list[bytes] = []

    # Intercept ExpressionProducer.express to record without hardware.
    import reachy.motion.expression as expr_mod

    def _fake_express(self, emoji: str):
        action = self._action_for(emoji)
        enqueued_labels.append(action.label)
        # Simulate the executor draining immediately so coalescing doesn't hide moves.
        self.queue.submit(action)
        self.queue.pop()
        return action

    monkeypatch.setattr(expr_mod.ExpressionProducer, "express", _fake_express)

    # Intercept TTS and playback.
    import reachy.cli._commands.think as think_mod

    monkeypatch.setattr(think_mod, "_synthesize", lambda text, **kw: b"pcm")
    monkeypatch.setattr(think_mod, "_play_audio", lambda pcm, **kw: played.append(pcm))

    # Intercept get_transport so no real robot is needed.
    import reachy.robot.transport as transport_mod

    monkeypatch.setattr(transport_mod, "get_transport", lambda args: _FakeTransport())

    args = _make_args()
    rc = cmd_think_demo(args)

    assert rc == 0
    # Exactly 3 markers → 3 expression labels
    assert len(enqueued_labels) == 3
    assert all(label.startswith("express ") for label in enqueued_labels)
    emojis_seen = [label.split(" ", 1)[1] for label in enqueued_labels]
    assert emojis_seen == ["🤔", "👂", "🙂"]


def test_demo_only_quoted_text_is_synthesized(monkeypatch):
    """TTS is called with ONLY the quoted speech text — never the emojis."""
    synthesized_texts: list[str] = []

    import reachy.cli._commands.think as think_mod
    import reachy.motion.expression as expr_mod
    import reachy.robot.transport as transport_mod

    monkeypatch.setattr(expr_mod.ExpressionProducer, "express", lambda self, emoji: None)
    monkeypatch.setattr(
        think_mod,
        "_synthesize",
        lambda text, **kw: synthesized_texts.append(text) or b"pcm",
    )
    monkeypatch.setattr(think_mod, "_play_audio", lambda pcm, **kw: None)
    monkeypatch.setattr(transport_mod, "get_transport", lambda args: _FakeTransport())

    args = _make_args()
    cmd_think_demo(args)

    # Exactly 3 speech phrases were synthesized.
    assert len(synthesized_texts) == 3
    # None of them is an emoji.
    for text in synthesized_texts:
        assert "🤔" not in text
        assert "👂" not in text
        assert "🙂" not in text
    # No asterisks either.
    for text in synthesized_texts:
        assert "*" not in text


def test_demo_events_in_order(monkeypatch):
    """Expression and speech events arrive in the order they appear in the script."""
    order: list[tuple[str, str]] = []

    import reachy.cli._commands.think as think_mod
    import reachy.motion.expression as expr_mod
    import reachy.robot.transport as transport_mod

    monkeypatch.setattr(
        expr_mod.ExpressionProducer,
        "express",
        lambda self, emoji: order.append(("express", emoji)),
    )
    monkeypatch.setattr(
        think_mod,
        "_synthesize",
        lambda text, **kw: order.append(("speak", text)) or b"pcm",
    )
    monkeypatch.setattr(think_mod, "_play_audio", lambda pcm, **kw: None)
    monkeypatch.setattr(transport_mod, "get_transport", lambda args: _FakeTransport())

    args = _make_args()
    cmd_think_demo(args)

    kinds = [k for k, _ in order]
    assert kinds == ["express", "speak", "express", "speak", "express", "speak"]
    emojis = [p for k, p in order if k == "express"]
    assert emojis == ["🤔", "👂", "🙂"]


def test_demo_json_result_contains_expressed_and_spoken(monkeypatch, capsys):
    """The --json result contains ``expressed`` and ``spoken`` lists."""
    import json

    import reachy.cli._commands.think as think_mod
    import reachy.motion.expression as expr_mod
    import reachy.robot.transport as transport_mod

    monkeypatch.setattr(expr_mod.ExpressionProducer, "express", lambda self, emoji: None)
    monkeypatch.setattr(think_mod, "_synthesize", lambda text, **kw: b"pcm")
    monkeypatch.setattr(think_mod, "_play_audio", lambda pcm, **kw: None)
    monkeypatch.setattr(transport_mod, "get_transport", lambda args: _FakeTransport())

    args = _make_args(json_mode=True)
    rc = cmd_think_demo(args)

    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "ok"
    assert "expressed" in payload
    assert "spoken" in payload
    assert len(payload["expressed"]) == 3
    assert len(payload["spoken"]) == 3


def test_demo_custom_script(monkeypatch):
    """A custom ``--script`` overrides the built-in sequence."""
    custom = '*😮* "Wow!" *🤖* "Hello."'
    expressed: list[str] = []
    synthesized: list[str] = []

    import reachy.cli._commands.think as think_mod
    import reachy.motion.expression as expr_mod
    import reachy.robot.transport as transport_mod

    monkeypatch.setattr(
        expr_mod.ExpressionProducer,
        "express",
        lambda self, emoji: expressed.append(emoji),
    )
    monkeypatch.setattr(
        think_mod,
        "_synthesize",
        lambda text, **kw: synthesized.append(text) or b"pcm",
    )
    monkeypatch.setattr(think_mod, "_play_audio", lambda pcm, **kw: None)
    monkeypatch.setattr(transport_mod, "get_transport", lambda args: _FakeTransport())

    args = _make_args(script=custom)
    rc = cmd_think_demo(args)

    assert rc == 0
    assert expressed == ["😮", "🤖"]
    assert synthesized == ["Wow!", "Hello."]


def test_demo_empty_tts_does_not_call_play_audio(monkeypatch):
    """If TTS returns empty bytes, play_audio is never called for that phrase."""
    played_count = 0

    import reachy.cli._commands.think as think_mod
    import reachy.motion.expression as expr_mod
    import reachy.robot.transport as transport_mod

    monkeypatch.setattr(expr_mod.ExpressionProducer, "express", lambda self, emoji: None)
    # TTS returns empty bytes for every call.
    monkeypatch.setattr(think_mod, "_synthesize", lambda text, **kw: b"")

    def _count_play(pcm, **kw):
        nonlocal played_count
        played_count += 1

    monkeypatch.setattr(think_mod, "_play_audio", _count_play)
    monkeypatch.setattr(transport_mod, "get_transport", lambda args: _FakeTransport())

    args = _make_args()
    rc = cmd_think_demo(args)

    assert rc == 0
    assert played_count == 0


def test_demo_expression_key_on_enqueued_action(monkeypatch):
    """Each expression move uses EXPRESSION_KEY (coalescing contract)."""
    action_keys: list[str | None] = []

    import reachy.cli._commands.think as think_mod
    import reachy.motion.expression as expr_mod
    import reachy.robot.transport as transport_mod

    def _recording_express(self, emoji: str):
        action = self._action_for(emoji)
        action_keys.append(action.coalesce_key)
        self.queue.submit(action)
        self.queue.pop()
        return action

    monkeypatch.setattr(expr_mod.ExpressionProducer, "express", _recording_express)
    monkeypatch.setattr(think_mod, "_synthesize", lambda text, **kw: b"pcm")
    monkeypatch.setattr(think_mod, "_play_audio", lambda pcm, **kw: None)
    monkeypatch.setattr(transport_mod, "get_transport", lambda args: _FakeTransport())

    args = _make_args()
    cmd_think_demo(args)

    assert all(k == EXPRESSION_KEY for k in action_keys)
