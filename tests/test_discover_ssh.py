"""Tests for reachy.discover.ssh — the login argv builder and the key install.

Acceptance criteria covered (one section each):

1. The login account resolves as ``--user`` then ``REACHY_WIRELESS_SSH_USER``
   then the documented default, asserted for all three precedence levels.
2. The ssh invocation ALWAYS carries ``-o HostKeyAlias=<alias>`` alongside the
   resolved IP, so host-key identity holds with no ``/etc/hosts`` pin and no
   privilege.
3. ``authorize`` refuses without an explicit confirmation naming the target
   ``hardware_id``, and a declined confirmation invokes ``ssh-copy-id``
   exactly ZERO times.
4. ``authorize`` is never reachable from find or ssh — asserted STRUCTURALLY
   (an AST call-graph walk over the module, in the style of
   ``tests/test_zero_llm_boundary.py``), not merely behaviourally — and it
   reports plainly when the key was already installed.
5. The documented default account is ``pollen`` and the default HostKeyAlias is
   ``reachy-mini``; the alias is NEVER derived from the daemon's ``robot_name``
   field, which reports the underscore form ``reachy_mini``.
6. ``authorize`` tolerates an interactive password prompt (the unit ships the
   factory default) and never logs, echoes or stores the password.

Nothing here spawns a real ``ssh`` or ``ssh-copy-id`` process and nothing here
reaches the network: every test injects the exec / run / which seams, and the
module-scoped ``_never_spawn_a_real_process`` autouse fixture turns the real
:func:`subprocess.call`, :func:`subprocess.run`, :func:`os.execvp` and
:func:`shutil.which` into loud failures for the duration of this file.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess  # nosec B404 — patched to raise; never used to spawn here
from pathlib import Path

import pytest

from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from reachy.discover.probe import UnitRecord
from reachy.discover.ssh import (
    DEFAULT_HOST_KEY_ALIAS,
    DEFAULT_SSH_USER,
    REFUSAL_CONFIRMATION_DECLINED,
    REFUSAL_MISSING_HARDWARE_ID,
    REFUSAL_NO_CONFIRMATION,
    REFUSALS,
    SSH_BINARY,
    SSH_COPY_ID_BINARY,
    SSH_USER_ENV,
    AuthorizeTarget,
    authorize,
    confirmation_prompt,
    open_shell,
    resolve_alias,
    resolve_user,
    ssh_argv,
    ssh_copy_id_argv,
)

_MODULE_PATH = Path(__file__).resolve().parent.parent / "reachy" / "discover" / "ssh.py"

#: The live unit's real address + identity, as recorded from the box (the same
#: fixture ``tests/test_discover_probe.py`` uses).
LIVE_ADDRESS = "192.168.1.162"
LIVE_HARDWARE_ID = "a89063c05ae79779"

#: The daemon's own ``robot_name`` — the UNDERSCORE form. It is here so the
#: tests can prove the hyphenated alias is never munged out of it.
DAEMON_ROBOT_NAME = "reachy_mini"


@pytest.fixture(autouse=True)
def _never_spawn_a_real_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make an accidental real ``ssh`` spawn (or PATH lookup) fail loudly."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("a test tried to spawn a real process or touch the network")

    monkeypatch.setattr(subprocess, "call", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(os, "execvp", _boom)
    monkeypatch.setattr(shutil, "which", lambda name: None)


class _ExecRecorder:
    """Stand-in for the ``os.execvp`` seam — records instead of replacing us."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, file: str, argv: list[str]) -> str:
        self.calls.append((file, tuple(argv)))
        return "exec-would-have-happened"

    @property
    def argv(self) -> tuple[str, ...]:
        assert len(self.calls) == 1, f"expected one exec, got {self.calls}"
        return self.calls[0][1]


