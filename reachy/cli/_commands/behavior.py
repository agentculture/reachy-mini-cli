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
import importlib
import json
import logging
import math
import os
import threading
from pathlib import Path
from typing import Any, Callable

from reachy import senselog
from reachy.behavior import control, library, liveness, reload_driver
from reachy.behavior import rules as rules_mod
from reachy.behavior import supervisor
from reachy.behavior.audio_pump import AudioPump
from reachy.behavior.audio_tee import AudioTee
from reachy.behavior.clip_rider import ClipRider, build_clip_encoder, clip_seconds_from_env
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
from reachy.behavior.face_sense import FaceSenseDriver, build_face_recognition
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
from reachy.behavior.rms_background import (
    DEFAULT_SILENCE_FLOOR,
    DEFAULT_WINDOW_S,
    SILENCE_FLOOR_ENV,
    WINDOW_S_ENV,
    RmsBackground,
)
from reachy.behavior.rms_sense import DEFAULT_MOVING_FLOOR, MOVING_FLOOR_ENV, make_rms_providers
from reachy.behavior.rule_engine import STAGE as RULE_STAGE
from reachy.behavior.rule_engine import TickBus
from reachy.behavior.rules import RulesLoader
from reachy.behavior.self_motion import (
    DEFAULT_EPS_DEG,
    DEFAULT_EPS_MM,
    DEFAULT_TAIL_S,
    EPS_DEG_ENV,
    EPS_MM_ENV,
    TAIL_S_ENV,
    SelfMotionDriver,
)
from reachy.behavior.sense import (
    FED_SENSE_FIELDS,
    DoaPoller,
    SenseProviders,
    read_doa,
    read_perception,
)
from reachy.behavior.sense_availability import SenseAvailabilityDriver, runtime_probes
from reachy.behavior.speech_act import SpeechActuator
from reachy.behavior.tick_metrics import TickMetrics, budget_from_hz
from reachy.behavior.transcript_sense import TranscriptSenseDriver
from reachy.cli._commands._robot import add_robot_args, emit_payload, get_transport, noun_overview
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from reachy.cli._export import add_runtime_export_args, build_runtime_export_consumer
from reachy.cli._logging import add_log_level_arg, install_logging
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.export.events_client import VENDOR_IMPORT, EventsCliClient
from reachy.export.mqtt import NervousPublisher, broker_url
from reachy.export.runtime import SenseSnapshotDriver
from reachy.motion.pat import PatDetector
from reachy.robot import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, INTERPOLATIONS
from reachy.robot.media_client import HeldMediaClient
from reachy.robot.state_reader import HeldStateReader
from reachy.speech.distinctness import find_too_similar as _find_too_similar
from reachy.speech.expressions import NEUTRAL_KEY, Catalog
from reachy.speech.realtime import RealtimeTranscriber

logger = logging.getLogger(__name__)

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
    except Exception as err:  # status must never crash on rules
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
        # Both react-only fields, reported even when unset (a stable key set
        # beats a shape that changes per rule). `say` especially: an operator
        # reading this verb is asking "what will my robot do", and the words it
        # is about to speak are the loudest part of that answer.
        data["duration_s"] = rule.duration_s
        data["say"] = rule.say
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
        # "No overlay" stopped meaning "no rules" when the release began
        # shipping defaults (t15): the rules listed above are real and running,
        # they just came from the package rather than from this path. Saying
        # "nothing configured" next to three listed rules would read as a bug.
        shipped = len(config.react) + len(config.inhibit)
        payload["note"] = (
            f"no box-local rules file yet — showing the {shipped} shipped default rule(s)"
            if shipped
            else "no rules file yet — nothing configured"
        )
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


def _unfed_field_warnings(config: rules_mod.RulesConfig) -> list[str]:
    """Warn on every rule predicate keyed to a sense field nothing feeds.

    ``reachy.behavior.rules.SENSE_FIELDS`` accepts more predicate fields than
    the current engine composition (``_compose_run_seam``, above) actually
    wires a live provider for — see
    ``reachy.behavior.sense.FED_SENSE_FIELDS``, the one declared source of
    truth this function reads. A rule keyed on a field outside that set
    validates cleanly (the schema only checks the field NAME is a known one)
    and then silently never fires — exactly the class of silent no-op
    ``reachy.senselog``'s "a drop always names its reason" discipline exists
    to prevent. This is a LINT finding, not a validation failure: the rule is
    well-formed, just currently inert, so it is reported as a warning (see
    ``cmd_rules_check``) and never a reason to reject the file.

    Checks both react and inhibit rules, in file order, so an operator sees
    every offending rule at once rather than one-at-a-time across repeated
    edits.
    """
    warnings: list[str] = []
    sections = ((rules_mod.KIND_REACT, config.react), (rules_mod.KIND_INHIBIT, config.inhibit))
    for kind, rules in sections:
        for index, rule in enumerate(rules):
            field = rule.when.field
            if field in FED_SENSE_FIELDS:
                continue
            warnings.append(
                f"{kind}[{index}] (id={rule.id!r}) is keyed on sense field {field!r}, but "
                "nothing in the current composition feeds it — this rule will validate "
                "cleanly but can never fire (fields currently fed: "
                f"{', '.join(sorted(FED_SENSE_FIELDS))})"
            )
    return warnings


def _uncorroborated_field_warnings(config: rules_mod.RulesConfig) -> list[str]:
    """Warn on every rule keyed on a sense field too noisy to stand alone.

    The sibling of ``_unfed_field_warnings``, and the same class of finding: a
    rule that is schema-valid and yet empirically wrong. Where that one catches
    a predicate that can NEVER fire, this catches one that fires far too OFTEN —
    ``speech_detected`` measured true 45.8 % of the time in a quiet room with
    nobody speaking, with an uncorrelated bearing
    (``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 2).
    ``reachy.behavior.rules.UNCORROBORATED_SENSE_FIELDS`` is the one declared
    source of truth this reads.

    Why a WARNING and not a fail-closed refusal
    ===========================================
    The repo's refusal precedent (``goto_intent``'s out-of-range axis, a react
    rule's unbounded looping lifetime) covers defects with a RUNAWAY actuator an
    operator cannot recover from — the incident behind the bounded-lifetime rule
    was a head that oscillated until stopped by hand. This is not that: a
    ``speech``-keyed rule is already bounded twice over, by ``cooldown_s`` on
    firing rate and by the ``duration_s`` the lifetime invariant forces onto any
    looping behavior. The failure is a nuisance, not a runaway.

    Against that, a load-time refusal would be actively unsafe HERE: rules are
    loaded by the boot-persistent runtime, so shipping one would turn an
    upgrade into a robot whose presence refuses to start over a rule that had
    been working — failing closed on the whole presence to fix a noisy
    predicate. And because a rule carries exactly one predicate, refusal would
    be indistinguishable from removing ``speech`` from
    ``reachy.behavior.rules.SENSE_FIELDS``; that is a product decision to take
    deliberately, not a side effect of a lint.

    The SHIPPED layer gets the hard treatment instead, where it belongs — it is
    ours, it reaches every robot on upgrade, and no one runs a linter when it
    does. ``tests/test_behavior_rules_cli.py`` pins it, so a future task that
    ships such a rule fails CI rather than a deployment.
    """
    warnings: list[str] = []
    sections = ((rules_mod.KIND_REACT, config.react), (rules_mod.KIND_INHIBIT, config.inhibit))
    for kind, rules in sections:
        for index, rule in enumerate(rules):
            field = rule.when.field
            if field not in rules_mod.UNCORROBORATED_SENSE_FIELDS:
                continue
            warnings.append(
                f"{kind}[{index}] (id={rule.id!r}) is keyed on bare sense field {field!r}, "
                f"which measured true {rules_mod.UNCORROBORATED_AT_REST_RATE} at rest — "
                "this rule will validate cleanly and then fire on roughly a coin flip. "
                "A rule carries exactly one predicate, so pair it with a corroborating "
                "signal instead by keying on one of: "
                f"{', '.join(rules_mod.CORROBORATING_SENSE_FIELDS)}"
            )
    return warnings


