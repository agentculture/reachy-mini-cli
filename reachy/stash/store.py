"""The behavior-stash store — persist stash records + their embedding vectors,
and answer semantic top-k queries over them.

The index (records + vectors) lives as one JSON file under the CLI's per-user
state dir (:func:`reachy.daemon.state_dir`), in a ``stash/`` subdirectory —
the same directory family every other piece of bookkeeping in this project
uses (daemon PID file, cognition-active flag, listen/think/sleep supervisor
PID files, ...).

:class:`StashStore` is deliberately small: :meth:`add` embeds a record's
``explanation`` (via the injected ``embed`` seam — the gateway
:func:`reachy.stash.embeddings.embed_text` by default) and persists it;
:meth:`search` embeds the query the same way and ranks stored records by
cosine similarity (``numpy`` only — already a base dependency, no vector-db
package). The store is robust to a missing or corrupt index file: it never
raises out of loading — a missing file, unparsable JSON, an unexpected shape,
or one unreadable record all degrade to "start fresh" / "drop that record",
logged once, never a traceback.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from reachy.daemon import state_dir
from reachy.stash.embeddings import embed_text
from reachy.stash.record import StashRecord

log = logging.getLogger(__name__)

#: The injectable embedding seam: ``embed(text) -> list[float]``.
EmbedFn = Callable[[str], list]

_INDEX_FILENAME = "index.json"
_INDEX_VERSION = 1

#: Below this magnitude a vector is treated as "effectively zero" for cosine
#: similarity purposes. Avoids both an exact-float equality check (S1244) and a
#: division by a denormal/near-zero norm that would blow the score up instead
#: of degrading it to 0.0 — real (unit-scale) embeddings never come this close
#: to zero, so this cannot change behavior for real embeddings.
_NORM_EPSILON = 1e-12


@dataclass(frozen=True)
class ScoredRecord:
    """One search hit: a stash record plus its cosine similarity to the query."""

    record: StashRecord
    score: float


def default_index_path() -> Path:
    """The default index location: ``<state_dir>/stash/index.json``."""
    return state_dir() / "stash" / _INDEX_FILENAME


class StashStore:
    """Persist stash records + embeddings; answer semantic top-k queries.

    Parameters
    ----------
    path:
        The index JSON file. Defaults to :func:`default_index_path` (under the
        resolved state dir) — tests inject a ``tmp_path`` location instead.
    embed:
        The embedding seam, ``embed(text) -> list[float]``. Defaults to
        :func:`reachy.stash.embeddings.embed_text` (the live gateway); tests
        inject a fake so no unit test ever hits the network.
    """

    def __init__(self, *, path: Path | None = None, embed: EmbedFn | None = None) -> None:
        self._path = path if path is not None else default_index_path()
        self._embed = embed if embed is not None else embed_text
        self._entries: list[tuple[StashRecord, list[float]]] | None = None

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Loading — never raises; a missing/corrupt file degrades to "empty"
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> list[tuple[StashRecord, list[float]]]:
        if self._entries is None:
            self._entries = self._load()
        return self._entries

    def _load(self) -> list[tuple[StashRecord, list[float]]]:
        if not self._path.exists():
            return []

        try:
            raw = self._path.read_text(encoding="utf-8")
            body = json.loads(raw)
        except (OSError, json.JSONDecodeError) as err:
            log.warning(
                "[stash] index at %s is unreadable/corrupt (%s) — starting fresh", self._path, err
            )
            return []

        if not isinstance(body, dict) or body.get("version") != _INDEX_VERSION:
            log.warning("[stash] index at %s has an unexpected shape — starting fresh", self._path)
            return []

        entries: list[tuple[StashRecord, list[float]]] = []
        for item in body.get("records") or []:
            try:
                record = StashRecord.from_dict(item["record"])
                embedding = [float(x) for x in item["embedding"]]
            except Exception as err:  # noqa: BLE001 — one bad record must not sink the store
                log.warning("[stash] dropping unreadable record in %s: %s", self._path, err)
                continue
            entries.append((record, embedding))
        return entries

    def _save(self) -> None:
        entries = self._ensure_loaded()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "version": _INDEX_VERSION,
            "records": [
                {"record": record.to_dict(), "embedding": vector} for record, vector in entries
            ],
        }
        # Write-then-replace: a crash mid-write never leaves a half-written index
        # in place of a good one.
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
        tmp_path.replace(self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, record: StashRecord) -> None:
        """Embed *record*'s explanation and persist it (upsert by ``record.name``)."""
        entries = self._ensure_loaded()
        vector = [float(x) for x in self._embed(record.explanation)]
        entries = [(r, v) for r, v in entries if r.name != record.name]
        entries.append((record, vector))
        self._entries = entries
        self._save()

    def search(self, query: str, k: int = 5) -> list[ScoredRecord]:
        """Return the *k* stored records semantically nearest to *query*.

        Ranks by cosine similarity between the query's embedding and each
        stored record's embedding (``numpy`` only). Returns an empty list when
        the store holds no records.
        """
        entries = self._ensure_loaded()
        if not entries:
            return []

        query_vec = np.asarray(self._embed(query), dtype=float)
        query_norm = float(np.linalg.norm(query_vec))

        scored: list[ScoredRecord] = []
        _dim_mismatch_warned = False
        for record, vector in entries:
            vec = np.asarray(vector, dtype=float)
            if vec.shape != query_vec.shape:
                if not _dim_mismatch_warned:
                    log.warning(
                        "[stash] skipping record %r: embedding dimension %d != query "
                        "dimension %d (possible index/model mismatch)",
                        record.name,
                        vec.shape[0],
                        query_vec.shape[0],
                    )
                    _dim_mismatch_warned = True
                continue
            vec_norm = float(np.linalg.norm(vec))
            if query_norm <= _NORM_EPSILON or vec_norm <= _NORM_EPSILON:
                score = 0.0
            else:
                score = float(np.dot(query_vec, vec) / (query_norm * vec_norm))
            scored.append(ScoredRecord(record=record, score=score))

        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[: max(0, k)]

    def all(self) -> list[StashRecord]:
        """Every stored record, in stash order (no ranking)."""
        return [record for record, _ in self._ensure_loaded()]

    def __len__(self) -> int:
        return len(self._ensure_loaded())
