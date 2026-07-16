"""Pure systemd ``--user`` unit-file text generation for the presence stack.

This module renders the unit text for the three units that make the robot a
boot-surviving, self-healing presence: the local ``reachy-mini-daemon`` and the
two mutually-exclusive presence loops (idle ``demo-mode`` and the folded-live
``listen run --live``). It is **pure**: every function returns a ``str`` and has
no side effects — no ``systemctl``, no file writes, no process launches. The
installer half (writing + enabling these) lives in sibling modules; this one only
*describes* the units so the text is trivially testable field-by-field.

The shape mirrors the hand-authored units this stack replaces (and the existing
:mod:`reachy.demo_service` unit grammar): ``Type=simple``, ``Restart=on-failure``,
``RestartSec=5``, ``After=network-online.target``, ``WantedBy=default.target``,
and an ``ExecStart`` that re-invokes the running interpreter against the
``-m reachy …`` module entry (PATH-independent).

Canonical unit names are exported as module constants
(:data:`DAEMON_UNIT` / :data:`DEMO_UNIT` / :data:`LIVE_UNIT`) — a cross-task
contract: anything that installs / enables / orders these units imports the
names from here rather than re-spelling the strings.
"""

from __future__ import annotations

import shutil
import sys

# Resolved at call time, not import time, so a test/install can inject the path
# of the daemon binary inside the [daemon] extra's venv.
DAEMON_BINARY = "reachy-mini-daemon"

# --------------------------------------------------------------------------- #
# Canonical unit names (CROSS-TASK CONTRACT — import these, never re-spell).
# --------------------------------------------------------------------------- #
DAEMON_UNIT = "reachy-daemon.service"
DEMO_UNIT = "reachy-demo-mode.service"
LIVE_UNIT = "reachy-live.service"


def _unit_arg(value: str) -> str:
    """Quote/escape one ExecStart argument for the systemd unit grammar.

    systemd splits ExecStart on whitespace and treats ``%`` as a specifier, so a
    path with spaces or ``%`` would corrupt the command. Double quotes preserve
    spaces; ``%`` becomes ``%%`` and ``"`` / ``\\`` are backslash-escaped. This
    matches :func:`reachy.demo_service._unit_arg` exactly.
    """
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _default_python() -> str:
    """The interpreter to launch the module entry with — the running one."""
    return sys.executable


def _default_daemon_cmd() -> str:
    """Resolve the daemon binary: PATH lookup, falling back to the bare name.

    Kept pure (no raising): rendering the unit text must never fail just because
    the binary is not on *this* box — the unit is often authored on one machine
    and started on another. The bare name is a valid ``ExecStart`` that systemd
    resolves at start time.
    """
    return shutil.which(DAEMON_BINARY) or DAEMON_BINARY


# --------------------------------------------------------------------------- #
# ExecStart lines.
# --------------------------------------------------------------------------- #


def daemon_exec_start(daemon_cmd: str | None = None) -> str:
    """ExecStart for the daemon unit: run the ``reachy-mini-daemon`` binary."""
    cmd = daemon_cmd or _default_daemon_cmd()
    return _unit_arg(cmd)


def demo_exec_start(python: str | None = None, config_file: str | None = None) -> str:
    """ExecStart for the idle presence unit: ``<python> -m reachy demo-mode run``.

    ``config_file`` is required by the caller in practice (the installer passes a
    concrete path so the unit never points at a missing file); a ``None`` default
    keeps the signature ergonomic for tests.
    """
    py = python or _default_python()
    cfg = config_file or ""
    return f"{_unit_arg(py)} -m reachy demo-mode run --config {_unit_arg(cfg)}"


def live_exec_start(python: str | None = None) -> str:
    """ExecStart for the live presence unit: the folded live loop.

    ``listen run --live --transcribe --voice-engine harmonic`` runs the folded
    live loop (hearing + pat + think + vision + sleep in one loop) with STT
    transcription on, so the deployed boot presence reasons about the *words*
    spoken near it (not just direction), and speaks through the harmonic voice
    engine. Both ``--transcribe`` and ``--voice-engine harmonic`` stay off at
    the CLI default (``--voice-engine`` defaults to ``tts``); the unit opts in
    to both so the on-robot presence runs the full hear-words behavior with the
    harmonic voice. The flags are implemented elsewhere — this only renders the
    string.
    """
    py = python or _default_python()
    return f"{_unit_arg(py)} -m reachy listen run --live --transcribe --voice-engine harmonic"


# --------------------------------------------------------------------------- #
# Full unit texts.
# --------------------------------------------------------------------------- #


def _render(
    *,
    description: str,
    exec_start: str,
    requires: str | None = None,
    after_daemon: bool = False,
) -> str:
    """Assemble one ``--user`` unit from its parts (shared skeleton).

    All three units share ``Type=simple`` + ``Restart=on-failure`` +
    ``RestartSec=5`` + ``WantedBy=default.target``. Presence units additionally
    ``Requires=`` and order ``After=`` the daemon unit so the daemon is up first.
    """
    after = "network-online.target"
    if after_daemon:
        # Daemon before network-online so the presence loop only starts once the
        # robot daemon it talks to is already running.
        after = f"{DAEMON_UNIT} network-online.target"
    requires_line = f"Requires={requires}\n" if requires else ""
    return (
        "[Unit]\n"
        f"Description={description}\n"
        f"{requires_line}"
        f"After={after}\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def daemon_unit_text(daemon_cmd: str | None = None) -> str:
    """Render ``reachy-daemon.service`` — the local robot daemon process."""
    return _render(
        description="Reachy Mini daemon (robot control process)",
        exec_start=daemon_exec_start(daemon_cmd),
    )


def demo_unit_text(python: str | None = None, config_file: str | None = None) -> str:
    """Render ``reachy-demo-mode.service`` — idle feel-alive presence loop."""
    return _render(
        description="Reachy Mini demo-mode (feel-alive idle motion)",
        exec_start=demo_exec_start(python, config_file),
        requires=DAEMON_UNIT,
        after_daemon=True,
    )


def live_unit_text(python: str | None = None) -> str:
    """Render ``reachy-live.service`` — folded live presence loop (listen --live)."""
    return _render(
        description="Reachy Mini live presence (hearing + pat, folded live loop)",
        exec_start=live_exec_start(python),
        requires=DAEMON_UNIT,
        after_daemon=True,
    )
