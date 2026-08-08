"""Task t6 — proves the sixth autouse ``conftest.py`` guard, ``_no_live_lan_sweep``.

``reachy/discover/sweep.py``'s module docstring names ``read_interfaces`` as the
ONE seam that touches the real box's NICs. Without a suite-wide guard, any
discovery test that forgets to inject a fake interface table would enumerate
this box's real interfaces and — via ``enumerate_hosts``/``sweep`` — probe a
real LAN, exactly the class of defect ``CLAUDE.md``'s "Hard constraints"
section records for ``events-cli`` (a suite run that reached the deployed
robot). These tests pin the guard's two promises: the real entry point is
neutralised process-wide by default, and a test that legitimately needs the
real thing can still opt out — the same escape hatch every sibling guard in
``tests/conftest.py`` offers.
"""

from __future__ import annotations

from reachy.discover import sweep as sweep_mod
from reachy.discover.sweep import Interface, enumerate_hosts, sweep


class TestNoLiveLanSweepGuard:
    def test_the_real_entry_point_returns_nothing_under_the_guard(self):
        """``read_interfaces`` is the module's own name for its real-NIC seam.

        Calling it directly, with nothing injected, must yield the same empty
        tuple an unavailable platform already produces — never this box's
        actual interface table (which, unguarded, carries a real /24).
        """
        assert sweep_mod.read_interfaces() == ()

    def test_enumerate_hosts_default_source_is_also_neutralised(self):
        """``enumerate_hosts()`` with no explicit ``source`` resolves the same
        module-level seam at call time, so it inherits the guard too."""
        assert enumerate_hosts() == ()

    def test_a_sweep_with_nothing_injected_probes_zero_real_hosts(self):
        """The acceptance case: a test that forgets to fake the interface
        source must not reach a single real host.

        A spy ``probe_fn`` that raises if ever called proves no probe is
        issued at all — not merely that none happened to answer. Zero
        candidate hosts is the loud, assertable signal: ``hosts_total == 0``
        fails any assertion a real sweep of a populated LAN would satisfy,
        rather than quietly returning whatever the live network answered.
        """
        calls: list[str] = []

        def _spy_probe(host, port, timeout):
            calls.append(host)
            raise AssertionError("a guarded sweep must never probe a real host")

        result = sweep(probe_fn=_spy_probe)

        assert result.units == ()
        assert result.hosts_total == 0
        assert result.hosts_probed == 0
        assert result.deadline_reached is False
        assert calls == []

    def test_opt_out_matches_the_sibling_guards_precedent(self, monkeypatch):
        """A test that wants real enumeration re-patches the same module
        attribute with its own function-scoped ``monkeypatch``, which wins
        over the autouse guard for the duration of the test — exactly what
        ``tests/test_discover_sweep.py``'s 49 tests already rely on, and the
        same pattern every guard beside this one in ``conftest.py`` supports.
        """
        fake = (Interface(name="eth0", address="203.0.113.5", prefixlen=24),)
        monkeypatch.setattr(sweep_mod, "read_interfaces", lambda: fake)

        assert sweep_mod.read_interfaces() == fake
        assert enumerate_hosts() != ()
