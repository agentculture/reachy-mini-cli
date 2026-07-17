"""Scene description — one shared VLM describe path (camera frame -> a sentence).

:func:`describe_frame` is the single entry point both the on-demand agent tool
(``describe_scene`` in :mod:`reachy.speech.tools`) and the periodic
:class:`~reachy.motion.listen_scene.SceneHook` call. It JPEG-encodes a camera
frame (long edge resized to ``max_edge`` px, default 1280), wraps it into an
OpenAI-compatible multimodal ``/v1/chat/completions`` request, and returns the
assistant's plain-text description.

LIVE-VERIFIED wire contract (proven on the box against the lobes gateway):
``POST {base_url}/v1/chat/completions`` with ``Authorization: Bearer {api_key}``,
``model`` per the new ``REACHY_VISION_MODEL_ID`` env (the lobes *senses* role),
and messages content
``[{"type":"text",...},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,<...>"}}]``.

Design (mirrors the rest of this repo's SDK-first, extra-gated senses):

* **``cv2`` is imported lazily, inside functions only** — never at module import
  time — so this module (and anything that merely imports it, e.g.
  :class:`~reachy.motion.listen_scene.SceneHook`) is loadable on a bare install
  with no OpenCV present. Cited from ``reachy_nova.nova_vision`` (the 3-frame
  ring, the 30 s fallback + on-demand trigger, the JPEG <= 1280 px resize) but
  reimplemented against the local OpenAI-compatible gateway instead of Bedrock.
* **Independent, stdlib-only urllib client** — like :mod:`reachy.stash.embeddings`,
  this does NOT go through :mod:`reachy.speech.llm`: it needs a multimodal
  (image) request and a *different* model env, and staying self-contained keeps
  it **legacy-free** (no ``REACHY_LLM_*`` fallback — only the canonical
  ``REACHY_OPENAI_*`` + ``REACHY_VISION_MODEL_ID``).
* **Every failure is a typed :class:`SceneError`** naming the reason — an
  unreachable/slow endpoint, a non-2xx status, a malformed response, a failed
  encode, or a missing ``[vision]`` extra. Callers (the tool dispatch, the hook
  worker) key on that one exception type.
* **The HTTP leg is an injectable ``transport`` seam** so no test hits the
  network; the default is :func:`_post_chat`.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# --- env / config ----------------------------------------------------------

#: Canonical env names (legacy-free — the ``REACHY_LLM_*`` fallback that
#: :mod:`reachy.speech.llm` honours is deliberately NOT read here).
ENV_BASE_URL = "REACHY_OPENAI_URL_BASE"
ENV_API_KEY = "REACHY_OPENAI_API_KEY"
#: The NEW env this task adds: the scene/vision model id (the lobes *senses* role).
ENV_VISION_MODEL = "REACHY_VISION_MODEL_ID"

#: Default vision model — the lobes senses role (Gemma-4 12B), proven live.
DEFAULT_VISION_MODEL = "coolthor/gemma-4-12B-it-NVFP4A16"
#: Default endpoint — the same daemon-local default :mod:`reachy.speech.llm`
#: uses; the deployed box points ``REACHY_OPENAI_URL_BASE`` at the lobes gateway.
DEFAULT_BASE_URL = "http://localhost:8000"
#: A describe should surface a slow/dead endpoint in tens of seconds, not hang.
DEFAULT_TIMEOUT = 30.0
#: Resize so the frame's long edge is at most this many px before JPEG encoding
#: (mirrors ``reachy_nova.nova_vision``'s 1280 px cap).
DEFAULT_MAX_EDGE = 1280
#: JPEG quality for the encode (matches nova's 80).
DEFAULT_JPEG_QUALITY = 80

#: The instruction sent alongside the image. First-person-free, terse, factual —
#: cognition wraps it into a ``noticed: <text>`` cue, so no greeting/commentary.
DEFAULT_SCENE_PROMPT = (
    "Describe what is visible in this image in 1-2 short, plain sentences. "
    "Be specific about people, objects, and what is happening. Do not greet "
    "anyone, do not add emotional commentary, and do not say 'I see' — just "
    "state what is there."
)


class SceneError(Exception):
    """A scene-description failure — the message always names the reason.

    Raised for an unreachable/slow endpoint, a non-2xx HTTP status, a malformed
    response body, a failed JPEG encode, or a missing ``[vision]`` extra. The
    describe path guarantees this is the ONLY exception :func:`describe_frame`
    raises, so callers (the tool dispatch and :class:`SceneHook`'s worker) key on
    this one type.
    """


@dataclass
class SceneConfig:
    """Resolved scene-description connection + encode config.

    Read from the canonical ``REACHY_OPENAI_URL_BASE`` / ``REACHY_OPENAI_API_KEY``
    environment variables plus the new ``REACHY_VISION_MODEL_ID`` (defaulting to
    the lobes senses role). Legacy-free by design — see the module docstring.
    """

    base_url: str
    model: str
    api_key: str | None = None
    prompt: str = DEFAULT_SCENE_PROMPT
    timeout: float = DEFAULT_TIMEOUT
    max_edge: int = DEFAULT_MAX_EDGE
    jpeg_quality: int = field(default=DEFAULT_JPEG_QUALITY)

    @classmethod
    def from_env(cls) -> "SceneConfig":
        return cls(
            base_url=os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL),
            model=os.environ.get(ENV_VISION_MODEL, DEFAULT_VISION_MODEL),
            api_key=os.environ.get(ENV_API_KEY),
        )


# --- cv2 encode leg (lazy import) ------------------------------------------


def _import_cv2():  # type: ignore[no-untyped-def]
    """Lazily import ``cv2``; raise a :class:`SceneError` when it is absent.

    Unlike :func:`reachy.vision.face._import_cv2` (which raises a ``CliError``),
    the scene path uniformly surfaces every failure — including a missing extra —
    as a :class:`SceneError`, so the tool dispatch and the hook worker key on one
    exception type.
    """
    try:
        import cv2
    except ImportError as err:
        raise SceneError(
            "scene description needs the opencv [vision] extra "
            "(pip install 'reachy-mini-cli[vision]')"
        ) from err
    return cv2


def _encode_jpeg(
    frame: object,
    *,
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> bytes:
    """Resize *frame* so its long edge is <= *max_edge*, then JPEG-encode it.

    *frame* is a BGR ``H x W x 3`` array (OpenCV's frame shape). Raises
    :class:`SceneError` when cv2 is absent or the encode fails.
    """
    cv2 = _import_cv2()
    try:
        height, width = frame.shape[:2]  # type: ignore[attr-defined]
        long_edge = max(int(height), int(width))
        if long_edge > max_edge:
            scale = max_edge / float(long_edge)
            frame = cv2.resize(frame, (int(width * scale), int(height * scale)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    except SceneError:
        raise
    except Exception as err:  # noqa: BLE001 — any cv2/array error is a describe failure
        raise SceneError(f"failed to JPEG-encode the camera frame: {err}") from err
    if not ok:
        raise SceneError("failed to JPEG-encode the camera frame")
    return bytes(buf)


# --- request construction + HTTP leg ---------------------------------------


def _build_messages(prompt: str, data_url: str) -> list[dict]:
    """Build the multimodal user message (the LIVE-VERIFIED content shape)."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]


def _post_chat(messages: list[dict], cfg: SceneConfig) -> str:
    """POST the multimodal request and return ``choices[0].message.content``.

    The default ``transport`` leg for :func:`describe_frame`. Pure stdlib urllib.
    Maps every transport / HTTP / decode failure to a :class:`SceneError` naming
    the reason — never a raw traceback.
    """
    url = cfg.base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": messages,
        "stream": False,
        "temperature": 0.4,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    # Bearer auth only when a real key is present (treat the literal "EMPTY" as
    # "no key" for local OpenAI-compatible servers, matching reachy.speech.llm).
    if cfg.api_key and cfg.api_key != "EMPTY":
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:  # nosec B310
            raw = resp.read()
    except urllib.error.HTTPError as err:
        raise SceneError(f"scene endpoint returned HTTP {err.code} ({cfg.base_url})") from err
    except OSError as err:  # URLError + socket.timeout are OSError subclasses
        raise SceneError(
            f"vlm-unreachable: cannot reach scene endpoint at {cfg.base_url}: {err}"
        ) from err

    try:
        body = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as err:
        raise SceneError(f"malformed response from scene endpoint: {err}") from err


# --- the one shared describe path ------------------------------------------


def describe_frame(frame: object, *, transport=None, cfg: SceneConfig | None = None) -> str:
    """Describe a single camera *frame* and return the assistant's text.

    JPEG-encodes *frame* (long edge <= ``cfg.max_edge``), wraps it into the
    multimodal chat-completions request, and returns the stripped description.

    Parameters
    ----------
    frame:
        A BGR ``H x W x 3`` camera frame (OpenCV's shape).
    transport:
        The HTTP leg — a callable ``(messages, cfg) -> str``. Defaults to
        :func:`_post_chat` (real urllib). Injected in tests so nothing networks.
    cfg:
        A :class:`SceneConfig`; defaults to :meth:`SceneConfig.from_env`.

    Raises
    ------
    SceneError
        The ONLY exception this raises — for a failed encode, an
        unreachable/slow/malformed endpoint, or an empty response. Callers key on
        this single type.
    """
    cfg = cfg if cfg is not None else SceneConfig.from_env()
    jpeg = _encode_jpeg(frame, max_edge=cfg.max_edge, quality=cfg.jpeg_quality)
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    messages = _build_messages(cfg.prompt, data_url)

    post = transport if transport is not None else _post_chat
    try:
        text = post(messages, cfg)
    except SceneError:
        raise
    except Exception as err:  # noqa: BLE001 — normalise any transport error to SceneError
        raise SceneError(f"scene description request failed: {err}") from err

    if not isinstance(text, str) or not text.strip():
        raise SceneError("scene description endpoint returned an empty response")
    return text.strip()


__all__ = [
    "SceneError",
    "SceneConfig",
    "describe_frame",
    "DEFAULT_VISION_MODEL",
    "DEFAULT_SCENE_PROMPT",
    "DEFAULT_MAX_EDGE",
    "DEFAULT_TIMEOUT",
]
