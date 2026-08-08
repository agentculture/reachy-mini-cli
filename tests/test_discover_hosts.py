"""The recoverable ``/etc/hosts`` managed block (:mod:`reachy.discover.hosts`).

**Every test in this file runs against a ``tmp_path`` stand-in.** The real
``/etc/hosts`` is never opened for writing, never backed up and never renamed —
:func:`_the_real_hosts_file_is_never_touched` is an autouse guard that snapshots
the real file's bytes and its directory listing around EVERY test in this module
and fails loudly if either moved. That guard is the point of the file as much as
the assertions are: the module under test can break name resolution box-wide,
and this box's entire hosts file is two lines.

``BOX_HOSTS`` reproduces this box's real hosts file **byte for byte**, trailing
whitespace and trailing blank line included (recorded with ``od -c /etc/hosts``,
56 bytes, root-owned 644). Those bytes are deliberately awkward — a naive
implementation that round-trips through ``str.splitlines()`` + ``"\\n".join()``
silently eats the trailing newline, and one that ``.strip()``s lines eats the
two trailing spaces. Both are "every byte outside the block preserved verbatim"
violations, and both are caught here.

No test in this module resolves a name. The ``.local`` half of the pinned line
is asserted as *text in the file*, never as something that answers a lookup:
``nsswitch.conf`` may route ``.local`` exclusively to mDNS, bypassing
``/etc/hosts`` entirely, so correctness must never depend on it resolving
through files. :func:`test_no_test_in_this_module_depends_on_a_name_resolving`
pins that structurally with an AST walk over this very file.
"""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path

import pytest

from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from reachy.discover import hosts

#: This box's real ``/etc/hosts``, byte for byte (see the module docstring).
BOX_HOSTS = "127.0.0.1       localhost\n127.0.0.1       spark-f8a9  \n\n"

#: A hosts file with CRLF line endings and no trailing newline — the other
#: shape a line-rejoining bug destroys silently.
CRLF_HOSTS = "127.0.0.1\tlocalhost\r\n10.0.0.5\tbuild-box\r\n192.168.1.9\tnas"


@pytest.fixture(autouse=True)
def _the_real_hosts_file_is_never_touched():
    """Fail loudly if any test in this module reaches the real ``/etc/hosts``.

    Snapshots the file's bytes and the set of ``hosts*`` names in ``/etc``
    (so a stray ``/etc/hosts.reachy-mini-cli.bak`` is caught too) before and
    after each test. A default-path call that somehow got through the
    writability refusal would show up here rather than on the operator's box.
    """
    real = Path("/etc/hosts")
    before = real.read_bytes() if real.exists() else None
    siblings = sorted(p.name for p in Path("/etc").glob("hosts*"))
    yield
    after = real.read_bytes() if real.exists() else None
    assert after == before, "a test modified the real /etc/hosts"
    assert sorted(p.name for p in Path("/etc").glob("hosts*")) == siblings


@pytest.fixture
def hosts_file(tmp_path: Path) -> Path:
    """A stand-in hosts file holding this box's exact two-line content."""
    path = tmp_path / "hosts"
    path.write_text(BOX_HOSTS, encoding="utf-8")
    return path


def _outside_the_block(text: str) -> str:
    """Everything in *text* except the managed block, as raw text."""
    before, _block, after = hosts.split_document(text)
    return before + after


# ---------------------------------------------------------------------------
# acceptance 1 — only the delimited block moves
# ---------------------------------------------------------------------------


def test_the_fixture_reproduces_this_boxs_hosts_file_byte_count():
    assert len(BOX_HOSTS.encode("utf-8")) == 56


def test_pin_touches_only_the_delimited_block(hosts_file: Path):
    original = hosts_file.read_bytes()

    assert hosts.pin("192.168.1.162", path=hosts_file) is True

    landed = hosts_file.read_text(encoding="utf-8")
    assert hosts.BEGIN_MARKER in landed
    assert hosts.END_MARKER in landed
    # Every byte outside the block is preserved verbatim: the block was
    # appended, so the file is exactly the original bytes plus the block.
    assert landed.encode("utf-8").startswith(original)
    assert _outside_the_block(landed) == BOX_HOSTS


