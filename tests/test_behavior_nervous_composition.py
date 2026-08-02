"""Composition tests for the nervous-system publisher (task t7, spec c34/c21).

Task t6 built :mod:`reachy.export.mqtt` against an injected client seam and
proved it against ``tests/fake_events_client.py``. This file proves the other
half: that ``behavior engine run`` actually *composes* that publisher onto the
engine's ONE ``TickBus`` — **unconditionally, with no flag** — and that doing so
changed nothing else.

Why unconditional matters, and why it is asserted rather than assumed
--------------------------------------------------------------------
The deployed ``reachy-runtime.service`` ``ExecStart`` carries no ``--export``
(probed 2026-07-24), so anything gated behind a flag would never run on the
robot. Configuration is :data:`reachy.export.mqtt.BROKER_URL_ENV`
(``REACHY_MQTT_URL``) and nothing else.

The events-cli wheel does not exist yet
---------------------------------------
The broker and its client belong to the sibling ``events-cli`` project
(``agentculture/events-cli#3``). This repo ships no MQTT library and no
dependency on one, so the client is imported LAZILY and DEFENSIVELY at
composition. Every condition below is **injected** (a fake client, a recording
factory, a deliberately-broken import spec) and never read off the running
interpreter — a test that asks "is events-cli installed here?" passes on bare CI
and fails on the first box that installs it, which is a property of the machine
rather than of the code under test.

The four contracts asserted here
--------------------------------
1. composed on every engine run; a missing package, an incompatible client and
   an unreachable broker each degrade to ONE named ``senselog`` drop and an
   otherwise-unchanged runtime;
2. **additivity** — the ``--export`` stdout feed is byte-identical (h20, and
   proved the strong way: the broker payloads ARE the stdout lines), no
   listening socket is added (h10), no new ``media.audio()`` / ``get_frame()``
   caller appears (h9), and no second SDK media session is constructed (h8);
3. ``REACHY_MQTT_URL`` (default ``localhost:1883``) reaches the client seam;
4. :meth:`reachy.behavior.tick_metrics.TickMetrics.close` is called from
   ``_RuntimeResources.close()``, so an overrun episode still open at shutdown
   is flushed (the t1 hand-off).
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import re
from pathlib import Path

import pytest

from reachy.behavior.engine import EngineConfig
from reachy.behavior.tick_metrics import EVENT_OVERRUN_SUMMARY, TickMetrics
from reachy.cli import main
from reachy.cli._commands import behavior as behavior_mod
from reachy.export import mqtt as M
from tests.fake_events_client import BlockingTrapClient, FakeEventsClient

SENSE_LOGGER = "reachy.sense"
REPO_ROOT = Path(__file__).parent.parent


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


@pytest.fixture
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.delenv(M.BROKER_URL_ENV, raising=False)
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.get_transport", lambda args: _QuietTransport()
    )
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tmp_path


def _inject_client(monkeypatch, client):
    """Make composition see exactly *client* (or ``None``) — never the ambient env."""
    monkeypatch.setattr(behavior_mod, "_make_events_client", lambda: client)
    return client


def _compose(**kwargs):
    """Compose the REAL run seam against a fake transport (no robot, no SDK)."""
    return behavior_mod._compose_run_seam(
        _QuietTransport(),
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        kwargs.pop("rules_driver", None),
        kwargs.pop("runtime_consumer", None),
        **kwargs,
    )


def _unwrap(seam, attr: str):
    """Find *attr* behind whatever wrappers the returned tick seam is wearing."""
    for _ in range(4):
        found = getattr(seam, attr, None)
        if found is not None:
            return found
        inner = getattr(seam, "_inner", None) or getattr(seam, "_seam", None)
        if inner is None:
            break
        seam = inner
    raise AssertionError(f"no {attr} found behind {type(seam).__name__}")


def _consumers_of(tick_seam) -> list:
    return list(_unwrap(tick_seam, "_consumers"))


def _drivers_of(tick_seam) -> list:
    return list(_unwrap(tick_seam, "_drivers"))


def _nervous_drops(stderr: str) -> list[str]:
    """Every ``[SENSE stage=nervous source=mqtt ...] dropped reason=…`` line.

    Read off CAPTURED STDERR, not ``caplog``: ``install_logging`` sets
    ``propagate = False`` on the ``reachy`` logger (the #96 fix, so a foreign
    root handler can never double our lines), which means pytest's root-level
    capture handler sees nothing at all once a CLI run has installed logging.
    The journal is the real surface an operator greps anyway.
    """
    marker = f"[SENSE stage={M.STAGE} source={M.SOURCE} event="
    return [
        line
        for line in stderr.splitlines()
        if line.startswith(marker) and "dropped reason=" in line
    ]


#: Keys whose value is a clock reading, so two runs of the same code can never
#: produce the same bytes. Stripped before comparing two feeds for equality.
_VOLATILE_KEYS = ("ts", "phase_started_at", "last_press_at")


def _stable(lines: list[str]) -> list[dict]:
    """Decode JSONL, dropping every clock reading, at any depth."""

    def _scrub(value):
        if isinstance(value, dict):
            return {k: _scrub(v) for k, v in value.items() if k not in _VOLATILE_KEYS}
        if isinstance(value, list):
            return [_scrub(v) for v in value]
        return value

    return [_scrub(json.loads(line)) for line in lines]


def _retained_state_tree(client) -> dict:
    """Reassemble the retained ``reachy/state/*`` tree the way a subscriber would.

    A retained topic carries its LAST value, so we fold the publish log keeping
    the newest payload per topic. The publisher-owned ``online`` key is dropped —
    it is availability metadata the runtime never writes to ``state.json`` (c36),
    so it has no counterpart on disk to compare against.
    """
    tree: dict = {}
    for p in client.published:
        if p.topic.startswith("reachy/state/") and p.retain:
            tree[p.topic.rsplit("/", 1)[-1]] = json.loads(p.payload)
    tree.pop(M.ONLINE_KEY, None)
    return tree


# --------------------------------------------------------------------------- #
# 1. Criterion 1 — composed on EVERY engine run, no flag                      #
# --------------------------------------------------------------------------- #


def test_the_publisher_is_composed_with_no_export_flag(_isolated, monkeypatch):
    """The load-bearing one: the boot unit passes no ``--export``."""
    client = _inject_client(monkeypatch, FakeEventsClient())
    _sense, tick_seam, resources = _compose(runtime_consumer=None)
    try:
        assert resources.publisher is not None
        assert any(
            getattr(c, "_sink", None) is resources.publisher for c in _consumers_of(tick_seam)
        ), "the publisher is not a consumer of the engine's ONE TickBus"
        assert client.connect_calls == 1, "composition must open the session at setup"
    finally:
        resources.close()


def test_the_publisher_is_composed_alongside_the_export_consumer(_isolated, monkeypatch):
    """``--export -`` and the bus are two consumers of ONE feed, never a choice."""
    _inject_client(monkeypatch, FakeEventsClient())
    exported: list = []
    _sense, tick_seam, resources = _compose(runtime_consumer=exported.append)
    try:
        consumers = _consumers_of(tick_seam)
        assert exported.append in consumers
        assert any(getattr(c, "_sink", None) is resources.publisher for c in consumers)
        assert len(consumers) == 2
    finally:
        resources.close()


def test_the_publisher_is_composed_on_the_probe_path_too(_isolated, monkeypatch):
    """``--probe-mode`` is still an engine run; "no flag" admits no exception."""
    _inject_client(monkeypatch, FakeEventsClient())
    _sense, tick_seam, resources = _compose(probe=("held", lambda record: None))
    try:
        assert resources.publisher is not None
        assert any(
            getattr(c, "_sink", None) is resources.publisher for c in _consumers_of(tick_seam)
        )
    finally:
        resources.close()


def test_a_missing_events_cli_package_is_one_named_drop_and_a_live_runtime(
    _isolated, monkeypatch, capsys
):
    """Today's real condition: no wheel, so no client. Injected, not sniffed."""
    _inject_client(monkeypatch, None)
    assert main(["behavior", "engine", "run", "--max-ticks", "5"]) == 0

    named = _nervous_drops(capsys.readouterr().err)
    assert len(named) == 1, named
    assert M.REASON_NO_CLIENT in named[0]

    from reachy.behavior import control

    state = control.read_state()
    assert isinstance(state, dict), "the runtime ran unchanged"
    assert "ownership" in state, "the runtime ran unchanged"


def test_an_incompatible_client_is_one_named_drop_and_a_live_runtime(
    _isolated, monkeypatch, capsys
):
    """The events-cli wheel could ship a different shape; that must not crash us."""

    class _WrongShape:
        connected = False

        def connect(self):  # missing will_set / disconnect / publish
            raise AssertionError("an incompatible client must never be driven")

    _inject_client(monkeypatch, _WrongShape())
    assert main(["behavior", "engine", "run", "--max-ticks", "5"]) == 0

    named = _nervous_drops(capsys.readouterr().err)
    assert len(named) == 1, named
    assert M.REASON_CLIENT_INCOMPATIBLE in named[0]


def test_an_unreachable_broker_is_one_named_drop_and_a_live_runtime(_isolated, monkeypatch, capsys):
    """A client that connects to nothing: the box's normal broker-down state."""
    _inject_client(monkeypatch, FakeEventsClient(autoconnect=False))
    assert main(["behavior", "engine", "run", "--max-ticks", "5"]) == 0

    named = _nervous_drops(capsys.readouterr().err)
    assert len(named) == 1, named
    assert M.REASON_BROKER_UNREACHABLE in named[0]


def test_a_client_whose_construction_raises_never_breaks_composition(_isolated, monkeypatch):
    """A broken constructor is the same class of fault as a missing package."""

    def _explode(_url):
        raise RuntimeError("no broker library on this box")

    monkeypatch.setattr(behavior_mod, "_import_events_client", lambda: _explode)
    assert behavior_mod._make_events_client() is None
    assert main(["behavior", "engine", "run", "--max-ticks", "3"]) == 0


def test_a_missing_import_spec_resolves_to_no_client():
    """The lazy import is total: an absent module is ``None``, never an ImportError."""
    assert behavior_mod._import_events_client(("reachy_no_such_events_pkg", "Client")) is None
    assert behavior_mod._import_events_client(("json", "NoSuchAttribute")) is None


def test_the_default_import_spec_names_the_events_cli_client():
    """The one-line binding point: when the wheel ships this spec resolves."""
    module_name, attr = behavior_mod.EVENTS_CLIENT_IMPORT
    assert module_name.startswith("events")
    assert attr


def test_the_seam_never_calls_a_blocking_client_method(_isolated, monkeypatch):
    """A ``flush``/``loop``/``wait_for_publish`` on the tick thread would be a
    latency hazard; the trap client fails loudly if composition reaches for one."""
    _inject_client(monkeypatch, BlockingTrapClient())
    assert main(["behavior", "engine", "run", "--max-ticks", "5"]) == 0


def test_shutdown_flips_the_retained_availability_topic_false(_isolated, monkeypatch):
    """``resources.close()`` must stop the publisher, not leak a live session."""
    client = _inject_client(monkeypatch, FakeEventsClient())
    _sense, _seam, resources = _compose()
    resources.close()

    online = client.by_topic("reachy/state/online")
    assert [p.payload for p in online] == [M.ONLINE_PAYLOAD, M.OFFLINE_PAYLOAD]
    assert client.disconnect_calls == 1
    assert client.will is not None
    assert client.will.payload == M.OFFLINE_PAYLOAD


# --------------------------------------------------------------------------- #
# 2. Criterion 2 — additivity                                                 #
# --------------------------------------------------------------------------- #


def _export_run(ticks: int = 4) -> None:
    assert main(["behavior", "engine", "run", "--export", "-", "--max-ticks", str(ticks)]) == 0


def test_h20_the_export_feed_is_identical_with_and_without_the_bus(_isolated, monkeypatch, capsys):
    """The strong form: the broker payloads ARE the stdout lines, verbatim.

    One serializer (:func:`reachy.export.runtime.runtime_to_jsonl`), two
    transports — so the stdout wire contract cannot drift from the bus, and the
    bus cannot add, drop or reorder an event relative to the feed.
    """
    client = _inject_client(monkeypatch, FakeEventsClient())
    _export_run()
    with_bus = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]

    events = [p.payload for p in client.published if p.topic.startswith("reachy/events/")]
    assert with_bus == events, "the bus and the stdout feed disagree"

    _inject_client(monkeypatch, None)
    _export_run()
    without_bus = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]

    assert _stable(with_bus) == _stable(without_bus), "the bus changed the stdout feed"
    assert with_bus, "the feed must not be empty (a baseline sense event fires tick 1)"


def test_h20_stdout_stays_pure_jsonl_with_the_bus_composed(_isolated, monkeypatch, capsys):
    """A publisher drop line must never land on the export feed's stdout."""
    _inject_client(monkeypatch, FakeEventsClient(autoconnect=False))  # forces a drop
    _export_run()
    out, err = capsys.readouterr()
    for line in out.splitlines():
        if line.strip():
            json.loads(line)
    assert "engine stopped" in err


def test_h20_a_bare_run_without_export_writes_nothing_to_stdout(_isolated, monkeypatch, capsys):
    _inject_client(monkeypatch, FakeEventsClient())
    assert main(["behavior", "engine", "run", "--max-ticks", "3"]) == 0
    out, _err = capsys.readouterr()
    assert out == ""


def test_h20_the_sense_snapshot_driver_is_not_composed_without_a_consumer(_isolated, monkeypatch):
    """Zero added per-tick work when nothing can consume it.

    With no events-cli client and no ``--export`` there is no consumer at all,
    so the snapshot driver stays off the bus exactly as before this task.
    """
    from reachy.export.runtime import SenseSnapshotDriver

    _inject_client(monkeypatch, None)
    _sense, tick_seam, resources = _compose()
    try:
        assert not any(isinstance(d, SenseSnapshotDriver) for d in _drivers_of(tick_seam))
    finally:
        resources.close()


def test_the_sense_snapshot_driver_is_composed_once_for_two_consumers(_isolated, monkeypatch):
    """With a client AND ``--export`` there is still exactly ONE snapshot driver."""
    from reachy.export.runtime import SenseSnapshotDriver

    _inject_client(monkeypatch, FakeEventsClient())
    _sense, tick_seam, resources = _compose(runtime_consumer=lambda _e: None)
    try:
        snapshots = [d for d in _drivers_of(tick_seam) if isinstance(d, SenseSnapshotDriver)]
        assert len(snapshots) == 1
    finally:
        resources.close()


def test_sense_events_reach_the_bus_when_a_client_exists_but_export_is_off(_isolated, monkeypatch):
    """The reason the driver is gated on a CONSUMER, not on ``--export``: the
    boot unit has no ``--export``, and a face/transcript flip must still ship."""
    client = _inject_client(monkeypatch, FakeEventsClient())
    assert main(["behavior", "engine", "run", "--max-ticks", "4"]) == 0
    assert any(p.topic == "reachy/events/sense/snapshot" for p in client.published), [
        p.topic for p in client.published
    ]


def test_h8_no_second_sdk_media_session_is_constructed(_isolated, monkeypatch):
    """The single-SDK-owner model survives the new leg: still exactly one client."""
    built: list = []
    real = behavior_mod._make_media_client

    def _spy():
        client = real()
        built.append(client)
        return client

    monkeypatch.setattr(behavior_mod, "_make_media_client", _spy)
    _inject_client(monkeypatch, FakeEventsClient())
    assert main(["behavior", "engine", "run", "--max-ticks", "5"]) == 0
    assert len(built) == 1, f"{len(built)} media clients constructed in one run"


def test_h8_the_publisher_module_reaches_no_sdk_and_no_media() -> None:
    """Static half of h8: the bus touches TickBus events, never the hardware."""
    source = (REPO_ROOT / "reachy/export/mqtt.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "reachy_mini" not in imported
    for banned in ("media_session", "get_frame", "start_recording", "ReachyMini"):
        assert banned not in source, f"the publisher names {banned}"


#: Every module under ``reachy/`` that CALLS ``.audio()`` or ``.get_frame()``.
#: Frozen by equality (h9): a new media reader anywhere — including one smuggled
#: in behind the nervous system — fails here and must be argued for.
_MEDIA_READERS = {
    # the ONE background mic drain (#100) and the ONE camera read
    "reachy/behavior/audio_pump.py",
    "reachy/behavior/transcript_sense.py",
    "reachy/robot/media_client.py",
    "reachy/robot/sdk_transport.py",
    "reachy/vision/producer.py",
    # not a media read: the retired listen loop's hook-chain signature
    "reachy/motion/server.py",
}


def test_h9_no_new_media_audio_or_get_frame_caller_appears() -> None:
    callers = set()
    for path in sorted((REPO_ROOT / "reachy").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("audio", "get_frame"):
                    callers.add(str(path.relative_to(REPO_ROOT)))
    assert callers == _MEDIA_READERS


#: Modules allowed to import ``socket``. Pinned by equality (h10): the runtime is
#: a publishing CLIENT — the broker listens, we never do. The embodiment layer
#: added the two ends of ONE local IPC pipe, both still non-network:
#: ``reachy/behavior/audio_tee.py`` (t4) BINDS a ``AF_UNIX`` sink under the state
#: dir (spec claim c19) — an IPC endpoint on a filesystem path, not a service —
#: and ``reachy/embody/media.py`` (t6) merely CONNECTS to it to read audio.
#: ``reachy/speech/realtime_duplex.py`` (t9) is the layer's own WebSocket
#: session client: it dials the lobes gateway exactly as
#: ``reachy/speech/realtime.py`` has since #115 and, like it, never binds.
#: See :data:`_LOCAL_IPC_LISTENERS` for the price of the one bind exemption.
_SOCKET_IMPORTERS = {
    "reachy/speech/realtime.py",
    "reachy/speech/realtime_duplex.py",
    "reachy/behavior/audio_tee.py",
    "reachy/embody/media.py",
}

#: The ONE file allowed to ``bind``/``listen``. The claim h10 stands for is "no
#: NETWORK server code", and a unix-domain socket is not one — so rather than
#: loosen the token ban for everyone, the exemption is one named file and it is
#: paid for by :func:`test_h10_the_only_listener_is_a_local_unix_socket`, which
#: asserts something STRICTLY STRONGER for that file than the token scan could:
#: every socket it opens is ``AF_UNIX``, and no network family appears at all.
#: The reader end (``reachy/embody/media.py``) is deliberately NOT here: it
#: connects only, so the unconditional bind/listen scan must stay clean for it.
_LOCAL_IPC_LISTENERS = {"reachy/behavior/audio_tee.py"}

_SERVER_TOKENS = re.compile(
    r"\b(socketserver|http\.server|serve_forever|create_server|start_server|SO_REUSEADDR)\b"
    r"|\.bind\(|\.listen\("
)


def test_h10_the_repo_still_contains_no_server_code() -> None:
    """The no-server-code fact must stay grep-true after the nervous-system leg."""
    offenders: list[str] = []
    importers: set[str] = set()
    for path in sorted((REPO_ROOT / "reachy").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(REPO_ROOT))
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("#", '"', "'")):
                continue
            if re.match(r"^(import|from)\s+socket\b", stripped):
                importers.add(rel)
            if _SERVER_TOKENS.search(line) and rel not in _LOCAL_IPC_LISTENERS:
                offenders.append(f"{rel}: {stripped}")
    assert offenders == []
    assert importers == _SOCKET_IMPORTERS


def test_h10_the_only_listener_is_a_local_unix_socket() -> None:
    """The price of the one ``bind``/``listen`` exemption, paid in a stronger check.

    The audio tee listens so the embodiment layer can HEAR without opening a
    second SDK media session (the single-SDK-owner model). What must stay true is
    that it listens on a filesystem path and nowhere else: no ``AF_INET``, no
    port, nothing a remote host could reach. Asserted over the AST — every
    ``socket.socket(...)`` in the file names ``AF_UNIX`` as its family — plus a
    source scan for any network family name at all.
    """
    for rel in sorted(_LOCAL_IPC_LISTENERS):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        families = {
            node.args[0].attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "socket"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
        }
        assert families == {"AF_UNIX"}, f"{rel} opens non-unix sockets: {sorted(families)}"
        for banned in ("AF_INET", "AF_INET6", "AF_NETLINK", "getaddrinfo", "gethostbyname"):
            assert banned not in source, f"{rel} names {banned} — that is a network listener"


_MQTT_LIBRARIES = ("paho", "gmqtt", "amqtt", "asyncio-mqtt", "hbmqtt")


def test_h10_no_mqtt_library_became_a_direct_dependency() -> None:
    """This repo imports the events-cli client; it never speaks MQTT itself.

    Stated precisely, because ``events-cli`` landing as a base dep DID bring an
    MQTT library into the resolved tree: ``paho-mqtt`` is a base dependency *of
    events-cli*. That is the recorded 2026-07-24 decision working as intended —
    "the transport underneath is events-cli's internal concern once outside this
    repo" — not a breach of it. What must stay true is that *this* repo never
    names one and never imports one, so the transport can be swapped (paho, or a
    no-deps rewrite) without a line changing here.
    """
    import tomllib

    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        project = tomllib.load(fh)["project"]
    assert project["dependencies"] == ["numpy>=1.24", "harmonics-cli>=0.8", "events-cli>=0.9"]
    flat = json.dumps(project.get("optional-dependencies", {})) + json.dumps(
        project["dependencies"]
    )
    for banned in _MQTT_LIBRARIES:
        assert banned not in flat


def test_h10_no_module_in_this_repo_imports_an_mqtt_library() -> None:
    """The stronger form of the same invariant, checked against the source."""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "reachy").rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("#", '"', "'")):
                continue
            if re.match(rf"^(import|from)\s+({'|'.join(_MQTT_LIBRARIES)})\b", stripped):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {stripped}")
    assert offenders == []


def test_exactly_one_module_names_the_vendor_package() -> None:
    """``reachy/export/events_client.py`` is the ONE point of vendor coupling.

    Checked by NAME rather than by import statement, because the binding is
    deliberately lazy (``importlib.import_module`` off a constant) — there is no
    literal ``import events_cli`` anywhere, and a grep for one would pass
    vacuously forever. A second module naming the package would mean the
    vendor's shape had leaked past the adapter, which is exactly what let the
    API mismatch (``is_connected``/``close``, will-at-construction) stay
    invisible until the wheel actually shipped.
    """
    namers = {
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / "reachy").rglob("*.py"))
        # \b matters: the adapter MODULE is `events_client`, and a plain
        # substring test would match every reference to it.
        if re.search(r"\bevents_cli\b", path.read_text(encoding="utf-8"))
    }
    assert namers == {"reachy/export/events_client.py"}


def test_the_composition_binding_is_an_alias_not_a_second_copy() -> None:
    """One constant, aliased — two copies of a vendor path drift apart."""
    from reachy.export import events_client

    assert behavior_mod.EVENTS_CLIENT_IMPORT is events_client.VENDOR_IMPORT


def test_the_state_json_mirror_is_additive_to_the_disk_write(_isolated, monkeypatch):
    """The disk write happens FIRST and unconditionally; the bus is the mirror."""
    from reachy.behavior import control

    client = _inject_client(monkeypatch, FakeEventsClient())
    assert main(["behavior", "engine", "run", "--max-ticks", "5"]) == 0

    on_disk = control.read_state()
    assert isinstance(on_disk, dict)
    assert "ownership" in on_disk

    retained = {
        p.topic.rsplit("/", 1)[-1]: p.payload
        for p in client.published
        if p.topic.startswith("reachy/state/") and p.retain
    }
    assert retained, "no retained state reached the bus"
    for key in ("ownership", "active", "compose_hz"):
        assert key in retained
        assert json.loads(retained[key]) == on_disk[key]


def test_the_retained_state_tree_equals_state_json_including_rider_keys(_isolated, monkeypatch):
    """h21/c36, the positive form: the two surfaces MIRROR each other, byte-for-byte.

    This replaces t7's known-gap pin. The gap was that the two seam riders
    (``SenseAvailabilityDriver`` -> ``senses``, ``IntentDriver`` -> ``intents``)
    read-modify-wrote ``state.json`` through their OWN spool, so those keys
    reached disk but never the bus. t14 injects the engine's
    ``state_writer``-wrapped spool INSTANCE into both riders as their
    ``main_control``, so every merged write now flows through the same wrapped
    writer.

    The assertion is strict EQUALITY, not a subset: the retained
    ``reachy/state/*`` tree a late subscriber would reassemble (minus the
    publisher-owned ``online`` key) equals the on-disk ``state.json`` — with
    ``senses`` and ``intents`` present on BOTH. One builder, two transports, no
    drift.
    """
    from reachy.behavior import control
    from reachy.behavior.sense_availability import STATE_KEY

    client = _inject_client(monkeypatch, FakeEventsClient())
    assert main(["behavior", "engine", "run", "--max-ticks", "5"]) == 0

    on_disk = control.read_state()
    assert isinstance(on_disk, dict)
    # The two rider keys are the whole point — assert they are on disk before
    # comparing, so a rider silently going dark reads as a clear failure.
    assert STATE_KEY in on_disk, "the availability rider stopped writing its block"
    assert "intents" in on_disk, "the intent rider stopped writing its view"

    assert _retained_state_tree(client) == on_disk


def test_state_json_still_carries_the_full_three_way_disk_merge(_isolated, monkeypatch):
    """No regression: the three writers still merge onto ONE file, all keys intact.

    The engine snapshot (``updated``/``compose_hz``/``active``/``ownership``/
    ``doa``), the availability rider (``senses``) and the intent rider
    (``intents``) all land in the SAME ``state.json`` — injecting the shared
    spool changed WHERE their writes are also mirrored, never WHETHER they reach
    disk.
    """
    from reachy.behavior import control

    _inject_client(monkeypatch, FakeEventsClient())
    assert main(["behavior", "engine", "run", "--max-ticks", "5"]) == 0

    on_disk = control.read_state()
    assert set(on_disk) >= {
        "updated",
        "compose_hz",
        "active",
        "ownership",
        "doa",
        "senses",
        "intents",
    }


def test_the_probe_path_mirrors_only_the_engine_snapshot(_isolated, monkeypatch, tmp_path):
    """Hazard 2, empirically: the observation-only probe composes NO riders.

    ``--probe-mode`` is still an engine run, so the engine's own snapshot is
    mirrored (it writes through the same wrapped spool). But the probe seam omits
    ``IntentDriver`` and ``SenseAvailabilityDriver`` entirely, so ``senses`` and
    ``intents`` have no writer and can never reach the retained tree there. The
    ``main_control`` injection must not change that.
    """
    client = _inject_client(monkeypatch, FakeEventsClient())
    output = tmp_path / "probe.jsonl"
    assert (
        main(
            [
                "behavior",
                "engine",
                "run",
                "--probe-mode",
                "held",
                "--probe-output",
                str(output),
                "--max-ticks",
                "5",
            ]
        )
        == 0
    )
    retained = _retained_state_tree(client)
    assert "ownership" in retained, "the engine snapshot must still be mirrored on the probe path"
    assert "senses" not in retained
    assert "intents" not in retained


def test_the_probe_seam_composes_no_state_riders(_isolated, monkeypatch):
    """The composition-level companion to the run above (hazard 2).

    Directly asserts the probe seam carries neither state rider, so the injection
    change cannot silently push a rider — and its own spool — onto the probe
    path.
    """
    from reachy.behavior.intents import IntentDriver
    from reachy.behavior.sense_availability import SenseAvailabilityDriver

    _inject_client(monkeypatch, FakeEventsClient())
    _sense, tick_seam, resources = _compose(probe=("held", lambda record: None))
    try:
        drivers = _drivers_of(tick_seam)
        offenders = [
            type(d).__name__
            for d in drivers
            if isinstance(d, (IntentDriver, SenseAvailabilityDriver))
        ]
        assert offenders == [], offenders
    finally:
        resources.close()


class _MinimalIntentCtx:
    """The smallest ``ctx`` ``IntentDriver.on_tick`` touches with an empty spool,
    no standing goal and no inhibitions — it never dereferences these, but a
    stub keeps the tick honest rather than relying on that."""

    now = 0.0
    tick = 0

    def active_names(self):
        return []

    def emit(self, _event):
        return None


def test_riders_pick_up_a_state_writer_patched_after_their_construction(tmp_path, monkeypatch):
    """Hazard 1, proven not assumed: the patch lands AFTER the riders are built.

    ``cmd_engine_run`` constructs the riders inside ``_compose_run_seam`` and
    only THEN patches ``spool.write_state`` with the publisher's
    ``state_writer``. Because each rider looks up ``self._main.write_state`` at
    TICK time (never captures a bound method at construction), a patch applied
    after construction is still picked up. A rider that snapshotted the writer
    early would miss it — and this test would fail. Asserted against a REAL
    ``NervousPublisher`` + fake client, so the merged payloads genuinely reach
    the bus.
    """
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    from reachy.behavior import control
    from reachy.behavior.intents import IntentDriver
    from reachy.behavior.sense_availability import (
        STATE_KEY,
        SenseAvailabilityDriver,
        runtime_probes,
    )
    from reachy.export.mqtt import NervousPublisher

    spool = control.CommandSpool()
    # Build BOTH riders holding the shared spool, while write_state is still the
    # plain bound method.
    availability = SenseAvailabilityDriver(
        runtime_probes(pat_composed=True, face_recognizer_ready=False),
        main_control=spool,
    )
    intents = IntentDriver(main_control=spool)

    # Now patch the instance, exactly as cmd_engine_run does — AFTER construction.
    client = FakeEventsClient()
    publisher = NervousPublisher(client)
    publisher.start()
    spool.write_state = publisher.state_writer(spool.write_state)
    try:
        availability(None)
        intents.on_tick(_MinimalIntentCtx())
    finally:
        publisher.stop()

    retained = _retained_state_tree(client)
    assert STATE_KEY in retained, "availability write did not reach the post-patch writer"
    assert "intents" in retained, "intents write did not reach the post-patch writer"


def test_the_state_mirror_survives_a_dead_bus(_isolated, monkeypatch):
    """A degraded bus must never cost the runtime its state file."""
    from reachy.behavior import control

    _inject_client(monkeypatch, None)
    assert main(["behavior", "engine", "run", "--max-ticks", "5"]) == 0
    assert isinstance(control.read_state(), dict)


# --------------------------------------------------------------------------- #
# 3. Criterion 3 — REACHY_MQTT_URL reaches the client seam                    #
# --------------------------------------------------------------------------- #


def _record_urls(monkeypatch) -> list[str]:
    seen: list[str] = []

    def _factory(url):
        seen.append(url)
        return FakeEventsClient()

    monkeypatch.setattr(behavior_mod, "_import_events_client", lambda: _factory)
    return seen


def test_the_default_broker_url_reaches_the_client_seam(monkeypatch):
    monkeypatch.delenv(M.BROKER_URL_ENV, raising=False)
    seen = _record_urls(monkeypatch)
    assert behavior_mod._make_events_client() is not None
    assert seen == [M.DEFAULT_BROKER_URL] == ["localhost:1883"]


def test_reachy_mqtt_url_reaches_the_client_seam(monkeypatch):
    monkeypatch.setenv(M.BROKER_URL_ENV, "10.0.0.9:1884")
    seen = _record_urls(monkeypatch)
    assert behavior_mod._make_events_client() is not None
    assert seen == ["10.0.0.9:1884"]


def test_reachy_mqtt_url_is_read_at_composition_not_at_import(_isolated, monkeypatch):
    """Read per run, like every other ``REACHY_*`` knob this module resolves."""
    seen = _record_urls(monkeypatch)
    monkeypatch.setenv(M.BROKER_URL_ENV, "first:1883")
    _sense, _seam, resources = _compose()
    resources.close()
    monkeypatch.setenv(M.BROKER_URL_ENV, "second:1883")
    _sense, _seam, resources = _compose()
    resources.close()
    assert seen == ["first:1883", "second:1883"]


# --------------------------------------------------------------------------- #
# 4. Criterion 4 — TickMetrics.close() is wired into shutdown (t1 hand-off)   #
# --------------------------------------------------------------------------- #


class _Ctx:
    tick = 7


def _overrunning_metrics() -> TickMetrics:
    """A metrics wrapper with ONE overrun episode left open."""
    readings = iter([0.0, 1.0])  # 1000 ms against a 20 ms budget
    metrics = TickMetrics(lambda _ctx: None, budget_s=0.02, duration_clock=lambda: next(readings))
    metrics(_Ctx())
    assert metrics.overruns == 1
    return metrics


def test_runtime_resources_close_flushes_an_open_overrun_episode(caplog):
    metrics = _overrunning_metrics()
    resources = behavior_mod._RuntimeResources(metrics=metrics)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        resources.close()
    lines = [r.getMessage() for r in caplog.records]
    assert any(EVENT_OVERRUN_SUMMARY in line for line in lines), lines


def test_runtime_resources_close_is_still_idempotent(caplog):
    metrics = _overrunning_metrics()
    resources = behavior_mod._RuntimeResources(metrics=metrics)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        resources.close()
        resources.close()
    summaries = [r.getMessage() for r in caplog.records if EVENT_OVERRUN_SUMMARY in r.getMessage()]
    assert len(summaries) == 1


def test_a_raising_metrics_flush_does_not_skip_the_rest_of_teardown():
    """One failing teardown step must never strand a held client."""

    class _Sick:
        def close(self):
            raise RuntimeError("flush exploded")

    closed: list[str] = []

    class _Holder:
        def close(self):
            closed.append("media")

    behavior_mod._RuntimeResources(metrics=_Sick(), media=_Holder()).close()
    assert closed == ["media"]


def test_the_composed_seam_is_the_metrics_object_that_gets_flushed(_isolated, monkeypatch):
    """Composition must hand ``close()`` the SAME wrapper it handed the engine."""
    _inject_client(monkeypatch, FakeEventsClient())
    _sense, tick_seam, resources = _compose()
    try:
        assert isinstance(tick_seam, TickMetrics)
        assert resources.metrics is tick_seam
    finally:
        resources.close()


def test_the_probe_seam_is_flushed_too(_isolated, monkeypatch):
    _inject_client(monkeypatch, FakeEventsClient())
    _sense, tick_seam, resources = _compose(probe=("held", lambda record: None))
    try:
        assert resources.metrics is tick_seam
    finally:
        resources.close()


def test_a_permanently_disabled_publisher_does_not_cost_a_tick(_isolated, monkeypatch):
    """A client that exists but was disabled at start() must not be fed.

    `publisher.start()` hard-disables publishing for the whole run when the
    client is incompatible or its `connect()` raises. Gating the snapshot driver
    on "a client object exists" kept paying a tick to build snapshots that were
    then dropped — the one degradation mode where the cost is pure waste. The
    gate reads `publishing_enabled` instead, which answers "could this ever
    publish again?"
    """
    client = FakeEventsClient()
    client.raise_on_connect = RuntimeError("broker library exploded")
    _inject_client(monkeypatch, client)

    publisher = behavior_mod._make_nervous_publisher()
    assert publisher.publishing_enabled is False, "a raising connect disables the run"
    assert main(["behavior", "engine", "run", "--max-ticks", "3"]) == 0


def test_a_broker_that_is_merely_not_up_yet_still_earns_its_tick(_isolated, monkeypatch):
    """The distinction that makes the gate correct rather than merely cheaper.

    "Not connected yet" is NOT "disabled": a session may land on any later tick,
    and a runtime that stopped producing snapshots in the meantime would have
    nothing to publish when it did.
    """
    client = FakeEventsClient(autoconnect=False)
    _inject_client(monkeypatch, client)

    publisher = behavior_mod._make_nervous_publisher()
    assert publisher.publishing_enabled is True, "a quiet broker is not a disabled one"
    assert publisher.degraded is True, "…though it IS degraded until a session lands"
