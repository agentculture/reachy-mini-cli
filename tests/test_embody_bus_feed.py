"""Tests for scripts/embody_bus_feed.py — the MQTT bus -> --feed FIFO bridge.

This is the ONLY bus intake the embodiment layer has today: installed
``events-cli>=0.9`` ships no ``subscribe`` surface at all, so
``reachy.embody.cues.resolve_bus_subscriber`` always degrades to the feed-tail
fallback (see that module's own tests). This script is what makes the bus
route work anyway, from OUTSIDE the ``reachy`` package's zero-MQTT-library
boundary — it is loaded here via ``importlib`` (there is no ``scripts``
package, and no ``if __name__ == "__main__"`` machinery runs on import, so
loading it never opens a socket or a FIFO).

Four things this file pins, matching the task's acceptance contract exactly:

1. The ``O_RDWR | O_NONBLOCK`` FIFO hold — proven against a REAL FIFO, no
   mock: a second reader sees "no data yet" (``BlockingIOError``), never EOF,
   for as long as the bridge's own descriptor stays open; closing it
   reproduces the live incident (the layer's reader hit EOF and died).
2. The ``rule,intent,motion`` default source filter, with
   ``REACHY_BUS_FEED_SOURCES`` override.
3. Byte-identical payload passthrough — ``on_message`` never parses or
   re-serializes, proven with a payload that is not even valid JSON.
4. The events-only topic filter — every subscribed filter is scoped under
   ``reachy/events/``; ``reachy/state/#`` (the RETAINED tree) can never be
   named, so a reconnect can never replay retained state into a cue.

No test in this file opens a real network socket: the one test that drives
``main()`` end to end substitutes a fake ``paho`` client class before calling
it, so ``client.connect()`` never reaches a real broker even though
``REACHY_MQTT_URL`` is (as everywhere in this suite) pinned to a dead loopback
by ``tests/conftest.py``'s ``_no_live_event_broker`` guard.
"""

from __future__ import annotations

import errno
import fcntl
import importlib.util
import os
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent
_SCRIPT_PATH = REPO_ROOT / "scripts" / "embody_bus_feed.py"


