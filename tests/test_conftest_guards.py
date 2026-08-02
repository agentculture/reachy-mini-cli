"""The suite's own safety rail: what conftest's autouse guards actually guard.

Every one of these fixtures exists because the suite once did the damage it now
prevents — acquiring the real robot's mic, opening a hearing session against the
live gateway, publishing retained state onto the deployed robot's MQTT tree, and
most recently speaking out loud through the robot's own speaker. Each was added
after the fact, and until now **none of them had a test**.

That is a real gap and not a pedantic one. A guard is invisible when it works:
nothing fails, nothing is logged, and the only signal that it has stopped
working is a physical side effect in another room. A guard that silently stops
guarding is exactly the invisible-failure class this repo keeps paying for, so
the rails get the same treatment as the code they protect.

These tests assert the guards are ACTIVE in the ambient environment every other
test runs in — they take no fixture and patch nothing, because the thing under
test is the default state of an arbitrary test.
"""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request

import pytest

from tests.conftest import _LOOPBACK_NAMES

#: The message the actuator ban raises. Matched as a substring so the test fails
#: loudly if the guard is removed, rather than passing on a different URLError
#: that happens to arrive for an unrelated reason.
_BAN_MARKER = "tests never reach the robot's actuators"


def _closed_port() -> int:
    """An ephemeral port reserved then released — connects are refused locally."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


# --------------------------------------------------------------------------- #
# The endpoint pins                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["REACHY_TTS_URL", "REACHY_BASE_URL", "REACHY_MQTT_URL", "REACHY_REALTIME_URL"],
)
def test_every_service_endpoint_is_pinned_somewhere_unreachable(name: str) -> None:
    """No test inherits the developer's real endpoints.

    All four resolve to a live service on the box this was written on, and three
    of them have a DEFAULT that is also live — so "unset" is not a safe state.
    """
    value = os.environ.get(name)
    assert value, f"{name} is not pinned at all — a test would reach the real service"
    assert "127.0.0.1:1" in value, f"{name}={value!r} does not point at the dead loopback"


# --------------------------------------------------------------------------- #
# The actuator ban                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("port", [8000, 9000])
@pytest.mark.parametrize("host", sorted(_LOOPBACK_NAMES))
def test_the_actuator_ports_are_refused_by_every_spelling_of_this_machine(
    host: str, port: int
) -> None:
    """:8000 drives hardware and :9000 makes sound — both are refused.

    Parametrized over every loopback spelling because a ban that only knows the
    word ``localhost`` is sidestepped by writing ``127.0.0.1``, which is exactly
    the kind of gap that reads as "guarded" right up until it isn't.

    IPv6 literals are bracketed, which is not cosmetic: ``http://::1:8000/`` is
    a MALFORMED url whose port cannot be parsed, so it reaches no actuator and
    is deliberately none of the guard's business, while ``http://[::1]:8000/``
    is the real thing and must be refused.
    """
    literal = f"[{host}]" if ":" in host else host
    with pytest.raises(urllib.error.URLError) as excinfo:
        urllib.request.urlopen(f"http://{literal}:{port}/media/play_sound", timeout=1)  # nosec B310
    assert _BAN_MARKER in str(excinfo.value)


def test_a_non_actuator_port_is_passed_through_rather_than_banned() -> None:
    """The ban is drawn at side effects, not at HTTP.

    Reading from the gateway is inert, and the opt-in live integration tests
    that hit it are the only thing that has ever caught a served-model drift
    (issue #132) — banning all HTTP would turn those into permanent skips and
    delete that signal. Proven against a CLOSED ephemeral port: the call must
    still reach the socket layer and fail there, with the ban's message absent.
    """
    port = _closed_port()
    with pytest.raises(urllib.error.URLError) as excinfo:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=1)  # nosec B310
    assert _BAN_MARKER not in str(excinfo.value), "a non-actuator port must not be banned"


def test_a_test_may_still_install_its_own_urlopen_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented escape hatch, pinned.

    Several tests legitimately exercise the http playback leg by installing
    their own ``urllib.request.urlopen`` in the test body. That must keep
    winning over the guard — otherwise this fixture would not be a safety rail,
    it would be a wall, and the fix for it would be to weaken the rail.
    """
    calls: list[str] = []

    def _fake(req, *_args, **_kwargs):  # noqa: ANN001 — urlopen's own shape
        calls.append(getattr(req, "full_url", req))
        raise AssertionError("reached the fake, which is the point")

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    with pytest.raises(AssertionError, match="reached the fake"):
        urllib.request.urlopen("http://localhost:8000/media/play_sound")  # nosec B310
    assert calls == ["http://localhost:8000/media/play_sound"]


# --------------------------------------------------------------------------- #
# The media gate                                                               #
# --------------------------------------------------------------------------- #


def test_no_test_can_acquire_the_real_robots_media_subsystem() -> None:
    """The two seams ``HeldMediaClient``'s readiness gate probes are stubbed.

    Without this, every test that constructs a holder would acquire the physical
    mic and camera, and one that crashed before ``close()`` would leave them
    held — on the robot, not in a fixture.
    """
    from reachy.robot import media_client

    assert media_client._get_json("http://localhost:8000/api/media/status", 1.0) is None
    assert media_client._post_ok("http://localhost:8000/api/media/acquire", 1.0) is False
