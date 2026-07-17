"""Tests for the behavior-stash store (:mod:`reachy.stash.store`).

Covers task t4 acceptance criterion 2: the index (records + vectors) persists
under the state dir, and a cosine top-k query (numpy only) over embedded
explanations returns the semantically nearest records. Every test injects a
fake embedder — nothing here hits the network.
"""

from __future__ import annotations

import json

import pytest

from reachy.stash.record import StashRecord
from reachy.stash.store import StashStore

_A = {
    "name": "gaze-up-left",
    "explanation": "look up and to the left, a curious glance",
    "generator": "gaze-hold",
    "params": {
        "yaw": {"default": 15.0, "unit": "deg", "help": "look angle"},
        "pitch": {"default": 8.0, "unit": "deg", "help": "look angle"},
        "roll": {"default": 0.0, "unit": "deg", "help": "roll"},
        "z": {"default": 0.0, "unit": "mm", "help": "height"},
    },
    "channels": ["head"],
    "stop_class": "stoppable",
    "lifetime": {"looping": False, "duration": 4.0},
}

_B = {
    "name": "no-shake",
    "explanation": "shake the head side to side to say no",
    "generator": "shake",
    "params": {
        "amp": {"default": 15.0, "unit": "deg", "help": "shake amplitude"},
        "period": {"default": 0.7, "unit": "s", "help": "shake cycle"},
    },
    "channels": ["head"],
    "stop_class": "stoppable",
    "lifetime": {"looping": True, "duration": None},
}


