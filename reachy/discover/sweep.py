"""Bounded concurrent LAN sweep — every candidate host, once, under one deadline.

:func:`probe` answers "is there a Reachy daemon at *this* address?". This
module answers "which addresses are worth asking?" and then asks all of them at
once, under a hard worker cap and a single overall deadline.

THE HAZARD THIS MODULE EXISTS TO MAKE IMPOSSIBLE
------------------------------------------------

The development box this feature was specified on carries **seven Docker bridge
networks on 172.x /16** (``docker0`` plus six ``br-*``), a Tailscale ``/32``,
loopback, and **two NICs on the same /24**. Its real ``ip -4 addr`` table is
reproduced verbatim in ``tests/test_discover_sweep.py``. Naively expanding
those seven ``/16``s is **~459 000 hosts** — a sweep that never finishes and a
CLI that appears to hang forever.

So a prefix wider than ``/24`` is rejected **by construction**, in
:func:`sweepable_networks`, before a single host is materialised — never by a
downstream cap that merely truncates the damage. Four independent filters run
there, and each one alone would be insufficient:

* **Width.** ``prefixlen < MIN_PREFIX_LEN`` (24) is refused outright: too many
  hosts to ask. ``prefixlen > MAX_PREFIX_LEN`` (30) is refused too, for the
  opposite reason: a ``/31`` is a point-to-point link and a ``/32`` is a host
  route — Tailscale's interface is a ``/32`` — neither describes a LAN with
  other machines on it.
* **Interface name.** Docker networks *can* be created on a ``/24``
  (``--subnet``), so width alone would let one through. ``docker*`` / ``br-*``
  / ``virbr*`` / ``veth*`` / ``lo`` are excluded by name, which is precise.
  Note what is deliberately NOT done: ``172.16.0.0/12`` is *not* blanket-
  excluded by address, because that is ordinary RFC 1918 space and many real
  corporate LANs live there.
* **Address class.** Loopback, link-local, multicast, reserved and unspecified
  networks carry no unit to find. Beyond those, the network must be **private**
  — a positive test, derived from :mod:`ipaddress` rather than a list of
  excluded literals. That one predicate covers both cases a bare exclusion list
  used to name: RFC 6598 shared address space, where Tailscale and carrier NAT
  live and which is the only range that is neither private nor global (the
  ``/32`` rule already covers today's ``tailscale0``; this also covers a future
  tailnet presenting a narrower prefix), and every publicly-routable range,
  which no robot is on and which this tool has no business probing. RFC 1122
  "this network" is rejected by its leading zero octet, since an interface with
  no address assigned reports ``SIOCGIFADDR`` as all-zero.
* **Deduplication.** Two NICs on one subnet (this box: ``192.168.1.157`` and
  ``192.168.1.118``) enumerate that subnet exactly **once**, and a unit that
  answers at two addresses is folded to one record on ``hardware_id``.

WHY IOCTL, AND NOT A NEW DEPENDENCY
-----------------------------------

Interfaces are enumerated with :func:`socket.if_nameindex` plus two
``SIOCGIFADDR`` / ``SIOCGIFNETMASK`` :mod:`fcntl` ioctls per interface — the
same pair ``ip -4 addr`` reads. That choice, over the alternatives:

* **vs. ``netifaces`` / ``psutil``** — a new base runtime dependency, which
  this repo's three-dep rule forbids (see ``CLAUDE.md``, "Hard constraints").
* **vs. shelling out to ``ip -4 -o addr``** — depends on an external binary,
  costs a fork per invocation, and introduces a subprocess that can hang. An
  ioctl cannot hang: it is answered by the kernel from memory.
* **vs. parsing the kernel's ``net/route`` table file** — it lists connected
  *networks* but not the local address, and it omits routes in the ``local``
  table (loopback among them), so the filters below would have nothing to
  reject. Its ``net/fib_trie`` sibling carries the addresses but not the
  owning interface. (Both live under the process filesystem, which
  ``tests/test_procsup.py`` pins as ``reachy/procsup.py``'s exclusive
  territory — one more reason this module reads neither.)

The accepted limitation: an ioctl returns only the **primary** IPv4 address of
each interface, so a secondary alias added with ``ip addr add`` is invisible.
That is benign here — an alias is almost always on a subnet the primary already
covers, and if it is not, the operator can still pass an explicit address.

:func:`read_interfaces` degrades to ``()`` rather than raising when the source
is unavailable (no :mod:`fcntl` on this platform, no permission, no interface),
and it is a **module-level name on purpose**: it is the ONE seam that touches
the real box, so a test — and task t6's autouse ``conftest.py`` guard, which
neutralises it suite-wide — replaces exactly this attribute::

    monkeypatch.setattr(reachy.discover.sweep, "read_interfaces", lambda: ())

Nothing in this module ever raises to its caller.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from reachy.discover.probe import DEFAULT_PORT, UnitRecord, probe

logger = logging.getLogger(__name__)

#: The WIDEST prefix that may be expanded. A ``/24`` is 254 hosts; a ``/23`` is
#: 510 and a ``/16`` is 65 534. This is the constant the 459 000-host hazard in
#: the module docstring turns on — do not lower it without re-deriving the
#: worker cap and the deadline together.
MIN_PREFIX_LEN = 24

#: The NARROWEST prefix that still describes a LAN. ``/31`` is a point-to-point
#: link and ``/32`` a host route (Tailscale's ``tailscale0`` is a ``/32``):
#: neither has other machines on it to find.
MAX_PREFIX_LEN = 30

#: A final, blunt bound on the materialised host list, independent of every
#: filter above. Four ``/24``s already exceed 1000 hosts; anything past this is
#: a misconfiguration, not a LAN worth sweeping.
MAX_HOSTS = 1024

#: Interface-name prefixes that never carry a robot: loopback, Docker's own
#: bridges (``docker0`` and the ``br-<hex>`` ones compose creates), libvirt's,
#: and container veth pairs. Matched case-insensitively as a prefix.
EXCLUDED_INTERFACE_PREFIXES = ("lo", "docker", "br-", "virbr", "veth")

#: Hard cap on concurrent probes. With :data:`DEFAULT_PROBE_TIMEOUT`, a fully
#: blackholing ``/24`` costs ``ceil(254 / 64) * 0.5 s ~= 2 s`` — inside the
#: spec's 5 s cold-discovery target, with :data:`DEFAULT_DEADLINE_S` as the
#: hard stop behind it.
DEFAULT_MAX_WORKERS = 64

#: The sweep's own per-host timeout, deliberately SHORTER than
#: :data:`reachy.discover.probe.DEFAULT_TIMEOUT` (1.0 s) rather than a change to
#: it. The two serve different questions: the registry fast path asks one
#: remembered address and can afford a full second, while the sweep asks 254
#: addresses of which ~253 will never answer, and its worst case is
#: ``ceil(hosts / workers) * timeout``. A LAN round trip is single-digit
#: milliseconds, so 0.5 s is ~50x headroom for a unit that is actually there.
DEFAULT_PROBE_TIMEOUT = 0.5

#: The ONE overall deadline. The spec bounds cold discovery at 10 s hard, and
#: this is that bound: when it expires, pending probes are cancelled and the
#: sweep returns what it has, flagged. It is never the expected path — the
#: worker cap and per-host timeout above should finish a ``/24`` in ~2 s.
DEFAULT_DEADLINE_S = 10.0

_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B


@dataclass(frozen=True)
class Interface:
    """One local IPv4 interface address, in the shape ``ip -4 addr`` reports.

    ``prefixlen`` is the CIDR width (``24`` for ``192.168.1.157/24``). This is
    a pure data row: every judgement about whether it is worth sweeping lives
    in :func:`sweepable_networks`, so an injected fake table exercises the real
    filters.
    """

    name: str
    address: str
    prefixlen: int


@dataclass(frozen=True)
class SweepResult:
    """What one bounded sweep saw.

    ``units`` is deduplicated on ``hardware_id`` and ordered by the host that
    answered first in address order — so a unit reachable at two addresses is
    ONE record, reported at its lowest address. ``deadline_reached`` is the
    honest flag: ``True`` means probes were still outstanding when the overall
    deadline expired, so ``units`` may be incomplete.
    """

    units: tuple[UnitRecord, ...]
    hosts_total: int
    hosts_probed: int
    deadline_reached: bool
    elapsed_s: float


#: The signature :func:`sweep` calls each probe through — ``probe``'s own
#: shape, so the real one drops in with no adapter.
ProbeFn = Callable[[str, int, float], UnitRecord | None]

#: A zero-argument callable yielding the local interface table.
InterfaceSource = Callable[[], Sequence[Interface]]


def _if_nameindex() -> list[tuple[int, str]]:
    """The kernel's interface list — the one syscall seam tests stub."""
    return list(socket.if_nameindex())


