"""Offline contracts for the passive in-engine held/unheld observer."""

from __future__ import annotations

import contextlib
import inspect
import io
import json
from types import SimpleNamespace

import pytest

from reachy.behavior.excited_motion_probe import (
    ARM_TIMEOUT_S,
    CUES,
    HEAD_AXES,
    MAX_TICK_GAP_S,
    MODES,
    MOTION_TIMEOUT_S,
    SETTLED_EDGE_S,
    ProbeDriver,
    SharedPoseReader,
)

pytestmark = pytest.mark.offline


class _Reader:
    def __init__(self, value=(1.25, -2.5)) -> None:
        self.value = value
        self.calls = 0

    def __call__(self):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.value


def _driver(mode: str, reader: SharedPoseReader, *, emit, **kwargs) -> ProbeDriver:
    """Use a loose gap only in coarse state-machine scenarios."""
    return ProbeDriver(mode, reader, emit=emit, max_tick_gap_s=100.0, **kwargs)


def _ctx(
    t: float,
    *,
    pitch: float = 0.0,
    owner: str = "feel-alive-1",
    active=frozenset({"feel-alive"}),
):
    head = dict.fromkeys(HEAD_AXES, 0.0)
    head["pitch"] = pitch
    return SimpleNamespace(
        now=t,
        tick=round(t * 50) + 1,
        pose={
            "head": head,
            "antennas": (2.0, -2.0),
            "body_yaw": 1.0,
        },
        ownership={"head": owner, "antennas": owner, "body_yaw": owner},
        active_names=lambda: set(active),
    )


@pytest.mark.parametrize("mode", MODES)
def test_schema_labels_cues_complete_pose_actual_and_timing(mode: str) -> None:
    source = _Reader()
    shared = SharedPoseReader(source)
    records: list[dict] = []
    clock = iter((10.0, 10.002, 10.02, 10.023))
    driver = _driver(mode, shared, emit=records.append, wall_now=lambda: next(clock))

    driver(_ctx(100.0))
    driver(_ctx(100.5))
    driver(_ctx(100.52, pitch=1.0))
    driver(_ctx(100.54, pitch=2.0))

    assert CUES == {"unheld": "HANDS OFF", "held": "START HOLD"}
    assert records[0]["type"] == "probe_start"
    assert records[0]["label"] == mode
    assert records[0]["phase"] == "armed"
    assert records[0]["cue"] == CUES[mode]
    samples = [record for record in records if record["type"] == "sample"]
    first = samples[0]
    assert first["timestamp_s"] == 100.52
    assert first["elapsed_s"] == 0.0
    assert first["phase"] == "excited_motion"
    assert tuple(first["commanded"]["head"]) == HEAD_AXES
    assert set(first["commanded"]) == {"head", "antennas", "body_yaw"}
    assert first["actual"] == {
        "availability": "available",
        "pitch": 1.25,
        "yaw": -2.5,
    }
    assert first["timing"] == {
        "sample_gap_s": None,
        "read_lag_s": pytest.approx(0.002),
    }
    assert samples[1]["timing"]["sample_gap_s"] == pytest.approx(0.02)
    assert source.calls == 2

    serialized = "\n".join(json.dumps(record, sort_keys=True) for record in records)
    assert '"classification"' not in serialized
    assert '"pat"' not in serialized
    assert '"scratch"' not in serialized


def test_starting_during_hold_waits_for_onset_without_actual_read() -> None:
    source = _Reader()
    records: list[dict] = []
    driver = _driver("unheld", SharedPoseReader(source), emit=records.append)

    driver(_ctx(5.0, pitch=3.0))
    driver(_ctx(5.0 + SETTLED_EDGE_S, pitch=3.0))
    driver(_ctx(8.0, pitch=3.0))

    assert source.calls == 0
    assert [record["phase"] for record in records] == ["armed", "armed"]

    driver(_ctx(8.02, pitch=3.1))

    sample = records[-1]
    assert sample["phase"] == "excited_motion"
    assert sample["elapsed_s"] == 0.0
    assert source.calls == 1


