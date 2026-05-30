"""``reachy-mini-cli learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from reachy import __version__
from reachy.cli._output import emit_result

_TEXT = """\
reachy-mini-cli — a clonable template for AgentCulture mesh agents.

Purpose
-------
Scaffold for a new Culture mesh agent: an agent-first CLI (cited from the teken
`python-cli` reference), an identity (culture.yaml + CLAUDE.md), the canonical
guildmaster skill kit under .claude/skills/, and a deploy/CI baseline. Clone it,
rename the package, and edit culture.yaml to mint a new agent.

Commands
--------
  reachy-mini-cli whoami             Identity from culture.yaml.
  reachy-mini-cli learn              This self-teaching prompt.
  reachy-mini-cli explain <path>...  Markdown docs for any noun/verb path.
  reachy-mini-cli overview           Descriptive snapshot of the agent.
  reachy-mini-cli doctor             Check the agent-identity invariants.
  reachy-mini-cli cli overview       Describe the CLI surface itself.

Robot commands (talk to the Reachy daemon; --transport http|sdk)
  reachy-mini-cli daemon start       Start the local daemon; also: stop/status.
  reachy-mini-cli device status      Daemon status / device info.
  reachy-mini-cli device state       Live robot state (pose, antennas).
  reachy-mini-cli app list           Available apps; also: status/start/stop.
  reachy-mini-cli move goto ...      Move head/antennas; also: wake/sleep.
  reachy-mini-cli demo-mode start    Make the robot feel alive (continuous).
                                     Also: stop/restart/status/run, config,
                                     install/enable/disable (systemd --user).

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error
  3+ reserved

More detail
-----------
  reachy-mini-cli explain reachy-mini-cli
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "reachy-mini-cli",
        "version": __version__,
        "purpose": "Clonable scaffold for a new AgentCulture mesh agent.",
        "commands": [
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
            {"path": ["daemon", "start"], "summary": "Start the local reachy-mini-daemon."},
            {"path": ["daemon", "stop"], "summary": "Stop the daemon this CLI started."},
            {"path": ["daemon", "status"], "summary": "Daemon process + HTTP-health state."},
            {"path": ["device", "status"], "summary": "Daemon status / device info."},
            {"path": ["device", "state"], "summary": "Live robot state."},
            {"path": ["app", "list"], "summary": "List/start/stop Reachy Mini apps."},
            {"path": ["move", "goto"], "summary": "Move head/antennas; wake/sleep."},
            {
                "path": ["demo-mode", "start"],
                "summary": "Start a background loop that makes the robot feel alive.",
            },
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
        },
        "json_support": True,
        "explain_pointer": "reachy-mini-cli explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
