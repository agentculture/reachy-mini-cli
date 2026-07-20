"""``reachy-mini-cli behavior`` — compose robot behaviors on a 50 Hz loop.

A persistent engine runs a 50 Hz loop and holds a set of active behaviors; you
push one-shot or looping behaviors onto it from separate invocations, and a
per-channel contention model (``passive`` / ``stoppable`` / ``unstoppable`` /
``stopping``) decides who drives ``head`` / ``antennas`` / ``body_yaw`` when they
conflict. ``feel-alive`` runs as a passive base layer so the robot stays alive on
any channel nothing else claims.

Most built-in behaviors are pure motion. Stateful presence and the sensor-driven
``pet-reaction`` are freshly instantiated per admission. For sound-orienting,
use the dedicated ``reachy listen`` loop, which drives the daemon's smooth
minjerk ``goto`` planner instead of streaming large immediate turns here.

* ``behavior list`` — the built-in behavior catalog (no robot needed).
* ``behavior run`` / ``stop`` / ``status`` — drive the running engine (auto-starts
  it) through the command spool. ``status`` additively reports rules-file health
  (path + counts) and, once the engine has published one, the live agent-intents
  view (goal/inhibitions/mode) from ``state.json``.
* ``behavior reload`` — reload ``rules.toml`` in the running engine, applied
  between ticks (see ``reachy.behavior.reload_driver``).
* ``behavior rules`` / ``rules check`` — render / lint ``rules.toml`` without
  touching a running engine (pure file read via ``reachy.behavior.rules``).
* ``behavior engine start|stop|status|run`` — manage the 50 Hz engine process.

The engine streams immediate ``set_target`` poses, so it owns motion exclusively
while running — don't drive the robot with ``move goto`` / ``demo-mode`` /
``reachy listen`` at the same time.

``behavior engine run`` also loads ``rules.toml`` (see ``reachy.behavior.rules``)
once at boot: a MISSING file is fine (no rules configured yet); a PRESENT but
malformed file is rejected without crashing the process — the engine falls back
to bare base presence (``feel-alive`` only) and logs the rejection (naming every
reason) via ``reachy.senselog``. ``behavior reload`` then lets an operator push a
corrected file into the already-running engine without a restart.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Callable

from reachy import senselog
from reachy.behavior import control, library, liveness, reload_driver
from reachy.behavior import rules as rules_mod
from reachy.behavior import supervisor
from reachy.behavior.engine import EngineConfig
from reachy.behavior.engine import run as engine_run
from reachy.behavior.excited_motion_probe import CUES as PROBE_CUES
from reachy.behavior.excited_motion_probe import MODES as PROBE_MODES
from reachy.behavior.excited_motion_probe import (
    ProbeCommandGuard,
    ProbeDriver,
    ProbeNamespaceGuard,
    SharedPoseReader,
)
from reachy.behavior.goto_intent import GOTO, make_goto_handler
from reachy.behavior.goto_lane import GotoLane
from reachy.behavior.intents import INTENT_NAMESPACE, IntentDriver
from reachy.behavior.model import CHANNELS, StopClass
from reachy.behavior.pat_sense import (
    DEFAULT_HP_TAU,
    DEFAULT_PRESS_THRESHOLD,
    DEFAULT_RELEASE_THRESHOLD,
    DEFAULT_STILL_EPS,
    DEFAULT_STILL_HOLD_S,
    RELEASE_AFTER_S,
    PatSenseDriver,
)
from reachy.behavior.pose_feed import LastPoseHolder
from reachy.behavior.rule_engine import STAGE as RULE_STAGE
from reachy.behavior.rule_engine import TickBus
from reachy.behavior.rules import RulesLoader
from reachy.behavior.sense import DoaPoller, SenseProviders, read_doa, read_perception
from reachy.behavior.tick_metrics import TickMetrics, budget_from_hz
from reachy.cli._commands._robot import add_robot_args, emit_payload, get_transport, noun_overview
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from reachy.cli._export import add_runtime_export_args, build_runtime_export_consumer
from reachy.cli._logging import add_log_level_arg, install_logging
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.export.runtime import SenseSnapshotDriver
from reachy.motion.pat import PatDetector
from reachy.robot import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, INTERPOLATIONS
from reachy.robot.state_reader import HeldStateReader
from reachy.speech.distinctness import find_too_similar as _find_too_similar
from reachy.speech.expressions import NEUTRAL_KEY, Catalog

_JSON_HELP = "Emit structured JSON."
_EMPTY_SUBMITTED = "(submitted)"
_AWAIT_TIMEOUT_HELP = "Seconds to wait for the engine to confirm (default: 1.0)."
_CLASSES = tuple(c.value for c in StopClass)
#: The heartbeat freshness/skew windows now live in
#: :mod:`reachy.behavior.liveness`, shared with the foreground ``pat run`` /
#: ``sleep run`` refusal, so both surfaces agree on when an engine is live.

#: The six head axes a goto may target, in the order ``goto_intent.HEAD_AXES`` /
#: ``move goto``'s own ``_HEAD_KEYS`` use — flag names match the GotoSpec payload's
#: head-axis keys exactly, so ``behavior goto``'s payload needs no translation.
_HEAD_KEYS = ("x", "y", "z", "roll", "pitch", "yaw")

_VERBS = [
    "behavior list — the built-in behavior catalog (names, channels, class, params)",
    "behavior run <name> — push a behavior onto the running engine (auto-starts it)",
    "behavior stop <id|name|all> — stop a running behavior (all = keep the idle base)",
    "behavior status — active behaviors + per-channel ownership + engine/daemon state "
    "+ rules health + agent intents (when published)",
    "behavior reload — reload rules.toml in the running engine, applied between ticks",
    "behavior goto — submit a goto (head/antennas/body-yaw) through the intents "
    "spool, the same path a live agent uses",
    "behavior rules — render the loaded rules.toml (react/inhibit rules, modes)",
    "behavior rules check — validate rules.toml (a linter; exit 0 unless unreadable)",
    "behavior expressions — list the expression pose catalog (and 'expressions check')",
    "behavior engine start — start the 50 Hz engine in the background",
    "behavior engine stop — stop the engine (eases the robot to neutral)",
    "behavior engine status — engine process + daemon reachability",
    "behavior engine run — run the engine in the foreground (what start launches)",
    "behavior overview — this summary",
]


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _parse_set(pairs: list[str] | None) -> dict[str, str]:
    """Parse ``key=value`` tokens into a dict, rejecting malformed ones."""
    out: dict[str, str] = {}
    for token in pairs or []:
        if "=" not in token:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"bad --set token {token!r} (expected key=value)",
                remediation="e.g. --set amp=20 period=0.5",
            )
        key, raw = token.split("=", 1)
        out[key.strip()] = raw.strip()
    return out


def _resolve_channels(names: list[str] | None) -> list[str] | None:
    if not names:
        return None
    bad = [n for n in names if n not in CHANNELS]
    if bad:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown channel(s): {', '.join(bad)}",
            remediation=f"valid channels: {', '.join(CHANNELS)}",
        )
    return list(names)


def _engine_config(args: argparse.Namespace) -> EngineConfig:
    return EngineConfig(
        compose_hz=args.compose_hz,
        base_layer=not args.no_base_layer,
        energy=args.energy,
        settle=not args.no_settle,
    )


# --------------------------------------------------------------------------- #
# overview / list                                                             #
# --------------------------------------------------------------------------- #


def cmd_overview(args: argparse.Namespace) -> int:
    noun_overview(
        "reachy-mini-cli behavior",
        _VERBS,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    entries = []
    for entry in library.LIBRARY.values():
        entries.append(
            {
                "name": entry.name,
                "summary": entry.summary,
                "channels": sorted(entry.channels),
                "default_class": entry.default_class.value,
                "kind": "looping" if entry.looping else "one-shot",
                "default_duration": entry.default_duration,
                "params": {
                    k: {"default": p.default, "unit": p.unit, "help": p.help}
                    for k, p in entry.params.items()
                },
            }
        )
    if json_mode:
        emit_result({"behaviors": entries}, json_mode=True)
    else:
        lines: list[str] = ["# behaviors", ""]
        for e in entries:
            dur = (
                "until stopped" if e["default_duration"] is None else f"{e['default_duration']:g}s"
            )
            lines.append(
                f"- {e['name']} [{e['kind']}, {e['default_class']}, {dur}] — {e['summary']}"
            )
            lines.append(f"    channels: {', '.join(e['channels'])}")
            if e["params"]:
                params = ", ".join(
                    f"{k}={p['default']:g}{p['unit']}" for k, p in e["params"].items()
                )
                lines.append(f"    params: {params}")
        emit_result("\n".join(lines), json_mode=False)
    return 0


# --------------------------------------------------------------------------- #
# run / stop / status (talk to the running engine via the spool)              #
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    entry = library.get(args.name)
    params = library.resolve_params(entry, _parse_set(args.set))
    stop_class = library.resolve_class(entry, args.behavior_class)
    lifetime = library.resolve_lifetime(
        entry, once=args.once, loop=args.loop, duration=args.duration
    )
    channels = _resolve_channels(args.channels)

    if not args.no_ensure_engine:
        supervisor.ensure_running(
            transport=args.transport,
            base_url=args.base_url,
            timeout=args.timeout,
            compose_hz=args.compose_hz,
            energy=args.energy,
            base_layer=not args.no_base_layer,
            settle=not args.no_settle,
        )

    cmd_id = control.submit(
        "add",
        name=args.name,
        params=params,
        lifetime={"looping": lifetime.looping, "duration": lifetime.duration},
        channels=channels,
        **{"class": stop_class.value},
    )
    result = control.await_result(cmd_id, timeout=args.await_timeout)
    if result is None:
        result = {
            "ok": False,
            "submitted": cmd_id,
            "note": _UNCONFIRMED_NOTE,
        }
    emit_payload(result, json_mode=json_mode, empty=_EMPTY_SUBMITTED)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    cmd_id = control.submit("stop", target=args.target)
    result = control.await_result(cmd_id, timeout=args.await_timeout)
    if result is None:
        result = {"ok": False, "submitted": cmd_id, "note": "engine did not confirm in time"}
    emit_payload(result, json_mode=json_mode, empty=_EMPTY_SUBMITTED)
    return 0


def _rules_status() -> dict[str, object]:
    """Rules-file health for ``behavior status`` — never raises.

    Uses :class:`~reachy.behavior.rules.RulesLoader`, whose ``reload()`` already
    degrades a missing/malformed file to "keep the last-good config, record why"
    rather than raising (see ``reachy.behavior.rules``) — so this can never take
    ``behavior status`` down, even with a broken ``rules.toml`` on disk. The
    outer ``try`` is one more defensive layer against anything genuinely
    unexpected (e.g. the state dir itself being unwritable).
    """
    try:
        loader = RulesLoader()
        loader.reload()
    except Exception as err:  # noqa: BLE001 - status must never crash on rules
        return {"path": None, "exists": False, "ok": False, "error": str(err)}
    info: dict[str, object] = {
        "path": str(loader.path),
        "exists": loader.path.is_file(),
        "ok": loader.last_error is None,
        "react": len(loader.current.react),
        "inhibit": len(loader.current.inhibit),
        "modes": len(loader.current.modes),
    }
    if loader.last_error is not None:
        info["error"] = loader.last_error
    return info


def cmd_status(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    engine_state = supervisor.status(base_url=args.base_url, timeout=args.timeout)
    data: dict[str, object] = {"engine": engine_state}
    published = control.read_state()
    if published is None:
        data["active"] = []
        data["ownership"] = dict.fromkeys(CHANNELS)
        data["note"] = "engine has not published state (not running, or just started)"
    else:
        data["active"] = published.get("active", [])
        data["ownership"] = published.get("ownership", {})
        data["compose_hz"] = published.get("compose_hz")
        if "doa" in published:
            data["doa"] = published["doa"]
        if "intents" in published:
            data["intents"] = published["intents"]
    data["rules"] = _rules_status()
    emit_payload(data, json_mode=json_mode)
    return 0


def cmd_reload(args: argparse.Namespace) -> int:
    """Ask the running engine to reload ``rules.toml`` between ticks.

    A rejected reload is NOT a ``CliError`` — the engine keeps the last-good
    rules config and simply reports why the candidate was refused (mirroring
    ``cmd_run``/``cmd_stop``'s "engine did not confirm in time" idiom below): a
    typo in the rules file is an operator fact to report, never a reason to
    exit non-zero or take the running presence down.
    """
    json_mode = bool(getattr(args, "json", False))
    cmd_id = reload_driver.submit_reload()
    result = reload_driver.await_result(cmd_id, timeout=args.await_timeout)
    if result is None:
        result = {
            "ok": False,
            "submitted": cmd_id,
            "note": _UNCONFIRMED_NOTE,
        }
    emit_payload(result, json_mode=json_mode, empty=_EMPTY_SUBMITTED)
    return 0


# --------------------------------------------------------------------------- #
# goto — submit a goto through the SAME intents spool an agent tool uses      #
# --------------------------------------------------------------------------- #


def _goto_payload(args: argparse.Namespace) -> dict[str, object]:
    """Build the GOTO command's fields from only the channel flags the operator gave.

    Mirrors ``move goto``'s flag-to-payload shape (``reachy/cli/_commands/move.py``)
    but stops at the payload dict — this verb submits into the spool instead of
    calling a transport directly. The "at least one channel" check is done HERE,
    synchronously, so a bare ``behavior goto`` (no channel flags) fails fast with a
    clean exit-1 even when no engine is running to hand back the kind's own
    (otherwise equivalent) rejection — see ``goto_intent.make_goto_handler``, whose
    handler performs the exact same check on the payload once it reaches the spool
    (a malicious/buggy non-CLI spool writer still hits that backstop).
    """
    head: dict[str, float] | None = None
    if any(getattr(args, key) is not None for key in _HEAD_KEYS):
        head = {key: getattr(args, key) for key in _HEAD_KEYS if getattr(args, key) is not None}
    antennas = tuple(args.antennas) if args.antennas is not None else None
    body_yaw = args.body_yaw
    if head is None and antennas is None and body_yaw is None:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="goto: a goto must target at least one channel (head axis, antennas, "
            "or body-yaw)",
            remediation="pass at least one of --x/--y/--z/--roll/--pitch/--yaw/"
            "--antennas/--body-yaw",
        )
    payload: dict[str, object] = {"duration": args.duration, "interpolation": args.interpolation}
    if head is not None:
        payload["head"] = head
    if antennas is not None:
        payload["antennas"] = list(antennas)
    if body_yaw is not None:
        payload["body_yaw"] = body_yaw
    if args.label is not None:
        payload["label"] = args.label
    return payload


def cmd_goto(args: argparse.Namespace) -> int:
    """Submit a GOTO command into the intents spool — exactly what a live agent's
    ``run_behavior``/... tools do (``reachy.speech.intent_tools._submit_and_await``),
    so this verb exercises the identical submission path.

    The submit is async: the engine applies it on its next drain, not this call. If
    an engine confirms in time, its outcome is reported verbatim — including a
    LIVE rejection from ``goto_intent``'s own validation (e.g. an out-of-range
    axis), which is surfaced here as a ``CliError`` (exit 1) rather than a
    silently-`ok:false` JSON blob, so a confirmed-bad goto reads the same as any
    other CLI validation error. If nothing confirms in time this degrades to a
    ``submitted``/unconfirmed report (exit 0) — the command is still on disk, so a
    later-started engine still applies it; this verb never pretends to know an
    outcome the spool hasn't reported.
    """
    json_mode = bool(getattr(args, "json", False))
    payload = _goto_payload(args)
    cmd_id = control.submit(GOTO, namespace=INTENT_NAMESPACE, **payload)
    result = control.await_result(cmd_id, namespace=INTENT_NAMESPACE, timeout=args.await_timeout)
    if result is None:
        result = {
            "ok": None,
            "submitted": cmd_id,
            "note": _UNCONFIRMED_NOTE,
        }
    elif result.get("ok") is False:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=str(result.get("error") or "goto: rejected by the engine"),
            remediation="adjust the goto payload and resubmit",
        )
    emit_payload(result, json_mode=json_mode, empty=_EMPTY_SUBMITTED)
    return 0


# --------------------------------------------------------------------------- #
# rules sub-noun — render / lint rules.toml (no running engine needed)        #
# --------------------------------------------------------------------------- #

_RULES_VERBS = [
    "rules / rules list — render the loaded rules.toml (react/inhibit rules, modes)",
    "rules check — validate rules.toml; always exits 0 unless the file is unreadable",
    "rules overview — this summary",
]


def _predicate_payload(pred: rules_mod.Predicate) -> dict[str, object]:
    return {"field": pred.field, "op": pred.op, "value": pred.value}


def _rule_payload(rule: rules_mod.Rule) -> dict[str, object]:
    data: dict[str, object] = {
        "id": rule.id,
        "when": _predicate_payload(rule.when),
        "cooldown_s": rule.cooldown_s,
        "hysteresis": rule.hysteresis,
    }
    if rule.kind == rules_mod.KIND_REACT:
        data["run"] = rule.behavior
        data["params"] = dict(rule.params)
    else:
        data["disable"] = sorted(rule.disable)
    return data


def _rules_config_payload(config: rules_mod.RulesConfig, *, path: Path, exists: bool) -> dict:
    payload: dict[str, object] = {
        "path": str(path),
        "exists": exists,
        "active_mode": config.active_mode,
        "react": [_rule_payload(r) for r in config.react],
        "inhibit": [_rule_payload(r) for r in config.inhibit],
        "modes": {name: dict(mode.params) for name, mode in config.modes.items()},
    }
    if not exists:
        payload["note"] = "no rules file yet — nothing configured"
    return payload


def cmd_rules_list(args: argparse.Namespace) -> int:
    """Render the loaded ``rules.toml`` — react/inhibit rules, modes, active_mode.

    A MISSING file is not an error ("no rules configured yet"): mirrors
    ``reachy.behavior.rules.load_rules``. A PRESENT but malformed file raises
    the very same ``CliError`` ``load_rules`` already raises — a clean exit-1
    naming every reason — because this verb is a straight read, not a lint (see
    ``rules check`` for the always-exit-0 linter).
    """
    json_mode = bool(getattr(args, "json", False))
    path = rules_mod.default_rules_path()
    exists = path.is_file()
    config = rules_mod.load_rules(path)  # raises CliError (exit 1) on a malformed file
    payload = _rules_config_payload(config, path=path, exists=exists)
    emit_payload(payload, json_mode=json_mode)
    return 0


def _rules_check_payload(
    path: Path, *, reader: Callable[[Path], str] | None = None
) -> dict[str, object]:
    """Validate *path*; returns ``{ok, path, exists, reasons, counts}`` — a report.

    Mirrors ``think expressions check``'s exit-0-warnings idiom: a malformed or
    missing rules file is a CONTENT problem, not an I/O failure — missing
    resolves ``ok=True`` ("nothing configured yet"), malformed resolves
    ``ok=False`` with ``reasons`` naming every problem
    (``reachy.behavior.rules.load_rules`` already collects them into one
    message) — never an exception. Only a genuine read failure on an EXISTING
    path (permissions, a vanished mount, ...) raises ``CliError(EXIT_ENV_ERROR)``
    — this is a linter, not a gate, so content issues never abort the command,
    but the file being physically unreadable is an environment fact, not a
    content one.

    ``reader`` is an injection seam for tests (default: ``Path.read_text``) so
    an I/O failure can be simulated deterministically with no OS-level
    permission juggling.
    """
    read = reader if reader is not None else (lambda p: p.read_text(encoding="utf-8"))
    exists = path.is_file()
    if exists:
        try:
            read(path)
        except OSError as err:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"rules file {path} could not be read: {err}",
                remediation="check file permissions",
            ) from err
    try:
        config = rules_mod.load_rules(path)
    except CliError as err:
        return {"ok": False, "path": str(path), "exists": exists, "reasons": [err.message]}
    return {
        "ok": True,
        "path": str(path),
        "exists": exists,
        "reasons": [],
        "counts": {
            "react": len(config.react),
            "inhibit": len(config.inhibit),
            "modes": len(config.modes),
        },
    }


def cmd_rules_check(args: argparse.Namespace) -> int:
    """Lint ``rules.toml``: a malformed file reports ``ok=False`` but still exits 0
    (a warning, not a gate — mirrors ``think expressions check``). Only an actual
    I/O failure on an existing path is a clean exit-2 (via ``CliError``, handled
    by ``_dispatch``).
    """
    json_mode = bool(getattr(args, "json", False))
    payload = _rules_check_payload(rules_mod.default_rules_path())
    emit_payload(payload, json_mode=json_mode)
    return 0


def cmd_rules_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "reachy-mini-cli behavior rules",
        [
            {
                "title": "What",
                "items": [
                    "The declarative rules.toml file the engine's rule seam evaluates "
                    "(react/inhibit rules + modes) — see 'behavior reload' to hot-swap "
                    "it into a running engine.",
                    "These verbs read the file directly — no running engine needed.",
                ],
            },
            {"title": "Verbs", "items": list(_RULES_VERBS)},
            {
                "title": "Conventions",
                "items": [
                    "every command supports --json",
                    "a missing rules file is not an error — 'no rules configured yet'",
                    "'rules check' is a linter: a malformed file reports ok=false but "
                    "still exits 0; only an unreadable path is a clean exit-2",
                ],
            },
        ],
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _rules_no_verb(args: argparse.Namespace) -> int:
    # Bare `behavior rules` renders the loaded config (mirrors `think expressions`).
    return cmd_rules_list(args)


# --------------------------------------------------------------------------- #
# expressions sub-noun — the pose catalog + distinctness check (t18)          #
# --------------------------------------------------------------------------- #
#
# Ported from `think expressions` (`reachy/cli/_commands/think.py`), which is
# being retired along with the rest of the LLM-cognition `think` noun. The
# catalog itself (`reachy.speech.expressions`, backed by `expressions.toml`)
# and the distinctness check (`reachy.speech.distinctness`) are NOT
# LLM-coupled — a TOML table and a geometric distance function — and stay
# needed afterward: `reachy.speech.tools`'s `apply_pose` tool (kept by `agent
# attach`) imports the catalog directly. So the data survives but its only CLI
# inspection surface would otherwise vanish with `think`; `behavior` is the
# new home — the surviving presence noun, and already hosts a sibling
# sub-noun (`rules`, just above) in exactly this "render + lint a file, no
# running engine needed" shape. `think expressions` itself is untouched here
# (a separate task deletes `think` wholesale) — the duplication between the
# two homes is deliberate and temporary.

_EXPRESSIONS_VERBS = [
    "expressions / expressions list — list the expression catalog (emoji + pose descriptor)",
    "expressions check — flag catalog poses too similar to be distinct",
    "expressions overview — this summary",
]


def _expression_emojis(catalog: Catalog | None = None) -> list[str]:
    """The catalog's expression emojis (every key except the neutral fallback)."""
    cat = catalog if catalog is not None else Catalog()
    return [key for key in cat.keys() if key != NEUTRAL_KEY]


