"""``docs/export-schema.md`` must describe what the code actually ships.

The schema doc is the *authoritative* wire contract for external consumers (a
reTerminal renderer, a log tail, an audio renderer) — they implement against the
document, not against a Python import. That makes the document a public API
surface with no compiler behind it: a producer can be deleted, a block type can
lose its only emitter, or a serializer can grow a field, and the prose stays
green forever.

These tests are that missing compiler. They walk the document itself and pin it
against the shipped code in three directions:

1. **Every producer command the doc names is a live CLI path that accepts
   ``--export``.** This is the tripwire for a retired noun: when ``think run``
   and ``listen run --live`` were deleted, the doc's attribution line became a
   claim about commands that no longer parse. Nothing failed. Now it would.
2. **The doc's block-type headings are exactly the shipped block sets** —
   :data:`reachy.export.blocks.BLOCKS` for the cognition feed and
   :data:`reachy.export.runtime.RUNTIME_BLOCKS` for the runtime feed, in both
   directions. A block type documented but no longer emitted (a contract
   narrowing) and a block type emitted but undocumented both fail here.
3. **Every JSON example line in the doc carries the key set its shipped
   serializer actually produces.** A parseable-but-stale example is worse than
   no example: a consumer codes against it.

What these tests deliberately do NOT check is prose semantics — that a block's
*description* still matches the producer's behaviour is a human review job. They
pin the machine-checkable half so the human half is the only thing left to get
wrong.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from reachy.cli import _build_parser
from reachy.export.blocks import BLOCKS
from reachy.export.events import EmotionEvent, MessageEvent, ThinkingEvent, to_jsonl
from reachy.export.runtime import (
    RUNTIME_BLOCKS,
    IntentEvent,
    MotionEvent,
    RuleEvent,
    SenseEvent,
    runtime_to_jsonl,
)

REPO_ROOT = Path(__file__).parent.parent
SCHEMA_DOC = REPO_ROOT / "docs" / "export-schema.md"

#: The installed console-script names a documented command may be prefixed with.
_PROG_NAMES = frozenset({"reachy", "reachy-mini-cli"})

#: Inline code spans.  ``re.DOTALL`` because a span may wrap across a line in the
#: source markdown (```agent\nattach --export -```); whitespace is
#: normalized before tokenizing.
_CODE_SPAN_RE = re.compile(r"`([^`]+)`", re.DOTALL)

#: A block-type heading, e.g. ``### `"thinking"` — internal reasoning turn``.
_BLOCK_HEADING_RE = re.compile(r'^#{2,4}\s+`"([a-z_]+)"`', re.MULTILINE)


def _doc_text() -> str:
    assert SCHEMA_DOC.exists(), f"Schema doc not found: {SCHEMA_DOC}"
    return SCHEMA_DOC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Documented producer commands are live, export-capable CLI paths
# ---------------------------------------------------------------------------


def _iter_argparse_paths(
    parser: argparse.ArgumentParser,
) -> dict[tuple[str, ...], argparse.ArgumentParser]:
    """Map every reachable command path to its (sub)parser.

    Walks ``argparse._SubParsersAction.choices`` recursively — the same structure
    ``_build_parser()`` populates — so it reflects exactly what
    ``reachy-mini-cli <path>`` accepts. Mirrors ``tests/test_cli.py``'s helper,
    but keeps the parser objects so each path's options can be inspected too.
    """
    found: dict[tuple[str, ...], argparse.ArgumentParser] = {}

    def walk(p: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        found[prefix] = p
        for action in p._actions:  # noqa: SLF001 - argparse has no public walk API
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for name, subparser in action.choices.items():
                    walk(subparser, prefix + (name,))

    walk(parser, ())
    return found


def _documented_export_commands() -> set[tuple[str, ...]]:
    """Every command path the doc names inside a code span, as a verb tuple.

    A code span counts as a command when it either starts with a console-script
    name or mentions ``--export``. The path is the run of leading non-flag tokens
    after an optional prog name; a span that is only a flag (``--export-blocks``)
    yields no path and is skipped.
    """
    commands: set[tuple[str, ...]] = set()
    for raw in _CODE_SPAN_RE.findall(_doc_text()):
        span = " ".join(raw.split())
        # A span carrying a markdown link target is prose, not a command.
        # ``_CODE_SPAN_RE`` pairs backticks SEQUENTIALLY, so a fenced block
        # (three backticks, whose empty pairs the regex cannot match) shifts
        # the pairing and can capture a run of prose as one "span". A
        # cross-reference whose anchor happens to contain ``--export``
        # (e.g. ``#runtime-event-feed-behavior-engine-run---export--``) then
        # reads as a documented command and the guard fails on prose.
        if "](" in span:
            continue
        tokens = span.split(" ")
        if not tokens:
            continue
        if tokens[0] in _PROG_NAMES:
            tokens = tokens[1:]
        elif "--export" not in span:
            continue
        path: list[str] = []
        for token in tokens:
            if token.startswith("-"):
                break
            path.append(token)
        if path:
            commands.add(tuple(path))
    return commands


def test_doc_names_at_least_the_two_known_producers() -> None:
    """Guard the extractor itself: a regex that silently matches nothing proves nothing."""
    commands = _documented_export_commands()
    assert ("agent", "attach") in commands, (
        "The schema doc no longer names `agent attach` — the cognition feed's only "
        f"producer. Extracted commands: {sorted(commands)!r}"
    )
    assert ("behavior", "engine", "run") in commands, (
        "The schema doc no longer names `behavior engine run` — the runtime feed's "
        f"producer. Extracted commands: {sorted(commands)!r}"
    )


def test_documented_producer_commands_are_live_cli_paths() -> None:
    """Every command the schema doc names must still exist in the CLI.

    Catches the doc attributing a feed to a retired noun (``think run``,
    ``listen run --live``): the document would keep telling a consumer to run a
    command that no longer parses, and CI would stay green.
    """
    live = _iter_argparse_paths(_build_parser())
    stale = sorted(path for path in _documented_export_commands() if path not in live)
    assert not stale, (
        "docs/export-schema.md names CLI commands that do not exist: "
        f"{stale!r}. Fix: update the document to name a live producer, or — if a "
        "producer was deleted — say so explicitly rather than leaving the "
        "attribution pointing at a removed verb."
    )


def test_documented_producer_commands_accept_export() -> None:
    """A documented producer must actually expose ``--export`` / ``--export-blocks``.

    Resolving as a live path is not enough: the doc's whole claim is that running
    the command with ``--export -`` yields this feed.
    """
    live = _iter_argparse_paths(_build_parser())
    missing: list[str] = []
    for path in sorted(_documented_export_commands()):
        parser = live.get(path)
        if parser is None:  # covered by the test above
            continue
        options = {
            opt for action in parser._actions for opt in action.option_strings
        }  # noqa: SLF001
        for flag in ("--export", "--export-blocks"):
            if flag not in options:
                missing.append(f"{' '.join(path)}: {flag}")
    assert not missing, (
        "docs/export-schema.md names commands as export producers that do not "
        f"register the export flags: {missing!r}"
    )


# ---------------------------------------------------------------------------
# 2. Documented block types == shipped block types, in both directions
# ---------------------------------------------------------------------------


def test_documented_block_headings_match_the_shipped_block_sets() -> None:
    """The doc's block-type headings must be exactly ``BLOCKS | RUNTIME_BLOCKS``.

    Both directions matter, and both are contract errors of opposite sign:

    * a heading with no matching block type documents something nothing emits —
      a **contract narrowing** left undeclared (e.g. the last producer of a block
      was deleted);
    * a block type with no heading is shipped-but-undocumented — a consumer
      cannot implement a reader for a line it has never been told about.
    """
    documented = set(_BLOCK_HEADING_RE.findall(_doc_text()))
    shipped = set(BLOCKS) | set(RUNTIME_BLOCKS)
    assert documented == shipped, (
        "docs/export-schema.md's block-type headings disagree with the code.\n"
        f"  documented but not shipped: {sorted(documented - shipped)!r}\n"
        f"  shipped but not documented: {sorted(shipped - documented)!r}\n"
        "A block type that lost its producer is a contract narrowing — declare it "
        "in the document, do not just delete the heading."
    )


def test_the_two_feeds_stay_disjoint_in_the_doc() -> None:
    """The doc's own claim — a consumer of one feed never sees the other's blocks."""
    assert set(BLOCKS).isdisjoint(set(RUNTIME_BLOCKS))


# ---------------------------------------------------------------------------
# 3. Documented JSON examples carry the shipped key sets
# ---------------------------------------------------------------------------


def _shipped_key_sets() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Per block type: ``(required_keys, full_keys)`` from the shipped serializers.

    ``required`` is what the serializer emits with every optional field absent;
    ``full`` additionally includes the optional/additive fields. They differ only
    for ``sense``, whose ``pat_state`` object is additive and omitted when the
    reading carries no pat state.
    """

    def keys(line: str) -> frozenset[str]:
        return frozenset(json.loads(line).keys())

    bare_sense = dict(doa=None, speech=False, rms=None, pat=None, face=None, frame_available=False)
    sense_required = keys(runtime_to_jsonl(SenseEvent(**bare_sense)))
    sense_full = keys(
        runtime_to_jsonl(SenseEvent(**bare_sense, pat_state={"availability": "available"}))
    )

    exact = {
        "emotion": keys(to_jsonl(EmotionEvent(emoji="x"))),
        "message": keys(to_jsonl(MessageEvent(text="x"))),
        "thinking": keys(to_jsonl(ThinkingEvent(cues=[], text="x"))),
        "rule": keys(
            runtime_to_jsonl(
                RuleEvent(action="fire", rule="r", kind="react", field="f", op="o", reason="fired")
            )
        ),
        "intent": keys(runtime_to_jsonl(IntentEvent(action="declare", name="n"))),
        "motion": keys(runtime_to_jsonl(MotionEvent(action="admit"))),
    }
    sets: dict[str, tuple[frozenset[str], frozenset[str]]] = {
        name: (ks, ks) for name, ks in exact.items()
    }
    sets["sense"] = (sense_required, sense_full)
    return sets


