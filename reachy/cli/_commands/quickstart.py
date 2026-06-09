"""``reachy-mini-cli quickstart`` — install and start "real mode" in a few steps.

A read-only verb (like ``learn``): it prints the copy-paste sequence to install
the CLI and bring a live Reachy Mini up, the HTTP-remote profile, and the
agent-first commands that work with no robot attached. No robot interaction and
no imports beyond :mod:`reachy.cli._output`, so it works on any install profile.
"""

from __future__ import annotations

import argparse

from reachy.cli._output import emit_result

_TEXT = """\
reachy-mini-cli quickstart — install and start real mode

Real mode — local robot (recommended)
-------------------------------------
1. Install once (CLI + daemon binary + SDK):
     uv tool install 'reachy-mini-cli[daemon]'
     # or: pip install 'reachy-mini-cli[daemon]'
     # the installed command is 'reachy-mini-cli' (short alias: 'reachy')

2. Start the daemon (wakes the robot on start):
     reachy-mini-cli daemon start

3. Verify it answers:
     reachy-mini-cli device status

4. Make it do something:
     reachy-mini-cli listen run                  # orient to sound (Ctrl-C to stop)
     reachy-mini-cli demo-mode start             # feel-alive idle loop (background)
     reachy-mini-cli move goto --z 10 --pitch -5 # one motion command

5. Put it back down when you are done:
     reachy-mini-cli daemon stop

Remote / HTTP-only — no local robot
-----------------------------------
Install numpy-only (no daemon binary), talk to a daemon running elsewhere:
     uv tool install reachy-mini-cli
     export REACHY_BASE_URL=http://reachy.local:8000
     reachy-mini-cli device status
     reachy-mini-cli listen run --transport http

Always available (no daemon needed)
-----------------------------------
     reachy-mini-cli learn               # the full command map
     reachy-mini-cli explain daemon      # full docs for the daemon noun
     reachy-mini-cli explain listen      # full docs for listen
     reachy-mini-cli quickstart --json   # this guide as JSON

Exit codes: 0 success, 1 user error, 2 environment/setup error.
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "reachy-mini-cli",
        "profiles": [
            {
                "name": "real-mode",
                "summary": "Local robot with the daemon binary (recommended).",
                "install": "uv tool install 'reachy-mini-cli[daemon]'",
                "steps": [
                    "reachy-mini-cli daemon start",
                    "reachy-mini-cli device status",
                    "reachy-mini-cli listen run",
                    "reachy-mini-cli daemon stop",
                ],
            },
            {
                "name": "http-remote",
                "summary": "numpy-only; talk to a daemon running elsewhere.",
                "install": "uv tool install reachy-mini-cli",
                "steps": [
                    "export REACHY_BASE_URL=http://reachy.local:8000",
                    "reachy-mini-cli device status",
                    "reachy-mini-cli listen run --transport http",
                ],
            },
        ],
        "install": {
            "real_mode": "uv tool install 'reachy-mini-cli[daemon]'",
            "pip": "pip install 'reachy-mini-cli[daemon]'",
            "http_remote": "uv tool install reachy-mini-cli",
        },
        "agent_first": [
            "reachy-mini-cli learn",
            "reachy-mini-cli explain daemon",
            "reachy-mini-cli explain listen",
            "reachy-mini-cli quickstart --json",
        ],
    }


def cmd_quickstart(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "quickstart",
        help="Print the install + start-real-mode sequence (copy-paste).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_quickstart)
