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
* :mod:`reachy.forge.lifecycle` — the staged/activated/rejected disk + event layer; and
* :mod:`reachy.forge.activate` — validator-gated AUTO-activation (no human gate): the
  restricted :class:`ForgedSkillContext`, the ``spec_from_file_location`` importer, the
  crash-catching handler wrapper, and :class:`ForgeActivator` (auto-activate on stage,
  hot-register into the live registry, announce, and boot-reload ``active/``).
"""

from __future__ import annotations

from reachy.forge.activate import (
    DEFAULT_FORGED_PARAMS,
    ForgeActivator,
    ForgedSkillContext,
    build_ctx_seams,
    import_forged_execute,
    read_skill_description,
    wrap_executor,
)
from reachy.forge.client import (
    DEFAULT_FORGE_BASE_URL,
    DEFAULT_FORGE_MODEL,
    DEFAULT_TIMEOUT,
    ForgeClient,
)

# NOTE: the lifecycle *move* helper ``lifecycle.activate`` is deliberately NOT re-exported
# here — the ``reachy.forge.activate`` name now belongs to the AUTO-activation submodule
# (:mod:`reachy.forge.activate`), and a same-named function attribute would shadow it. The
# low-level move remains available as :func:`reachy.forge.lifecycle.activate`.
from reachy.forge.lifecycle import (
    EVENT_ACTIVATED,
    EVENT_REJECTED,
    EVENT_STAGED,
    EXECUTOR_FILENAME,
    SKILL_FILENAME,
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
    "ForgeActivator",
    "ForgedSkillContext",
    "import_forged_execute",
    "wrap_executor",
    "read_skill_description",
    "build_ctx_seams",
    "DEFAULT_FORGED_PARAMS",
    "DEFAULT_FORGE_BASE_URL",
    "DEFAULT_FORGE_MODEL",
    "DEFAULT_TIMEOUT",
    "validate",
    "ALLOWED_IMPORTS",
    "DEFAULT_ALLOWED_CTX_ATTRS",
    "FORBIDDEN_NAMES",
    "SAFE_BUILTIN_CALLS",
    "MAX_EXECUTOR_LINES",
    "default_active_root",
    "default_staging_root",
    "reject",
    "stage",
    "write_artifacts",
    "EVENT_STAGED",
    "EVENT_ACTIVATED",
    "EVENT_REJECTED",
    "SKILL_FILENAME",
    "EXECUTOR_FILENAME",
]