def _doc_json_examples() -> list[dict]:
    """Every standalone JSON object line in the doc, parsed."""
    examples: list[dict] = []
    for line in _doc_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                examples.append(obj)
    return examples


def test_doc_has_an_example_for_every_block_type() -> None:
    """Each documented block type needs at least one worked example line."""
    seen = {obj.get("t") for obj in _doc_json_examples()}
    shipped = set(BLOCKS) | set(RUNTIME_BLOCKS)
    assert shipped <= seen, f"No JSON example in the doc for: {sorted(shipped - seen)!r}"


@pytest.mark.parametrize("index", range(len(_doc_json_examples())))
def test_doc_json_examples_match_shipped_key_sets(index: int) -> None:
    """A doc example must carry exactly the keys its serializer emits.

    ``tests/test_export_events.py`` already checks the examples *parse*; a stale
    example parses perfectly well while telling a consumer to expect a field the
    code stopped emitting (or to miss one it started emitting).
    """
    obj = _doc_json_examples()[index]
    block = obj.get("t")
    sets = _shipped_key_sets()
    assert block in sets, f"Doc example uses an unknown block type {block!r}: {obj!r}"

    required, full = sets[block]
    actual = frozenset(obj.keys())
    assert required <= actual, (
        f"{block!r} example is missing required key(s) {sorted(required - actual)!r} "
        f"that {'runtime_to_jsonl' if block in RUNTIME_BLOCKS else 'to_jsonl'} always "
        f"emits: {obj!r}"
    )
    assert actual <= full, (
        f"{block!r} example carries key(s) {sorted(actual - full)!r} the shipped "
        f"serializer never emits: {obj!r}"
    )
