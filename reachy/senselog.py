"""Per-stage sensory logging.

Cited from ``reachy_nova``'s ``reachy_nova/sensory_log.py`` (cite-don't-import,
see ``docs/skill-sources.md``-style provenance convention used across this
repo) and adapted to this project's logger namespace.

stdlib-only helper that gives every stage of a sense pipeline (capture, gate,
inject, reaction, ...) one grep-able, parseable INFO-level log line under the
dedicated ``reachy.sense`` logger name, so "a sense was heard/handled
correctly" — or deliberately dropped, and why — is verifiable from the log
alone instead of living only at DEBUG level or being lost entirely.

Line shape (fixed, parseable)::

    [SENSE stage=<stage> source=<source> event=<event>] <detail>

Example::

    [SENSE stage=vad source=speech event=3f2a9c1e] utterance detected

A dropped sense uses the same shape via :func:`drop`, whose ``detail`` always
names the reason so it stays greppable::

    [SENSE stage=engagement source=speech event=3f2a9c1e] dropped reason=self-mute

This module is intentionally tiny and pure: it only formats and emits log
lines. It installs no handlers and configures no logging levels — that is a
concern for whatever wires up logging for the process (a separate task).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("reachy.sense")

_LINE_FORMAT = "[SENSE stage=%s source=%s event=%s] %s"


def stage(stage_name: str, source: str, event: str, detail: str) -> None:
    """Emit one INFO-level, parseable sensory log line for a pipeline stage.

    Args:
        stage_name: the pipeline stage (e.g. ``"vad"``, ``"inject"``,
            ``"capture"``).
        source: the sensory source (e.g. ``"speech"``, ``"vision"``,
            ``"touch"``).
        event: an identifier for this specific sensory event.
        detail: free-form human-readable detail for the line.
    """
    logger.info(_LINE_FORMAT, stage_name, source, event, detail)


def drop(stage_name: str, source: str, event: str, reason: str) -> None:
    """Emit one INFO-level, parseable sensory log line for a dropped event.

    Same fixed line shape as :func:`stage`, but the detail always names the
    ``reason`` a sense was dropped (e.g. ``"self-mute"``, ``"throttle"``,
    ``"gate-reject"``, ``"cooldown"``) so a dropped sense is as greppable as a
    handled one — never a silent no-op.

    Args:
        stage_name: the pipeline stage where the drop occurred.
        source: the sensory source (e.g. ``"speech"``, ``"vision"``,
            ``"touch"``).
        event: an identifier for this specific sensory event.
        reason: why the event was dropped (e.g. ``"self-mute"``,
            ``"throttle"``, ``"gate-reject"``, ``"cooldown"``).
    """
    logger.info(_LINE_FORMAT, stage_name, source, event, f"dropped reason={reason}")
