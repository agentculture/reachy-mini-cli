"""Tests for reachy.discover.sweep — interface enumeration + the bounded sweep.

Acceptance criteria covered (one section each):

1. Given a fake interface table containing a /16 bridge, loopback and a
   tailscale /32 alongside a real /24, enumeration returns ONLY the /24 hosts
   — a prefix wider than /24 is NEVER expanded.
2. Two interfaces on the same subnet (mirroring this box's 192.168.1.157 and
   192.168.1.118) yield each host exactly once, and one unit answering on both
   yields a single record.
3. With every host blackholing, the sweep returns within its overall deadline
   and the worker pool never exceeds its cap.

**No test here touches a real NIC or a real network.** Every test injects the
interface table through the module-level ``read_interfaces`` seam (the same
name task t6's autouse conftest guard neutralises suite-wide) and injects a
fake ``probe_fn``, so nothing here opens a socket.
"""

from __future__ import annotations

import dataclasses
import inspect
import threading
import time

import pytest

from reachy.discover import sweep as sweep_mod
from reachy.discover.probe import UnitRecord
from reachy.discover.sweep import (
    DEFAULT_MAX_WORKERS,
    Interface,
    SweepResult,
    enumerate_hosts,
    read_interfaces,
    sweep,
    sweepable_networks,
)

# ---------------------------------------------------------------------------
# The real interface table of the box this feature was specified on, verbatim
# from `ip -4 addr` (spec honesty condition h1). Seven Docker bridges on 172.x
# /16, a Tailscale /32, loopback, and TWO NICs on the SAME /24.
#
# Naively expanding the seven /16s is ~459 000 hosts, which is exactly the
# hazard this module exists to make structurally impossible.
# ---------------------------------------------------------------------------
THIS_BOX = (
    Interface(name="lo", address="127.0.0.1", prefixlen=8),
    Interface(name="wlP9s9", address="192.168.1.157", prefixlen=24),
    Interface(name="tailscale0", address="100.127.105.72", prefixlen=32),
    Interface(name="br-9de08280ae86", address="172.23.0.1", prefixlen=16),
    Interface(name="br-ac364e623b34", address="172.19.0.1", prefixlen=16),
    Interface(name="br-31c3209412dc", address="172.21.0.1", prefixlen=16),
    Interface(name="br-3d690c587b1d", address="172.20.0.1", prefixlen=16),
    Interface(name="br-4adccda474ac", address="172.24.0.1", prefixlen=16),
    Interface(name="docker0", address="172.17.0.1", prefixlen=16),
    Interface(name="br-88d70424e624", address="172.18.0.1", prefixlen=16),
    Interface(name="wlx90de80db7994", address="192.168.1.118", prefixlen=24),
)

#: Every host of 192.168.1.0/24 — .1 through .254, network and broadcast excluded.
LAN_24_HOSTS = tuple(f"192.168.1.{n}" for n in range(1, 255))


def table(*interfaces: Interface):
    """Build an injected interface source over a fixed table."""
    return lambda: tuple(interfaces)


def unit(hardware_id: str, address: str, *, wireless: bool = True) -> UnitRecord:
    """A probe result stand-in — only identity + address matter to the sweep."""
    return UnitRecord(
        hardware_id=hardware_id,
        robot_name="reachy_mini",
        model="Reachy Mini Wireless" if wireless else "Reachy Mini Lite",
        wireless=wireless,
        version="1.9.0",
        wlan_ip=address,
        address=address,
    )


# ===========================================================================
# 1. A prefix wider than /24 is never expanded
# ===========================================================================


