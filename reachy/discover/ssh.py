"""SSH onto a discovered unit, and the SEPARATE one-time key install.

Two capabilities live here, and the boundary between them is the point of the
module:

* :func:`ssh_argv` / :func:`open_shell` — build (and hand to an injected exec
  seam) the argv that opens an interactive shell on a resolved unit. Pure
  argv construction plus one ``execvp``; it reaches nothing that could modify
  the robot.
* :func:`authorize` — the explicit, operator-confirmed ``ssh-copy-id`` push
  that makes subsequent logins passwordless.

**Key install is never a side effect of finding or logging in.** That is a
security property, not a convention: the target was chosen by SCANNING an
unauthenticated LAN service rather than typed by the operator (spec c25 /
c28), so pushing a key at it must be a deliberate, separately-invoked act.
The property is enforced two ways — :func:`authorize` demands an explicit
confirmation callback that returns ``True``, and nothing on the shell-opening
path references it, which ``tests/test_discover_ssh.py`` asserts by walking
this module's own AST call graph.

The stable-identity contract
============================

Every ssh invocation this module builds carries ``-o HostKeyAlias=<alias>``
alongside the resolved IP address. That is what lets ``known_hosts`` key on a
stable name while the unit's DHCP address moves underneath it — and, crucially,
it works with **no ``/etc/hosts`` pin and no privilege** (spec h25). The hosts
pin (``reachy/discover/hosts.py``) needs ``sudo``; stable host-key identity must
not, or unprivileged discovery would only half-work. This module therefore
never reaches the hosts machinery at all.

Two strings that look alike and must never be conflated (spec c34):

* the alias is ``reachy-mini`` — HYPHEN, operator-chosen, matching Pollen's own
  documentation and DNS convention;
* the daemon's ``robot_name`` field reports the UNDERSCORE spelling.

Deriving one from the other by string munging would regenerate the exact name
the co-resident Lite already claims in mDNS, which is the c27 collision. So the
alias is written out as a literal here and this module never reads the daemon's
name field at all.

The password prompt is TOLERATED, never HANDLED
===============================================

The unit ships with a factory-default password, so the first :func:`authorize`
run hits an interactive prompt. This module lets ``ssh``/``ssh-copy-id`` own
that prompt end to end: the child process inherits this process's terminal, so
no prompt, and no typed secret, ever passes through Python. There is no
password parameter, no reader (``input``/``getpass``), no logger, and no stdio
redirection anywhere in this file — each of which is asserted structurally
rather than merely intended.

Stdlib only (:mod:`os`, :mod:`re`, :mod:`shutil`, :mod:`subprocess`), plus the
CLI's own :class:`~reachy.cli._errors.CliError` contract.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 — fixed argv, never shell=True; see _default_run
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from reachy.discover.probe import UnitRecord

#: The documented Pollen default account. ``reachy_mini`` docs
#: ``quickstart.md:21`` and ``troubleshooting.md:349`` both spell the login as
#: ``ssh pollen@reachy-mini``; confirmed with the operator (spec c32).
DEFAULT_SSH_USER = "pollen"

#: Overrides the default account without a flag, for a box whose image differs.
SSH_USER_ENV = "REACHY_WIRELESS_SSH_USER"

#: The stable, operator-chosen host-key alias (spec c33/c34). HYPHENATED, and
#: written out as a literal on purpose — never derived from any daemon field.
DEFAULT_HOST_KEY_ALIAS = "reachy-mini"

SSH_BINARY = "ssh"
SSH_COPY_ID_BINARY = "ssh-copy-id"

#: Options the "is a key already installed?" pre-flight adds. ``BatchMode=yes``
#: is the load-bearing one: it makes ssh FAIL rather than prompt, so the
#: pre-flight can never sit on a password prompt the operator did not expect.
PREFLIGHT_OPTIONS: tuple[str, ...] = ("BatchMode=yes", "ConnectTimeout=5")

#: The cheapest possible remote command — the pre-flight only needs to know
#: whether authentication succeeded, not to do anything on the unit.
PREFLIGHT_COMMAND: tuple[str, ...] = ("true",)

# --------------------------------------------------------------------------- #
# Named refusals — the label is what a caller reports verbatim                 #
# --------------------------------------------------------------------------- #

#: No confirmation callback was supplied at all. Fail-closed: a caller that
#: forgot to wire the operator in must not silently push a key.
REFUSAL_NO_CONFIRMATION = "no-confirmation"

#: The operator was asked and did not answer with an explicit ``True``.
REFUSAL_CONFIRMATION_DECLINED = "confirmation-declined"

#: The target carries no ``hardware_id``, so the confirmation could not name
#: WHICH unit is about to receive a key — refused before the operator is asked.
REFUSAL_MISSING_HARDWARE_ID = "missing-hardware-id"

#: ``ssh-copy-id`` ran and exited non-zero (wrong password, refused connection,
#: host-key mismatch). Carries the exit code in ``detail``.
REFUSAL_COPY_ID_FAILED = "ssh-copy-id-failed"

REFUSALS = frozenset(
    {
        REFUSAL_NO_CONFIRMATION,
        REFUSAL_CONFIRMATION_DECLINED,
        REFUSAL_MISSING_HARDWARE_ID,
        REFUSAL_COPY_ID_FAILED,
    }
)

# --------------------------------------------------------------------------- #
# Argv hygiene                                                                 #
# --------------------------------------------------------------------------- #

#: A host / alias token: no whitespace, no leading ``-`` (which ssh would read
#: as an option), no ``@`` (which would re-split the login target).
_HOST_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

#: A login account token, on the same fail-closed principle.
_USER_TOKEN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def _validated(value: str, pattern: re.Pattern[str], what: str) -> str:
    """Return *value* unchanged, or raise a clean exit-1 :class:`CliError`.

    Fail-closed rather than sanitising: a value that cannot be spelled safely
    on an ssh command line is an operator mistake worth naming, not something
    to quietly rewrite into something that runs.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text or not pattern.match(text):
        raise CliError(
            EXIT_USER_ERROR,
            f"invalid {what}: {value!r}",
            f"pass a plain {what} (letters, digits, dots, hyphens) — "
            "a blank or option-shaped value is refused so it can never be "
            "read as an ssh option",
        )
    return text


