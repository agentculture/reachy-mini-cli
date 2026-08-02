"""Guards issue #132: executable model defaults name gateway ROLES, not served ids.

The lobes gateway's ``resolve_model`` accepts role names, tier aliases and
served ids by contract, and a role name is the only one of the three that
survives a model promotion. This already drifted twice, live:

* ``reachy/forge/client.py``'s ``DEFAULT_FORGE_MODEL`` named the served id
  ``qwen3`` (cited from ``reachy_nova``). Live-probed 2026-08-02 against the
  real gateway: ``qwen3`` now 404s (``model_not_found``) — forge dispatch is
  broken TODAY on an unconfigured box.
* ``reachy/vision/scene.py``'s ``DEFAULT_VISION_MODEL`` named the served id
  ``coolthor/gemma-4-12B-it-NVFP4A16`` directly. Still served as of the same
  probe, so not broken yet — but one promotion away from the exact same fate.

Both moved to the gateway's ROLE names (``cortex`` / ``senses``, live-probed
2026-08-02 to both resolve 200). This module pins the fix two ways: by value
(so the constants can never silently drift back) and by a source-level sweep
(so no OTHER executable default in ``reachy/`` — not just these two files —
reintroduces either dead pattern). The sweep is a regex on **default-shaped**
occurrences (``= "..."`` or ``.get(..., "...")``), not a bare substring, so it
does not false-positive on the prose that discusses the old defaults
elsewhere (``reachy/forge/__init__.py``'s docstring, the ``forge`` entry in
``reachy/explain/catalog.py``) — those are documentation of a moment (this
task's instruction is explicit that such prose stays) and name no default
themselves.
"""

from __future__ import annotations

import re
from pathlib import Path

from reachy.forge.client import DEFAULT_FORGE_MODEL
from reachy.vision.scene import DEFAULT_VISION_MODEL

REPO_ROOT = Path(__file__).parent.parent

#: The exact dead/drift-prone served ids issue #132 found — never again an
#: executable default anywhere under ``reachy/``.
_BANNED_SERVED_IDS = (
    "qwen3",
    "coolthor/gemma-4-12B-it-NVFP4A16",
    # The dead alias `qwen3` itself replaced (see the module docstring on
    # tests/test_speech_llm_tools_integration.py) — the SAME drift class, so it
    # is banned here too even though it never landed inside reachy/.
    "sakamakismile/Qwen3.6-27B-Text-NVFP4-MTP",
)

#: Matches a banned id used as a default: the RHS of an assignment
#: (``NAME = "qwen3"``) or the fallback argument of an ``os.environ.get(...)``
#: call (``.get("ENV", "qwen3")``) — never a bare mention inside prose, which
#: has no leading ``=`` or ``, `` immediately before the quote.
_DEFAULT_USE_RE = re.compile(
    r'(?:=|,)\s*"(' + "|".join(re.escape(v) for v in _BANNED_SERVED_IDS) + r')"'
)


def test_forge_default_model_is_the_cortex_role_not_a_served_id() -> None:
    assert DEFAULT_FORGE_MODEL == "cortex"
    assert "/" not in DEFAULT_FORGE_MODEL  # a served id always carries an org/repo slash


def test_scene_default_model_is_the_senses_role_not_a_served_id() -> None:
    assert DEFAULT_VISION_MODEL == "senses"
    assert "/" not in DEFAULT_VISION_MODEL


def test_no_reachy_source_names_a_banned_served_id_as_a_default() -> None:
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "reachy").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _DEFAULT_USE_RE.finditer(text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")
    assert offenders == []