class _RunRecorder:
    """Stand-in for the subprocess seam ``authorize`` drives.

    Records the argv of every invocation AND every keyword argument, so a test
    can assert the child's stdio is never redirected (criterion 6).
    """

    def __init__(self, codes: list[int] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict[str, object]] = []
        self.codes = list(codes or [])

    def __call__(self, argv: list[str], **kwargs: object) -> int:
        self.calls.append(tuple(argv))
        self.kwargs.append(dict(kwargs))
        return self.codes.pop(0) if self.codes else 0

    @property
    def copy_id_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c and c[0] == SSH_COPY_ID_BINARY]


def _which_all(name: str) -> str:
    """A ``which`` seam that finds every binary this module ever looks up."""
    return f"/usr/bin/{name}"


def _which_missing_copy_id(name: str) -> str | None:
    return None if name == SSH_COPY_ID_BINARY else f"/usr/bin/{name}"


def _target(**overrides: object) -> AuthorizeTarget:
    fields: dict[str, object] = {
        "address": LIVE_ADDRESS,
        "hardware_id": LIVE_HARDWARE_ID,
        "alias": DEFAULT_HOST_KEY_ALIAS,
    }
    fields.update(overrides)
    return AuthorizeTarget(**fields)  # type: ignore[arg-type]


def _module_tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Criterion 1 — the login account's three precedence levels
# ---------------------------------------------------------------------------


def test_the_explicit_user_flag_wins_over_the_env_var_and_the_default() -> None:
    env = {SSH_USER_ENV: "from-env"}
    assert resolve_user("from-flag", env=env) == "from-flag"
    assert ssh_argv(LIVE_ADDRESS, user="from-flag", env=env)[-1] == f"from-flag@{LIVE_ADDRESS}"


def test_the_env_var_wins_over_the_default_when_no_flag_is_given() -> None:
    env = {SSH_USER_ENV: "from-env"}
    assert resolve_user(None, env=env) == "from-env"
    assert ssh_argv(LIVE_ADDRESS, env=env)[-1] == f"from-env@{LIVE_ADDRESS}"


def test_the_documented_default_applies_when_neither_flag_nor_env_is_set() -> None:
    assert resolve_user(None, env={}) == DEFAULT_SSH_USER
    assert ssh_argv(LIVE_ADDRESS, env={})[-1] == f"{DEFAULT_SSH_USER}@{LIVE_ADDRESS}"


def test_a_blank_flag_or_env_value_falls_through_rather_than_logging_in_as_nobody() -> None:
    assert resolve_user("   ", env={SSH_USER_ENV: "from-env"}) == "from-env"
    assert resolve_user(None, env={SSH_USER_ENV: "  "}) == DEFAULT_SSH_USER


def test_the_env_seam_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SSH_USER_ENV, "process-env-user")
    assert resolve_user() == "process-env-user"
    monkeypatch.delenv(SSH_USER_ENV, raising=False)
    assert resolve_user() == DEFAULT_SSH_USER


def test_a_user_that_could_be_read_as_an_ssh_option_is_refused() -> None:
    with pytest.raises(CliError) as excinfo:
        ssh_argv(LIVE_ADDRESS, user="-oProxyCommand=touch /tmp/pwned", env={})  # nosec B108
    assert excinfo.value.code == EXIT_USER_ERROR
    assert excinfo.value.remediation


# ---------------------------------------------------------------------------
# Criterion 2 — HostKeyAlias always rides along, with no pin and no privilege
# ---------------------------------------------------------------------------


def _alias_option(argv: tuple[str, ...] | list[str]) -> str:
    argv = list(argv)
    hits = [
        argv[i + 1]
        for i, tok in enumerate(argv[:-1])
        if tok == "-o" and argv[i + 1].startswith("HostKeyAlias=")
    ]
    assert len(hits) == 1, f"expected exactly one HostKeyAlias option in {argv}"
    return hits[0]


def test_ssh_argv_always_carries_the_host_key_alias_beside_the_resolved_ip() -> None:
    argv = ssh_argv(LIVE_ADDRESS, env={})
    assert argv[0] == SSH_BINARY
    assert _alias_option(argv) == f"HostKeyAlias={DEFAULT_HOST_KEY_ALIAS}"
    # The connection target is the resolved IP, NOT the alias: the alias exists
    # only to key known_hosts, so nothing here needs /etc/hosts to resolve.
    assert argv[-1] == f"{DEFAULT_SSH_USER}@{LIVE_ADDRESS}"


