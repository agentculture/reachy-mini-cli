"""Tests for the ``reachy wireless`` noun — the operator-facing discovery surface.

Every test drives the CLI through :func:`reachy.cli.main` with INJECTED seams, so
the suite **never touches a real network, a real ``/etc/hosts``, or a real ssh**:

* ``wireless._sweep`` / ``wireless._probe`` are replaced with recording fakes, so
  no socket is opened (the autouse ``_no_live_lan_sweep`` guard in
  ``tests/conftest.py`` already forces interface enumeration to ``()``; these
  fakes are what let a test assert on a *found* unit at all);
* ``wireless._EXEC_SSH`` / ``wireless._RUN_SSH`` / ``wireless._WHICH`` are the
  three optional process seams — ``None`` in production, so
  ``reachy/discover/ssh.py`` owns the terminal (and therefore the unit's
  factory-default password prompt) end to end — and every test here sets them;
* ``--hosts-path`` points at a temp file, never ``/etc/hosts``;
* ``REACHY_STATE_DIR`` points the registry at a temp dir.

The output contract (results→stdout, errors+diagnostics→stderr, never mixed; the
two-line ``error:``/``hint:`` text shape; ``--json`` on every verb) is asserted
in BOTH modes via ``capsys``.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess  # nosec B404 — fixed argv, sys.executable, never shell=True
import sys
from pathlib import Path

import pytest

from reachy.cli import _build_parser, main
from reachy.cli._commands import wireless
from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR
from reachy.discover.hosts import BEGIN_MARKER, END_MARKER, LOCAL_ALIAS, PRIMARY_ALIAS
from reachy.discover.probe import DEFAULT_PORT, UnitRecord
from reachy.discover.registry import RegistryRecord, UnitRegistry
from reachy.discover.sweep import SweepResult
from reachy.explain import known_paths

#: Every verb the noun must expose (plan t9 / spec decision c29).
VERBS = ("find", "list", "ssh", "authorize", "pin", "unpin", "forget", "overview")

#: The real unit recorded in the spec, used verbatim so the fixtures describe
#: the box this feature was specified against.
WIRELESS = UnitRecord(
    hardware_id="a89063c05ae79779",
    robot_name="reachy_mini",
    model="Reachy Mini Wireless",
    wireless=True,
    version="1.9.0",
    wlan_ip="192.168.1.162",
    address="192.168.1.162",
)

#: The co-resident Lite — same ``robot_name``, different unit. Ambiguity is the
#: NORMAL case on this box, not a corner case.
LITE = UnitRecord(
    hardware_id="0f1e2d3c4b5a6978",
    robot_name="reachy_mini",
    model="Reachy Mini Lite",
    wireless=False,
    version="1.9.0",
    wlan_ip=None,
    address="192.168.1.50",
)

#: A unit reachable only over IPv6 — the documented out-of-scope case that must
#: stay usable by EXPLICIT address (spec: "the IPv4 boundary is visible").
V6_ADDRESS = "2a0d:6fc2:4:1::756b"


class FakeSweep:
    """Records every call and serves a canned :class:`SweepResult`."""

    def __init__(self, units: tuple[UnitRecord, ...] = ()) -> None:
        self.units = tuple(units)
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> SweepResult:
        self.calls.append(dict(kwargs))
        return SweepResult(
            units=self.units,
            hosts_total=254,
            hosts_probed=254,
            deadline_reached=False,
            elapsed_s=0.42,
        )


class FakeProbe:
    """Serves canned records by host string; records every host asked."""

    def __init__(self, answers: dict[str, UnitRecord] | None = None) -> None:
        self.answers = dict(answers or {})
        self.hosts: list[str] = []

    def __call__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 1.0):
        self.hosts.append(host)
        return self.answers.get(host)


class FakeRunner:
    """Records argv vectors and serves canned exit codes (ssh / ssh-copy-id)."""

    def __init__(self, codes: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.codes = list(codes or [])

    def __call__(self, argv) -> int:
        self.calls.append(list(argv))
        return self.codes.pop(0) if self.codes else 0


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """State dir, env overrides and the MAC-enrichment seam, all neutralised."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("REACHY_WIRELESS_UNIT", raising=False)
    monkeypatch.delenv("REACHY_WIRELESS_SSH_USER", raising=False)
    # `ip neigh` is a real subprocess; the enrichment is opportunistic by design,
    # so the suite simply never observes a MAC.
    monkeypatch.setattr(wireless, "_mac_for", lambda address: None, raising=True)
    monkeypatch.setattr(wireless, "_WHICH", lambda name: f"/usr/bin/{name}", raising=True)