def _rules_check_payload(
    path: Path, *, reader: Callable[[Path], str] | None = None
) -> dict[str, object]:
    """Validate *path*; returns ``{ok, path, exists, reasons, warnings, counts}``.

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

    ``warnings`` (t16) additively reports every rule keyed to a sense field
    nothing currently feeds (see ``_unfed_field_warnings``) — a rule can be
    schema-valid (``reasons`` empty) yet still earn a warning, since "well
    formed" and "wired to something live" are different questions. ``ok`` folds
    BOTH signals, mirroring ``think expressions check``'s ``ok = not flagged``:
    ``True`` only when the file is both valid and every predicate is fed. This
    never changes the exit code — a warning is a warning, not a gate.

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
        return {
            "ok": False,
            "path": str(path),
            "exists": exists,
            "reasons": [err.message],
            "warnings": [],
        }
    warnings = _unfed_field_warnings(config) + _uncorroborated_field_warnings(config)
    return {
        "ok": not warnings,
        "path": str(path),
        "exists": exists,
        "reasons": [],
        "warnings": warnings,
        "counts": {
            "react": len(config.react),
            "inhibit": len(config.inhibit),
            "modes": len(config.modes),
        },
    }


def cmd_rules_check(args: argparse.Namespace) -> int:
    """Lint ``rules.toml``: a malformed file, or a rule keyed to a sense field
    nothing feeds, reports ``ok=False`` but still exits 0 (a warning, not a gate
    — mirrors ``think expressions check``). Only an actual I/O failure on an
    existing path is a clean exit-2 (via ``CliError``, handled by ``_dispatch``).
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
                    "'rules check' also warns (ok=false, exit 0) on a rule keyed to a "
                    "sense field nothing currently feeds — it validates but can never fire",
                    "'rules check' likewise warns on a rule keyed on bare 'speech', which "
                    "measured 45.8% true in a quiet room — pair it with a corroborating "
                    "signal (transcript/rms_ratio/rms/pat/face) instead",
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
# Ported from the retired `think expressions` sub-noun, which went with the
# rest of the LLM-cognition `think` noun (t20). The catalog itself
# (`reachy.speech.expressions`, backed by `expressions.toml`) and the
# distinctness check (`reachy.speech.distinctness`) are NOT LLM-coupled — a
# TOML table and a geometric distance function — and stayed needed
# afterward: `reachy.speech.tools`'s `apply_pose` tool (kept by `agent
# attach`) imports the catalog directly. So the data survives, and `behavior`
# is now its ONE CLI inspection surface — the surviving presence noun, which
# already hosts a sibling sub-noun (`rules`, just above) in exactly this
# "render + lint a file, no running engine needed" shape.

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
    return reload_driver.ReloadDriver(loader, param_overrides=_behavior_param_overrides())


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


def _make_self_motion() -> SelfMotionDriver:
    """Build the self-motion latch (#95), honouring its env tuning.

    Reads :data:`~reachy.behavior.self_motion.TAIL_S_ENV` /
    :data:`~reachy.behavior.self_motion.EPS_DEG_ENV` /
    :data:`~reachy.behavior.self_motion.EPS_MM_ENV` at composition time — the
    same read-at-composition pattern as the ``REACHY_PAT_*`` knobs above — so
    an operator who never sets any of them composes a driver at the shipped
    defaults, and the driver module itself stays environment-free.
    """
    return SelfMotionDriver(
        eps_deg=_pat_float_env(EPS_DEG_ENV, DEFAULT_EPS_DEG),
        eps_mm=_pat_float_env(EPS_MM_ENV, DEFAULT_EPS_MM),
        tail_s=_pat_float_env(TAIL_S_ENV, DEFAULT_TAIL_S),
    )


def _rms_moving_floor() -> float:
    """Resolve the moving rms floor (#95), or its infinite shipped default.

    Deliberately NOT :func:`_pat_float_env`: that helper refuses ``inf`` (a
    non-finite tuning value is never valid for the pat filters), while here
    INFINITY is the shipped default — full suppression while moving — and the
    string ``"inf"`` must round-trip as an explicit operator choice. Only
    ``nan`` and negatives are refused (fail-closed as a clean user error,
    matching the CLI's malformed-env contract).
    """
    raw = os.environ.get(MOVING_FLOOR_ENV)
    if raw is None:
        return DEFAULT_MOVING_FLOOR
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid {MOVING_FLOOR_ENV}={raw!r} (expected a number or 'inf')",
            remediation=f"set {MOVING_FLOOR_ENV} to a number or 'inf', or unset it",
        ) from exc
    if math.isnan(value) or value < 0.0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid {MOVING_FLOOR_ENV}={raw!r} (expected a non-negative number)",
            remediation=f"set {MOVING_FLOOR_ENV} to zero or more (or 'inf'), or unset it",
        )
    return value


#: ``REACHY_*`` env name -> the ``orient-to-sound`` knob it tunes. The three d6
#: admission knobs, and only those: the ratio that earns the antenna lean, and
#: the LOUD/ONGOING pair that promotes to a head turn. Every other orient knob
#: stays rules-file-only — a box tunes admission, a rules file states behavior.
_ORIENT_PARAM_ENV: dict[str, str] = {
    "REACHY_ORIENT_RMS_RATIO": "rms_ratio",
    "REACHY_ORIENT_RMS_RATIO_LOUD": "rms_ratio_loud",
    "REACHY_ORIENT_SUSTAIN_S": "sustain_s",
}


def _behavior_param_overrides() -> dict[str, dict[str, float]]:
    """Resolve the ``REACHY_ORIENT_*`` knobs into a rule-engine param overlay.

    Read at COMPOSITION time (the same pattern as ``REACHY_PAT_*`` /
    ``REACHY_SELF_MOVING_*`` / :func:`_rms_background`), so
    :mod:`reachy.behavior.orient` and :mod:`reachy.behavior.rule_engine` stay
    environment-free and deterministic in tests. A malformed value is a clean
    exit-1 user error, never a silent fallback — see :func:`_pat_float_env`.

    The overlay loses to the rules file on purpose
    (:class:`~reachy.behavior.rule_engine.RuleEngine`'s constructor note): this
    is the surface for a box-local systemd drop-in, the way the deployed robot
    already tunes the pat sense, not a way to override version-controlled
    behavior config from a stray exported variable.
    """
    values: dict[str, float] = {}
    for env_name, param in _ORIENT_PARAM_ENV.items():
        if os.environ.get(env_name) is None:
            continue
        values[param] = _pat_float_env(env_name, 0.0)
    return {"orient-to-sound": values} if values else {}


def _rms_background() -> RmsBackground:
    """Build the rolling background estimator (#102), honouring its env tuning.

    Reads :data:`~reachy.behavior.rms_background.WINDOW_S_ENV` /
    :data:`~reachy.behavior.rms_background.SILENCE_FLOOR_ENV` at composition
    time — the same read-at-composition pattern as the ``REACHY_PAT_*`` and
    ``REACHY_SELF_MOVING_*`` knobs above — so an operator who sets neither
    composes the shipped defaults and the estimator module itself stays
    environment-free. The RATIO is deliberately NOT an env knob here: it is the
    admission point, and it lives where an operator can already see and override
    it, in the ``look-toward-sound`` rule and ``OrientParams``.
    """
    return RmsBackground(
        window_s=_pat_float_env(WINDOW_S_ENV, DEFAULT_WINDOW_S),
        silence_floor=_pat_float_env(SILENCE_FLOOR_ENV, DEFAULT_SILENCE_FLOOR),
    )


#: How often the background keeper re-checks each held client's free
#: :attr:`connected` predicate (seconds). Deliberately faster than the holders'
#: own 5 s retry backoff: the poll itself costs nothing (a pure attribute read),
#: and the backoff — not this period — is what throttles actual reconnect
#: attempts, so a shorter period only shortens the window between a daemon
#: coming up and the sense noticing.
HOLDER_KEEPER_PERIOD_S = 2.0

#: Bounded join for the keeper thread at teardown.
_KEEPER_JOIN_TIMEOUT_S = 2.0

_WARM_STAGE = "warmup"

#: ``senselog`` source for the hearing session's own composition-time lines.
_REALTIME_LABEL = "realtime"

#: Mic rate the hearing session STARTS at when the held media client cannot
#: report a real one at composition time — a cold holder (the daemon may not be
#: up yet) reports ``None`` rather than blocking, by construction.
#:
#: It is a starting guess, never a lie the session keeps: the rate rides the
#: session's connect URL and the server resamples from it, so
#: :class:`~reachy.behavior.transcript_sense.TranscriptSenseDriver` pushes the
#: REAL rate through ``set_sample_rate`` after its first successful read, which
#: costs one clean, intentional reconnect. The fallback is announced (a named
#: ``mic-rate-unknown`` drop), never silent — a session quietly mis-declaring a
#: 48 kHz mic as 16 kHz mis-times every server-side VAD decision, and that is
#: exactly the failure that would otherwise present as "hearing is just bad".
DEFAULT_MIC_SAMPLE_RATE = 16000


def _mic_sample_rate(source) -> int:
    """The mic's REAL rate for the hearing session, or the announced fallback.

    *source* is the composed :class:`_AudioTap` (which duck-types the held media
    client's ``samplerate``). Under ``allow_inline_connect=False`` that property
    is a free read: it reports ``None`` on a cold holder instead of triggering
    the blocking construction, so calling it here cannot stall setup.

    A missing/unusable rate returns :data:`DEFAULT_MIC_SAMPLE_RATE` and says so
    on both channels — a log line for a human and a named ``senselog`` drop for
    the journal. See that constant's note for why silence would be wrong.
    """
    try:
        rate = source.samplerate
    except Exception as err:  # a rate probe must never block boot
        logger.debug("behavior: mic samplerate probe raised (%s); assuming unknown", err)
        rate = None
    try:
        value = int(rate) if rate else 0
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        senselog.stage(_WARM_STAGE, _REALTIME_LABEL, "setup", f"mic rate {value} Hz")
        return value
    logger.info(
        "behavior: mic sample rate unknown at composition (media not up yet); the hearing "
        "session starts at %d Hz and re-negotiates on the first real mic read",
        DEFAULT_MIC_SAMPLE_RATE,
    )
    senselog.drop(
        _WARM_STAGE,
        _REALTIME_LABEL,
        "setup",
        f"mic-rate-unknown (session assumes {DEFAULT_MIC_SAMPLE_RATE} Hz until the first read)",
    )
    return DEFAULT_MIC_SAMPLE_RATE


