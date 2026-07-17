"""Tests for reachy.vision.scene — the shared scene-description path (task t10).

TDD — these tests define the contract before the implementation exists.

``describe_frame(frame, *, transport=None, cfg=None)`` JPEG-encodes a camera
frame (long edge resized to <= 1280 px), wraps it into an OpenAI-compatible
multimodal ``/v1/chat/completions`` request, and returns the assistant's text
description. The LIVE-VERIFIED wire contract (proven on the box against the lobes
gateway at localhost:8001): messages content is
``[{type:"text",...},{type:"image_url","image_url":{"url":"data:image/jpeg;base64,<...>"}}]``
and the model is the new ``REACHY_VISION_MODEL_ID`` env (the lobes senses role).

Two split so the whole suite is green WITH or WITHOUT the ``[vision]`` extra
(CI's bare ``uv sync`` never installs cv2):

* The **request / config / error** path is exercised cv2-free — the message
  shape and transport are checked by monkeypatching the private ``_encode_jpeg``
  seam (so no real cv2 encode is needed) and injecting a fake ``transport`` (so
  no test hits the network), and the default urllib POST leg is driven by
  monkeypatching ``urllib.request.urlopen``.
* The **real cv2 encode** path (resize + JPEG magic bytes) lives in
  ``pytest.importorskip("cv2")``-gated tests, so those skip in CI and run locally
  after ``uv sync --extra vision``.
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error

import pytest

from reachy.vision import scene

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Recorder:
    """A fake ``transport``: records the (messages, cfg) it was called with."""

    def __init__(self, reply: str = "a person waving") -> None:
        self.reply = reply
        self.messages: list | None = None
        self.cfg = None
        self.calls = 0

    def __call__(self, messages, cfg) -> str:
        self.calls += 1
        self.messages = messages
        self.cfg = cfg
        return self.reply


class _FakeResp:
    """A minimal urlopen response context manager returning canned bytes."""

    def __init__(self, raw: str) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_a) -> bool:
        return False


def _fake_urlopen(raw: str, capture: dict | None = None):
    def _open(req, timeout=None):  # noqa: ANN001
        if capture is not None:
            capture["req"] = req
            capture["timeout"] = timeout
        return _FakeResp(raw)

    return _open


def _ok_body(text: str = "a red mug on a desk") -> str:
    return json.dumps({"choices": [{"message": {"content": text}}]})


# ---------------------------------------------------------------------------
# SceneConfig — env resolution (legacy-free)
# ---------------------------------------------------------------------------


def test_scene_config_reads_openai_env(monkeypatch) -> None:
    monkeypatch.setenv("REACHY_OPENAI_URL_BASE", "http://gw:8001")
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("REACHY_VISION_MODEL_ID", "custom-vlm")
    cfg = scene.SceneConfig.from_env()
    assert cfg.base_url == "http://gw:8001"
    assert cfg.api_key == "sk-x"
    assert cfg.model == "custom-vlm"


def test_scene_config_default_model_is_the_gemma_senses_role(monkeypatch) -> None:
    monkeypatch.delenv("REACHY_VISION_MODEL_ID", raising=False)
    cfg = scene.SceneConfig.from_env()
    assert cfg.model == scene.DEFAULT_VISION_MODEL
    assert "gemma" in cfg.model.lower()


def test_scene_config_is_legacy_free(monkeypatch) -> None:
    """The legacy ``REACHY_LLM_*`` names are NOT honoured for the vision model."""
    monkeypatch.delenv("REACHY_VISION_MODEL_ID", raising=False)
    monkeypatch.setenv("REACHY_LLM_MODEL", "legacy-model")
    cfg = scene.SceneConfig.from_env()
    assert cfg.model == scene.DEFAULT_VISION_MODEL  # legacy name ignored


def test_scene_config_default_timeout_is_reasonable() -> None:
    cfg = scene.SceneConfig(base_url="http://x", model="m")
    assert cfg.timeout == pytest.approx(30.0, abs=5.0)
    assert cfg.max_edge == 1280


# ---------------------------------------------------------------------------
# describe_frame — the multimodal message shape (cv2-free via _encode_jpeg seam)
# ---------------------------------------------------------------------------


def test_describe_frame_builds_the_multimodal_message(monkeypatch) -> None:
    monkeypatch.setattr(scene, "_encode_jpeg", lambda frame, **kw: b"\xff\xd8FAKEJPEG")
    rec = _Recorder("a person waving")

    out = scene.describe_frame(object(), transport=rec)

    assert out == "a person waving"
    assert rec.calls == 1
    content = rec.messages[0]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"]  # a non-empty prompt
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"\xff\xd8FAKEJPEG"


def test_describe_frame_strips_the_response(monkeypatch) -> None:
    monkeypatch.setattr(scene, "_encode_jpeg", lambda frame, **kw: b"j")
    rec = _Recorder("  a cat  \n")
    assert scene.describe_frame(object(), transport=rec) == "a cat"


def test_describe_frame_uses_explicit_cfg(monkeypatch) -> None:
    monkeypatch.setattr(scene, "_encode_jpeg", lambda frame, **kw: b"j")
    cfg = scene.SceneConfig(base_url="http://x", model="m", api_key=None)
    rec = _Recorder("ok")
    scene.describe_frame(object(), transport=rec, cfg=cfg)
    assert rec.cfg is cfg


def test_describe_frame_empty_response_raises_scene_error(monkeypatch) -> None:
    monkeypatch.setattr(scene, "_encode_jpeg", lambda frame, **kw: b"j")
    rec = _Recorder("   ")
    with pytest.raises(scene.SceneError):
        scene.describe_frame(object(), transport=rec)


def test_describe_frame_wraps_a_transport_error_as_scene_error(monkeypatch) -> None:
    monkeypatch.setattr(scene, "_encode_jpeg", lambda frame, **kw: b"j")

    def _boom(messages, cfg):
        raise RuntimeError("kaboom")

    with pytest.raises(scene.SceneError):
        scene.describe_frame(object(), transport=_boom)


def test_describe_frame_propagates_a_transport_scene_error(monkeypatch) -> None:
    monkeypatch.setattr(scene, "_encode_jpeg", lambda frame, **kw: b"j")

    def _down(messages, cfg):
        raise scene.SceneError("vlm-unreachable: cannot reach endpoint")

    with pytest.raises(scene.SceneError) as ei:
        scene.describe_frame(object(), transport=_down)
    assert "unreachable" in str(ei.value)


# ---------------------------------------------------------------------------
# _post_chat — the default urllib request leg (cv2-free; urlopen monkeypatched)
# ---------------------------------------------------------------------------


def test_post_chat_returns_assistant_text(monkeypatch) -> None:
    monkeypatch.setattr(scene.urllib.request, "urlopen", _fake_urlopen(_ok_body("a red mug")))
    cfg = scene.SceneConfig(base_url="http://x", model="m")
    assert scene._post_chat([{"role": "user", "content": []}], cfg) == "a red mug"


def test_post_chat_sends_model_url_and_bearer(monkeypatch) -> None:
    cap: dict = {}
    monkeypatch.setattr(scene.urllib.request, "urlopen", _fake_urlopen(_ok_body(), cap))
    cfg = scene.SceneConfig(base_url="http://gw:8001", model="mymodel", api_key="sk-1")

    scene._post_chat([{"role": "user", "content": []}], cfg)

    req = cap["req"]
    assert req.full_url == "http://gw:8001/v1/chat/completions"
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["model"] == "mymodel"
    assert payload["messages"] == [{"role": "user", "content": []}]
    assert req.get_header("Authorization") == "Bearer sk-1"
    assert cap["timeout"] == pytest.approx(cfg.timeout)


def test_post_chat_no_auth_header_without_a_key(monkeypatch) -> None:
    cap: dict = {}
    monkeypatch.setattr(scene.urllib.request, "urlopen", _fake_urlopen(_ok_body(), cap))
    cfg = scene.SceneConfig(base_url="http://x", model="m", api_key=None)
    scene._post_chat([], cfg)
    assert cap["req"].get_header("Authorization") is None


def test_post_chat_no_auth_header_for_empty_sentinel(monkeypatch) -> None:
    cap: dict = {}
    monkeypatch.setattr(scene.urllib.request, "urlopen", _fake_urlopen(_ok_body(), cap))
    cfg = scene.SceneConfig(base_url="http://x", model="m", api_key="EMPTY")
    scene._post_chat([], cfg)
    assert cap["req"].get_header("Authorization") is None


def test_post_chat_http_error_becomes_scene_error(monkeypatch) -> None:
    def _open(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError("http://x", 500, "err", {}, None)

    monkeypatch.setattr(scene.urllib.request, "urlopen", _open)
    cfg = scene.SceneConfig(base_url="http://x", model="m")
    with pytest.raises(scene.SceneError) as ei:
        scene._post_chat([], cfg)
    assert "500" in str(ei.value)


def test_post_chat_url_error_becomes_scene_error(monkeypatch) -> None:
    def _open(req, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(scene.urllib.request, "urlopen", _open)
    cfg = scene.SceneConfig(base_url="http://x", model="m")
    with pytest.raises(scene.SceneError):
        scene._post_chat([], cfg)


def test_post_chat_malformed_json_becomes_scene_error(monkeypatch) -> None:
    monkeypatch.setattr(scene.urllib.request, "urlopen", _fake_urlopen("not-json"))
    cfg = scene.SceneConfig(base_url="http://x", model="m")
    with pytest.raises(scene.SceneError):
        scene._post_chat([], cfg)


def test_post_chat_missing_choices_becomes_scene_error(monkeypatch) -> None:
    monkeypatch.setattr(scene.urllib.request, "urlopen", _fake_urlopen(json.dumps({"foo": 1})))
    cfg = scene.SceneConfig(base_url="http://x", model="m")
    with pytest.raises(scene.SceneError):
        scene._post_chat([], cfg)


# ---------------------------------------------------------------------------
# _encode_jpeg — missing cv2 (cv2-free via the sys.modules None seam)
# ---------------------------------------------------------------------------


def test_encode_jpeg_without_cv2_raises_scene_error(monkeypatch) -> None:
    """A missing ``[vision]`` extra surfaces as a SceneError from the encode leg."""
    monkeypatch.setitem(sys.modules, "cv2", None)
    with pytest.raises(scene.SceneError):
        scene._encode_jpeg(object())


def test_describe_frame_without_cv2_raises_scene_error(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "cv2", None)
    frame = object()
    transport = _Recorder("x")
    with pytest.raises(scene.SceneError):
        scene.describe_frame(frame, transport=transport)


# ---------------------------------------------------------------------------
# Real cv2 encode path (importorskip-gated; skipped in CI's bare install)
# ---------------------------------------------------------------------------


def test_encode_jpeg_produces_jpeg_and_resizes_long_edge() -> None:
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    frame = np.zeros((2000, 3000, 3), dtype=np.uint8)  # long edge 3000 > 1280
    jpeg = scene._encode_jpeg(frame, max_edge=1280)

    assert jpeg[:2] == b"\xff\xd8"  # JPEG SOI marker
    decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = decoded.shape[:2]
    assert max(h, w) <= 1280


def test_encode_jpeg_leaves_small_frames_untouched_in_scale() -> None:
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    frame = np.zeros((100, 120, 3), dtype=np.uint8)  # already small
    jpeg = scene._encode_jpeg(frame, max_edge=1280)
    decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[:2] == (100, 120)


def test_describe_frame_end_to_end_with_real_encode() -> None:
    pytest.importorskip("cv2")
    import numpy as np

    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    rec = _Recorder("a dark frame")
    out = scene.describe_frame(frame, transport=rec)

    assert out == "a dark frame"
    url = rec.messages[0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1])[:2] == b"\xff\xd8"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
