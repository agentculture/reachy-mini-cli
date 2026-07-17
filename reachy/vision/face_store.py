"""Face-embedding storage — cosine matching + temporary/permanent tiers.

Cited from ``reachy_nova.face_manager.FaceManager`` (cite-don't-import): the
cosine-similarity matcher, the temporary-vs-permanent tier split, and the
on-disk layout (one JSON index + one ``.npy`` file per stored embedding) are
ported faithfully. Deviations from nova, and why:

* **No admin-auth tier.** Nova's ``FaceManager`` bakes in a hard-coded admin
  name, an auto-created admin placeholder record, and an ``is_admin`` /
  voice-command-authorization flow. There is no voice-command-authorization
  feature in reachy-mini-cli yet, so all of that (``ADMIN_NAME``,
  ``_ensure_admin``, ``is_admin``) is dropped — explicitly out of scope for
  this port.
* **``enroll(name, embedding)`` replaces ``consent(temp_id, name)``.** Nova's
  permanent-tier write always promotes an embedding already sitting in the
  temporary tier. This store keeps the temporary tier (useful later for a
  "who are you?" enrollment flow) but *also* exposes a direct ``enroll`` that
  creates a permanent record from a fresh embedding in one call — the shape
  requested for this task.
* **Storage root.** Nova hard-codes ``~/.reachy_nova/faces``; this module
  defaults to ``reachy.daemon.state_dir() / "faces"``, the same per-user state
  dir every other stateful noun in this repo uses (honours
  ``$REACHY_STATE_DIR`` for test isolation, mirrors
  :func:`reachy.stash.store.default_index_path`).
* **No ``merge`` / ``add_angles`` / ``get_person_images``.** Nova's
  multi-angle enrollment and face-merge admin tools aren't part of this
  task's contract; the on-disk shape (a list of ``embedding_files`` per face)
  leaves room to add them later without a migration.
* **Corrupt/missing index degrades to "start fresh", never raises** — mirrors
  :mod:`reachy.stash.store`'s load robustness rather than nova's bare
  ``json.load`` (which nova wraps in its own try/except at the call site).

Determinism seams for tests: every time-sensitive method takes an optional
``now=`` override (mirrors :meth:`reachy.motion.pat.PatDetector.update`); the
constructor takes ``base_dir=`` to point at an isolated directory instead of
the real state dir, and ``clock=`` for the default-now callable.
"""

from __future__ import annotations

import json
import logging
import secrets
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from reachy.daemon import state_dir

logger = logging.getLogger(__name__)

#: Minimum cosine similarity for :meth:`FaceStore.match` to report a hit.
#: Matches nova's ``FaceManager.match`` default.
DEFAULT_MATCH_THRESHOLD = 0.5

#: Temporary-tier time-to-live, seconds. Matches nova's ``TEMP_TTL``.
DEFAULT_TEMP_TTL = 15 * 60

_ID_ALPHABET = string.ascii_lowercase + string.digits
_ID_LENGTH = 4
_TEMP_PREFIX = "tmp_"

_INDEX_FILENAME = "faces.json"
_EMBEDDINGS_DIRNAME = "embeddings"

