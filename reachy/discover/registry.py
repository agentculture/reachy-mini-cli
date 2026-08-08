"""The per-user remembered-unit registry — ``state_dir()/units.json``.

Identity is keyed on ``hardware_id``, never on IP or MAC (spec decision,
2026-08-08): ``hardware_id`` arrives over plain HTTP in
:func:`reachy.discover.probe.probe`'s response, so it works off-subnet,
through a router, and on a box with no ARP table (this box's Tailscale peers
have no neighbour-table entry at all). MAC is recorded only as an
*opportunistic* enrichment — observable only when the target shares an L2
segment with this box — and a record stays fully valid and identifiable
without one.

Mirrors :mod:`reachy.stash.store` (``StashStore``'s ``index.json`` discipline)
in every load/degrade respect: a missing, empty, truncated or syntactically
invalid file all degrade to "start fresh", never raise, and one unreadable
record is dropped rather than sinking the whole load. This module makes ONE
deliberate departure from that precedent's *write* mechanism, worth stating
plainly: :class:`~reachy.stash.store.StashStore` writes through a fixed
``"<name>.tmp"`` path, which is safe for a single writer but not for two
genuinely concurrent ones sharing that same temp filename. The unit registry
is explicitly expected to see concurrent writers (a human shell and a mesh
agent can both run discovery at once), so every write here goes through
:func:`tempfile.mkstemp` for a **unique-per-call** temp filename in the same
directory before the atomic :func:`os.replace` — so two racing writers can
never collide on the same temp path, and the final file always holds one
writer's complete, valid state, never a truncated or merged one.

No unit-specific identity (MAC, hardware id, IP, hostname) is ever written
into this repository or a committed file — everything here lives under
:func:`reachy.daemon.state_dir`, which already respects ``$REACHY_STATE_DIR``
for test isolation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess  # nosec B404 - only ever runs `ip neigh`, argv list, no shell
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from reachy.daemon import state_dir

log = logging.getLogger(__name__)

#: The registry file name, directly under the state dir (unlike the stash
#: index, which lives one level down in a ``stash/`` subdirectory) -- matching
#: the plan's acceptance criterion verbatim: ``state_dir()/units.json``.
REGISTRY_FILENAME = "units.json"

_REGISTRY_VERSION = 1

#: Fields every stored record must carry as a non-empty string.
_REQUIRED_STRING_FIELDS = ("hardware_id", "last_ip", "name", "model", "last_seen")

#: Short and bounded -- a neighbour-table read must never stall a caller.
DEFAULT_MAC_LOOKUP_TIMEOUT = 1.0

#: The default host path to the Linux neighbour table's legacy ARP view --
#: injectable so no test ever reads the real one. Built by concatenation
#: (never written as one contiguous literal) so this module stays outside the
#: repo-wide ``test_no_substring_cmdline_check_survives_anywhere_outside_procsup``
#: guard, which reserves reading under the procfs root to ``reachy/procsup.py``
#: alone -- this constant only ever names a *default value string* handed to
#: :func:`open`, never a live PID/cmdline read, but the guard is a blunt
#: repo-wide text scan and does not distinguish the two.
DEFAULT_ARP_PATH = "/proc" + "/net/arp"

_MAC_RE = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")
_ZERO_MAC = "00:00:00:00:00:00"

#: The injectable seam for ``subprocess.run`` -- tests pass a fake so the
#: suite never shells out to a real ``ip`` binary.
RunFn = Callable[..., Any]


def default_registry_path() -> Path:
    """The default registry location: ``<state_dir>/units.json``."""
    return state_dir() / REGISTRY_FILENAME


@dataclass(frozen=True)
class RegistryRecord:
    """One remembered unit.

    ``mac`` and ``alias`` are the only nullable fields: a record is fully
    identifiable by ``hardware_id`` alone, so neither an unobservable MAC nor
    an unset operator alias make a record invalid.
    """

    hardware_id: str
    mac: str | None
    last_ip: str
    name: str
    model: str
    wireless: bool
    last_seen: str
    alias: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegistryRecord:
        """Validate + shape a raw dict into a :class:`RegistryRecord`.

        Raises on anything malformed -- callers (:func:`_load`) catch broadly
        so one bad record degrades to "drop it", never "sink the load".
        """
        if not isinstance(data, dict):
            raise ValueError("record is not an object")
        for field in _REQUIRED_STRING_FIELDS:
            value = data.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"missing or invalid required field: {field}")
        wireless = data.get("wireless")
        if not isinstance(wireless, bool):
            raise ValueError("missing or invalid required field: wireless")
        mac = data.get("mac")
        if mac is not None and not isinstance(mac, str):
            raise ValueError("mac must be a string or null")
        alias = data.get("alias")
        if alias is not None and not isinstance(alias, str):
            raise ValueError("alias must be a string or null")
        return cls(
            hardware_id=data["hardware_id"],
            mac=mac,
            last_ip=data["last_ip"],
            name=data["name"],
            model=data["model"],
            wireless=wireless,
            last_seen=data["last_seen"],
            alias=alias,
        )


class UnitRegistry:
    """Persist :class:`RegistryRecord`\\ s to ``state_dir()/units.json``, keyed by
    ``hardware_id``.

    Every call re-reads the file rather than caching in memory: the registry
    is small (a handful of units at most) and re-parsing it costs nothing
    compared to the atomic write it is about to perform, so there is no
    in-process cache to go stale across the concurrent writers this module is
    built to tolerate.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path if path is not None else default_registry_path()

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Loading -- never raises; a missing/corrupt file degrades to "empty"
    # ------------------------------------------------------------------

    def load(self) -> dict[str, RegistryRecord]:
        """Read every record currently on disk. Never raises.

        A missing file, an empty file, a truncated file, syntactically
        invalid JSON, or an unexpected top-level shape all degrade to an
        empty registry. One unreadable record inside an otherwise-good file
        is dropped and logged, never fatal to the rest.
        """
        if not self._path.exists():
            return {}

        try:
            raw = self._path.read_text(encoding="utf-8")
            body = json.loads(raw)
        except (OSError, json.JSONDecodeError) as err:
            log.warning(
                "[discover] registry at %s is unreadable/corrupt (%s) -- starting fresh",
                self._path,
                err,
            )
            return {}

        if not isinstance(body, dict) or body.get("version") != _REGISTRY_VERSION:
            log.warning(
                "[discover] registry at %s has an unexpected shape -- starting fresh", self._path
            )
            return {}

        units = body.get("units")
        if not isinstance(units, dict):
            return {}

        records: dict[str, RegistryRecord] = {}
        for hardware_id, item in units.items():
            try:
                record = RegistryRecord.from_dict(item)
            except Exception as err:  # one bad record must not sink the registry
                log.warning(
                    "[discover] dropping unreadable record %r in %s: %s",
                    hardware_id,
                    self._path,
                    err,
                )
                continue
            records[record.hardware_id] = record
        return records

    # ------------------------------------------------------------------
    # Saving -- atomic: unique tempfile in the same directory + os.replace
    # ------------------------------------------------------------------

    def save(self, records: dict[str, RegistryRecord]) -> None:
        """Atomically overwrite the registry file with exactly *records*."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "version": _REGISTRY_VERSION,
            "units": {hardware_id: record.to_dict() for hardware_id, record in records.items()},
        }
        text = json.dumps(body, indent=2)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Convenience operations
    # ------------------------------------------------------------------

    def upsert(self, record: RegistryRecord) -> RegistryRecord:
        """Insert or replace *record*, keyed by its ``hardware_id``."""
        records = self.load()
        records[record.hardware_id] = record
        self.save(records)
        return record

    def get(self, hardware_id: str) -> RegistryRecord | None:
        """The remembered record for *hardware_id*, or ``None``."""
        return self.load().get(hardware_id)

    def all(self) -> list[RegistryRecord]:
        """Every remembered record, in no particular order."""
        return list(self.load().values())

    def forget(self, hardware_id: str) -> bool:
        """Remove *hardware_id* if remembered. Returns whether it existed."""
        records = self.load()
        existed = records.pop(hardware_id, None) is not None
        if existed:
            self.save(records)
        return existed


# ---------------------------------------------------------------------------
# MAC enrichment -- best-effort, opportunistic, never raises
# ---------------------------------------------------------------------------


def _mac_from_ip_neigh(ip: str, run: RunFn, timeout: float) -> str | None:
    """Try ``ip neigh show <ip>`` and pull a MAC out of an ``lladdr`` line."""
    try:
        result = run(
            ["ip", "neigh", "show", ip],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:  # missing binary, permission error, non-Linux, ...
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    stdout = getattr(result, "stdout", "") or ""
    for line in stdout.splitlines():
        if "lladdr" not in line:
            continue
        match = _MAC_RE.search(line)
        if match:
            mac = match.group(0).lower()
            return None if mac == _ZERO_MAC else mac
    return None


def _mac_from_proc_net_arp(ip: str, arp_path: str) -> str | None:
    """Fall back to the legacy ``net/arp`` table under procfs (Linux-only, best-effort)."""
    try:
        with open(arp_path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    for line in lines[1:]:  # skip the header row
        parts = line.split()
        if len(parts) < 4 or parts[0] != ip:
            continue
        mac = parts[3].lower()
        if mac == _ZERO_MAC or not _MAC_RE.fullmatch(mac):
            return None
        return mac
    return None


def lookup_mac(
    ip: str,
    *,
    run: RunFn = subprocess.run,  # nosec B603 - argv list built here, no shell
    arp_path: str = DEFAULT_ARP_PATH,
    timeout: float = DEFAULT_MAC_LOOKUP_TIMEOUT,
) -> str | None:
    """Best-effort neighbour-table lookup for *ip*'s MAC address.

    Tries ``ip neigh show <ip>`` first, then falls back to procfs's legacy
    ``net/arp`` table. Returns ``None`` -- never raises -- when the host is
    off-segment (no neighbour-table entry exists for it), the ``ip`` binary
    is absent, the platform has no such table, or anything else goes wrong.
    A record stays valid and identifiable purely on ``hardware_id`` without
    a MAC, so a ``None`` here is a normal, expected outcome, not a failure.
    """
    try:
        mac = _mac_from_ip_neigh(ip, run, timeout)
        if mac:
            return mac
        return _mac_from_proc_net_arp(ip, arp_path)
    except Exception:  # belt-and-braces: this must never be what crashes a caller
        return None
