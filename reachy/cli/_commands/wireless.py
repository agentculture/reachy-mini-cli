"""``reachy-mini-cli wireless`` — find, remember, pin and log into a Reachy Mini.

The operator-facing front for the :mod:`reachy.discover` package. Every robot
session starts with "where is it"; this noun answers that once and then
remembers, so the address stops being something a human retypes.

Like ``daemon`` and ``service``, ``wireless`` does **not** use a robot
transport: it never calls ``_robot.get_transport`` / ``_robot.noun_overview``
and has no ``--transport`` flag. It speaks plain HTTP to a *candidate* daemon's
``/api/daemon/status`` route (:func:`reachy.discover.probe.probe`), plus
``/etc/hosts`` and ``ssh`` for the two side-effecting verbs. That is what makes
it work on the **bare HTTP remote profile** — a box with neither the ``[sdk]``
nor the ``[daemon]`` extra installed, which is exactly the audience: someone
driving a robot their box is not hosting. Nothing in this module's import
closure reaches ``reachy_mini`` (asserted in ``tests/test_wireless_cli.py``).

What it does NOT do: it never moves an existing default. ``DEFAULT_BASE_URL``
stays ``http://localhost:8000`` and ``reachy/robot/transport.py`` is untouched —
discovery only *supplies* a value the operator or an agent may pass onward,
which is why every ``find`` result carries a ready-made ``base_url``.

Verbs
-----

* ``find`` — sweep the local IPv4 subnets (or probe one explicit ``--address``)
  and report every unit, remembering what it saw. Filters to
  ``wireless_version=true`` by DEFAULT; ``--all`` reveals every Reachy daemon
  the sweep answered for, because a Lite tethered to another box on the LAN is
  discoverable and is *not* wireless. The noun's name describes the default,
  not a limit of the mechanism.
* ``list`` — the remembered units, from the registry alone. No network.
* ``ssh`` — resolve one unit and hand the terminal to ``ssh``.
* ``authorize`` — the SEPARATE, explicitly-confirmed one-time key install.
* ``pin`` / ``unpin`` — the recoverable ``/etc/hosts`` managed block.
* ``forget`` — drop a remembered unit (or all of them). No network.
* ``overview`` — this noun's summary, including the IPv4-and-default-port
  boundary and the trusted-network cost.

Injection seams
---------------

Four module-level names exist so a test never touches the real world, and three
of them are ``None`` in production ON PURPOSE — ``reachy/discover/ssh.py`` owns
the child process's stdio, so the unit's factory-default password prompt is
read by ``ssh`` itself and never passes through Python:

* :data:`_EXEC_SSH` — the exec seam (production: ``os.execvp`` inside
  ``ssh.open_shell``);
* :data:`_RUN_SSH` — the subprocess seam for the pre-flight + ``ssh-copy-id``;
* :data:`_WHICH` — PATH lookup for the two OpenSSH binaries;
* :func:`_read_answer` — the confirmation reader (production: :func:`input`).

:func:`_probe` / :func:`_sweep` / :func:`_mac_for` are the network seams.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Callable

from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.cli._output import emit_diagnostic
from reachy.discover import hosts as hosts_mod
from reachy.discover import probe as probe_mod
from reachy.discover import registry as registry_mod
from reachy.discover import resolve as resolve_mod
from reachy.discover import ssh as ssh_mod
from reachy.discover import sweep as sweep_mod

_JSON_HELP = "Emit structured JSON."

#: Selects a remembered unit without a flag, for a box that always drives one.
UNIT_ENV = "REACHY_WIRELESS_UNIT"

#: The scheme every ``base_url`` this noun hands back is built from.
#:
#: It is ``http`` because that is what the daemon SPEAKS, not a choice made
#: here: ``reachy-mini-daemon`` exposes one plain-HTTP listener and no TLS
#: listener at all, which is why ``reachy/robot/transport.py`` already ships
#: ``DEFAULT_BASE_URL = "http://localhost:8000"``. Discovery only reaches it
#: over loopback or a trusted LAN — the same trusted-network assumption the
#: overview's "Costs and cautions" section states out loud, since the status
#: route the sweep probes is unauthenticated regardless of scheme. Naming the
#: scheme here keeps that decision stated once, where it can be revisited if
#: the daemon ever grows a TLS endpoint, instead of inlined at the format site.
DAEMON_URL_SCHEME = "http"

_VERBS = [
    "wireless find — sweep the LAN (or one --address) for Reachy daemons and remember them",
    "wireless list — the remembered units, from the registry alone (no network)",
    "wireless ssh — open a shell on the resolved unit (never types an address)",
    "wireless authorize — install this box's SSH key on the unit, after explicit confirmation",
    "wireless pin — pin the unit's address to a stable alias in /etc/hosts (needs sudo)",
    "wireless unpin — remove that managed /etc/hosts block",
    "wireless forget — drop a remembered unit from the registry (no network)",
    "wireless overview — this summary",
]

# --------------------------------------------------------------------------- #
# Injection seams — see the module docstring                                   #
# --------------------------------------------------------------------------- #

#: Replace this process with ssh. ``None`` in production so
#: :func:`reachy.discover.ssh.open_shell` uses its own ``os.execvp``.
_EXEC_SSH: Callable[[str, list[str]], object] | None = None

#: Run one ssh / ssh-copy-id invocation and return its exit code. ``None`` in
#: production so :func:`reachy.discover.ssh.authorize` uses its own runner,
#: which INHERITS this process's terminal — no typed password ever reaches here.
_RUN_SSH: Callable[..., int] | None = None

#: PATH lookup for ``ssh`` / ``ssh-copy-id``. ``None`` in production so
#: ``ssh.py`` uses :func:`shutil.which`.
_WHICH: Callable[[str], str | None] | None = None


def _probe(host: str, port: int, timeout: float) -> probe_mod.UnitRecord | None:
    """The one identity probe, as a named module attribute a test can replace."""
    return probe_mod.probe(host, port, timeout)


def _sweep(**kwargs: Any) -> sweep_mod.SweepResult:
    """The bounded LAN sweep, as a named module attribute a test can replace.

    Keyword-only by design: :func:`reachy.discover.resolve.resolve` calls its
    ``sweep_fn`` as ``sweep_fn(port=..., probe_fn=..., **sweep_kwargs)``, so one
    seam serves both this module's ``find`` and the shared resolver.
    """
    return sweep_mod.sweep(**kwargs)


def _mac_for(address: str) -> str | None:
    """Opportunistic neighbour-table MAC lookup; ``None`` whenever unavailable."""
    return registry_mod.lookup_mac(address)


def _read_answer(prompt: str) -> str:
    """Read the operator's confirmation. Never used for a password."""
    return input(prompt)