def _make_realtime_client(sample_rate: int) -> RealtimeTranscriber:
    """Build the runtime's ONE hearing session client — a test-injection seam.

    The sibling of :func:`_make_state_reader` / :func:`_make_media_client`, and
    the same discipline: everything about WHICH gateway is resolved inside
    :class:`~reachy.speech.realtime.RealtimeTranscriber` from the environment
    (``REACHY_REALTIME_URL`` / ``REACHY_REALTIME_API_KEY``, falling back to the
    shared ``REACHY_OPENAI_*`` gateway pair), so this stays a bare constructor
    call and an unusable endpoint is a clean exit-1 ``CliError`` at SETUP rather
    than a mid-session surprise.

    Only the rate is passed, because only composition knows it: it is a required
    constructor argument with no default on purpose (a hard-coded 16000 against
    a 48 kHz mic mis-times every VAD decision). See :func:`_mic_sample_rate`.

    Composed UNCONDITIONALLY, like the rest of the sense stack: the client is
    pure stdlib + numpy, needs no ``[sdk]`` extra, and a gateway that is not
    there is a LATCHED ``session-down`` (one line, then quiet) plus its own
    bounded reconnect backoff — never an exception and never a per-tick flood.
    """
    return RealtimeTranscriber(sample_rate=sample_rate)


def _make_state_reader() -> HeldStateReader:
    """Build the held, media-free SDK pose reader — a test-injection seam.

    Isolated as a module-level factory so a composition test can inject a fake
    reader (recording ``warm_up``/``read``/``close``) via ``monkeypatch.setattr``
    without a real SDK. In production it returns a
    :class:`~reachy.robot.state_reader.HeldStateReader`, which itself degrades to
    a permanently-``None`` reader (one logged warning, then no reading) when the
    ``[sdk]`` extra is absent — which is why :func:`_compose_run_seam` composes
    the enabled pat stack without gating on an SDK-import probe.

    ``allow_inline_connect=False`` closes the on-tick-thread construction door,
    and it is HALF of a pair that must never be split: with the door closed a
    read can never construct, so a holder nobody warms is a silently DEAD sense.
    :func:`_warm_holder` (at setup) and :class:`_HolderKeeper` (for a mid-run
    drop) are the other half. Together they are what actually fixes the measured
    425-1213 ms startup tick overruns — the connect is charged to setup, where
    there is no 20 ms budget to blow.
    """
    return HeldStateReader(allow_inline_connect=False)


def _make_speech_actuator(
    *, media_session_provider: Callable[[], Any] | None = None
) -> SpeechActuator:
    """Build the runtime's ONE voice — a test-injection seam.

    Everything about WHICH voice and WHICH speaker is resolved inside
    :class:`~reachy.behavior.speech_act.SpeechActuator` from the environment
    (``REACHY_VOICE_ENGINE``, ``REACHY_SPEECH_TRANSPORT``), so this stays a
    near-bare constructor call and a malformed variable fails at SETUP with a
    clean ``CliError`` rather than mid-utterance on the worker thread.

    The ONE thing the environment cannot supply is *media_session_provider*: the
    voice's ``sdk`` route plays through the runtime's HELD media client rather
    than opening a second one, and only composition knows that object. It
    arrives as a LATE-BOUND zero-arg callable — resolved per utterance on the
    speech worker — because this actuator is deliberately built BEFORE the media
    client exists (see :func:`_compose_run_seam`).

    Unlike the two held SDK clients, the actuator is composed with no degrade
    path to worry about: its shipped voice is the in-process harmonic synth, so
    a box with no ``[sdk]`` extra, no network and no TTS still has one, and a
    provider that yields nothing falls back to the daemon ``http`` route.
    """
    return SpeechActuator(media_session_provider=media_session_provider)


#: ``(module, attribute)`` naming the **events-cli** client class — the ONE
#: binding point for the nervous system's transport.
#:
#: The broker and its client belong to the sibling ``events-cli`` project
#: (``agentculture/events-cli#3``); this repo ships no MQTT library and speaks
#: no wire protocol (see CLAUDE.md's events-cli decision record). The wheel
#: shipped on 2026-07-24 as ``events-cli>=0.9`` and is now a base dependency,
#: so this spec resolves on a normal install.
#:
#: The class it names is NOT driven directly: its surface differs from the one
#: :mod:`reachy.export.mqtt` declares (``is_connected``/``close``, and a
#: constructor-time Last Will), so :mod:`reachy.export.events_client` adapts it
#: and is the only module in this repo that names the vendor. This is an ALIAS
#: of that module's constant, deliberately — two copies of a vendor's import
#: path are two things to update and one of them will be missed.
EVENTS_CLIENT_IMPORT = VENDOR_IMPORT


def _import_events_client(spec: tuple[str, str] = EVENTS_CLIENT_IMPORT):
    """Resolve a ``factory(url)`` for the bus client LAZILY, or ``None``.

    Total by construction: an absent package, a broken package, or a wheel that
    renamed the class all resolve to ``None`` — never an ``ImportError`` on the
    caller. That is what makes the publisher composable UNCONDITIONALLY on a box
    where events-cli is not installed (the bare HTTP profile, or a CI runner).

    What comes back is the ADAPTER
    (:class:`~reachy.export.events_client.EventsCliClient`), not the vendor
    class — the vendor is checked for presence here and driven from that one
    module. Constructing the adapter never touches the network: it records the
    broker address and builds the real client later, inside ``connect()``, which
    is the only point at which the Last Will is known.

    Deliberately NOT a module-scope import: ``_build_parser()`` imports this
    module for *every* invocation (``say run``, ``daemon status``, ``--help``),
    and a hard import there would put the cost on all of them. *spec* is
    injectable so both directions are testable without touching ``sys.modules``.
    """
    module_name, attr = spec
    try:
        module = importlib.import_module(module_name)
    except Exception as err:  # an optional package must never raise here
        logger.debug("behavior: events-cli client unavailable (%s: %s)", type(err).__name__, err)
        return None
    if getattr(module, attr, None) is None:
        logger.debug("behavior: %s exposes no %r", module_name, attr)
        return None
    return EventsCliClient


def _make_events_client():
    """Build the events-cli client for this run, or ``None`` — a test seam.

    The sibling of :func:`_make_state_reader` / :func:`_make_media_client` /
    :func:`_make_realtime_client`, and the same discipline: everything about
    WHICH broker is resolved from the environment
    (:func:`~reachy.export.mqtt.broker_url`, i.e. ``REACHY_MQTT_URL`` defaulting
    to ``localhost:1883``), read HERE at composition time so the publisher module
    stays environment-free.

    A ``None`` return is the NORMAL no-broker profile, not a fault:
    :class:`~reachy.export.mqtt.NervousPublisher` names it once
    (``dropped reason=no-client``) and every publish becomes a no-op. A
    constructor that raises is the same class of outcome and gets the same
    answer — the detail goes to the module logger, and the ONE named
    ``senselog`` drop still comes from the publisher, so a degraded bus is
    always exactly one greppable line.
    """
    factory = _import_events_client()
    if factory is None:
        return None
    url = broker_url()
    try:
        return factory(url)
    except Exception as err:  # a broken client must not block boot
        logger.warning(
            "behavior: events-cli client construction failed for %s (%s: %s); "
            "the nervous-system bus is disabled for this run",
            url,
            type(err).__name__,
            err,
        )
        return None


def _make_nervous_publisher() -> NervousPublisher:
    """Build + start the nervous-system publisher.

    Composed UNCONDITIONALLY — no flag, no env gate on whether to compose (only
    on WHERE to publish). That is load-bearing rather than tidy: the deployed
    ``reachy-runtime.service`` ``ExecStart`` carries no ``--export``, so a leg
    gated behind a flag would never run on the robot at all.

    ``start()`` is total (it configures the Last Will, connects, and reports one
    named drop for an absent/incompatible/unreachable broker), so there is
    nothing to guard here. Whether a :class:`~reachy.export.runtime.
    SenseSnapshotDriver` is worth a tick is then read off
    :attr:`~reachy.export.mqtt.NervousPublisher.publishing_enabled` — which
    answers "could this ever publish again?" rather than merely "is a client
    object present", so a client that exists but was disabled at ``start()``
    (connect raised, or an incompatible shape) correctly stops costing ticks.
    """
    publisher = NervousPublisher(_make_events_client())
    publisher.start()
    return publisher


def _make_media_client() -> HeldMediaClient:
    """Build the ONE held media client (mic + camera) — a test-injection seam.

    The media-side sibling of :func:`_make_state_reader`, with the same
    inline-connect discipline and for a stronger reason: the full media profile
    warms SLOWER than the ``no_media`` pose handle, so an inline connect on the
    tick thread would add a second, larger stall on top of the one t27/t28 exist
    to remove.

    This is the runtime's single media owner (the single-SDK-owner model in
    ``CLAUDE.md``): the rms, transcript, face and frame-available senses all read
    through THIS object, never their own client. It degrades to permanently-quiet
    on a bare box (no ``[sdk]`` extra), so it is composed unconditionally.
    """
    return HeldMediaClient(allow_inline_connect=False)