def test_the_trailing_whitespace_and_blank_line_survive(hosts_file: Path):
    hosts.pin("192.168.1.162", path=hosts_file)
    landed = hosts_file.read_text(encoding="utf-8")
    # The two trailing spaces after spark-f8a9 and the trailing blank line are
    # exactly what a splitlines()/strip() round trip silently eats.
    assert "spark-f8a9  \n\n" in landed


def test_crlf_endings_and_a_missing_trailing_newline_survive(tmp_path: Path):
    path = tmp_path / "hosts"
    path.write_text(CRLF_HOSTS, encoding="utf-8")

    hosts.pin("10.0.0.9", path=path)

    # read_bytes, not read_text: universal-newline mode would rewrite \r\n to
    # \n in the assertion itself and hide exactly the bug this test is for.
    landed = path.read_bytes().decode("utf-8")
    assert landed.startswith(CRLF_HOSTS)
    assert "\r\n" in landed
    assert _outside_the_block(landed).startswith(CRLF_HOSTS)


def test_lines_after_the_block_are_preserved_verbatim(tmp_path: Path):
    path = tmp_path / "hosts"
    tail = "10.0.0.1  gateway\n# a trailing operator comment\n"
    path.write_text(BOX_HOSTS + hosts.render_block("1.2.3.4", ("reachy-mini",)) + tail, "utf-8")

    hosts.pin("192.168.1.162", path=path)

    landed = path.read_text(encoding="utf-8")
    assert landed.startswith(BOX_HOSTS)
    assert landed.endswith(tail)
    assert _outside_the_block(landed) == BOX_HOSTS + tail


# ---------------------------------------------------------------------------
# acceptance 5 — the pinned line carries BOTH names
# ---------------------------------------------------------------------------


def test_the_pinned_line_carries_both_names_on_one_line(hosts_file: Path):
    hosts.pin("192.168.1.162", path=hosts_file)

    landed = hosts_file.read_text(encoding="utf-8")
    assert "192.168.1.162 reachy-mini reachy-mini.local\n" in landed
    entries = hosts.parse_hosts(hosts.managed_block(landed) or "")
    assert entries == (("192.168.1.162", ("reachy-mini", "reachy-mini.local")),)


def test_the_primary_alias_is_the_plain_name(hosts_file: Path):
    # The plain 'reachy-mini' is PRIMARY; '.local' is an additional convenience
    # that nsswitch may route to mDNS instead of files.
    assert hosts.DEFAULT_ALIASES[0] == "reachy-mini"
    assert hosts.DEFAULT_ALIASES == ("reachy-mini", "reachy-mini.local")


