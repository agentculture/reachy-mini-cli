"""systemd ``--user`` supervision for ``demo-mode``.

Generates and manages a user-level unit so the feel-alive loop runs always-on:
it starts on boot (with linger), restarts on crash, and logs to the journal.
The unit's ``ExecStart`` re-invokes this CLI's foreground loop
(``python -m reachy demo-mode run --config <path>``), so a ``systemctl --user
restart`` — or ``demo-mode restart`` — re-imports the latest motion code and
re-reads the config; that is how an update is applied.

The unit text is pure (testable). Every ``systemctl`` / ``loginctl`` call goes
through :func:`shutil.which` (no bandit B607 partial-path) and degrades
gracefully when the tool is absent: read-only queries report ``unknown`` and
mutating verbs raise a clean :class:`CliError` (exit 2) rather than a traceback.
"""

from __future__ import annotations

import getpass
import shutil
import subprocess  # nosec B404 - only ever runs the resolved systemctl/loginctl binaries
import sys
from pathlib import Path

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.demo_config import config_path, xdg_config_home

UNIT_NAME = "reachy-demo-mode.service"


def unit_dir() -> Path:
    return xdg_config_home() / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT_NAME


def _unit_arg(value: str) -> str:
    """Quote/escape one ExecStart argument for the systemd unit grammar.

    systemd splits ExecStart on whitespace and treats ``%`` as a specifier, so a
    path with spaces or ``%`` would corrupt the command. Double quotes preserve
    spaces; ``%`` becomes ``%%`` and ``"``/``\\`` are backslash-escaped.
    """
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def exec_start(config_file: str | None = None) -> str:
    """ExecStart line: the running interpreter + module entry (PATH-independent)."""
    cfg = config_file or str(config_path())
    return f"{_unit_arg(sys.executable)} -m reachy demo-mode run --config {_unit_arg(cfg)}"


def unit_text(config_file: str | None = None) -> str:
    return (
        "[Unit]\n"
        "Description=Reachy Mini demo-mode (feel-alive idle motion)\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start(config_file)}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemctl() -> str | None:
    return shutil.which("systemctl")


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    """Run ``systemctl <args>``; return None if systemctl is absent / unusable."""
    exe = _systemctl()
    if exe is None:
        return None
    try:
        return subprocess.run(  # nosec B603 - resolved binary, arg list, no shell
            [exe, *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None


def _require(args: list[str], action: str) -> subprocess.CompletedProcess:
    """Run a mutating ``systemctl`` command, raising CliError on absence/failure."""
    result = _run(args)
    if result is None:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot {action}: 'systemctl --user' is not available",
            remediation="this needs a Linux systemd user session; run demo-mode start/stop instead",
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"{action} failed: {detail}" if detail else f"{action} failed",
            remediation=f"inspect 'systemctl --user status {UNIT_NAME}'",
        )
    return result


def _query(args: list[str]) -> str:
    """Read-only ``systemctl`` query; 'unknown' when unavailable. Never raises."""
    result = _run(args)
    if result is None:
        return "unknown"
    return (result.stdout or "").strip() or "unknown"


def is_active() -> bool:
    """True if the unit reports ``active`` (used to route ``restart``)."""
    result = _run(["--user", "is-active", UNIT_NAME])
    return (
        result is not None and result.returncode == 0 and (result.stdout or "").strip() == "active"
    )


def install(config_file: str | None = None) -> dict[str, object]:
    """Write the unit file and reload the user manager."""
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(config_file), encoding="utf-8")
    _require(["--user", "daemon-reload"], "reload the systemd user manager")
    return {"status": "installed", "unit": UNIT_NAME, "unit_path": str(path)}


def enable(*, linger: bool = True) -> dict[str, object]:
    """Enable + start the unit now; enable linger so it survives logout/reboot."""
    _require(["--user", "enable", "--now", UNIT_NAME], "enable the demo-mode service")
    lingered = False
    if linger:
        exe = shutil.which("loginctl")
        if exe is not None:
            try:
                subprocess.run(  # nosec B603 - resolved binary, arg list, no shell
                    [exe, "enable-linger", getpass.getuser()],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                lingered = True
            except OSError:
                lingered = False
    return {"status": "enabled", "unit": UNIT_NAME, "linger": lingered}


def disable() -> dict[str, object]:
    """Disable + stop the unit now."""
    _require(["--user", "disable", "--now", UNIT_NAME], "disable the demo-mode service")
    return {"status": "disabled", "unit": UNIT_NAME}


def restart() -> dict[str, object]:
    """Restart the unit (re-imports new code + re-reads config)."""
    _require(["--user", "restart", UNIT_NAME], "restart the demo-mode service")
    return {"status": "restarted", "unit": UNIT_NAME}


def uninstall() -> dict[str, object]:
    """Best-effort disable, remove the unit file, reload the manager."""
    _run(["--user", "disable", "--now", UNIT_NAME])
    path = unit_path()
    existed = path.is_file()
    if existed:
        path.unlink()
    _run(["--user", "daemon-reload"])
    return {
        "status": "uninstalled" if existed else "not-installed",
        "unit": UNIT_NAME,
        "unit_path": str(path),
    }


def status() -> dict[str, object]:
    """Report unit presence + active/enabled state (via is-active / is-enabled)."""
    return {
        "unit": UNIT_NAME,
        "unit_path": str(unit_path()),
        "installed": unit_path().is_file(),
        "active": _query(["--user", "is-active", UNIT_NAME]),
        "enabled": _query(["--user", "is-enabled", UNIT_NAME]),
    }