@pytest.fixture
def sweeper(monkeypatch):
    def _install(*units: UnitRecord) -> FakeSweep:
        fake = FakeSweep(units)
        monkeypatch.setattr(wireless, "_sweep", fake, raising=True)
        return fake

    return _install


@pytest.fixture
def prober(monkeypatch):
    def _install(**answers: UnitRecord) -> FakeProbe:
        fake = FakeProbe(dict(answers))
        monkeypatch.setattr(wireless, "_probe", fake, raising=True)
        return fake

    return _install


def _hosts_file(tmp_path: Path) -> Path:
    """A stand-in for this box's real two-line ``/etc/hosts``."""
    path = tmp_path / "hosts"
    path.write_text("127.0.0.1 localhost\n127.0.0.1 spark-f8a9\n", encoding="utf-8")
    return path


def _registry_with(*records: RegistryRecord) -> UnitRegistry:
    registry = UnitRegistry()
    for record in records:
        registry.upsert(record)
    return registry


def _record(unit: UnitRecord, *, alias: str | None = None) -> RegistryRecord:
    return RegistryRecord(
        hardware_id=unit.hardware_id,
        mac=None,
        last_ip=unit.address,
        name=unit.robot_name,
        model=unit.model,
        wireless=unit.wireless,
        last_seen="2026-08-08T00:00:00+00:00",
        alias=alias,
    )


# --------------------------------------------------------------------------- #
# 1. Registration, --json on every verb, and the error contract in both modes  #
# --------------------------------------------------------------------------- #


def _wireless_parser():
    for action in _build_parser()._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "wireless" in choices:
            return choices["wireless"]
    raise AssertionError("the 'wireless' noun is not registered in _build_parser()")


def _verb_parsers() -> dict[str, object]:
    for action in _wireless_parser()._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "find" in choices:
            return dict(choices)
    raise AssertionError("the 'wireless' noun exposes no sub-verbs")


def test_every_verb_registers() -> None:
    assert set(_verb_parsers()) == set(VERBS)


@pytest.mark.parametrize("verb", VERBS)
def test_every_verb_accepts_json(verb: str) -> None:
    options = {opt for action in _verb_parsers()[verb]._actions for opt in action.option_strings}
    assert "--json" in options, f"'wireless {verb}' does not accept --json"


@pytest.mark.parametrize("verb", VERBS)
def test_verb_errors_render_two_line_text_contract(
    verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["wireless", verb, "--definitely-not-a-flag"])
    assert exc.value.code == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == "", "an error must never write to stdout"
    lines = err.strip().splitlines()
    assert lines[0].startswith("error: ")
    assert any(line.startswith("hint: ") for line in lines)


@pytest.mark.parametrize("verb", VERBS)
def test_verb_errors_render_json_contract(verb: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["wireless", verb, "--json", "--definitely-not-a-flag"])
    assert exc.value.code == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    payload = json.loads(err)
    assert set(payload) == {"code", "message", "remediation"}
    assert payload["code"] == EXIT_USER_ERROR


# --------------------------------------------------------------------------- #
# 2. overview — the IPv4-and-default-port boundary is VISIBLE                  #
# --------------------------------------------------------------------------- #


def test_bare_noun_prints_the_overview(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["wireless"]) == 0
    out = capsys.readouterr().out
    assert "wireless" in out
    for verb in VERBS:
        assert verb in out


def test_overview_states_the_ipv4_and_default_port_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["wireless", "overview"]) == 0
    out = capsys.readouterr().out
    assert "IPv4" in out
    assert str(DEFAULT_PORT) in out
    # ... and names the escape hatch that keeps a v6-only unit usable.
    assert "--address" in out
    assert "IPv6" in out