def _ioctl_ipv4(sock: socket.socket, name: str, op: int) -> str:
    """Answer one ``SIOCGIF*`` ioctl as a dotted-quad string.

    Raises :class:`OSError` when the interface has no IPv4 address (errno 99,
    ``EADDRNOTAVAIL`` — every ``veth`` on this box) — the caller skips it.
    """
    import fcntl  # local: absent on non-Unix, and this is the only user

    ifreq = struct.pack("16sH14s", name.encode("ascii")[:15], 0, b"\x00" * 14)
    res = fcntl.ioctl(sock.fileno(), op, ifreq)
    return socket.inet_ntoa(res[20:24])


def _ipv4_of(sock: socket.socket, name: str) -> tuple[str, int]:
    """Return ``(address, prefixlen)`` for one interface, or raise ``OSError``."""
    address = _ioctl_ipv4(sock, name, _SIOCGIFADDR)
    netmask = _ioctl_ipv4(sock, name, _SIOCGIFNETMASK)
    return address, _prefixlen_of(netmask)


def _prefixlen_of(netmask: str) -> int:
    """Prefix length for a dotted-quad *netmask*, or ``ValueError`` if malformed.

    Reads the mask as bits and takes the leading run of ones. The idiomatic
    alternative builds a throwaway network from an all-zero address, which
    means "any" but reads as a hardcoded IP to a reviewer (and to SonarCloud);
    this says what a prefix length IS with no address literal involved.

    The contiguity check is not decoration — it is what the discarded form gave
    for free. ``ipaddress`` rejects a non-contiguous mask like ``255.0.255.0``,
    and :func:`read_interfaces` turns that raise into "skip this interface". A
    bare population count would instead accept it and return a prefix length
    that describes no real network, so the mask is required to be ones followed
    by zeros, and the raise is preserved.
    """
    bits = f"{int(ipaddress.IPv4Address(netmask)):032b}"
    prefixlen = bits.find("0")
    if prefixlen < 0:
        return len(bits)
    if "1" in bits[prefixlen:]:
        raise ValueError(f"{netmask} is not a contiguous netmask")
    return prefixlen


