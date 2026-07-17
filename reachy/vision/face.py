"""Face detection + embedding engine — OpenCV YuNet + SFace.

Cited from ``reachy_nova.face_recognition`` (cite-don't-import): OpenCV's
built-in YuNet face detector and SFace 128-dim face-recognition embedder,
largest-face selection, ``alignCrop`` + ``feature`` extraction. Deviations
from nova, and why:

* **``cv2`` is imported lazily, inside functions only — never at module import
  time.** This keeps :mod:`reachy.vision.face` (and anything that merely
  imports it, e.g. a future hook) loadable on a bare install with no OpenCV
  present. A missing ``opencv-python-headless`` surfaces as the repo's
  established clean exit-2 :class:`~reachy.cli._errors.CliError` pointing at
  the ``[vision]`` extra — the same pattern
  :mod:`reachy.robot.sdk_transport` uses for the ``[sdk]`` extra.
* **No threading / no background dispatch loop.** Nova's ``FaceRecognition``
  owns a daemon thread, a busy flag, and a fixed 500 ms detect interval
  (``update_frame`` dispatches to ``_run_detection`` on a background thread).
  That loop-owning responsibility belongs to the live ``listen --live``
  ``FaceHook`` — a separate task, out of scope here. :class:`FaceEngine` is a
  synchronous, stateless-per-call ``detect(frame)``; the hook throttles and
  calls it from its own tick, exactly like the vision motion/light detectors.
* **Models auto-download under ``state_dir()/models/``**, not
  ``~/.reachy_nova/models`` — same per-user state dir every other stateful
  piece of this repo uses. Downloads also get a **size sanity check**: nova
  trusts ``urlretrieve`` blindly, but a truncated download, a dropped
  connection, or an HTML error page swapped in by a captive portal would
  otherwise be written to disk, "exist" on the next run, and then fail
  cryptically inside OpenCV instead of at download time. A download that
  lands under the expected floor is rejected and never renamed into place.
* **No ``on_face_bbox`` callback, no admin auth, no re-announce cooldown.**
  Cue emission / cooldown timing is a hook-loop concern (a future task);
  admin auth is explicitly out of scope for this port.
"""

from __future__ import annotations

import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from reachy.cli._errors import EXIT_ENV_ERROR, CliError

logger = logging.getLogger(__name__)

# --- OpenCV model zoo — same models/URLs reachy_nova uses -------------------
YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
    "face_detection_yunet_2023mar.onnx"
)
SFACE_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
    "face_recognition_sface_2021dec.onnx"
)
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
SFACE_FILE = "face_recognition_sface_2021dec.onnx"

# Sanity floors for the size check below. A genuine download is ~230 KB
# (YuNet) / ~37 MB (SFace); a truncated download or a swapped-in HTML error
# page is nowhere near these sizes, so a generous floor well below the real
# size still catches a corrupt download without being fragile to the model
# zoo's file growing a little across revisions.
YUNET_MIN_BYTES = 100_000
SFACE_MIN_BYTES = 20_000_000

MODELS_DIRNAME = "models"

DEFAULT_SCORE_THRESHOLD = 0.6
DEFAULT_NMS_THRESHOLD = 0.3
DEFAULT_TOP_K = 5
DEFAULT_INPUT_SIZE = (320, 320)

#: 128 — the fixed embedding width SFace produces (documented for callers that
#: want to validate a stored/incoming vector's shape without loading cv2).
EMBEDDING_DIM = 128


@dataclass(frozen=True)
class FaceDetection:
    """One detected face: its normalised bounding box and 128-dim embedding."""

    #: ``(x1, y1, x2, y2)`` in ``[0, 1]``, normalised by frame width/height.
    bbox_norm: tuple[float, float, float, float]
    embedding: np.ndarray


def _import_cv2():  # type: ignore[no-untyped-def]
    """Lazily import ``cv2``; raise a clean exit-2 CliError when it's absent."""
    try:
        import cv2
    except ImportError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="the opencv face-recognition engine is not installed",
            remediation="install the vision extra: pip install 'reachy-mini-cli[vision]'",
        ) from err
    return cv2


def _default_models_dir() -> Path:
    from reachy.daemon import state_dir

    return state_dir() / MODELS_DIRNAME


