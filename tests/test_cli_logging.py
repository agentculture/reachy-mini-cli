"""Tests for the shared logging installer (``reachy.cli._logging``).

Every module in this codebase logs via ``logging.getLogger(__name__)``, but
until this module existed nothing ever attached a handler or called
``logging.basicConfig`` — so INFO-level traces were silently dropped by
Python's "last resort" handler (WARNING+ only, see the stdlib docs for
``logging.Logger.callHandlers``). :func:`install_logging` fixes that for the
three long-running foreground loops (``listen run`` / ``think run`` /
``sleep run``):

* it attaches exactly ONE ``StreamHandler(sys.stderr)`` to the ``"reachy"``
  logger (the common ancestor every ``reachy.*`` module logger propagates
  to), with a level resolved ``--log-level`` flag > ``REACHY_LOG_LEVEL`` env >
  a caller-supplied default (``"INFO"`` for the three loops);
* a second call is a no-op for the handler — no duplicate handler object, no
  duplicate log lines;
* the handler is stderr-only, by construction, so ``listen run --live
  --export -``'s stdout stays pure JSONL.

This file also covers the CLI wiring: ``listen run`` / ``think run`` /
``sleep run`` each expose ``--log-level`` and call :func:`install_logging` at
run entry.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys

import pytest

from reachy.cli._logging import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENV,
    add_log_level_arg,
    install_logging,
    resolve_log_level,
)

_LOGGER_NAME = "reachy"


@pytest.fixture(autouse=True)
def _clean_reachy_logger():
    """Isolate each test: snapshot then fully restore the shared 'reachy' logger.

    ``install_logging`` mutates process-global logging state (handlers + level
    on a module-level logger object), so without this fixture one test's
    installed handler would leak into the next test — and into unrelated
    suites in a full-repo run.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers = []
    yield
    logger.handlers = original_handlers
    logger.setLevel(original_level)
    logger.propagate = original_propagate


# --- install_logging: handler shape ----------------------------------------


def test_install_logging_attaches_a_single_stderr_handler() -> None:
    stream = io.StringIO()
    handler = install_logging("INFO", stream=stream)
    logger = logging.getLogger(_LOGGER_NAME)
    assert handler in logger.handlers
    assert len(logger.handlers) == 1
    assert isinstance(handler, logging.StreamHandler)


def test_handler_targets_stderr_never_stdout() -> None:
    """Export purity: the handler must write to stderr, never stdout.

    Under ``listen run --live --export -`` stdout is reserved for the pure
    JSONL feed (see ``reachy.cli._export``) — a log line landing on stdout
    would corrupt that feed.
    """
    handler = install_logging("INFO")
    assert handler.stream is sys.stderr
    assert handler.stream is not sys.stdout


def test_returns_a_stream_handler_instance() -> None:
    handler = install_logging("INFO", stream=io.StringIO())
    assert isinstance(handler, logging.Handler)


# --- idempotency -------------------------------------------------------


def test_double_install_reuses_the_same_handler() -> None:
    stream = io.StringIO()
    first = install_logging("INFO", stream=stream)
    second = install_logging("INFO", stream=stream)
    logger = logging.getLogger(_LOGGER_NAME)
    assert first is second
    assert len(logger.handlers) == 1


def test_double_install_emits_no_duplicate_lines() -> None:
    stream = io.StringIO()
    install_logging("INFO", stream=stream)
    install_logging("INFO", stream=stream)
    logging.getLogger("reachy.somewhere").info("hello-once")
    lines = [ln for ln in stream.getvalue().splitlines() if "hello-once" in ln]
    assert len(lines) == 1


def test_triple_install_still_a_single_handler() -> None:
    stream = io.StringIO()
    for _ in range(3):
        install_logging("DEBUG", stream=stream)
    logger = logging.getLogger(_LOGGER_NAME)
    assert len(logger.handlers) == 1


# --- level resolution: flag > env > default ---------------------------------


def test_default_level_is_info() -> None:
    stream = io.StringIO()
    install_logging(None, stream=stream)
    logger = logging.getLogger(_LOGGER_NAME)
    assert logger.level == logging.INFO
    logging.getLogger("reachy.somewhere").debug("hidden-debug-line")
    logging.getLogger("reachy.somewhere").info("shown-info-line")
    out = stream.getvalue()
    assert "hidden-debug-line" not in out
    assert "shown-info-line" in out


def test_explicit_flag_takes_precedence_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "ERROR")
    stream = io.StringIO()
    install_logging("DEBUG", stream=stream)
    logger = logging.getLogger(_LOGGER_NAME)
    assert logger.level == logging.DEBUG


def test_env_used_when_flag_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "WARNING")
    stream = io.StringIO()
    install_logging(None, stream=stream)
    logger = logging.getLogger(_LOGGER_NAME)
    assert logger.level == logging.WARNING


def test_default_used_when_no_flag_and_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    stream = io.StringIO()
    install_logging(None, stream=stream)
    logger = logging.getLogger(_LOGGER_NAME)
    assert logging.getLevelName(logger.level) == DEFAULT_LOG_LEVEL


