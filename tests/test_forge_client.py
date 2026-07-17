"""Tests for the forge dispatch client (:mod:`reachy.forge.client`).

Covers task t12 acceptance criterion 1 (+ the fail-closed wiring of 2/3): a
``dispatch(goal, context, improve)`` that runs the whole coder-model round-trip
on a background daemon thread, POSTs chat/completions to ``FORGE_BASE_URL`` /
``FORGE_MODEL`` (default: the lobes gateway on :8001 and model ``qwen3``) with an
optional bearer key, and turns EVERY failure path — unreachable endpoint,
timeout, unparseable reply, missing fence, bad name, failed stage, rejecting or
raising or unavailable validator, or an unexpected internal bug — into a loud
``forge/rejected`` event, never an exception on the caller's thread. The HTTP
transport is injected, so nothing here touches the network.
"""

from __future__ import annotations

import logging
import threading

import pytest

from reachy.forge.client import ForgeClient


class _Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, event_type, payload):
        self.events.append((event_type, payload))

    def types(self):
        return [e[0] for e in self.events]


class _FakeTransport:
    def __init__(self, *, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers, timeout))
        if self.exc is not None:
            raise self.exc
        return self.response


def _reply(content):
    return {"choices": [{"message": {"content": content}}]}


def _fence(label, body):
    return f"```{label}\n{body}\n```"


_GOOD_SKILL_MD = "---\nname: Wave Hello\ndescription: wave\n---\nWave when greeted.\n"
_GOOD_EXECUTOR = "def execute(params, ctx):\n    ctx.speak('hi')\n"
_GOOD_CONTENT = (
    _fence("SKILL.md", _GOOD_SKILL_MD.rstrip("\n"))
    + "\n\n"
    + _fence("executor.py", _GOOD_EXECUTOR.rstrip("\n"))
)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("FORGE_BASE_URL", raising=False)
    monkeypatch.delenv("FORGE_MODEL", raising=False)
    monkeypatch.delenv("FORGE_API_KEY", raising=False)
    monkeypatch.delenv("REACHY_OPENAI_API_KEY", raising=False)
    return tmp_path


def _run(client, *args, **kwargs):
    thread = client.dispatch(*args, **kwargs)
    thread.join(timeout=5)
    assert not thread.is_alive()
    return thread


# ---------------------------------------------------------------------------
# Background-thread dispatch + happy path
# ---------------------------------------------------------------------------


def test_dispatch_returns_a_started_daemon_thread(state):
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    client = ForgeClient(pub, validator=lambda d: (True, []), transport=transport)
    thread = client.dispatch("wave hello", {"doa": 30})
    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_happy_path_stages_and_emits_staged(state):
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    seen = {}

    def _validator(skill_dir):
        seen["dir"] = skill_dir
        return (True, [])

    _run(ForgeClient(pub, validator=_validator, transport=transport), "wave hello")

    assert seen["dir"].name == "wave-hello"
    staged = state / "forge" / "staged" / "wave-hello"
    assert (staged / "SKILL.md").exists()
    assert (staged / "executor.py").exists()
    assert "forge/staged" in pub.types()
    assert "forge/rejected" not in pub.types()


# ---------------------------------------------------------------------------
# Endpoint resolution — default lobes gateway + env overrides
# ---------------------------------------------------------------------------


def test_posts_to_default_lobes_gateway_url_and_qwen3(state):
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    url, payload, headers, _timeout = transport.calls[0]
    assert url == "http://localhost:8001/v1/chat/completions"
    assert payload["model"] == "qwen3"
    assert "Authorization" not in headers


def test_apikey_falls_back_to_reachy_openai_key(state, monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "gateway-key")
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    _url, _payload, headers, _timeout = transport.calls[0]
    assert headers["Authorization"] == "Bearer gateway-key"


def test_dedicated_forge_key_wins_over_fallback(state, monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "gateway-key")
    monkeypatch.setenv("FORGE_API_KEY", "forge-key")
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    _url, _payload, headers, _timeout = transport.calls[0]
    assert headers["Authorization"] == "Bearer forge-key"


def test_forge_api_key_empty_literal_is_treated_as_no_key(state, monkeypatch):
    """The repo-wide convention (reachy/speech/llm.py) treats the literal "EMPTY" as
    no-auth for local OpenAI-compatible servers — ForgeClient must match it."""
    monkeypatch.setenv("FORGE_API_KEY", "EMPTY")
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    _url, _payload, headers, _timeout = transport.calls[0]
    assert "Authorization" not in headers