def _pose_descriptor(catalog: Catalog, emoji: str) -> str:
    """A short, generated descriptor of an emoji's pose (its non-zero axes).

    The catalog is pose values only (the TOML's prose lives in comments, which
    ``tomllib`` drops), so we summarise the pose itself — the non-zero axes and
    their signed magnitudes — giving an agent a machine-stable, catalog-derived
    descriptor without duplicating the TOML comments in code.
    """
    pose = catalog.get(emoji)
    axes = [
        ("head_x", pose.head_x),
        ("head_y", pose.head_y),
        ("head_z", pose.head_z),
        ("head_roll", pose.head_roll),
        ("head_pitch", pose.head_pitch),
        ("head_yaw", pose.head_yaw),
        ("antenna_right", pose.antenna_right),
        ("antenna_left", pose.antenna_left),
        ("body_yaw", pose.body_yaw),
    ]
    moved = [f"{name}{value:+g}" for name, value in axes if value]
    return ", ".join(moved) if moved else "neutral (no offset)"


def cmd_expressions_list(args: argparse.Namespace) -> int:
    """List the expression catalog: each emoji + a short pose descriptor."""
    catalog = Catalog()
    rows = [
        {"emoji": emoji, "descriptor": _pose_descriptor(catalog, emoji)}
        for emoji in _expression_emojis(catalog)
    ]
    if bool(getattr(args, "json", False)):
        emit_result({"expressions": rows}, json_mode=True)
    else:
        lines = [f"{row['emoji']}  {row['descriptor']}" for row in rows]
        emit_result("\n".join(lines), json_mode=False)
    return 0