class TestPrefixWidthIsRejectedByConstruction:
    def test_this_box_enumerates_only_the_lan_24(self):
        assert enumerate_hosts(source=table(*THIS_BOX)) == LAN_24_HOSTS

    def test_the_seven_docker_16s_contribute_no_host(self):
        hosts = enumerate_hosts(source=table(*THIS_BOX))
        assert not [h for h in hosts if h.startswith("172.")]

    def test_loopback_contributes_no_host(self):
        hosts = enumerate_hosts(source=table(*THIS_BOX))
        assert not [h for h in hosts if h.startswith("127.")]

    def test_the_tailscale_32_contributes_no_host(self):
        hosts = enumerate_hosts(source=table(*THIS_BOX))
        assert not [h for h in hosts if h.startswith("100.")]

    @pytest.mark.parametrize("address", ["100.64.0.1", "100.99.5.7", "100.127.255.1"])
    def test_shared_address_space_is_refused_at_a_sweepable_width(self, address):
        """RFC 6598 stays out even when the width rule would have let it in.

        ``tailscale0`` is a ``/32`` today, so the width rule alone hides this.
        A tailnet presenting a ``/24`` would not be hidden — and the rule that
        catches it is derived (``is_private`` is False for shared address
        space, the only range that is neither private nor global) rather than a
        hardcoded ``100.64.0.0/10`` literal.
        """
        iface = Interface(name="tailscale0", address=address, prefixlen=24)
        assert sweepable_networks([iface]) == ()
        assert enumerate_hosts(source=table(iface)) == ()

    @pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
    def test_a_publicly_routable_range_is_never_swept(self, address):
        """No robot is on public space, and probing it is not this tool's business.

        Gained for free by requiring a PRIVATE network rather than listing the
        ranges that are not — the previous exclusion list would have swept this.
        """
        iface = Interface(name="eth0", address=address, prefixlen=24)
        assert sweepable_networks([iface]) == ()

    def test_an_interface_with_no_address_assigned_contributes_nothing(self):
        """RFC 1122 'this network' — ``SIOCGIFADDR`` is all-zero on an unconfigured NIC.

        ``is_unspecified`` is only true of the ``/32``, so paired with a real
        netmask this would otherwise expand to 254 meaningless hosts.
        """
        iface = Interface(name="eth0", address="0.0.0.0", prefixlen=24)  # nosec B104
        assert sweepable_networks([iface]) == ()

    @pytest.mark.parametrize(
        "netmask,expected",
        [
            ("255.255.255.0", 24),
            ("255.255.0.0", 16),
            ("255.255.255.255", 32),
            ("255.255.255.252", 30),
            ("255.128.0.0", 9),
            ("0.0.0.0", 0),  # nosec B104
        ],
    )
    def test_prefixlen_of_a_netmask(self, netmask, expected):
        assert sweep_mod._prefixlen_of(netmask) == expected

    @pytest.mark.parametrize("netmask", ["255.0.255.0", "0.255.0.0", "255.255.0.255"])
    def test_a_non_contiguous_netmask_is_refused(self, netmask):
        """The discarded ``ip_network`` form rejected these for free.

        ``read_interfaces`` turns the raise into "skip this interface"; a bare
        population count would have accepted the row and returned a prefix
        length describing no real network.
        """
        with pytest.raises(ValueError):
            sweep_mod._prefixlen_of(netmask)

    def test_enumeration_of_this_box_stays_tiny_not_459k(self):
        # The whole point: seven /16s naively expanded are ~459 000 hosts.
        assert len(enumerate_hosts(source=table(*THIS_BOX))) == 254

    @pytest.mark.parametrize("prefixlen", [8, 12, 16, 20, 23])
    def test_any_prefix_wider_than_24_yields_nothing(self, prefixlen):
        wide = Interface(name="eth0", address="10.1.2.3", prefixlen=prefixlen)
        assert enumerate_hosts(source=table(wide)) == ()
        assert sweepable_networks([wide]) == ()

    def test_exactly_24_is_accepted(self):
        iface = Interface(name="eth0", address="10.1.2.3", prefixlen=24)
        assert len(enumerate_hosts(source=table(iface))) == 254

    @pytest.mark.parametrize("prefixlen", [25, 26, 28, 30])
    def test_prefixes_narrower_than_24_are_accepted(self, prefixlen):
        iface = Interface(name="eth0", address="10.1.2.3", prefixlen=prefixlen)
        hosts = enumerate_hosts(source=table(iface))
        assert 0 < len(hosts) <= 254

    @pytest.mark.parametrize("prefixlen", [31, 32])
    def test_host_routes_carry_no_lan_and_are_dropped(self, prefixlen):
        # A /32 (tailscale) or /31 (point-to-point) is not a LAN to sweep.
        iface = Interface(name="tailscale0", address="100.127.105.72", prefixlen=prefixlen)
        assert enumerate_hosts(source=table(iface)) == ()

    def test_a_docker_bridge_on_a_24_is_still_rejected_by_name(self):
        # docker networks CAN be /24 (`--subnet`), so the width rule alone is
        # not enough: the interface naming is the second, independent guard.
        for name in ("docker0", "br-9de08280ae86", "virbr0", "veth1914a21"):
            iface = Interface(name=name, address="172.28.5.1", prefixlen=24)
            assert enumerate_hosts(source=table(iface)) == (), name

    def test_link_local_is_dropped_even_on_a_narrow_prefix(self):
        iface = Interface(name="eth0", address="169.254.7.1", prefixlen=24)
        assert enumerate_hosts(source=table(iface)) == ()

    def test_multicast_and_unspecified_are_dropped(self):
        for address in ("224.0.0.1", "0.0.0.0"):  # nosec B104 - a filter fixture
            iface = Interface(name="eth0", address=address, prefixlen=24)
            assert enumerate_hosts(source=table(iface)) == (), address

    def test_a_malformed_row_is_skipped_not_raised(self):
        bad = Interface(name="eth0", address="not-an-ip", prefixlen=24)
        good = Interface(name="eth1", address="192.168.9.5", prefixlen=24)
        assert len(enumerate_hosts(source=table(bad, good))) == 254

    def test_a_source_that_raises_degrades_to_no_hosts(self):
        def boom():
            raise OSError("no /proc here")

        assert enumerate_hosts(source=boom) == ()

    def test_the_total_host_count_is_hard_capped(self):
        # Four /24s = 1016 hosts; a cap of 300 truncates rather than expanding.
        ifaces = [
            Interface(name=f"eth{n}", address=f"192.168.{n}.1", prefixlen=24) for n in range(4)
        ]
        hosts = enumerate_hosts(source=table(*ifaces), max_hosts=300)
        assert len(hosts) == 300


