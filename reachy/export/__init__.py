"""Export subsystem for ``reachy-mini-cli``.

Provides the event model and JSONL serializer for streaming thinking, message,
and emotion blocks to external consumers (e.g. a reTerminal renderer), plus the
``--export-blocks`` selection parser.

Public API::

    from reachy.export.events import EmotionEvent, MessageEvent, ThinkingEvent, to_jsonl
    from reachy.export.blocks import BLOCKS, Selection, parse_blocks
"""
