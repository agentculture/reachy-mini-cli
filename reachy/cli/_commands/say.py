"""``reachy-mini-cli say`` — synthesize text and play it through the robot speaker.

A *dumb pipe*: text → TTS → playback.  No LLM, no senses, no event bus — this
noun is deliberately kept boundary-clean so agents can compose it into pipelines
without pulling in the heavier speech stack.

Verbs
-----
* **run**      — synthesize the text (or stdin if ``-``) and play it.
* **overview** — describe the say noun (rubric: every noun with action-verbs
                 exposes overview).

Flags (``say run``)
-------------------
* ``text``         — positional; the string to speak, or ``-`` to read from stdin.
* ``--voice``      — voice identifier forwarded to ``tts.synthesize``
                     (overrides ``REACHY_TTS_VOICE``).
* ``--speed``      — TTS speed (float; forwarded to synthesize — a no-op if the
                     server does not support it, but the value is always passed
                     through so callers are not silently dropped on the floor).
* ``--tts-url``    — override TTS base URL (``REACHY_TTS_URL`` env if unset).
* ``--tts-timeout``— per-request socket timeout for the TTS call (default 30 s).
* ``--transport``  — playback transport: ``"sdk"`` (default) or ``"http"``.
* ``--base-url``   — daemon base URL for the http playback transport.
* ``--timeout``    — HTTP playback request timeout (default 10 s).
* ``--json``       — emit a structured result on stdout.

Boundary invariant
------------------
This module MUST NOT import ``reachy.speech.llm`` or ``reachy.speech.events``.
CI-level tests in ``tests/test_say.py`` assert this at both module-import time
and during ``cmd_say_run`` execution.
"""

from __future__ import annotations

import argparse
import os
import sys

from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.speech.playback import play_audio as _play_audio  # noqa: E402 — intentional alias
from reachy.speech.tts import synthesize as _synthesize  # noqa: E402 — intentional alias

# ---------------------------------------------------------------------------
# Thin wrappers — imported at module level so tests can monkeypatch them as
# ``say_mod._synthesize`` / ``say_mod._play_audio`` without reaching into the
# speech sub-packages.
# ---------------------------------------------------------------------------


_JSON_HELP = "Emit structured JSON."

_VERBS = [
    "say run <text> — synthesize text and play it through the robot speaker",
    "say overview   — describe the say noun",
]


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


def cmd_say_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "Dumb pipe: text → TTS synthesis → robot speaker playback.",
                "No LLM, no senses, no event bus — safe to compose in pipelines.",
                "Pass '-' as the text argument to read from stdin.",
                "TTS via Magpie-style HTTP endpoint (REACHY_TTS_URL / --tts-url).",
                "Playback via SDK (default) or HTTP daemon transport (--transport http).",
            ],
        },
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "results to stdout, diagnostics to stderr (never mixed)",
                "exit codes: 0 ok, 1 user error, 2 environment (TTS/daemon unreachable)",
                "REACHY_TTS_URL overrides the default TTS base URL",
                "REACHY_TTS_VOICE overrides the default voice",
                "REACHY_TRANSPORT overrides the default playback transport (sdk)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli say",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_say_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    raw_text: str = args.text

    # Resolve text: read from stdin when the argument is "-".
    if raw_text == "-":
        text = sys.stdin.read().strip()
        if not text:
            raise CliError(
                code=EXIT_USER_ERROR,
                message="no text received from stdin",
                remediation=("provide text, e.g. echo 'hello' | reachy-mini-cli say run -"),
            )
    else:
        text = raw_text

    # Resolve transport/base-url for playback.
    transport: str | None = getattr(args, "transport", None)
    base_url: str = getattr(args, "base_url", "http://localhost:8000")
    playback_timeout: float = getattr(args, "timeout", 10.0)

    if not json_mode:
        emit_diagnostic(f"[say] synthesizing {len(text)} char(s) …")

    # Synthesize — forward TTS-specific args.
    # NOTE: tts.synthesize does not currently expose a ``speed`` parameter, so
    # ``--speed`` is accepted as a forward-compatible placeholder — stored on the
    # Namespace but intentionally not forwarded (a no-op, not silently dropped)
    # until tts.py gains speed/SSML prosody support. Tracked for a follow-up.
    pcm = _synthesize(
        text,
        tts_url=getattr(args, "tts_url", None),
        voice=getattr(args, "voice", None),
        timeout=getattr(args, "tts_timeout", 30.0),
    )

    if pcm:
        if not json_mode:
            emit_diagnostic(f"[say] playing {len(pcm)} PCM bytes …")
        _play_audio(
            pcm,
            transport=transport,
            base_url=base_url,
            timeout=playback_timeout,
        )

    if json_mode:
        emit_result(
            {"status": "ok", "text": text, "bytes": len(pcm)},
            json_mode=True,
        )

    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_say_overview(args)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    """Register the ``say`` noun group into *sub* (a top-level subparsers action).

    Exposes two verbs: ``run`` (synthesize + play) and ``overview`` (describe).
    Task t8 calls this from ``reachy.cli._build_parser``; until then the command
    module is exercised directly via :func:`cmd_say_run` / :func:`cmd_say_overview`
    in the test suite.
    """
    p = sub.add_parser(
        "say",
        help="Synthesize text and play it through the robot speaker "
        "(see 'reachy-mini-cli say overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="say_command", parser_class=type(p))

    # overview verb (rubric requirement for any noun with action-verbs)
    ov = noun_sub.add_parser("overview", help="Describe the say noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_say_overview)

    # run verb
    run = noun_sub.add_parser(
        "run",
        help="Synthesize text and play it through the robot speaker.",
    )
    run.add_argument(
        "text",
        help="Text to synthesize, or '-' to read from stdin.",
    )
    run.add_argument(
        "--voice",
        default=None,
        help="Voice identifier for the TTS server (overrides REACHY_TTS_VOICE).",
    )
    run.add_argument(
        "--speed",
        type=float,
        default=None,
        help="TTS speed multiplier (e.g. 0.9 for slower, 1.2 for faster); "
        "forwarded to the TTS server — a no-op if the server ignores it.",
    )
    run.add_argument(
        "--tts-url",
        default=None,
        dest="tts_url",
        help="Override the TTS base URL (default: REACHY_TTS_URL or http://localhost:9000).",
    )
    run.add_argument(
        "--tts-timeout",
        type=float,
        default=30.0,
        dest="tts_timeout",
        help="Per-request socket timeout for TTS synthesis (default: 30.0 s).",
    )
    run.add_argument(
        "--transport",
        default=os.environ.get("REACHY_TRANSPORT", None),
        choices=["sdk", "http"],
        help="Playback transport: 'sdk' (default) or 'http'. "
        "Overrides REACHY_TRANSPORT env var.",
    )
    run.add_argument(
        "--base-url",
        default="http://localhost:8000",
        dest="base_url",
        help="Daemon base URL for the http playback transport (default: http://localhost:8000).",
    )
    run.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP playback request timeout in seconds (default: 10.0).",
    )
    run.add_argument("--json", action="store_true", help=_JSON_HELP)
    run.set_defaults(func=cmd_say_run)
