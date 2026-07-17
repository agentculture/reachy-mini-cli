"""Tests for reachy.vision.face + reachy.vision.face_store (task t8).

TDD — these tests define the contract; the implementation must satisfy them.

``FaceStore``'s matching math (cosine threshold, temporary-vs-permanent
tiers, TTL expiry) and its on-disk persistence are pure ``numpy`` + stdlib —
every test in ``TestFaceStore*`` below runs with **no cv2 and no robot**,
using synthetic 128-dim embeddings.

``face.py``'s lazy ``cv2`` import (``_import_cv2``) and its model-download
sanity check (``_ensure_model``) are pure-Python logic and are ALSO tested
with **no cv2 required** — the missing-extra path is proven with
``monkeypatch.setitem(sys.modules, "cv2", None)``, the same seam
``tests/test_sleep_wake.py`` uses for the ``openwakeword`` engine, so the
assertion holds regardless of whether ``cv2`` happens to be installed in this
environment.

``FaceEngine.detect()`` genuinely needs cv2 — it constructs real
``cv2.FaceDetectorYN`` / ``cv2.FaceRecognizerSF`` objects and downloads the
real (~37 MB) model pair on first use. Those tests live in
``TestFaceEngineWithCv2`` and are ``pytest.importorskip("cv2")``-gated so the
suite passes either way. CI's bare ``uv sync`` never installs the ``[vision]``
extra, so that class always skips in CI — run it locally after
``uv sync --extra vision``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import numpy as np
import pytest

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.vision import face
from reachy.vision.face_store import (
    DEFAULT_MATCH_THRESHOLD,
    DEFAULT_TEMP_TTL,
    FaceStore,
    cosine_similarity,
    default_base_dir,
)

_REPO_ROOT = Path(__file__).parent.parent
_RNG = np.random.default_rng(20260717)


def _embedding(seed: int) -> np.ndarray:
    """A deterministic, non-degenerate 128-dim synthetic embedding."""
    rng = np.random.default_rng(seed)
    return rng.normal(size=128).astype(float)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Pin state-dir bookkeeping into a throwaway dir (mirrors test_daemon.py)."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))


# ---------------------------------------------------------------------------
# cosine_similarity — pure math
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        v = _embedding(1)
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        a = np.array([1.0, 0.0, 0.0])
        b = np.array([0.0, 1.0, 0.0])
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors_score_minus_one(self):
        v = _embedding(2)
        assert cosine_similarity(v, -v) == pytest.approx(-1.0)

    def test_near_zero_vector_degrades_to_zero(self):
        v = _embedding(3)
        zero = np.zeros(128)
        assert cosine_similarity(v, zero) == 0.0
        assert cosine_similarity(zero, zero) == 0.0


# ---------------------------------------------------------------------------
# FaceStore — enroll + match (permanent tier)
# ---------------------------------------------------------------------------


class TestFaceStoreEnrollAndMatch:
    def test_enroll_returns_a_face_id_and_match_finds_it(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        emb = _embedding(10)

        face_id = store.enroll("Ada Lovelace", emb)
        assert isinstance(face_id, str) and face_id

        match = store.match(emb)
        assert match is not None
        assert match.face_id == face_id
        assert match.name == "Ada Lovelace"
        assert match.score == pytest.approx(1.0, abs=1e-6)

    def test_unrelated_embedding_does_not_match(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        store.enroll("Ada Lovelace", _embedding(11))

        # A random, unrelated 128-dim vector should not cross the 0.5 threshold.
        unrelated = _embedding(999)
        assert store.match(unrelated) is None

    def test_default_threshold_is_point_five(self):
        assert DEFAULT_MATCH_THRESHOLD == 0.5
        store = FaceStore(base_dir=Path("/nonexistent"))
        assert store.threshold == 0.5

    def test_match_threshold_override(self, tmp_path):
        store = FaceStore(base_dir=tmp_path, threshold=0.5)
        emb = _embedding(20)
        # A vector at a small angle from `emb` — similarity is high but not 1.0.
        near = emb + _embedding(21) * 0.05
        store.enroll("Grace Hopper", emb)

        score = cosine_similarity(near, emb)
        # Sanity: the synthetic perturbation keeps similarity high.
        assert score > 0.9

        # A threshold above the actual score must reject the match, even
        # though the store's own default (0.5) would accept it.
        assert store.match(near, threshold=score + 0.01) is None
        assert store.match(near, threshold=score - 0.01) is not None

    def test_match_picks_the_closer_of_two_enrolled_faces(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        emb_a = _embedding(30)
        emb_b = _embedding(31)
        id_a = store.enroll("Alice", emb_a)
        store.enroll("Bob", emb_b)

        match = store.match(emb_a)
        assert match.face_id == id_a
        assert match.name == "Alice"

    def test_forget_removes_permanent_face_and_embedding_file(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        emb = _embedding(40)
        face_id = store.enroll("Carol", emb)
        emb_path = tmp_path / "embeddings" / f"{face_id}.npy"
        assert emb_path.exists()

        assert store.forget(face_id) is True
        assert store.match(emb) is None
        assert not emb_path.exists()

    def test_forget_missing_id_returns_false(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        assert store.forget("nope") is False

    def test_list_faces_reports_id_name_and_embedding_count(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        face_id = store.enroll("Dana", _embedding(50))

        listed = store.list_faces()
        assert len(listed) == 1
        assert listed[0]["id"] == face_id
        assert listed[0]["name"] == "Dana"
        assert listed[0]["num_embeddings"] == 1
        assert "created" in listed[0]

    def test_get_unique_id_is_case_insensitive(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        face_id = store.enroll("Erin Malone", _embedding(60))

        assert store.get_unique_id("erin malone") == face_id
        assert store.get_unique_id("  ERIN MALONE  ") == face_id
        assert store.get_unique_id("nobody") is None

    def test_permanent_count(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        assert store.permanent_count == 0
        store.enroll("Fay", _embedding(70))
        assert store.permanent_count == 1


# ---------------------------------------------------------------------------
# FaceStore — temporary tier + TTL expiry
# ---------------------------------------------------------------------------


class TestFaceStoreTemporaryTier:
    def test_remember_temporary_returns_a_tmp_prefixed_id(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        temp_id = store.remember_temporary(_embedding(80))
        assert temp_id.startswith("tmp_")
        assert store.temporary_count == 1

    def test_get_temporary_round_trips_the_embedding(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        emb = _embedding(81)
        temp_id = store.remember_temporary(emb)

        got = store.get_temporary(temp_id)
        assert got is not None
        assert np.allclose(got, emb)

    def test_get_temporary_returns_a_copy_not_the_original(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        temp_id = store.remember_temporary(_embedding(82))

        got = store.get_temporary(temp_id)
        got[0] = 999.0
        got_again = store.get_temporary(temp_id)
        assert got_again[0] != 999.0

    def test_get_temporary_missing_id_returns_none(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        assert store.get_temporary("tmp_nope") is None

    def test_temporary_tier_is_never_matched(self, tmp_path):
        """match() only searches the permanent tier — a temp embedding never matches."""
        store = FaceStore(base_dir=tmp_path)
        emb = _embedding(83)
        store.remember_temporary(emb)

        assert store.match(emb) is None

    def test_cleanup_expired_evicts_only_entries_past_ttl(self, tmp_path):
        store = FaceStore(base_dir=tmp_path, ttl=DEFAULT_TEMP_TTL)
        assert DEFAULT_TEMP_TTL == 15 * 60

        old_id = store.remember_temporary(_embedding(90), now=1_000.0)
        fresh_id = store.remember_temporary(_embedding(91), now=1_000.0 + DEFAULT_TEMP_TTL - 1)

        removed = store.cleanup_expired(now=1_000.0 + DEFAULT_TEMP_TTL + 1)

        assert removed == 1
        assert store.get_temporary(old_id) is None
        assert store.get_temporary(fresh_id) is not None

    def test_cleanup_expired_with_nothing_expired_returns_zero(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        store.remember_temporary(_embedding(92), now=0.0)
        assert store.cleanup_expired(now=1.0) == 0

    def test_injected_clock_drives_default_now(self, tmp_path):
        """Omitting now= falls back to the injected clock, not wall-clock time."""
        fake_time = {"t": 500.0}
        store = FaceStore(base_dir=tmp_path, clock=lambda: fake_time["t"])

        temp_id = store.remember_temporary(_embedding(93))
        fake_time["t"] = 500.0 + DEFAULT_TEMP_TTL + 1
        removed = store.cleanup_expired()

        assert removed == 1
        assert store.get_temporary(temp_id) is None


# ---------------------------------------------------------------------------
# FaceStore — persistence round trip
# ---------------------------------------------------------------------------


class TestFaceStorePersistence:
    def test_enrolled_face_survives_a_new_store_instance(self, tmp_path):
        store_a = FaceStore(base_dir=tmp_path)
        emb = _embedding(100)
        face_id = store_a.enroll("Grace", emb)

        store_b = FaceStore(base_dir=tmp_path)  # fresh instance, same directory
        match = store_b.match(emb)

        assert match is not None
        assert match.face_id == face_id
        assert match.name == "Grace"

    def test_index_json_and_npy_files_land_on_disk(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        face_id = store.enroll("Heidi", _embedding(101))

        index_path = tmp_path / "faces.json"
        emb_path = tmp_path / "embeddings" / f"{face_id}.npy"
        assert index_path.exists()
        assert emb_path.exists()

        body = json.loads(index_path.read_text(encoding="utf-8"))
        assert face_id in body
        assert body[face_id]["name"] == "Heidi"
        assert body[face_id]["embedding_files"] == [f"{face_id}.npy"]

        stored = np.load(emb_path)
        assert stored.shape == (128,)

    def test_default_base_dir_resolves_under_state_dir(self, tmp_path):
        # REACHY_STATE_DIR is pinned to tmp_path by the autouse fixture.
        assert default_base_dir() == tmp_path / "faces"

    def test_corrupt_index_degrades_to_empty_without_raising(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "faces.json").write_text("{ not valid json", encoding="utf-8")

        store = FaceStore(base_dir=tmp_path)
        store.load()  # must not raise

        assert store.permanent_count == 0
        assert store.list_faces() == []

    def test_missing_embedding_file_is_skipped_not_raised(self, tmp_path):
        store = FaceStore(base_dir=tmp_path)
        face_id = store.enroll("Ivy", _embedding(102))
        (tmp_path / "embeddings" / f"{face_id}.npy").unlink()

        # match() must tolerate a missing on-disk embedding, not raise.
        assert store.match(_embedding(102)) is None


# ---------------------------------------------------------------------------
# face.py — lazy cv2 import
# ---------------------------------------------------------------------------


class TestImportCv2:
    def test_missing_cv2_raises_clean_exit2_cli_error(self, monkeypatch):
        """Simulate cv2 being uninstalled, regardless of the real environment.

        Same seam tests/test_sleep_wake.py uses for the openwakeword engine:
        setting sys.modules[name] = None makes `import name` raise ImportError.
        monkeypatch reverts this automatically at teardown.
        """
        monkeypatch.setitem(sys.modules, "cv2", None)  # type: ignore[arg-type]

        with pytest.raises(CliError) as excinfo:
            face._import_cv2()

        assert excinfo.value.code == EXIT_ENV_ERROR
        assert "opencv" in excinfo.value.message.lower()
        assert "reachy-mini-cli[vision]" in excinfo.value.remediation

    def test_face_engine_detect_surfaces_the_same_clean_error(self, monkeypatch, tmp_path):
        monkeypatch.setitem(sys.modules, "cv2", None)  # type: ignore[arg-type]

        engine = face.FaceEngine(models_dir=tmp_path)
        frame = np.zeros((10, 10, 3), dtype=np.uint8)

        with pytest.raises(CliError) as excinfo:
            engine.detect(frame)

        assert excinfo.value.code == EXIT_ENV_ERROR


# ---------------------------------------------------------------------------
# face.py — model auto-download + size sanity check (no cv2 required)
# ---------------------------------------------------------------------------


class TestEnsureModel:
    def test_downloads_when_missing(self, tmp_path, monkeypatch):
        calls = []

        def _fake_download(url, dest, *, timeout=30.0):
            calls.append((url, dest))
            dest.write_bytes(b"x" * 150_000)

        monkeypatch.setattr(face, "_download", _fake_download)

        path = face._ensure_model(
            "model.onnx",
            "https://example.invalid/model.onnx",
            min_bytes=100_000,
            models_dir=tmp_path,
        )

        assert path == tmp_path / "model.onnx"
        assert path.stat().st_size == 150_000
        assert len(calls) == 1

    def test_skips_download_when_already_present(self, tmp_path, monkeypatch):
        tmp_path.mkdir(parents=True, exist_ok=True)
        existing = tmp_path / "model.onnx"
        existing.write_bytes(b"y" * 500_000)

        def _fail(*_a, **_k):
            raise AssertionError("must not download when the model file already exists")

        monkeypatch.setattr(face, "_download", _fail)

        path = face._ensure_model(
            "model.onnx",
            "https://example.invalid/model.onnx",
            min_bytes=100_000,
            models_dir=tmp_path,
        )

        assert path == existing
        assert path.stat().st_size == 500_000

    def test_rejects_a_truncated_download(self, tmp_path, monkeypatch):
        def _fake_download(url, dest, *, timeout=30.0):
            dest.write_bytes(b"x" * 10)  # far below any sane floor

        monkeypatch.setattr(face, "_download", _fake_download)

        with pytest.raises(CliError) as excinfo:
            face._ensure_model(
                "model.onnx",
                "https://example.invalid/model.onnx",
                min_bytes=100_000,
                models_dir=tmp_path,
            )

        assert excinfo.value.code == EXIT_ENV_ERROR
        assert not (tmp_path / "model.onnx").exists()
        assert not (tmp_path / "model.onnx.part").exists()

    def test_rejects_download_with_network_error_and_cleans_up_partial(self, tmp_path, monkeypatch):
        def _raising_download(url, dest, *, timeout=30.0):
            dest.write_bytes(b"partial")
            raise urllib.error.URLError("boom")

        monkeypatch.setattr(face, "_download", _raising_download)

        with pytest.raises(urllib.error.URLError):
            face._ensure_model(
                "model.onnx",
                "https://example.invalid/model.onnx",
                min_bytes=100_000,
                models_dir=tmp_path,
            )

        assert not (tmp_path / "model.onnx.part").exists()
        assert not (tmp_path / "model.onnx").exists()

    def test_real_model_constants_clear_their_own_sanity_floors(self):
        """Guard against the floors and the real files drifting apart silently."""
        assert face.YUNET_MIN_BYTES < 300_000  # real file is ~230KB
        assert face.SFACE_MIN_BYTES < 39_000_000  # real file is ~37MB


# ---------------------------------------------------------------------------
# face.py / face_store.py — import boundary: no cv2 at plain import time
# ---------------------------------------------------------------------------


def _module_pulls_in(dotted: str, forbidden: str) -> bool:
    """True if importing *dotted* pulls *forbidden* into sys.modules.

    Run in a fresh SUBPROCESS so the probe has zero effect on this
    interpreter's sys.modules (mirrors tests/test_sleep_boundary.py /
    tests/test_sleep_wakeword.py).
    """
    code = f"import sys; import {dotted}; print({forbidden!r} in sys.modules)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    return proc.stdout.strip() == "True"


class TestCv2ImportBoundary:
    def test_face_module_does_not_import_cv2_at_module_load(self):
        assert _module_pulls_in("reachy.vision.face", "cv2") is False

    def test_face_store_module_does_not_import_cv2_at_module_load(self):
        assert _module_pulls_in("reachy.vision.face_store", "cv2") is False


# ---------------------------------------------------------------------------
# FaceEngine — tests that genuinely need cv2 (skip-marked)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _real_models_dir(tmp_path_factory):
    """Session-scoped dir for real YuNet/SFace downloads.

    Session-scoped so every test in ``TestFaceEngineWithCv2`` shares one
    download instead of re-fetching the ~37MB SFace model per test.
    """
    pytest.importorskip("cv2")
    return tmp_path_factory.mktemp("face_models")


class TestFaceEngineWithCv2:
    """Genuinely needs cv2 — skip-marked so the suite is green without it.

    CI's bare ``uv sync`` never installs ``[vision]``, so this class always
    skips in CI. Run locally with ``uv sync --extra vision``.
    """

    def test_detect_returns_none_on_a_faceless_frame(self, _real_models_dir):
        pytest.importorskip("cv2")
        engine = face.FaceEngine(models_dir=_real_models_dir)
        frame = np.zeros((240, 320, 3), dtype=np.uint8)  # solid black — no face

        result = engine.detect(frame)

        assert result is None
        assert (_real_models_dir / face.YUNET_FILE).stat().st_size >= face.YUNET_MIN_BYTES
        assert (_real_models_dir / face.SFACE_FILE).stat().st_size >= face.SFACE_MIN_BYTES

    def test_detect_finds_a_real_face_and_embeds_it(self, _real_models_dir):
        cv2 = pytest.importorskip("cv2")
        import urllib.request

        image_path = _real_models_dir / "sample_face.jpg"
        if not image_path.exists():
            try:
                # NOTE: must use the github.com "raw/main" form, not
                # raw.githubusercontent.com — this asset is Git-LFS-tracked and
                # only the former redirects through to the resolved LFS bytes;
                # the latter serves the bare LFS pointer text (learned by
                # inspecting a failed decode during t8 verification).
                urllib.request.urlretrieve(  # nosec B310 - test-only, stable sample asset
                    "https://github.com/opencv/opencv_zoo/raw/main/models/"
                    "face_detection_yunet/example_outputs/largest_selfie.jpg",
                    str(image_path),
                )
            except OSError:
                pytest.skip(
                    "sample face image fetch failed (network) — skipping real-face assertion"
                )

        frame = cv2.imread(str(image_path))
        if frame is None:
            pytest.skip("sample face image failed to decode — skipping real-face assertion")

        engine = face.FaceEngine(models_dir=_real_models_dir)
        result = engine.detect(frame)

        assert result is not None
        assert result.embedding.shape == (face.EMBEDDING_DIM,)
        assert all(0.0 <= c <= 1.0 for c in result.bbox_norm)