def read_interfaces() -> tuple[Interface, ...]:
    """Read the box's real IPv4 interface table. NEVER raises; ``()`` on failure.

    **This is the injection seam.** It is the only function in the package that
    touches the machine's NICs, which is why it is a plain module-level name:
    a test (and task t6's autouse guard) replaces this exact attribute rather
    than reaching inside anything.

    See the module docstring for why ioctls, and for the primary-address-only
    limitation this accepts.
    """
    interfaces: list[Interface] = []
    try:
        names = _if_nameindex()
    except Exception:  # no /proc, no netlink, an unsupported platform
        logger.debug("interface enumeration unavailable", exc_info=True)
        return ()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _index, name in names:
            try:
                address, prefixlen = _ipv4_of(sock, name)
            except Exception:  # no IPv4 on this interface (EADDRNOTAVAIL), or no fcntl
                # The NORMAL outcome for every veth/down interface on the box,
                # so it is a debug line and not a warning — but never a silent
                # pass: a whole-table failure would otherwise look identical.
                logger.debug("interface %s has no IPv4 address", name, exc_info=True)
                continue
            interfaces.append(Interface(name=name, address=address, prefixlen=prefixlen))
    except Exception:
        logger.debug("interface enumeration failed", exc_info=True)
        return tuple(interfaces)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return tuple(interfaces)


