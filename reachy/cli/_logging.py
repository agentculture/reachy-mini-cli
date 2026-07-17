"""Shared logging setup for the long-running sense loops (``listen``/``think``/
``sleep`` ``run``).

Every module in this codebase logs via ``logging.getLogger(__name__)``, but
nothing ever attached a handler or called ``logging.basicConfig`` — so
INFO-level traces were silently dropped by Python's "last resort" handler
(WARNING+ only). :func:`install_logging` attaches exactly ONE
:class:`logging.StreamHandler` bound to ``sys.stderr`` on the ``"reachy"``
logger — the common ancestor every ``reachy.*`` module logger propagates to
by default — so a single call at run entry makes every module's traces
visible, with no per-module instrumentation required.

Export purity: the handler always targets ``sys.stderr`` (never ``stdout``),
so under ``listen run --live --export -`` stdout stays a pure JSONL feed (see
``reachy.cli._export``).

Level precedence: an explicit ``--log-level`` flag value beats the
``REACHY_LOG_LEVEL`` environment variable, which beats the caller-supplied
default (``"INFO"`` for the three long-running loops).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import TextIO

#: Env var honoured when ``--log-level`` is not passed explicitly.
LOG_LEVEL_ENV = "REACHY_LOG_LEVEL"

#: Default verbosity for the long-running foreground loops.
DEFAULT_LOG_LEVEL = "INFO"

# The logger name every ``reachy.*`` module logger (``logging.getLogger(__name__)``)
# is a descendant of, so attaching ONE handler here reaches all of them via normal
# propagation — no per-module wiring needed.
_ROOT_LOGGER_NAME = "reachy"

# Marks the handler this module installed, so a later install_logging() call can
# find and reuse it instead of attaching a duplicate.
_INSTALLED_MARKER = "_reachy_cli_installed"


def add_log_level_arg(parser: argparse.ArgumentParser, *, default: str = DEFAULT_LOG_LEVEL) -> None:
    """Register the shared ``--log-level`` flag on *parser*.

    Mirrors the ``add_export_args`` / ``add_robot_args`` pattern (one shared
    helper) so ``listen run`` / ``think run`` / ``sleep run`` present an
    identical flag instead of drifting three ways.
    """
    parser.add_argument(
        "--log-level",
        default=None,
        dest="log_level",
        metavar="LEVEL",
        help="Logging verbosity for reachy.* module loggers (e.g. DEBUG, INFO, "
        f"WARNING, ERROR); overrides {LOG_LEVEL_ENV} (default: {default}).",
    )


def resolve_log_level(level: str | int | None, *, default: str = DEFAULT_LOG_LEVEL) -> str | int:
    """Resolve the effective level: explicit *level* > ``REACHY_LOG_LEVEL`` env > *default*."""
    if level is not None:
        return level
    return os.environ.get(LOG_LEVEL_ENV) or default


def _coerce_level(level: str | int) -> int:
    """A numeric ``logging`` level from a name (``"info"``/``"INFO"``) or int."""
    if isinstance(level, int):
        return level
    text = str(level).strip()
    if text.isdigit():
        return int(text)
    mapping = logging.getLevelNamesMapping()
    try:
        return mapping[text.upper()]
    except KeyError as exc:
        valid = ", ".join(name for name in sorted(mapping) if name != "NOTSET")
        raise ValueError(f"unknown log level {level!r}; choose one of: {valid}") from exc


def install_logging(
    level: str | int | None = None,
    *,
    default: str = DEFAULT_LOG_LEVEL,
    stream: TextIO | None = None,
) -> logging.Handler:
    """Attach ONE stderr :class:`~logging.StreamHandler` to the ``"reachy"`` logger.

    ``level`` resolves through :func:`resolve_log_level` (flag > env >
    *default*) before being applied to both the logger and the handler.

    Calling this more than once is a no-op for the handler: the SAME handler
    object is found and reused (its level is simply refreshed), so repeated
    calls — e.g. ``restart`` re-reading tuning, or a defensive call at more
    than one entry point — never duplicate the handler or log lines.

    ``stream`` is an injection seam for tests; production callers never pass
    it — the handler always targets ``sys.stderr``, so stdout stays available
    for ``--export -``'s pure JSONL feed.
    """
    target_stream = stream if stream is not None else sys.stderr
    numeric_level = _coerce_level(resolve_log_level(level, default=default))

    root = logging.getLogger(_ROOT_LOGGER_NAME)
    handler = next((h for h in root.handlers if getattr(h, _INSTALLED_MARKER, False)), None)
    if handler is None:
        handler = logging.StreamHandler(target_stream)
        setattr(handler, _INSTALLED_MARKER, True)
        root.addHandler(handler)

    handler.setLevel(numeric_level)
    root.setLevel(numeric_level)
    return handler