def test_starting_mid_motion_waits_for_next_full_burst_and_settled_edge() -> None:
    source = _Reader()
    records: list[dict] = []
    driver = _driver("unheld", SharedPoseReader(source), emit=records.append)

    # Already moving at probe start: this episode must not be captured.
    driver(_ctx(0.0, pitch=1.0))
    driver(_ctx(0.2, pitch=2.0))
    driver(_ctx(1.0, pitch=3.0))
    driver(_ctx(2.0, pitch=3.0))
    driver(_ctx(2.0 + SETTLED_EDGE_S, pitch=3.0))
    assert source.calls == 0

    # The next change is a genuine onset. Capture its whole moving episode and
    # retain samples through the first 0.5 s stable edge.
    driver(_ctx(5.0, pitch=3.1))
    driver(_ctx(6.0, pitch=4.0))
    driver(_ctx(7.0, pitch=5.0))
    driver(_ctx(8.0, pitch=5.0))
    driver(_ctx(8.0 + SETTLED_EDGE_S, pitch=5.0))
    driver(_ctx(99.0, pitch=5.0))

    assert [record["type"] for record in records].count("probe_end") == 1
    assert records[-1]["phase"] == "settled"
    assert records[-1]["elapsed_s"] == pytest.approx(3.0 + SETTLED_EDGE_S)
    samples = [record for record in records if record["type"] == "sample"]
    assert samples[0]["timestamp_s"] == 5.0
    assert samples[0]["elapsed_s"] == 0.0
    assert [sample["phase"] for sample in samples[-2:]] == ["settled", "settled"]
    assert source.calls == len(samples) == 5


def test_no_motion_times_out_fail_closed_without_actual_read() -> None:
    source = _Reader()
    records: list[dict] = []
    driver = _driver("held", SharedPoseReader(source), emit=records.append)

    driver(_ctx(0.0, pitch=2.0))
    driver(_ctx(SETTLED_EDGE_S, pitch=2.0))
    driver(_ctx(ARM_TIMEOUT_S + 0.001, pitch=2.0))

    assert records[-1]["type"] == "probe_refused"
    assert "timeout" in records[-1]["reason"]
    assert source.calls == 0


def test_never_settling_motion_episode_has_a_separate_hard_timeout() -> None:
    source = _Reader()
    records: list[dict] = []
    driver = _driver("held", SharedPoseReader(source), emit=records.append)

    driver(_ctx(0.0))
    driver(_ctx(SETTLED_EDGE_S))
    driver(_ctx(1.0, pitch=1.0))
    driver(_ctx(1.0 + MOTION_TIMEOUT_S + 0.001, pitch=2.0))

    assert records[-1]["type"] == "probe_refused"
    assert "motion episode timeout" in records[-1]["reason"]
    assert source.calls == 1


@pytest.mark.parametrize("actual", [None, (float("nan"), 1.0), (1.0, float("inf"))])
def test_missing_or_nonfinite_actual_refuses_capture(actual: object) -> None:
    source = _Reader(actual)
    records: list[dict] = []
    driver = _driver("held", SharedPoseReader(source), emit=records.append)

    driver(_ctx(0.0))
    driver(_ctx(SETTLED_EDGE_S))
    driver(_ctx(1.0, pitch=1.0))

    assert records[-1]["type"] == "probe_refused"
    assert "actual pose unavailable" in records[-1]["reason"]
    assert not any(record["type"] == "sample" for record in records)
    assert not any(record["type"] == "probe_end" for record in records)
    json.dumps(records, allow_nan=False)
    assert source.calls == 1


def test_raising_actual_reader_refuses_capture() -> None:
    calls = 0

    def fail():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise RuntimeError("reader failed")

    records: list[dict] = []
    driver = _driver("held", SharedPoseReader(fail), emit=records.append)
    driver(_ctx(0.0))
    driver(_ctx(SETTLED_EDGE_S))
    driver(_ctx(1.0, pitch=1.0))

    assert records[-1]["type"] == "probe_refused"
    assert "actual pose unavailable" in records[-1]["reason"]
    assert calls == 1


