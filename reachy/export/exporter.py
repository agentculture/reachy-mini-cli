"""Broken-pipe-safe JSONL stdout exporter for the ``reachy-mini-cli`` export feed.

The exporter is a passive, one-way sink: it accepts :data:`~reachy.export.events.Event`
objects, checks them against a :class:`~reachy.export.blocks.Selection`, and writes
matching events as NDJSON lines to a writable text stream (typically ``sys.stdout``).

It is designed to run on the cognition thread and must never block, raise, or acquire
a lock that outlives the call.  Pipe disconnection is handled internally — callers
never see :exc:`BrokenPipeError`, :exc:`OSError`, or :exc:`ValueError`.

Public API
----------
:class:`JsonlExporter`
    Construct once; call :meth:`~JsonlExporter.emit` for every event produced by
    the cognition loop.
:class:`ExportHook`
    A small bundle of the export seams (``emit`` / ``pose_resolver`` / ``time_fn``)
    handed to the cognition engine as a single parameter.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO

from reachy.export.blocks import Selection
from reachy.export.events import Event, to_jsonl

# ---------------------------------------------------------------------------
# Export hook
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportHook:
    """Bundle of export collaborators handed to the cognition engine.

    Groups the three export-time seams so the engine takes ONE optional parameter
    instead of three:

    - ``emit``: called with each built export event (typically
      :meth:`JsonlExporter.emit`).
    - ``pose_resolver``: maps an emoji to a pose dict, or ``None`` when the emoji
      is unknown; fills :attr:`~reachy.export.events.EmotionEvent.pose`.
    - ``time_fn``: wall-clock source used to stamp every event's ``ts``.
    """

    emit: Callable[[object], None]
    pose_resolver: Callable[[str], dict | None] | None = None
    time_fn: Callable[[], float] = field(default=time.time)


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


class JsonlExporter:
    """Write selected events to a text stream as newline-delimited JSON.

    Each call to :meth:`emit` that passes the :class:`~reachy.export.blocks.Selection`
    filter results in exactly one ``stream.write(line + "\\n")`` followed by one
    ``stream.flush()``, ensuring real-time delivery to downstream consumers.

    If the underlying stream raises :exc:`BrokenPipeError`, :exc:`OSError`, or
    :exc:`ValueError` (e.g. the stream was closed), the exporter:

    1. Swallows the exception (never re-raises).
    2. Logs a single concise warning to :data:`sys.stderr` the **first time only**.
    3. Sets an internal ``_broken`` flag, making all subsequent :meth:`emit` calls
       immediate no-ops with no further I/O or logging.

    Args:
        stream: A writable text stream (e.g. ``sys.stdout``, ``io.StringIO``).
        selection: A :class:`~reachy.export.blocks.Selection` describing which
            block types to forward.  Events whose ``t`` attribute is not in the
            selection are dropped silently before any I/O is attempted.

    Example::

        exporter = JsonlExporter(sys.stdout, Selection.all())
        exporter.emit(MessageEvent(text="hello", ts=time.time()))
    """

    def __init__(self, stream: IO[str], selection: Selection) -> None:
        self._stream = stream
        self._selection = selection
        self._broken = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def emit(self, event: Event) -> None:
        """Write *event* to the stream if it matches the selection.

        - Filtered-out events (not in selection) → immediate return, no I/O.
        - Allowed events → ``stream.write(line)`` then ``stream.flush()``.
        - Any :exc:`BrokenPipeError`, :exc:`OSError`, or :exc:`ValueError`
          from write/flush is caught; a warning is printed to stderr once, and
          the exporter silently disables itself for all future calls.

        Args:
            event: Any :class:`~reachy.export.events.Event` instance.
        """
        # Fast-path: already broken — do nothing.
        if self._broken:
            return

        # Selection gate — check before any I/O.
        if not self._selection.allows(event.t):
            return

        line = to_jsonl(event) + "\n"
        try:
            self._stream.write(line)
            self._stream.flush()
        except (OSError, ValueError) as exc:
            # BrokenPipeError is a subclass of OSError, so it is caught here too.
            self._broken = True
            # The warning itself must not break pipe-safety: if stderr is also a
            # closed/broken pipe (e.g. ``2>&1 | head``), printing it would raise
            # and still kill the loop — so guard the warning write as well.
            try:
                print(
                    f"reachy export: stream closed, export disabled "
                    f"({type(exc).__name__}: {exc})",
                    file=sys.stderr,
                )
            except (OSError, ValueError):
                pass
