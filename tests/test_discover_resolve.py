"""Tests for reachy.discover.resolve — the composed fast-path + escalation lookup.

Acceptance criteria covered (one class each):

1. A remembered unit answering at its last-known IP with a matching
   hardware_id is returned WITHOUT any sweep being invoked — asserted by call
   count, not just by inspecting the result.
2. A remembered IP now answering as a DIFFERENT hardware_id is rejected,
   escalates to the sweep, and the record is re-pinned to the new address.
3. A dark remembered IP costs ONE short bounded timeout before escalating,
   not a full per-unit connect timeout.
4. With two units in the registry, resolution with no selector exits non-zero
   naming both candidates and never picks one.

Plus coverage for the ``--unit`` selector, the registry-level default, the
first-ever-discovery path (zero known units), and the "still not found after
a sweep" refusal.

Every test injects its own ``probe_fn`` / ``sweep_fn`` fakes and a ``tmp_path``
-backed registry — nothing here opens a socket or touches a real interface.
"""

from __future__ import annotations

import pytest

from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.discover.probe import UnitRecord
from reachy.discover.registry import RegistryRecord, UnitRegistry
from reachy.discover.resolve import (
    FAST_PATH_TIMEOUT,
    REASON_ESCALATED_MISMATCH,
    REASON_ESCALATED_MISS,
    REASON_FAST_PATH,
    REASON_FIRST_SIGHTING,
    ResolvedUnit,
    resolve,
)
from reachy.discover.sweep import SweepResult

# ---------------------------------------------------------------------------
# Shared fixture data + helpers
# ---------------------------------------------------------------------------

WIRELESS_HW = "a89063c05ae79779"
LITE_HW = "deadbeef01234567"


def _unit(
    *,
    hardware_id: str = WIRELESS_HW,
    robot_name: str = "reachy_mini",
    model: str = "Reachy Mini Wireless",
    wireless: bool = True,
    version: str = "1.9.0",
    wlan_ip: str | None = "192.168.1.162",
    address: str = "192.168.1.162",
) -> UnitRecord:
    return UnitRecord(
        hardware_id=hardware_id,
        robot_name=robot_name,
        model=model,
        wireless=wireless,
        version=version,
        wlan_ip=wlan_ip,
        address=address,
    )


def _record(
    *,
    hardware_id: str = WIRELESS_HW,
    last_ip: str = "192.168.1.162",
    name: str = "reachy_mini",
    model: str = "Reachy Mini Wireless",
    wireless: bool = True,
    mac: str | None = None,
    alias: str | None = None,
    last_seen: str = "2026-08-08T00:00:00+00:00",
) -> RegistryRecord:
    return RegistryRecord(
        hardware_id=hardware_id,
        mac=mac,
        last_ip=last_ip,
        name=name,
        model=model,
        wireless=wireless,
        last_seen=last_seen,
        alias=alias,
    )


def _sweep_result(units: tuple[UnitRecord, ...]) -> SweepResult:
    return SweepResult(
        units=units,
        hosts_total=254,
        hosts_probed=254,
        deadline_reached=False,
        elapsed_s=1.5,
    )


class _Counter:
    """A tiny call-tracking wrapper so a test can assert call COUNT, not just
    that the eventual result looks right."""

    def __init__(self, fn):
        self._fn = fn
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._fn(*args, **kwargs)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _never_call(*_args, **_kwargs):  # pragma: no cover - only reached on a bug
    raise AssertionError("this seam must not be called on this path")


def _probe_echo(host, port, timeout):
    """A fake probe_fn: answers as the WIRELESS unit at whatever host was asked."""
    return _unit(address=host)


def _probe_dark(host, port, timeout):
    """A fake probe_fn: never answers."""
    return None


def _probe_impostor(host, port, timeout):
    """A fake probe_fn: answers, but as a DIFFERENT unit than expected."""
    return _unit(hardware_id="someone-elses-unit")


def _sweep_empty(**kwargs):
    return _sweep_result(())


def _sweep_finds_wireless_at(address):
    def _fake(**kwargs):
        return _sweep_result((_unit(address=address),))

    return _fake


# ---------------------------------------------------------------------------
# Criterion 1 -- matching fast path returns without any sweep invocation
# ---------------------------------------------------------------------------