def test_open_shell_passes_the_alias_through_to_the_exec_seam() -> None:
    recorder = _ExecRecorder()
    open_shell(LIVE_ADDRESS, env={}, exec_fn=recorder, which=_which_all)
    assert _alias_option(recorder.argv) == f"HostKeyAlias={DEFAULT_HOST_KEY_ALIAS}"
    assert recorder.calls[0][0] == SSH_BINARY
    assert recorder.argv[-1] == f"{DEFAULT_SSH_USER}@{LIVE_ADDRESS}"


def test_the_key_install_invocation_carries_the_same_alias() -> None:
    argv = ssh_copy_id_argv(LIVE_ADDRESS, env={})
    assert argv[0] == SSH_COPY_ID_BINARY
    assert _alias_option(argv) == f"HostKeyAlias={DEFAULT_HOST_KEY_ALIAS}"
    assert argv[-1] == f"{DEFAULT_SSH_USER}@{LIVE_ADDRESS}"


def test_every_argv_authorize_runs_carries_the_alias() -> None:
    runner = _RunRecorder(codes=[1, 0])
    authorize(
        _target(),
        confirm=lambda prompt: True,
        env={},
        run=runner,
        which=_which_all,
    )
    assert runner.calls, "authorize ran nothing"
    for argv in runner.calls:
        assert _alias_option(argv) == f"HostKeyAlias={DEFAULT_HOST_KEY_ALIAS}"


def test_a_per_unit_alias_override_replaces_the_default_everywhere() -> None:
    argv = ssh_argv(LIVE_ADDRESS, alias="lab-reachy", env={})
    assert _alias_option(argv) == "HostKeyAlias=lab-reachy"
    assert _alias_option(ssh_copy_id_argv(LIVE_ADDRESS, alias="lab-reachy", env={})) == (
        "HostKeyAlias=lab-reachy"
    )


def _imported_modules(tree: ast.Module) -> set[str]:
    return {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }


def test_stable_host_key_identity_never_depends_on_the_hosts_pin() -> None:
    """The module must not reach the /etc/hosts machinery at all (spec h25).

    Docstrings may (and do) EXPLAIN the relationship; what must not exist is a
    code path — an import of ``reachy.discover.hosts``, or a hosts-file path
    literal — that makes the alias depend on a privileged pin.
    """
    tree = _module_tree()
    imported = _imported_modules(tree)
    assert not any("hosts" in name for name in imported), imported
    docstrings = _docstring_node_ids(tree)
    code_strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    assert not any("/etc/hosts" in s for s in code_strings), code_strings


def test_a_blank_or_option_shaped_alias_is_refused_rather_than_smuggled_into_argv() -> None:
    for bad in ("", "   ", "-oProxyCommand=x", "two words"):
        with pytest.raises(CliError) as excinfo:
            ssh_argv(LIVE_ADDRESS, alias=bad, env={})
        assert excinfo.value.code == EXIT_USER_ERROR


def test_a_blank_or_option_shaped_address_is_refused() -> None:
    for bad in ("", "   ", "-oProxyCommand=x"):
        with pytest.raises(CliError) as excinfo:
            ssh_argv(bad, env={})
        assert excinfo.value.code == EXIT_USER_ERROR


# ---------------------------------------------------------------------------
# Criterion 3 — authorize refuses without an explicit, target-naming confirm
# ---------------------------------------------------------------------------


def test_authorize_with_no_confirmation_callback_runs_ssh_copy_id_zero_times() -> None:
    runner = _RunRecorder()
    result = authorize(_target(), confirm=None, env={}, run=runner, which=_which_all)
    assert result.ok is False
    assert result.refusal == REFUSAL_NO_CONFIRMATION
    assert runner.calls == []
    assert runner.copy_id_calls == []