def _make_audio_tee(samplerate_provider: Callable[[], object]) -> AudioTee:
    """Build the mic fan-out socket — a test-injection seam.

    The sibling of :func:`_make_media_client` / :func:`_make_realtime_client`,
    and composed with the same discipline: every knob (the path, the kill
    switch) resolves inside :class:`~reachy.behavior.audio_tee.AudioTee` from the
    environment, so this stays a bare constructor call.

    Composed UNCONDITIONALLY and with no flag, for the reason the nervous-system
    publisher is: the deployed ``reachy-runtime.service`` ExecStart carries no
    flags, so a leg gated behind one would never run on the robot.
    ``REACHY_AUDIO_TEE=0`` is the kill switch, and every failure — an unusable
    path, a socket somebody else is serving — leaves an inert tee and one named
    drop, never an exception.

    *samplerate_provider* is the mic's REAL rate, peeked per accepted consumer.
    It is passed as a CALLABLE rather than a value because composition runs
    before (and outlives) any warm-up: a rate read once here could be the cold
    holder's ``None`` forever, and an external hearer that guesses the rate
    mis-times every VAD decision it makes.
    """
    return AudioTee(samplerate_provider=samplerate_provider)


def _make_clip_rider(main_control) -> ClipRider:
    """Build the rolling-clip rider — a test-injection seam.

    The sibling of :func:`_make_audio_tee`, composed with the same discipline:
    every knob (X, the encoder) resolves inside :mod:`reachy.behavior.
    clip_rider` from the environment or a probe, so this stays a thin
    constructor call. Composed UNCONDITIONALLY, like every other sense piece —
    a cv2-less box (no ``[vision]`` extra) gets a permanently-quiet rider
    reporting a named reason, never a disabled composition step.

    *main_control* is the SAME spool :func:`_compose_run_seam`'s other state
    riders (``SenseAvailabilityDriver``, ``IntentDriver``) receive, so the
    ``clip`` key rides the identical ``state_writer``-wrapped write those use —
    no second publish path.
    """
    return ClipRider(
        encoder=build_clip_encoder(),
        clip_seconds=clip_seconds_from_env(),
        main_control=main_control,
    )


def _warm_holder(holder, *, label: str) -> bool:
    """Construct *holder*'s client NOW, on the caller's (setup) thread.

    Returns whether a live client is held. A ``False`` is a NORMAL outcome, not a
    fault: the daemon may simply not be up yet (systemd orders the daemon unit
    before a presence unit but does not wait for its readiness), so it is logged
    as a named drop and left to :class:`_HolderKeeper` to retry.

    The two failure modes are deliberately NOT treated alike. A holder that
    *fails* to warm degrades quietly (above); a holder with no ``warm_up`` at all
    is a WIRING bug — no runtime condition can produce one — so the attribute
    lookup sits outside the guard and its ``AttributeError`` propagates. A
    swallowed one would silently skip the warm-up on a holder built with
    ``allow_inline_connect=False``, which is precisely the dead-sense/stalled-tick
    pair this function exists to prevent.
    """
    warm_up = holder.warm_up
    try:
        warmed = bool(warm_up())
    except Exception as err:  # a warm-up fault must not block boot
        logger.warning("behavior: %s holder warm-up raised (%s); sense degraded", label, err)
        senselog.drop(_WARM_STAGE, label, "setup", f"warm-up raised ({err}); keeper will retry")
        return False
    if warmed:
        senselog.stage(_WARM_STAGE, label, "setup", "warmed before the first tick")
    else:
        # Never a fault — see the docstring. Named, so it is never a silent no-op.
        senselog.drop(
            _WARM_STAGE, label, "setup", "no client yet (daemon not up?); keeper will retry"
        )
    return warmed


class _HolderKeeper:
    """Re-warm a dropped held client OFF the tick thread, for the run's lifetime.

    The explicit answer to "what happens when a warm-up fails, or a live client
    drops mid-run?". With ``allow_inline_connect=False`` a read can never rebuild
    the client, so without this the first failure would mean a DEAD sense for the
    rest of the run — on a boot-persistent presence unit that starts alongside
    the daemon, that is the common case, not the rare one, and it would leave a
    rebooted robot deaf and blind until a human restarted it.

    The policy is therefore: poll each holder's free :attr:`connected` predicate
    on a background daemon thread and call ``warm_up()`` only when it reads
    ``False``. Two properties make that safe against the holders' documented
    "not thread-safe" note:

    * a DISCONNECTED holder is inert on the tick thread — with the inline door
      closed, a read only observes ``_client is None`` and returns "no reading";
      it mutates nothing — so the only thread mutating the holder is this one;
    * ``warm_up`` is idempotent, never raises, and its own retry backoff
      throttles the real reconnect attempts, so a fast poll cannot storm.

    Every probe is guarded: a raising holder is logged and polled again, never
    fatal to the keeper.
    """

    def __init__(
        self,
        holders,
        *,
        period: float = HOLDER_KEEPER_PERIOD_S,
        join_timeout: float = _KEEPER_JOIN_TIMEOUT_S,
    ) -> None:
        self._holders = [(label, holder) for label, holder in holders if holder is not None]
        self._period = max(0.0, float(period))
        self._join_timeout = max(0.0, float(join_timeout))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Spawn the keeper thread. A no-op when there is nothing to keep."""
        if self._thread is not None or not self._holders:
            return
        self._thread = threading.Thread(
            target=self._loop, name="behavior-holder-keeper", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the keeper with a bounded join. Idempotent."""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._join_timeout)

    def poll_once(self) -> None:
        """One sweep: re-warm every holder that is not currently connected."""
        for label, holder in self._holders:
            if self._stop.is_set():
                return
            try:
                if holder.connected:
                    continue
            except Exception as err:  # a raising probe is not a verdict
                logger.debug("behavior: %s liveness probe raised (%s)", label, err)
                continue
            if _warm_holder(holder, label=label):
                senselog.stage(_WARM_STAGE, label, "keeper", "re-warmed after a drop")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # the keeper must outlive any single sweep
                logger.warning("behavior: holder keeper sweep raised; continuing", exc_info=True)
            self._stop.wait(self._period)


class _AudioTap:
    """ONE pump take per tick, fanned out to every audio consumer.

    ``AudioPump.take()`` is a CONSUMING swap of the pump's pending buffer:
    calling it twice in a tick would hand each consumer half the audio. THREE
    consumers need it — the ``rms`` provider (read at the START of the tick,
    when perception is composed), the transcript driver (which runs at the END
    of the tick) and the :class:`~reachy.behavior.audio_tee.AudioTee` (offered
    the chunk right after the swap) — so composition, not any of those modules,
    owns the fan-out. This is the ``SenseSample`` pattern the retired folded
    ``listen`` loop used, restated for the engine's tick seam.

    Since #100 the tap performs NO audio I/O of its own: acquisition lives on
    the background :class:`~reachy.behavior.audio_pump.AudioPump` (the SDK's
    appsink is a ``drop=True, max-buffers=500`` FIFO whose ``get_sample``
    blocks up to 20 ms when empty — read at tick rate it serves seconds-stale
    audio, and the block lands on the 20 ms tick budget). :meth:`pull` is a
    latch swap of whatever the pump accumulated since last tick, returned as
    one concatenated chunk.

    :meth:`pull` is called once per tick from the sense reader and is idempotent
    within a tick (guarded on the tick's clock value), so a second perception
    read in the same tick cannot steal a chunk. The two in-process consumers
    then see the identical chunk via :meth:`audio`. Also duck-types
    ``samplerate`` / ``channels`` (off the held media client) so it can be
    injected wherever the media client itself would be for an audio-only
    consumer.

    A PUSH consumer registers through :meth:`add_sink` and is called with the
    chunk the moment it is latched — that is how the third consumer, the
    :class:`~reachy.behavior.audio_tee.AudioTee`, receives audio (spec claim
    c29). Push rather than peek, deliberately: a sink cannot then be wired to
    anything BUT this one take, so the fan-out property is structural instead of
    a call-site convention that a later refactor could quietly re-derive from
    the pump. A sink is called only when there IS audio (never with ``None``, so
    "nothing arrived" is not reported as "audio was discarded") and its faults
    are swallowed here — a fan-out consumer must never break perception.
    """

    def __init__(self, pump, media, sinks=()) -> None:
        self._pump = pump
        self._media = media
        self._chunk = None
        self._pulled_at: float | None = None
        self._sinks = list(sinks)

    def add_sink(self, sink) -> None:
        """Register a push consumer of every latched chunk (see the class note)."""
        self._sinks.append(sink)

    def pull(self, t: float | None = None) -> None:
        """Latch this tick's audio once. Never raises — a fault is "no audio"."""
        if t is not None and t == self._pulled_at:
            return
        self._pulled_at = t
        try:
            self._chunk = self._pump.take()
        except Exception as err:  # a take fault degrades to no audio
            logger.debug("behavior: audio take raised (%s); no audio this tick", err)
            self._chunk = None
        if self._chunk is None:
            return
        for sink in self._sinks:
            try:
                sink(self._chunk)
            except Exception as err:  # a sink must never break the tick
                logger.debug("behavior: audio sink raised (%s); chunk not fanned out", err)

    def audio(self):
        """This tick's mic audio (or ``None``) — a non-consuming PEEK."""
        return self._chunk

    @property
    def samplerate(self):
        return getattr(self._media, "samplerate", None)

    @property
    def channels(self):
        return getattr(self._media, "channels", None)