def test_no_test_in_this_module_depends_on_a_name_resolving():
    """Structural: this file never calls a resolver, so '.local' is only ever text."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    banned = {
        "gethostbyname",
        "gethostbyname_ex",
        "getaddrinfo",
        "gethostbyaddr",
        "create_connection",
    }
    called: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in banned:
                called.add(name)
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not called, f"this module must not resolve names: {sorted(called)}"
    assert "socket" not in imported


# ---------------------------------------------------------------------------
# acceptance 3 — idempotency and re-pinning a moved unit
# ---------------------------------------------------------------------------


def test_pinning_twice_is_idempotent_and_the_second_run_writes_nothing(hosts_file: Path):
    assert hosts.pin("192.168.1.162", path=hosts_file) is True
    after_first = hosts_file.read_bytes()
    backup = hosts.backup_path(hosts_file)
    backup.unlink()

    assert hosts.pin("192.168.1.162", path=hosts_file) is False

    assert hosts_file.read_bytes() == after_first
    assert after_first.count(b"reachy-mini reachy-mini.local") == 1
    # Nothing changed, so nothing was modified — and therefore nothing was
    # backed up either.
    assert not backup.exists()


def test_repinning_a_moved_unit_replaces_the_stale_address(hosts_file: Path):
    hosts.pin("192.168.1.162", path=hosts_file)

    assert hosts.pin("192.168.1.77", path=hosts_file) is True

    landed = hosts_file.read_text(encoding="utf-8")
    assert "192.168.1.77 reachy-mini reachy-mini.local" in landed
    assert "192.168.1.162" not in landed
    assert landed.count(hosts.BEGIN_MARKER) == 1
    assert landed.count("reachy-mini reachy-mini.local") == 1
    assert _outside_the_block(landed) == BOX_HOSTS


def test_pinned_address_reads_the_block_back(hosts_file: Path):
    assert hosts.pinned_address(path=hosts_file) is None
    hosts.pin("192.168.1.162", path=hosts_file)
    assert hosts.pinned_address(path=hosts_file) == "192.168.1.162"
    hosts.pin("192.168.1.77", path=hosts_file)
    assert hosts.pinned_address(path=hosts_file) == "192.168.1.77"


# ---------------------------------------------------------------------------
# acceptance 4 — unpin
# ---------------------------------------------------------------------------


def test_unpin_removes_the_block_and_restores_the_file_byte_for_byte(hosts_file: Path):
    original = hosts_file.read_bytes()
    hosts.pin("192.168.1.162", path=hosts_file)

    assert hosts.unpin(path=hosts_file) is True

    assert hosts_file.read_bytes() == original


def test_unpin_leaves_every_other_line_untouched(tmp_path: Path):
    path = tmp_path / "hosts"
    tail = "10.0.0.1  gateway\n"
    path.write_text(BOX_HOSTS + tail, encoding="utf-8")
    hosts.pin("192.168.1.162", path=path)

    hosts.unpin(path=path)

    assert path.read_text(encoding="utf-8") == BOX_HOSTS + tail


def test_unpin_on_an_unpinned_file_changes_nothing(hosts_file: Path):
    original = hosts_file.read_bytes()

    assert hosts.unpin(path=hosts_file) is False

    assert hosts_file.read_bytes() == original
    assert not hosts.backup_path(hosts_file).exists()


# ---------------------------------------------------------------------------
# acceptance 6 — the backup, and the post-write verification
# ---------------------------------------------------------------------------


def test_a_backup_holding_the_pre_write_bytes_is_written_before_the_modification(
    hosts_file: Path,
):
    original = hosts_file.read_bytes()
    backup = hosts.backup_path(hosts_file)
    assert backup.name == "hosts.reachy-mini-cli.bak"
    assert not backup.exists()

    hosts.pin("192.168.1.162", path=hosts_file)

    assert backup.read_bytes() == original


def test_the_backup_tracks_the_immediately_preceding_state(hosts_file: Path):
    hosts.pin("192.168.1.162", path=hosts_file)
    after_first = hosts_file.read_bytes()

    hosts.pin("192.168.1.77", path=hosts_file)

    # The backup is the PRE-WRITE state of this write, which is what makes
    # "byte-identical to the pre-write backup" a meaningful rollback promise.
    assert hosts.backup_path(hosts_file).read_bytes() == after_first


def test_post_write_verification_rolls_back_a_document_that_lost_localhost(
    hosts_file: Path,
):
    original = hosts_file.read_bytes()
    doomed = "127.0.0.1       spark-f8a9\n"  # localhost gone
    assert not hosts.document_is_safe(doomed)

    with pytest.raises(CliError) as excinfo:
        hosts.write_document(hosts_file, doomed)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "localhost" in excinfo.value.message
    assert hosts_file.read_bytes() == original
    assert hosts.backup_path(hosts_file).read_bytes() == original


def test_post_write_verification_rolls_back_an_unparseable_document(hosts_file: Path):
    original = hosts_file.read_bytes()
    doomed = BOX_HOSTS + "not-an-ip-address  reachy-mini\n"

    with pytest.raises(CliError) as excinfo:
        hosts.write_document(hosts_file, doomed)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert hosts_file.read_bytes() == original
    assert hosts.backup_path(hosts_file).read_bytes() == original


def test_a_hosts_file_that_already_lacks_localhost_is_refused_untouched(tmp_path: Path):
    path = tmp_path / "hosts"
    broken = "127.0.0.1  spark-f8a9\n"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(CliError) as excinfo:
        hosts.pin("192.168.1.162", path=path)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert path.read_text(encoding="utf-8") == broken
    # Refused BEFORE anything was modified: no backup, no temp file.
    assert not hosts.backup_path(path).exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hosts"]


# ---------------------------------------------------------------------------
# acceptance 2 — a write failing midway
# ---------------------------------------------------------------------------


def test_a_rename_that_fails_before_touching_the_destination_leaves_it_identical(
    hosts_file: Path, monkeypatch: pytest.MonkeyPatch
):
    original = hosts_file.read_bytes()

    def _boom(src: str, dst) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(hosts, "_replace", _boom)

    with pytest.raises(CliError) as excinfo:
        hosts.pin("192.168.1.162", path=hosts_file)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert hosts_file.read_bytes() == original
    assert hosts.backup_path(hosts_file).read_bytes() == original


def test_a_write_failing_midway_leaves_the_file_byte_identical(
    hosts_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The destination is genuinely damaged, then the write fails — and rolls back.

    This is the dangerous half of "fails midway": not a rename that never
    started, but one that left a TRUNCATED destination behind. The fake below
    copies only the first 12 bytes of the staged temp file onto the
    destination — dropping the localhost line — and then raises, exactly as a
    torn write would.
    """
    original = hosts_file.read_bytes()

    def _torn(src: str, dst) -> None:
        staged = Path(src).read_bytes()
        Path(dst).write_bytes(staged[:12])
        os.unlink(src)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(hosts, "_replace", _torn)

    with pytest.raises(CliError) as excinfo:
        hosts.pin("192.168.1.162", path=hosts_file)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert hosts_file.read_bytes() == original
    assert hosts.backup_path(hosts_file).read_bytes() == original