def test_overview_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["wireless", "overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "reachy-mini-cli wireless"
    assert payload["sections"]


def test_overview_warns_about_the_factory_default_password(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Discovery makes the unit easier to find; the password is the first move."""
    assert main(["wireless", "overview"]) == 0
    out = capsys.readouterr().out.lower()
    assert "password" in out


# --------------------------------------------------------------------------- #
# 3. find — the default is wireless-only, and the flag reveals everything      #
# --------------------------------------------------------------------------- #


def test_find_defaults_to_wireless_units(sweeper, capsys: pytest.CaptureFixture[str]) -> None:
    sweeper(WIRELESS, LITE)
    assert main(["wireless", "find", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [u["hardware_id"] for u in payload["units"]] == [WIRELESS.hardware_id]
    assert payload["wireless_only"] is True
    # The count of everything the sweep saw is reported, so the filter is
    # visible rather than silent.
    assert payload["found_total"] == 2


def test_find_all_reveals_every_reachy_daemon_the_sweep_saw(
    sweeper, capsys: pytest.CaptureFixture[str]
) -> None:
    sweeper(WIRELESS, LITE)
    assert main(["wireless", "find", "--all", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {u["hardware_id"] for u in payload["units"]} == {
        WIRELESS.hardware_id,
        LITE.hardware_id,
    }
    assert payload["wireless_only"] is False
    assert any(u["wireless"] is False for u in payload["units"])


def test_find_emits_a_base_url_an_agent_can_pass_straight_to_base_url(
    sweeper, capsys: pytest.CaptureFixture[str]
) -> None:
    sweeper(WIRELESS)
    assert main(["wireless", "find", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["units"][0]["base_url"] == "http://192.168.1.162:8000"
    assert payload["units"][0]["port"] == DEFAULT_PORT


def test_find_base_url_follows_an_explicit_port(
    sweeper, capsys: pytest.CaptureFixture[str]
) -> None:
    sweeper(WIRELESS)
    assert main(["wireless", "find", "--port", "8123", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["units"][0]["base_url"] == "http://192.168.1.162:8123"


def test_find_remembers_what_it_found(sweeper, capsys: pytest.CaptureFixture[str]) -> None:
    sweeper(WIRELESS)
    assert main(["wireless", "find", "--json"]) == 0
    capsys.readouterr()
    remembered = {r.hardware_id: r for r in UnitRegistry().all()}
    assert WIRELESS.hardware_id in remembered
    assert remembered[WIRELESS.hardware_id].last_ip == "192.168.1.162"


def test_find_does_not_remember_a_lite_it_filtered_out(
    sweeper, capsys: pytest.CaptureFixture[str]
) -> None:
    sweeper(WIRELESS, LITE)
    assert main(["wireless", "find", "--json"]) == 0
    capsys.readouterr()
    assert {r.hardware_id for r in UnitRegistry().all()} == {WIRELESS.hardware_id}


def test_find_preserves_an_existing_alias_when_it_re_remembers(
    sweeper, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS, alias="bench"))
    moved = dataclasses.replace(WIRELESS, address="192.168.1.200")
    sweeper(moved)
    assert main(["wireless", "find", "--json"]) == 0
    capsys.readouterr()
    record = UnitRegistry().get(WIRELESS.hardware_id)
    assert record is not None
    assert record.alias == "bench"
    assert record.last_ip == "192.168.1.200"


def test_find_with_only_a_lite_names_the_flag_that_reveals_it(
    sweeper, capsys: pytest.CaptureFixture[str]
) -> None:
    sweeper(LITE)
    assert main(["wireless", "find"]) == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error: ")
    assert "--all" in err


def test_find_with_nothing_answering_exits_with_a_hint(
    sweeper, capsys: pytest.CaptureFixture[str]
) -> None:
    sweeper()
    assert main(["wireless", "find"]) == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error: ")
    assert "hint: " in err


def test_find_reports_the_sweep_bounds_it_ran_under(
    sweeper, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = sweeper(WIRELESS)
    assert main(["wireless", "find", "--timeout", "0.25", "--deadline", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hosts_total"] == 254
    assert payload["deadline_reached"] is False
    assert fake.calls[0]["timeout"] == 0.25
    assert fake.calls[0]["deadline_s"] == 3.0


# --------------------------------------------------------------------------- #
# 4. find --address — the v6 / non-default-port escape hatch                   #
# --------------------------------------------------------------------------- #


def test_find_with_an_explicit_address_never_sweeps(
    sweeper, prober, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_sweep = sweeper(WIRELESS)
    prober(**{"192.168.1.162": WIRELESS})
    assert main(["wireless", "find", "--address", "192.168.1.162", "--json"]) == 0
    capsys.readouterr()
    assert fake_sweep.calls == []


def test_an_ipv6_only_unit_stays_usable_by_explicit_address(
    prober, capsys: pytest.CaptureFixture[str]
) -> None:
    """v6 sweeping is out of scope; a v6 unit is still reachable by address."""
    v6_unit = dataclasses.replace(WIRELESS, address=f"[{V6_ADDRESS}]")
    fake = prober(**{f"[{V6_ADDRESS}]": v6_unit})
    assert main(["wireless", "find", "--address", V6_ADDRESS, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    # The probe is asked for the BRACKETED host (a bare v6 literal would make an
    # unparseable URL), while the reported address stays the bare literal.
    assert fake.hosts == [f"[{V6_ADDRESS}]"]
    assert payload["units"][0]["address"] == V6_ADDRESS
    assert payload["units"][0]["base_url"] == f"http://[{V6_ADDRESS}]:8000"


def test_find_with_a_dark_explicit_address_names_it(
    prober, capsys: pytest.CaptureFixture[str]
) -> None:
    prober()
    assert main(["wireless", "find", "--address", "192.168.1.9"]) == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert "192.168.1.9" in err
    assert "hint: " in err


def test_find_rejects_an_address_that_is_not_an_ip(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["wireless", "find", "--address", "reachy-mini"]) == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error: ")
    assert "hint: " in err


# --------------------------------------------------------------------------- #
# 5. list — the registry, with no network at all                               #
# --------------------------------------------------------------------------- #


def test_list_on_an_empty_registry(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["wireless", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["units"] == []
    assert payload["count"] == 0


def test_list_reports_every_remembered_unit(capsys: pytest.CaptureFixture[str]) -> None:
    _registry_with(_record(WIRELESS, alias="bench"), _record(LITE))
    assert main(["wireless", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    by_id = {u["hardware_id"]: u for u in payload["units"]}
    assert by_id[WIRELESS.hardware_id]["alias"] == "bench"
    assert by_id[WIRELESS.hardware_id]["base_url"] == "http://192.168.1.162:8000"


def test_list_never_touches_the_network(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _boom(**_kwargs):
        raise AssertionError("list must not sweep")

    monkeypatch.setattr(wireless, "_sweep", _boom, raising=True)
    monkeypatch.setattr(wireless, "_probe", _boom, raising=True)
    _registry_with(_record(WIRELESS))
    assert main(["wireless", "list", "--json"]) == 0
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# 6. ssh — resolve, then hand the terminal to ssh                              #
# --------------------------------------------------------------------------- #


def test_ssh_execs_with_the_stable_host_key_alias(
    monkeypatch, prober, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    seen: list[list[str]] = []
    monkeypatch.setattr(wireless, "_EXEC_SSH", lambda f, argv: seen.append(list(argv)))
    assert main(["wireless", "ssh"]) == 0
    capsys.readouterr()
    assert seen, "ssh was never invoked"
    argv = seen[0]
    assert argv[0] == "ssh"
    assert "HostKeyAlias=reachy-mini" in argv
    assert f"pollen@{WIRELESS.address}" in argv


def test_ssh_user_override_reaches_the_argv(monkeypatch, prober) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    seen: list[list[str]] = []
    monkeypatch.setattr(wireless, "_EXEC_SSH", lambda f, argv: seen.append(list(argv)))
    assert main(["wireless", "ssh", "--user", "ubuntu"]) == 0
    assert f"ubuntu@{WIRELESS.address}" in seen[0]


def test_ssh_dry_run_prints_the_argv_and_execs_nothing(
    monkeypatch, prober, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    seen: list[list[str]] = []
    monkeypatch.setattr(wireless, "_EXEC_SSH", lambda f, argv: seen.append(list(argv)))
    assert main(["wireless", "ssh", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert seen == []
    assert payload["executed"] is False
    assert payload["argv"][0] == "ssh"
    assert payload["unit"]["hardware_id"] == WIRELESS.hardware_id


def test_ssh_refuses_an_ambiguous_registry_and_names_both_candidates(
    monkeypatch, prober, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS), _record(LITE))
    prober(**{"192.168.1.162": WIRELESS, "192.168.1.50": LITE})
    seen: list[list[str]] = []
    monkeypatch.setattr(wireless, "_EXEC_SSH", lambda f, argv: seen.append(list(argv)))
    assert main(["wireless", "ssh"]) == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert seen == [], "an ambiguous resolve must never open a shell"
    assert WIRELESS.hardware_id in err
    assert LITE.hardware_id in err
    assert "--unit" in err


def test_ssh_unit_selector_picks_among_known_units(monkeypatch, prober) -> None:
    _registry_with(_record(WIRELESS), _record(LITE))
    prober(**{"192.168.1.162": WIRELESS, "192.168.1.50": LITE})
    seen: list[list[str]] = []
    monkeypatch.setattr(wireless, "_EXEC_SSH", lambda f, argv: seen.append(list(argv)))
    assert main(["wireless", "ssh", "--unit", WIRELESS.hardware_id]) == 0
    assert f"pollen@{WIRELESS.address}" in seen[0]


def test_ssh_unit_selector_also_reads_the_env(monkeypatch, prober) -> None:
    _registry_with(_record(WIRELESS), _record(LITE))
    prober(**{"192.168.1.162": WIRELESS, "192.168.1.50": LITE})
    monkeypatch.setenv("REACHY_WIRELESS_UNIT", LITE.hardware_id)
    seen: list[list[str]] = []
    monkeypatch.setattr(wireless, "_EXEC_SSH", lambda f, argv: seen.append(list(argv)))
    assert main(["wireless", "ssh"]) == 0
    assert f"pollen@{LITE.address}" in seen[0]


# --------------------------------------------------------------------------- #
# 7. authorize — explicit confirmation, and never a side effect                #
# --------------------------------------------------------------------------- #


def test_authorize_declined_runs_ssh_copy_id_zero_times(
    monkeypatch, prober, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    runner = FakeRunner()
    monkeypatch.setattr(wireless, "_RUN_SSH", runner)
    monkeypatch.setattr(wireless, "_read_answer", lambda prompt: "n")
    assert main(["wireless", "authorize"]) == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert runner.calls == []
    assert err.startswith("error: ")
    assert "hint: " in err


def test_authorize_prompt_names_the_hardware_id(monkeypatch, prober) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    prompts: list[str] = []
    monkeypatch.setattr(wireless, "_RUN_SSH", FakeRunner())
    monkeypatch.setattr(wireless, "_read_answer", lambda prompt: (prompts.append(prompt), "n")[1])
    main(["wireless", "authorize"])
    assert prompts and WIRELESS.hardware_id in prompts[0]


def test_authorize_yes_pushes_the_key(
    monkeypatch, prober, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    # 1 => the BatchMode pre-flight fails (no key yet), then ssh-copy-id succeeds.
    runner = FakeRunner([1, 0])
    monkeypatch.setattr(wireless, "_RUN_SSH", runner)
    assert main(["wireless", "authorize", "--yes", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["already_installed"] is False
    assert runner.calls[-1][0] == "ssh-copy-id"
    assert f"pollen@{WIRELESS.address}" in runner.calls[-1]


def test_authorize_reports_an_already_installed_key_plainly(
    monkeypatch, prober, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    runner = FakeRunner([0])  # the pre-flight succeeds
    monkeypatch.setattr(wireless, "_RUN_SSH", runner)
    assert main(["wireless", "authorize", "--yes", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["already_installed"] is True
    assert all(call[0] != "ssh-copy-id" for call in runner.calls)


def test_authorize_reports_a_failed_ssh_copy_id_as_an_error(
    monkeypatch, prober, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    monkeypatch.setattr(wireless, "_RUN_SSH", FakeRunner([1, 1]))
    assert main(["wireless", "authorize", "--yes"]) == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error: ")
    assert "hint: " in err


def test_authorize_is_never_reached_from_find_or_ssh() -> None:
    """Structural, not behavioural: key install is never a side effect."""
    source = Path(wireless.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    guarded = {"cmd_wireless_find", "cmd_wireless_ssh"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in guarded:
            names = {
                n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", "")
                for n in ast.walk(node)
                if isinstance(n, (ast.Name, ast.Attribute))
            }
            assert not any("authorize" in name for name in names), (
                f"{node.name} references authorize — key install must never be a "
                "side effect of finding or logging in"
            )


# --------------------------------------------------------------------------- #
# 8. pin / unpin — the managed block, never the real /etc/hosts                #
# --------------------------------------------------------------------------- #


def test_pin_writes_both_aliases_into_a_managed_block(
    prober, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    hosts = _hosts_file(tmp_path)
    assert main(["wireless", "pin", "--hosts-path", str(hosts), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    body = hosts.read_text(encoding="utf-8")
    assert payload["changed"] is True
    assert BEGIN_MARKER in body and END_MARKER in body
    assert f"192.168.1.162 {PRIMARY_ALIAS} {LOCAL_ALIAS}" in body
    assert "127.0.0.1 localhost" in body


def test_pin_is_idempotent(prober, tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    hosts = _hosts_file(tmp_path)
    assert main(["wireless", "pin", "--hosts-path", str(hosts), "--json"]) == 0
    capsys.readouterr()
    assert main(["wireless", "pin", "--hosts-path", str(hosts), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is False
    assert hosts.read_text(encoding="utf-8").count(BEGIN_MARKER) == 1


def test_pin_with_an_explicit_address_needs_no_registry(
    monkeypatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(**_kwargs):
        raise AssertionError("an explicit --address must not resolve")

    monkeypatch.setattr(wireless, "_sweep", _boom, raising=True)
    hosts = _hosts_file(tmp_path)
    argv = ["wireless", "pin", "--address", "192.168.1.7", "--hosts-path", str(hosts), "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    assert "192.168.1.7 reachy-mini" in hosts.read_text(encoding="utf-8")


def test_pin_on_an_unwritable_hosts_file_is_a_clean_exit_2(
    prober, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    missing = tmp_path / "nope" / "hosts"
    assert main(["wireless", "pin", "--hosts-path", str(missing)]) == EXIT_ENV_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error: ")
    assert "hint: " in err


def test_unpin_removes_the_block_and_leaves_every_other_line(
    prober, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    _registry_with(_record(WIRELESS))
    prober(**{"192.168.1.162": WIRELESS})
    hosts = _hosts_file(tmp_path)
    before = hosts.read_text(encoding="utf-8")
    assert main(["wireless", "pin", "--hosts-path", str(hosts), "--json"]) == 0
    capsys.readouterr()
    assert main(["wireless", "unpin", "--hosts-path", str(hosts), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is True
    assert hosts.read_text(encoding="utf-8") == before


def test_unpin_with_nothing_pinned_is_a_no_op(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    hosts = _hosts_file(tmp_path)
    assert main(["wireless", "unpin", "--hosts-path", str(hosts), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is False


# --------------------------------------------------------------------------- #
# 9. forget                                                                    #
# --------------------------------------------------------------------------- #


def test_forget_removes_one_remembered_unit(capsys: pytest.CaptureFixture[str]) -> None:
    _registry_with(_record(WIRELESS), _record(LITE))
    assert main(["wireless", "forget", "--unit", WIRELESS.hardware_id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["forgotten"] == [WIRELESS.hardware_id]
    assert {r.hardware_id for r in UnitRegistry().all()} == {LITE.hardware_id}


def test_forget_resolves_an_alias(capsys: pytest.CaptureFixture[str]) -> None:
    _registry_with(_record(WIRELESS, alias="bench"))
    assert main(["wireless", "forget", "--unit", "bench", "--json"]) == 0
    capsys.readouterr()
    assert UnitRegistry().all() == []


def test_forget_all_clears_the_registry(capsys: pytest.CaptureFixture[str]) -> None:
    _registry_with(_record(WIRELESS), _record(LITE))
    assert main(["wireless", "forget", "--all", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert sorted(payload["forgotten"]) == sorted([WIRELESS.hardware_id, LITE.hardware_id])
    assert UnitRegistry().all() == []


def test_forget_with_no_selector_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    _registry_with(_record(WIRELESS))
    assert main(["wireless", "forget"]) == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert "--unit" in err
    assert "--all" in err
    assert UnitRegistry().all() != []


def test_forget_an_unknown_unit_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    _registry_with(_record(WIRELESS))
    assert main(["wireless", "forget", "--unit", "deadbeef"]) == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error: ")
    assert "hint: " in err


def test_forget_never_touches_the_network(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _boom(**_kwargs):
        raise AssertionError("forget must not sweep")

    monkeypatch.setattr(wireless, "_sweep", _boom, raising=True)
    _registry_with(_record(WIRELESS))
    assert main(["wireless", "forget", "--all", "--json"]) == 0
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# 10. Catalog + the bare-install boundary                                      #
# --------------------------------------------------------------------------- #


def test_every_wireless_path_has_an_explain_catalog_entry() -> None:
    keys = set(known_paths())
    missing = [p for p in [("wireless",)] + [("wireless", v) for v in VERBS] if p not in keys]
    assert not missing, f"missing explain catalog entries: {missing!r}"


def test_explain_renders_for_every_wireless_verb(capsys: pytest.CaptureFixture[str]) -> None:
    for verb in VERBS:
        assert main(["explain", "wireless", verb]) == 0
        assert "wireless" in capsys.readouterr().out


def _imported_names(path: Path) -> set[str]:
    """Every dotted name *path* imports, in any syntactic form (module or local)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_noun_itself_names_no_sdk_import() -> None:
    """Not even a lazy, function-local one — the whole noun is transport-free."""
    imported = _imported_names(Path(wireless.__file__))
    assert not [n for n in imported if n == "reachy_mini" or n.startswith("reachy_mini.")]
    assert not [n for n in imported if n.startswith("reachy.robot.sdk")]
    assert "reachy.discover.probe" in imported, "the scan found no imports — it is broken"


def _run_cli(tmp_path: Path, *argv: str) -> tuple[int, str]:
    """Run one CLI invocation in a FRESH interpreter; report rc + the sdk verdict.

    Subprocess, never in-process: ``reachy_mini`` may already be imported by an
    earlier test in this worker, and evicting it from ``sys.modules`` splits
    module identity (the lesson ``tests/test_sleep_boundary.py`` records).
    """
    repo_root = Path(wireless.__file__).resolve().parents[3]
    code = (
        "import sys, json\n"
        "from reachy.cli import main\n"
        f"rc = main({list(argv)!r})\n"
        "sys.stderr.write('SDK=%s\\n' % ('reachy_mini' in sys.modules))\n"
        "sys.exit(rc)\n"
    )
    env = dict(os.environ, REACHY_STATE_DIR=str(tmp_path / "bare-state"))
    proc = subprocess.run(  # nosec B603 — fixed argv, sys.executable, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env=env,
        check=False,
    )
    return proc.returncode, proc.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ("wireless", "overview", "--json"),
        ("wireless", "list", "--json"),
        ("wireless", "forget", "--all", "--json"),
        ("explain", "wireless", "find"),
    ],
)
def test_the_whole_noun_works_without_the_sdk_or_daemon_extra(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    """A bare install has no ``reachy_mini`` at all; the noun must never need one."""
    rc, err = _run_cli(tmp_path, *argv)
    assert rc == 0, err
    assert "SDK=False" in err, f"'{' '.join(argv)}' imported the reachy_mini SDK: {err}"


def test_the_transport_default_did_not_move() -> None:
    """Discovery only SUPPLIES an address; it never changes the transport contract."""
    from reachy.robot.transport import DEFAULT_BASE_URL

    assert DEFAULT_BASE_URL == "http://localhost:8000"