#: Below this vector magnitude, cosine similarity is defined as 0.0 rather
#: than risking a division by (near) zero. Matches nova's ``_cosine_similarity``.
_NORM_EPSILON = 1e-8


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors; ``0.0`` if either is near-zero."""
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a < _NORM_EPSILON or norm_b < _NORM_EPSILON:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


@dataclass(frozen=True)
class FaceMatch:
    """A permanent-tier hit returned by :meth:`FaceStore.match`."""

    face_id: str
    name: str
    score: float


def _generate_id(existing: set[str]) -> str:
    """Generate a unique 4-char lowercase-alnum id, retrying on collision.

    Uses :mod:`secrets` (not :mod:`random`) so no bandit "insecure random"
    suppression is needed — the id itself carries no confidentiality
    requirement, but a CSPRNG source is free here and sidesteps the lint.
    """
    for _ in range(1000):
        candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
        if candidate not in existing:
            return candidate
    raise RuntimeError(
        "could not generate a unique face id after 1000 attempts"
    )  # pragma: no cover


def default_base_dir() -> Path:
    """The default persistence root: ``<state_dir>/faces``."""
    return state_dir() / "faces"


class FaceStore:
    """Two-tier face-embedding store: temporary (TTL) + permanent (persisted).

    Parameters
    ----------
    base_dir:
        Root directory for persistence. Defaults to :func:`default_base_dir`.
        Tests pass an isolated ``tmp_path`` instead.
    clock:
        Zero-arg callable returning the current time (seconds). Defaults to
        :func:`time.time`; override for deterministic tests.
    ttl:
        Temporary-tier time-to-live, seconds. Default :data:`DEFAULT_TEMP_TTL`.
    threshold:
        Default cosine-similarity match threshold. Default
        :data:`DEFAULT_MATCH_THRESHOLD`.
    """

    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
        ttl: float = DEFAULT_TEMP_TTL,
        threshold: float = DEFAULT_MATCH_THRESHOLD,
    ) -> None:
        self._base_dir = Path(base_dir) if base_dir is not None else default_base_dir()
        self._embeddings_dir = self._base_dir / _EMBEDDINGS_DIRNAME
        self._index_path = self._base_dir / _INDEX_FILENAME
        self._clock = clock
        self.ttl = ttl
        self.threshold = threshold

        self._permanent: dict[str, dict] = {}
        self._temporary: dict[str, dict] = {}
        self._loaded = False

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    # ------------------------------------------------------------------
    # Persistence — never raises; a missing/corrupt index degrades to empty.
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load the permanent-tier index from disk (idempotent)."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._embeddings_dir.mkdir(parents=True, exist_ok=True)

        if not self._index_path.exists():
            self._permanent = {}
            self._loaded = True
            return

        try:
            raw = self._index_path.read_text(encoding="utf-8")
            body = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "face index at %s is unreadable/corrupt (%s) — starting fresh",
                self._index_path,
                exc,
            )
            body = {}

        self._permanent = body if isinstance(body, dict) else {}
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> None:
        """Persist the permanent-tier index to disk (write-then-replace)."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._index_path.with_name(self._index_path.name + ".tmp")
        try:
            tmp_path.write_text(json.dumps(self._permanent, indent=2), encoding="utf-8")
            tmp_path.replace(self._index_path)
        except OSError as exc:
            logger.warning("failed to save face index at %s: %s", self._index_path, exc)

    # ------------------------------------------------------------------
    # Temporary tier
    # ------------------------------------------------------------------

    def remember_temporary(self, embedding: np.ndarray, *, now: float | None = None) -> str:
        """Stash *embedding* in the temporary tier (TTL-bound). Returns its temp id."""
        if now is None:
            now = self._clock()
        temp_id = f"{_TEMP_PREFIX}{_generate_id(set(self._temporary.keys()))}"
        self._temporary[temp_id] = {
            "embedding": np.asarray(embedding, dtype=float),
            "created": now,
        }
        return temp_id

    def get_temporary(self, temp_id: str) -> np.ndarray | None:
        """Return the stored embedding for *temp_id*, or ``None`` if absent."""
        entry = self._temporary.get(temp_id)
        return None if entry is None else entry["embedding"].copy()

    def cleanup_expired(self, *, now: float | None = None) -> int:
        """Evict temporary entries older than ``ttl``. Returns the count removed."""
        if now is None:
            now = self._clock()
        expired = [tid for tid, data in self._temporary.items() if now - data["created"] > self.ttl]
        for tid in expired:
            del self._temporary[tid]
        return len(expired)

    @property
    def temporary_count(self) -> int:
        return len(self._temporary)

    # ------------------------------------------------------------------
    # Permanent tier
    # ------------------------------------------------------------------

    def enroll(self, name: str, embedding: np.ndarray, *, now: float | None = None) -> str:
        """Create a new permanent face record from *embedding*. Returns its id."""
        self._ensure_loaded()
        if now is None:
            now = self._clock()

        face_id = _generate_id(set(self._permanent.keys()))
        emb_file = f"{face_id}.npy"
        self._embeddings_dir.mkdir(parents=True, exist_ok=True)
        np.save(self._embeddings_dir / emb_file, np.asarray(embedding, dtype=float))

        self._permanent[face_id] = {
            "name": name,
            "created": now,
            "embedding_files": [emb_file],
        }
        self.save()
        return face_id

    def match(self, embedding: np.ndarray, *, threshold: float | None = None) -> FaceMatch | None:
        """Return the best permanent-tier match for *embedding*, or ``None``.

        Only the permanent tier is searched (mirrors nova's
        ``FaceManager.match``) — a temporary embedding is "seen but not yet
        named" and cannot itself be a match target.
        """
        self._ensure_loaded()
        if threshold is None:
            threshold = self.threshold

        query = np.asarray(embedding, dtype=float)
        best_id: str | None = None
        best_name: str | None = None
        best_score = -1.0

        for face_id, data in self._permanent.items():
            for emb_file in data.get("embedding_files", []):
                path = self._embeddings_dir / emb_file
                if not path.exists():
                    continue
                try:
                    stored = np.load(path)
                except (OSError, ValueError) as exc:
                    logger.warning("failed to load embedding %s: %s", path, exc)
                    continue
                score = cosine_similarity(query, stored)
                if score > best_score:
                    best_score = score
                    best_id = face_id
                    best_name = data.get("name")

        if best_id is not None and best_score >= threshold:
            return FaceMatch(face_id=best_id, name=best_name, score=best_score)
        return None

    def forget(self, face_id: str) -> bool:
        """Delete a permanent face record and its embedding files. Returns success."""
        self._ensure_loaded()
        data = self._permanent.pop(face_id, None)
        if data is None:
            return False
        for emb_file in data.get("embedding_files", []):
            path = self._embeddings_dir / emb_file
            if path.exists():
                path.unlink()
        self.save()
        return True

    def list_faces(self) -> list[dict]:
        """List all permanent faces (id, name, created, embedding count)."""
        self._ensure_loaded()
        return [
            {
                "id": face_id,
                "name": data.get("name"),
                "created": data.get("created", 0),
                "num_embeddings": len(data.get("embedding_files", [])),
            }
            for face_id, data in self._permanent.items()
        ]

    def get_unique_id(self, name: str) -> str | None:
        """Look up a permanent face id by (case-insensitive) name."""
        self._ensure_loaded()
        target = name.lower().strip()
        for face_id, data in self._permanent.items():
            if str(data.get("name", "")).lower().strip() == target:
                return face_id
        return None

    @property
    def permanent_count(self) -> int:
        self._ensure_loaded()
        return len(self._permanent)