def _is_excluded_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.startswith(prefix) for prefix in EXCLUDED_INTERFACE_PREFIXES)


def _network_of(iface: Interface) -> ipaddress.IPv4Network | None:
    """Turn one interface row into the network it should contribute, or ``None``.

    Every rejection reason from the module docstring is applied here, so a
    caller cannot get a too-wide network by going around a later cap.
    """
    if _is_excluded_name(iface.name):
        return None
    if not (MIN_PREFIX_LEN <= int(iface.prefixlen) <= MAX_PREFIX_LEN):
        return None
    try:
        network = ipaddress.ip_network(f"{iface.address}/{iface.prefixlen}", strict=False)
    except ValueError:  # a malformed row is skipped, never fatal
        return None
    if not isinstance(network, ipaddress.IPv4Network):
        return None
    if (
        network.is_loopback
        or network.is_link_local
        or network.is_multicast
        or network.is_reserved
        or network.is_unspecified
        # A LAN unit lives on a PRIVATE address, so require one rather than
        # naming the ranges that are not. This is derived, not hardcoded, and
        # it is strictly stronger than the two literals it replaced: it rejects
        # RFC 6598 shared address space (100.64.0.0/10 — where Tailscale and
        # carrier NAT live, and the only range that is neither private nor
        # global) AND every publicly-routable range, which no robot is on and
        # which this tool has no business probing.
        or not network.is_private
        # RFC 1122 "this network". `is_unspecified` is only true of the /32, so
        # an interface with no address assigned (SIOCGIFADDR yields 0.0.0.0)
        # paired with a real netmask would otherwise expand to 254 meaningless
        # hosts. Tested by leading octet so no address literal is needed.
        or network.network_address.packed[0] == 0
    ):
        return None
    return network


def sweepable_networks(interfaces: Iterable[Interface]) -> tuple[ipaddress.IPv4Network, ...]:
    """The de-duplicated set of subnets worth sweeping, widest-prefix rules applied.

    Two NICs on one subnet collapse to one network here — which is what makes
    "each host exactly once" a property of the enumeration rather than of a
    later set() that would have already paid for expanding it twice.
    """
    seen: dict[ipaddress.IPv4Network, None] = {}
    for iface in interfaces or ():
        try:
            network = _network_of(iface)
        except Exception:  # a hostile row is skipped, never fatal
            logger.debug("skipping unusable interface row %r", iface, exc_info=True)
            continue
        if network is not None:
            seen.setdefault(network, None)
    return tuple(sorted(seen, key=lambda n: (int(n.network_address), n.prefixlen)))


def enumerate_hosts(
    source: InterfaceSource | None = None,
    *,
    max_hosts: int = MAX_HOSTS,
) -> tuple[str, ...]:
    """Every sweepable host address, each exactly once, in ascending address order.

    ``source`` defaults to the module-level :func:`read_interfaces` — resolved
    at CALL time, not bound as a default argument, so patching the module
    attribute actually takes effect (a default argument would capture the
    original function at import).

    Never raises: an unavailable or hostile source degrades to ``()``.
    """
    reader = source if source is not None else read_interfaces
    try:
        interfaces = tuple(reader() or ())
    except Exception:
        logger.debug("interface source failed", exc_info=True)
        return ()
    hosts: dict[str, None] = {}
    for network in sweepable_networks(interfaces):
        for host in network.hosts():
            hosts.setdefault(str(host), None)
            if len(hosts) >= max_hosts:
                logger.debug("host enumeration capped at %d", max_hosts)
                return tuple(sorted(hosts, key=lambda h: int(ipaddress.ip_address(h))))
    return tuple(sorted(hosts, key=lambda h: int(ipaddress.ip_address(h))))