# ===========================================================================
# 2. Dedupe — overlapping subnets, and one unit on two interfaces
# ===========================================================================


class TestDedupe:
    def test_two_nics_on_the_same_24_yield_each_host_exactly_once(self):
        both = table(
            Interface(name="wlP9s9", address="192.168.1.157", prefixlen=24),
            Interface(name="wlx90de80db7994", address="192.168.1.118", prefixlen=24),
        )
        hosts = enumerate_hosts(source=both)
        assert hosts == LAN_24_HOSTS
        assert len(hosts) == len(set(hosts))

    def test_the_same_subnet_is_only_enumerated_once(self):
        both = [
            Interface(name="wlP9s9", address="192.168.1.157", prefixlen=24),
            Interface(name="wlx90de80db7994", address="192.168.1.118", prefixlen=24),
        ]
        assert len(sweepable_networks(both)) == 1

    def test_overlapping_subnets_of_different_widths_yield_each_host_once(self):
        overlapping = table(
            Interface(name="eth0", address="192.168.1.157", prefixlen=24),
            Interface(name="eth1", address="192.168.1.118", prefixlen=25),
        )
        hosts = enumerate_hosts(source=overlapping)
        assert len(hosts) == len(set(hosts)) == 254

    def test_hosts_are_returned_in_a_deterministic_ascending_order(self):
        two = table(
            Interface(name="eth1", address="192.168.9.1", prefixlen=30),
            Interface(name="eth0", address="192.168.1.1", prefixlen=30),
        )
        assert enumerate_hosts(source=two) == (
            "192.168.1.1",
            "192.168.1.2",
            "192.168.9.1",
            "192.168.9.2",
        )

    def test_one_unit_answering_on_two_addresses_is_one_record(self):
        answers = {
            "192.168.1.10": unit("a89063c05ae79779", "192.168.1.10"),
            "192.168.1.20": unit("a89063c05ae79779", "192.168.1.20"),
        }
        result = sweep(
            hosts=tuple(answers),
            probe_fn=lambda host, port, timeout: answers.get(host),
        )
        assert [u.hardware_id for u in result.units] == ["a89063c05ae79779"]

    def test_the_kept_address_is_the_lowest_ordered_one(self):
        answers = {
            "192.168.1.10": unit("a89063c05ae79779", "192.168.1.10"),
            "192.168.1.20": unit("a89063c05ae79779", "192.168.1.20"),
        }
        result = sweep(
            hosts=("192.168.1.10", "192.168.1.20"),
            probe_fn=lambda host, port, timeout: answers.get(host),
        )
        assert result.units[0].address == "192.168.1.10"

    def test_two_distinct_units_are_two_records(self):
        answers = {
            "192.168.1.10": unit("aaaa", "192.168.1.10"),
            "192.168.1.20": unit("bbbb", "192.168.1.20", wireless=False),
        }
        result = sweep(
            hosts=tuple(answers),
            probe_fn=lambda host, port, timeout: answers.get(host),
        )
        assert sorted(u.hardware_id for u in result.units) == ["aaaa", "bbbb"]

    def test_units_are_reported_in_host_order_not_completion_order(self):
        answers = {h: unit(h, h) for h in ("192.168.1.5", "192.168.1.6", "192.168.1.7")}

        def slow_first(host, port, timeout):
            if host.endswith(".5"):
                time.sleep(0.05)
            return answers[host]

        result = sweep(hosts=tuple(sorted(answers)), probe_fn=slow_first, max_workers=4)
        assert [u.address for u in result.units] == ["192.168.1.5", "192.168.1.6", "192.168.1.7"]