def test_reachy_openai_api_key_empty_literal_is_treated_as_no_key(state, monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "EMPTY")
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    _url, _payload, headers, _timeout = transport.calls[0]
    assert "Authorization" not in headers


def test_forge_api_key_empty_falls_through_to_reachy_openai_key(state, monkeypatch):
    """ "EMPTY" on the dedicated key is not a real override — the shared gateway key
    (when it's a real value) still authenticates the request."""
    monkeypatch.setenv("FORGE_API_KEY", "EMPTY")
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "gateway-key")
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    _url, _payload, headers, _timeout = transport.calls[0]
    assert headers["Authorization"] == "Bearer gateway-key"


def test_env_overrides_url_model_and_apikey(state, monkeypatch):
    monkeypatch.setenv("FORGE_BASE_URL", "http://coder:9999/v1/")
    monkeypatch.setenv("FORGE_MODEL", "coder-model")
    monkeypatch.setenv("FORGE_API_KEY", "secret")
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    url, payload, headers, _timeout = transport.calls[0]
    assert url == "http://coder:9999/v1/chat/completions"
    assert payload["model"] == "coder-model"
    assert headers["Authorization"] == "Bearer secret"


def test_improve_and_context_reach_the_payload(state):
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    _run(
        ForgeClient(pub, validator=lambda d: (True, []), transport=transport),
        "improve the wave",
        {"doa": 42},
        "the old wave was too fast",
    )
    _url, payload, _headers, _timeout = transport.calls[0]
    blob = "".join(m["content"] for m in payload["messages"])
    assert "improve the wave" in blob
    assert "42" in blob
    assert "too fast" in blob


# ---------------------------------------------------------------------------
# Every failure path -> a loud forge/rejected, never an exception
# ---------------------------------------------------------------------------


def test_unreachable_endpoint_rejects_loudly(state, caplog):
    pub = _Recorder()
    transport = _FakeTransport(exc=ConnectionError("connection refused"))
    with caplog.at_level(logging.WARNING):
        _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    assert pub.types() == ["forge/rejected"]
    assert "unreachable" in pub.events[0][1]["reason"]
    assert caplog.text  # loud


def test_timeout_rejects(state, caplog):
    pub = _Recorder()
    transport = _FakeTransport(exc=TimeoutError("slow"))
    with caplog.at_level(logging.WARNING):
        _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    assert pub.events[0][0] == "forge/rejected"
    assert "timed out" in pub.events[0][1]["reason"]


def test_unparseable_reply_shape_rejects(state):
    pub = _Recorder()
    transport = _FakeTransport(response={"nope": True})
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    assert pub.events[0][0] == "forge/rejected"
    assert "unparseable" in pub.events[0][1]["reason"]


def test_missing_skill_md_fence_rejects(state):
    pub = _Recorder()
    content = _fence("executor.py", _GOOD_EXECUTOR.rstrip("\n"))
    transport = _FakeTransport(response=_reply(content))
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    assert pub.events[0][0] == "forge/rejected"
    assert "SKILL.md" in pub.events[0][1]["reason"]


def test_missing_executor_fence_rejects(state):
    pub = _Recorder()
    content = _fence("SKILL.md", _GOOD_SKILL_MD.rstrip("\n"))
    transport = _FakeTransport(response=_reply(content))
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    assert pub.events[0][0] == "forge/rejected"
    assert "executor.py" in pub.events[0][1]["reason"]


def test_invalid_name_rejects(state):
    pub = _Recorder()
    bad_md = "---\nname: ///\ndescription: x\n---\nbody\n"
    content = (
        _fence("SKILL.md", bad_md.rstrip("\n"))
        + "\n\n"
        + _fence("executor.py", _GOOD_EXECUTOR.rstrip("\n"))
    )
    transport = _FakeTransport(response=_reply(content))
    _run(ForgeClient(pub, validator=lambda d: (True, []), transport=transport), "g")
    assert pub.events[0][0] == "forge/rejected"
    assert "name" in pub.events[0][1]["reason"]


