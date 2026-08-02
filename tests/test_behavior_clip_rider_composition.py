"""The clip rider, composed into the runtime seam (task t5).

``tests/test_behavior_clip_rider.py`` pins the rider itself against a hand-
built spool and an injected encoder; this file pins the WIRING —
``_compose_run_seam`` actually registers :class:`~reachy.behavior.clip_rider.
ClipRider` on the ONE ``TickBus`` and feeds it frames through
:class:`~reachy.behavior.face_sense.FaceSenseDriver`'s ``add_frame_sink`` seam,
never a second camera read — by driving the REAL ``behavior engine run`` CLI
path against a fake transport (no robot, no daemon, no SDK, no network) and
reading the ``state.json`` it leaves behind, mirroring
``tests/test_behavior_availability_composition.py``'s and
``tests/test_behavior_audio_tee_composition.py``'s own composition-test shape.
"""

from __future__ import annotations

import ast
import contextlib
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from reachy.behavior import clip_rider as CR
from reachy.behavior import control, face_sense
from reachy.behavior.clip_rider import ClipRider
from reachy.cli import main
from reachy.cli._commands import behavior as behavior_mod
from reachy.export.mqtt import is_text_reference_only

pytestmark = pytest.mark.offline

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BEHAVIOR_MODULE = _REPO_ROOT / "reachy" / "cli" / "_commands" / "behavior.py"


# --------------------------------------------------------------------------- #
# Fakes / fixtures                                                            #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def set_target(self, *a, **k):
        return {"ok": True}


class _QuietTransport:
    """A fake transport whose DoA route has no reading (a mic-less box)."""

    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink

    def doa(self, timeout=None):
        return None


def _frame(width: int = 4, height: int = 3) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


class _FakeMedia:
    """The held media client stand-in: warms, hands out a fresh frame every read."""

    samplerate = 16000
    channels = 1
    camera_available = True

    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self._n = 0

    def warm_up(self) -> bool:
        self.connected = True
        return True

    def audio(self):
        return None

    def frame(self):
        self._n += 1
        return _frame(width=4 + (self._n % 3))

    def close(self) -> None:
        self.closed = True


class _FakeEncoder:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls = 0
        self.ran = threading.Event()

    def __call__(self, frames, fps, path):
        self.calls += 1
        self.ran.set()
        if self.result:
            path.write_bytes(b"\x00" * 16)
        return self.result


@pytest.fixture
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.setenv("REACHY_PAT_SENSE", "0")  # no held pose reader to fake
    monkeypatch.setattr(behavior_mod, "_make_media_client", lambda: _FakeMedia())
    monkeypatch.setattr(behavior_mod, "get_transport", lambda args: _QuietTransport())
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tmp_path


@pytest.fixture(autouse=True)
def _restore_vision_latches():
    saved_face = face_sense._VISION_WARNED
    saved_clip = CR._VISION_WARNED
    yield
    face_sense._VISION_WARNED = saved_face
    CR._VISION_WARNED = saved_clip


def _drivers_of(tick_seam) -> list:
    """The TickBus driver list behind whatever wrappers the seam is wearing."""
    seam = tick_seam
    for _ in range(4):
        drivers = getattr(seam, "_drivers", None)
        if drivers is not None:
            return list(drivers)
        inner = getattr(seam, "_inner", None) or getattr(seam, "_seam", None)
        if inner is None:
            break
        seam = inner
    raise AssertionError(f"no driver list found behind {type(tick_seam).__name__}")


def _run_engine(ticks: int = 6) -> dict:
    assert main(["behavior", "engine", "run", "--max-ticks", str(ticks)]) == 0
    state = control.read_state()
    assert isinstance(state, dict), "the engine published no state.json"
    return state


def _wait(predicate, *, timeout: float = 5.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _module_tree() -> ast.Module:
    return ast.parse(_BEHAVIOR_MODULE.read_text(encoding="utf-8"))


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found — this scan is blind")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    found: list[ast.Call] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if (isinstance(func, ast.Name) and func.id == name) or (
            isinstance(func, ast.Attribute) and func.attr == name
        ):
            found.append(child)
    return found


# --------------------------------------------------------------------------- #
# 1. The rider is composed onto the ONE TickBus, unconditionally               #
# --------------------------------------------------------------------------- #


