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
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

#: Every spelling of "this machine" a URL can use — the actuator ban below must
#: not be sidestepped by writing 127.0.0.1 where localhost was meant.
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # nosec B104

#: Loopback + a port nothing listens on: connect-refused is immediate and local
#: (no DNS, no external dependency, no flakiness) even before our guard fires.
_UNREACHABLE = "http://127.0.0.1:1"

#: The shared budget for "wait until a background thread does the thing".
#:
#: Every concurrency test in this suite — the tee, the realtime client and its
#: composition seams, the transcript driver — waits on some worker thread that
#: does its job within a few milliseconds on an idle box. This number is NOT a
#: behavioural expectation about that latency; it only decides how much
#: scheduler starvation is tolerated before the test declares breakage.
#:
#: It lives here, cited by every module in that family, because five private
#: copies is exactly how the value drifts. Each of those modules independently
#: chose 5.0 s, and the clearest case for raising it is
#: tests/test_realtime_client.py, where a test's own 5.0 s reconnect backoff
#: EQUALLED its 5.0 s waiter — so a lost connect race and the deadline landed on
#: the same instant, failing with nothing wrong. A flaky suite costs the merge
#: gate its meaning, and generosity here costs wall-clock only when something is
#: actually broken: a healthy run never reaches the deadline, and a broken one
#: still fails rather than hanging.
#:
#: What this number is NOT is a way to make a racy assertion pass. Two flakes
#: chased during the embodiment-layer build turned out to be genuine
#: cross-thread ordering bugs in the tests themselves (an assertion on a counter
#: another thread bumps after the fact; an assertion on bytes the server had not
#: been given time to receive), and both were fixed by waiting on the thing
#: rather than by widening a budget. Raise the budget for scheduler starvation;
#: fix the test for everything else.
#:
#: A test that needs a DIFFERENT budget (a deliberate long backoff it must
#: outlast, say) should derive it from this — not redefine it.
WAIT_BUDGET_S = 30.0

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
def _no_live_event_broker(monkeypatch: pytest.MonkeyPatch):
    """No test may publish onto a REAL event broker.

    The third sibling of the two guards above, and it earned its place the same
    way they did — by the suite actually doing the damage. The moment
    ``events-cli`` became a base dependency, ``_import_events_client`` started
    resolving for real, so every composition test built a live client against
    ``REACHY_MQTT_URL`` (default ``localhost:1883``) and published to whatever
    was listening. On this box that is the deployed Mosquitto: a full ``pytest``
    run wrote the suite's synthetic ticks onto the robot's own
    ``reachy/events/**`` tree and left RETAINED ``reachy/state/*`` values behind,
    which outlive the test process by design and are what a real consumer reads
    on connect. A late subscriber would have been served test fixtures as the
    robot's current pose.

    So the broker address is pointed at the same guaranteed-unreachable loopback
    the realtime guard uses. Composition still builds the client and still calls
    ``connect()`` — the code under test is untouched — and the connection is
    simply refused immediately and locally, which is the NORMAL "broker not up
    yet" outcome the publisher already reports as one named
    ``broker-unreachable`` drop. paho's network loop runs on a daemon thread, so
    a test that leaves a client open cannot hang interpreter exit.

    Tests that DO want a specific broker re-set this same variable with their own
    function-scoped ``monkeypatch`` (which wins and is undone at teardown), or
    inject a fake client outright (``tests/fake_events_client.py`` — still the
    right tool for asserting what got published).
    """
    monkeypatch.setenv("REACHY_MQTT_URL", "127.0.0.1:1")
    yield