def _registry() -> registry_mod.UnitRegistry:
    return registry_mod.UnitRegistry()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Address handling — IPv4 is the sweep's scope, v6 stays usable by address     #
# --------------------------------------------------------------------------- #


def _validated_address(address: str) -> str:
    """Return the canonical bare literal for *address*, or raise an exit-1 :class:`CliError`.

    A hostname is refused: this verb takes an address, and a name that silently
    fails to resolve would be reported as "nothing answered there".

    This used to also return a URL-ready (bracketed for IPv6) spelling, because
    ``http://2a0d::1:8000/`` is not a parseable URL. That second value is gone:
    :func:`reachy.discover.probe.probe` now brackets IPv6 itself, at the ONE
    site that formats the URL, so every caller is correct rather than each
    remembering to pre-bracket. Bracketing here as well meant probing a
    bracketed host and then undoing it so the bare form reached the registry —
    a round trip whose only purpose was to survive the missing fix.
    """
    text = (address or "").strip().strip("[]")
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError as err:
        raise CliError(
            EXIT_USER_ERROR,
            f"{address!r} is not an IP address",
            "pass the unit's address, e.g. --address 192.168.1.162 (IPv6 literals are "
            "accepted too) — 'reachy-mini-cli wireless find' discovers it for you",
        ) from err
    return str(parsed)