@pytest.mark.parametrize(
    ("ctx", "reason"),
    [
        (_ctx(0.0, owner="nod-2"), "feel-alive"),
        (
            SimpleNamespace(
                now=0.0,
                tick=1,
                pose={},
                ownership={"head": "feel-alive-1", "antennas": None, "body_yaw": None},
                active_names=lambda: {"feel-alive"},
            ),
            "all three",
        ),
        (_ctx(0.0, active=frozenset()), "active"),
    ],
)
def test_refuses_before_pose_or_actual_inspection(
    ctx, reason: str  # type: ignore[no-untyped-def]
) -> None:
    source = _Reader()
    records: list[dict] = []
    driver = _driver("held", SharedPoseReader(source), emit=records.append)

    driver(ctx)

    assert len(records) == 1
    assert {key: records[0][key] for key in records[0] if key != "reason"} == {
        "schema": "reachy.excited-motion-probe/v1",
        "type": "probe_refused",
        "label": "held",
        "phase": "refused",
        "timestamp_s": 0.0,
    }
    assert reason in records[0]["reason"]
    assert source.calls == 0


def test_ownership_drift_aborts_before_another_actual_read() -> None:
    source = _Reader()
    records: list[dict] = []
    driver = _driver("held", SharedPoseReader(source), emit=records.append)

    driver(_ctx(0.0))
    driver(_ctx(SETTLED_EDGE_S))
    driver(_ctx(1.0, pitch=1.0))
    driver(_ctx(1.02, pitch=2.0, owner="gaze-hold-2"))

    assert records[-1]["type"] == "probe_refused"
    assert source.calls == 1


def test_shared_reader_samples_once_for_probe_and_existing_consumer() -> None:
    source = _Reader()
    shared = SharedPoseReader(source)
    driver = _driver("held", shared, emit=lambda _record: None)

    driver(_ctx(0.0))
    driver(_ctx(SETTLED_EDGE_S))
    driver(_ctx(1.0, pitch=1.0))
    assert shared.read() == (1.25, -2.5)  # existing sense consumer, same engine tick
    assert source.calls == 1

    driver(_ctx(1.02, pitch=2.0))
    assert shared.read() == (1.25, -2.5)
    assert source.calls == 2


@pytest.mark.parametrize(
    ("fault_t", "reason"),
    [
        pytest.param(0.51, "backward", id="backward"),
        pytest.param(0.52 + MAX_TICK_GAP_S + 0.001, "gap", id="excessive-gap"),
    ],
)
def test_corrupt_tick_timing_refuses_before_another_actual_read(
    fault_t: float, reason: str
) -> None:
    source = _Reader()
    records: list[dict] = []
    driver = ProbeDriver("held", SharedPoseReader(source), emit=records.append)

    for index in range(26):
        driver(_ctx(index * 0.02))
    driver(_ctx(0.52, pitch=1.0))
    assert source.calls == 1

    driver(_ctx(fault_t, pitch=2.0))

    assert records[-1]["type"] == "probe_refused"
    assert reason in records[-1]["reason"]
    assert source.calls == 1


def test_module_has_no_robot_command_or_motion_generator_path() -> None:
    import reachy.behavior.excited_motion_probe as module

    source = inspect.getsource(module)
    for forbidden in (
        "SdkTransport",
        "streaming(",
        "set_target",
        "TargetSink",
        "make_feel_alive",
        "neutral_head",
    ):
        assert forbidden not in source


