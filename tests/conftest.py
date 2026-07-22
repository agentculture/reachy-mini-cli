"""Shared pytest fixtures.

Today this file exists for exactly one thing: the ``offline`` marker's guard.
``offline`` is registered in ``pyproject.toml``'s ``[tool.pytest.ini_options]``
(the project's existing home for pytest config); this module supplies the
behavior behind it.

A test decorated ``@pytest.mark.offline`` gets two things, and ONLY while that
marker is present on the test:

1. Every external service env var the CLI can read (``REACHY_OPENAI_URL_BASE``,
   the legacy ``REACHY_LLM_BASE_URL`` fallback, ``REACHY_TTS_URL``,
   ``REACHY_STT_URL``, ``FORGE_BASE_URL``) is pointed at a guaranteed-unreachable
   loopback address.
2. Real network connects are blocked outright: ``socket.socket.connect`` and
   ``socket.create_connection`` are monkeypatched to raise ``AssertionError`` —
   so a stray network call inside an ``offline``-marked test is a loud,
   immediate test failure, never a silent pass or a slow hang/timeout.

The guard fixture is ``autouse`` at collection time but a no-op for every test
that is NOT marked ``offline`` (see the ``get_closest_marker`` check below) — it
must never leak into the other 1000+ tests in the suite.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

#: Loopback + a port nothing listens on: connect-refused is immediate and local
#: (no DNS, no external dependency, no flakiness) even before our guard fires.
_UNREACHABLE = "http://127.0.0.1:1"

#: Every external HTTP leg the CLI can reach out over, by env var name — the
#: canonical LLM/TTS/STT/forge endpoints plus the legacy REACHY_LLM_* alias
#: reachy.speech.llm.py still honours as a fallback. REACHY_VISION_MODEL_ID is
#: deliberately excluded: it names a model, not an endpoint (it rides
#: REACHY_OPENAI_URL_BASE, already covered).
_SERVICE_ENV_VARS = (
    "REACHY_OPENAI_URL_BASE",
    "REACHY_LLM_BASE_URL",
    "REACHY_TTS_URL",
    "REACHY_STT_URL",
    "FORGE_BASE_URL",
)


def _deny_network(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("offline lane: network call attempted")


@pytest.fixture(autouse=True)
def _offline_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Point every service env var offline + block real sockets — ``offline`` tests only.

    Scoped via ``request.node.get_closest_marker("offline")``: for any test that
    does not carry ``@pytest.mark.offline`` this fixture does nothing at all, so
    the rest of the suite is byte-for-byte unaffected.
    """
    if request.node.get_closest_marker("offline") is None:
        yield
        return

    for name in _SERVICE_ENV_VARS:
        monkeypatch.setenv(name, _UNREACHABLE)

    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket, "create_connection", _deny_network)

    yield


@pytest.fixture(autouse=True)
def _no_live_daemon_media_gate(monkeypatch: pytest.MonkeyPatch):
    """No test may acquire or release the REAL robot's media subsystem.

    ``HeldMediaClient`` gained a daemon readiness gate (t30): before constructing
    its SDK client it probes ``GET /api/media/status`` and, when media is
    released, takes it with ``POST /api/media/acquire`` — releasing it again on
    ``close()``. That gate defaults to ``http://localhost:8000``, which on a
    developer box or the robot itself is a LIVE daemon. Without this fixture,
    running the suite would mutate real hardware state: every test that
    constructs a holder would acquire the physical mic/camera, and a test that
    crashed before ``close()`` would leave them held.

    So the two HTTP seams are stubbed suite-wide to "daemon unreachable", which
    the gate treats as absence of information and falls open on — i.e. every test
    that does not care about the gate sees exactly the pre-t30 behavior.

    Tests that DO exercise the gate re-patch these same two names with their own
    fake daemon (``tests/test_robot_media_client.py``); a function-scoped
    ``monkeypatch.setattr`` in the test body simply wins over this one, and both
    are undone at teardown. The few tests that need the *real* helpers hold a
    module-level reference captured at import time.
    """
    from reachy.robot import media_client as _media_client

    monkeypatch.setattr(_media_client, "_get_json", lambda _url, _timeout: None)
    monkeypatch.setattr(_media_client, "_post_ok", lambda _url, _timeout: False)
    yield


@pytest.fixture(autouse=True)
def _no_live_realtime_gateway(monkeypatch: pytest.MonkeyPatch):
    """No test may open a hearing session against a REAL lobes gateway.

    Since the realtime arc (#115), ``behavior engine run``'s composition builds
    and ``start()``s ONE :class:`~reachy.speech.realtime.RealtimeTranscriber` —
    unconditionally, and before the first tick. Its endpoint resolves from
    ``REACHY_REALTIME_URL``, then ``REACHY_OPENAI_URL_BASE``, then
    ``ws://localhost:8001/v1/realtime``. On a developer box BOTH fallbacks name a
    live service (the machine that motivated this fixture has
    ``REACHY_OPENAI_URL_BASE`` exported *and* something listening on :8001), so
    without this every composition test would open a real WebSocket session
    against it — a hidden network dependency and a source of machine-dependent
    flakiness, on top of the ``offline`` lane's own guard which only covers
    ``offline``-marked tests.

    So the explicit override is pointed at the same guaranteed-unreachable
    loopback the ``offline`` guard uses. Composition still builds and starts the
    client (the code under test is untouched); the connect is simply refused
    immediately and locally — the same NORMAL "gateway not up yet" outcome a
    booting robot sees, latched to one ``session-down`` line.

    Tests that DO want a real session either construct the client with an
    explicit ``url=``, which wins over any env (``tests/test_realtime_client.py``,
    ``tests/test_behavior_transcript_realtime.py``), or re-set this same variable
    with their own function-scoped ``monkeypatch``, which likewise wins and is
    undone at teardown.
    """
    monkeypatch.setenv("REACHY_REALTIME_URL", "ws://127.0.0.1:1/v1/realtime")
    yield


@pytest.fixture(autouse=True)
def _isolate_reachy_logging():
    """Never let one test's installed log handler outlive it.

    ``reachy.cli._logging.install_logging`` attaches ONE handler to the
    ``"reachy"`` logger and keeps it for the process lifetime — deliberately, so
    repeat calls in a long-running loop cannot duplicate log lines. That design
    is right for production and wrong for a test process, where it makes the
    handler shared mutable state: any test that installs it leaves every later
    test in the same worker logging through it.

    The failure that motivated this fixture: a test asserting ``err == ""``
    began failing once an unrelated code path started emitting a ``[SENSE]``
    line, because a *different* test earlier in the same ``pytest -n auto``
    worker had installed the handler. Which tests share a worker varies per run,
    so it presented as an order-dependent flake (6 of 8 runs) rather than a
    reproducible failure — and the emitting code was blameless.

    Since the #96 fix, ``install_logging`` also sets ``propagate = False`` on
    the ``"reachy"`` logger (so a foreign root handler can never double our
    lines). In a test worker that same flag would starve every LATER test's
    ``caplog`` — pytest's capture handler lives on the ROOT logger — turning
    one ``main([... run ...])`` call into silent capture failures elsewhere in
    the worker. So the snapshot covers ``propagate`` too.

    Snapshot the handlers, the level, and the propagate flag; restore all
    three afterwards. Cheap, and it makes "does this code log?" a local
    question again.
    """
    import logging as _logging

    logger = _logging.getLogger("reachy")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    try:
        yield
    finally:
        logger.handlers[:] = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