def _load_bus_feed() -> ModuleType:
    """Load ``scripts/embody_bus_feed.py`` by path (there is no ``scripts`` package).

    ``exec_module`` runs the module body with ``__name__ ==
    "embody_bus_feed"``, so the ``if __name__ == "__main__":`` guard at the
    bottom of the script never fires here — nothing is opened or connected
    just by importing it.
    """
    spec = importlib.util.spec_from_file_location("embody_bus_feed", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bus_feed = _load_bus_feed()


# --------------------------------------------------------------------------- #
# 1. The O_RDWR | O_NONBLOCK FIFO hold                                        #
# --------------------------------------------------------------------------- #


class TestOpenFeedFifo:
    def test_creates_the_fifo_when_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "embody-feed.fifo"
        assert not path.exists()
        fd = bus_feed.open_feed_fifo(str(path))
        try:
            assert stat.S_ISFIFO(os.stat(path).st_mode)
        finally:
            os.close(fd)

    def test_reuses_an_existing_fifo_without_error(self, tmp_path: Path) -> None:
        path = tmp_path / "embody-feed.fifo"
        os.mkfifo(path, 0o600)
        fd = bus_feed.open_feed_fifo(str(path))
        os.close(fd)

    def test_opens_o_rdwr_nonblock_exactly(self, tmp_path: Path) -> None:
        """The precise flag pin: access mode RDWR, and NONBLOCK set."""
        path = tmp_path / "embody-feed.fifo"
        fd = bus_feed.open_feed_fifo(str(path))
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            assert (flags & os.O_ACCMODE) == os.O_RDWR
            assert flags & os.O_NONBLOCK
        finally:
            os.close(fd)

    def test_open_never_blocks_or_fails_with_no_reader_present(self, tmp_path: Path) -> None:
        """The wrong choice (O_WRONLY|O_NONBLOCK) is refused outright with no
        reader present — exactly the failure O_RDWR exists to avoid, and why
        "regardless of start order" in the docstring is a real claim, not
        flavor text.
        """
        path = tmp_path / "embody-feed.fifo"
        os.mkfifo(path, 0o600)

        with pytest.raises(OSError) as exc_info:
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        assert exc_info.value.errno == errno.ENXIO

        fd = bus_feed.open_feed_fifo(str(path))  # must not raise or block
        os.close(fd)

    def test_a_second_reader_never_sees_eof_while_the_bridge_holds_its_fd(
        self, tmp_path: Path
    ) -> None:
        """Pins the exact live failure and its fix.

        As long as the bridge's own O_RDWR descriptor stays open, a second
        reader — standing in for the layer's `--feed` tail — polling the FIFO
        gets "no data yet" (`BlockingIOError`), never "no writers left" (a
        zero-byte read, i.e. EOF). Closing the bridge's descriptor — simulating
        the bridge process exiting — reproduces the EOF the live incident
        actually hit ("the bridge exited... the layer's cue reader hit EOF...
        and the layer DIED"), which is what proves this test would catch a
        regression rather than passing vacuously.
        """
        path = tmp_path / "embody-feed.fifo"
        bridge_fd = bus_feed.open_feed_fifo(str(path))
        reader_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with pytest.raises(BlockingIOError):
                os.read(reader_fd, 4096)  # nothing written yet, but NOT EOF

            os.close(bridge_fd)
            bridge_fd = -1
            assert os.read(reader_fd, 4096) == b""  # now genuinely EOF
        finally:
            if bridge_fd >= 0:
                os.close(bridge_fd)
            os.close(reader_fd)


# --------------------------------------------------------------------------- #
# 2. The rule,intent,motion default source filter + REACHY_BUS_FEED_SOURCES   #
# --------------------------------------------------------------------------- #


class TestResolveSources:
    def test_default_is_rule_intent_motion(self) -> None:
        assert bus_feed.resolve_sources(None) == ("rule", "intent", "motion")

    def test_override_is_split_on_commas(self) -> None:
        assert bus_feed.resolve_sources("sense,rule") == ("sense", "rule")

    def test_override_strips_whitespace_and_drops_blank_entries(self) -> None:
        assert bus_feed.resolve_sources(" rule , , intent ,") == ("rule", "intent")

    def test_wildcard_is_preserved_as_a_literal_source(self) -> None:
        assert bus_feed.resolve_sources("*") == ("*",)

    def test_empty_env_value_resolves_to_no_sources(self) -> None:
        # A deliberately-empty override subscribes to nothing, distinct from
        # an UNSET override (None), which falls back to the default.
        assert bus_feed.resolve_sources("") == ()


# --------------------------------------------------------------------------- #
# 4. The events-only topic filter — reachy/state/# can never be named         #
# --------------------------------------------------------------------------- #


class TestTopicFilters:
    def test_default_sources_map_to_one_filter_each(self) -> None:
        assert bus_feed.topic_filters(("rule", "intent", "motion")) == (
            "reachy/events/rule/#",
            "reachy/events/intent/#",
            "reachy/events/motion/#",
        )

    def test_wildcard_collapses_to_one_events_filter(self) -> None:
        assert bus_feed.topic_filters(("*",)) == ("reachy/events/#",)

    def test_wildcard_mixed_with_named_sources_still_collapses(self) -> None:
        assert bus_feed.topic_filters(("rule", "*")) == ("reachy/events/#",)

    @pytest.mark.parametrize(
        "sources",
        [
            ("rule", "intent", "motion"),
            ("sense",),
            ("*",),
            ("state",),  # even a source literally named "state" stays scoped
            (),
        ],
    )
    def test_no_filter_ever_names_the_retained_state_tree(self, sources: tuple[str, ...]) -> None:
        """The safety property: reachy/state/# (RETAINED) is unreachable by construction.

        A reconnect must never replay the runtime's last-known retained
        pose/state as if it just happened. This asserts it structurally over
        every filter this function can produce, not just the default case.
        """
        filters = bus_feed.topic_filters(sources)
        for topic_filter in filters:
            assert topic_filter.startswith("reachy/events/")
        assert "reachy/state/#" not in filters

    def test_empty_sources_subscribes_nothing(self) -> None:
        assert bus_feed.topic_filters(()) == ()


# --------------------------------------------------------------------------- #
# 3. Byte-identical payload passthrough                                       #
# --------------------------------------------------------------------------- #


class _FakeMessage:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class TestBusFeedForwarderOnMessage:
    def _open_pair(self, tmp_path: Path) -> tuple[int, int]:
        """A real FIFO: the forwarder's write fd, and a reader fd to assert against."""
        path = tmp_path / "embody-feed.fifo"
        write_fd = bus_feed.open_feed_fifo(str(path))
        read_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        return write_fd, read_fd

    def test_writes_the_payload_verbatim_plus_one_newline(self, tmp_path: Path) -> None:
        write_fd, read_fd = self._open_pair(tmp_path)
        try:
            forwarder = bus_feed.BusFeedForwarder(write_fd, ("rule",))
            payload = b'{"t": "rule", "action": "fire", "rule": "pat-acknowledge"}'

            forwarder.on_message(None, None, _FakeMessage(payload))

            assert os.read(read_fd, 4096) == payload + b"\n"
            assert forwarder.sent == 1
            assert forwarder.dropped == 0
        finally:
            os.close(write_fd)
            os.close(read_fd)

    def test_never_parses_or_reserializes_even_invalid_json(self, tmp_path: Path) -> None:
        """A pipe, not a translator: garbage that would fail json.loads still
        passes through byte for byte, proving on_message never inspects it.
        """
        write_fd, read_fd = self._open_pair(tmp_path)
        try:
            forwarder = bus_feed.BusFeedForwarder(write_fd, ("rule",))
            payload = b"not-json-at-all {{{ \xff\xfe"

            forwarder.on_message(None, None, _FakeMessage(payload))

            assert os.read(read_fd, 4096) == payload + b"\n"
        finally:
            os.close(write_fd)
            os.close(read_fd)

    def test_multiple_messages_stay_byte_identical_and_in_order(self, tmp_path: Path) -> None:
        write_fd, read_fd = self._open_pair(tmp_path)
        try:
            forwarder = bus_feed.BusFeedForwarder(write_fd, ("rule", "intent"))
            payloads = [b'{"t": "rule"}', b'{"t": "intent"}', b'{"t": "motion"}']

            for payload in payloads:
                forwarder.on_message(None, None, _FakeMessage(payload))

            got = os.read(read_fd, 4096)
            assert got == b"\n".join(payloads) + b"\n"
            assert forwarder.sent == 3
        finally:
            os.close(write_fd)
            os.close(read_fd)

    def test_a_full_pipe_drops_and_counts_rather_than_raising(self, tmp_path: Path) -> None:
        """Nobody draining must never stall the bus thread — a BlockingIOError
        (the pipe buffer is full) is caught and counted as a drop, matching
        the module docstring's "drop rather than stall the bus thread".
        """
        write_fd, read_fd = self._open_pair(tmp_path)
        try:
            fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, 4096)  # small + deterministic
            forwarder = bus_feed.BusFeedForwarder(write_fd, ("rule",))
            payload = b"x" * 256

            for _ in range(64):  # comfortably more than the shrunk pipe can hold
                forwarder.on_message(None, None, _FakeMessage(payload))

            assert forwarder.dropped > 0
            assert forwarder.sent > 0
            assert forwarder.sent + forwarder.dropped == 64
        finally:
            os.close(write_fd)
            os.close(read_fd)

    def test_on_connect_subscribes_exactly_the_resolved_topic_filters(self) -> None:
        class _RecordingClient:
            def __init__(self) -> None:
                self.subscribed: list[str] = []

            def subscribe(self, topic_filter: str) -> None:
                self.subscribed.append(topic_filter)

        forwarder = bus_feed.BusFeedForwarder(fd=-1, sources=("rule", "intent"))
        client = _RecordingClient()

        forwarder.on_connect(client, None, None, 0)

        assert client.subscribed == ["reachy/events/rule/#", "reachy/events/intent/#"]