def test_internal_error_resolves_to_rejected(state, caplog):
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    # A non-string dict key can't be json-serialized even with default=str,
    # so _build_messages raises -> the outer safety net must catch it.
    bad_context = {("tuple", "key"): 1}
    with caplog.at_level(logging.WARNING):
        _run(
            ForgeClient(pub, validator=lambda d: (True, []), transport=transport),
            "g",
            bad_context,
        )
    assert pub.types() == ["forge/rejected"]
    assert "internal error" in pub.events[0][1]["reason"]


# ---------------------------------------------------------------------------
# Defensive fence parsing — content-sniffing fallback
# ---------------------------------------------------------------------------


def test_content_sniffing_fallback_parses_mislabeled_fences(state):
    pub = _Recorder()
    seen = {}

    def _validator(skill_dir):
        seen["dir"] = skill_dir
        return (True, [])

    content = (
        _fence("markdown", _GOOD_SKILL_MD.rstrip("\n"))
        + "\n\n"
        + _fence("python", _GOOD_EXECUTOR.rstrip("\n"))
    )
    transport = _FakeTransport(response=_reply(content))
    _run(ForgeClient(pub, validator=_validator, transport=transport), "g")
    assert seen.get("dir") is not None
    assert "forge/staged" in pub.types()


# ---------------------------------------------------------------------------
# Validator gate: staged fires ONLY after validation passes
# ---------------------------------------------------------------------------


def test_validator_rejection_moves_to_rejected_no_staged(state, caplog):
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    with caplog.at_level(logging.WARNING):
        _run(
            ForgeClient(
                pub,
                validator=lambda d: (False, ["import 'os' is not allowed (line 1)"]),
                transport=transport,
            ),
            "g",
        )
    assert "forge/staged" not in pub.types()
    assert "forge/rejected" in pub.types()
    assert pub.events[-1][1]["reasons"] == ["import 'os' is not allowed (line 1)"]
    assert (state / "forge" / "staged" / ".rejected" / "wave-hello").exists()
    assert not (state / "forge" / "staged" / "wave-hello").exists()


def test_validator_raising_rejects_not_staged(state, caplog):
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))

    def _boom(_skill_dir):
        raise RuntimeError("validator bug")

    with caplog.at_level(logging.WARNING):
        _run(ForgeClient(pub, validator=_boom, transport=transport), "g")
    assert pub.types() == ["forge/rejected"]
    assert "validator error" in pub.events[0][1]["reason"]


def test_validator_unavailable_fails_closed(state, caplog):
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    with caplog.at_level(logging.WARNING):
        _run(
            ForgeClient(pub, transport=transport, validator_factory=lambda: None),
            "g",
        )
    assert pub.types() == ["forge/rejected"]
    assert "validator unavailable" in pub.events[0][1]["reason"]


# ---------------------------------------------------------------------------
# End-to-end against the REAL in-package AST validator (no injection)
# ---------------------------------------------------------------------------


def test_real_validator_passes_a_clean_generated_skill(state):
    pub = _Recorder()
    transport = _FakeTransport(response=_reply(_GOOD_CONTENT))
    _run(ForgeClient(pub, transport=transport), "g")  # no validator= -> uses the real one
    assert "forge/staged" in pub.types()


def test_real_validator_rejects_forbidden_generated_code(state):
    pub = _Recorder()
    bad_exec = "import os\n\n\ndef execute(params, ctx):\n    return os.getcwd()\n"
    content = (
        _fence("SKILL.md", _GOOD_SKILL_MD.rstrip("\n")) + "\n\n" + _fence("executor.py", bad_exec)
    )
    transport = _FakeTransport(response=_reply(content))
    _run(ForgeClient(pub, transport=transport), "g")
    assert "forge/staged" not in pub.types()
    assert "forge/rejected" in pub.types()


def test_allowed_ctx_attrs_injected_through_client(state):
    pub = _Recorder()
    custom_exec = "def execute(params, ctx):\n    ctx.wiggle()\n"
    content = (
        _fence("SKILL.md", _GOOD_SKILL_MD.rstrip("\n"))
        + "\n\n"
        + _fence("executor.py", custom_exec)
    )
    transport = _FakeTransport(response=_reply(content))
    # ctx.wiggle would be rejected by the default surface; injecting it allows it.
    _run(ForgeClient(pub, transport=transport, allowed_ctx_attrs={"wiggle"}), "g")
    assert "forge/staged" in pub.types()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