class TestFastPathNeverSweeps:
    def test_matching_fast_path_returns_without_invoking_sweep(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())

        probe_fn = _Counter(_probe_echo)
        sweep_fn = _Counter(_never_call)

        result = resolve(registry=registry, probe_fn=probe_fn, sweep_fn=sweep_fn)

        assert sweep_fn.call_count == 0
        assert probe_fn.call_count == 1
        assert isinstance(result, ResolvedUnit)
        assert result.unit.hardware_id == WIRELESS_HW
        assert result.reason == REASON_FAST_PATH

    def test_fast_path_probes_exactly_the_remembered_ip(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(last_ip="192.168.1.162"))

        probe_fn = _Counter(_probe_echo)
        sweep_fn = _Counter(_never_call)

        resolve(registry=registry, probe_fn=probe_fn, sweep_fn=sweep_fn)

        args, _kwargs = probe_fn.calls[0]
        assert args[0] == "192.168.1.162"

    def test_fast_path_refreshes_last_seen_and_preserves_alias_and_mac(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(mac="88:a2:9e:8c:fa:bf", alias="bench"))

        sweep_fn = _Counter(_never_call)

        result = resolve(
            registry=registry,
            probe_fn=_probe_echo,
            sweep_fn=sweep_fn,
            now=lambda: "2026-08-08T12:00:00+00:00",
        )

        assert result.registry_record.mac == "88:a2:9e:8c:fa:bf"
        assert result.registry_record.alias == "bench"
        assert result.registry_record.last_seen == "2026-08-08T12:00:00+00:00"
        assert registry.get(WIRELESS_HW).last_seen == "2026-08-08T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Criterion 2 -- identity mismatch is rejected, escalates, and re-pins
# ---------------------------------------------------------------------------


class TestMismatchEscalatesAndRepins:
    def test_a_different_hardware_id_at_the_remembered_ip_is_rejected_and_escalates(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(last_ip="192.168.1.162"))

        # DHCP handed .162 to a totally different device.
        probe_fn = _Counter(_probe_impostor)
        sweep_fn = _Counter(_sweep_finds_wireless_at("192.168.1.200"))

        result = resolve(registry=registry, probe_fn=probe_fn, sweep_fn=sweep_fn)

        assert sweep_fn.call_count == 1
        assert result.reason == REASON_ESCALATED_MISMATCH
        assert result.unit.address == "192.168.1.200"
        assert result.unit.hardware_id == WIRELESS_HW

    def test_the_record_is_repinned_to_the_newly_found_address(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(last_ip="192.168.1.162", mac="88:a2:9e:8c:fa:bf", alias="bench"))

        resolve(
            registry=registry,
            probe_fn=_probe_impostor,
            sweep_fn=_sweep_finds_wireless_at("192.168.1.200"),
        )

        repinned = registry.get(WIRELESS_HW)
        assert repinned.last_ip == "192.168.1.200"
        # Re-pinning is a location update, not a fresh sighting: mac + alias
        # carry forward unchanged.
        assert repinned.mac == "88:a2:9e:8c:fa:bf"
        assert repinned.alias == "bench"

    def test_a_mismatch_never_returns_the_impostor_that_answered(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())

        result = resolve(
            registry=registry,
            probe_fn=_probe_impostor,
            sweep_fn=_sweep_finds_wireless_at("192.168.1.200"),
        )

        assert result.unit.hardware_id != "someone-elses-unit"

    def test_still_unfindable_after_the_sweep_raises_a_named_clierror(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())

        with pytest.raises(CliError) as excinfo:
            resolve(registry=registry, probe_fn=_probe_impostor, sweep_fn=_sweep_empty)

        assert excinfo.value.code == EXIT_USER_ERROR
        assert WIRELESS_HW in excinfo.value.message


# ---------------------------------------------------------------------------
# Criterion 3 -- a dark remembered IP costs ONE bounded timeout, then escalates
# ---------------------------------------------------------------------------