# ===========================================================================
# 3. The sweep is bounded — deadline + worker cap, always terminates
# ===========================================================================


class TestBoundedSweep:
    def test_a_blackholing_lan_returns_inside_the_deadline(self):
        release = threading.Event()

        def blackhole(host, port, timeout):
            release.wait(30.0)
            return None

        started = time.monotonic()
        try:
            result = sweep(
                hosts=LAN_24_HOSTS,
                probe_fn=blackhole,
                max_workers=8,
                deadline_s=0.5,
            )
            elapsed = time.monotonic() - started
        finally:
            release.set()

        assert elapsed < 5.0
        assert result.deadline_reached is True
        assert result.units == ()

    def test_the_worker_pool_never_exceeds_its_cap(self):
        release = threading.Event()
        lock = threading.Lock()
        state = {"live": 0, "peak": 0}

        def blackhole(host, port, timeout):
            with lock:
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
            release.wait(30.0)
            with lock:
                state["live"] -= 1
            return None

        try:
            sweep(hosts=LAN_24_HOSTS, probe_fn=blackhole, max_workers=8, deadline_s=0.5)
        finally:
            release.set()

        assert 0 < state["peak"] <= 8

    def test_a_slow_host_does_not_hide_a_fast_answer(self):
        release = threading.Event()
        found = unit("a89063c05ae79779", "192.168.1.2")

        def mixed(host, port, timeout):
            if host == "192.168.1.2":
                return found
            release.wait(30.0)
            return None

        try:
            result = sweep(
                hosts=LAN_24_HOSTS,
                probe_fn=mixed,
                max_workers=8,
                deadline_s=1.0,
            )
        finally:
            release.set()

        assert [u.hardware_id for u in result.units] == ["a89063c05ae79779"]

    def test_an_already_expired_deadline_returns_immediately(self):
        started = time.monotonic()
        result = sweep(hosts=LAN_24_HOSTS, probe_fn=lambda h, p, t: None, deadline_s=0.0)
        assert time.monotonic() - started < 5.0
        assert result.deadline_reached is True

    def test_a_probe_that_raises_never_reaches_the_caller(self):
        def boom(host, port, timeout):
            raise RuntimeError("this host bit back")

        result = sweep(hosts=("192.168.1.1", "192.168.1.2"), probe_fn=boom)
        assert result.units == ()
        assert result.deadline_reached is False

    def test_an_empty_host_list_is_an_empty_result(self):
        result = sweep(hosts=())
        assert result.units == ()
        assert result.hosts_total == 0
        assert result.deadline_reached is False

    def test_every_host_is_probed_exactly_once(self):
        seen = []
        lock = threading.Lock()

        def record(host, port, timeout):
            with lock:
                seen.append(host)
            return None

        hosts = LAN_24_HOSTS[:32]
        result = sweep(hosts=hosts, probe_fn=record, max_workers=8, deadline_s=10.0)
        assert sorted(seen) == sorted(hosts)
        assert result.hosts_probed == len(hosts)
        assert result.hosts_total == len(hosts)

    def test_the_port_and_timeout_reach_the_probe(self):
        calls = []
        result = sweep(
            hosts=("192.168.1.9",),
            probe_fn=lambda host, port, timeout: calls.append((host, port, timeout)),
            port=9999,
            timeout=0.25,
        )
        assert calls == [("192.168.1.9", 9999, 0.25)]
        assert result.units == ()

    def test_the_result_is_a_frozen_dataclass(self):
        result = sweep(hosts=())
        assert isinstance(result, SweepResult)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.units = ()  # type: ignore[misc]

    def test_the_default_worker_cap_is_bounded(self):
        assert 1 <= DEFAULT_MAX_WORKERS <= 256

    def test_the_sweep_defaults_to_the_injected_interface_source(self, monkeypatch):
        monkeypatch.setattr(sweep_mod, "read_interfaces", lambda: ())
        result = sweep(probe_fn=lambda h, p, t: None)
        assert result.hosts_total == 0