def test_a_declined_confirmation_runs_ssh_copy_id_exactly_zero_times() -> None:
    runner = _RunRecorder()
    seen: list[str] = []

    def _decline(prompt: str) -> bool:
        seen.append(prompt)
        return False

    result = authorize(_target(), confirm=_decline, env={}, run=runner, which=_which_all)
    assert result.ok is False
    assert result.refusal == REFUSAL_CONFIRMATION_DECLINED
    assert len(runner.copy_id_calls) == 0
    assert runner.calls == [], "nothing at all may run before an affirmative confirmation"
    assert len(seen) == 1


def test_only_a_strict_true_counts_as_an_affirmative() -> None:
    """Fail-closed: a truthy non-``True`` is not an explicit affirmative."""
    for answer in (None, 0, "", "yes", "y", 1, [1]):
        runner = _RunRecorder()
        result = authorize(
            _target(),
            confirm=lambda prompt, a=answer: a,  # type: ignore[misc,return-value]
            env={},
            run=runner,
            which=_which_all,
        )
        assert result.ok is False, answer
        assert result.refusal == REFUSAL_CONFIRMATION_DECLINED, answer
        assert runner.copy_id_calls == [], answer


def test_the_confirmation_prompt_names_the_alias_the_ip_and_the_hardware_id() -> None:
    prompt = confirmation_prompt(_target())
    assert LIVE_HARDWARE_ID in prompt
    assert LIVE_ADDRESS in prompt
    assert DEFAULT_HOST_KEY_ALIAS in prompt
    # It must say WHY the confirmation exists: the target was scanned, not typed.
    assert "scan" in prompt.lower()


def test_the_prompt_handed_to_the_callback_is_the_one_that_names_the_target() -> None:
    runner = _RunRecorder(codes=[1, 0])
    seen: list[str] = []
    authorize(
        _target(),
        confirm=lambda prompt: bool(seen.append(prompt)) or True,
        env={},
        run=runner,
        which=_which_all,
    )
    assert len(seen) == 1
    assert LIVE_HARDWARE_ID in seen[0]
    assert LIVE_ADDRESS in seen[0]


def test_a_target_with_no_hardware_id_is_refused_before_anything_runs() -> None:
    runner = _RunRecorder()
    confirmed: list[str] = []
    result = authorize(
        _target(hardware_id="  "),
        confirm=lambda prompt: bool(confirmed.append(prompt)) or True,
        env={},
        run=runner,
        which=_which_all,
    )
    assert result.ok is False
    assert result.refusal == REFUSAL_MISSING_HARDWARE_ID
    assert runner.calls == []
    assert confirmed == [], "an unidentifiable target is refused before the operator is asked"


def test_every_refusal_this_module_can_return_is_a_named_member_of_the_vocabulary() -> None:
    assert REFUSAL_NO_CONFIRMATION in REFUSALS
    assert REFUSAL_CONFIRMATION_DECLINED in REFUSALS
    assert REFUSAL_MISSING_HARDWARE_ID in REFUSALS
    for name in REFUSALS:
        assert name and name == name.lower() and " " not in name


def test_a_missing_ssh_copy_id_binary_is_a_clean_exit_two_and_never_asks_first() -> None:
    runner = _RunRecorder()
    confirmed: list[str] = []
    with pytest.raises(CliError) as excinfo:
        authorize(
            _target(),
            confirm=lambda prompt: bool(confirmed.append(prompt)) or True,
            env={},
            run=runner,
            which=_which_missing_copy_id,
        )
    assert excinfo.value.code == EXIT_ENV_ERROR
    assert SSH_COPY_ID_BINARY in excinfo.value.message
    assert excinfo.value.remediation
    assert runner.calls == []
    assert confirmed == []


def test_a_missing_ssh_binary_is_a_clean_exit_two_from_the_shell_path_too() -> None:
    recorder = _ExecRecorder()
    with pytest.raises(CliError) as excinfo:
        open_shell(LIVE_ADDRESS, env={}, exec_fn=recorder, which=lambda name: None)
    assert excinfo.value.code == EXIT_ENV_ERROR
    assert recorder.calls == []


