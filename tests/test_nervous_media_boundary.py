"""The nervous system's hard product boundary: **media never rides the bus**.

Task t6, criterion 4 (spec claim c29). Events carry only TEXT REFERENCES to
media — a file location, or a memory-link handle. Frames and audio move
out-of-band; the bus only announces *where* they are. This governs v1 and every
future raw-media leg, so it is asserted at the SCHEMA level (the event model's
own fields) rather than left to reviewer vigilance, and again at the seam (a
binary value is refused, by name, before it can reach a client).

Three layers of assertion:

1. **Schema** — no field of any runtime event type is binary-typed, and no field
   name declares an inline-media payload.
2. **Wire** — a maximally-populated event of every type serializes to JSON whose
   every leaf is a JSON scalar, with no data-URI and no base64 blob.
3. **Seam** — a ``bytes`` value smuggled into an event or into standing state is
   dropped by name, never published; and neither the event model nor the
   publisher imports a codec that would make inline media convenient.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from pathlib import Path

from reachy.export import mqtt as M
from reachy.export.runtime import (
    IntentEvent,
    MotionEvent,
    RuleEvent,
    SenseEvent,
    runtime_to_jsonl,
)
from tests.fake_events_client import FakeEventsClient

SENSE_LOGGER = "reachy.sense"
REPO_ROOT = Path(__file__).parent.parent

EVENT_TYPES = (SenseEvent, RuleEvent, IntentEvent, MotionEvent)

#: Field names that would declare an INLINE media payload rather than a
#: reference. A reference-shaped name (``frame_path``, ``clip_url``,
#: ``memory_link``) is fine — it is the ``_data``/``_b64``/``_bytes`` family
#: that means "the bytes themselves travel here".
INLINE_MEDIA_FIELD_NAMES = (
    "b64",
    "base64",
    "blob",
    "bytes",
    "data_uri",
    "datauri",
    "image_data",
    "frame_data",
    "audio_data",
    "jpeg",
    "png",
    "pcm",
    "wav",
    "raw_frame",
    "raw_audio",
)

BINARY_TYPE_NAMES = ("bytes", "bytearray", "memoryview")


def _live_publisher():
    client = FakeEventsClient()
    pub = M.NervousPublisher(client)
    pub.start()
    return pub, client


def _maximal_events() -> list:
    """One instance of every event type, every optional field populated."""
    return [
        SenseEvent(
            doa=0.7,
            speech=True,
            rms=0.031,
            pat=["scratch", "level2"],
            face="ori",
            frame_available=True,
            ts=1718362800.5,
            tick=42,
            pat_state={
                "availability": "available",
                "contact": True,
                "touch_type": "scratch",
                "level": "level2",
                "yaw_deg": 3.5,
                "phase": "contentment",
                "phase_started_at": 1718362800.1,
                "last_press_at": 1718362800.4,
            },
        ),
        RuleEvent(
            action="fire",
            rule="greet-when-addressed",
            kind="react",
            field="transcript",
            op="contains",
            reason="fired",
            behavior="nod",
            disable=["feel-alive"],
            ts=1718362801.0,
            tick=43,
        ),
        IntentEvent(
            action="declare",
            name="stay-alert",
            payload={"mode": "focus", "note": "/var/lib/reachy/frames/0042.jpg"},
            ts=1718362802.0,
            tick=44,
        ),
        MotionEvent(
            action="goto",
            behavior="look-left",
            channels=["head", "body_yaw"],
            detail={"phase": "admitted", "duration": 1.5},
            ts=1718362803.0,
            tick=45,
        ),
    ]


def _leaves(value):
    """Yield every leaf value of a decoded JSON document."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


# --------------------------------------------------------------------------- #
# 1. Schema level                                                             #
# --------------------------------------------------------------------------- #


def test_no_runtime_event_field_is_binary_typed() -> None:
    violations = []
    for cls in EVENT_TYPES:
        for f in dataclasses.fields(cls):
            annotation = str(f.type)
            for binary in BINARY_TYPE_NAMES:
                if re.search(rf"\b{binary}\b", annotation):
                    violations.append(f"{cls.__name__}.{f.name}: {annotation}")
    assert violations == [], (
        "a runtime event type declared a binary field — media must travel "
        f"out-of-band, the bus carries text references only: {violations}"
    )