def test_no_temp_file_is_left_behind_when_a_write_fails(
    hosts_file: Path, monkeypatch: pytest.MonkeyPatch
):
    def _boom(src: str, dst) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(hosts, "_replace", _boom)

    with pytest.raises(CliError):
        hosts.pin("192.168.1.162", path=hosts_file)

    leftovers = sorted(p.name for p in hosts_file.parent.iterdir())
    assert leftovers == ["hosts", "hosts.reachy-mini-cli.bak"]


def test_a_failed_write_names_the_backup_in_its_remediation(
    hosts_file: Path, monkeypatch: pytest.MonkeyPatch
):
    def _boom(src: str, dst) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(hosts, "_replace", _boom)

    with pytest.raises(CliError) as excinfo:
        hosts.pin("192.168.1.162", path=hosts_file)

    assert str(hosts.backup_path(hosts_file)) in excinfo.value.remediation


def test_no_temp_file_survives_a_successful_write(hosts_file: Path):
    hosts.pin("192.168.1.162", path=hosts_file)

    leftovers = sorted(p.name for p in hosts_file.parent.iterdir())
    assert leftovers == ["hosts", "hosts.reachy-mini-cli.bak"]


def test_the_file_mode_survives_the_write(hosts_file: Path):
    """A 0600 temp file renamed onto a 0644 /etc/hosts breaks every non-root reader."""
    os.chmod(hosts_file, 0o644)

    hosts.pin("192.168.1.162", path=hosts_file)

    assert stat.S_IMODE(hosts_file.stat().st_mode) == 0o644


# ---------------------------------------------------------------------------
# refusals: unwritable, missing, malformed input
# ---------------------------------------------------------------------------


def test_a_non_writable_hosts_file_is_a_clean_exit_2_naming_sudo(hosts_file: Path):
    original = hosts_file.read_bytes()
    os.chmod(hosts_file, 0o444)
    try:
        with pytest.raises(CliError) as excinfo:
            hosts.pin("192.168.1.162", path=hosts_file)
    finally:
        os.chmod(hosts_file, 0o644)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "sudo" in excinfo.value.remediation
    assert hosts_file.read_bytes() == original
    assert not hosts.backup_path(hosts_file).exists()


