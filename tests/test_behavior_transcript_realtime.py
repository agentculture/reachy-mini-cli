"""The transcript sense over the REAL realtime session (task t4).

Two things this file proves that neither ``tests/test_realtime_client.py`` (the
wire, with no driver) nor ``tests/test_behavior_transcript_sense.py`` (the
driver, with a fake session) can prove alone:

1. **Structurally, the tick thread never touches a socket.** Asserted over the
   AST of BOTH halves, the way this repo already asserts its boundaries
   (``tests/test_zero_llm_boundary.py``): the call closure reachable from
   :meth:`~reachy.behavior.transcript_sense.TranscriptSenseDriver.__call__`
   contains no socket/select/TLS name and reaches the session client through
   nothing but its two O(1), non-blocking methods; and inside
   :mod:`reachy.speech.realtime`, those two methods' own closure is socket-free
   while the worker's (``_run``) is not — a vacuity guard, so the first half
   cannot pass by scanning nothing.

   This matters because the engine holds a **20 ms budget at 50 Hz**, and the
   deployed box has already paid for one blocking call on that thread: a
   measured 425-1213 ms startup overrun, 21x-61x over budget
   (``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 3).
   A structural test is the only kind that survives a refactor which "just
   inlines one small helper".

2. **End to end, words spoken into the wire reach the sense snapshot.** The
   driver is composed against a real
   :class:`~reachy.speech.realtime.RealtimeTranscriber` over a loopback socket
   to :class:`tests.fake_realtime_server.FakeRealtimeServer`, so the whole path
   runs: mic chunk -> ``input_audio_buffer.append`` -> server VAD ->
   ``conversation.item.input_audio_transcription.completed`` -> engagement gate
   -> the one-tick latch. Offline, deterministic, no fleet, no new dependency.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from reachy.behavior.sense import Sense, SenseProviders, read_perception
from reachy.behavior.transcript_sense import TranscriptSenseDriver, TranscriptTuning
from reachy.speech.realtime import (
    OPENAI_API_KEY_ENV,
    OPENAI_URL_BASE_ENV,
    REALTIME_API_KEY_ENV,
    REALTIME_URL_ENV,
    RealtimeTranscriber,
)
from tests.fake_realtime_server import FakeRealtimeServer, Scenario

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DRIVER_MODULE = _REPO_ROOT / "reachy" / "behavior" / "transcript_sense.py"
_CLIENT_MODULE = _REPO_ROOT / "reachy" / "speech" / "realtime.py"

RATE = 16000
CHUNK = 160
DT = 0.01
T0 = 100.0
NAMED = "reachy can you look at me"

#: Names that only ever appear where a socket is being used. Any of them inside
#: the tick-thread closure means the 20 ms budget is one syscall from a stall.
_SOCKET_NAMES = frozenset(
    {
        "socket",
        "ssl",
        "select",
        "sendall",
        "recv",
        "recv_exact",
        "create_connection",
        "wrap_socket",
        "read_until",
        "build_frame",
        "read_frame",
        "build_handshake_request",
        "parse_response_head",
    }
)

#: The session client's whole tick-thread surface (everything the driver may
#: call), plus the lifecycle calls composition makes off-tick.
_CLIENT_TICK_SURFACE = ("submit_audio", "take_utterance", "set_sample_rate")


@pytest.fixture(autouse=True)
def _clean_realtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here may inherit a developer's realtime/gateway configuration."""
    for name in (REALTIME_URL_ENV, REALTIME_API_KEY_ENV, OPENAI_URL_BASE_ENV, OPENAI_API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# AST helpers — a call closure over one module                                #
# --------------------------------------------------------------------------- #


def _functions(path: Path) -> dict[str, ast.FunctionDef]:
    """Every ``def`` in *path* by bare name (methods included, one flat map).

    A flat map is enough here because both modules have one class each and no
    name collisions; a collision would only ever make the closure LARGER, which
    is the safe direction for a ban.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node: ast.AST) -> set[str]:
    """Bare names this function calls: ``self.foo()``, ``foo()``, ``Class.foo()``."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def _closure(defs: dict[str, ast.FunctionDef], roots: tuple[str, ...]) -> set[str]:
    """Every function in *defs* reachable from *roots* by intra-module calls."""
    seen: set[str] = set()
    pending = [name for name in roots if name in defs]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for name in _called_names(defs[current]):
            if name in defs and name not in seen:
                pending.append(name)
    return seen


def _identifiers(defs: dict[str, ast.FunctionDef], names: set[str]) -> set[str]:
    """Every identifier (name + attribute) mentioned anywhere in *names*' bodies."""
    found: set[str] = set()
    for name in names:
        for node in ast.walk(defs[name]):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
    return found


def _is_session(node: ast.AST) -> bool:
    """True for the expression ``self._realtime``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "_realtime"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _session_surface(defs: dict[str, ast.FunctionDef], names: set[str]) -> set[str]:
    """Every member of the injected session client the given functions reach.

    Three shapes count, because all three are used in this module and any of
    them could smuggle in a blocking call:

    * ``self._realtime.foo(...)`` — the direct attribute;
    * ``client = self._realtime`` then ``client.foo(...)`` — the local alias;
    * ``getattr(self._realtime, "foo", None)`` — the defensive duck-type probe.
    """
    found: set[str] = set()
    for name in names:
        body = defs[name]
        aliases = {
            target.id
            for node in ast.walk(body)
            if isinstance(node, ast.Assign) and _is_session(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(body):
            if isinstance(node, ast.Attribute):
                if _is_session(node.value):
                    found.add(node.attr)
                elif isinstance(node.value, ast.Name) and node.value.id in aliases:
                    found.add(node.attr)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and node.args
                and _is_session(node.args[0])
            ):
                found.update(
                    arg.value
                    for arg in node.args[1:]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                )
    return found


# --------------------------------------------------------------------------- #
# 1 — the tick thread never touches a socket                                  #
# --------------------------------------------------------------------------- #


def test_the_transcript_tick_closure_contains_no_socket_call() -> None:
    """Criterion: WS I/O is unreachable from the driver's tick entry point."""
    defs = _functions(_DRIVER_MODULE)
    assert "__call__" in defs, "the tick entry point moved — this scan is blind"
    closure = _closure(defs, ("__call__",))
    assert {"_process", "_stream", "_take_utterances", "_heard"} <= closure, (
        "the tick closure no longer contains the capture path; the scan below "
        f"would pass vacuously. Found: {sorted(closure)}"
    )
    offences = sorted(_identifiers(defs, closure) & _SOCKET_NAMES)
    assert not offences, (
        f"the 50 Hz tick can now reach socket work: {offences}. The session "
        "client's worker thread owns the wire; the tick may only touch its "
        "bounded queues."
    )


def test_the_driver_module_imports_no_wire_primitive() -> None:
    """A module that cannot import a socket cannot accidentally use one."""
    tree = ast.parse(_DRIVER_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not imported & {"socket", "ssl", "select", "http", "http.client", "urllib.request"}
    assert "reachy.speech.realtime_wire" not in imported
    assert "reachy.speech.realtime" in imported, "the session client is the one hearing edge"


def test_the_driver_reaches_the_session_only_through_its_non_blocking_pair() -> None:
    """Everything the tick asks of the session is O(1) and queue-backed.

    All three call shapes count (see :func:`_session_surface`): the direct
    attribute, a local alias, and the defensive ``getattr`` probe — any of which
    could otherwise slip a new, blocking method past an attribute-only scan.
    """
    defs = _functions(_DRIVER_MODULE)
    reached = _session_surface(defs, _closure(defs, ("__call__",)))

    assert reached, "nothing reaches the session client — this scan is blind"
    assert "submit_audio" in reached and "take_utterance" in reached
    assert reached <= set(_CLIENT_TICK_SURFACE), (
        f"the driver reaches the session client through {sorted(reached)}; only "
        f"{list(_CLIENT_TICK_SURFACE)} are non-blocking. Anything else is "
        "either blocking work or lifecycle the composition root owns."
    )


def test_the_session_clients_tick_surface_is_socket_free_but_its_worker_is_not() -> None:
    """The other half of the claim, inside the module that DOES own the socket.

    Pinned in both directions on purpose: the tick-thread surface must be
    socket-free, and the worker must genuinely do socket work — otherwise the
    ban above would be satisfied by a client that never connects at all.
    """
    defs = _functions(_CLIENT_MODULE)
    surface = _closure(defs, _CLIENT_TICK_SURFACE)
    assert "start" in surface, "submit_audio no longer spawns the worker — scan is stale"
    offences = sorted(_identifiers(defs, surface) & _SOCKET_NAMES)
    assert not offences, (
        f"RealtimeTranscriber's tick-thread surface now reaches {offences}; the "
        "caller's 20 ms tick would pay for it."
    )

    worker = _closure(defs, ("_run",))
    assert _identifiers(defs, worker) & _SOCKET_NAMES, (
        "the session worker no longer does any socket work — the ban above is "
        "now vacuous, which is worse than no test at all."
    )


# --------------------------------------------------------------------------- #
# 2 — end to end over a real loopback session                                 #
# --------------------------------------------------------------------------- #


class _Mic:
    """A held-media-client stand-in producing one 10 ms chunk per tick."""

    def __init__(self) -> None:
        self.samplerate = RATE
        self.channels = 1
        self.calls = 0

    def audio(self) -> np.ndarray:
        self.calls += 1
        return (np.arange(CHUNK, dtype=np.float32) % 8 - 4.0) / 16.0


def _ctx(now: float):
    return SimpleNamespace(now=now, tick=int(now * 100), sense=Sense())


def _tick_until(driver, predicate, *, timeout: float = 5.0) -> bool:
    """Drive real ticks until *predicate* holds — the loop the runtime itself is."""
    deadline = time.monotonic() + timeout
    t = T0
    while time.monotonic() < deadline:
        driver(_ctx(t))
        t += DT
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_words_spoken_into_the_wire_reach_the_sense_snapshot() -> None:
    """The whole capture path, joined: mic -> append -> server VAD -> gate -> latch.

    The transcript names the robot, so admission takes the gate's name fast path
    and costs ZERO classifier calls — the offline-safe half of the engagement
    gate, exercised here through the real wire rather than a fake utterance.
    """
    with FakeRealtimeServer(Scenario.HAPPY_PATH, transcript=NAMED) as server:
        client = RealtimeTranscriber(
            sample_rate=RATE,
            url=server.url,
            backoff_initial_s=0.02,
            backoff_max_s=0.05,
            stale_after_s=120.0,
        )
        mic = _Mic()
        driver = TranscriptSenseDriver(
            media=mic,
            realtime=client,
            tuning=TranscriptTuning(min_words=3, engage_window_s=1.0),
        )
        try:
            client.start()
            # Poll on the LATCH itself: it lives for exactly one tick, so
            # waiting on a counter and then ticking again would clear it.
            assert _tick_until(driver, lambda: driver.peek() is not None), (
                "no transcript reached the latch over a real session "
                f"(sessions={client.sessions}, utterances={client.utterances}, "
                f"streamed={driver.streamed})"
            )
            providers = SenseProviders(transcript=driver.as_provider())
            assert read_perception(providers).transcript == NAMED
            assert server.append_payloads, "no audio ever reached the server"
        finally:
            driver.close()
            client.close()


def test_a_refused_handshake_leaves_the_runtime_hearing_nothing_and_ticking() -> None:
    """The no-fallback contract (c17) over a real socket.

    A gateway that 401s every attempt means no words — not a local endpointer
    quietly taking over — and the tick loop must not notice: every tick still
    runs, the mic is still read, and the transcript field stays ``None``.
    """
    with FakeRealtimeServer(Scenario.UNAUTHORIZED) as server:
        client = RealtimeTranscriber(
            sample_rate=RATE,
            url=server.url,
            backoff_initial_s=0.02,
            backoff_max_s=0.05,
        )
        mic = _Mic()
        driver = TranscriptSenseDriver(media=mic, realtime=client)
        try:
            client.start()
            _tick_until(driver, lambda: client.connect_failures >= 2, timeout=3.0)
            assert client.connect_failures >= 1, "the refusal never happened"
            assert driver.peek() is None
            assert driver.transcripts == 0
            assert driver.ticks == mic.calls, "a tick was dropped while the gateway refused"
            assert driver.streamed == driver.ticks, "audio stopped being offered to the session"
        finally:
            driver.close()
            client.close()