def test_the_default_binary_lookup_is_shutil_which() -> None:
    """With ``shutil.which`` patched to find nothing, the default seam reports it."""
    with pytest.raises(CliError) as excinfo:
        open_shell(LIVE_ADDRESS, env={}, exec_fn=_ExecRecorder())
    assert excinfo.value.code == EXIT_ENV_ERROR


# ---------------------------------------------------------------------------
# Criterion 4 — authorize is structurally unreachable from the shell path,
#               and reports plainly when the key was already installed
# ---------------------------------------------------------------------------


def _toplevel_functions(tree: ast.Module) -> dict[str, ast.AST]:
    """Every module-level ``def``/``class`` body, by the name callers use."""
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{sub.name}"] = sub
    return out


def _referenced_names(node: ast.AST) -> set[str]:
    """Every bare name and attribute name mentioned inside *node*.

    Deliberately broader than "called": a function that merely *mentions*
    ``authorize`` (stores it, passes it on, wraps it) is an edge too.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _reachable(starts: set[str], graph: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(starts)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for edge in graph.get(current, set()):
            if edge in graph and edge not in seen:
                stack.append(edge)
            # a method reference like `.from_record` reaches every same-named body
            for qualified in graph:
                if qualified.endswith(f".{edge}") and qualified not in seen:
                    stack.append(qualified)
    return seen


def _call_graph() -> dict[str, set[str]]:
    tree = _module_tree()
    return {name: _referenced_names(node) for name, node in _toplevel_functions(tree).items()}


def test_the_call_graph_walk_is_not_vacuous() -> None:
    """Guard the guard: the same walk DOES find authorize's own edges."""
    graph = _call_graph()
    assert "authorize" in graph and "open_shell" in graph and "ssh_argv" in graph
    from_authorize = _reachable({"authorize"}, graph)
    assert "ssh_argv" in from_authorize, from_authorize
    assert "ssh_copy_id_argv" in from_authorize, from_authorize


def test_authorize_is_structurally_unreachable_from_the_shell_opening_path() -> None:
    graph = _call_graph()
    reached = _reachable({"open_shell", "ssh_argv", "resolve_user", "resolve_alias"}, graph)
    for forbidden in ("authorize", "ssh_copy_id_argv", "confirmation_prompt"):
        assert forbidden not in reached, (
            f"{forbidden} is reachable from the shell-opening path: {sorted(reached)}. "
            "Key install must never be a side effect of finding or logging in."
        )


def test_the_shell_opening_path_never_mentions_the_key_install_binary() -> None:
    tree = _module_tree()
    bodies = _toplevel_functions(tree)
    graph = _call_graph()
    reached = _reachable({"open_shell", "ssh_argv"}, graph)
    for name in sorted(reached):
        node = bodies.get(name)
        if node is None:
            continue
        source = ast.unparse(node)
        assert "ssh-copy-id" not in source, name
        assert "SSH_COPY_ID_BINARY" not in source, name


def test_opening_a_shell_invokes_no_subprocess_runner_at_all() -> None:
    """Behavioural mirror of the structural pin above."""
    recorder = _ExecRecorder()
    runner = _RunRecorder()
    open_shell(LIVE_ADDRESS, env={}, exec_fn=recorder, which=_which_all)
    assert runner.calls == []
    assert len(recorder.calls) == 1
    assert recorder.calls[0][0] == SSH_BINARY


def test_no_other_discover_module_can_reach_the_key_install_path() -> None:
    """Structural: only this module holds the install path.

    Sibling modules may name it in PROSE (``__init__.py``'s package docstring
    describes it), but no sibling may carry executable code that reaches it:
    no import of :mod:`reachy.discover.ssh`, no reference to the install
    functions, and no ``ssh-copy-id`` string outside a docstring.
    """
    discover = _MODULE_PATH.parent
    for path in sorted(discover.rglob("*.py")):
        if path == _MODULE_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert "reachy.discover.ssh" not in _imported_modules(tree), path
        names = _identifiers(tree)
        for forbidden in ("authorize", "ssh_copy_id_argv", "AuthorizeTarget"):
            assert forbidden not in names, f"{path}: {forbidden}"
        docstrings = _docstring_node_ids(tree)
        code_strings = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        assert not any("ssh-copy-id" in s for s in code_strings), path