class TestDarkIpCostsOneBoundedTimeout:
    def test_one_probe_call_at_a_short_bounded_timeout_then_escalates(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(last_ip="192.168.1.162"))

        probe_fn = _Counter(_probe_dark)  # dark: never answers
        sweep_fn = _Counter(_sweep_finds_wireless_at("192.168.1.200"))

        result = resolve(registry=registry, probe_fn=probe_fn, sweep_fn=sweep_fn)

        assert probe_fn.call_count == 1
        # timeout is the third positional arg per the ProbeFn signature.
        host, port, timeout = probe_fn.calls[0][0]
        assert host == "192.168.1.162"
        assert timeout == FAST_PATH_TIMEOUT
        # Short and bounded: nowhere near an unbounded/system-default connect.
        assert timeout <= 2.0
        assert sweep_fn.call_count == 1
        assert result.reason == REASON_ESCALATED_MISS
        assert result.unit.address == "192.168.1.200"

    def test_a_dark_ip_never_retries_the_fast_path_before_escalating(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())

        probe_fn = _Counter(_probe_dark)

        resolve(
            registry=registry,
            probe_fn=probe_fn,
            sweep_fn=_sweep_finds_wireless_at("192.168.1.200"),
        )

        # Exactly one fast-path probe -- no silent retry loop hiding a slower
        # path behind the same call count.
        assert probe_fn.call_count == 1

    def test_a_dark_ip_that_the_sweep_also_cannot_find_raises_named_error(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())

        with pytest.raises(CliError) as excinfo:
            resolve(registry=registry, probe_fn=_probe_dark, sweep_fn=_sweep_empty)

        assert excinfo.value.code == EXIT_USER_ERROR


# ---------------------------------------------------------------------------
# Criterion 4 -- ambiguity with two known units and no selector is refused
# ---------------------------------------------------------------------------