def _base_url(address: str, port: int) -> str:
    """The ``--base-url`` / ``REACHY_BASE_URL`` value for *address*, ready to pass on."""
    host = address
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{DAEMON_URL_SCHEME}://{host}:{port}"


def _probe_address(address: str, port: int, timeout: float) -> probe_mod.UnitRecord | None:
    """Probe ONE explicit address, reporting it back in its bare (unbracketed) form."""
    return _probe(_validated_address(address), port, timeout)


def _unit_payload(record: probe_mod.UnitRecord, port: int) -> dict[str, Any]:
    return {
        "hardware_id": record.hardware_id,
        "robot_name": record.robot_name,
        "model": record.model,
        "wireless": record.wireless,
        "version": record.version,
        "wlan_ip": record.wlan_ip,
        "address": record.address,
        "port": port,
        "base_url": _base_url(record.address, port),
    }


def _registry_payload(record: registry_mod.RegistryRecord, port: int) -> dict[str, Any]:
    return {
        "hardware_id": record.hardware_id,
        "alias": record.alias,
        "name": record.name,
        "model": record.model,
        "wireless": record.wireless,
        "last_ip": record.last_ip,
        "mac": record.mac,
        "last_seen": record.last_seen,
        "base_url": _base_url(record.last_ip, port),
    }


# --------------------------------------------------------------------------- #
# find                                                                         #
# --------------------------------------------------------------------------- #


def _nothing_found_error(
    seen: Sequence[probe_mod.UnitRecord], *, wireless_only: bool, address: str | None, port: int
) -> CliError:
    """Name exactly WHY nothing came back — a filter, a dark address, or a quiet LAN."""
    if seen and wireless_only:
        names = ", ".join(
            f"{u.robot_name} ({u.hardware_id}) at {u.address} [{u.model}]" for u in seen
        )
        return CliError(
            EXIT_USER_ERROR,
            f"{len(seen)} Reachy daemon(s) answered, but none reports wireless_version=true",
            f"re-run with --all to list every Reachy daemon found — a Lite tethered to "
            f"another box on the LAN is discoverable and is not wireless: {names}",
        )
    if address:
        return CliError(
            EXIT_USER_ERROR,
            f"no Reachy daemon answered at {address} on port {port}",
            "check the unit is powered on and reachable, or pass --port for a daemon on a "
            "non-default port",
        )
    return CliError(
        EXIT_USER_ERROR,
        "no Reachy Mini unit answered on this network",
        f"check the unit is powered on and joined to this LAN. The sweep covers IPv4 "
        f"/24-or-narrower subnets on port {port} only — for a unit on IPv6, on another "
        f"subnet, or on a different port, pass --address (and --port) explicitly",
    )


def _remember(
    units: Sequence[probe_mod.UnitRecord], *, clock: Callable[[], str] | None = None
) -> list[str]:
    """Upsert every reported unit, carrying an existing ``mac``/``alias`` forward."""
    registry = _registry()
    known = registry.load()
    now = (clock or _now_iso)()
    remembered: list[str] = []
    for unit in units:
        existing = known.get(unit.hardware_id)
        mac = existing.mac if existing is not None and existing.mac else _mac_for(unit.address)
        registry.upsert(
            registry_mod.RegistryRecord(
                hardware_id=unit.hardware_id,
                mac=mac,
                last_ip=unit.address,
                name=unit.robot_name,
                model=unit.model,
                wireless=unit.wireless,
                last_seen=now,
                alias=existing.alias if existing is not None else None,
            )
        )
        remembered.append(unit.hardware_id)
    return remembered


