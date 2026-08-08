"""A recoverable, delimited managed block in ``/etc/hosts``.

Pins the robot's **stable alias** to its **volatile IP**, so every tool on the
box — not just this CLI — resolves the unit by name.

Why this exists
---------------

On this box ``reachy-mini`` and ``reachy-mini.local`` BOTH fail to resolve, and
only ``reachy-mini-2.local`` answers: the co-resident Reachy Mini **Lite**
claimed the base mDNS name first, so avahi handed the Wireless unit the ``-2``
collision suffix. Pollen's own documented ``ssh pollen@reachy-mini`` therefore
fails here. Pinning the alias is what makes it work — load-bearing, not a
convenience.

That also fixes the alias's *provenance*: the ``-2`` suffix can MOVE (if the
Lite is absent at boot, or the claim order flips), so the alias must never be
derived from mDNS. Nor from the daemon's own ``robot_name`` field, which
reports the underscore form ``reachy_mini``. It is a constant this module owns:
:data:`DEFAULT_ALIASES`.

The pinned line carries BOTH names on one line::

    <ip> reachy-mini reachy-mini.local

The plain ``reachy-mini`` is PRIMARY. The ``.local`` form is an *additional
convenience only*: ``.local`` is the mDNS domain, and some ``nsswitch.conf``
configurations route it exclusively to mDNS (``mdns4_minimal``/``mdns_minimal``
ahead of, or instead of, ``files``), bypassing ``/etc/hosts`` entirely.
**Correctness must never depend on the ``.local`` form resolving through
files** — that is why the plain name is the one everything else in this feature
uses, and why nothing here tests a lookup.

Blast radius, and what is done about it
---------------------------------------

This box's entire ``/etc/hosts`` is 56 bytes, root-owned, and holds the
``localhost`` line an enormous amount of software depends on. Losing that line
breaks name resolution box-wide. So every mutation goes through
:func:`write_document`, which:

1. refuses outright if the file is missing, unreadable, unwritable, or already
   fails :func:`document_is_safe` — never modifying a file it did not break;
2. writes ``<hosts>.reachy-mini-cli.bak`` holding the **exact pre-write bytes**
   before the first modification;
3. stages the new content in a temp file **in the same directory** (so the
   rename is atomic), carrying the original's mode and — best effort — its
   ownership, because a 0600 temp file renamed onto a 0644 ``/etc/hosts``
   breaks every non-root reader on the box;
4. ``os.replace``\\ s it in;
5. **re-reads the landed file** and re-checks :func:`document_is_safe`;
6. restores the pre-write bytes and raises a clean exit-2
   :class:`~reachy.cli._errors.CliError` if any of 4-5 failed — including the
   torn-write case where the rename damaged the destination before failing.

Only the block between :data:`BEGIN_MARKER` and :data:`END_MARKER` is ever
rewritten; every byte outside it is preserved verbatim, trailing whitespace,
CRLF endings and a missing final newline included. :func:`pin` is idempotent —
re-pinning the same address writes nothing at all, and re-pinning a MOVED unit
replaces the stale address rather than appending a second line.
:func:`unpin` removes the block and leaves everything else untouched, so
``pin`` followed by ``unpin`` returns the file **byte-identical** to what it
was, whatever shape it had.

The one insertion, and how it is taken back
-------------------------------------------

A block appended to a document that does **not** end in a newline needs one:
you cannot put a line after an unterminated line. That inserted byte is the
single thing standing between this module and the round trip above, so it is
recorded — in the file itself, not beside it. The managed block simply carries
the document's own final-newline property: appended to an unterminated
document the block is written **without** its own trailing newline, so the file
still does not end in one, and :func:`unpin` reads that back off the bytes in
front of it and removes the newline immediately before the block.

That is deliberately not the ``.bak``'s job even though the backup holds the
exact pre-write bytes: ``unpin`` routinely runs long after ``pin``, from
another process and another day, by which time the backup is stale (it tracks
the *immediately* preceding write) or gone. State that lives anywhere but in
the file being edited is state that can disagree with it.

The hosts path is a **parameter** (defaulting to :data:`DEFAULT_HOSTS_PATH`)
precisely so the test suite never touches the real ``/etc/hosts`` — and it is
also a documented operator flag (``--hosts-path``) for a box whose hosts file
lives somewhere else.

That makes it **operator-controlled data reaching the filesystem**, so it goes
through ONE boundary before any ``open``/``write``/``rename`` sees it:
:func:`_safe_hosts_path`. Every public entry point that takes ``path=``
(:func:`pin`, :func:`unpin`, :func:`pinned_address`, :func:`write_document`,
:func:`backup_path`) funnels through it, and everything downstream — the
staged temp file, the backup, the rollback — is derived from the value the
validator RETURNED, never from the caller's raw argument. One boundary, not a
check per call site: a second check is a second thing to forget.

Stdlib only.
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
import tempfile
from pathlib import Path

from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

#: The file this module manages a block inside. A DEFAULT, never a constant the
#: code reaches for directly — every entry point takes ``path=``.
DEFAULT_HOSTS_PATH = Path("/etc/hosts")

#: The delimiters. Everything between them is ours; everything outside is the
#: operator's and is preserved byte for byte.
BEGIN_MARKER = "# BEGIN reachy-mini-cli"
END_MARKER = "# END reachy-mini-cli"

#: A human-readable note inside the block. ASCII only: a hosts file is read by
#: a lot of very old parsers.
MANAGED_NOTE = "# Managed by reachy-mini-cli - 'reachy wireless pin' / 'reachy wireless unpin'."

#: Appended to the hosts filename for the pre-write backup.
BACKUP_SUFFIX = ".reachy-mini-cli.bak"

#: The PRIMARY alias: operator-chosen, never harvested from mDNS (which offers
#: the movable ``reachy-mini-2``) and never from the daemon's ``robot_name``
#: (which reports the underscore form ``reachy_mini``).
PRIMARY_ALIAS = "reachy-mini"

#: The ``.local`` convenience form. See the module docstring: nsswitch may route
#: this exclusively to mDNS, so nothing may DEPEND on it resolving from files.
LOCAL_ALIAS = "reachy-mini.local"

#: Both names go on ONE line, primary first.
DEFAULT_ALIASES: tuple[str, ...] = (PRIMARY_ALIAS, LOCAL_ALIAS)

#: The name whose survival is the whole point of the post-write verification.
LOCALHOST = "localhost"

#: Conservative hostname charset. Anything outside it — whitespace, ``#``, a
#: newline — could inject a line into the file (including a forged END marker),
#: so aliases are validated fail-closed rather than escaped.
_ALIAS_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")

#: RFC 1035's limit on a full domain name.
_MAX_ALIAS_LEN = 253

#: The charset a single component of the hosts path may hold, applied with
#: :meth:`re.Pattern.fullmatch` to every component of the RESOLVED path. An
#: allow-list rather than a deny-list, and deliberately narrow: it admits the
#: shapes a real hosts path takes (``/etc/hosts``, a ``tmp_path`` stand-in, a
#: home-relative path once expanded) and refuses quoting, globbing and
#: shell-metacharacter payloads outright. A path this rejects is refused, never
#: escaped or repaired.
_PATH_COMPONENT_RE = re.compile(r"[A-Za-z0-9._+@~,=: -]{1,255}")

#: Components that must never survive normalisation. :meth:`Path.resolve`
#: already collapses them; re-checking is cheap and makes the refusal explicit
#: rather than a property of a stdlib call someone could later swap out.
_TRAVERSAL_COMPONENTS = frozenset({".", ".."})

#: The one remediation every path refusal points at.
_PATH_HINT = (
    "pass --hosts-path an absolute path to an EXISTING regular hosts file, e.g. "
    "--hosts-path /etc/hosts — this tool edits a hosts file in place and never creates one"
)


def _safe_hosts_path(path: Path | str | None) -> Path:
    """The ONE boundary an operator-supplied hosts path crosses. Fail-closed.

    ``--hosts-path`` (and the ``path=`` parameter behind it) is operator input
    that ends up in :func:`open`, :meth:`Path.write_bytes`, :func:`os.replace`
    and :func:`tempfile.mkstemp`. Rather than sprinkling checks over those call
    sites, every entry point normalises through here FIRST and then uses only
    the returned value.

    What the returned path is guaranteed to be:

    * **absolute and symlink-free** — ``expanduser()`` then ``resolve()``, so a
      relative path, a ``~`` prefix, a ``..`` segment and a symlinked directory
      are all collapsed BEFORE anything is opened, and what is checked is
      exactly what is later written;
    * **rebuilt from validated components** — each component of the resolved
      path is matched against :data:`_PATH_COMPONENT_RE` and the path is
      reassembled from the MATCHED text, so a NUL byte, a newline, a quote or a
      shell metacharacter cannot reach a filesystem call;
    * **an existing regular file inside an existing directory** — a directory,
      a FIFO, a device node, a dangling symlink and a missing file are all
      refused. This module edits a hosts file in place; it never creates one,
      and it must not be pointed at something that only looks like one.

    ``None`` means "the default", :data:`DEFAULT_HOSTS_PATH`, which is a module
    constant and not operator input — it is returned as-is on purpose, so the
    production path is not gated on ``/etc/hosts`` passing an existence probe
    twice.

    Exit codes follow the module's existing split: a path that is malformed as
    INPUT is an exit-1 user error (like :func:`_validated_address`), while a
    well-formed path naming something absent or wrong on this box is an exit-2
    environment error (like :func:`_read_text`).
    """
    if path is None:
        return DEFAULT_HOSTS_PATH
    if not isinstance(path, (str, Path)):
        raise CliError(
            EXIT_USER_ERROR,
            f"{path!r} is not a filesystem path",
            _PATH_HINT,
        )

    raw = str(path)
    if not raw.strip():
        raise CliError(EXIT_USER_ERROR, "the hosts path is empty", _PATH_HINT)
    if any(ord(char) < 0x20 or char == "\x7f" for char in raw):
        # NUL, newline, tab, DEL: none of them belong in a hosts path, and a
        # NUL in particular truncates the name every C-level open() sees.
        raise CliError(
            EXIT_USER_ERROR,
            "the hosts path holds a control character",
            _PATH_HINT,
        )

    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError) as err:
        # A symlink loop (ELOOP) or an un-expandable '~'.
        raise CliError(
            EXIT_ENV_ERROR,
            f"the hosts path {raw!r} could not be resolved: {err}",
            _PATH_HINT,
        ) from err

    parts = resolved.parts
    if not parts or not resolved.anchor:  # pragma: no cover - resolve() absolutises
        raise CliError(EXIT_USER_ERROR, f"{raw!r} is not an absolute path", _PATH_HINT)

    safe = Path(parts[0])
    for part in parts[1:]:
        match = _PATH_COMPONENT_RE.fullmatch(part)
        if match is None or match.group(0) in _TRAVERSAL_COMPONENTS:
            raise CliError(
                EXIT_USER_ERROR,
                f"{part!r} is not a usable component of a hosts path",
                _PATH_HINT,
            )
        # Rebuilt from the MATCH, not from the caller's string: what continues
        # downstream is the validator's own output.
        safe = safe / match.group(0)

    if not safe.parent.is_dir():
        raise CliError(
            EXIT_ENV_ERROR,
            f"{safe.parent} is not an existing directory",
            _PATH_HINT,
        )
    if not safe.exists():
        raise CliError(
            EXIT_ENV_ERROR,
            f"{safe} does not exist",
            _PATH_HINT,
        )
    if not safe.is_file():
        raise CliError(
            EXIT_ENV_ERROR,
            f"{safe} is not a regular file",
            _PATH_HINT,
        )
    return safe


def _replace(src: str, dst: Path) -> None:
    """Indirection over :func:`os.replace`.

    A named seam, not decoration: it is what lets a test simulate a rename that
    fails — including one that damages the destination on the way out — without
    monkeypatching :func:`os.replace` globally for the whole process.
    """
    os.replace(src, dst)


def backup_path(path: Path | str) -> Path:
    """``<hosts>.reachy-mini-cli.bak`` beside *path*.

    Derived from the VALIDATED path, never from the raw argument: the backup is
    written and the rollback is read through this name, so it has to be exactly
    as constrained as the file it protects — and it has to agree, byte for byte
    in its string form, with what :func:`write_document` computes internally.
    """
    p = _safe_hosts_path(path)
    return p.with_name(p.name + BACKUP_SUFFIX)


# ---------------------------------------------------------------------------
# parsing / verification
# ---------------------------------------------------------------------------


def _address_or_none(token: str) -> str | None:
    """*token* if it is an IP address (``%zone`` suffix tolerated), else ``None``."""
    bare = token.split("%", 1)[0]
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        return None
    return token


def parse_hosts(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Parse a hosts document into ``(address, (name, ...))`` entries.

    Comments (``#`` to end of line) and blank lines are ignored. Raises
    :class:`ValueError` on a line that is not ``<address> <name> [name ...]``.

    Deliberately STRICT. A hosts file this rejects is one this module refuses to
    modify at all (see :func:`write_document`) — a conservative refusal is
    always safer here than a best-effort rewrite of a file we do not understand.
    """
    entries: list[tuple[str, tuple[str, ...]]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            raise ValueError(f"line {lineno}: expected an address and at least one hostname")
        address = _address_or_none(fields[0])
        if address is None:
            raise ValueError(f"line {lineno}: {fields[0]!r} is not an IP address")
        entries.append((address, tuple(fields[1:])))
    return tuple(entries)


def resolves_localhost(text: str) -> bool:
    """Does *text* map a LOOPBACK address to ``localhost``?

    The loopback requirement matters: a stray ``10.0.0.5 localhost`` carries the
    name but is not a working localhost, and accepting it would let the safety
    net pass a file that is functionally broken.

    Never raises — a document that does not parse simply does not resolve
    anything.
    """
    try:
        entries = parse_hosts(text)
    except ValueError:
        return False
    for address, names in entries:
        if not any(name.lower() == LOCALHOST for name in names):
            continue
        try:
            if ipaddress.ip_address(address.split("%", 1)[0]).is_loopback:
                return True
        except ValueError:  # pragma: no cover - parse_hosts already validated it
            continue
    return False


def document_is_safe(text: str) -> bool:
    """The one predicate the pre-check and the post-write verification share.

    A document is safe when it parses AND still resolves ``localhost``. Using
    ONE predicate on both sides is what makes the promise precise: this module
    never leaves the file in a state it would itself have refused to accept.
    """
    try:
        parse_hosts(text)
    except ValueError:
        return False
    return resolves_localhost(text)


# ---------------------------------------------------------------------------
# the managed block
# ---------------------------------------------------------------------------


def split_document(text: str) -> tuple[str, str | None, str]:
    """Split *text* into ``(before, block, after)`` around the managed block.

    ``block`` is ``None`` when there is no managed block. ``before`` and
    ``after`` are raw slices — line endings, trailing whitespace and a missing
    final newline all survive a ``before + block + after`` round trip exactly.

    Raises :class:`CliError` when a BEGIN marker has no matching END: treating
    "the rest of the file" as the block would delete every operator line after
    it, so a half-written block is refused rather than guessed at.
    """
    lines = text.splitlines(keepends=True)
    begin = None
    for index, line in enumerate(lines):
        if line.strip() == BEGIN_MARKER:
            begin = index
            break
    if begin is None:
        return text, None, ""
    for index in range(begin + 1, len(lines)):
        if lines[index].strip() == END_MARKER:
            return (
                "".join(lines[:begin]),
                "".join(lines[begin : index + 1]),
                "".join(lines[index + 1 :]),
            )
    raise CliError(
        EXIT_ENV_ERROR,
        f"the hosts file has a {BEGIN_MARKER!r} line with no matching {END_MARKER!r}",
        "repair or delete the half-written block by hand, then re-run — this tool refuses "
        "to guess where an unterminated managed block ends",
    )


def managed_block(text: str) -> str | None:
    """The managed block's text, or ``None`` when there is none."""
    _before, block, _after = split_document(text)
    return block


def render_block(
    address: str,
    aliases: tuple[str, ...] | list[str],
    *,
    terminated: bool = True,
) -> str:
    """Render the managed block for *address*. Ends with a newline by default.

    ``terminated=False`` renders the block WITHOUT its trailing newline, which
    is how a block appended to a document that did not end in one records that
    fact (see the module docstring). Only :func:`pin` passes it: a caller that
    just wants the canonical block gets the canonical, newline-terminated one.
    """
    names = " ".join(aliases)
    block = f"{BEGIN_MARKER}\n{MANAGED_NOTE}\n{address} {names}\n{END_MARKER}\n"
    return block if terminated else block[: -len("\n")]


def _validated_address(address: str) -> str:
    """Normalise *address*, or raise an exit-1 :class:`CliError`.

    A hostname is refused on purpose: an ``/etc/hosts`` entry maps an ADDRESS to
    names, and pinning a name to a name is silently useless.
    """
    try:
        return str(ipaddress.ip_address(str(address).strip()))
    except ValueError as err:
        raise CliError(
            EXIT_USER_ERROR,
            f"{address!r} is not an IP address",
            "pass the unit's IPv4 address, e.g. 192.168.1.162 — 'reachy wireless find --json' "
            "reports it",
        ) from err


def _validated_aliases(aliases: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate the alias list fail-closed, or raise an exit-1 :class:`CliError`.

    Fail-closed rather than escaped: an alias carrying whitespace, ``#`` or a
    newline could inject arbitrary lines into ``/etc/hosts`` — including a
    forged END marker that would orphan the rest of the block.
    """
    names = tuple(aliases)
    if not names:
        raise CliError(
            EXIT_USER_ERROR,
            "a hosts pin needs at least one alias",
            f"omit the argument to use the default {' '.join(DEFAULT_ALIASES)}",
        )
    for name in names:
        if not isinstance(name, str) or len(name) > _MAX_ALIAS_LEN or not _ALIAS_RE.match(name):
            raise CliError(
                EXIT_USER_ERROR,
                f"{name!r} is not a usable hostname",
                "aliases may hold only letters, digits, '.', '-' and '_' — no whitespace, no "
                "'#', no newline",
            )
    return names


# ---------------------------------------------------------------------------
# the guarded write
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """Read *path* verbatim, or raise an exit-2 :class:`CliError`.

    Goes through BYTES on purpose. :meth:`Path.read_text` opens in universal-
    newline mode and silently rewrites ``\\r\\n`` to ``\\n``, so a CRLF hosts
    file round-tripped through it comes back with every line ending changed —
    a "preserved verbatim" violation that no assertion on the *content* would
    catch. Never creates the file.
    """
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as err:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{path} is not valid UTF-8",
            "this tool refuses to rewrite a hosts file it cannot decode losslessly",
        ) from err
    except FileNotFoundError as err:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{path} does not exist",
            "this tool edits an existing hosts file and never creates one — check the path",
        ) from err
    except OSError as err:
        raise CliError(
            EXIT_ENV_ERROR,
            f"could not read {path}: {err}",
            "check the file's permissions, then re-run",
        ) from err


def _require_writable(path: Path) -> None:
    """Refuse early, and by name, when the write could not possibly succeed.

    Both the file AND its directory must be writable: the atomic rename stages a
    temp file beside the target, so a writable file inside a read-only directory
    still cannot be replaced.
    """
    if os.access(path, os.W_OK) and os.access(path.parent, os.W_OK):
        return
    raise CliError(
        EXIT_ENV_ERROR,
        f"{path} is not writable by this user",
        f"re-run the pin with elevated privileges, e.g. 'sudo reachy wireless pin' — "
        f"discovery itself needs no privilege, only the {path} write does",
    )


def _stage(path: Path, text: str) -> str:
    """Write *text* to a temp file beside *path*, carrying *path*'s mode/owner.

    Same directory, so the later :func:`os.replace` is atomic. The mode copy is
    not cosmetic: :func:`tempfile.mkstemp` creates 0600, and renaming that onto
    a 0644 ``/etc/hosts`` would break name resolution for every non-root process
    on the box — the exact blast radius this module exists to avoid.
    """
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            # Binary, matching _read_text: text mode would translate newlines
            # on the way out just as it does on the way in.
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        info = path.stat()
        os.chmod(name, stat.S_IMODE(info.st_mode))
        try:
            os.chown(name, info.st_uid, info.st_gid)
        except (OSError, AttributeError):
            # Best effort: an unprivileged owner cannot chown, and on a
            # stand-in file in a temp dir there is nothing to preserve.
            pass
    except OSError:
        _discard(name)
        raise
    return name


def _discard(name: str | None) -> None:
    if not name:
        return
    try:
        os.unlink(name)
    except OSError:  # pragma: no cover - defensive
        pass


def _restore(path: Path, original: bytes, backup: Path) -> None:
    """Put the pre-write bytes back, or raise an exit-2 :class:`CliError`.

    Deliberately a direct :meth:`Path.write_bytes` rather than another
    stage-and-rename: this is the recovery path, and routing it through the very
    mechanism that just failed would make recovery depend on the broken thing.
    The backup is already on disk, so the worst case is an operator restoring
    it by hand — which the raised error names.
    """
    try:
        path.write_bytes(original)
    except OSError as err:  # pragma: no cover - defensive
        raise CliError(
            EXIT_ENV_ERROR,
            f"{path} could not be restored after a failed write: {err}",
            f"restore it by hand from {backup}",
        ) from err


def write_document(path: Path | str, text: str) -> None:
    """Replace *path*'s entire content with *text*, recoverably.

    The one guarded write every mutation in this module goes through. See the
    module docstring for the full sequence; the short version is backup ->
    stage -> atomic rename -> **re-read and verify** -> roll back on failure.

    *text* is NOT pre-checked, on purpose. The post-write re-read is the real
    safety net — it catches a torn rename, a filesystem that lied, and a
    concurrent third-party rewrite, none of which a pre-check can see.

    *path* IS pre-checked: it crosses :func:`_safe_hosts_path` here, and every
    filesystem call below — the read, the backup, the staged temp file, the
    rename, the re-read and the rollback — uses the validated result.
    """
    p = _safe_hosts_path(path)
    original_text = _read_text(p)
    if not document_is_safe(original_text):
        raise CliError(
            EXIT_ENV_ERROR,
            f"{p} does not parse as a hosts file, or does not resolve {LOCALHOST!r}, "
            "before any change — refusing to modify it",
            "repair the file by hand first: this tool will not rewrite a hosts file it "
            "cannot verify it left working",
        )
    _require_writable(p)

    original = original_text.encode("utf-8")
    backup = backup_path(p)
    try:
        backup.write_bytes(original)
    except OSError as err:
        raise CliError(
            EXIT_ENV_ERROR,
            f"could not write the backup {backup}: {err}",
            "no change was made — free space or fix permissions on the directory, then re-run",
        ) from err

    staged: str | None = None
    try:
        staged = _stage(p, text)
        _replace(staged, p)
    except OSError as err:
        _discard(staged)
        _restore(p, original, backup)
        raise CliError(
            EXIT_ENV_ERROR,
            f"the write to {p} failed: {err}",
            f"the file was rolled back to its pre-write content; a copy is at {backup}",
        ) from err

    try:
        landed = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        _restore(p, original, backup)
        raise CliError(
            EXIT_ENV_ERROR,
            f"{p} could not be re-read after the write: {err}",
            f"the file was rolled back to its pre-write content; a copy is at {backup}",
        ) from err

    if not document_is_safe(landed):
        _restore(p, original, backup)
        raise CliError(
            EXIT_ENV_ERROR,
            f"the rewritten {p} failed verification — it no longer parses or no longer "
            f"resolves {LOCALHOST!r}, so it was rolled back",
            f"nothing changed; a copy of the pre-write content is at {backup}. This is a bug "
            "in reachy-mini-cli — please report it",
        )


# ---------------------------------------------------------------------------
# the two verbs
# ---------------------------------------------------------------------------


def pin(
    address: str,
    *,
    aliases: tuple[str, ...] | list[str] = DEFAULT_ALIASES,
    path: Path | str | None = None,
) -> bool:
    """Pin *address* to *aliases* inside the managed block. Returns "changed?".

    Idempotent in the strongest sense available: when the block already says
    exactly this, the file is not written AT ALL — no backup, no temp file, no
    rename — and ``False`` comes back. A unit that MOVED replaces the stale
    address in place rather than appending a second line.

    The document's final-newline property survives either way: a file that did
    not end in a newline still does not end in one once pinned, because the
    block it gained is written unterminated. That is not cosmetic — it is the
    record :func:`unpin` reads to undo the newline this function had to insert
    in front of the block (see the module docstring).
    """
    target = _safe_hosts_path(path)
    normalised = _validated_address(address)
    names = _validated_aliases(aliases)

    current = _read_text(target)
    before, block, after = split_document(current)
    if block is None:
        # A NEW block is appended, so the document's own terminator decides
        # both whether a separator is needed and how the block is rendered.
        terminated = not before or before.endswith("\n")
        separator = "" if terminated else "\n"
        updated = before + separator + render_block(normalised, names, terminated=terminated)
    else:
        # Re-pinning keeps whatever terminator the block already carries, so a
        # moved unit does not quietly newline-terminate the file — and so an
        # unchanged pin still compares equal below and writes nothing.
        wanted = render_block(normalised, names, terminated=block.endswith("\n"))
        if block == wanted:
            return False
        updated = before + wanted + after
    write_document(target, updated)
    return True


def _without_the_block(before: str, block: str, after: str) -> str:
    """The document with the managed block — and :func:`pin`'s insertion — removed.

    An UNTERMINATED block is one :func:`pin` appended to a document that did
    not end in a newline: it can only be the last thing in the file, and the
    newline right in front of it is the one ``pin`` inserted. Exactly one byte
    is taken back, because exactly one was ever put in.

    A block that ends in a newline says the document already ended in one, so
    every operator byte — including a run of trailing blank lines — is left
    alone. So is a hand-written unterminated block that no ``pin`` appended,
    which is why the ``before`` side is checked too rather than assumed.
    """
    if block.endswith("\n") or not before.endswith("\n"):
        return before + after
    return before[: -len("\n")] + after


def unpin(*, path: Path | str | None = None) -> bool:
    """Remove the managed block. Returns "changed?"; every other byte survives.

    Including the byte :func:`pin` had to insert in front of the block, so the
    file comes back byte-identical to its pre-pin content — a document with no
    final newline still has none afterwards. Nothing outside the file is
    consulted to know that: the backup would be the wrong authority, since
    ``unpin`` typically runs in a later process where the ``.bak`` is stale or
    already deleted.
    """
    target = _safe_hosts_path(path)
    current = _read_text(target)
    before, block, after = split_document(current)
    if block is None:
        return False
    write_document(target, _without_the_block(before, block, after))
    return True


def pinned_address(*, path: Path | str | None = None) -> str | None:
    """The address currently pinned in the managed block, or ``None``."""
    target = _safe_hosts_path(path)
    block = managed_block(_read_text(target))
    if not block:
        return None
    try:
        entries = parse_hosts(block)
    except ValueError:  # pragma: no cover - a block we wrote always parses
        return None
    return entries[0][0] if entries else None