def test_a_non_writable_directory_is_a_clean_exit_2(hosts_file: Path):
    os.chmod(hosts_file.parent, 0o555)
    try:
        with pytest.raises(CliError) as excinfo:
            hosts.pin("192.168.1.162", path=hosts_file)
    finally:
        os.chmod(hosts_file.parent, 0o755)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "sudo" in excinfo.value.remediation


def test_a_missing_hosts_file_is_exit_2_and_is_never_created(tmp_path: Path):
    path = tmp_path / "hosts"

    with pytest.raises(CliError) as excinfo:
        hosts.pin("192.168.1.162", path=path)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert not path.exists()


def test_a_begin_marker_without_an_end_marker_is_refused_untouched(tmp_path: Path):
    path = tmp_path / "hosts"
    mangled = BOX_HOSTS + hosts.BEGIN_MARKER + "\n1.2.3.4 reachy-mini\n"
    path.write_text(mangled, encoding="utf-8")

    with pytest.raises(CliError) as excinfo:
        hosts.pin("192.168.1.162", path=path)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert hosts.END_MARKER in excinfo.value.message
    assert path.read_text(encoding="utf-8") == mangled


@pytest.mark.parametrize("bad", ["reachy-mini", "", "999.1.1.1", "192.168.1.162/24", "  "])
def test_a_non_ip_pin_target_is_a_user_error(hosts_file: Path, bad: str):
    original = hosts_file.read_bytes()

    with pytest.raises(CliError) as excinfo:
        hosts.pin(bad, path=hosts_file)

    assert excinfo.value.code == EXIT_USER_ERROR
    assert hosts_file.read_bytes() == original


@pytest.mark.parametrize(
    "alias",
    [
        "reachy mini",
        "reachy-mini\n# END reachy-mini-cli\n1.2.3.4 evil",
        "# comment",
        "",
        "reachy-mini\t",
    ],
)
def test_an_alias_that_could_inject_a_line_is_refused(hosts_file: Path, alias: str):
    original = hosts_file.read_bytes()

    with pytest.raises(CliError) as excinfo:
        hosts.pin("192.168.1.162", aliases=(alias,), path=hosts_file)

    assert excinfo.value.code == EXIT_USER_ERROR
    assert hosts_file.read_bytes() == original


def test_an_empty_alias_tuple_is_refused(hosts_file: Path):
    with pytest.raises(CliError) as excinfo:
        hosts.pin("192.168.1.162", aliases=(), path=hosts_file)
    assert excinfo.value.code == EXIT_USER_ERROR


def test_an_ipv6_address_is_accepted_and_normalised(hosts_file: Path):
    hosts.pin("2A0D:6FC2:0:0:0:0:0:756B", path=hosts_file)

    landed = hosts_file.read_text(encoding="utf-8")
    assert "2a0d:6fc2::756b reachy-mini reachy-mini.local" in landed


# ---------------------------------------------------------------------------
# the verifier itself
# ---------------------------------------------------------------------------


def test_parse_hosts_reads_addresses_names_and_ignores_comments():
    text = "# a comment\n\n127.0.0.1  localhost  loopback   # trailing\n10.0.0.1\tgw\n"
    assert hosts.parse_hosts(text) == (
        ("127.0.0.1", ("localhost", "loopback")),
        ("10.0.0.1", ("gw",)),
    )


@pytest.mark.parametrize("text", ["127.0.0.1\n", "definitely-not-an-ip  host\n"])
def test_parse_hosts_rejects_a_malformed_line(text: str):
    with pytest.raises(ValueError):
        hosts.parse_hosts(text)


def test_parse_hosts_tolerates_an_ipv6_zone_id():
    assert hosts.parse_hosts("fe80::1%eth0  linklocal\n") == (("fe80::1%eth0", ("linklocal",)),)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("127.0.0.1 localhost\n", True),
        ("::1 localhost ip6-localhost\n", True),
        ("127.0.0.1 LOCALHOST\n", True),
        ("127.0.0.1 spark-f8a9\n", False),
        ("", False),
        # A non-loopback address claiming the name is not a working localhost.
        ("10.0.0.5 localhost\n", False),
    ],
)
def test_resolves_localhost_requires_a_loopback_mapping(text: str, expected: bool):
    assert hosts.resolves_localhost(text) is expected