def test_a_key_that_is_already_installed_is_reported_plainly_and_pushes_nothing() -> None:
    runner = _RunRecorder(codes=[0])  # the passwordless pre-flight succeeds
    result = authorize(
        _target(),
        confirm=lambda prompt: True,
        env={},
        run=runner,
        which=_which_all,
    )
    assert result.ok is True
    assert result.already_installed is True
    assert result.refusal is None
    assert runner.copy_id_calls == [], "nothing to install — ssh-copy-id must not run"
    assert result.detail and "already" in result.detail.lower()


def test_a_fresh_unit_gets_exactly_one_ssh_copy_id_invocation() -> None:
    runner = _RunRecorder(codes=[255, 0])  # pre-flight refused, then install succeeds
    result = authorize(
        _target(),
        confirm=lambda prompt: True,
        env={},
        run=runner,
        which=_which_all,
    )
    assert result.ok is True
    assert result.already_installed is False
    assert len(runner.copy_id_calls) == 1
    assert result.argv == runner.copy_id_calls[0]


def test_a_failing_ssh_copy_id_is_a_named_refusal_carrying_the_exit_code() -> None:
    runner = _RunRecorder(codes=[255, 1])
    result = authorize(
        _target(),
        confirm=lambda prompt: True,
        env={},
        run=runner,
        which=_which_all,
    )
    assert result.ok is False
    assert result.refusal in REFUSALS
    assert "1" in result.detail


def test_the_install_appends_and_never_truncates_authorized_keys() -> None:
    """ssh-copy-id semantics: no flag that could replace the remote key file."""
    argv = ssh_copy_id_argv(LIVE_ADDRESS, env={})
    for destructive in ("-f", "--force", "-n", "--dry-run"):
        assert destructive not in argv, argv


# ---------------------------------------------------------------------------
# Criterion 5 — the documented defaults, and the alias that is never derived
# ---------------------------------------------------------------------------


def test_the_documented_default_account_is_pollen() -> None:
    assert DEFAULT_SSH_USER == "pollen"


def test_the_default_host_key_alias_is_the_hyphenated_reachy_mini() -> None:
    assert DEFAULT_HOST_KEY_ALIAS == "reachy-mini"
    assert "_" not in DEFAULT_HOST_KEY_ALIAS
    assert resolve_alias(None) == DEFAULT_HOST_KEY_ALIAS


def test_the_alias_is_never_derived_from_the_daemons_robot_name() -> None:
    """The daemon says ``reachy_mini``; the alias must stay ``reachy-mini``.

    Deriving one from the other by string munging would regenerate exactly the
    name the co-resident Lite already claims in mDNS (spec c34 / c27).
    """
    record = UnitRecord(
        hardware_id=LIVE_HARDWARE_ID,
        robot_name=DAEMON_ROBOT_NAME,
        model="Reachy Mini Wireless",
        wireless=True,
        version="1.9.0",
        wlan_ip=LIVE_ADDRESS,
        address=LIVE_ADDRESS,
    )
    target = AuthorizeTarget.from_record(record)
    assert target.alias == DEFAULT_HOST_KEY_ALIAS
    assert target.address == LIVE_ADDRESS
    assert target.hardware_id == LIVE_HARDWARE_ID
    argv = ssh_argv(record.address, env={})
    assert _alias_option(argv) == f"HostKeyAlias={DEFAULT_HOST_KEY_ALIAS}"
    assert DAEMON_ROBOT_NAME not in " ".join(argv)