# ===========================================================================
# The injection seam itself — what task t6's autouse guard neutralises
# ===========================================================================


class TestTheInterfaceSourceSeam:
    def test_the_package_attribute_named_sweep_is_the_MODULE_not_the_function(self):
        """The seam only exists if ``reachy.discover.sweep`` names the module.

        ``sweep.py`` holds a function also called ``sweep``. Re-exporting it
        from ``reachy/discover/__init__.py`` rebinds the package attribute from
        the module to the function, and ``import reachy.discover.sweep as m``
        consults ``getattr(package, "sweep")`` BEFORE ``sys.modules`` — so every
        ``monkeypatch.setattr(<module>, "read_interfaces", ...)``, task t6's
        autouse guard included, would fail with ``AttributeError``. This
        happened; the pin is here so it cannot happen quietly again.
        """
        import reachy.discover

        assert inspect.ismodule(reachy.discover.sweep)
        assert reachy.discover.sweep is sweep_mod
        assert "sweep" not in getattr(reachy.discover, "__all__", ())

    def test_read_interfaces_is_a_module_level_callable(self, monkeypatch):
        """Asserts a structural fact about the UNGUARDED module.

        Task t6's autouse ``_no_live_lan_sweep`` guard patches this exact
        attribute by default on every test, this one included — so
        ``monkeypatch.undo()`` first, reverting every patch registered on this
        test's (shared, function-scoped) ``monkeypatch`` instance so far, to
        see the real binding rather than the guard's stand-in.
        """
        monkeypatch.undo()
        assert callable(sweep_mod.read_interfaces)
        assert sweep_mod.read_interfaces is read_interfaces

    def test_patching_the_module_global_redirects_enumeration(self, monkeypatch):
        monkeypatch.setattr(
            sweep_mod,
            "read_interfaces",
            lambda: (Interface(name="fake0", address="10.9.9.1", prefixlen=30),),
        )
        assert enumerate_hosts() == ("10.9.9.1", "10.9.9.2")

    def test_a_neutralised_seam_yields_no_hosts_at_all(self, monkeypatch):
        monkeypatch.setattr(sweep_mod, "read_interfaces", lambda: ())
        assert enumerate_hosts() == ()

    def test_read_interfaces_returns_a_tuple_of_interfaces(self, monkeypatch):
        # Called for real, but with its ONE syscall seam stubbed, so no NIC is
        # touched: this asserts the shape of the contract, not the box's table.
        monkeypatch.setattr(sweep_mod, "_if_nameindex", lambda: [(1, "eth0")])
        monkeypatch.setattr(sweep_mod, "_ipv4_of", lambda _sock, _name: ("10.0.0.5", 24))
        result = read_interfaces()
        assert result == (Interface(name="eth0", address="10.0.0.5", prefixlen=24),)

    def test_read_interfaces_degrades_to_empty_when_the_source_is_unavailable(self, monkeypatch):
        def boom():
            raise OSError("no netlink, no /proc, no ioctl")

        monkeypatch.setattr(sweep_mod, "_if_nameindex", boom)
        assert read_interfaces() == ()

    def test_read_interfaces_skips_an_interface_with_no_ipv4(self, monkeypatch):
        def per_interface(_sock, name):
            if name == "enP7s7":
                raise OSError(99, "Cannot assign requested address")
            return ("10.0.0.5", 24)

        monkeypatch.setattr(sweep_mod, "_if_nameindex", lambda: [(1, "enP7s7"), (2, "eth0")])
        monkeypatch.setattr(sweep_mod, "_ipv4_of", per_interface)
        assert [i.name for i in read_interfaces()] == ["eth0"]
