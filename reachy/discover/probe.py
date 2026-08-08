"""Identity probe for a Reachy Mini daemon reachable over HTTP.

A single, cheap, read-only ``GET /api/daemon/status`` — the exact route
:meth:`reachy.robot.http_transport.HttpTransport.daemon_status` speaks — used
here as a **presence + identity check**, not as a transport. :func:`probe`
never arms motors and never opens a media session: it issues that one GET and
nothing else, which is what makes it safe to run against an arbitrary address
on the LAN (the sweep in a later task calls it once per candidate host).

:func:`probe` NEVER raises to the caller. Every failure — a refused
connection, a DNS failure, a timeout, a non-200 response, a body that is not
valid JSON, or valid JSON that is not a Reachy daemon status payload — degrades
to ``None``, mirroring the never-raise ethos of
:class:`reachy.speech.stt.Transcriber`.

:class:`UnitRecord` is deliberately a **pure probe result**: exactly what one
GET answered, nothing remembered across calls. A later registry layer
(``reachy/discover/registry.py``) adds ``mac`` / ``last_ip`` / ``last_seen`` on
top of a probed record without needing to change this dataclass.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

#: The daemon's own default REST port — matches
#: ``reachy.robot.transport.DEFAULT_BASE_URL``'s ``http://localhost:8000``.
DEFAULT_PORT = 8000

#: Short and bounded on purpose: a later sweep probes many hosts, most of which
#: will never answer, so a single probe must fail fast rather than stall a scan.
DEFAULT_TIMEOUT = 1.0

#: The one route this module ever calls — the same route
#: :meth:`reachy.robot.http_transport.HttpTransport.daemon_status` speaks.
STATUS_PATH = "/api/daemon/status"

#: The daemon is a LAN / loopback control-plane service with no TLS listener —
#: this is the protocol, not a choice this module makes (SonarCloud
#: python:S5332). ``reachy.robot.transport.DEFAULT_BASE_URL`` and
#: ``reachy.robot.http_transport`` name the same scheme for the same reason;
#: it is re-declared here rather than imported because
#: ``tests/test_discover_boundary.py`` pins ``reachy/discover/``'s first-party
#: ``reachy.*`` edges by equality, and reaching into ``reachy.robot`` would add
#: one for a single string constant.
HTTP_SCHEME = "http"


@dataclass(frozen=True)
class UnitRecord:
    """One Reachy Mini daemon's identity, as answered by a single probe.

    ``model`` is *derived*, not carried by the daemon: the
    ``/api/daemon/status`` payload has no ``model`` field, only a
    ``wireless_version`` bool (see :func:`_model_for`), so a Wireless unit
    reports ``model="Reachy Mini Wireless"`` and a Lite unit
    ``model="Reachy Mini Lite"``. ``address`` is the host actually probed —
    it may differ from ``wlan_ip`` (the daemon's own view of its wireless
    interface) when reached over a different interface.
    """

    hardware_id: str
    robot_name: str
    model: str
    wireless: bool
    version: str
    wlan_ip: str | None
    address: str


def _url_host(host: str) -> str:
    """Return *host* in the form a URL authority needs — IPv6 literals BRACKETED.

    ``f"http://{host}:{port}"`` is only a parseable URL when an IPv6 literal is
    wrapped in brackets (RFC 3986 §3.2.2): without them
    ``http://2a0d:6fc2::756b:8000`` has a port indistinguishable from another
    hextet, and :func:`urllib.parse.urlsplit` refuses to cast it. This lives
    HERE, in :func:`probe`'s one URL-formatting site, rather than at each call
    site, because every path into the probe crosses it — the
    :mod:`reachy.discover.resolve` fast path (which passes a registry
    ``last_ip`` back in), the :mod:`reachy.discover.sweep` fan-out, and the
    CLI's own ``--address`` — so a future caller cannot reintroduce the bug by
    forgetting to bracket. The bare form is deliberately what the registry
    stores (``reachy/cli/_commands/wireless.py`` reports the unbracketed
    literal so ``wlan_ip``-style values stay clean), which is exactly why the
    fast path used to hand this function an unbracketed literal.

    Detection is :func:`ipaddress.ip_address`, never a scan for colons: a bare
    ``::1`` has colons and a hostname does not, but a bracketed literal must be
    unwrapped before the question can even be asked. Idempotent by
    construction — an already-bracketed ``[::1]`` is unwrapped, recognised and
    re-bracketed, never doubled.

    Anything :mod:`ipaddress` does not recognise (a hostname; a scoped
    link-local like ``fe80::1%eth0``, which needs percent-encoding this
    version does not attempt) is returned untouched: this function's job is to
    make a literal usable, not to validate — ``probe`` degrades every bad host
    to ``None`` anyway.
    """
    text = host.strip()
    inner = text[1:-1] if text.startswith("[") and text.endswith("]") else text
    try:
        parsed = ipaddress.ip_address(inner)
    except ValueError:
        return host
    return f"[{inner}]" if parsed.version == 6 else inner


def _model_for(wireless: bool) -> str:
    """Derive the friendly model name from the daemon's ``wireless_version`` flag.

    The ``/api/daemon/status`` payload never carries a ``model`` field (see the
    live fixture recorded in ``tests/test_discover_probe.py``) — only
    ``wireless_version`` — so this is the one place that name gets decided:
    ``True`` -> "Reachy Mini Wireless", ``False`` -> "Reachy Mini Lite".
    """
    return "Reachy Mini Wireless" if wireless else "Reachy Mini Lite"


def _parse_status(payload: object, address: str) -> UnitRecord | None:
    """Validate + shape a decoded JSON body into a :class:`UnitRecord`, or ``None``.

    Fail-closed: any missing or wrong-typed required field means this body is
    not a Reachy daemon status payload, so the probe reports "nothing here"
    rather than a partially-populated record.
    """
    if not isinstance(payload, dict):
        return None
    hardware_id = payload.get("hardware_id")
    robot_name = payload.get("robot_name")
    version = payload.get("version")
    wireless_version = payload.get("wireless_version")
    if not isinstance(hardware_id, str) or not hardware_id:
        return None
    if not isinstance(robot_name, str) or not robot_name:
        return None
    if not isinstance(version, str) or not version:
        return None
    # bool check must come before any int/str coercion: bool is an int subclass
    # in Python, so isinstance(x, int) would silently accept 0/1.
    if not isinstance(wireless_version, bool):
        return None
    wlan_ip = payload.get("wlan_ip")
    if not isinstance(wlan_ip, str):
        wlan_ip = None
    return UnitRecord(
        hardware_id=hardware_id,
        robot_name=robot_name,
        model=_model_for(wireless_version),
        wireless=wireless_version,
        version=version,
        wlan_ip=wlan_ip,
        address=address,
    )


def _fetch_status(url: str, timeout: float) -> bytes | None:
    """Issue the ONE probe GET; return the body, or ``None`` on a non-200.

    Raises on any transport failure (refused connection, DNS failure, timeout,
    an HTTP error) — :func:`probe` is the only caller, and it catches broadly.
    Kept as a raising seam (rather than swallowing here) so a test can assert
    on the exact exception path if it ever needs to.
    """
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - fixed http(s) URL
        status = getattr(resp, "status", None) or resp.getcode()
        if int(status) != 200:
            return None
        return resp.read()


def probe(
    host: str, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT
) -> UnitRecord | None:
    """GET ``http://<host>:<port>/api/daemon/status`` and parse it into a UnitRecord.

    Stdlib-only (:mod:`urllib` + :mod:`ipaddress`). Issues exactly ONE GET
    request — no follow-up call, no other route — so it is safe to run against
    an arbitrary address on the LAN: it neither arms motors nor opens a media
    session.

    *host* may be a hostname, an IPv4 literal, or an IPv6 literal in EITHER
    form — bare (``::1``, which is what the registry's ``last_ip`` holds) or
    already bracketed (``[::1]``). :func:`_url_host` normalises it, so no
    caller has to know that ``http://<ipv6>:<port>`` needs brackets.
    ``UnitRecord.address`` still reports *host* exactly as it was passed in,
    so a bare literal round-trips through the registry unchanged.

    Every failure degrades to ``None`` and is NEVER raised to the caller:

    * a refused connection, DNS failure, or timeout (``OSError`` and its
      ``urllib.error.URLError`` subclass, which also covers
      ``urllib.error.HTTPError``);
    * a body that is not valid JSON;
    * valid JSON that is not a Reachy daemon status payload (a missing or
      wrong-typed ``hardware_id`` / ``robot_name`` / ``version`` /
      ``wireless_version``);
    * anything else unanticipated — the broad ``except Exception`` below is
      deliberate: a probe run against an arbitrary LAN host must never be the
      thing that crashes a sweep.
    """
    url = f"{HTTP_SCHEME}://{_url_host(host)}:{port}{STATUS_PATH}"
    try:
        raw = _fetch_status(url, timeout)
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:  # json.JSONDecodeError / UnicodeDecodeError both subclass this
        return None
    return _parse_status(payload, host)
