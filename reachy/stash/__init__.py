"""The behavior stash — a persistent, semantically searchable store of body behaviors.

A stash record is DECLARATIVE DATA in the :class:`~reachy.behavior.library.LibraryEntry`
mold — never free-form code. Each record names an existing generator template from
:data:`reachy.behavior.library.LIBRARY` plus a typed parameter set, the channels it
claims, its stop-class and lifetime, and a natural-language ``explanation`` that gets
embedded (via the lobes gateway ``/v1/embeddings`` route) for semantic top-k search.

Public API
----------
* :class:`~reachy.stash.record.StashRecord` / :class:`~reachy.stash.record.StashParam`
  — the record schema (:meth:`StashRecord.from_dict` validates and refuses anything
  smelling of code; :meth:`StashRecord.to_dict` serializes back to plain JSON data).
* :class:`~reachy.stash.store.StashStore` — ``add(record)`` embeds + persists;
  ``search(query, k)`` returns the semantically nearest records (cosine, numpy-only).
* :func:`~reachy.stash.embeddings.embed_text` / :class:`~reachy.stash.embeddings.EmbeddingConfig`
  — the injectable embedding client (stdlib ``urllib`` only; independent of
  :mod:`reachy.speech.llm`).

The index (records + vectors) persists under the CLI's per-user state dir
(:func:`reachy.daemon.state_dir`), in a ``stash/`` subdirectory.
"""

from __future__ import annotations

from reachy.stash.embeddings import EmbeddingConfig, embed_text
from reachy.stash.record import StashParam, StashRecord
from reachy.stash.store import ScoredRecord, StashStore

__all__ = [
    "StashRecord",
    "StashParam",
    "StashStore",
    "ScoredRecord",
    "embed_text",
    "EmbeddingConfig",
]