class TestAmbiguityIsRefused:
    def test_two_known_units_with_no_selector_exits_non_zero_naming_both(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(
            _record(
                hardware_id=LITE_HW, last_ip="127.0.0.1", model="Reachy Mini Lite", wireless=False
            )
        )
        registry.upsert(
            _record(hardware_id=WIRELESS_HW, last_ip="192.168.1.162", model="Reachy Mini Wireless")
        )

        probe_fn = _Counter(_never_call)
        sweep_fn = _Counter(_never_call)

        with pytest.raises(CliError) as excinfo:
            resolve(registry=registry, probe_fn=probe_fn, sweep_fn=sweep_fn)

        err = excinfo.value
        assert err.code == EXIT_USER_ERROR
        assert LITE_HW in err.remediation
        assert WIRELESS_HW in err.remediation
        # Ambiguity is refused BEFORE any network I/O -- it is a pure registry
        # fact, so neither seam is ever touched.
        assert probe_fn.call_count == 0
        assert sweep_fn.call_count == 0

    def test_ambiguity_never_silently_picks_one(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(hardware_id=LITE_HW, last_ip="127.0.0.1"))
        registry.upsert(_record(hardware_id=WIRELESS_HW, last_ip="192.168.1.162"))

        with pytest.raises(CliError):
            resolve(registry=registry, probe_fn=_never_call, sweep_fn=_never_call)

    def test_selector_disambiguates_between_two_known_units(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(hardware_id=LITE_HW, last_ip="127.0.0.1", model="Reachy Mini Lite"))
        registry.upsert(_record(hardware_id=WIRELESS_HW, last_ip="192.168.1.162"))

        sweep_fn = _Counter(_never_call)

        result = resolve(WIRELESS_HW, registry=registry, probe_fn=_probe_echo, sweep_fn=sweep_fn)

        assert result.unit.hardware_id == WIRELESS_HW
        assert sweep_fn.call_count == 0

    def test_selector_by_alias_disambiguates(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(hardware_id=LITE_HW, last_ip="127.0.0.1", alias="lite"))
        registry.upsert(_record(hardware_id=WIRELESS_HW, last_ip="192.168.1.162", alias="wireless"))

        def probe_as_lite(host, port, timeout):
            return _unit(hardware_id=LITE_HW, address=host)

        result = resolve("lite", registry=registry, probe_fn=probe_as_lite, sweep_fn=_never_call)

        assert result.unit.hardware_id == LITE_HW

    def test_registry_level_default_is_used_when_no_selector_given(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(hardware_id=LITE_HW, last_ip="127.0.0.1"))
        registry.upsert(_record(hardware_id=WIRELESS_HW, last_ip="192.168.1.162"))

        result = resolve(
            None,
            default=WIRELESS_HW,
            registry=registry,
            probe_fn=_probe_echo,
            sweep_fn=_never_call,
        )

        assert result.unit.hardware_id == WIRELESS_HW

    def test_an_explicit_selector_overrides_the_default(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(hardware_id=LITE_HW, last_ip="127.0.0.1"))
        registry.upsert(_record(hardware_id=WIRELESS_HW, last_ip="192.168.1.162"))

        def probe_as_lite(host, port, timeout):
            return _unit(hardware_id=LITE_HW, address=host)

        result = resolve(
            LITE_HW,
            default=WIRELESS_HW,
            registry=registry,
            probe_fn=probe_as_lite,
            sweep_fn=_never_call,
        )

        assert result.unit.hardware_id == LITE_HW

    def test_an_unknown_selector_raises_a_named_clierror(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())

        with pytest.raises(CliError) as excinfo:
            resolve(
                "not-a-known-unit", registry=registry, probe_fn=_never_call, sweep_fn=_never_call
            )

        assert excinfo.value.code == EXIT_USER_ERROR
        assert "not-a-known-unit" in excinfo.value.message


# ---------------------------------------------------------------------------
# Zero known units -- first-ever discovery falls straight through to a sweep
# ---------------------------------------------------------------------------


class TestFirstEverDiscovery:
    def test_zero_known_units_sweeps_and_registers_the_single_find(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")

        sweep_fn = _Counter(lambda **kwargs: _sweep_result((_unit(),)))

        result = resolve(registry=registry, probe_fn=_never_call, sweep_fn=sweep_fn)

        assert sweep_fn.call_count == 1
        assert result.reason == REASON_FIRST_SIGHTING
        assert result.unit.hardware_id == WIRELESS_HW
        assert registry.get(WIRELESS_HW) is not None
        assert registry.get(WIRELESS_HW).last_ip == "192.168.1.162"

    def test_zero_known_units_and_zero_sweep_hits_raises_named_error(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")

        with pytest.raises(CliError) as excinfo:
            resolve(registry=registry, probe_fn=_never_call, sweep_fn=_sweep_empty)

        assert excinfo.value.code == EXIT_USER_ERROR

    def test_zero_known_units_and_multiple_sweep_hits_refuses_and_names_both(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")

        def sweep_finds_both(**kwargs):
            return _sweep_result(
                (
                    _unit(hardware_id=LITE_HW, address="127.0.0.1", model="Reachy Mini Lite"),
                    _unit(hardware_id=WIRELESS_HW, address="192.168.1.162"),
                )
            )

        with pytest.raises(CliError) as excinfo:
            resolve(registry=registry, probe_fn=_never_call, sweep_fn=sweep_finds_both)

        err = excinfo.value
        assert err.code == EXIT_USER_ERROR
        assert LITE_HW in err.remediation
        assert WIRELESS_HW in err.remediation
        # Nothing was persisted -- an ambiguous first sighting registers nothing.
        assert registry.all() == []


# ---------------------------------------------------------------------------
# Defaults resolve to the real probe/sweep seams when nothing is injected --
# proven by patching the module-level seam names, never by touching a real
# socket or a real interface.
# ---------------------------------------------------------------------------


class TestDefaultsResolveToTheRealSeams:
    def test_omitting_probe_fn_falls_back_to_the_real_probe_seam(self, tmp_path, monkeypatch):
        import reachy.discover.resolve as resolve_mod

        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())

        calls = []

        def fake_probe(host, port, timeout):
            calls.append((host, port, timeout))
            return _unit(address=host)

        monkeypatch.setattr(resolve_mod, "_real_probe", fake_probe)

        result = resolve(registry=registry, sweep_fn=_never_call)

        assert calls == [("192.168.1.162", 8000, FAST_PATH_TIMEOUT)]
        assert result.reason == REASON_FAST_PATH

    def test_omitting_sweep_fn_falls_back_to_the_real_sweep_seam(self, tmp_path, monkeypatch):
        import reachy.discover.resolve as resolve_mod

        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())

        calls = []

        def fake_sweep(**kwargs):
            calls.append(kwargs)
            return _sweep_result((_unit(address="192.168.1.200"),))

        monkeypatch.setattr(resolve_mod, "_real_probe", _probe_dark)
        monkeypatch.setattr(resolve_mod, "_real_sweep", fake_sweep)

        result = resolve(registry=registry)

        assert len(calls) == 1
        assert result.reason == REASON_ESCALATED_MISS
