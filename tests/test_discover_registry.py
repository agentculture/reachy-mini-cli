"""Tests for reachy.discover.registry — the per-user remembered-unit registry.

Acceptance criteria covered (one section each):

1. Records persist to state_dir()/units.json keyed by hardware_id, holding
   mac, last_ip, name, model, wireless and last_seen.
2. A missing, empty, truncated and syntactically invalid file each load as an
   empty registry without raising.
3. Two interleaved writers always leave valid parseable JSON holding one
   writer's complete state, never a truncated or merged file.
4. MAC enrichment returns None when the neighbour table is unavailable or the
   host is off-segment, and the record remains valid and identifiable without
   it.

Every test is isolated via ``tmp_path`` (injected ``path=``) or
``REACHY_STATE_DIR`` (for the default-path test) so the suite never touches a
developer's real state dir, and MAC lookup tests inject a fake ``run``/
``arp_path`` seam so the suite never shells out to a real ``ip neigh``.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from reachy.discover.registry import (
    REGISTRY_FILENAME,
    RegistryRecord,
    UnitRegistry,
    default_registry_path,
    lookup_mac,
)

# ---------------------------------------------------------------------------
# Shared fixture data
# ---------------------------------------------------------------------------

REAL_HARDWARE_ID = "a89063c05ae79779"
REAL_MAC = "88:a2:9e:8c:fa:bf"


def _record(**overrides) -> RegistryRecord:
    fields = {
        "hardware_id": REAL_HARDWARE_ID,
        "mac": REAL_MAC,
        "last_ip": "192.168.1.162",
        "name": "reachy_mini",
        "model": "Reachy Mini Wireless",
        "wireless": True,
        "last_seen": "2026-08-08T12:00:00+00:00",
    }
    fields.update(overrides)
    return RegistryRecord(**fields)


# ---------------------------------------------------------------------------
# Criterion 1 -- records persist, keyed by hardware_id, holding every field
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_upsert_then_get_round_trips_every_field(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        record = _record()

        registry.upsert(record)
        fetched = registry.get(REAL_HARDWARE_ID)

        assert fetched == record

    def test_records_persist_to_disk_keyed_by_hardware_id(self, tmp_path):
        path = tmp_path / "units.json"
        registry = UnitRegistry(path=path)
        registry.upsert(_record())

        raw = json.loads(path.read_text(encoding="utf-8"))

        assert REAL_HARDWARE_ID in raw["units"]
        stored = raw["units"][REAL_HARDWARE_ID]
        assert stored["mac"] == REAL_MAC
        assert stored["last_ip"] == "192.168.1.162"
        assert stored["name"] == "reachy_mini"
        assert stored["model"] == "Reachy Mini Wireless"
        assert stored["wireless"] is True
        assert stored["last_seen"] == "2026-08-08T12:00:00+00:00"

    def test_a_fresh_registry_instance_loads_the_persisted_record(self, tmp_path):
        path = tmp_path / "units.json"
        UnitRegistry(path=path).upsert(_record())

        fetched = UnitRegistry(path=path).get(REAL_HARDWARE_ID)

        assert fetched == _record()

    def test_all_returns_every_stored_record(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())
        registry.upsert(_record(hardware_id="deadbeef01", last_ip="192.168.1.157", wireless=False))

        records = {r.hardware_id for r in registry.all()}

        assert records == {REAL_HARDWARE_ID, "deadbeef01"}

    def test_upsert_overwrites_the_existing_record_for_the_same_hardware_id(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record(last_ip="192.168.1.162"))

        registry.upsert(_record(last_ip="192.168.1.200"))

        assert registry.get(REAL_HARDWARE_ID).last_ip == "192.168.1.200"
        assert len(registry.all()) == 1

    def test_get_on_unknown_hardware_id_returns_none(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")

        assert registry.get("never-seen") is None

    def test_forget_removes_the_record_and_reports_it_existed(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())

        removed = registry.forget(REAL_HARDWARE_ID)

        assert removed is True
        assert registry.get(REAL_HARDWARE_ID) is None

    def test_forget_on_unknown_hardware_id_is_a_no_op_reporting_false(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())

        removed = registry.forget("never-seen")

        assert removed is False
        assert registry.get(REAL_HARDWARE_ID) is not None

    def test_alias_defaults_to_none_and_round_trips_when_set(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        registry.upsert(_record())
        assert registry.get(REAL_HARDWARE_ID).alias is None

        registry.upsert(_record(alias="bench"))

        assert registry.get(REAL_HARDWARE_ID).alias == "bench"

    def test_record_is_a_frozen_dataclass(self):
        record = _record()
        with pytest.raises(FrozenInstanceError):
            record.last_ip = "10.0.0.1"

    def test_default_path_lives_under_the_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))

        registry = UnitRegistry()

        assert registry.path == tmp_path / REGISTRY_FILENAME
        assert default_registry_path() == tmp_path / REGISTRY_FILENAME

    def test_load_and_save_are_available_as_explicit_public_operations(self, tmp_path):
        path = tmp_path / "units.json"
        registry = UnitRegistry(path=path)

        records = registry.load()
        records[REAL_HARDWARE_ID] = _record()
        registry.save(records)

        assert UnitRegistry(path=path).get(REAL_HARDWARE_ID) == _record()


# ---------------------------------------------------------------------------
# Criterion 2 -- missing / empty / truncated / invalid all degrade to empty
# ---------------------------------------------------------------------------


class TestDegradesToEmptyNeverRaises:
    def test_missing_file_loads_as_empty(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")

        assert registry.all() == []
        assert registry.load() == {}

    def test_empty_file_loads_as_empty(self, tmp_path):
        path = tmp_path / "units.json"
        path.write_text("", encoding="utf-8")

        registry = UnitRegistry(path=path)

        assert registry.all() == []

    def test_truncated_file_loads_as_empty(self, tmp_path):
        path = tmp_path / "units.json"
        full = json.dumps({"version": 1, "units": {REAL_HARDWARE_ID: _record().to_dict()}})
        path.write_text(full[: len(full) // 2], encoding="utf-8")

        registry = UnitRegistry(path=path)

        assert registry.all() == []

    def test_syntactically_invalid_json_loads_as_empty(self, tmp_path):
        path = tmp_path / "units.json"
        path.write_text("{not valid json at all", encoding="utf-8")

        registry = UnitRegistry(path=path)

        assert registry.all() == []

    def test_unexpected_top_level_shape_loads_as_empty(self, tmp_path):
        path = tmp_path / "units.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        registry = UnitRegistry(path=path)

        assert registry.all() == []

    def test_wrong_version_loads_as_empty(self, tmp_path):
        path = tmp_path / "units.json"
        path.write_text(
            json.dumps({"version": 999, "units": {REAL_HARDWARE_ID: _record().to_dict()}}),
            encoding="utf-8",
        )

        registry = UnitRegistry(path=path)

        assert registry.all() == []

    def test_one_unreadable_record_is_dropped_not_fatal(self, tmp_path):
        path = tmp_path / "units.json"
        good = _record(hardware_id="good-unit").to_dict()
        bad = {"hardware_id": "bad-unit"}  # missing required fields
        path.write_text(
            json.dumps({"version": 1, "units": {"good-unit": good, "bad-unit": bad}}),
            encoding="utf-8",
        )

        registry = UnitRegistry(path=path)

        hardware_ids = {r.hardware_id for r in registry.all()}
        assert hardware_ids == {"good-unit"}

    def test_a_write_after_a_corrupt_read_recovers_cleanly(self, tmp_path):
        path = tmp_path / "units.json"
        path.write_text("not json", encoding="utf-8")
        registry = UnitRegistry(path=path)

        registry.upsert(_record())

        assert UnitRegistry(path=path).get(REAL_HARDWARE_ID) == _record()


# ---------------------------------------------------------------------------
# Criterion 3 -- interleaved writers never corrupt the file
# ---------------------------------------------------------------------------


class TestConcurrentWritesStayAtomic:
    def test_a_racing_reader_never_observes_invalid_or_truncated_json(self, tmp_path, monkeypatch):
        import reachy.discover.registry as registry_module

        path = tmp_path / "units.json"
        # Seed a valid baseline so the reader always has *something* to parse.
        UnitRegistry(path=path).save({})

        real_dumps = json.dumps

        def slow_dumps(*args, **kwargs):
            body = real_dumps(*args, **kwargs)
            time.sleep(0.02)  # widen the race window around the write
            return body

        monkeypatch.setattr(registry_module.json, "dumps", slow_dumps)

        stop = threading.Event()
        read_errors: list[Exception] = []

        def reader():
            while not stop.is_set():
                try:
                    raw = path.read_text(encoding="utf-8")
                    if raw:
                        json.loads(raw)
                except (OSError, json.JSONDecodeError) as err:  # pragma: no cover - failure path
                    read_errors.append(err)

        def writer(hardware_id: str, barrier: threading.Barrier):
            barrier.wait()
            UnitRegistry(path=path).upsert(_record(hardware_id=hardware_id))

        barrier = threading.Barrier(2)
        reader_thread = threading.Thread(target=reader)
        writer_a = threading.Thread(target=writer, args=("writer-a", barrier))
        writer_b = threading.Thread(target=writer, args=("writer-b", barrier))

        reader_thread.start()
        writer_a.start()
        writer_b.start()
        writer_a.join()
        writer_b.join()
        stop.set()
        reader_thread.join()

        assert read_errors == []

    def test_the_final_file_holds_exactly_one_writers_complete_state(self, tmp_path, monkeypatch):
        import reachy.discover.registry as registry_module

        path = tmp_path / "units.json"
        UnitRegistry(path=path).save({})

        real_dumps = json.dumps

        def slow_dumps(*args, **kwargs):
            body = real_dumps(*args, **kwargs)
            time.sleep(0.02)
            return body

        monkeypatch.setattr(registry_module.json, "dumps", slow_dumps)

        barrier = threading.Barrier(2)

        def writer(hardware_id: str):
            barrier.wait()
            UnitRegistry(path=path).upsert(_record(hardware_id=hardware_id))

        writer_a = threading.Thread(target=writer, args=("writer-a",))
        writer_b = threading.Thread(target=writer, args=("writer-b",))
        writer_a.start()
        writer_b.start()
        writer_a.join()
        writer_b.join()

        raw = json.loads(path.read_text(encoding="utf-8"))
        stored_ids = set(raw["units"].keys())

        # Both writers loaded an empty baseline before either saved, so the
        # last writer to complete wins outright (last-write-wins) -- but the
        # crucial invariant is that the file holds exactly ONE writer's
        # complete, valid record, never a byte-level merge of both.
        assert stored_ids in ({"writer-a"}, {"writer-b"})
        winner = next(iter(stored_ids))
        assert raw["units"][winner]["last_ip"] == "192.168.1.162"

    def test_writes_use_unique_temp_filenames_not_a_shared_name(self, tmp_path):
        # Two concurrent saves must never write through the SAME temp path --
        # a shared fixed name (e.g. "<name>.tmp") is exactly the hazard a
        # unique-per-writer tempfile name avoids.
        import reachy.discover.registry as registry_module

        path = tmp_path / "units.json"
        seen_tmp_names: list[str] = []
        real_mkstemp = registry_module.tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            seen_tmp_names.append(name)
            return fd, name

        registry_module.tempfile.mkstemp = recording_mkstemp
        try:
            UnitRegistry(path=path).upsert(_record())
            UnitRegistry(path=path).upsert(_record(hardware_id="second"))
        finally:
            registry_module.tempfile.mkstemp = real_mkstemp

        assert len(seen_tmp_names) == len(set(seen_tmp_names))

    def test_writes_land_in_the_same_directory_as_the_target(self, tmp_path):
        import reachy.discover.registry as registry_module

        path = tmp_path / "units.json"
        seen_dirs: list[str] = []
        real_mkstemp = registry_module.tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            seen_dirs.append(kwargs.get("dir"))
            return real_mkstemp(*args, **kwargs)

        registry_module.tempfile.mkstemp = recording_mkstemp
        try:
            UnitRegistry(path=path).upsert(_record())
        finally:
            registry_module.tempfile.mkstemp = real_mkstemp

        assert seen_dirs == [str(tmp_path)]


# ---------------------------------------------------------------------------
# Criterion 4 -- MAC enrichment degrades to None, never raises
# ---------------------------------------------------------------------------


class TestMacEnrichment:
    def test_ip_neigh_hit_returns_the_mac(self):
        def fake_run(*args, **kwargs):
            return _CompletedProcess(
                returncode=0,
                stdout="192.168.1.162 dev wlP9s9 lladdr 88:a2:9e:8c:fa:bf STALE\n",
            )

        mac = lookup_mac("192.168.1.162", run=fake_run)

        assert mac == "88:a2:9e:8c:fa:bf"

    def test_ip_neigh_miss_falls_back_to_proc_net_arp(self, tmp_path):
        def fake_run(*args, **kwargs):
            return _CompletedProcess(returncode=0, stdout="")

        arp_path = tmp_path / "arp"
        arp_path.write_text(
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.1.162    0x1         0x2         88:a2:9e:8c:fa:bf     *        wlP9s9\n",
            encoding="utf-8",
        )

        mac = lookup_mac("192.168.1.162", run=fake_run, arp_path=str(arp_path))

        assert mac == "88:a2:9e:8c:fa:bf"

    def test_neighbour_table_unavailable_returns_none(self, tmp_path):
        def fake_run(*args, **kwargs):
            raise FileNotFoundError("ip: command not found")

        mac = lookup_mac("192.168.1.162", run=fake_run, arp_path=str(tmp_path / "does-not-exist"))

        assert mac is None

    def test_host_off_segment_returns_none(self, tmp_path):
        def fake_run(*args, **kwargs):
            return _CompletedProcess(returncode=0, stdout="")

        arp_path = tmp_path / "arp"
        arp_path.write_text(
            "IP address       HW type     Flags       HW address            Mask     Device\n",
            encoding="utf-8",
        )

        mac = lookup_mac("10.99.99.99", run=fake_run, arp_path=str(arp_path))

        assert mac is None

    def test_ip_neigh_nonzero_return_code_falls_back_not_raises(self, tmp_path):
        def fake_run(*args, **kwargs):
            return _CompletedProcess(returncode=1, stdout="")

        mac = lookup_mac("192.168.1.162", run=fake_run, arp_path=str(tmp_path / "missing"))

        assert mac is None

    def test_malformed_neighbour_output_returns_none_not_raises(self):
        def fake_run(*args, **kwargs):
            return _CompletedProcess(returncode=0, stdout="garbage output with no mac here\n")

        mac = lookup_mac("192.168.1.162", run=fake_run, arp_path="/nonexistent/arp")

        assert mac is None

    def test_a_run_seam_that_raises_unexpectedly_still_returns_none(self):
        def fake_run(*args, **kwargs):
            raise RuntimeError("something the subprocess layer never documented")

        mac = lookup_mac("192.168.1.162", run=fake_run, arp_path="/nonexistent/arp")

        assert mac is None

    def test_incomplete_arp_entry_all_zero_mac_returns_none(self, tmp_path):
        def fake_run(*args, **kwargs):
            return _CompletedProcess(returncode=0, stdout="")

        arp_path = tmp_path / "arp"
        arp_path.write_text(
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.1.162    0x1         0x0         00:00:00:00:00:00     *        wlP9s9\n",
            encoding="utf-8",
        )

        mac = lookup_mac("192.168.1.162", run=fake_run, arp_path=str(arp_path))

        assert mac is None

    def test_a_record_without_a_mac_is_still_valid_and_identifiable(self, tmp_path):
        registry = UnitRegistry(path=tmp_path / "units.json")
        record = _record(mac=None)

        registry.upsert(record)

        fetched = registry.get(REAL_HARDWARE_ID)
        assert fetched is not None
        assert fetched.mac is None
        assert fetched.hardware_id == REAL_HARDWARE_ID

    def test_lookup_mac_never_shells_out_to_a_real_ip_binary_in_this_suite(self):
        # Sanity check on the test seam itself: passing an obviously-fake run
        # callable proves lookup_mac() actually calls the injected seam rather
        # than reaching for the real `ip` binary internally.
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            return _CompletedProcess(returncode=1, stdout="")

        lookup_mac("192.168.1.1", run=fake_run, arp_path="/nonexistent/arp")

        assert len(calls) == 1


class _CompletedProcess:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, *, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