def test_document_is_safe_requires_both_parseability_and_localhost():
    assert hosts.document_is_safe(BOX_HOSTS) is True
    assert hosts.document_is_safe("127.0.0.1 localhost\nnope\n") is False
    assert hosts.document_is_safe("10.0.0.1 gw\n") is False


def test_the_default_path_is_etc_hosts_but_is_only_ever_a_default():
    assert hosts.DEFAULT_HOSTS_PATH == Path("/etc/hosts")


# ---------------------------------------------------------------------------
# the path boundary — `--hosts-path` is operator input reaching the filesystem
# ---------------------------------------------------------------------------


def test_the_validator_returns_an_absolute_symlink_free_path(tmp_path: Path, monkeypatch):
    """A relative path is resolved ONCE, at the boundary, before anything opens it."""
    (tmp_path / "hosts").write_text(BOX_HOSTS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    resolved = hosts._safe_hosts_path("hosts")

    assert resolved.is_absolute()
    assert resolved == (tmp_path / "hosts").resolve()


def test_a_relative_path_still_pins_and_backs_up_beside_the_resolved_file(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "hosts").write_text(BOX_HOSTS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert hosts.pin("192.168.1.162", path="hosts") is True

    assert "192.168.1.162 reachy-mini" in (tmp_path / "hosts").read_text(encoding="utf-8")
    # The backup landed beside the RESOLVED file, and backup_path agrees with
    # what write_document computed internally.
    assert hosts.backup_path("hosts") == (tmp_path / "hosts.reachy-mini-cli.bak").resolve()
    assert hosts.backup_path("hosts").read_bytes() == BOX_HOSTS.encode("utf-8")


def test_the_validator_follows_a_symlink_and_the_symlink_survives_the_write(tmp_path: Path):
    """The symlink is resolved at the boundary, so os.replace lands on the target.

    A naive implementation renames a temp file onto the LINK, silently
    replacing it with a regular file and detaching every other reader.
    """
    real = tmp_path / "real-hosts"
    real.write_text(BOX_HOSTS, encoding="utf-8")
    link = tmp_path / "hosts"
    link.symlink_to(real)

    assert hosts._safe_hosts_path(link) == real.resolve()
    assert hosts.pin("192.168.1.162", path=link) is True

    assert link.is_symlink()
    assert "192.168.1.162 reachy-mini" in real.read_text(encoding="utf-8")
    assert hosts.backup_path(link) == (tmp_path / "real-hosts.reachy-mini-cli.bak").resolve()


def test_a_traversal_segment_is_collapsed_before_anything_is_opened(tmp_path: Path):
    (tmp_path / "hosts").write_text(BOX_HOSTS, encoding="utf-8")
    (tmp_path / "sub").mkdir()

    resolved = hosts._safe_hosts_path(tmp_path / "sub" / ".." / "hosts")

    assert ".." not in resolved.parts
    assert resolved == (tmp_path / "hosts").resolve()


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/hosts\x00/../../evil",
        "/etc/hosts\n1.2.3.4 evil",
        "/etc/\thosts",
    ],
)
def test_a_control_character_in_the_path_is_refused(bad: str):
    with pytest.raises(CliError) as excinfo:
        hosts._safe_hosts_path(bad)
    assert excinfo.value.code == EXIT_USER_ERROR


@pytest.mark.parametrize(
    "component",
    ["ho$(id)sts", "hosts;rm -rf /", "ho*sts", "hosts'", 'hosts"', "ho|sts", "ho`id`sts"],
)
def test_a_hostile_path_component_is_refused_never_escaped(tmp_path: Path, component: str):
    target = tmp_path / component
    target.write_text(BOX_HOSTS, encoding="utf-8")

    with pytest.raises(CliError) as excinfo:
        hosts._safe_hosts_path(target)

    assert excinfo.value.code == EXIT_USER_ERROR
    assert "--hosts-path" in excinfo.value.remediation
    # Refused, not repaired: the file is still exactly as it was.
    assert target.read_text(encoding="utf-8") == BOX_HOSTS