def test_level_resolution_is_case_insensitive() -> None:
    stream = io.StringIO()
    install_logging("debug", stream=stream)
    logger = logging.getLogger(_LOGGER_NAME)
    assert logger.level == logging.DEBUG


def test_unknown_level_name_raises_value_error() -> None:
    stream = io.StringIO()
    with pytest.raises(ValueError):
        install_logging("NOT_A_REAL_LEVEL", stream=stream)


def test_resolve_log_level_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "WARNING")
    assert resolve_log_level("DEBUG") == "DEBUG"
    assert resolve_log_level(None) == "WARNING"
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    assert resolve_log_level(None) == DEFAULT_LOG_LEVEL


# --- add_log_level_arg -------------------------------------------------


def test_add_log_level_arg_defaults_to_none() -> None:
    parser = argparse.ArgumentParser()
    add_log_level_arg(parser)
    args = parser.parse_args([])
    assert args.log_level is None


def test_add_log_level_arg_accepts_the_flag() -> None:
    parser = argparse.ArgumentParser()
    add_log_level_arg(parser)
    args = parser.parse_args(["--log-level", "DEBUG"])
    assert args.log_level == "DEBUG"


# --- wiring: listen / think / sleep run each expose --log-level ------------


def test_listen_run_parser_has_log_level_flag() -> None:
    from reachy.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["listen", "run", "--log-level", "DEBUG"])
    assert args.log_level == "DEBUG"


def test_think_run_parser_has_log_level_flag() -> None:
    from reachy.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["think", "run", "--log-level", "WARNING"])
    assert args.log_level == "WARNING"


def test_sleep_run_parser_has_log_level_flag() -> None:
    from reachy.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["sleep", "run", "--log-level", "ERROR"])
    assert args.log_level == "ERROR"


# --- wiring: cmd_*_run actually installs logging at run entry --------------


class _FakeListenTransport:
    """Mirrors ``tests/test_listen_cli.py``'s ``_FakeTransport``: records
    gotos, no mic (``doa`` returns ``None``) so the loop reacts to nothing."""

    name = "fake"

    def __init__(self) -> None:
        self.gotos: list[dict] = []

    def move_goto(self, **kwargs: object) -> object:
        self.gotos.append(kwargs)
        return {"uuid": "x"}

    def doa(self, *, timeout: float | None = None) -> object:
        return None


def test_listen_run_installs_logging_at_the_resolved_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    import reachy.cli._commands.listen as listen_mod

    tr = _FakeListenTransport()
    monkeypatch.setattr(listen_mod, "get_transport", lambda args: tr)

    from reachy.cli import main

    rc = main(["listen", "run", "--log-level", "DEBUG", "--max-ticks", "1"])
    assert rc == 0
    logger = logging.getLogger(_LOGGER_NAME)
    assert logger.level == logging.DEBUG
    assert any(h.stream is sys.stderr for h in logger.handlers)


class _ThinkRecorder:
    def synth(self, text: str, **_kw: object) -> bytes:
        return ("pcm:" + text).encode("utf-8")

    def play(self, pcm: bytes, **_kw: object) -> None:
        pass


def test_think_run_installs_logging_at_the_resolved_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    import reachy.cli._commands.think as think_mod

    rec = _ThinkRecorder()

    def fake_stream(messages: object, **_kw: object):
        yield "Okay."

    def fake_feed(buffer: object) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: fake_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)

    from reachy.cli import main

    rc = main(["think", "run", "--log-level", "WARNING", "--max-turns", "1"])
    assert rc == 0
    logger = logging.getLogger(_LOGGER_NAME)
    assert logger.level == logging.WARNING
    assert any(h.stream is sys.stderr for h in logger.handlers)


class _SilentSleepSession:
    """No DoA, no audio — the sleep loop senses 'nothing' every tick."""

    samplerate = 48000

    def __enter__(self) -> "_SilentSleepSession":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def doa(self, *, timeout: float | None = None) -> object:
        return None

    def get_audio_sample(self) -> object:
        return None


class _FakeSleepTransport:
    name = "sdk"

    def __init__(self) -> None:
        self.gotos: list[dict] = []

    def media_session(self) -> _SilentSleepSession:
        return _SilentSleepSession()

    def move_goto(self, **kwargs: object) -> None:
        self.gotos.append(kwargs)


def test_sleep_run_installs_logging_at_the_resolved_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    import reachy.cli._commands.sleep as sleep_mod

    fake = _FakeSleepTransport()
    monkeypatch.setattr(sleep_mod, "get_transport", lambda args: fake)
    monkeypatch.setattr(sleep_mod.time, "sleep", lambda *_a, **_k: None)

    from reachy.cli import main

    rc = main(["sleep", "run", "--transport", "sdk", "--log-level", "ERROR", "--ticks", "1"])
    assert rc == 0
    logger = logging.getLogger(_LOGGER_NAME)
    assert logger.level == logging.ERROR
    assert any(h.stream is sys.stderr for h in logger.handlers)