def test_cli_engine_run_wires_passive_probe_without_extra_sends(
    monkeypatch, tmp_path, capsys
) -> None:
    from reachy.behavior import engine as engine_module
    from reachy.cli import main
    from reachy.cli._commands import behavior as behavior_module

    class Sink:
        def __init__(self) -> None:
            self.calls = 0

        def set_target(self, **_command) -> None:  # type: ignore[no-untyped-def]
            self.calls += 1

    class Transport:
        name = "fake"

        def __init__(self) -> None:
            self.sink = Sink()

        @contextlib.contextmanager
        def streaming(self):  # type: ignore[no-untyped-def]
            yield self.sink

        def doa(self, timeout=None):  # type: ignore[no-untyped-def]
            return None

    class Held:
        def __init__(self) -> None:
            self.calls = 0
            self.closed = False

        def read(self):  # type: ignore[no-untyped-def]
            self.calls += 1
            return (1.0, -1.0)

        def close(self) -> None:
            self.closed = True

    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            self.value += 0.1
            return self.value

    transport = Transport()
    held = Held()
    output = tmp_path / "unheld.jsonl"
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(behavior_module, "get_transport", lambda _args: transport)
    monkeypatch.setattr(behavior_module, "_make_state_reader", lambda: held)
    real_run = engine_module.run

    def run_with_clock(*args, **kwargs):  # type: ignore[no-untyped-def]
        return real_run(*args, **{**kwargs, "now": Clock(), "sleep": lambda _seconds: None})

    monkeypatch.setattr(behavior_module, "engine_run", run_with_clock)

    rc = main(
        [
            "behavior",
            "engine",
            "run",
            "--max-ticks",
            "4",
            "--probe-mode",
            "unheld",
            "--probe-output",
            str(output),
        ]
    )

    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert rc == 0
    assert [record["type"] for record in records] == ["probe_start"]
    assert records[0]["phase"] == "armed"
    assert "HANDS OFF" in capsys.readouterr().err
    assert transport.sink.calls == 6  # preflight + 4 engine ticks + engine settle
    assert held.calls == 0
    assert held.closed is True


def test_probe_mode_omits_acting_stack_and_rejects_both_command_spools(
    monkeypatch, tmp_path
) -> None:
    from reachy.behavior import control
    from reachy.behavior import engine as engine_module
    from reachy.behavior.intents import INTENT_NAMESPACE
    from reachy.behavior.rules import default_rules_path
    from reachy.cli import main
    from reachy.cli._commands import behavior as behavior_module

    state_root = tmp_path / "state"
    monkeypatch.setenv("REACHY_STATE_DIR", str(state_root))
    rules_path = default_rules_path()
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        """
[[react]]
id = "pat-acknowledge"
when = { field = "pat", op = "is_true" }
run = "pet-reaction"
cooldown_s = 0.0
""".strip(),
        encoding="utf-8",
    )

    main_id = control.submit("add", name="thoughtful")
    intent_id = control.submit("run_behavior", namespace=INTENT_NAMESPACE, name="pet-reaction")
    goto_id = control.submit("goto", namespace=INTENT_NAMESPACE, head={"pitch": 9.0}, duration=1.0)

    class Sink:
        def set_target(self, **_command) -> None:  # type: ignore[no-untyped-def]
            return None

    class Transport:
        name = "fake"

        @contextlib.contextmanager
        def streaming(self):  # type: ignore[no-untyped-def]
            yield Sink()

        def doa(self, timeout=None):  # type: ignore[no-untyped-def]
            return None

    class Held:
        def __init__(self) -> None:
            self.samples = iter([(-3.0, 0.0), (0.0, 0.0), (-3.0, 0.0)])
            self.last = (0.0, 0.0)
            self.calls = 0
            self.closed = False

        def read(self):  # type: ignore[no-untyped-def]
            self.calls += 1
            self.last = next(self.samples, self.last)
            return self.last

        def close(self) -> None:
            self.closed = True

    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            self.value += 0.02
            return self.value

    def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("acting runtime component composed in probe mode")

    held = Held()
    transport = Transport()
    events: list[dict] = []
    admissions: list[str] = []
    real_admit = engine_module.Engine.admit_behavior

    def record_admit(self, behavior, now):  # type: ignore[no-untyped-def]
        admissions.append(behavior.name)
        return real_admit(self, behavior, now)

    monkeypatch.setattr(behavior_module, "get_transport", lambda _args: transport)
    monkeypatch.setattr(behavior_module, "_make_state_reader", lambda: held)
    monkeypatch.setattr(behavior_module, "_boot_tick_seam", forbidden)
    monkeypatch.setattr(behavior_module, "PatSenseDriver", forbidden)
    monkeypatch.setattr(behavior_module, "IntentDriver", forbidden)
    monkeypatch.setattr(behavior_module, "GotoLane", forbidden)
    monkeypatch.setattr(engine_module.Engine, "admit_behavior", record_admit)
    real_run = engine_module.run

    def run_observed(*args, **kwargs):  # type: ignore[no-untyped-def]
        original_emit = kwargs["emit"]

        def emit(event):  # type: ignore[no-untyped-def]
            events.append(event)
            original_emit(event)

        return real_run(
            *args,
            **{
                **kwargs,
                "emit": emit,
                "now": Clock(),
                "sleep": lambda _seconds: None,
            },
        )

    monkeypatch.setattr(behavior_module, "engine_run", run_observed)
    output = tmp_path / "held.jsonl"

    rc = main(
        [
            "behavior",
            "engine",
            "run",
            "--json",
            "--max-ticks",
            "850",
            "--probe-mode",
            "held",
            "--probe-output",
            str(output),
        ]
    )

    assert rc == 0
    assert admissions == []
    assert held.calls > 0
    assert held.closed is True
    for event in events:
        owners = event["ownership"]
        assert len(set(owners.values())) == 1
        owner = owners["head"]
        assert owner.startswith("feel-alive-")
        assert owner.removeprefix("feel-alive-").isdigit()
    state = control.read_state()
    assert {item["name"] for item in state["active"]} == {"feel-alive"}

    for cmd_id, namespace in (
        (main_id, ""),
        (intent_id, INTENT_NAMESPACE),
        (goto_id, INTENT_NAMESPACE),
    ):
        result = control.await_result(cmd_id, namespace=namespace, timeout=0.0)
        assert result["ok"] is False
        assert "observation-only" in result["error"]