@pytest.mark.parametrize("bad", ["", "   "])
def test_an_empty_path_is_refused(bad: str):
    with pytest.raises(CliError) as excinfo:
        hosts._safe_hosts_path(bad)
    assert excinfo.value.code == EXIT_USER_ERROR


def test_a_non_path_object_is_refused():
    with pytest.raises(CliError) as excinfo:
        hosts._safe_hosts_path(7)  # type: ignore[arg-type]
    assert excinfo.value.code == EXIT_USER_ERROR


def test_a_directory_is_refused_as_a_hosts_path(tmp_path: Path):
    with pytest.raises(CliError) as excinfo:
        hosts._safe_hosts_path(tmp_path)
    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "regular file" in excinfo.value.message


def test_a_fifo_is_refused_as_a_hosts_path(tmp_path: Path):
    fifo = tmp_path / "hosts"
    os.mkfifo(fifo)

    with pytest.raises(CliError) as excinfo:
        hosts._safe_hosts_path(fifo)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "regular file" in excinfo.value.message


def test_a_path_whose_parent_does_not_exist_is_refused(tmp_path: Path):
    with pytest.raises(CliError) as excinfo:
        hosts.pin("192.168.1.162", path=tmp_path / "nope" / "hosts")

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert not (tmp_path / "nope").exists()


def test_a_dangling_symlink_is_refused(tmp_path: Path):
    link = tmp_path / "hosts"
    link.symlink_to(tmp_path / "gone")

    with pytest.raises(CliError) as excinfo:
        hosts._safe_hosts_path(link)

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "does not exist" in excinfo.value.message
    assert not (tmp_path / "gone").exists()


def test_none_means_the_module_default_and_probes_nothing():
    # The default is a module constant, not operator input — it is returned
    # as-is, which is also why this test cannot touch the real /etc/hosts.
    assert hosts._safe_hosts_path(None) == hosts.DEFAULT_HOSTS_PATH


def test_a_file_that_vanishes_after_validation_is_still_a_clean_exit_2(tmp_path: Path):
    """The read keeps its own refusal: validation is a boundary, not a lock."""
    with pytest.raises(CliError) as excinfo:
        hosts._read_text(tmp_path / "hosts")

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "does not exist" in excinfo.value.message


def test_every_entry_point_taking_a_raw_path_funnels_through_the_validator():
    """Structural: one boundary, not a check per call site.

    Any module-level function whose ``path`` parameter accepts a raw ``str``
    is an entry point for operator-controlled data, and must call
    :func:`hosts._safe_hosts_path`. Adding a sixth entry point without routing
    it through the boundary fails here rather than in a SonarCloud report.
    """
    tree = ast.parse(Path(hosts.__file__).read_text(encoding="utf-8"))
    entry_points: dict[str, bool] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        args = [*node.args.args, *node.args.kwonlyargs]
        takes_raw_path = any(
            arg.arg == "path"
            and arg.annotation is not None
            and "str" in ast.unparse(arg.annotation)
            for arg in args
        )
        if not takes_raw_path or node.name == "_safe_hosts_path":
            continue
        entry_points[node.name] = any(
            isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "_safe_hosts_path"
            for inner in ast.walk(node)
        )

    assert set(entry_points) == {"backup_path", "write_document", "pin", "unpin", "pinned_address"}
    assert all(
        entry_points.values()
    ), f"not funnelled: {sorted(n for n, ok in entry_points.items() if not ok)}"


def test_no_entry_point_re_materialises_the_raw_path_argument():
    """``Path(path)`` is gone from the module: only the validator's output flows on.

    The validator parses its own local copy (``Path(raw).expanduser()``); an
    entry point that rebuilt ``Path(path)`` would hand a filesystem call the
    caller's string again, which is precisely the taint S2083 reports.
    """
    tree = ast.parse(Path(hosts.__file__).read_text(encoding="utf-8"))
    offenders = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "Path"
        and any(getattr(arg, "id", "") == "path" for arg in node.args)
    ]
    assert offenders == []
