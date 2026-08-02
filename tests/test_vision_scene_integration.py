"""Gateway-gated integration test: scene's default model resolves live (#132).

Unlike the mocked unit tests in ``test_vision_scene.py``, this talks to the
REAL lobes gateway named by ``REACHY_OPENAI_*`` and auto-skips cleanly when
unreachable or unconfigured — so the suite stays green on CI and on a bare
box. Mirrors ``test_speech_llm_tools_integration.py`` /
``test_stash_embeddings_integration.py``'s gating style.

Needs no ``[vision]`` extra: the round trip is driven directly through
:func:`reachy.vision.scene._build_messages` + :func:`reachy.vision.scene._post_chat`
with a hand-rolled, minimal valid 1x1 JPEG (a stdlib ``bytes.fromhex`` literal),
bypassing the cv2-only :func:`reachy.vision.scene._encode_jpeg` leg the way
``test_vision_scene.py`` already splits its cv2-free tests from its
``pytest.importorskip("cv2")``-gated ones.
"""

from __future__ import annotations

import base64
import urllib.error
import urllib.request

import pytest

from reachy.vision import scene

_PROBE_TIMEOUT = 3.0
_CALL_TIMEOUT = 15.0

#: The smallest legal JPEG (a single black pixel, 123 bytes) — needs no image
#: library to construct, only ``bytes.fromhex`` + ``base64`` (both stdlib), so
#: this module stays importable/runnable with no ``[vision]`` extra.
_TINY_JPEG = bytes.fromhex(
    "FFD8FFE000104A46494600010100000100010000FFDB00430003020202020203020202030303"
    "0304060404040404080606050609080A0A090809090A0C0F0C0A0B0E0B09090D110D0E0F1010"
    "11100A0C121311100C110F1010FFC9000B08000100010100FFCC000600101005FFDA00080101"
    "00003F00D2CF20FFD9"
)


def _gateway_or_skip() -> scene.SceneConfig:
    """Resolve scene's real config; skip cleanly if unset/unreachable.

    Uses :meth:`SceneConfig.from_env`, the same resolution production code
    uses, so the test honours ``REACHY_OPENAI_*`` / ``REACHY_VISION_MODEL_ID``
    exactly.
    """
    cfg = scene.SceneConfig.from_env()
    if not cfg.api_key or cfg.api_key == "EMPTY":
        pytest.skip("gateway credentials not set (REACHY_OPENAI_API_KEY unset) — skipping")

    url = cfg.base_url.rstrip("/") + "/v1/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg.api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT):  # nosec B310
            pass
    except urllib.error.HTTPError as err:
        if err.code >= 500:
            pytest.skip(f"gateway {cfg.base_url} returned HTTP {err.code} — skipping")
    except OSError as err:
        pytest.skip(f"gateway {cfg.base_url} unreachable ({err}) — skipping")
    return cfg


def test_integration_scene_default_model_resolves_via_senses_role(monkeypatch):
    """The exact #132 regression check: a describe request must not 404.

    Live-probed 2026-08-02 (see the plan/spec + tests/test_gateway_role_model_defaults.py):
    the ``senses`` role resolves 200 and describes the 1x1 probe image. Uses the
    default (no ``REACHY_VISION_MODEL_ID`` override) so this exercises
    :data:`reachy.vision.scene.DEFAULT_VISION_MODEL` itself.
    """
    monkeypatch.delenv("REACHY_VISION_MODEL_ID", raising=False)
    cfg = _gateway_or_skip()
    assert cfg.model == scene.DEFAULT_VISION_MODEL == "senses"

    data_url = "data:image/jpeg;base64," + base64.b64encode(_TINY_JPEG).decode("ascii")
    messages = scene._build_messages("Describe this image in one word.", data_url)
    cfg.timeout = _CALL_TIMEOUT
    try:
        text = scene._post_chat(messages, cfg)
    except scene.SceneError as err:
        message = str(err)
        unresolved = (
            f"scene's default model {scene.DEFAULT_VISION_MODEL!r} failed to resolve "
            f"live: {message}"
        )
        assert "model_not_found" not in message, unresolved
        assert "404" not in message, unresolved
        pytest.skip(f"scene endpoint failed for a non-#132 reason: {message}")

    assert isinstance(text, str)
    assert text.strip()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