def test_the_clip_rider_is_composed_onto_the_tick_seam(_isolated):
    from reachy.behavior.engine import EngineConfig

    sense_reader, tick_seam, resources = behavior_mod._compose_run_seam(
        _QuietTransport(), EngineConfig(compose_hz=50, base_layer=True, settle=False), None, None
    )
    try:
        drivers = _drivers_of(tick_seam)
        assert any(isinstance(d, ClipRider) for d in drivers), [type(d).__name__ for d in drivers]
    finally:
        resources.close()


def test_the_probe_composition_carries_no_clip_rider(_isolated):
    """The observation-only probe seam composes no riders at all (matches
    ``sense_availability``'s own probe-path exclusion)."""
    from reachy.behavior.engine import EngineConfig

    sense_reader, tick_seam, resources = behavior_mod._compose_run_seam(
        _QuietTransport(),
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        None,
        None,
        probe=("held", lambda record: None),
    )
    try:
        drivers = _drivers_of(tick_seam)
        assert not any(isinstance(d, ClipRider) for d in drivers)
    finally:
        resources.close()


def test_a_missing_vision_extra_reports_a_named_reason_never_a_crash(_isolated, monkeypatch):
    """The real, un-mocked path: cv2 is genuinely absent in this environment."""
    state = _run_engine()
    assert state[CR.STATE_KEY]["available"] is False
    assert state[CR.STATE_KEY]["reason"] in (
        CR.VISION_EXTRA_ABSENT,
        CR.VISION_STACK_UNAVAILABLE,
    )
    assert is_text_reference_only(state[CR.STATE_KEY])


def test_the_senses_and_clip_blocks_are_both_additive_to_engine_state(_isolated):
    """The rider merges; it never replaces the engine's own keys or the
    availability rider's."""
    state = _run_engine()
    for key in ("updated", "compose_hz", "active", "ownership", "doa", "senses", CR.STATE_KEY):
        assert key in state, f"a rider clobbered {key!r} or never wrote it"


# --------------------------------------------------------------------------- #
# 2. Frames reach the rider by PUSH, never a second camera read                #
# --------------------------------------------------------------------------- #