def cmd_wireless_find(args: argparse.Namespace) -> int:
    port = int(args.port)
    json_mode = bool(getattr(args, "json", False))
    wireless_only = not bool(args.all)

    if args.address:
        record = _probe_address(args.address, port, float(args.timeout))
        seen: tuple[probe_mod.UnitRecord, ...] = (record,) if record is not None else ()
        result: sweep_mod.SweepResult | None = None
    else:
        result = _sweep(
            port=port,
            probe_fn=_probe,
            timeout=float(args.timeout),
            deadline_s=float(args.deadline),
        )
        seen = tuple(result.units)

    units = tuple(u for u in seen if u.wireless) if wireless_only else seen
    if not units:
        raise _nothing_found_error(
            seen, wireless_only=wireless_only, address=args.address, port=port
        )

    remembered = _remember(units)
    payload: dict[str, Any] = {
        "units": [_unit_payload(u, port) for u in units],
        "count": len(units),
        "found_total": len(seen),
        "wireless_only": wireless_only,
        "port": port,
        "remembered": remembered,
        "hosts_total": result.hosts_total if result is not None else 1,
        "hosts_probed": result.hosts_probed if result is not None else 1,
        "deadline_reached": bool(result.deadline_reached) if result is not None else False,
        "elapsed_s": round(result.elapsed_s, 3) if result is not None else None,
    }
    if payload["deadline_reached"]:
        emit_diagnostic(
            "the sweep hit its overall deadline; this list may be incomplete — "
            "raise --deadline or narrow the search with --address"
        )
    emit_payload(payload, json_mode=json_mode)
    return 0


# --------------------------------------------------------------------------- #
# list / forget — registry only, never the network                             #
# --------------------------------------------------------------------------- #