def _download(url: str, dest: Path, *, timeout: float = 30.0) -> None:
    """Stream *url* to *dest* (stdlib ``urllib`` only, chunked write)."""
    req = urllib.request.Request(url, headers={"User-Agent": "reachy-mini-cli"})
    with urllib.request.urlopen(  # nosec B310 - fixed https model-zoo URL, not user input
        req, timeout=timeout
    ) as resp:
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)


def _ensure_model(filename: str, url: str, *, min_bytes: int, models_dir: Path) -> Path:
    """Download *filename* from *url* into *models_dir* if not already present.

    Downloads to a ``.part`` sibling first and only renames it into place once
    the transfer completes AND clears the ``min_bytes`` sanity floor, so a
    partial/failed/corrupt download can never masquerade as a usable model
    file on a later call.
    """
    path = models_dir / filename
    if path.exists():
        return path

    models_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = models_dir / f"{filename}.part"
    logger.info("downloading %s ...", filename)
    try:
        _download(url, tmp_path)
        size = tmp_path.stat().st_size
        if size < min_bytes:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"downloaded {filename} looks truncated ({size} bytes)",
                remediation="check network connectivity and retry",
            )
        tmp_path.rename(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    logger.info("downloaded %s (%d bytes)", filename, path.stat().st_size)
    return path


class FaceEngine:
    """Detect the largest face in a frame and extract its 128-dim embedding.

    Lazily loads the YuNet detector + SFace recognizer (via :func:`_import_cv2`
    and :func:`_ensure_model`) on the first :meth:`detect` call; a missing
    ``[vision]`` extra surfaces as a clean exit-2 :class:`CliError` at that
    point, not at construction or import time.

    Parameters
    ----------
    models_dir:
        Directory model files are downloaded into/read from. Defaults to
        ``state_dir()/models``; tests inject an isolated ``tmp_path``.
    score_threshold, nms_threshold, top_k:
        Passed straight through to ``cv2.FaceDetectorYN.create`` — see the
        OpenCV docs. Defaults match nova's.
    """

    def __init__(
        self,
        *,
        models_dir: Path | None = None,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        nms_threshold: float = DEFAULT_NMS_THRESHOLD,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        self._models_dir = models_dir
        self._score_threshold = score_threshold
        self._nms_threshold = nms_threshold
        self._top_k = top_k
        self._detector = None
        self._recognizer = None

    def _resolve_models_dir(self) -> Path:
        return self._models_dir if self._models_dir is not None else _default_models_dir()

    def _load(self) -> None:
        """Lazily construct the YuNet detector + SFace recognizer (once)."""
        if self._detector is not None and self._recognizer is not None:
            return

        cv2 = _import_cv2()
        models_dir = self._resolve_models_dir()
        yunet_path = _ensure_model(
            YUNET_FILE, YUNET_URL, min_bytes=YUNET_MIN_BYTES, models_dir=models_dir
        )
        sface_path = _ensure_model(
            SFACE_FILE, SFACE_URL, min_bytes=SFACE_MIN_BYTES, models_dir=models_dir
        )

        self._detector = cv2.FaceDetectorYN.create(
            str(yunet_path),
            "",
            DEFAULT_INPUT_SIZE,
            score_threshold=self._score_threshold,
            nms_threshold=self._nms_threshold,
            top_k=self._top_k,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(str(sface_path), "")

    def detect(self, frame: np.ndarray) -> FaceDetection | None:
        """Detect the largest face in *frame* (BGR ``H x W x 3``) and embed it.

        Returns ``None`` when no face is found in the frame. Raises
        :class:`CliError` (exit 2) the first time this is called without the
        ``[vision]`` extra installed.
        """
        self._load()

        h, w = frame.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(frame)
        if faces is None or len(faces) == 0:
            return None

        # Largest-face selection (width * height), mirrors nova.
        areas = faces[:, 2] * faces[:, 3]
        best_idx = int(np.argmax(areas))
        best_face = faces[best_idx]

        bx, by, bw, bh = (float(best_face[i]) for i in range(4))
        bbox_norm = (bx / w, by / h, (bx + bw) / w, (by + bh) / h)

        aligned = self._recognizer.alignCrop(frame, best_face)
        embedding = self._recognizer.feature(aligned).flatten()

        return FaceDetection(bbox_norm=bbox_norm, embedding=embedding)
