"""Shared pytest fixtures.

Today this file exists for exactly one thing: the ``offline`` marker's guard.
``offline`` is registered in ``pyproject.toml``'s ``[tool.pytest.ini_options]``
(the project's existing home for pytest config); this module supplies the
behavior behind it.

A test decorated ``@pytest.mark.offline`` gets two things, and ONLY while that
marker is present on the test:

1. Every external service env var the CLI can read (``REACHY_OPENAI_URL_BASE``,
   the legacy ``REACHY_LLM_BASE_URL`` fallback, ``REACHY_TTS_URL``,
   ``REACHY_STT_URL``, ``FORGE_BASE_URL``) is pointed at a guaranteed-unreachable
   loopback address.
2. Real network connects are blocked outright: ``socket.socket.connect`` and
   ``socket.create_connection`` are monkeypatched to raise ``AssertionError`` —
   so a stray network call inside an ``offline``-marked test is a loud,
   immediate test failure, never a silent pass or a slow hang/timeout.

The guard fixture is ``autouse`` at collection time but a no-op for every test
that is NOT marked ``offline`` (see the ``get_closest_marker`` check below) — it
must never leak into the other 1000+ tests in the suite.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

#: Loopback + a port nothing listens on: connect-refused is immediate and local
#: (no DNS, no external dependency, no flakiness) even before our guard fires.
_UNREACHABLE = "http://127.0.0.1:1"

#: Every external HTTP leg the CLI can reach out over, by env var name — the
#: canonical LLM/TTS/STT/forge endpoints plus the legacy REACHY_LLM_* alias
#: reachy.speech.llm.py still honours as a fallback. REACHY_VISION_MODEL_ID is
#: deliberately excluded: it names a model, not an endpoint (it rides
#: REACHY_OPENAI_URL_BASE, already covered).
_SERVICE_ENV_VARS = (
    "REACHY_OPENAI_URL_BASE",
    "REACHY_LLM_BASE_URL",
    "REACHY_TTS_URL",
    "REACHY_STT_URL",
    "FORGE_BASE_URL",
)


def _deny_network(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("offline lane: network call attempted")


@pytest.fixture(autouse=True)
def _offline_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Point every service env var offline + block real sockets — ``offline`` tests only.

    Scoped via ``request.node.get_closest_marker("offline")``: for any test that
    does not carry ``@pytest.mark.offline`` this fixture does nothing at all, so
    the rest of the suite is byte-for-byte unaffected.
    """
    if request.node.get_closest_marker("offline") is None:
        yield
        return

    for name in _SERVICE_ENV_VARS:
        monkeypatch.setenv(name, _UNREACHABLE)

    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket, "create_connection", _deny_network)

    yield