def _probe_one(probe_fn: ProbeFn, host: str, port: int, timeout: float) -> UnitRecord | None:
    """Run one probe on a worker thread, swallowing anything it throws.

    ``probe`` already never raises, but ``probe_fn`` is injectable — and one
    hostile host must never be able to abort a sweep.
    """
    try:
        return probe_fn(host, port, timeout)
    except Exception:
        logger.debug("probe of %s failed", host, exc_info=True)
        return None


def _dedupe_units(found: list[tuple[int, UnitRecord]]) -> tuple[UnitRecord, ...]:
    """One record per ``hardware_id``, keeping its lowest-ordered address.

    A unit reachable over two interfaces (or holding two addresses on one
    subnet) answers twice; ``hardware_id`` is the stable identity, so the
    second sighting is the same robot, not a second one.
    """
    by_id: dict[str, UnitRecord] = {}
    for _order, record in sorted(found, key=lambda pair: pair[0]):
        by_id.setdefault(record.hardware_id, record)
    return tuple(by_id.values())


def sweep(
    hosts: Sequence[str] | None = None,
    *,
    source: InterfaceSource | None = None,
    port: int = DEFAULT_PORT,
    probe_fn: ProbeFn | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    max_workers: int = DEFAULT_MAX_WORKERS,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> SweepResult:
    """Probe every candidate host concurrently, bounded and always terminating.

    ``hosts`` defaults to :func:`enumerate_hosts` over ``source`` (itself
    defaulting to the :func:`read_interfaces` seam). ``probe_fn`` defaults to
    :func:`reachy.discover.probe.probe`, resolved at call time.

    Two bounds, both hard:

    * at most ``max_workers`` probes are in flight at once, whatever the host
      count — so the sweep cannot exhaust file descriptors or thread stacks;
    * the whole sweep returns within ``deadline_s`` of entry. On expiry the
      pending futures are cancelled and the executor is shut down WITHOUT
      waiting (``wait=False``), so a wedged probe delays the caller by nothing.
      In-flight probes finish on their own bounded timeout in the background.

    Never raises.
    """
    started = time.monotonic()
    prober: ProbeFn = probe_fn if probe_fn is not None else probe

    if hosts is None:
        candidates = enumerate_hosts(source=source)
    else:
        candidates = tuple(hosts)

    if not candidates:
        return SweepResult(
            units=(),
            hosts_total=0,
            hosts_probed=0,
            deadline_reached=False,
            elapsed_s=time.monotonic() - started,
        )

    workers = max(1, min(int(max_workers), len(candidates)))
    found: list[tuple[int, UnitRecord]] = []
    probed = 0
    deadline_reached = False

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reachy-sweep")
    try:
        futures = {
            executor.submit(_probe_one, prober, host, port, timeout): order
            for order, host in enumerate(candidates)
        }
        remaining = deadline_s - (time.monotonic() - started)
        try:
            for future in as_completed(futures, timeout=max(remaining, 0.0)):
                probed += 1
                record = future.result()
                if record is not None:
                    found.append((futures[future], record))
        except TimeoutError:
            deadline_reached = True
        except Exception:  # a sweep must never raise to the CLI
            logger.debug("sweep aborted early", exc_info=True)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return SweepResult(
        units=_dedupe_units(found),
        hosts_total=len(candidates),
        hosts_probed=probed,
        deadline_reached=deadline_reached,
        elapsed_s=time.monotonic() - started,
    )