class _RuntimeResources:
    """Everything :func:`_compose_run_seam` opened that the caller MUST release.

    ``cmd_engine_run`` used to close a single held pose reader; the runtime now
    owns two held SDK clients, three driver-owned worker threads (transcript,
    face, clip) and the speech actuator's worker, so the teardown travels as
    one object rather than a growing tuple. ``close()`` is idempotent, releases
    in reverse dependency
    order (keeper, then the voice, then drivers that READ the holders, then the
    holders themselves), and never raises — a failing teardown step must not
    stop the remaining ones, because an unclosed client hangs the process at
    interpreter exit.

    The speech actuator is released BEFORE the sense drivers on purpose: it is
    the only piece that can still be mid-I/O when the loop ends, and its join is
    bounded, so draining it first keeps the shutdown ordering honest rather than
    racing a half-played clip against a closing media client. The audio pump
    (#100) closes after the drivers and BEFORE the media client it reads, so its
    final guarded read still lands on a live-ish holder rather than a closed one.

    The audio TEE is released with the pump, right after it: they are the two
    ends of one leg (the pump produces, the tee fans out), so stopping
    production first means the tee's final sweep drains what it already holds
    rather than racing new audio. Its ``close()`` also removes the socket file,
    so a runtime that exits leaves behind no path an external consumer could
    connect to and then wait on forever.

    The hearing session client is released just after the drivers that feed it,
    for the same reason the SDK clients are released at all: it owns a socket
    and a worker thread, and an unclosed one leaks the thread and can hang the
    process at interpreter exit. Its ``close()`` is idempotent and bounded (it
    shuts the socket down under a parked worker and joins with a timeout), so a
    session that never connected costs nothing here.

    Two book-ends were added by the nervous-system arc, and both sit OUTSIDE the
    client ordering above because neither depends on it:

    * ``metrics`` is flushed FIRST. :class:`~reachy.behavior.tick_metrics.
      TickMetrics` logs overruns per EPISODE (#121), and at the sustained 77%
      overrun rate measured on the box an episode may never close on its own —
      so without this flush the whole tail of a run would have no count/mean/max
      in the journal at all. It is pure logging, it cannot fail into anything
      else, and doing it first means the summary lands ahead of the teardown's
      own lines.
    * ``publisher`` is stopped LAST. Stopping flips the retained availability
      topic false and closes the session gracefully (no Last Will), so it wants
      to outlive every other step that might still have something to say.
    """

    def __init__(
        self,
        *,
        pose_reader=None,
        media=None,
        drivers=(),
        keeper=None,
        speech=None,
        pump=None,
        tee=None,
        realtime=None,
        metrics=None,
        publisher=None,
    ):
        self.pose_reader = pose_reader
        self.media = media
        self.drivers = tuple(driver for driver in drivers if driver is not None)
        self.keeper = keeper
        self.speech = speech
        self.pump = pump
        self.tee = tee
        self.realtime = realtime
        self.metrics = metrics
        self.publisher = publisher
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.metrics is not None:
            self._release(self.metrics.close, "tick metrics")
        if self.keeper is not None:
            self._release(self.keeper.stop, "holder keeper")
        if self.speech is not None:
            self._release(self.speech.close, "speech actuator")
        for driver in self.drivers:
            self._release(driver.close, f"{type(driver).__name__}")
        if self.realtime is not None:
            self._release(self.realtime.close, "realtime session")
        if self.pump is not None:
            self._release(self.pump.close, "audio pump")
        if self.tee is not None:
            self._release(self.tee.close, "audio tee")
        if self.media is not None:
            self._release(self.media.close, "media client")
        if self.pose_reader is not None:
            self._release(self.pose_reader.close, "pose reader")
        if self.publisher is not None:
            self._release(self.publisher.stop, "nervous publisher")

    @staticmethod
    def _release(close, what: str) -> None:
        try:
            close()
        except Exception:  # one failing teardown must not skip the rest
            logger.warning("behavior: closing the %s raised; continuing", what, exc_info=True)


def _engagement_classifier():
    """The transcript gate's optional LLM classifier, or ``None``.

    Mirrors the retired ``listen --live --transcribe`` builder so the symbolic runtime
    applies the SAME layered engagement gate the retiring loop applied: name
    fast-path, then one "is this addressed to me, in context?" call, then a
    heuristic fallback. Imported lazily and built defensively — construction does
    no network I/O, and a build fault leaves the gate on the pure heuristic
    rather than disabling hearing.

    ``REACHY_ENGAGE_HEURISTIC`` short-circuits it entirely: the driver would
    ignore an injected classifier anyway, so no classifier is built at all.
    """
    from reachy.behavior.transcript_sense import _env_truthy  # local: stdlib-only helper

    if _env_truthy(os.environ.get("REACHY_ENGAGE_HEURISTIC")):
        return None
    try:
        from reachy.speech.engagement import EngagementClassifier

        # No base_url/model/api_key overrides — llm.complete resolves the one
        # REACHY_OPENAI_* endpoint, the same backend any external cognition uses.
        return EngagementClassifier()
    except Exception:  # a build fault must not disable hearing
        logger.warning(
            "behavior: engagement classifier unavailable; the transcript gate "
            "stays on the heuristic",
            exc_info=True,
        )
        return None


def _attach_nervous_system(drivers: list, runtime_consumer):
    """Wire the publisher onto a driver list; return ``(publisher, consumers)``.

    Shared by both composition paths because they wire it IDENTICALLY, and a
    second copy of this is exactly how the two paths drift apart.

    ``SenseSnapshotDriver`` is the one piece that costs a TICK, so it is the one
    piece gated — on whether anything can actually consume it, never on
    ``--export``. "Can consume" means the publisher could still publish at some
    point in this run (``publishing_enabled``), NOT merely that a client object
    exists: a client disabled at ``start()`` never publishes again, so paying a
    tick to feed it is pure waste. Before the bus existed that condition and "is
    ``--export`` set" were the same question; they no longer are, and using the
    flag would leave the boot runtime (no ``--export``) publishing rules and
    motions but no perception at all — exactly the transcript/face flips an
    external subscriber exists to see. With BOTH a client and ``--export`` there
    is still exactly ONE driver feeding two consumers.
    """
    publisher = _make_nervous_publisher()
    consumers = [runtime_consumer] if runtime_consumer is not None else []
    consumers.append(publisher.as_tick_consumer())
    if runtime_consumer is not None or publisher.publishing_enabled:
        drivers.append(SenseSnapshotDriver())
    return publisher, consumers


def _build_pat_sense_driver(reader) -> "PatSenseDriver":
    """Build the pat sense driver for *reader*, applying only REAL overrides.

    ``detector`` and ``hp_tau`` are passed ONLY when actually overridden.
    Occupying a keyword with its own default looks like a no-op but is not: a
    caller injecting its own detector or high-pass (every pet-runtime
    integration test does) would collide on it. Env overrides must be additive —
    absent env leaves the driver's own defaults alone.

    The *reader* is constructed by the caller, not here: it is a held SDK client,
    and the caller's failure path can only release what it already holds.
    """
    still_hold_s, still_eps = _pat_still_tuning()
    pat_kwargs: dict = {
        "still_hold_s": still_hold_s,  # REACHY_PAT_STILL_HOLD_S override (t2)
        "still_eps": still_eps,  # REACHY_PAT_STILL_EPS override (t2)
    }
    override = _pat_detector()
    if override is not None:
        pat_kwargs["detector"] = override  # REACHY_PAT_*_PRESS_DEG overrides
    hp_tau = _pat_float_override(_HP_TAU_ENV, DEFAULT_HP_TAU)  # frequency gate
    if hp_tau is not None:
        pat_kwargs["hp_tau"] = hp_tau
    release_after = _pat_float_override(_RELEASE_AFTER_ENV, RELEASE_AFTER_S)
    if release_after is not None:
        pat_kwargs["release_after_s"] = release_after
    return PatSenseDriver(reader=reader.read, **pat_kwargs)  # default detector (#79)


def _compose_probe_seam(probe, config: EngineConfig, doa_poller, runtime_consumer):
    """The ``--probe-mode`` composition: observation-only, its own early return.

    Deliberately separate from the main seam rather than a branch inside it: it
    omits rules, ordinary pat classification, intents, goto and the pose holder,
    so almost nothing is shared beyond the pose reader and the bus. Keeping it
    here means the main seam reads as one linear composition instead of one
    wrapped in a large alternative.

    The pose reader is still warmed at setup — the probe ticks at the same 50 Hz
    and would take the same startup overrun otherwise.
    """
    reader = _make_state_reader()
    keeper = publisher = None
    try:  # release the held client if composition fails — see the main path's note
        _warm_holder(reader, label="state")
        shared_reader = SharedPoseReader(reader.read)
        mode, probe_emit = probe
        providers = SenseProviders()

        def probe_sense_reader(t):
            return read_perception(providers, base=doa_poller(t))

        drivers = [
            ProbeNamespaceGuard(control.CommandSpool(namespace=INTENT_NAMESPACE)),
            ProbeDriver(mode, shared_reader, emit=probe_emit),
        ]
        # The nervous system rides the probe path too: `--probe-mode` is still an
        # engine run, and "unconditional, no flag" admits no exception. It stays
        # observation-only — the publisher reads the bus, it never drives anything.
        publisher, consumers = _attach_nervous_system(drivers, runtime_consumer)
        bus = TickBus(drivers=drivers, consumers=consumers)
        keeper = _HolderKeeper([("state", reader)])
        keeper.start()
        metrics = TickMetrics(bus, budget_s=budget_from_hz(config.compose_hz))
    except BaseException:
        _RuntimeResources(pose_reader=reader, keeper=keeper, publisher=publisher).close()
        raise
    return (
        probe_sense_reader,
        metrics,
        _RuntimeResources(pose_reader=reader, keeper=keeper, metrics=metrics, publisher=publisher),
    )