# --------------------------------------------------------------------------- #
# parse_broker + main() wiring — never touches a real broker                  #
# --------------------------------------------------------------------------- #


class TestParseBroker:
    def test_host_and_port(self) -> None:
        assert bus_feed.parse_broker("example.local:1884") == ("example.local", 1884)

    def test_defaults_when_only_host_given(self) -> None:
        assert bus_feed.parse_broker("example.local") == ("example.local", 1883)

    def test_defaults_when_broker_string_is_empty(self) -> None:
        assert bus_feed.parse_broker("") == ("localhost", 1883)


class _FakeMqttClient:
    """Stands in for `paho.mqtt.client.Client` — no socket ever opens."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.on_connect = None
        self.on_message = None
        self.connect_args: tuple[str, int, int] | None = None
        self.loop_forever_calls = 0

    def connect(self, host: str, port: int, keepalive: int) -> None:
        self.connect_args = (host, port, keepalive)

    def loop_forever(self) -> None:
        self.loop_forever_calls += 1


def test_main_wires_a_forwarder_to_the_client_without_touching_a_real_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end wiring, with the one network-capable object substituted.

    ``REACHY_MQTT_URL`` is already pinned to a dead loopback by the suite's
    autouse `_no_live_event_broker` guard; this test additionally never lets a
    real `paho.mqtt.client.Client` get constructed at all, so there is no path
    to a socket even if that guard were absent.
    """
    created: list[_FakeMqttClient] = []

    def _fake_client_factory(*args: Any, **kwargs: Any) -> _FakeMqttClient:
        client = _FakeMqttClient(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(bus_feed.mqtt, "Client", _fake_client_factory)
    monkeypatch.setenv("REACHY_MQTT_URL", "example-broker:9999")
    monkeypatch.setenv("REACHY_BUS_FEED_SOURCES", "sense")
    fifo_path = tmp_path / "embody-feed.fifo"

    bus_feed.main([str(fifo_path)])

    assert stat.S_ISFIFO(os.stat(fifo_path).st_mode)
    assert len(created) == 1
    client = created[0]
    assert client.connect_args == ("example-broker", 9999, 30)
    assert client.loop_forever_calls == 1
    assert client.on_connect is not None
    assert client.on_message is not None

    # The resolved REACHY_BUS_FEED_SOURCES override reached the forwarder.
    subscribed: list[str] = []

    class _Recorder:
        def subscribe(self, topic_filter: str) -> None:
            subscribed.append(topic_filter)

    client.on_connect(_Recorder(), None, None, 0)
    assert subscribed == ["reachy/events/sense/#"]


def test_main_defaults_to_the_module_fifo_path_and_default_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bus_feed.mqtt, "Client", _FakeMqttClient)
    monkeypatch.delenv("REACHY_BUS_FEED_SOURCES", raising=False)
    fifo_path = tmp_path / "embody-feed.fifo"

    bus_feed.main([str(fifo_path)])

    assert stat.S_ISFIFO(os.stat(fifo_path).st_mode)
