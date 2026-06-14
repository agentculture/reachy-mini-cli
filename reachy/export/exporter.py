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
    The only public symbol.  Construct once; call :meth:`~JsonlExporter.emit` for
    every event produced by the cognition loop.
"""

from __future__ import annotations

import sys
from typing import IO

from reachy.export.blocks import Selection
from reachy.export.events import Event, to_jsonl

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
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._broken = True
            print(
                f"reachy export: stream closed, export disabled ({type(exc).__name__}: {exc})",
                file=sys.stderr,
            )