def test_the_rider_is_wired_as_a_frame_sink_not_a_reader_of_its_own(_isolated):
    """Structural: the seam registers ``clip_rider.offer`` on the ONE
    ``FaceSenseDriver``, and ``ClipRider`` itself is never handed the media
    client — this file's own module (clip_rider.py) never calls ``.frame()``.
    """
    tree = _module_tree()
    seam = _function(tree, "_compose_run_seam")
    registrations = [
        call
        for call in _calls(seam, "add_frame_sink")
        if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
    ]
    assert (
        len(registrations) == 1
    ), f"expected exactly one frame sink registration, got {len(registrations)}"
    call = registrations[0]
    (arg,) = call.args
    assert (
        isinstance(arg, ast.Attribute) and arg.attr == "offer"
    ), "the registered frame sink is not the clip rider's offer()"

    source = Path(__file__).resolve().parent.parent / "reachy" / "behavior" / "clip_rider.py"
    clip_tree = ast.parse(source.read_text(encoding="utf-8"))
    frame_calls = [
        node
        for node in ast.walk(clip_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "frame"
    ]
    assert frame_calls == [], "clip_rider.py must never call media.frame() itself"


class _StepClock:
    """A deterministic monotonic clock advancing ``dt`` seconds per call.

    Mirrors ``test_behavior_audio_tee_composition.py``'s own helper: several
    runtime fields are timing-dependent, so a real engine run needs an
    injected clock rather than the wall clock to stay reproducible.
    """

    def __init__(self, dt: float = 0.02) -> None:
        self.t = 0.0
        self.dt = dt

    def __call__(self) -> float:
        self.t += self.dt
        return self.t


def test_frames_flow_from_face_driver_to_the_rider_end_to_end(_isolated, monkeypatch):
    """No stubs between the seam and the rider: real ticks (through the ACTUAL
    engine loop, so ``face_driver`` — and therefore ``clip_rider.offer`` — is
    genuinely invoked, not just ``sense_reader``), a real fake camera, a real
    (fake) encoder — the clip descriptor must reach state.json."""
    from reachy.behavior import control as control_mod
    from reachy.behavior import engine as engine_mod
    from reachy.behavior.engine import EngineConfig

    encoder = _FakeEncoder()
    monkeypatch.setattr(behavior_mod, "build_clip_encoder", lambda: encoder)
    monkeypatch.setattr(behavior_mod, "clip_seconds_from_env", lambda: 10.0)

    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    spool = control_mod.CommandSpool()
    sense_reader, tick_seam, resources = behavior_mod._compose_run_seam(
        _QuietTransport(), config, None, None, main_control=spool
    )
    try:
        # The real engine loop, so every composed DRIVER (face_driver among
        # them) is actually ticked — not just sense_reader, which only PEEKS
        # driver state and never advances face_driver's frame read.
        engine_mod.run(
            _QuietTransport(),
            config,
            sleep=lambda *_: None,
            now=_StepClock(dt=0.02),
            max_ticks=20,
            control=spool,
            sense=sense_reader,
            tick_seam=tick_seam,
        )
        assert encoder.ran.wait(timeout=5.0), "the fake encoder was never invoked"
        clip_rider = next(d for d in resources.drivers if isinstance(d, ClipRider))
        # ``encoder.ran`` only proves the encode CALL happened; the worker still
        # has a few more steps (os.replace, latching the descriptor) after that
        # call returns, so poll the rider's own block rather than racing it.
        assert _wait(lambda: clip_rider.block().get("available") is True), clip_rider.block()
        clip_rider(None)  # force a final state.json publish deterministically
        state = spool.read_state()
        assert state[CR.STATE_KEY]["available"] is True
        assert state[CR.STATE_KEY]["path"].endswith(CR.DEFAULT_CLIP_FILENAME)
        assert is_text_reference_only(state[CR.STATE_KEY])
    finally:
        resources.close()


# --------------------------------------------------------------------------- #
# 3. Resource lifecycle: composed unconditionally, closed with everything else #
# --------------------------------------------------------------------------- #


def test_the_rider_is_released_by_runtime_resources(_isolated, monkeypatch):
    encoder = _FakeEncoder()
    monkeypatch.setattr(behavior_mod, "build_clip_encoder", lambda: encoder)

    from reachy.behavior.engine import EngineConfig

    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    _sense_reader, _metrics, resources = behavior_mod._compose_run_seam(
        _QuietTransport(), config, None, None
    )
    clip_rider = next(d for d in resources.drivers if isinstance(d, ClipRider))
    assert clip_rider.worker_alive is True
    resources.close()
    assert clip_rider.worker_alive is False


def test_the_rider_is_released_even_when_composition_fails_after_it_opens(_isolated, monkeypatch):
    """A raise part-way through composition must not strand the worker thread."""
    encoder = _FakeEncoder()
    monkeypatch.setattr(behavior_mod, "build_clip_encoder", lambda: encoder)

    class _Boom(RuntimeError):
        pass

    def _explode(*_a, **_k):
        raise _Boom("composition exploded after the clip rider was opened")

    from reachy.behavior.engine import EngineConfig

    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    with monkeypatch.context() as m:
        m.setattr(behavior_mod, "GotoLane", _explode)
        with pytest.raises(_Boom):
            behavior_mod._compose_run_seam(_QuietTransport(), config, None, None)
    # No direct handle survives a failed composition; the only observable proof
    # is indirect — a second composition (GotoLane restored) must still
    # succeed: no leaked global state, no wedged thread holding a lock this
    # process needs again.
    _sense_reader, _metrics, resources = behavior_mod._compose_run_seam(
        _QuietTransport(), config, None, None
    )
    resources.close()


# --------------------------------------------------------------------------- #
# 4. The bus carries only a text reference — through the real seam             #
# --------------------------------------------------------------------------- #


def test_the_retained_bus_tree_mirrors_the_clip_key_too(_isolated, monkeypatch):
    """The nervous-system mirror (t14) picks up ``clip`` the same way it picks
    up ``senses``/``intents`` — no special-casing needed, because ClipRider
    writes through the SAME injected ``main_control``."""
    from tests.fake_events_client import FakeEventsClient

    client = FakeEventsClient()
    monkeypatch.setattr(behavior_mod, "_make_events_client", lambda: client)

    state = _run_engine()
    assert CR.STATE_KEY in state

    retained = {
        p.topic.rsplit("/", 1)[-1]: p.payload
        for p in client.published
        if p.topic.startswith("reachy/state/") and p.retain
    }
    assert CR.STATE_KEY in retained
    assert json.loads(retained[CR.STATE_KEY]) == state[CR.STATE_KEY]
    assert is_text_reference_only(json.loads(retained[CR.STATE_KEY]))