def _docstring_node_ids(tree: ast.Module) -> set[int]:
    """Ids of every Constant node that is a docstring (prose, not behaviour)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def test_the_module_never_reads_robot_name_at_all() -> None:
    """Structural: no code path can munge ``robot_name`` into an alias.

    Docstrings are excluded — the module SHOULD explain the two spellings in
    prose; what it must never do is carry the underscore form, or an
    underscore-to-hyphen rewrite, in executable code.
    """
    tree = _module_tree()
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "robot_name" not in attributes
    docstrings = _docstring_node_ids(tree)
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }
    assert DAEMON_ROBOT_NAME not in strings, (
        "the underscore spelling must never appear in executable code — "
        "the alias is written out as a literal, never derived"
    )
    assert DEFAULT_HOST_KEY_ALIAS in strings, "the alias literal must be spelled out here"
    # ...and no code path performs the underscore->hyphen rewrite itself.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "replace":
                assert "_" not in ast.unparse(node), ast.unparse(node)


def test_an_explicit_alias_still_wins_over_the_documented_default() -> None:
    assert resolve_alias("lab-reachy") == "lab-reachy"
    assert (
        AuthorizeTarget.from_record(
            UnitRecord(
                hardware_id=LIVE_HARDWARE_ID,
                robot_name=DAEMON_ROBOT_NAME,
                model="Reachy Mini Wireless",
                wireless=True,
                version="1.9.0",
                wlan_ip=LIVE_ADDRESS,
                address=LIVE_ADDRESS,
            ),
            alias="lab-reachy",
        ).alias
        == "lab-reachy"
    )


# ---------------------------------------------------------------------------
# Criterion 6 — the password prompt is tolerated, never handled
# ---------------------------------------------------------------------------


def _identifiers(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
    return names


def test_no_identifier_in_the_module_holds_or_names_a_password() -> None:
    for name in _identifiers(_module_tree()):
        lowered = name.lower()
        for forbidden in ("password", "passwd", "passphrase", "secret", "credential"):
            assert forbidden not in lowered, name


def test_the_module_imports_no_prompt_reader_and_no_logger() -> None:
    tree = _module_tree()
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    for forbidden in ("getpass", "logging", "pty", "pexpect"):
        assert forbidden not in imported, imported
    names = _identifiers(tree)
    for forbidden in ("input", "getpass", "print"):
        assert forbidden not in names, forbidden


def test_no_call_in_the_module_redirects_a_childs_stdio() -> None:
    """The child inherits the terminal, so ssh's own prompt never passes us."""
    tree = _module_tree()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            assert kw.arg not in (
                "stdin",
                "stdout",
                "stderr",
                "capture_output",
                "input",
            ), ast.unparse(node)
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert "PIPE" not in source
    assert "check_output" not in source


def test_authorize_hands_the_runner_an_argv_and_nothing_else() -> None:
    runner = _RunRecorder(codes=[255, 0])
    authorize(
        _target(),
        confirm=lambda prompt: True,
        env={},
        run=runner,
        which=_which_all,
    )
    assert runner.kwargs == [{}, {}], runner.kwargs


def test_the_result_carries_no_field_that_could_hold_a_secret() -> None:
    runner = _RunRecorder(codes=[255, 0])
    result = authorize(
        _target(),
        confirm=lambda prompt: True,
        env={},
        run=runner,
        which=_which_all,
    )
    rendered = repr(result)
    assert "root" not in rendered, "the factory password must never reach a repr"
    assert set(result.__dataclass_fields__) == {
        "ok",
        "already_installed",
        "target",
        "argv",
        "refusal",
        "detail",
    }


def test_the_preflight_uses_batchmode_so_it_never_consumes_the_prompt() -> None:
    """The 'is it already installed?' probe must not sit on a password prompt."""
    runner = _RunRecorder(codes=[255, 0])
    authorize(
        _target(),
        confirm=lambda prompt: True,
        env={},
        run=runner,
        which=_which_all,
    )
    preflight = runner.calls[0]
    assert preflight[0] == SSH_BINARY
    assert "BatchMode=yes" in preflight
    # ...and the install itself does NOT force BatchMode, or the factory
    # password could never be typed on first contact.
    install = runner.copy_id_calls[0]
    assert "BatchMode=yes" not in install
