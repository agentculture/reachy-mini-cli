"""Persisted tuning for ``demo-mode`` — a zero-dependency JSON config file.

The "feel alive" loop is meant to run continuously and be improved over time, so
its knobs live in a file you can edit (or set with ``demo-mode config --set``)
rather than only on the command line. ``run``/``start`` read this file at startup;
explicit CLI flags override it; a missing file falls back to the built-in
defaults. ``restart`` (and the systemd service) re-read it, so editing the file
then restarting is how you apply an update.

``tomllib`` is read-only in the stdlib, so JSON is used — ``config --init`` and
``--set`` need to *write* it too. The config holds both the connection (transport,
base URL, timeout) and the motion (interval, energy, interpolation, seed, wake,
settle), so the systemd unit can run with nothing but ``run --config <path>``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from reachy.alive import AliveConfig
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, INTERPOLATIONS, TRANSPORTS

_APP = "reachy"
_FILE = "demo-mode.json"

# The fields a user may persist / override, in display order.
FIELDS = (
    "transport",
    "base_url",
    "timeout",
    "interval",
    "energy",
    "interpolation",
    "seed",
    "wake",
    "settle",
)


def xdg_config_home() -> Path:
    """Base XDG config dir (``$XDG_CONFIG_HOME`` or ``~/.config``)."""
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))


def config_dir() -> Path:
    return xdg_config_home() / _APP


def config_path() -> Path:
    return config_dir() / _FILE


@dataclass
class DemoConfig:
    """Resolved demo-mode settings. Motion defaults track :class:`AliveConfig`."""

    transport: str = "http"
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT
    interval: float = AliveConfig.interval
    energy: float = AliveConfig.energy
    interpolation: str = AliveConfig.interpolation
    seed: int | None = None
    wake: bool = True
    settle: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    def to_alive_config(self) -> AliveConfig:
        return AliveConfig(
            interval=self.interval,
            energy=self.energy,
            interpolation=self.interpolation,
            seed=self.seed,
        )


def _from_dict(data: dict) -> DemoConfig:
    cfg = DemoConfig()
    cfg.transport = str(data.get("transport", cfg.transport))
    cfg.base_url = str(data.get("base_url", cfg.base_url))
    cfg.timeout = float(data.get("timeout", cfg.timeout))
    cfg.interval = float(data.get("interval", cfg.interval))
    cfg.energy = float(data.get("energy", cfg.energy))
    cfg.interpolation = str(data.get("interpolation", cfg.interpolation))
    seed = data.get("seed", cfg.seed)
    cfg.seed = int(seed) if seed is not None else None
    cfg.wake = bool(data.get("wake", cfg.wake))
    cfg.settle = bool(data.get("settle", cfg.settle))
    return cfg


def load(path: str | None = None) -> DemoConfig:
    """Load config from ``path`` (or the default), tolerating an absent/corrupt file."""
    cfg_path = Path(path) if path else config_path()
    if not cfg_path.is_file():
        return DemoConfig()
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DemoConfig()
    if not isinstance(data, dict):
        return DemoConfig()
    try:
        return _from_dict(data)
    except (ValueError, TypeError):
        return DemoConfig()


def save(cfg: DemoConfig, path: str | None = None) -> Path:
    """Write ``cfg`` as pretty JSON, creating the config dir. Returns the path."""
    cfg_path = Path(path) if path else config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n", encoding="utf-8")
    return cfg_path


def ensure(path: str | None = None) -> Path:
    """Return the resolved config path, writing defaults first if it is missing.

    Used before installing the systemd unit so its ``--config <path>`` always
    points at a real file — whether the operator passed ``--config`` or not.
    """
    cfg_path = Path(path) if path else config_path()
    if not cfg_path.is_file():
        save(load(path), path)
    return cfg_path


def _coerce(key: str, raw: str) -> object:
    """Coerce a ``key=value`` string token to the field's type, validating enums."""
    if key in ("interval", "energy", "timeout"):
        return float(raw)
    if key == "seed":
        return None if raw.strip().lower() in ("none", "null", "") else int(raw)
    if key in ("wake", "settle"):
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{key} must be a boolean (true/false)")
    if key == "transport":
        if raw not in TRANSPORTS:
            raise ValueError(f"transport must be one of: {', '.join(TRANSPORTS)}")
        return raw
    if key == "interpolation":
        if raw not in INTERPOLATIONS:
            raise ValueError(f"interpolation must be one of: {', '.join(INTERPOLATIONS)}")
        return raw
    return raw  # base_url: any string (validated by the transport at use time)


def apply_set(cfg: DemoConfig, pairs: list[str]) -> DemoConfig:
    """Apply ``key=value`` tokens to ``cfg`` in place. Raises CliError on bad input."""
    for token in pairs:
        if "=" not in token:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"bad --set token {token!r} (expected key=value)",
                remediation=f"use one of: {', '.join(FIELDS)} (e.g. energy=0.8)",
            )
        key, raw = token.split("=", 1)
        key = key.strip()
        if key not in FIELDS:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"unknown config key {key!r}",
                remediation=f"valid keys: {', '.join(FIELDS)}",
            )
        try:
            setattr(cfg, key, _coerce(key, raw.strip()))
        except ValueError as err:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"invalid value for {key}: {err}",
                remediation="see 'reachy-mini-cli explain demo-mode' for valid values",
            ) from err
    return cfg


def validate(cfg: DemoConfig) -> list[str]:
    """Return human-readable problems with ``cfg`` (empty == valid)."""
    errors: list[str] = []
    if cfg.transport not in TRANSPORTS:
        errors.append(f"transport must be one of: {', '.join(TRANSPORTS)}")
    if cfg.interpolation not in INTERPOLATIONS:
        errors.append(f"interpolation must be one of: {', '.join(INTERPOLATIONS)}")
    if cfg.interval <= 0:
        errors.append("interval must be > 0")
    if cfg.energy < 0:
        errors.append("energy must be >= 0")
    if cfg.timeout <= 0:
        errors.append("timeout must be > 0")
    return errors
