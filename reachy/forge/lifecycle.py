"""The forge artifact lifecycle: staged -> activated / rejected, plus the events.

Forged skills live under the CLI's per-user state dir (:func:`reachy.daemon.state_dir`)
in a ``forge/`` subdirectory — the same directory family every other piece of
bookkeeping in this project uses (daemon PID file, the ``*_active.flag`` motion flags,
the behavior stash index, ...):

* ``<state_dir>/forge/staged/<name>/``            — a freshly written, validated skill;
* ``<state_dir>/forge/staged/.rejected/<name>/``  — where a rejected skill is quarantined;
* ``<state_dir>/forge/active/<name>/``            — where activation moves a staged skill.

This module is the disk + event layer; the dispatch client (:mod:`reachy.forge.client`)
drives it. Every lifecycle transition is announced through a caller-supplied ``publish``
callback as a ``forge/*`` event (``forge/staged`` / ``forge/activated`` /
``forge/rejected``) so the nervous system stays observable end to end.

Two guarantees the client depends on:

* :func:`stage` is the ONLY path that emits ``forge/staged`` — the client calls it
  strictly *after* validation passes, so a staged event never precedes a clean gate; and
* :func:`reject` is loud — it always logs a ``logging.warning`` naming the reason before
  emitting ``forge/rejected``, and it never raises (a failed folder move or a raising
  publish callback is caught and logged, not propagated).

Activation WIRING (deciding *when* to move a staged skill live, hot-registering the tool,
the restricted ``ctx``) lives in :mod:`reachy.forge.activate`; this module only provides
the move + the event (:func:`activate`).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

from reachy.daemon import state_dir

logger = logging.getLogger(__name__)

#: ``publish(event_type, payload)`` — ``event_type`` is already ``"forge/<transition>"``.
PublishFn = Callable[[str, dict], None]

EVENT_STAGED = "forge/staged"
EVENT_ACTIVATED = "forge/activated"
EVENT_REJECTED = "forge/rejected"

_REJECTED_DIRNAME = ".rejected"

#: Canonical filenames for a forged skill's two staged artifacts. This is the single
#: source of truth for the package — every sibling forge module imports these rather
#: than repeating the literals (SonarCloud S1192: duplicated string literals).
SKILL_FILENAME = "SKILL.md"
EXECUTOR_FILENAME = "executor.py"


def default_staging_root() -> Path:
    """``<state_dir>/forge/staged`` — where freshly forged skills are written."""
    return state_dir() / "forge" / "staged"


def default_active_root() -> Path:
    """``<state_dir>/forge/active`` — where activation moves a staged skill."""
    return state_dir() / "forge" / "active"


def write_artifacts(
    name: str,
    skill_md: str,
    executor_py: str,
    *,
    staging_root: Path | None = None,
) -> Path:
    """Write ``SKILL.md`` + ``executor.py`` into ``staging_root/<name>/`` and return it.

    Raises :class:`OSError` on a filesystem failure — the caller turns that into a
    rejection (never lets it escape to the caller's thread).
    """
    root = staging_root if staging_root is not None else default_staging_root()
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_FILENAME).write_text(skill_md)
    (skill_dir / EXECUTOR_FILENAME).write_text(executor_py)
    return skill_dir


def emit(publish: PublishFn, event_type: str, payload: dict) -> None:
    """Publish an event, isolating a broken ``publish`` callback (never raises)."""
    try:
        publish(event_type, payload)
    except Exception as err:  # noqa: BLE001 - a broken publish callback must not crash us
        logger.warning("forge publish callback raised for %s: %s", event_type, err)


def stage(publish: PublishFn, name: str, skill_dir: Path) -> None:
    """Emit ``forge/staged``. The ONLY path that does so — called post-validation."""
    emit(publish, EVENT_STAGED, {"name": name, "path": str(skill_dir)})


def reject(
    publish: PublishFn,
    name: str | None,
    reasons: Iterable[str],
    skill_dir: Path | None = None,
    *,
    staging_root: Path | None = None,
) -> None:
    """Emit ``forge/rejected``, loudly, moving a staged folder to ``.rejected/<name>/``.

    Always logs a ``logging.warning`` naming the reason first; the folder move and the
    publish are both fault-isolated so a rejection can never itself raise.
    """
    reasons = list(reasons)
    logger.warning("forge rejected %s: %s", name or "<unnamed>", "; ".join(reasons))

    final_dir = skill_dir
    if skill_dir is not None and name is not None:
        root = staging_root if staging_root is not None else default_staging_root()
        rejected_dir = root / _REJECTED_DIRNAME / name
        try:
            rejected_dir.parent.mkdir(parents=True, exist_ok=True)
            if rejected_dir.exists():
                shutil.rmtree(rejected_dir)
            shutil.move(str(skill_dir), str(rejected_dir))
            final_dir = rejected_dir
        except OSError as err:
            logger.warning("forge failed moving rejected folder for %s: %s", name, err)

    payload: dict = {"reason": "; ".join(reasons), "reasons": list(reasons)}
    if name:
        payload["name"] = name
    if final_dir is not None:
        payload["path"] = str(final_dir)
    emit(publish, EVENT_REJECTED, payload)


def activate(
    publish: PublishFn,
    name: str,
    *,
    staging_root: Path | None = None,
    active_root: Path | None = None,
) -> Path | None:
    """Move ``staged/<name>`` -> ``active/<name>`` and emit ``forge/activated``.

    Returns the destination path, or ``None`` if the move failed (in which case no
    ``forge/activated`` event is emitted — activation must never be announced falsely).
    Deciding *when* to call this (the auto-activation policy + hot registration) is a
    later task; this is only the move + the event.
    """
    s_root = staging_root if staging_root is not None else default_staging_root()
    a_root = active_root if active_root is not None else default_active_root()
    src = s_root / name
    dst = a_root / name
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))
    except OSError as err:
        logger.warning("forge failed activating %s: %s", name, err)
        return None
    emit(publish, EVENT_ACTIVATED, {"name": name, "path": str(dst)})
    return dst
