"""Shared CLI helper: build the JSONL export sinks from CLI args.

``agent attach`` and ``behavior engine run`` expose the same ``--export`` /
``--export-blocks`` pair and wire the *same* generic sink — a newline-delimited
JSON event feed on stdout (``thinking`` / ``message`` / ``emotion`` blocks; see
``docs/export-schema.md``). The feed is format-agnostic by design: a reTerminal
panel, an audio renderer, a log tail, or any other consumer subscribes to the one
documented wire contract. Keeping the builder here means the two command modules
produce a byte-identical feed instead of drifting. :func:`build_export_hook` /
:func:`add_export_args` are that pair's builder + flag registration.

``behavior engine run`` exposes the SAME two flag names for a wholly SEPARATE
feed — the runtime's own perception/rule/intent/motion events (decision c27: an
agent's cognition publishes its own feed through the pair above; the runtime
feed never carries a cognition block). :func:`build_runtime_export_consumer` /
:func:`add_runtime_export_args` are that feed's builder + flag registration,
deliberately NOT sharing schema/selection logic with the pair above — only
reusing :class:`~reachy.export.exporter.JsonlExporter`'s disconnect-safe sink
via its injectable ``serialize`` (see ``reachy/export/runtime.py`` for the
runtime schema).

Pure stdlib + the existing ``reachy.export`` package and expression catalog — no
new dependency.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from typing import Callable, TextIO

from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.export.blocks import Selection, parse_blocks
from reachy.export.exporter import ExportHook, JsonlExporter
from reachy.export.runtime import (
    RUNTIME_BLOCKS,
    RuntimeConsumer,
    parse_runtime_blocks,
    runtime_to_jsonl,
)
from reachy.speech.expressions import Catalog


def build_export_hook(
    args: argparse.Namespace, *, stream: TextIO | None = None
) -> ExportHook | None:
    """Build the export sink from ``--export`` / ``--export-blocks``, or ``None``.

    Returns ``None`` when ``--export`` is absent. Only ``-`` (stdout) is supported
    in this version; any other target is a clean exit-1 user error. ``--export-blocks``
    selects which block types to emit (default: all three). The pose resolver returns
    ``None`` for an emoji not in the catalog — the schema requires ``pose: null`` for
    unknown emoji so consumers can detect them.

    Parameters
    ----------
    args:
        The parsed namespace; reads ``args.export`` and ``args.export_blocks``.
    stream:
        The sink stream (defaults to ``sys.stdout``); injectable for tests.
    """
    export_target = getattr(args, "export", None)
    if export_target is None:
        return None
    if export_target != "-":
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unsupported export target: {export_target!r}",
            remediation="only '-' (stdout) is supported in this version; "
            "HTTP and file sinks are future work",
        )
    export_blocks_csv = getattr(args, "export_blocks", None)
    selection = parse_blocks(export_blocks_csv) if export_blocks_csv else Selection.all()
    exporter = JsonlExporter(stream if stream is not None else sys.stdout, selection)
    catalog = Catalog()

    def _resolve_pose(emoji: str) -> dict | None:
        return dataclasses.asdict(catalog.get(emoji)) if emoji in catalog else None

    return ExportHook(emit=exporter.emit, pose_resolver=_resolve_pose)


def add_export_args(parser: argparse.ArgumentParser) -> None:
    """Register the shared ``--export`` / ``--export-blocks`` arguments on *parser*.

    One shared registration so every command mode presents an identical
    surface. The caller decides any mode constraints (e.g. ``listen`` requires
    ``--live`` for the feed to carry cognition blocks).
    """
    parser.add_argument(
        "--export",
        default=None,
        dest="export",
        metavar="TARGET",
        help="Export events as JSONL to TARGET.  Only '-' (stdout) is supported in this "
        "version.  When set, stdout carries a pure JSONL event feed and all diagnostics "
        "are redirected to stderr.",
    )
    parser.add_argument(
        "--export-blocks",
        default=None,
        dest="export_blocks",
        metavar="BLOCKS",
        help="Comma-separated list of block types to include in the export feed "
        "(valid: thinking, message, emotion).  Default: all three when --export is set.",
    )


# ---------------------------------------------------------------------------
# The behavior engine's runtime-event feed (a separate contract — see the
# module docstring's decision-c27 note).
# ---------------------------------------------------------------------------


def build_runtime_export_consumer(
    args: argparse.Namespace, *, stream: TextIO | None = None
) -> Callable[[dict], None] | None:
    """Build the runtime-events JSONL consumer from ``--export`` / ``--export-blocks``.

    Returns ``None`` when ``--export`` is absent, mirroring
    :func:`build_export_hook`. Only ``-`` (stdout) is supported; any other target
    is a clean exit-1 user error. The returned callable is a plain
    ``consumer(event: dict) -> None`` — usable directly as a
    :class:`reachy.behavior.rule_engine.TickBus` consumer — that maps the raw
    event dicts a tick driver publishes via ``ctx.emit`` (rule fires/suppresses,
    perception snapshots, …) onto :mod:`reachy.export.runtime`'s event model and
    writes them through the SAME disconnect-safe
    :class:`~reachy.export.exporter.JsonlExporter` sink the cognition feed uses,
    just serialized with :func:`~reachy.export.runtime.runtime_to_jsonl` instead.

    Parameters
    ----------
    args:
        The parsed namespace; reads ``args.export`` and ``args.export_blocks``.
    stream:
        The sink stream (defaults to ``sys.stdout``); injectable for tests.
    """
    export_target = getattr(args, "export", None)
    if export_target is None:
        return None
    if export_target != "-":
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unsupported export target: {export_target!r}",
            remediation="only '-' (stdout) is supported in this version; "
            "HTTP and file sinks are future work",
        )
    export_blocks_csv = getattr(args, "export_blocks", None)
    selection = (
        parse_runtime_blocks(export_blocks_csv) if export_blocks_csv else Selection(RUNTIME_BLOCKS)
    )
    exporter = JsonlExporter(
        stream if stream is not None else sys.stdout, selection, serialize=runtime_to_jsonl
    )
    return RuntimeConsumer(exporter)


def add_runtime_export_args(parser: argparse.ArgumentParser) -> None:
    """Register ``--export`` / ``--export-blocks`` for the runtime-event feed.

    Same flag names/shape as :func:`add_export_args` (so every ``--export``-
    capable noun presents an identical surface) but with runtime-block-type help
    text, since ``behavior engine run --export-blocks`` does NOT accept
    ``thinking``/``message``/``emotion`` — see the module docstring.
    """
    parser.add_argument(
        "--export",
        default=None,
        dest="export",
        metavar="TARGET",
        help="Export runtime events (perception/rule/intent/motion) as JSONL to TARGET. "
        "Only '-' (stdout) is supported in this version.  When set, stdout carries a "
        "pure JSONL runtime-event feed and all diagnostics are redirected to stderr.",
    )
    parser.add_argument(
        "--export-blocks",
        default=None,
        dest="export_blocks",
        metavar="BLOCKS",
        help="Comma-separated list of runtime event types to include in the export feed "
        f"(valid: {', '.join(RUNTIME_BLOCKS)}).  Default: all when --export is set.",
    )