def _require_binary(name: str, which: Callable[[str], str | None]) -> str:
    """Resolve *name* on PATH, or raise the environment-error CliError."""
    found = which(name)
    if not found:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{name} not found on PATH",
            f"install an OpenSSH client ({name} ships with openssh-client on "
            "Debian/Ubuntu, openssh-clients on Fedora)",
        )
    return found


# --------------------------------------------------------------------------- #
# Account + alias resolution                                                   #
# --------------------------------------------------------------------------- #


def resolve_user(explicit: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Resolve the login account: explicit > ``REACHY_WIRELESS_SSH_USER`` > default.

    A blank value at either of the first two levels FALLS THROUGH rather than
    being taken literally, because an env var set to the empty string is the
    normal way to spell "unset" — and logging in as nobody is never what was
    meant.
    """
    if explicit is not None and explicit.strip():
        return explicit.strip()
    environ = os.environ if env is None else env
    from_env = environ.get(SSH_USER_ENV, "")
    if from_env.strip():
        return from_env.strip()
    return DEFAULT_SSH_USER


def resolve_alias(explicit: str | None = None) -> str:
    """Resolve the host-key alias: an explicit per-unit value, else the default.

    Unlike :func:`resolve_user` there is no env layer here, so a blank explicit
    alias is an outright mistake rather than a spelling of "unset" — it is
    refused by :func:`_validated` instead of falling through to the default.
    """
    if explicit is None:
        return DEFAULT_HOST_KEY_ALIAS
    return _validated(explicit, _HOST_TOKEN, "host-key alias")


def _option_args(options: Sequence[str]) -> list[str]:
    """Render ``("A=1", "B=2")`` as ``["-o", "A=1", "-o", "B=2"]``."""
    argv: list[str] = []
    for option in options:
        argv.extend(["-o", option])
    return argv


# --------------------------------------------------------------------------- #
# The shell-opening path                                                       #
# --------------------------------------------------------------------------- #


def ssh_argv(
    address: str,
    *,
    user: str | None = None,
    alias: str | None = None,
    env: Mapping[str, str] | None = None,
    options: Sequence[str] = (),
    command: Sequence[str] = (),
) -> list[str]:
    """Build the ssh argv for *address* — argv only, nothing is spawned here.

    The connection TARGET is the resolved IP; the alias only ever appears as
    ``-o HostKeyAlias=<alias>``, so ``known_hosts`` keys on a stable name while
    nothing needs the name to resolve. That is what keeps host-key identity
    working with no ``/etc/hosts`` pin and no privilege.
    """
    host = _validated(address, _HOST_TOKEN, "address")
    account = _validated(resolve_user(user, env), _USER_TOKEN, "ssh user")
    host_key_alias = resolve_alias(alias)
    argv = [SSH_BINARY, "-o", f"HostKeyAlias={host_key_alias}"]
    argv.extend(_option_args(options))
    argv.append(f"{account}@{host}")
    argv.extend(command)
    return argv


def _default_exec(file: str, argv: Sequence[str]) -> None:
    """Replace this process with ssh. Never returns on success."""
    os.execvp(file, list(argv))  # nosec B606 — fixed program, validated argv


def open_shell(
    address: str,
    *,
    user: str | None = None,
    alias: str | None = None,
    env: Mapping[str, str] | None = None,
    options: Sequence[str] = (),
    exec_fn: Callable[[str, list[str]], object] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> object:
    """Open an interactive shell on *address*.

    The exec seam is injected so a test can assert the exact argv without ever
    spawning ssh; in production it is :func:`os.execvp`, which replaces this
    process — so the operator's terminal (and any password prompt on it)
    belongs entirely to ssh.
    """
    argv = ssh_argv(address, user=user, alias=alias, env=env, options=options)
    _require_binary(SSH_BINARY, which or shutil.which)
    runner = exec_fn or _default_exec
    return runner(argv[0], argv)


# --------------------------------------------------------------------------- #
# The key-install path — separate, explicit, and confirmed                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AuthorizeTarget:
    """The unit a key is about to be pushed to, named the way the operator sees it.

    All three fields appear in :func:`confirmation_prompt` because the target
    was chosen by scanning rather than typed: the alias says what it will be
    called, the address says where it is right now, and ``hardware_id`` is the
    only stable fact that says WHICH robot it is.
    """

    address: str
    hardware_id: str
    alias: str = DEFAULT_HOST_KEY_ALIAS

    @classmethod
    def from_record(cls, record: UnitRecord, alias: str | None = None) -> AuthorizeTarget:
        """Adapt a probed :class:`~reachy.discover.probe.UnitRecord`.

        Reads exactly two fields — the probed ``address`` and the stable
        ``hardware_id``. The alias is passed in or defaulted; it is NEVER
        derived from the record's name field.
        """
        return cls(
            address=record.address,
            hardware_id=record.hardware_id,
            alias=resolve_alias(alias),
        )


@dataclass(frozen=True)
class AuthorizeResult:
    """What :func:`authorize` did, in terms a caller can render verbatim.

    ``refusal`` is ``None`` on success and otherwise one of :data:`REFUSALS`.
    ``argv`` holds the ``ssh-copy-id`` invocation that actually ran, and is
    empty whenever none did — which is what makes "a declined confirmation
    invokes ssh-copy-id zero times" observable rather than merely claimed.
    """

    ok: bool
    already_installed: bool
    target: AuthorizeTarget
    argv: tuple[str, ...] = ()
    refusal: str | None = None
    detail: str = ""


def confirmation_prompt(target: AuthorizeTarget) -> str:
    """The text the operator must affirm before any key is pushed.

    It names the alias, the IP and the ``hardware_id``, and says WHY it is
    asking: the address came from a network scan of an unauthenticated
    service, so only the operator can say whether that robot is theirs.
    """
    return (
        f"Install your SSH public key on '{target.alias}' at {target.address} "
        f"(hardware_id {target.hardware_id})?\n"
        "This target was chosen by SCANNING the local network, not typed by you — "
        "confirm it is your robot before continuing."
    )


def ssh_copy_id_argv(
    address: str,
    *,
    user: str | None = None,
    alias: str | None = None,
    env: Mapping[str, str] | None = None,
    identity: str | None = None,
) -> list[str]:
    """Build the ``ssh-copy-id`` argv — argv only, nothing is spawned here.

    ``ssh-copy-id`` APPENDS to the remote ``authorized_keys`` and skips keys
    that are already present. No force / dry-run flag is ever added, so this
    path structurally cannot truncate or replace what is already on the robot.
    """
    host = _validated(address, _HOST_TOKEN, "address")
    account = _validated(resolve_user(user, env), _USER_TOKEN, "ssh user")
    host_key_alias = resolve_alias(alias)
    argv = [SSH_COPY_ID_BINARY, "-o", f"HostKeyAlias={host_key_alias}"]
    if identity is not None:
        argv.extend(["-i", _validated(identity, re.compile(r"^[^\s]+$"), "identity file")])
    argv.append(f"{account}@{host}")
    return argv


def _default_run(argv: Sequence[str]) -> int:
    """Run *argv* with this process's stdio INHERITED, and return its exit code.

    Inheritance is the whole design: ssh writes its own password prompt to the
    terminal and reads the answer itself, so nothing typed by the operator is
    ever seen, buffered, logged or stored by this process. Deliberately not
    :func:`subprocess.run` with any capture — a captured stream would mean the
    prompt passed through here.
    """
    return subprocess.call(list(argv))  # nosec B603 — fixed program, validated argv


def _preflight_argv(
    target: AuthorizeTarget, user: str | None, env: Mapping[str, str] | None
) -> list[str]:
    """The non-interactive "is passwordless login already working?" probe."""
    return ssh_argv(
        target.address,
        user=user,
        alias=target.alias,
        env=env,
        options=PREFLIGHT_OPTIONS,
        command=PREFLIGHT_COMMAND,
    )


def authorize(
    target: AuthorizeTarget,
    *,
    confirm: Callable[[str], bool] | None = None,
    user: str | None = None,
    env: Mapping[str, str] | None = None,
    identity: str | None = None,
    run: Callable[..., int] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> AuthorizeResult:
    """Install this box's SSH public key on *target*, after explicit confirmation.

    The order is load-bearing:

    1. refuse an unidentifiable target — a confirmation that cannot name the
       ``hardware_id`` is not a confirmation;
    2. resolve both binaries, so a missing OpenSSH client is a clean exit-2
       BEFORE the operator is asked to approve anything;
    3. ask ``confirm`` exactly once, and require a strict ``True`` — a truthy
       string is not an explicit affirmative, and fail-closed is the only safe
       direction when the target came from a scan;
    4. only then run anything at all.

    Step 4 begins with a ``BatchMode=yes`` pre-flight: if passwordless login
    already works, the result says so plainly and ``ssh-copy-id`` is not run.
    Otherwise ``ssh-copy-id`` runs once, inheriting the terminal so the unit's
    factory-default password can be typed at ssh's own prompt — this process
    never sees it.
    """
    if not target.hardware_id or not target.hardware_id.strip():
        return AuthorizeResult(
            ok=False,
            already_installed=False,
            target=target,
            refusal=REFUSAL_MISSING_HARDWARE_ID,
            detail=(
                "the target carries no hardware_id, so a confirmation could not "
                "name which unit would receive the key"
            ),
        )

    lookup = which or shutil.which
    _require_binary(SSH_BINARY, lookup)
    _require_binary(SSH_COPY_ID_BINARY, lookup)

    if confirm is None:
        return AuthorizeResult(
            ok=False,
            already_installed=False,
            target=target,
            refusal=REFUSAL_NO_CONFIRMATION,
            detail=(
                "installing a key needs an explicit confirmation callback; "
                "it is never a side effect of finding or logging in"
            ),
        )
    if confirm(confirmation_prompt(target)) is not True:
        return AuthorizeResult(
            ok=False,
            already_installed=False,
            target=target,
            refusal=REFUSAL_CONFIRMATION_DECLINED,
            detail="the operator did not confirm the target; nothing was installed",
        )

    runner = run or _default_run
    if runner(_preflight_argv(target, user, env)) == 0:
        return AuthorizeResult(
            ok=True,
            already_installed=True,
            target=target,
            detail=(
                f"a key is already installed — passwordless login to "
                f"{target.address} already works, so nothing was pushed"
            ),
        )

    argv = ssh_copy_id_argv(
        target.address, user=user, alias=target.alias, env=env, identity=identity
    )
    code = runner(argv)
    if code != 0:
        return AuthorizeResult(
            ok=False,
            already_installed=False,
            target=target,
            argv=tuple(argv),
            refusal=REFUSAL_COPY_ID_FAILED,
            detail=f"ssh-copy-id exited {code}; nothing on the unit was changed by us",
        )
    return AuthorizeResult(
        ok=True,
        already_installed=False,
        target=target,
        argv=tuple(argv),
        detail=f"key appended to authorized_keys on {target.address}",
    )