def cmd_expressions_check(args: argparse.Namespace) -> int:
    """Run the distinctness check; report flagged pairs (clean check exits 0).

    A flagged pair is a *warning*, not an error — the catalog still works — so
    the exit code stays 0; the ``--json`` ``ok`` field is the machine-readable
    signal (mirrors ``behavior rules check``'s exit-0-warnings idiom).
    """
    catalog = Catalog()
    flagged = _find_too_similar(catalog)
    ok = not flagged
    if bool(getattr(args, "json", False)):
        emit_result(
            {"ok": ok, "flagged": [[a, b, score] for a, b, score in flagged]},
            json_mode=True,
        )
    else:
        if ok:
            emit_result("clean — all expressions are sufficiently distinct", json_mode=False)
        else:
            lines = [f"{a} ~ {b} (distance {score:.3f})" for a, b, score in flagged]
            emit_result(
                "too similar (" + str(len(flagged)) + " pair(s)):\n" + "\n".join(lines),
                json_mode=False,
            )
    return 0


def cmd_expressions_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "reachy-mini-cli behavior expressions",
        [
            {
                "title": "What",
                "items": [
                    "The emoji-keyed expression pose catalog (loaded from "
                    "expressions.toml) that agent tool-use's apply_pose drives.",
                    "list — every catalog emoji + a generated pose descriptor.",
                    "check — flags catalog poses too similar to be meaningfully " "distinct.",
                    "These verbs read the catalog file directly — no running " "engine needed.",
                ],
            },
            {"title": "Verbs", "items": list(_EXPRESSIONS_VERBS)},
            {
                "title": "Conventions",
                "items": [
                    "every command supports --json",
                    "results to stdout, diagnostics to stderr (never mixed)",
                    "a flagged 'check' is a warning, not an error — exit stays 0",
                ],
            },
        ],
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _expressions_no_verb(args: argparse.Namespace) -> int:
    # Bare `behavior expressions` lists the catalog (mirrors `behavior rules`).
    return cmd_expressions_list(args)