def cmd_wireless_list(args: argparse.Namespace) -> int:
    port = int(args.port)
    records = sorted(_registry().all(), key=lambda r: (r.alias or "", r.hardware_id))
    payload = {
        "units": [_registry_payload(r, port) for r in records],
        "count": len(records),
        "registry": str(registry_mod.default_registry_path()),
    }
    emit_payload(payload, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_wireless_forget(args: argparse.Namespace) -> int:
    registry = _registry()
    records = registry.all()
    if args.all:
        forgotten = [r.hardware_id for r in records]
        for hardware_id in forgotten:
            registry.forget(hardware_id)
    else:
        selector = (args.unit or "").strip()
        if not selector:
            raise CliError(
                EXIT_USER_ERROR,
                "forget needs to know WHICH unit to drop",
                "pass --unit <hardware_id-or-alias> ('reachy-mini-cli wireless list' shows "
                "both), or --all to clear the registry",
            )
        matches = [r for r in records if selector in (r.hardware_id, r.alias)]
        if not matches:
            raise CliError(
                EXIT_USER_ERROR,
                f"no remembered unit matches {selector!r}",
                "run 'reachy-mini-cli wireless list' to see what is remembered",
            )
        forgotten = [r.hardware_id for r in matches]
        for hardware_id in forgotten:
            registry.forget(hardware_id)
    payload = {"forgotten": forgotten, "count": len(forgotten)}
    emit_payload(payload, json_mode=bool(getattr(args, "json", False)))
    return 0


# --------------------------------------------------------------------------- #
# ssh / authorize                                                              #
# --------------------------------------------------------------------------- #


def _resolved(args: argparse.Namespace) -> resolve_mod.ResolvedUnit:
    """Resolve exactly one unit: ``--unit`` > ``REACHY_WIRELESS_UNIT`` > the only one.

    Every refusal (unknown selector, ambiguity, a unit that cannot be found) is
    a :class:`CliError` raised by :func:`reachy.discover.resolve.resolve` — this
    module adds no second copy of those rules.
    """
    return resolve_mod.resolve(
        getattr(args, "unit", None),
        default=os.environ.get(UNIT_ENV) or None,
        registry=_registry(),
        probe_fn=_probe,
        sweep_fn=_sweep,
        port=int(args.port),
    )


def cmd_wireless_ssh(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    resolved = _resolved(args)
    unit = resolved.unit
    alias = ssh_mod.resolve_alias(args.alias)
    argv = ssh_mod.ssh_argv(unit.address, user=args.user, alias=alias)
    payload = {
        "unit": _unit_payload(unit, int(args.port)),
        "user": ssh_mod.resolve_user(args.user),
        "alias": alias,
        "reason": resolved.reason,
        "argv": argv,
        "executed": not args.dry_run,
    }
    if args.dry_run:
        emit_payload(payload, json_mode=json_mode)
    else:
        if json_mode:
            # Emitted BEFORE the exec: this process is about to be replaced, so
            # an agent's structured result has to leave the pipe first.
            emit_payload(payload, json_mode=True)
        else:
            emit_diagnostic(
                f"connecting to {unit.robot_name} ({unit.hardware_id}) at {unit.address} "
                f"as {payload['user']} ..."
            )
        # Never returns in production: os.execvp replaces this process, so ssh
        # owns the operator's terminal from here on.
        ssh_mod.open_shell(
            unit.address,
            user=args.user,
            alias=alias,
            exec_fn=_EXEC_SSH,
            which=_WHICH,
        )
    # The ONE terminus, and the only exit code this handler can produce: every
    # failure left by raising CliError (the repo-wide contract in
    # reachy/cli/_errors.py), and in production the exec path never gets here.
    return 0


def _confirm(args: argparse.Namespace) -> Callable[[str], bool]:
    """Build the strict-``True`` confirmation callback :func:`ssh.authorize` demands."""

    def confirm(prompt: str) -> bool:
        if args.yes:
            return True
        try:
            answer = _read_answer(f"{prompt}\nInstall the key? [y/N] ")
        except (EOFError, KeyboardInterrupt, OSError):
            # A non-interactive stdin is a DECLINE, never an accident-accept.
            return False
        return answer.strip().lower() in ("y", "yes")

    return confirm


def cmd_wireless_authorize(args: argparse.Namespace) -> int:
    resolved = _resolved(args)
    target = ssh_mod.AuthorizeTarget.from_record(resolved.unit, args.alias)
    result = ssh_mod.authorize(
        target,
        confirm=_confirm(args),
        user=args.user,
        identity=args.identity,
        run=_RUN_SSH,
        which=_WHICH,
    )
    if not result.ok:
        raise CliError(
            EXIT_USER_ERROR,
            f"key install refused: {result.refusal}",
            result.detail
            or "re-run 'reachy-mini-cli wireless authorize' and confirm the target, or "
            "pass --yes to confirm it up front",
        )
    payload = {
        "unit": _unit_payload(resolved.unit, int(args.port)),
        "user": ssh_mod.resolve_user(args.user),
        "alias": target.alias,
        "already_installed": result.already_installed,
        "argv": list(result.argv),
        "detail": result.detail,
    }
    emit_payload(payload, json_mode=bool(getattr(args, "json", False)))
    return 0


# --------------------------------------------------------------------------- #
# pin / unpin                                                                  #
# --------------------------------------------------------------------------- #


def cmd_wireless_pin(args: argparse.Namespace) -> int:
    hardware_id: str | None = None
    if args.address:
        address = _validated_address(args.address)
    else:
        resolved = _resolved(args)
        address = resolved.unit.address
        hardware_id = resolved.unit.hardware_id
    aliases = tuple(args.alias_name) if args.alias_name else hosts_mod.DEFAULT_ALIASES
    changed = hosts_mod.pin(address, aliases=aliases, path=args.hosts_path)
    payload = {
        "address": address,
        "aliases": list(aliases),
        "changed": changed,
        "hardware_id": hardware_id,
        "hosts_path": str(args.hosts_path or hosts_mod.DEFAULT_HOSTS_PATH),
    }
    emit_payload(payload, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_wireless_unpin(args: argparse.Namespace) -> int:
    changed = hosts_mod.unpin(path=args.hosts_path)
    payload = {
        "changed": changed,
        "hosts_path": str(args.hosts_path or hosts_mod.DEFAULT_HOSTS_PATH),
    }
    emit_payload(payload, json_mode=bool(getattr(args, "json", False)))
    return 0


# --------------------------------------------------------------------------- #
# overview                                                                     #
# --------------------------------------------------------------------------- #


def cmd_wireless_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "Scope",
            "items": [
                f"IPv4 only, and only the default daemon port {probe_mod.DEFAULT_PORT} "
                "(override with --port)",
                "the sweep covers local /24-or-narrower subnets; wider prefixes, loopback "
                "and docker/bridge interfaces are excluded by construction",
                "a unit on IPv6, on another subnet, or behind a router stays usable by "
                "explicit address: 'wireless find --address <ip>' accepts an IPv6 literal, "
                "and every result carries a ready-made base_url",
                "'find' filters to wireless_version=true by DEFAULT; --all reveals every "
                "Reachy daemon found, including a Lite tethered to another box",
                "no transport flag: this noun talks HTTP to a candidate daemon, /etc/hosts "
                "and ssh — never the reachy_mini SDK, so it works with no extras installed",
            ],
        },
        {
            "title": "State",
            "items": [
                f"registry: {registry_mod.default_registry_path()} (keyed by hardware_id)",
                f"hosts block: {hosts_mod.BEGIN_MARKER} ... {hosts_mod.END_MARKER} "
                f"in {hosts_mod.DEFAULT_HOSTS_PATH}",
                f"ssh account: {ssh_mod.DEFAULT_SSH_USER} (--user, or ${ssh_mod.SSH_USER_ENV})",
                f"host-key alias: {ssh_mod.DEFAULT_HOST_KEY_ALIAS} "
                "(passed as -o HostKeyAlias, so it needs no /etc/hosts pin and no privilege)",
                f"unit selector: --unit <hardware_id-or-alias>, or ${UNIT_ENV}",
            ],
        },
        {
            "title": "Costs and cautions",
            "items": [
                "discovery assumes a TRUSTED network: the daemon's status route is "
                "unauthenticated, so on a shared LAN a sweep finds robots that are not yours",
                "the unit ships with a factory-default password for the "
                f"'{ssh_mod.DEFAULT_SSH_USER}' account — discovery makes the robot easier to "
                "find, so changing that password is the operator's first move",
                "'pin'/'unpin' write /etc/hosts and need sudo; everything else needs no "
                "privilege at all",
                "'authorize' is never a side effect of 'find' or 'ssh': it asks first, names "
                "the hardware_id, and appends to authorized_keys (never truncates)",
            ],
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "results to stdout, errors/diagnostics to stderr (never mixed)",
                "exit codes: 0 ok, 1 user error (nothing found, ambiguous, refused), "
                "2 environment (unwritable /etc/hosts, missing ssh client)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli wireless",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_wireless_overview(args)


# --------------------------------------------------------------------------- #
# Registration                                                                 #
# --------------------------------------------------------------------------- #


def _add_port(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--port",
        type=int,
        default=probe_mod.DEFAULT_PORT,
        help=f"Daemon port to probe (default: {probe_mod.DEFAULT_PORT}).",
    )


def _add_unit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--unit",
        default=None,
        metavar="HARDWARE_ID_OR_ALIAS",
        help=f"Which remembered unit to act on (env {UNIT_ENV}).",
    )


def _add_ssh_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--user",
        default=None,
        help=f"SSH login account (default: {ssh_mod.DEFAULT_SSH_USER}; "
        f"env {ssh_mod.SSH_USER_ENV}).",
    )
    parser.add_argument(
        "--alias",
        default=None,
        help=f"Host-key alias passed as -o HostKeyAlias "
        f"(default: {ssh_mod.DEFAULT_HOST_KEY_ALIAS}).",
    )


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "wireless",
        help="Find, remember and log into a Reachy Mini on the LAN "
        "(see 'reachy-mini-cli wireless overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="wireless_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the wireless noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_wireless_overview)

    find = noun_sub.add_parser("find", help="Discover Reachy daemons on the local network.")
    find.add_argument("--json", action="store_true", help=_JSON_HELP)
    find.add_argument(
        "--all",
        action="store_true",
        help="Report every Reachy daemon found, not only wireless_version=true units.",
    )
    find.add_argument(
        "--address",
        default=None,
        help="Probe exactly this address instead of sweeping (accepts an IPv6 literal).",
    )
    _add_port(find)
    find.add_argument(
        "--timeout",
        type=float,
        default=sweep_mod.DEFAULT_PROBE_TIMEOUT,
        help=f"Per-host probe timeout in seconds "
        f"(default: {sweep_mod.DEFAULT_PROBE_TIMEOUT:g}).",
    )
    find.add_argument(
        "--deadline",
        type=float,
        default=sweep_mod.DEFAULT_DEADLINE_S,
        help=f"Hard overall sweep deadline in seconds "
        f"(default: {sweep_mod.DEFAULT_DEADLINE_S:g}).",
    )
    find.set_defaults(func=cmd_wireless_find)

    lst = noun_sub.add_parser("list", help="List remembered units (registry only, no network).")
    lst.add_argument("--json", action="store_true", help=_JSON_HELP)
    _add_port(lst)
    lst.set_defaults(func=cmd_wireless_list)

    sh = noun_sub.add_parser("ssh", help="Open a shell on the resolved unit.")
    sh.add_argument("--json", action="store_true", help=_JSON_HELP)
    _add_unit(sh)
    _add_ssh_args(sh)
    _add_port(sh)
    sh.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the resolved unit and the ssh argv without connecting.",
    )
    sh.set_defaults(func=cmd_wireless_ssh)

    auth = noun_sub.add_parser(
        "authorize",
        help="Install this box's SSH key on the unit (asks first; never implicit).",
    )
    auth.add_argument("--json", action="store_true", help=_JSON_HELP)
    _add_unit(auth)
    _add_ssh_args(auth)
    _add_port(auth)
    auth.add_argument(
        "--identity",
        default=None,
        help="Public key file to install (passed to ssh-copy-id -i).",
    )
    auth.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the target up front instead of answering the interactive prompt.",
    )
    auth.set_defaults(func=cmd_wireless_authorize)

    pin = noun_sub.add_parser(
        "pin", help="Pin the unit's address to a stable /etc/hosts alias (needs sudo)."
    )
    pin.add_argument("--json", action="store_true", help=_JSON_HELP)
    _add_unit(pin)
    _add_port(pin)
    pin.add_argument(
        "--address",
        default=None,
        help="Pin this address instead of resolving a unit.",
    )
    pin.add_argument(
        "--alias",
        dest="alias_name",
        action="append",
        default=None,
        metavar="NAME",
        help=f"Alias to pin (repeatable; default: {' '.join(hosts_mod.DEFAULT_ALIASES)}).",
    )
    pin.add_argument(
        "--hosts-path",
        default=None,
        help=f"Hosts file to edit (default: {hosts_mod.DEFAULT_HOSTS_PATH}).",
    )
    pin.set_defaults(func=cmd_wireless_pin)

    unpin = noun_sub.add_parser("unpin", help="Remove the managed /etc/hosts block.")
    unpin.add_argument("--json", action="store_true", help=_JSON_HELP)
    unpin.add_argument(
        "--hosts-path",
        default=None,
        help=f"Hosts file to edit (default: {hosts_mod.DEFAULT_HOSTS_PATH}).",
    )
    unpin.set_defaults(func=cmd_wireless_unpin)

    forget = noun_sub.add_parser("forget", help="Drop a remembered unit from the registry.")
    forget.add_argument("--json", action="store_true", help=_JSON_HELP)
    _add_unit(forget)
    forget.add_argument(
        "--all",
        action="store_true",
        help="Forget every remembered unit.",
    )
    forget.set_defaults(func=cmd_wireless_forget)
