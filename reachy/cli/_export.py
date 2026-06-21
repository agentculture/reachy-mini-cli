"""Shared CLI helper: build the JSONL export :class:`ExportHook` from CLI args.

Both ``think run`` and ``listen run --live`` expose the same ``--export`` /
``--export-blocks`` pair and wire the *same* generic sink — a newline-delimited
JSON event feed on stdout (``thinking`` / ``message`` / ``emotion`` blocks; see
``docs/export-schema.md``). The feed is format-agnostic by design: a reTerminal
panel, an audio renderer, a log tail, or any other consumer subscribes to the one
documented wire contract. Keeping the builder here means the two command modules
produce a byte-identical feed instead of drifting.

Pure stdlib + the existing ``reachy.export`` package and expression catalog — no
new dependency.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from typing import TextIO

from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.export.blocks import Selection, parse_blocks
from reachy.export.exporter import ExportHook, JsonlExporter
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

    Mirrors the pair on ``think run`` so the two command modes present an identical
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