class _FakeEmbedder:
    """A deterministic fake embedder: fixed 2-D vectors keyed by substring."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self._table = table
        self.calls: list[str] = []

    def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        for key, vector in self._table.items():
            if key in text:
                return list(vector)
        return [0.0, 0.0]


def _embedder() -> _FakeEmbedder:
    return _FakeEmbedder(
        {
            "look up": [1.0, 0.0],
            "shake the head": [0.0, 1.0],
        }
    )


# ---------------------------------------------------------------------------
# add + search happy path
# ---------------------------------------------------------------------------


def test_search_on_empty_store_returns_empty_list(tmp_path):
    store = StashStore(path=tmp_path / "stash" / "index.json", embed=_embedder())
    assert store.search("anything", k=3) == []


def test_add_then_search_returns_semantically_nearest_first(tmp_path):
    embed = _embedder()
    store = StashStore(path=tmp_path / "stash" / "index.json", embed=embed)
    store.add(StashRecord.from_dict(_A))
    store.add(StashRecord.from_dict(_B))

    results = store.search("please look up", k=2)
    assert [r.record.name for r in results] == ["gaze-up-left", "no-shake"]
    assert results[0].score > results[1].score

    results = store.search("shake the head no", k=1)
    assert results[0].record.name == "no-shake"


def test_add_embeds_the_explanation_text(tmp_path):
    embed = _embedder()
    store = StashStore(path=tmp_path / "stash" / "index.json", embed=embed)
    record = StashRecord.from_dict(_A)
    store.add(record)
    assert embed.calls == [record.explanation]


def test_add_upserts_by_name(tmp_path):
    embed = _embedder()
    store = StashStore(path=tmp_path / "stash" / "index.json", embed=embed)
    store.add(StashRecord.from_dict(_A))
    updated = dict(_A)
    updated["explanation"] = "shake the head no instead"
    store.add(StashRecord.from_dict(updated))
    assert len(store) == 1
    results = store.search("shake the head", k=1)
    assert results[0].record.explanation == "shake the head no instead"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_index_persists_to_disk_under_given_path(tmp_path):
    path = tmp_path / "stash" / "index.json"
    store = StashStore(path=path, embed=_embedder())
    store.add(StashRecord.from_dict(_A))
    assert path.exists()
    body = json.loads(path.read_text())
    assert body["records"][0]["record"]["name"] == "gaze-up-left"
    assert body["records"][0]["embedding"] == [1.0, 0.0]


def test_a_fresh_store_instance_loads_the_persisted_index(tmp_path):
    path = tmp_path / "stash" / "index.json"
    embed = _embedder()
    StashStore(path=path, embed=embed).add(StashRecord.from_dict(_A))

    # A brand-new StashStore instance, same path, must see the persisted record.
    store2 = StashStore(path=path, embed=_embedder())
    results = store2.search("look up", k=1)
    assert results[0].record.name == "gaze-up-left"


def test_default_path_lives_under_the_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    store = StashStore(embed=_embedder())
    assert store.path == tmp_path / "stash" / "index.json"


# ---------------------------------------------------------------------------
# Robustness — missing / corrupt index file never raises a traceback
# ---------------------------------------------------------------------------


def test_missing_index_file_starts_empty_without_raising(tmp_path):
    path = tmp_path / "stash" / "index.json"
    assert not path.exists()
    store = StashStore(path=path, embed=_embedder())
    assert len(store) == 0
    assert store.all() == []


def test_corrupt_json_index_file_is_treated_as_empty_not_a_crash(tmp_path):
    path = tmp_path / "stash" / "index.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json at all", encoding="utf-8")

    store = StashStore(path=path, embed=_embedder())
    assert len(store) == 0  # rebuilds cleanly rather than raising

    # And the store keeps working — a subsequent add overwrites the corrupt file.
    store.add(StashRecord.from_dict(_A))
    assert json.loads(path.read_text())["records"][0]["record"]["name"] == "gaze-up-left"


def test_index_file_with_unexpected_shape_is_treated_as_empty(tmp_path):
    path = tmp_path / "stash" / "index.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"totally": "unexpected"}), encoding="utf-8")

    store = StashStore(path=path, embed=_embedder())
    assert len(store) == 0


def test_index_file_with_one_unreadable_record_drops_only_that_record(tmp_path):
    path = tmp_path / "stash" / "index.json"
    path.parent.mkdir(parents=True)
    body = {
        "version": 1,
        "records": [
            {"record": {"not": "a valid stash record"}, "embedding": [1.0, 0.0]},
            {"record": StashRecord.from_dict(_B).to_dict(), "embedding": [0.0, 1.0]},
        ],
    }
    path.write_text(json.dumps(body), encoding="utf-8")

    store = StashStore(path=path, embed=_embedder())
    assert len(store) == 1
    assert store.all()[0].name == "no-shake"


# ---------------------------------------------------------------------------
# Dimension mismatch — index/model mismatch degrades gracefully
# ---------------------------------------------------------------------------


def test_search_skips_records_with_different_embedding_dimension(tmp_path):
    """When a stored embedding has a different dimension than the query's,
    search() skips that record (no ValueError) and returns compatible hits.
    """

    class _DimEmbedder:
        """Returns 3-D for 'query', 2-D for 'old-model'."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, text: str) -> list[float]:
            self.calls.append(text)
            if "query" in text:
                return [1.0, 0.0, 0.0]  # 3-D query
            return [1.0, 0.0]  # 2-D (old model)

    embed = _DimEmbedder()
    store = StashStore(path=tmp_path / "stash" / "index.json", embed=embed)

    # Manually write an index with a 2-D embedding (simulating old-model record)
    import json as _json

    path = store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": 1,
        "records": [
            {
                "record": StashRecord.from_dict(_A).to_dict(),
                "embedding": [1.0, 0.0],  # 2-D — mismatched
            },
            {
                "record": StashRecord.from_dict(_B).to_dict(),
                "embedding": [0.0, 1.0, 0.0],  # 3-D — compatible
            },
        ],
    }
    path.write_text(_json.dumps(body), encoding="utf-8")

    # Force reload
    store._entries = None

    # search() must NOT raise — it skips the 2-D record and scores the 3-D one.
    results = store.search("query", k=5)
    # Only the compatible record is returned.
    assert len(results) == 1
    assert results[0].record.name == "no-shake"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