def _compose_run_seam(
    transport,
    config: EngineConfig,
    rules_driver,
    runtime_consumer,
    probe=None,
    main_control=None,
):
    """Build ``behavior engine run``'s sense reader + tick seam + owned resources.

    Composes runtime sense/act pieces onto the engine's ONE per-tick seam and
    returns ``(sense_reader, tick_seam, resources)``. Everything rides ONE
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
    ``read_perception(providers, base=doa_poller(t))``: the
    :class:`~reachy.behavior.sense.DoaPoller` supplies the throttled DoA/speech
    leg (its own low-rate polling + failure-swallowing preserved), and every
    other field is a non-consuming PEEK of a driver's one-tick latch or held
    condition. A mic-less box reads EMPTY_SENSE for the DoA leg exactly as
    before. Eight providers are wired, from five producers:

    * ``pat_event`` / ``pat_state`` — two PEEKs of the ONE
      :class:`PatSenseDriver`, so both views describe one held reader and
      detector;
    * ``rms`` / ``rms_ratio`` — two PEEKs of the ONE
      :class:`~reachy.behavior.rms_sense.RmsSense` over this tick's shared mic
      chunk. ``rms`` is the raw loudness, gated by the self-motion latch below
      (#95): while the engine commands motion and the measured rms sits under
      the moving floor (:func:`_rms_moving_floor`), the reading reports quiet
      (0.0) so the robot's own actuator noise can never re-admit
      ``look-toward-sound``. ``rms_ratio`` is that same reading over a rolling
      median of the room's own background (:func:`_rms_background`, #102) — the
      field sound admission actually keys on, because the measured mic
      background drifts ~25x within a day and no absolute floor is right in
      both the daytime and the night room. The self-motion latch does double
      duty: it also EXCLUDES the sample from the estimate, so self-noise is
      neither heard nor learned as the room;
    * ``transcript`` — the :class:`TranscriptSenseDriver`'s one-tick latch of an
      ADDRESSED utterance (endpointing + transcription happen on the lobes
      ``/v1/realtime`` session, and the engagement gate on the driver's own
      worker thread — never here);
    * ``face`` / ``frame_available`` — the :class:`FaceSenseDriver`'s one-tick
      name latch and TTL-held camera condition (detection likewise runs on that
      driver's worker);
    * ``self_moving`` — the :class:`SelfMotionDriver`'s held latch (the same
      peek the rms gate consults), so a rule can key on "am I commanding
      motion" directly.

    Wiring these is what makes ``rms``/``rms_ratio``/``face``/``frame_available``/
    ``transcript``/``self_moving`` FED fields: before this, each was a
    schema-valid ``rules.toml`` predicate that validated cleanly and then
    silently never fired. The declared truth ``behavior rules check`` lints
    against (:data:`reachy.behavior.sense.FED_SENSE_FIELDS`) moves in lockstep
    with this function — see that module's contract note.

    Audio rides a background pump; the tick side is a latch (#100)
    --------------------------------------------------------------
    ALL mic acquisition lives on ONE :class:`~reachy.behavior.audio_pump.
    AudioPump` — a background thread draining ``media.audio()`` at production
    pace. The SDK's audio appsink is a ``drop=True, max-buffers=500`` FIFO
    whose ``get_sample`` blocks up to 20 ms when empty: pulled once per tick
    (the pre-#100 shape) it serves SECONDS-stale audio (live-verified: rule
    fires the instant the #95 gate closed, STT transcribing the past) and puts
    audio I/O on the 20 ms tick budget (#97's residual). The pump discards any
    standing backlog before going live and is started HERE, after the media
    warm-up, so its drain measures a real queue rather than a cold holder.

    ``rms``, the transcript driver and the audio tee are THREE consumers of ONE
    consuming read — :meth:`AudioPump.take`, an O(1) latch swap — so
    :class:`_AudioTap` takes it ONCE at the top of the tick (in
    ``sense_reader``) and all three read that concatenated chunk. No module
    opens an audio source of its own; each takes an injected one, and this is
    the only place that decides there is exactly one.

    The mic leaves the process through the tee, not a second session
    ---------------------------------------------------------------
    :class:`~reachy.behavior.audio_tee.AudioTee` is the third consumer, and the
    only one outside this process: it writes the same per-tick chunk to a local
    unix socket under the state dir so the embodiment layer can hear the room
    without opening a media session it could never win (the single-SDK-owner
    model). It is an ADDITIVE export leg — it feeds nothing back, decides
    nothing, and with no consumer attached it costs the tick a flag store. A
    wedged consumer loses the OLDEST audio through a bounded queue with a named
    drop; it can never backpressure the 20 ms tick.

    Hearing rides ONE session, opened HERE (issue #115)
    ---------------------------------------------------
    The transcript sense no longer endpoints utterances locally: the runtime
    holds ONE :class:`~reachy.speech.realtime.RealtimeTranscriber` session to the
    lobes ``/v1/realtime`` route, streams every mic chunk into it, and takes back
    already-endpointed utterances decided by the server's ``server_vad``. That
    client is constructed and ``start()``-ed HERE, after the media warm-up and
    before the first tick, for the reasons spelled out at the call site: only a
    warmed holder can report the mic's REAL rate (which rides the session's
    connect URL — see :func:`_mic_sample_rate`), and spawning a worker plus a
    blocking first connect is setup work, never tick work.

    The audio it streams is the SAME per-tick chunk the rms providers see: the
    driver reads the injected :class:`_AudioTap`, exactly as before, and hands
    that chunk to the session's O(1) ``submit_audio``. Nothing here opens a
    second ``media.audio()`` reader — the pump is still the only one (#100).

    Held clients: warmed HERE, before the first tick
    ------------------------------------------------
    Both holders — the ``no_media`` pose reader and the media client — are
    constructed with ``allow_inline_connect=False`` and warmed synchronously
    during composition, which runs before ``engine_run`` ticks anything. That
    ordering IS the fix for the reproducible 425-1213 ms startup tick overruns
    (21x-61x over a 20 ms budget, measured on every runtime start —
    ``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 3): the
    blocking connect is charged to setup, which has no tick budget. Warming them
    on a background thread *after* ticking begins would merely relocate the
    stall, and warming without the flag would leave a mid-run fault free to
    reconstruct inline and reproduce it later in the run. A failed warm is normal
    (the daemon may still be starting); :class:`_HolderKeeper` re-warms off-thread
    for the life of the run.

    Degrade contract (no ``[sdk]``/``[vision]`` extra)
    --------------------------------------------------
    The whole sense stack is composed UNCONDITIONALLY: every piece is import-safe
    without ``reachy_mini`` and ``cv2``, and each holder degrades internally to
    permanently-quiet when its extra is absent (one logged warning, then no
    reading). So on a bare box the engine behaves exactly as before EXCEPT for a
    few inert drivers — the pat driver reads ``None`` every tick, the mic chunk is
    always ``None`` (no rms, no utterances, no STT request ever made), the camera
    condition stays ``False`` and :func:`build_face_recognition` returns ``None``
    so the face driver starts no worker — i.e. DoA-only sense, no exceptions. The
    goto path needs no SDK, so it still works.

    Act-in seams (the ONE TickBus, in driver order)
    -----------------------------------------------
    ``[rules_driver, intent_driver, pat_driver, transcript_driver, face_driver,
    self_motion, holder, goto_lane, availability, clip_rider]`` (with a
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
    * ``transcript_driver`` / ``face_driver`` / ``self_motion`` — the other
      latching sense drivers, grouped with the pat driver for the same reason:
      each clears/updates its latch, does bounded O(1) tick-thread work, and
      latches for the NEXT tick's sense read (``self_motion`` mirrors the pat
      driver's end-of-tick cadence exactly: it deltas THIS tick's streamed
      ``ctx.pose``, so the rms provider's read at the start of the next tick
      consults a latch that already reflects this tick's command). None mutates
      engine state, so their order among themselves is immaterial; they are
      ordered after the pat driver by convention, and before the pose holder so
      the act-in half of the seam stays contiguous.
    * ``holder`` BEFORE ``goto_lane`` — the holder stashes this tick's streamed
      ``ctx.pose``; the goto lane's ``start_pose_provider`` peeks that stash when
      it admits a goto. Running the holder first means a goto admitted THIS tick
      (from a command the intent driver just drained) seeds its minjerk start from
      this tick's freshest pose instead of last tick's stale one.
    * ``availability`` — the per-sense structural availability block (#120b),
      merged additively into the SAME ``state.json`` the engine's heartbeat
      writes. Placed last among the always-on riders because it is a WRITER: the
      engine publishes its own snapshot BEFORE the seam runs, so a rider that
      augments the file wants to be the tick's final word. It reads nothing off
      ``ctx``, so this is ordering hygiene rather than a correctness constraint.
    * ``clip_rider`` — the rolling-clip reference (spec claim c18), the same
      kind of WRITER as ``availability`` and ordered beside it for the same
      reason. It reads nothing off ``ctx`` either — its OWN frame feed arrives
      by PUSH from ``face_driver.add_frame_sink(clip_rider.offer)``, wired
      right after ``face_driver`` is constructed above, never through this
      tick seam. All the encoding work happens on its own background worker
      (:mod:`reachy.behavior.clip_rider`'s module docstring); this driver
      entry only republishes ``state.json``'s ``clip`` key on change.

    Both state RIDERS — ``availability`` (``senses``) and ``intent_driver``
    (``intents``) — take *main_control* as their state spool, and so does
    ``clip_rider`` (``clip``). When
    ``cmd_engine_run`` passes the engine's own ``CommandSpool``, and that spool's
    ``write_state`` is later wrapped with the nervous-system ``state_writer``,
    the riders' merged writes reach the retained bus tree as well as disk (t14,
    closing the h21/c36 mirror gap). ``None`` (the default) leaves each rider to
    build its own spool — the pre-t14 shape used by the composition unit tests.
    The probe path below composes NEITHER rider, so it never mirrors these keys.
    * ``SenseSnapshotDriver`` last (export only) — publishes the tick's perception
      snapshot on change; it reads the fixed ``ctx.sense`` so its position is
      immaterial, appended last so the sense block trails the decisions.

    The returned ``resources`` bundles every held client, worker-owning driver
    and session the caller MUST ``close()`` at shutdown (an unclosed client hangs
    the process at interpreter exit, whatever its profile — see
    :mod:`reachy.robot.state_reader`; the hearing session leaks its worker thread
    the same way).
    """
    doa_poller = DoaPoller(lambda: read_doa(transport))

    if probe is not None:
        return _compose_probe_seam(probe, config, doa_poller, runtime_consumer)

    # Everything below OPENS resources (two held SDK clients, two worker-owning
    # drivers, the keeper thread, the audio pump thread, the tee's socket +
    # thread). A raise part-way through would strand them: `cmd_engine_run` only
    # closes what it was RETURNED, and an unclosed client hangs the process at
    # interpreter exit — turning a clean structured failure into a wedged unit
    # that `Restart=on-failure` never restarts. So the whole construction is
    # guarded and releases what it opened before re-raising.
    reader = media = transcript_driver = face_driver = keeper = speech = pump = None
    realtime = publisher = tee = clip_rider = None
    try:
        # The voice, first: built and STARTED here on the setup thread so no tick
        # ever pays for thread creation, and so a malformed REACHY_VOICE_ENGINE /
        # REACHY_SPEECH_TRANSPORT is a clean startup error. `say()` is O(1) and
        # non-blocking; every slow leg (synthesis, playback) runs on its worker.
        #
        # Its speaker is the runtime's OWN held media client, not a second one
        # (spec claim c16) — but that client is constructed further down, and the
        # speech-first ordering above is deliberate and must not be swapped. So
        # the seam is LATE-BOUND: this closure reads the `media` local at PLAY
        # time, on the speech worker, by which point the holder is warm. A
        # `None` (holder not up, no `[sdk]` extra, or a mid-run drop) means the
        # daemon http route — never a second SDK client, which is the whole
        # point. `getattr` keeps an injected fake holder without the accessor
        # from raising; it simply reads as "no session".
        def _held_media_session():
            return getattr(media, "media_session", None) if media is not None else None

        speech = _make_speech_actuator(media_session_provider=_held_media_session)
        speech.start()
        # The pat sense stack ships ON after the hands-on #80 gate finding: the
        # complete command must hold still before sensing, which removes wander
        # ghosts structurally while allowing a settled reaction owner to keep
        # sensing. REACHY_PAT_SENSE=0 is the explicit sensing rollback.
        pat_driver = None
        if _pat_sense_enabled():
            # The reader is built HERE, before anything else can raise, so the
            # guard below releases a client it is guaranteed to be holding.
            reader = _make_state_reader()
            pat_driver = _build_pat_sense_driver(reader)

        # The ONE media owner, and the senses that read through it (the face
        # driver here; the audio pair — rms and the transcript driver — through
        # the single `_AudioTap` below). Composed unconditionally: every piece
        # degrades to permanently-quiet without its extra. The pump is
        # CONSTRUCTED here but started only after the media warm-up below, so its
        # backlog drain measures a real appsink queue (#100).
        media = _make_media_client()
        pump = AudioPump(media)
        audio_tap = _AudioTap(pump, media)
        # ONE background estimator for the whole runtime (#102), feeding the
        # `rms_ratio` field the orienting gate keys on: the measured mic
        # background drifts ~25x within a day, so no absolute floor is right in
        # both the daytime and the night room. It no longer feeds a capture gate
        # — utterance endpointing is the lobes server's `server_vad` now — so
        # this is a single-consumer estimate, built here because that is where
        # the one mic tap lives.
        background = _rms_background()
        recognition = build_face_recognition()
        face_driver = FaceSenseDriver(
            media=media,
            engine=recognition[0] if recognition is not None else None,
            store=recognition[1] if recognition is not None else None,
        )
        # The rolling clip rider (spec claim c18): fed by PUSH off the SAME
        # frame `face_driver` already reads — never a second `media.frame()`
        # call (the single-SDK-owner model). Composed unconditionally; a
        # cv2-less box gets a permanently-quiet rider reporting a named reason
        # in state.json rather than a disabled composition step.
        clip_rider = _make_clip_rider(main_control)
        face_driver.add_frame_sink(clip_rider.offer)

        # Warm BOTH held clients here, on the setup thread, before anything ticks.
        # Sequential and in this order on purpose: the ``no_media`` pose handle is
        # the cheaper of the two to bring up, so the shipped, load-bearing pat sense
        # is live soonest. They are NOT warmed in parallel — two concurrent SDK
        # client constructions is an unverified claim about the SDK, and setup has no
        # tick budget to protect, so there is nothing to buy with the risk.
        if reader is not None:
            _warm_holder(reader, label="state")
        _warm_holder(media, label="media")
        keeper = _HolderKeeper([("state", reader), ("media", media)])
        keeper.start()
        # Start the audio pump strictly AFTER the media warm-up: from here on the
        # pump thread owns every `media.audio()` call, drains the appsink's
        # standing backlog, and the tick thread only ever swaps its latch (#100).
        pump.start()
        # The THIRD consumer of that one latch (spec claim c19): a local unix
        # socket carrying the same mic audio out of this process, so the
        # embodiment layer can hear without opening a second media session it
        # could never win (the single-SDK-owner model). Started here, beside the
        # pump, because binding + spawning a thread is setup work — and because
        # the rate its consumers are told is only real once the holder is warm.
        # A missing consumer, a wedged one or an unusable path each leave an
        # inert tee and one named drop; nothing here can fail the runtime.
        tee = _make_audio_tee(lambda: audio_tap.samplerate)
        tee.start()
        # ...and registered as a SINK on the tap, so it is fed by the same one
        # `take()` the rms providers and the transcript driver read. Not a peek
        # at the call site: a sink cannot be wired to anything but this latch,
        # which is what makes "never a second take" structural. `offer` is O(1)
        # and swallows everything, so this costs the tick a bounded append.
        audio_tap.add_sink(tee.offer)

        # HEARING. Built here — after the media warm-up, before the first tick —
        # for two reasons that are one reason:
        #
        # * the session's `input_sample_rate` rides its connect URL, and a warmed
        #   holder is the only thing that can report the mic's REAL rate; asking
        #   a cold one yields None and the announced 16 kHz guess (which the
        #   driver then corrects with one intentional reconnect);
        # * `start()` spawns the session worker, and thread creation plus the
        #   first blocking connect are exactly the setup-shaped work t27/t28
        #   moved OFF the tick thread after measuring 425-1213 ms overruns
        #   against a 20 ms budget. The worker does the connecting; `start()`
        #   itself returns immediately and is idempotent.
        #
        # A gateway that is not up yet is NORMAL, not a fault: the client latches
        # ONE `session-down` line and reconnects on its own bounded backoff. No
        # second retry layer belongs here, and there is deliberately no local
        # fallback endpointer (confirmed decision c17) — when the session is
        # down, hearing is simply quiet.
        realtime = _make_realtime_client(_mic_sample_rate(audio_tap))
        realtime.start()
        transcript_driver = TranscriptSenseDriver(
            media=audio_tap,  # the shared per-tick chunk, never a second mic read
            # The ONE hearing session. Injected, never constructed by the driver:
            # this composition root owns its lifecycle (start above, close in
            # `_RuntimeResources`), exactly as it owns the two held SDK clients.
            realtime=realtime,
            classifier=_engagement_classifier(),
            # Self-mute: the mic and the speaker share a room, so without this the
            # runtime transcribes its OWN voice, the transcript fires a rule, the
            # rule speaks, and the robot talks to itself forever. The actuator
            # publishes the window its clip occupies; this closes the loop.
            mute_until=speech.mute_until,
        )

        holder = LastPoseHolder()
        # The self-motion latch (#95): composed UNCONDITIONALLY (it reads only
        # ctx.pose — no SDK, no extra), consulted by the rms provider at read
        # time so the robot's own actuator noise reads quiet while it moves.
        self_motion = _make_self_motion()
        # ONE mic read per tick, two sense fields off it (#102): the raw
        # loudness (gated by the moving floor above) and its ratio over the
        # rolling background. The self-motion latch does double duty here — it
        # gates the raw reading AND excludes the sample from the background, so
        # the robot's own noise can neither be heard nor learned as the room.
        rms_sense, rms_provider, rms_ratio_provider = make_rms_providers(
            audio_tap.audio,
            moving=self_motion.is_moving,
            moving_floor=_rms_moving_floor(),
            background=background,  # the ONE estimator, shared with capture above
        )
        providers = SenseProviders(
            pat_event=pat_driver.as_provider() if pat_driver is not None else None,
            pat_state=pat_driver.as_state_provider() if pat_driver is not None else None,
            rms=rms_provider,
            rms_ratio=rms_ratio_provider,
            transcript=transcript_driver.as_provider(),
            face=face_driver.as_face_provider(),
            frame_available=face_driver.as_frame_available_provider(),
            self_moving=self_motion.is_moving,
        )

        def sense_reader(t):
            # Take the tick's ONE audio latch swap first (no mic I/O — the pump
            # owns that, #100), so the rms sense below and the transcript
            # driver later in this same tick share the identical chunk.
            # That one swap also pushes the chunk to the tap's sinks — today the
            # audio tee, which carries it out of the process. Never a second
            # `pump.take()`: a second consuming swap would hand each consumer
            # half the audio, which to a server-side VAD reads as an endpoint.
            audio_tap.pull(t)
            # Then the tick's ONE loudness read, off that chunk. Both rms
            # providers are latch peeks; pulling here (and only here) is what
            # keeps `rms` and `rms_ratio` two views of one measurement and the
            # background estimator fed exactly once per tick.
            rms_sense.pull(t)
            # DoA (throttled by the poller) as the base; every other field is a
            # non-consuming peek of a driver's latch or held condition.
            return read_perception(providers, base=doa_poller(t))

        # Give the rules layer its voice. The driver is built before this function
        # runs (`_boot_tick_seam`, so a malformed rules file is reported before
        # anything is opened), and it remembers the seam across a live reload — a
        # rebuilt engine that silently stopped talking would be a nasty bug.
        if rules_driver is not None:
            rules_driver.set_speech(speech.say)

        goto_lane = GotoLane(start_pose_provider=holder.as_start_pose_provider())
        # Both state RIDERS below take the engine's OWN `main_control` spool
        # (injected by `cmd_engine_run` — `None` here defaults each to its own,
        # the pre-t14 shape used by unit tests). This is what closes the h21/c36
        # gap: the engine wraps THIS spool's `write_state` with the nervous-system
        # `state_writer` AFTER composition, so a rider holding the same instance
        # mirrors its merged `intents`/`senses` write onto the retained bus tree,
        # not just onto disk. The riders look up `self._main.write_state` at TICK
        # time, so the later patch is picked up regardless of construction order
        # (pinned by `test_riders_pick_up_a_state_writer_patched_after_...`).
        intent_driver = IntentDriver(
            mode_setter=rules_driver.set_active_mode if rules_driver is not None else None,
            known_modes=rules_driver.known_modes if rules_driver is not None else None,
            main_control=main_control,
            # The enroll seam (issue #166): a spoken name binds to the face
            # sense's most recent unknown face. A vision-less box composes
            # None and the kind answers with the vision-unavailable refusal.
            enroll_face=face_driver.enroll_current if face_driver is not None else None,
        )
        # Register the GOTO kind into the intent driver's OWN registry (which already
        # carries the four intent defaults) so all five kinds share one registry.
        intent_driver.registry.register(GOTO, make_goto_handler(goto_lane))

        # Per-sense availability into the standing `state.json` (#120b). A seam
        # RIDER, not an `Engine.state()` key: which providers got wired, and
        # whether each one's extra is installed, is composition-time knowledge
        # the engine has no access to. It rides last so it is the tick's final
        # writer, exactly as `engine.py`'s pre-seam state write invites.
        availability = SenseAvailabilityDriver(
            runtime_probes(
                pat_composed=pat_driver is not None,
                face_recognizer_ready=recognition is not None,
            ),
            main_control=main_control,
        )
        drivers = [
            d
            for d in (
                rules_driver,
                intent_driver,
                pat_driver,
                transcript_driver,
                face_driver,
                self_motion,
                holder,
                goto_lane,
                availability,
                clip_rider,
            )
            if d is not None
        ]
        # THE NERVOUS SYSTEM (the runtime feed on an event bus).
        #
        # Built and started HERE, unconditionally and with no flag: the deployed
        # `reachy-runtime.service` ExecStart carries no `--export`, so a leg
        # gated behind a flag would never run on the robot. Configuration is
        # REACHY_MQTT_URL and nothing else. A missing events-cli package, an
        # incompatible client or an unreachable broker each resolve to ONE named
        # `[SENSE stage=nervous source=mqtt ...] dropped reason=…` line and
        # no-op publishes — the runtime is byte-for-byte unaffected.
        #
        # It is a bus CONSUMER, not a driver: it reads what the drivers already
        # publish through `ctx.emit` and never touches the head, the media
        # session or the clock. That is why it does not appear in the driver
        # list above and why it cannot perturb the tick.
        publisher, consumers = _attach_nervous_system(drivers, runtime_consumer)
        bus = TickBus(drivers=drivers, consumers=consumers)
        metrics = TickMetrics(bus, budget_s=budget_from_hz(config.compose_hz))
        resources = _RuntimeResources(
            pose_reader=reader,
            media=media,
            drivers=(transcript_driver, face_driver, clip_rider),
            keeper=keeper,
            speech=speech,
            pump=pump,
            tee=tee,
            realtime=realtime,
            metrics=metrics,
            publisher=publisher,
        )
    except BaseException:
        _RuntimeResources(
            pose_reader=reader,
            media=media,
            drivers=(transcript_driver, face_driver, clip_rider),
            keeper=keeper,
            speech=speech,
            pump=pump,
            tee=tee,
            realtime=realtime,
            publisher=publisher,
        ).close()
        raise
    return sense_reader, metrics, resources


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
    """Build the one-line 'engine live' banner naming the composed layers.

    The three-way split matters because the shipped layer now carries content
    (t15): a rejected overlay no longer means "no rules at all", it means the
    SHIPPED rules survived and only the operator's edits were lost. Saying
    plain ``+ rules`` there would read as "your file loaded" to the one person
    who most needs to know it did not — so a rejection is named on the banner,
    not left to the ``[SENSE ... event=boot]`` drop line alone.
    """
    rejected = rules_driver is not None and rules_driver.loader.last_error is not None
    if probe_mode is not None:
        rules_note = " (observation-only probe)"
    elif rejected:
        rules_note = " + shipped rules (your overlay was rejected)"
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
    resources = None
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
        sense_reader, tick_seam, resources = _compose_run_seam(
            transport, config, rules_driver, runtime_consumer, probe=probe, main_control=spool
        )
        # Mirror the standing `state.json` payload onto RETAINED bus topics, so
        # a late subscriber immediately sees current state instead of waiting
        # for the next change. Purely ADDITIVE by construction: the wrapper runs
        # the disk write FIRST and unconditionally (see
        # `NervousPublisher.state_writer`), so a dead bus can never cost the
        # runtime its state file — and because both surfaces receive the
        # identical object, the bus is a transport for the ONE builder's truth,
        # never a second source of it. Patched on the spool INSTANCE, which
        # `ProbeCommandGuard` also delegates to at call time.
        #
        # This patch lands AFTER `_compose_run_seam` builds the two state riders
        # (`SenseAvailabilityDriver` -> `senses`, `IntentDriver` -> `intents`),
        # yet both mirror their keys onto the bus all the same: they were handed
        # THIS very `spool` as `main_control` above, and each looks up
        # `self._main.write_state` at TICK time rather than capturing a bound
        # method at construction — so the wrapped writer is picked up regardless
        # of the patch order (t14; pinned by
        # `test_riders_pick_up_a_state_writer_patched_after_their_construction`
        # and the strict-equality `test_the_retained_state_tree_equals_state_json_
        # including_rider_keys`). This is what closes the h21/c36 gap the prior
        # revision only pinned: the retained `reachy/state/*` tree now equals the
        # on-disk `state.json`, `senses` and `intents` included. The probe path
        # composes NO riders, so it still mirrors only the engine snapshot.
        publisher = getattr(resources, "publisher", None)
        if publisher is not None:
            spool.write_state = publisher.state_writer(spool.write_state)

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
        # `resources` owns BOTH held SDK clients plus the worker-owning sense
        # drivers; an unclosed client hangs the process at interpreter exit.
        if resources is not None:
            resources.close()
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