# --------------------------------------------------------------------------- #
# rules tick-seam composition (boot resilience)                               #
# --------------------------------------------------------------------------- #


def _boot_tick_seam() -> reload_driver.ReloadDriver | None:
    """Build the ``behavior engine run`` tick seam, resiliently.

    Loads the rules (``RulesLoader.reload()``, see ``reachy.behavior.rules``)
    exactly once at boot — both layers: the SHIPPED package resource and the
    box-local overlay layered over it. ``RulesLoader.reload()`` never raises a
    ``CliError`` itself — a MISSING overlay resolves to the shipped layer alone
    (nothing configured locally yet, not a rejection) — but on a PRESENT,
    malformed overlay it keeps the loader's last-good config (here: the shipped
    layer, since this is the first load) and records why in
    ``loader.last_error``.

    On a rejection this logs exactly one ``[SENSE stage=rule source=rules
    event=boot]`` drop line naming every reason (the validator's own message,
    which itself enumerates every offending field/id/value it found), and then
    degrades as far as it can — never crashing the process (which would
    otherwise feed a systemd ``Restart=on-failure`` crash loop):

    * when the fallback config still holds rules (the shipped layer), the seam
      IS installed and the robot keeps its shipped reactions — an operator's
      typo in their own overlay must not cost them the defaults as well;
    * when there is genuinely nothing left to run, this returns ``None`` and
      the caller installs NO tick seam at all, so the engine runs bare base
      presence (``feel-alive`` only, no rule seam).

    On success (including "no overlay yet") returns a ready
    :class:`~reachy.behavior.reload_driver.ReloadDriver`, which serves both rule
    evaluation and any later ``behavior reload`` for the life of this run.
    """
    loader = RulesLoader()
    loader.reload()
    if loader.last_error is not None:
        senselog.drop(RULE_STAGE, "rules", "boot", loader.last_error)
        if not (loader.current.react or loader.current.inhibit):
            return None
    return reload_driver.ReloadDriver(loader)


# --------------------------------------------------------------------------- #
# engine sub-noun (the 50 Hz process)                                         #
# --------------------------------------------------------------------------- #