def test_cli_probe_refuses_fresh_engine_before_output_or_transport(
    monkeypatch, tmp_path, capsys
) -> None:
    from reachy.behavior import control
    from reachy.cli import main
    from reachy.cli._commands import behavior as behavior_module

    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path / "state"))
    control.CommandSpool().write_state({"updated": 100.0})
    monkeypatch.setattr(behavior_module.time, "monotonic", lambda: 101.0)
    transport_calls: list[object] = []
    engine_calls: list[object] = []
    monkeypatch.setattr(behavior_module, "get_transport", lambda args: transport_calls.append(args))
    monkeypatch.setattr(
        behavior_module, "engine_run", lambda *args, **kwargs: engine_calls.append(args)
    )
    output = tmp_path / "must-not-exist.jsonl"

    rc = main(
        [
            "behavior",
            "engine",
            "run",
            "--probe-mode",
            "held",
            "--probe-output",
            str(output),
        ]
    )

    assert rc != 0
    assert not output.exists()
    assert transport_calls == []
    assert engine_calls == []
    error = capsys.readouterr().err
    assert "already-running" in error
    assert "Ctrl-C in its owning terminal" in error
    assert "behavior engine stop' only" in error
    assert "launched with 'reachy behavior engine start'" in error
    assert "sole foreground 'reachy behavior engine run'" in error


def test_probe_output_closes_when_seam_composition_fails(monkeypatch, tmp_path) -> None:
    from reachy.cli import main
    from reachy.cli._commands import behavior as behavior_module

    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path / "state"))
    stream = io.StringIO()
    monkeypatch.setattr(behavior_module, "_open_probe_output", lambda _path: stream)
    monkeypatch.setattr(
        behavior_module, "get_transport", lambda _args: SimpleNamespace(name="fake")
    )
    monkeypatch.setattr(behavior_module, "_boot_tick_seam", lambda: None)
    monkeypatch.setattr(behavior_module, "build_runtime_export_consumer", lambda _args: None)

    def fail_composition(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        assert stream.closed is False
        raise RuntimeError("composition failed")

    monkeypatch.setattr(behavior_module, "_compose_run_seam", fail_composition)

    rc = main(
        [
            "behavior",
            "engine",
            "run",
            "--probe-mode",
            "held",
            "--probe-output",
            str(tmp_path / "probe.jsonl"),
        ]
    )

    assert rc != 0
    assert stream.closed is True


def test_invalid_mode_is_rejected_without_a_read() -> None:
    source = _Reader()
    # Constructed outside the raises block so the assertion below pins the
    # refusal to ProbeDriver's own mode check, not to reader construction.
    reader = SharedPoseReader(source)
    with pytest.raises(ValueError, match="mode"):
        ProbeDriver("unknown", reader, emit=lambda _record: None)
    assert source.calls == 0