def test_no_runtime_event_field_name_declares_inline_media() -> None:
    violations = []
    for cls in EVENT_TYPES:
        for f in dataclasses.fields(cls):
            lowered = f.name.lower()
            for token in INLINE_MEDIA_FIELD_NAMES:
                if token in lowered:
                    violations.append(f"{cls.__name__}.{f.name}")
    assert violations == [], f"inline-media-shaped field name(s) on the event schema: {violations}"


def test_neither_the_event_model_nor_the_seam_imports_a_media_codec() -> None:
    """No ``base64``/``binascii``/imaging import — inline media stays inconvenient."""
    codec = re.compile(r"^\s*(import|from)\s+(base64|binascii|struct|cv2|PIL|wave)\b", re.MULTILINE)
    for rel in ("reachy/export/runtime.py", "reachy/export/mqtt.py"):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        hits = [line.strip() for line in source.splitlines() if codec.match(line)]
        assert hits == [], f"{rel} imports a media codec: {hits}"


# --------------------------------------------------------------------------- #
# 2. Wire level                                                               #
# --------------------------------------------------------------------------- #


def test_every_published_payload_carries_only_json_text_and_scalars() -> None:
    pub, client = _live_publisher()
    for event in _maximal_events():
        pub.emit(event)

    assert len(client.published) >= len(EVENT_TYPES)
    for published in client.published:
        assert isinstance(published.payload, str)
        obj = json.loads(published.payload)
        for leaf in _leaves(obj):
            assert isinstance(
                leaf, (str, int, float, bool, type(None))
            ), f"non-scalar leaf {leaf!r} on {published.topic}"
        assert M.is_text_reference_only(obj), f"{published.topic} carries inline media"


def test_a_media_reference_field_is_allowed_when_it_is_a_plain_string() -> None:
    """The rule bans inline bytes, not references: a path/handle is the point."""
    payload = {
        "t": "intent",
        "frame": "/var/lib/reachy/frames/0042.jpg",
        "clip": "memlink://reachy/audio/9f2a",
        "note": "see the frame",
    }
    assert M.is_text_reference_only(payload) is True


def test_is_text_reference_only_rejects_data_uris_and_base64_blobs() -> None:
    assert M.is_text_reference_only({"frame": "data:image/jpeg;base64,/9j/4AAQSkZJRg=="}) is False
    assert M.is_text_reference_only({"clip": "data:audio/wav;base64,UklGRiQ="}) is False
    assert M.is_text_reference_only({"frame": b"\xff\xd8\xff"}) is False
    assert M.is_text_reference_only({"nested": [{"frame": bytearray(b"\x00")}]}) is False
    assert M.is_text_reference_only({"blob": "A" * 512}) is False, "a long base64 run is a blob"


# --------------------------------------------------------------------------- #
# 3. Seam level                                                               #
# --------------------------------------------------------------------------- #


def test_bytes_in_an_event_payload_never_reaches_the_bus(caplog) -> None:
    pub, client = _live_publisher()
    smuggled = MotionEvent(action="admit", behavior="nod", detail={"frame": b"\xff\xd8\xff"})

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.emit(smuggled)  # must not raise

    drops = [r.getMessage() for r in caplog.records if "dropped reason=" in r.getMessage()]
    assert any(M.REASON_UNSERIALIZABLE in line for line in drops)
    assert [p for p in client.published if p.topic.startswith("reachy/events/")] == []


def test_bytes_in_standing_state_never_reaches_the_bus(caplog) -> None:
    pub, client = _live_publisher()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        pub.publish_state({"updated": 1.0, "last_frame": b"\xff\xd8\xff"})

    drops = [r.getMessage() for r in caplog.records if "dropped reason=" in r.getMessage()]
    assert any(M.REASON_UNSERIALIZABLE in line for line in drops)
    assert "reachy/state/last_frame" not in [p.topic for p in client.published]
    assert "reachy/state/updated" in [p.topic for p in client.published]


def test_the_serializer_itself_cannot_encode_binary() -> None:
    """The structural guarantee beneath the seam guard: ``json.dumps``, no ``default=``."""
    smuggled = MotionEvent(action="admit", detail={"frame": b"\xff\xd8\xff"})
    try:
        runtime_to_jsonl(smuggled)
    except TypeError:
        return
    raise AssertionError(
        "runtime_to_jsonl encoded binary — the no-media boundary is not structural"
    )