def cmd_engine_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "reachy-mini-cli behavior engine",
        [
            {
                "title": "What",
                "items": [
                    "The persistent 50 Hz loop that composes active behaviors and "
                    "streams one immediate pose per tick to the robot.",
                    "It owns motion exclusively while running — don't also use "
                    "'move goto' / 'demo-mode'.",
                ],
            },
            {
                "title": "Verbs",
                "items": [
                    "engine start — spawn the loop in the background",
                    "engine stop — stop it (eases the robot to neutral)",
                    "engine status — process + daemon reachability",
                    "engine run — run it in the foreground (what start launches)",
                    "engine overview — this summary",
                ],
            },
            {
                "title": "State",
                "items": [
                    f"pid file: {supervisor.pid_file()}",
                    f"log file: {supervisor.log_file()}",
                    f"control spool: {control.behavior_dir()}",
                ],
            },
        ],
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def cmd_engine_start(args: argparse.Namespace) -> int:
    data = supervisor.start(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        compose_hz=args.compose_hz,
        energy=args.energy,
        base_layer=not args.no_base_layer,
        settle=not args.no_settle,
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_engine_stop(args: argparse.Namespace) -> int:
    data = supervisor.stop(timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_engine_status(args: argparse.Namespace) -> int:
    data = supervisor.status(base_url=args.base_url, timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


#: The submit-verbs' shared degrade note when no live engine confirms in time
#: (the command persists on disk for a later-started engine).
_UNCONFIRMED_NOTE = "engine did not confirm in time — is 'behavior engine' running?"


#: Explicit ``REACHY_PAT_SENSE`` tokens read as "opt out" (case/whitespace
#: insensitive) — see :func:`_pat_sense_enabled`.
_PAT_SENSE_FALSEY = frozenset({"0", "false", "no", "off"})


def _pat_sense_enabled() -> bool:
    """Whether the pat sense stack composes. Default ON (issue #80).

    ``REACHY_PAT_SENSE`` is read as a four-way value, not a plain denylist:

    * ABSENT -> enabled (the shipped default since issue #80).
    * an explicit falsey token (``0``/``false``/``no``/``off``, case/whitespace
      insensitive) -> disabled.
    * set but empty or blank (``REACHY_PAT_SENSE=`` or all-whitespace) ->
      disabled. A denylist alone would miss this: "" is not in the falsey set,
      so it would silently fall through to the default-on path even though an
      operator setting a var to nothing almost always means "unset this", not
      "turn it on" (Qodo review finding #4 on PR #83).
    * anything else (``1``/``true``/``yes``/``on``, or any other non-blank
      string) -> enabled.

    Shipped dormant in 0.36.0 (issue #79) because no threshold separated a real
    pat from the idle wander. The hands-on calibration session settled why: the
    plant is quiet ONLY while it is not tracking a moving target, so the sense
    now gates on commanded stillness (see :mod:`reachy.behavior.pat_sense`) and
    the ghost class is structurally impossible rather than threshold-managed.
    """
    raw = os.environ.get("REACHY_PAT_SENSE")
    if raw is None:
        return True
    value = raw.strip().lower()
    if not value:
        return False
    return value not in _PAT_SENSE_FALSEY


#: Env vars exposing the pat-sense stillness gate's tuning without editing
#: source (t2, "no-freeze pat sense" — a runnable experiment surface). Each is
#: read directly at composition time, mirroring ``REACHY_PAT_SENSE`` right
#: above rather than threading through ``EngineConfig``/``supervisor``'s
#: background-spawn argv: those exist for tuning that must survive `behavior
#: engine start`'s detached re-spawn, while this pair is a bench/experiment
#: knob for the foreground `_compose_run_seam` call the on/off switch beside
#: it already reads the same way. Unset -> the shipped defaults from
#: :mod:`reachy.behavior.pat_sense`, so a box that never sets either var
#: composes a byte-identical driver.
_STILL_HOLD_S_ENV = "REACHY_PAT_STILL_HOLD_S"
_STILL_EPS_ENV = "REACHY_PAT_STILL_EPS"
#: Press-threshold overrides, in degrees of conditioned deviation. Sensing
#: through CONTINUOUS idle motion needs a far firmer press than sensing on a
#: still head: `pat_sense`'s 0.5 deg default was measured against a quiet plant
#: and fires phantom pats once the head never holds still (15 detections /
#: 11 reaction fires in 45 s hands-off, measured on the robot). `listen`'s
#: `PatHook` already runs 2.5 deg / 6.0 deg for exactly this reason.
_PRESS_THRESHOLD_ENV = "REACHY_PAT_PRESS_DEG"
_YAW_PRESS_THRESHOLD_ENV = "REACHY_PAT_YAW_PRESS_DEG"
#: Deviation high-pass time constant (s). This is the FREQUENCY discriminator,
#: and under continuous idle motion it matters more than amplitude: the robot's
#: own wander is slow (components at 0.13-0.37 Hz) while a hand's presses are
#: fast and jagged, so a tighter high-pass rejects the plant and keeps the pat.
#: Measured over the shipped wander fixtures, tightening 0.8 -> 0.08 cuts ghosts
#: 6 -> 1 while keeping all 8 petted detections; amplitude thresholds alone
#: could not separate them at any value.
_HP_TAU_ENV = "REACHY_PAT_HP_TAU"
#: Seconds of quiet before an interaction is declared released. Must OUTLAST the
#: window the reaction blinds itself for, or contact dies before it can be
#: re-acquired and the ladder can never climb (the t12 sustain failure: max
#: contact 0.82 s against a 4.0 s contentment threshold). That blind window is
#: the reaction's entry slew plus the gate's re-arm, so a slow-window gate at
#: `still_hold_s=1.0` needs a release budget well above 1.0 s.
_RELEASE_AFTER_ENV = "REACHY_PAT_RELEASE_AFTER_S"


def _pat_float_env(name: str, default: float) -> float:
    """Parse *name* as a float, or fall back to *default* when unset.

    A set-but-unparseable value is a clean user error (never a silent
    fallback to *default*, never a raw traceback) naming the offending env
    var and its value, matching the error contract every other malformed-
    input path in this CLI follows (see e.g. ``reachy.speech.harmonic``'s
    ``REACHY_HARMONIC_ARTICULATION`` handling).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid {name}={raw!r} (expected a number)",
            remediation=f"set {name} to a number, or unset it to use the default",
        ) from exc
    # "nan"/"inf" parse cleanly as floats but are never valid tuning. Left
    # unchecked they propagate into the filters and thresholds and silently
    # disable sensing rather than reporting a mistake.
    if not math.isfinite(value):
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid {name}={raw!r} (expected a finite number)",
            remediation=f"set {name} to a finite number, or unset it to use the default",
        )
    if value < 0.0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid {name}={raw!r} (expected a non-negative number)",
            remediation=f"set {name} to zero or more, or unset it to use the default",
        )
    return value


def _pat_float_override(name: str, default: float) -> float | None:
    """Return the override for *name*, or ``None`` when it is not set.

    Presence of the env var — not inequality against the default — decides
    whether an override exists. That avoids comparing floats for equality, and
    is the more honest question anyway: setting a variable to the shipped
    default is still an explicit choice by the operator.
    """
    if os.environ.get(name) is None:
        return None
    return _pat_float_env(name, default)


def _pat_still_tuning() -> tuple[float, float]:
    """Resolve this run's ``(still_hold_s, still_eps)`` for :class:`PatSenseDriver`.

    Both default to today's shipped values
    (:data:`~reachy.behavior.pat_sense.DEFAULT_STILL_HOLD_S` /
    :data:`~reachy.behavior.pat_sense.DEFAULT_STILL_EPS`) when
    :data:`_STILL_HOLD_S_ENV` / :data:`_STILL_EPS_ENV` are unset, so an
    operator who never touches either var gets a byte-identical driver.
    """
    return (
        _pat_float_env(_STILL_HOLD_S_ENV, DEFAULT_STILL_HOLD_S),
        _pat_float_env(_STILL_EPS_ENV, DEFAULT_STILL_EPS),
    )


def _pat_detector() -> PatDetector | None:
    """Build the pat detector, honouring the press-sensitivity overrides.

    Returns ``None`` when neither override is set, so ``PatSenseDriver`` keeps
    minting its own tuned default detector (#79) exactly as before — the
    override path must not change the shipped default by existing.

    Release thresholds track the press thresholds proportionally, preserving the
    shipped 0.4 press-to-release ratio (0.5/0.2) so raising sensitivity does not
    accidentally leave a press latched.
    """
    press_override = _pat_float_override(_PRESS_THRESHOLD_ENV, DEFAULT_PRESS_THRESHOLD)
    yaw_override = _pat_float_override(_YAW_PRESS_THRESHOLD_ENV, DEFAULT_PRESS_THRESHOLD)
    if press_override is None and yaw_override is None:
        return None
    press = DEFAULT_PRESS_THRESHOLD if press_override is None else press_override
    yaw_press = DEFAULT_PRESS_THRESHOLD if yaw_override is None else yaw_override
    ratio = DEFAULT_RELEASE_THRESHOLD / DEFAULT_PRESS_THRESHOLD
    return PatDetector(
        press_threshold=press,
        release_threshold=press * ratio,
        yaw_press_threshold=yaw_press,
        yaw_release_threshold=yaw_press * ratio,
    )


def _make_state_reader() -> HeldStateReader:
    """Build the held, media-free SDK pose reader — a test-injection seam.

    Isolated as a module-level factory so a composition test can inject a fake
    reader (recording ``read``/``close``) via ``monkeypatch.setattr`` without a
    real SDK. In production it returns a bare
    :class:`~reachy.robot.state_reader.HeldStateReader`, which itself degrades to
    a permanently-``None`` reader (one logged warning, then no reading) when the
    ``[sdk]`` extra is absent — which is why :func:`_compose_run_seam` composes
    the enabled pat stack without gating on an SDK-import probe.
    """
    return HeldStateReader()


def _compose_run_seam(transport, config: EngineConfig, rules_driver, runtime_consumer, probe=None):
    """Build ``behavior engine run``'s sense reader + tick seam + held pose reader.

    Composes runtime sense/act pieces onto the engine's ONE per-tick seam and
    returns ``(sense_reader, tick_seam, reader)``. Everything rides ONE
    :class:`TickBus` wrapped in :class:`TickMetrics`, so a tick-budget breach
    surfaces as a ``[SENSE ... event=overrun]`` line (c22).

    Probe composition is intentionally separate and observation-only: it omits
    rules, ordinary pat classification, intents, goto, and the pose holder. Its
    seam contains only a namespaced-command rejector, the passive probe, and an
    optional export snapshot. The main command spool is independently guarded by
    :func:`cmd_engine_run`; both paths return explicit negative command results.

    Perception (``sense_reader``)
    -----------------------------
    Each tick's :class:`~reachy.behavior.sense.Sense` is
    ``read_perception(SenseProviders(pat_event=..., pat_state=...),
    base=doa_poller(t))``: the
    :class:`~reachy.behavior.sense.DoaPoller` supplies the throttled DoA/speech
    leg (its own low-rate polling + failure-swallowing preserved), while the pat
    legacy cue is a non-consuming PEEK of the pat driver's one-tick latch, while
    the persistent state is a second PEEK of that same driver — so both views
    describe one held reader and detector. A mic-less box reads EMPTY_SENSE for
    the DoA leg exactly as before.

    Degrade contract (no ``[sdk]`` extra)
    -------------------------------------
    The SDK sense stack (:class:`HeldStateReader` + :class:`PatDetector` +
    :class:`PatSenseDriver` + :class:`LastPoseHolder`) is ALWAYS composed: every
    piece is import-safe without ``reachy_mini``, and :class:`HeldStateReader`
    degrades internally to permanently-``None`` when the extra is absent (one
    logged warning, then no reading). So on a bare box the engine behaves exactly
    as before EXCEPT for a few inert drivers — the pat driver reads ``None`` every
    tick (no pat events, no errors), the holder harmlessly stashes each pose, and
    an empty goto lane is a per-tick no-op — i.e. DoA-only sense, no pat, no
    exceptions. The goto path itself needs no SDK, so it still works.

    Act-in seams (the ONE TickBus, in driver order)
    -----------------------------------------------
    ``[rules_driver, intent_driver, pat_driver, holder, goto_lane]`` (with a
    :class:`SenseSnapshotDriver` appended when exporting):

    * ``rules_driver`` / ``intent_driver`` first — they make the tick's symbolic
      decisions (admit/evict, drain the intent + goto command spools). The GOTO
      kind handler runs inside the intent driver's drain and enqueues onto the
      goto lane. The intent driver builds its OWN registry with the four intent
      kinds, then the GOTO kind is registered into THAT SAME registry, so all
      five kinds share one registry: the merged
      :class:`~reachy.behavior.intents.IntentDriver` only auto-registers its four
      defaults when it builds the registry itself, so GOTO is added afterward
      rather than pre-loaded into an injected (would-be-empty-of-defaults)
      registry.
    * ``pat_driver`` — reads THIS tick's ``ctx.pose`` (commanded head) and the
      injected reader (actual head) DIRECTLY (never via the holder), advances the
      detector, and latches a pat for the NEXT tick's sense read. It mutates no
      shared engine state and only latches, so its position among the readers is
      immaterial to correctness; it sits after the symbolic drivers by
      convention.
    * ``holder`` BEFORE ``goto_lane`` — the holder stashes this tick's streamed
      ``ctx.pose``; the goto lane's ``start_pose_provider`` peeks that stash when
      it admits a goto. Running the holder first means a goto admitted THIS tick
      (from a command the intent driver just drained) seeds its minjerk start from
      this tick's freshest pose instead of last tick's stale one.
    * ``SenseSnapshotDriver`` last (export only) — publishes the tick's perception
      snapshot on change; it reads the fixed ``ctx.sense`` so its position is
      immaterial, appended last so the sense block trails the decisions.

    The returned ``reader`` is the held SDK client the caller MUST ``close()`` at
    shutdown (an unclosed ``no_media`` client hangs the process at interpreter
    exit — see :mod:`reachy.robot.state_reader`).
    """
    doa_poller = DoaPoller(lambda: read_doa(transport))

    if probe is not None:
        reader = _make_state_reader()
        shared_reader = SharedPoseReader(reader.read)
        mode, probe_emit = probe
        providers = SenseProviders()

        def probe_sense_reader(t):
            return read_perception(providers, base=doa_poller(t))

        drivers = [
            ProbeNamespaceGuard(control.CommandSpool(namespace=INTENT_NAMESPACE)),
            ProbeDriver(mode, shared_reader, emit=probe_emit),
        ]
        consumers = []
        if runtime_consumer is not None:
            drivers.append(SenseSnapshotDriver())
            consumers.append(runtime_consumer)
        bus = TickBus(drivers=drivers, consumers=consumers)
        return (
            probe_sense_reader,
            TickMetrics(bus, budget_s=budget_from_hz(config.compose_hz)),
            reader,
        )

    # The pat sense stack ships ON after the hands-on #80 gate finding: the
    # complete command must hold still before sensing, which removes wander
    # ghosts structurally while allowing a settled reaction owner to keep
    # sensing. REACHY_PAT_SENSE=0 is the explicit sensing rollback.
    reader = None
    pat_driver = None
    if _pat_sense_enabled():
        reader = _make_state_reader()
        still_hold_s, still_eps = _pat_still_tuning()
        pat_kwargs: dict = {
            "still_hold_s": still_hold_s,  # REACHY_PAT_STILL_HOLD_S override (t2)
            "still_eps": still_eps,  # REACHY_PAT_STILL_EPS override (t2)
        }
        # `detector` and `hp_tau` are passed ONLY when actually overridden.
        # Occupying a keyword with its own default looks like a no-op but is
        # not: a caller injecting its own detector or high-pass (every
        # pet-runtime integration test does) would collide on it. Env overrides
        # must be additive — absent env leaves the driver's own defaults alone.
        override = _pat_detector()
        if override is not None:
            pat_kwargs["detector"] = override  # REACHY_PAT_*_PRESS_DEG overrides
        hp_tau = _pat_float_override(_HP_TAU_ENV, DEFAULT_HP_TAU)  # frequency gate
        if hp_tau is not None:
            pat_kwargs["hp_tau"] = hp_tau
        release_after = _pat_float_override(_RELEASE_AFTER_ENV, RELEASE_AFTER_S)
        if release_after is not None:
            pat_kwargs["release_after_s"] = release_after
        pat_driver = PatSenseDriver(reader=reader.read, **pat_kwargs)  # default detector (#79)
    holder = LastPoseHolder()
    providers = SenseProviders(
        pat_event=pat_driver.as_provider() if pat_driver is not None else None,
        pat_state=pat_driver.as_state_provider() if pat_driver is not None else None,
    )

    def sense_reader(t):
        # DoA (throttled by the poller) as the base; the pat cue (when the
        # enabled stack is composed) is peeked in legacy-event and persistent-
        # state forms from the same driver.
        return read_perception(providers, base=doa_poller(t))

    goto_lane = GotoLane(start_pose_provider=holder.as_start_pose_provider())
    intent_driver = IntentDriver(
        mode_setter=rules_driver.set_active_mode if rules_driver is not None else None,
        known_modes=rules_driver.known_modes if rules_driver is not None else None,
    )
    # Register the GOTO kind into the intent driver's OWN registry (which already
    # carries the four intent defaults) so all five kinds share one registry.
    intent_driver.registry.register(GOTO, make_goto_handler(goto_lane))

    drivers = [
        d for d in (rules_driver, intent_driver, pat_driver, holder, goto_lane) if d is not None
    ]
    consumers = []
    if runtime_consumer is not None:
        drivers.append(SenseSnapshotDriver())
        consumers.append(runtime_consumer)
    bus = TickBus(drivers=drivers, consumers=consumers)
    return sense_reader, TickMetrics(bus, budget_s=budget_from_hz(config.compose_hz)), reader


def _probe_engine_is_fresh() -> bool:
    """Whether state.json proves another CLI engine heartbeat is still live.

    Delegates to :func:`reachy.behavior.liveness.engine_is_live` — the ONE
    definition of "an engine is driving the head", shared with the foreground
    ``pat run`` / ``sleep run`` refusal. Two independently-drifting answers to
    that question is precisely the defect the shared module exists to prevent;
    see its docstring for the freshness/skew rationale that used to live here.
    """
    return liveness.engine_is_live()


def _open_probe_output(path: Path):  # type: ignore[no-untyped-def]
    """Exclusive output-open seam, kept narrow for cleanup verification."""
    return path.open("x", encoding="utf-8")


def _refuse_bad_probe_request(probe_mode: str | None, probe_output: str | None) -> None:
    """Reject a malformed or unsafe probe request before anything is constructed."""
    if bool(probe_mode) != bool(probe_output):
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--probe-mode and --probe-output must be supplied together",
            remediation="choose held/unheld and a new JSONL output path",
        )
    if probe_mode is not None and _probe_engine_is_fresh():
        raise CliError(
            code=EXIT_USER_ERROR,
            message="probe refused: already-running CLI behavior engine has a fresh heartbeat",
            remediation=(
                "for a foreground 'reachy behavior engine run', press Ctrl-C in its owning "
                "terminal; use 'reachy behavior engine stop' only when it was launched with "
                "'reachy behavior engine start'; then start the sole foreground "
                "'reachy behavior engine run' with --probe-mode and --probe-output"
            ),
        )


def _open_probe(probe_mode: str, probe_output: str):  # type: ignore[no-untyped-def]
    """Open the exclusive capture stream and pair it with its record emitter.

    Returns ``(stream, probe)`` so the caller keeps the stream for its
    ``finally`` cleanup while handing ``probe`` to the seam composer.
    """
    try:
        stream = _open_probe_output(Path(probe_output))
    except FileExistsError as err:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"probe output already exists: {probe_output}",
            remediation="choose a new --probe-output path; captures are never overwritten",
        ) from err
    except OSError as err:
        # path.open("x") also raises for a missing parent, an unwritable
        # directory, or a path that is itself a directory. Each is a user-fixable
        # mistake and earns the same structured remediation as the clash above.
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"cannot create probe output {probe_output}: {type(err).__name__}: {err}",
            remediation=(
                "choose a --probe-output path in an existing, writable directory "
                "that is not itself a directory"
            ),
        ) from err

    def _probe_emit(record: dict) -> None:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()

    return stream, (probe_mode, _probe_emit)


def _engine_live_line(  # type: ignore[no-untyped-def]
    config, transport, rules_driver, probe_mode: str | None
) -> str:
    """Build the one-line 'engine live' banner naming the composed layers."""
    if probe_mode is not None:
        rules_note = " (observation-only probe)"
    elif rules_driver is not None:
        rules_note = " + rules"
    else:
        rules_note = " (rules rejected — base presence only)"
    base_note = " + base layer" if config.base_layer else ""
    return (
        f"[behavior] engine live: {config.compose_hz:g} Hz via {transport.name}"
        f"{base_note}{rules_note}; Ctrl-C to stop"
    )


def cmd_engine_run(args: argparse.Namespace) -> int:
    install_logging(getattr(args, "log_level", None))
    json_mode = bool(getattr(args, "json", False))
    probe_mode = getattr(args, "probe_mode", None)
    probe_output = getattr(args, "probe_output", None)
    _refuse_bad_probe_request(probe_mode, probe_output)

    # The fresh-heartbeat refusal above precedes transport construction, output
    # creation, and engine_run's control.reset() preamble: a probe invocation can
    # never become a second command owner beside a known-live CLI engine.
    transport = get_transport(args)
    config = _engine_config(args)
    spool = control.CommandSpool()
    rules_driver = None if probe_mode is not None else _boot_tick_seam()
    engine_control = ProbeCommandGuard(spool) if probe_mode is not None else spool
    probe_stream = None
    reader = None
    runtime_consumer = None
    try:
        probe = None
        if probe_mode is not None:
            probe_stream, probe = _open_probe(probe_mode, probe_output)

        def _on_start() -> None:
            if probe_mode is not None:
                emit_diagnostic(f"{PROBE_CUES[probe_mode]} — passive {probe_mode} observation")
            if not json_mode:
                emit_diagnostic(_engine_live_line(config, transport, rules_driver, probe_mode))

        # Runtime-events export sink (None unless --export -); this remains
        # separate from the cognition feed and carries runtime events only.
        runtime_consumer = build_runtime_export_consumer(args)
        sense_reader, tick_seam, reader = _compose_run_seam(
            transport, config, rules_driver, runtime_consumer, probe=probe
        )

        def _emit(event: dict) -> None:
            if json_mode and runtime_consumer is None:
                emit_result(event, json_mode=True)

        ticks = engine_run(
            transport,
            config,
            on_start=_on_start,
            emit=_emit,
            max_ticks=args.max_ticks,
            control=engine_control,
            sense=sense_reader,
            tick_seam=tick_seam,
        )
    finally:
        # Covers output-open, export setup, seam composition, and engine_run.
        if reader is not None:
            reader.close()
        if probe_stream is not None:
            probe_stream.close()
    if runtime_consumer is not None:
        emit_diagnostic(f"[behavior] engine stopped after {ticks} tick(s) (export: stdout)")
    elif not json_mode:
        emit_diagnostic(f"[behavior] engine stopped after {ticks} tick(s)")
    return 0


# --------------------------------------------------------------------------- #
# registration                                                                #
# --------------------------------------------------------------------------- #


def _add_engine_tuning(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--compose-hz",
        type=float,
        default=50.0,
        dest="compose_hz",
        help="Engine tick rate in Hz (default: 50).",
    )
    parser.add_argument(
        "--energy", type=float, default=1.0, help="Base-layer liveliness multiplier (default: 1.0)."
    )
    parser.add_argument(
        "--no-base-layer",
        action="store_true",
        dest="no_base_layer",
        help="Do not seed the passive feel-alive base layer.",
    )
    parser.add_argument(
        "--no-settle",
        action="store_true",
        dest="no_settle",
        help="Do not ease the robot to neutral on stop.",
    )


def _register_list(noun_sub: argparse._SubParsersAction) -> None:
    p = noun_sub.add_parser("list", help="List the built-in behaviors.")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_list)


def _register_run(noun_sub: argparse._SubParsersAction) -> None:
    p = noun_sub.add_parser("run", help="Push a behavior onto the running engine.")
    p.add_argument("name", help="Behavior name (see 'behavior list').")
    p.add_argument(
        "--set",
        nargs="*",
        default=None,
        metavar="KEY=VALUE",
        help="Override behavior parameters (e.g. --set amp=20 period=0.5).",
    )
    p.add_argument(
        "--class",
        dest="behavior_class",
        choices=_CLASSES,
        default=None,
        help="Contention class (default: the behavior's own).",
    )
    p.add_argument(
        "--channels",
        nargs="*",
        default=None,
        metavar="CHANNEL",
        help=f"Override claimed channels ({', '.join(CHANNELS)}).",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="Run once (one-shot).")
    group.add_argument("--loop", action="store_true", help="Run looping until stopped.")
    p.add_argument(
        "--duration", type=float, default=None, help="Lifetime in seconds (default: per behavior)."
    )
    p.add_argument(
        "--no-ensure-engine",
        action="store_true",
        dest="no_ensure_engine",
        help="Do not auto-start the engine if it is not running.",
    )
    p.add_argument(
        "--await-timeout",
        type=float,
        default=1.0,
        dest="await_timeout",
        help=_AWAIT_TIMEOUT_HELP,
    )
    _add_engine_tuning(p)  # forwarded to an auto-start
    add_robot_args(p)
    p.set_defaults(func=cmd_run)


def _register_stop(noun_sub: argparse._SubParsersAction) -> None:
    p = noun_sub.add_parser("stop", help="Stop a running behavior (id | name | all).")
    p.add_argument("target", help="Behavior id, name, or 'all' (keeps the idle base layer).")
    p.add_argument(
        "--await-timeout",
        type=float,
        default=1.0,
        dest="await_timeout",
        help=_AWAIT_TIMEOUT_HELP,
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_stop)


def _register_status(noun_sub: argparse._SubParsersAction) -> None:
    p = noun_sub.add_parser("status", help="Active behaviors + channel ownership + engine state.")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Daemon base URL.")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Request timeout.")
    p.set_defaults(func=cmd_status)


def _register_reload(noun_sub: argparse._SubParsersAction) -> None:
    p = noun_sub.add_parser(
        "reload", help="Reload rules.toml in the running engine (applied between ticks)."
    )
    p.add_argument(
        "--await-timeout",
        type=float,
        default=1.0,
        dest="await_timeout",
        help=_AWAIT_TIMEOUT_HELP,
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_reload)


def _register_goto(noun_sub: argparse._SubParsersAction) -> None:
    p = noun_sub.add_parser(
        "goto", help="Submit a goto (head/antennas/body-yaw) through the intents spool."
    )
    p.add_argument("--x", type=float, default=None, help="Head X offset in mm.")
    p.add_argument("--y", type=float, default=None, help="Head Y offset in mm.")
    p.add_argument("--z", type=float, default=None, help="Head Z offset in mm.")
    p.add_argument("--roll", type=float, default=None, help="Head roll in degrees.")
    p.add_argument("--pitch", type=float, default=None, help="Head pitch in degrees.")
    p.add_argument("--yaw", type=float, default=None, help="Head yaw in degrees.")
    p.add_argument(
        "--antennas",
        type=float,
        nargs=2,
        metavar=("RIGHT", "LEFT"),
        default=None,
        help="Antenna angles in degrees (right, left).",
    )
    p.add_argument(
        "--body-yaw",
        type=float,
        default=None,
        dest="body_yaw",
        help="Body yaw in degrees.",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=1.0,
        help="Movement duration in seconds (default: 1.0; the kind refuses over 10s).",
    )
    p.add_argument(
        "--interpolation",
        choices=INTERPOLATIONS,
        default="minjerk",
        help="Interpolation curve (default: minjerk).",
    )
    p.add_argument("--label", default=None, help="Optional label for the goto (default: 'goto').")
    p.add_argument(
        "--await-timeout",
        type=float,
        default=1.0,
        dest="await_timeout",
        help=_AWAIT_TIMEOUT_HELP,
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_goto)


def _register_rules(noun_sub: argparse._SubParsersAction) -> None:
    """The ``behavior rules`` sub-noun: render + validate ``rules.toml``.

    A noun with action-verbs must also expose ``overview`` (rubric requirement);
    bare ``rules`` (no sub-verb) lists the loaded config, mirroring ``think
    expressions``' bare-defaults-to-list idiom. ``parser_class`` propagates so
    nested parse errors keep the structured error contract.
    """
    r = noun_sub.add_parser(
        "rules", help="Render/validate rules.toml (see 'behavior rules overview')."
    )
    r.add_argument("--json", action="store_true", help=_JSON_HELP)
    r.set_defaults(func=_rules_no_verb, json=False)
    r_sub = r.add_subparsers(dest="rules_command", parser_class=type(r))

    ov = r_sub.add_parser("overview", help="Describe the rules sub-noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_rules_overview)

    ls = r_sub.add_parser("list", help="Render the loaded rules.toml.")
    ls.add_argument("--json", action="store_true", help=_JSON_HELP)
    ls.set_defaults(func=cmd_rules_list)

    ck = r_sub.add_parser("check", help="Validate rules.toml (always exits 0 on a content issue).")
    ck.add_argument("--json", action="store_true", help=_JSON_HELP)
    ck.set_defaults(func=cmd_rules_check)


def _register_expressions(noun_sub: argparse._SubParsersAction) -> None:
    """The ``behavior expressions`` sub-noun: list + check the expression catalog.

    Ported from ``think expressions`` (t18) — see the "expressions sub-noun"
    section above for why. A noun with action-verbs must also expose
    ``overview`` (rubric requirement); bare ``expressions`` (no sub-verb) lists
    the catalog, mirroring ``behavior rules``' bare-defaults-to-list idiom.
    ``parser_class`` propagates so nested parse errors keep the structured
    error contract.
    """
    ex = noun_sub.add_parser(
        "expressions",
        help="List/check the expression pose catalog (see 'behavior expressions overview').",
    )
    ex.add_argument("--json", action="store_true", help=_JSON_HELP)
    ex.set_defaults(func=_expressions_no_verb, json=False)
    ex_sub = ex.add_subparsers(dest="expressions_command", parser_class=type(ex))

    ov = ex_sub.add_parser("overview", help="Describe the expressions sub-noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_expressions_overview)

    ls = ex_sub.add_parser("list", help="List the expression pose catalog.")
    ls.add_argument("--json", action="store_true", help=_JSON_HELP)
    ls.set_defaults(func=cmd_expressions_list)

    ck = ex_sub.add_parser("check", help="Flag catalog poses too similar to be distinct.")
    ck.add_argument("--json", action="store_true", help=_JSON_HELP)
    ck.set_defaults(func=cmd_expressions_check)


def _register_engine(noun_sub: argparse._SubParsersAction) -> None:
    eng = noun_sub.add_parser("engine", help="Manage the 50 Hz engine process.")
    eng.add_argument("--json", action="store_true", help=_JSON_HELP)
    eng.set_defaults(func=cmd_engine_overview, json=False)
    eng_sub = eng.add_subparsers(dest="engine_command", parser_class=type(eng))

    ov = eng_sub.add_parser("overview", help="Describe the engine sub-noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_engine_overview)

    start = eng_sub.add_parser("start", help="Start the engine in the background.")
    _add_engine_tuning(start)
    add_robot_args(start)
    start.set_defaults(func=cmd_engine_start)

    run = eng_sub.add_parser("run", help="Run the engine in the foreground.")
    _add_engine_tuning(run)
    run.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        dest="max_ticks",
        help="Stop after this many ticks (default: run until signalled).",
    )
    run.add_argument(
        "--probe-mode",
        choices=PROBE_MODES,
        default=None,
        help="Passively observe the next full feel-alive motion episode through settling.",
    )
    run.add_argument(
        "--probe-output",
        type=Path,
        default=None,
        help="New JSONL path for --probe-mode (existing files are never overwritten).",
    )
    add_runtime_export_args(run)
    add_robot_args(run)
    add_log_level_arg(run)
    run.set_defaults(func=cmd_engine_run)

    stop = eng_sub.add_parser("stop", help="Stop the engine.")
    stop.add_argument("--json", action="store_true", help=_JSON_HELP)
    stop.add_argument(
        "--timeout",
        type=float,
        default=supervisor.DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait after SIGTERM before SIGKILL "
        f"(default: {supervisor.DEFAULT_STOP_TIMEOUT:g}).",
    )
    stop.set_defaults(func=cmd_engine_stop)

    st = eng_sub.add_parser("status", help="Engine process + daemon reachability.")
    st.add_argument("--json", action="store_true", help=_JSON_HELP)
    st.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Daemon base URL.")
    st.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Request timeout.")
    st.set_defaults(func=cmd_engine_status)


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "behavior",
        help="Compose robot behaviors on a 50 Hz loop (see 'reachy-mini-cli behavior overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="behavior_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the behavior noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)

    _register_list(noun_sub)
    _register_run(noun_sub)
    _register_stop(noun_sub)
    _register_status(noun_sub)
    _register_reload(noun_sub)
    _register_goto(noun_sub)
    _register_rules(noun_sub)
    _register_expressions(noun_sub)
    _register_engine(noun_sub)
