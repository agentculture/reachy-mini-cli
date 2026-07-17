"""Tests for the forge artifact lifecycle (:mod:`reachy.forge.lifecycle`).

Covers task t12 acceptance criterion 3: artifacts land under
``state_dir()/forge/staged/<name>/``, move to ``active/<name>/`` on activation,
and rejected artifacts move to ``staged/.rejected/<name>/`` with ``{reason,
reasons}`` recorded. ``staged`` fires only through :func:`stage` (the client
calls it strictly after validation passes), and every rejection logs loudly and
names the reason (asserted via caplog). A raising publish callback is isolated.
"""

from __future__ import annotations

import logging

import pytest

from reachy.forge import lifecycle


class _Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, event_type, payload):
        self.events.append((event_type, payload))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Staging paths + write_artifacts
# ---------------------------------------------------------------------------


def test_write_artifacts_lands_under_state_dir_staged(state):
    skill_dir = lifecycle.write_artifacts("wave-hello", "md-body", "py-body")
    assert skill_dir == state / "forge" / "staged" / "wave-hello"
    assert (skill_dir / "SKILL.md").read_text() == "md-body"
    assert (skill_dir / "executor.py").read_text() == "py-body"


def test_default_roots_live_under_state_dir(state):
    assert lifecycle.default_staging_root() == state / "forge" / "staged"
    assert lifecycle.default_active_root() == state / "forge" / "active"


# ---------------------------------------------------------------------------
# stage — the only path that fires forge/staged
# ---------------------------------------------------------------------------


def test_stage_emits_staged_event_only(state):
    pub = _Recorder()
    skill_dir = lifecycle.write_artifacts("wave-hello", "md", "py")
    lifecycle.stage(pub, "wave-hello", skill_dir)
    assert pub.events == [("forge/staged", {"name": "wave-hello", "path": str(skill_dir)})]


# ---------------------------------------------------------------------------
# reject — move to .rejected, record reasons, log loudly
# ---------------------------------------------------------------------------


def test_reject_moves_to_rejected_and_records_reasons(state, caplog):
    pub = _Recorder()
    skill_dir = lifecycle.write_artifacts("bad-skill", "md", "py")
    reasons = ["import 'os' is not allowed (line 1)", "dunder attribute access '.__class__'"]
    with caplog.at_level(logging.WARNING):
        lifecycle.reject(pub, "bad-skill", reasons, skill_dir)

    assert not skill_dir.exists()
    rejected = state / "forge" / "staged" / ".rejected" / "bad-skill"
    assert (rejected / "SKILL.md").exists()

    assert len(pub.events) == 1
    event_type, payload = pub.events[0]
    assert event_type == "forge/rejected"
    assert payload["reason"] == "; ".join(reasons)
    assert payload["reasons"] == reasons
    assert payload["name"] == "bad-skill"
    assert payload["path"] == str(rejected)

    assert "bad-skill" in caplog.text
    assert "os" in caplog.text  # the reason itself is in the loud log


def test_reject_without_skill_dir_still_emits_and_logs(state, caplog):
    pub = _Recorder()
    with caplog.at_level(logging.WARNING):
        lifecycle.reject(pub, None, ["endpoint unreachable: boom"])

    event_type, payload = pub.events[0]
    assert event_type == "forge/rejected"
    assert payload["reason"] == "endpoint unreachable: boom"
    assert payload["reasons"] == ["endpoint unreachable: boom"]
    assert "path" not in payload
    assert "name" not in payload
    assert "endpoint unreachable" in caplog.text


def test_reject_overwrites_existing_rejected_dir(state):
    pub = _Recorder()
    rejected = state / "forge" / "staged" / ".rejected" / "dupe"
    rejected.mkdir(parents=True)
    (rejected / "stale.txt").write_text("old")

    skill_dir = lifecycle.write_artifacts("dupe", "fresh-md", "fresh-py")
    lifecycle.reject(pub, "dupe", ["nope"], skill_dir)

    assert (rejected / "SKILL.md").read_text() == "fresh-md"
    assert not (rejected / "stale.txt").exists()


def test_reject_survives_move_failure(state, monkeypatch, caplog):
    pub = _Recorder()
    skill_dir = lifecycle.write_artifacts("boom", "md", "py")

    def _boom(*_args, **_kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(lifecycle.shutil, "move", _boom)
    with caplog.at_level(logging.WARNING):
        lifecycle.reject(pub, "boom", ["some reason"], skill_dir)

    assert pub.events[0][0] == "forge/rejected"
    # the move failed, so path falls back to the original staged dir
    assert pub.events[0][1]["path"] == str(skill_dir)
    assert "disk on fire" in caplog.text


# ---------------------------------------------------------------------------
# activate — move staged -> active + emit (wiring is a later task)
# ---------------------------------------------------------------------------


def test_activate_moves_staged_to_active_and_emits(state):
    pub = _Recorder()
    lifecycle.write_artifacts("wave-hello", "md", "py")
    dst = lifecycle.activate(pub, "wave-hello")

    assert dst == state / "forge" / "active" / "wave-hello"
    assert (dst / "SKILL.md").exists()
    assert not (state / "forge" / "staged" / "wave-hello").exists()
    assert pub.events == [("forge/activated", {"name": "wave-hello", "path": str(dst)})]


def test_activate_missing_staged_dir_logs_and_emits_no_event(state, caplog):
    pub = _Recorder()
    with caplog.at_level(logging.WARNING):
        result = lifecycle.activate(pub, "ghost")
    assert result is None
    assert pub.events == []
    assert "ghost" in caplog.text


# ---------------------------------------------------------------------------
# emit — a broken publish callback must not crash us
# ---------------------------------------------------------------------------


def test_emit_isolates_a_raising_publish_callback(state, caplog):
    def _bad(_event_type, _payload):
        raise RuntimeError("publish is down")

    with caplog.at_level(logging.WARNING):
        lifecycle.emit(_bad, "forge/staged", {"name": "x"})  # must not raise
    assert "publish is down" in caplog.text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