@pytest.fixture(autouse=True)
def _no_live_voice_out(monkeypatch: pytest.MonkeyPatch):
    """No test may synthesize speech on, or play sound through, the REAL robot.

    The fourth sibling of the guards above, and it earned its place the same way
    every one of them did — by the suite actually doing the damage. While
    building the embodiment layer's composition root, a boundary probe dispatched
    the ``speak`` tool on the *robot* media profile with the real voice engine
    resolved. It POSTed to model-gear's TTS and pushed the resulting audio to the
    daemon's play route: the deployed robot in the next room said it out loud,
    once, before anyone noticed.

    Two layers, because an env var alone cannot cover this:

    * **Env pins.** TTS reads ``REACHY_TTS_URL`` at call time, so pointing it at
      the dead loopback covers every caller that respects it. Worth stating
      plainly: the DEFAULT is ``http://localhost:9000``, a live model-gear
      container on this box — so "the developer exported nothing" is not the
      safe state, it is the dangerous one.
    * **A port ban at the socket's door.** Daemon playback cannot be fixed by an
      env var: ``play_audio(base_url=DEFAULT_BASE_URL)`` binds that literal
      ``http://localhost:8000`` as a DEFAULT ARGUMENT at import time, so
      patching the module constant is already too late.

    The ban is deliberately drawn at **side effects, not at HTTP**. It refuses
    the two endpoints that act on the physical world — TTS (:9000) makes sound,
    the daemon (:8000) drives hardware — and lets everything else through. In
    particular the gateway (:8001) stays reachable, because reading from it is
    inert and the opt-in live integration tests that hit it are the only thing
    that has ever caught a served-model drift (issue #132). Banning all HTTP
    would turn those into permanent skips and quietly delete that signal.

    It refuses with ``URLError`` — an ``OSError`` subclass, exactly what a
    refused connection raises — so every caller's existing degradation path runs
    unchanged, including ``_gateway_or_skip``-style probes that skip on a
    transport failure. A test asserting "a dead speaker is a named drop" still
    sees a dead speaker; it simply no longer needs a live one to prove it.

    Patching ``urlopen`` (rather than a helper further in) is what keeps the
    escape hatch working: the several tests that legitimately exercise the http
    playback leg install their OWN ``urllib.request.urlopen`` fake in the test
    body, which replaces this wrapper outright and is undone at teardown.
    """
    real_urlopen = urllib.request.urlopen
    banned = {8000, 9000}

    def _guarded_urlopen(req, *args, **kwargs):  # urlopen's own shape
        url = getattr(req, "full_url", req)
        try:
            parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
            port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
        except ValueError:  # a malformed URL is not our business to police
            parts, port = None, None
        if parts is not None and parts.hostname in _LOOPBACK_NAMES and port in banned:
            raise urllib.error.URLError(
                f"tests never reach the robot's actuators (refused {parts.hostname}:{port}) — "
                "inject a fake or patch urllib.request.urlopen to exercise this leg"
            )
        return real_urlopen(req, *args, **kwargs)

    monkeypatch.setenv("REACHY_TTS_URL", _UNREACHABLE)
    monkeypatch.setenv("REACHY_BASE_URL", _UNREACHABLE)
    monkeypatch.setattr(urllib.request, "urlopen", _guarded_urlopen)
    yield


@pytest.fixture(autouse=True)
def _no_live_audio_tee_socket(monkeypatch: pytest.MonkeyPatch, tmp_path_factory):
    """No test may bind — or unlink — the REAL runtime's audio-tee socket.

    The fourth sibling of the three guards above, filed before it could do the
    damage they each did. ``behavior engine run`` composes
    :class:`~reachy.behavior.audio_tee.AudioTee` unconditionally, and its path
    defaults to ``state_dir()/audio_tee.sock`` — which on this box is the socket
    the DEPLOYED runtime is serving to its embodiment layer. A composition test
    that does not set ``REACHY_STATE_DIR`` would bind there, and a tee that owns
    the path removes it on ``close()``: the suite would quietly cut a live
    consumer's only way back in.

    (The tee already refuses a path a live listener answers on — that guard is
    real and tested — but it degrades QUIETLY, which is exactly why it must not
    be the only thing standing between the suite and the robot.)

    So every test gets the socket pointed at its xdist worker's own temp dir
    (``tmp_path_factory`` is per-worker, so parallel workers never collide).
    Tests that care about the path re-set this same variable with their own
    function-scoped ``monkeypatch``, which wins and is undone at teardown.
    """
    from reachy.behavior.audio_tee import SOCKET_ENV

    monkeypatch.setenv(SOCKET_ENV, str(tmp_path_factory.getbasetemp() / "audio_tee.sock"))
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
