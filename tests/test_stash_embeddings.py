"""Tests for the stash embeddings client (:mod:`reachy.stash.embeddings`).

Stdlib ``urllib`` only, mirroring ``reachy/speech/llm.py``'s config-resolution
style — but this module must NOT import ``reachy.speech.llm`` (the stash stays
independent of the chat client). Every unit test here fakes
``urllib.request.urlopen`` — none hits the network.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import urllib.error

import pytest

import reachy.stash.embeddings as embeddings_mod
from reachy.cli._errors import CliError
from reachy.stash.embeddings import EmbeddingConfig, embed_text


class _FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status


# ---------------------------------------------------------------------------
# Import boundary — must not import reachy.speech.llm
# ---------------------------------------------------------------------------


def _imported_modules(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def test_embeddings_module_does_not_import_speech_llm():
    for name in _imported_modules(embeddings_mod):
        assert "speech.llm" not in name, f"embeddings.py must not import the LLM client ({name!r})"
    assert "llm" not in embeddings_mod.__dict__


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_config_resolves_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("REACHY_OPENAI_URL_BASE", raising=False)
    monkeypatch.delenv("REACHY_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("REACHY_OPENAI_EMBED_MODEL_ID", raising=False)
    cfg = EmbeddingConfig.resolve()
    assert cfg.base_url == "http://localhost:8000"
    assert cfg.model == "Qwen/Qwen3-Embedding-0.6B"
    assert cfg.api_key is None


def test_config_honours_env_overrides(monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_URL_BASE", "http://localhost:8001")
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "secret-key")
    cfg = EmbeddingConfig.resolve()
    assert cfg.base_url == "http://localhost:8001"
    assert cfg.api_key == "secret-key"


def test_explicit_kwargs_win_over_env(monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_URL_BASE", "http://localhost:8001")
    cfg = EmbeddingConfig.resolve(base_url="http://example.com:9")
    assert cfg.base_url == "http://example.com:9"


# ---------------------------------------------------------------------------
# embed_text — happy path (fake urlopen)
# ---------------------------------------------------------------------------


def test_embed_text_posts_openai_shaped_payload_and_parses_vector(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        body = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
        return _FakeResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(embeddings_mod.urllib.request, "urlopen", fake_urlopen)

    vector = embed_text(
        "a soft nod",
        base_url="http://localhost:8001",
        model="Qwen/Qwen3-Embedding-0.6B",
        api_key="tok",
    )

    assert vector == [0.1, 0.2, 0.3]
    assert captured["url"] == "http://localhost:8001/v1/embeddings"
    assert captured["body"] == {"model": "Qwen/Qwen3-Embedding-0.6B", "input": "a soft nod"}
    # urllib normalizes header casing to Title-Case.
    assert captured["headers"]["Authorization"] == "Bearer tok"


def test_embed_text_skips_auth_header_when_no_api_key(monkeypatch):
    # An explicit api_key=None means "no key" here, not "fall through to the
    # environment" — clear it so a real REACHY_OPENAI_API_KEY on the host (e.g.
    # this dev box's live gateway setup) can't leak into the assertion.
    monkeypatch.delenv("REACHY_OPENAI_API_KEY", raising=False)
    captured = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["headers"] = dict(req.header_items())
        body = {"data": [{"embedding": [1.0]}]}
        return _FakeResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(embeddings_mod.urllib.request, "urlopen", fake_urlopen)
    embed_text("hi", base_url="http://localhost:8001", api_key=None)
    assert "Authorization" not in captured["headers"]


# ---------------------------------------------------------------------------
# Error contract — never a raw traceback
# ---------------------------------------------------------------------------


def test_embed_text_http_error_raises_clean_cli_error(monkeypatch):
    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, 500, "boom", None, None)

    monkeypatch.setattr(embeddings_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(CliError) as excinfo:
        embed_text("hi", base_url="http://localhost:8001")
    assert "500" in excinfo.value.message


def test_embed_text_unreachable_raises_clean_cli_error(monkeypatch):
    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        raise OSError("connection refused")

    monkeypatch.setattr(embeddings_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(CliError) as excinfo:
        embed_text("hi", base_url="http://localhost:8001")
    assert "connection refused" in excinfo.value.message.lower() or "cannot reach" in (
        excinfo.value.message.lower()
    )


def test_embed_text_malformed_response_raises_clean_cli_error(monkeypatch):
    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        return _FakeResponse(json.dumps({"oops": "no data key"}).encode("utf-8"))

    monkeypatch.setattr(embeddings_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(CliError):
        embed_text("hi", base_url="http://localhost:8001")


def test_embed_text_non_2xx_status_raises_clean_cli_error(monkeypatch):
    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        return _FakeResponse(b"{}", status=503)

    monkeypatch.setattr(embeddings_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(CliError):
        embed_text("hi", base_url="http://localhost:8001")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
