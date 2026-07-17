"""The forge package — runtime self-extension for Reachy Mini.

Dispatch a natural-language goal to a coder model (``qwen3`` on the lobes gateway by
default), parse the two fenced files it returns (``SKILL.md`` + ``executor.py``), stage
them under the state dir, and run them through an AST-only, fail-closed validator before
they are ever eligible for activation. Every lifecycle transition is announced as a
``forge/*`` event.

Ported (cite-don't-import) from ``reachy_nova``'s ``skill_forge.py`` +
``forge_validator.py``, split three ways:

* :mod:`reachy.forge.client` — :class:`ForgeClient`, the background-thread dispatch
  client;
* :mod:`reachy.forge.validator` — :func:`validate`, the AST-only static gate; and
* :mod:`reachy.forge.lifecycle` — the staged/activated/rejected disk + event layer.

The ``ctx`` tool surface a forged skill is handed, and the auto-activation wiring, are a
later task's responsibility (t13); this package provides the client, the gate, and the
move + event primitives only.
"""

from __future__ import annotations

from reachy.forge.client import (
    DEFAULT_FORGE_BASE_URL,
    DEFAULT_FORGE_MODEL,
    DEFAULT_TIMEOUT,
    ForgeClient,
)
from reachy.forge.lifecycle import (
    EVENT_ACTIVATED,
    EVENT_REJECTED,
    EVENT_STAGED,
    activate,
    default_active_root,
    default_staging_root,
    reject,
    stage,
    write_artifacts,
)
from reachy.forge.validator import (
    ALLOWED_IMPORTS,
    DEFAULT_ALLOWED_CTX_ATTRS,
    FORBIDDEN_NAMES,
    MAX_EXECUTOR_LINES,
    SAFE_BUILTIN_CALLS,
    validate,
)

__all__ = [
    "ForgeClient",
    "DEFAULT_FORGE_BASE_URL",
    "DEFAULT_FORGE_MODEL",
    "DEFAULT_TIMEOUT",
    "validate",
    "ALLOWED_IMPORTS",
    "DEFAULT_ALLOWED_CTX_ATTRS",
    "FORBIDDEN_NAMES",
    "SAFE_BUILTIN_CALLS",
    "MAX_EXECUTOR_LINES",
    "activate",
    "default_active_root",
    "default_staging_root",
    "reject",
    "stage",
    "write_artifacts",
    "EVENT_STAGED",
    "EVENT_ACTIVATED",
    "EVENT_REJECTED",
]
