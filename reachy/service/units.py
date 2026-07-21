"""Pure systemd ``--user`` unit-file text generation for the presence stack.

This module renders the unit text for the four units that make the robot a
boot-surviving, self-healing presence: the local ``reachy-mini-daemon`` and
three mutually-exclusive presence loops — idle ``demo-mode``, the folded-live
``listen run --live``, and the AI-agnostic symbolic runtime
(``behavior engine run``). It is **pure**: every function returns a ``str`` and
has no side effects — no ``systemctl``, no file writes, no process launches. The
installer half (writing + enabling these) lives in sibling modules; this one only
*describes* the units so the text is trivially testable field-by-field.

The shape mirrors the hand-authored units this stack replaces (and the existing
:mod:`reachy.demo_service` unit grammar): ``Type=simple``, ``Restart=on-failure``,
``RestartSec=5``, ``After=network-online.target``, ``WantedBy=default.target``,
and an ``ExecStart`` that re-invokes the running interpreter against the
``-m reachy …`` module entry (PATH-independent).

Canonical unit names are exported as module constants
(:data:`DAEMON_UNIT` / :data:`DEMO_UNIT` / :data:`LIVE_UNIT` / :data:`RUNTIME_UNIT`)
— a cross-task contract: anything that installs / enables / orders these units
imports the names from here rather than re-spelling the strings.

The runtime unit (:data:`RUNTIME_UNIT`) is the boot default per decision c19
(issue #70): the deterministic ``behavior engine run`` loop owns presence with
zero external AI services, and an agent attaches externally afterwards (see the
``agent`` noun) — its ``ExecStart`` carries no LLM flag and no
``REACHY_OPENAI_*`` reference, by design, so a box with no reachable model
endpoint still boots to full presence.
"""

from __future__ import annotations

import shutil
import sys

# Resolved at call time, not import time, so a test/install can inject the path
# of the daemon binary inside the [daemon] extra's venv.
DAEMON_BINARY = "reachy-mini-daemon"

# --------------------------------------------------------------------------- #
# Canonical unit names (CROSS-TASK CONTRACT — import these, never re-spell).
# --------------------------------------------------------------------------- #
DAEMON_UNIT = "reachy-daemon.service"
DEMO_UNIT = "reachy-demo-mode.service"
LIVE_UNIT = "reachy-live.service"
RUNTIME_UNIT = "reachy-runtime.service"

# --------------------------------------------------------------------------- #
# Retired unit names (CROSS-TASK CONTRACT — the migration list).
# --------------------------------------------------------------------------- #
#
# Unit names this CLI once installed and no longer does. A name leaving the
# catalog above does NOT make it leave the deployed robot: nothing writes or
# removes unit files on ``pip upgrade``, and every install/enable path only ever
# touches units still IN the catalog. So a retired unit survives the upgrade
# with an ``ExecStart`` naming a subcommand that no longer exists — and because
# every unit here carries ``Restart=on-failure`` + ``RestartSec=5`` (see
# :func:`_render`), that is a 5-second crash loop, not a quiet no-op.
#
# ``ServiceManager.cleanup_retired_units`` walks this tuple on every ordinary
# ``service enable`` / ``install`` / ``uninstall`` and unconditionally
# ``disable --now``s each name, unlinks its unit file, and removes its ``.d/``
# drop-in directory. **Retiring a unit is therefore a one-line change: move the
# name out of the catalog above and into this tuple.** Never list a unit that is
# still a live presence mode — the migration would disable it out from under the
# operator on the next ``service`` command.
#
# ``reachy-listen.service`` is the hand-authored unit the CLI-generated
# :data:`LIVE_UNIT` superseded; an orphaned copy still sits enabled in
# ``~/.config/systemd/user`` on the deployed box, in no catalog and removed by
# nothing. It is the reason this list exists.
RETIRED_UNITS: tuple[str, ...] = ("reachy-listen.service",)

# --------------------------------------------------------------------------- #
# Runtime unit's explicit TTS route (task t7 / issue #70 arc, decision c27).  #
# --------------------------------------------------------------------------- #
#
# The deployed box's ONLY ``REACHY_TTS_ROUTE`` configuration lives in
# ``reachy-live.service.d/tts.conf`` -- a hand-authored drop-in belonging to
# :data:`LIVE_UNIT`, a unit this arc deletes. That drop-in's own comment
# documents WHY it exists: the default route ("chatterbox") POSTs to
# ``REACHY_TTS_URL`` (default ``http://localhost:9000``), but model-gear's
# chatterbox container is EXPOSE-only, never published to the host -- so the
# default route is connection-refused on this box. Combined with
# ``audio_optional=True``-style silent degradation (here,
# :class:`reachy.behavior.speech_act.SpeechActuator`'s failure latch), a
# runtime unit with no route configured would fail a live TTS check in total
# silence, with nothing in the log to say why.
#
# So :data:`RUNTIME_UNIT` sets its route EXPLICITLY, baked into its own unit
# text via an ``Environment=`` directive, rather than inheriting a sibling
# unit's drop-in that is about to disappear. The value routes through the
# lobes gateway's ``/v1/audio/speech`` leg (``REACHY_OPENAI_URL_BASE``,
# already set process-wide via ``environment.d`` -- see ``reachy/speech/tts.py``)
# instead of the broken default port. This is a TTS route, not an LLM call:
# the runtime's DEFAULT voice
# (:data:`reachy.behavior.speech_act.RUNTIME_DEFAULT_VOICE_ENGINE`) is
# ``"harmonic"`` and needs no route at all, so this variable is INERT until an
# operator opts into ``REACHY_VOICE_ENGINE=tts`` -- but when they do, it must
# not silently hit a port that only a retiring sibling unit ever routed around.
RUNTIME_TTS_ROUTE_ENV = "REACHY_TTS_ROUTE"
DEFAULT_RUNTIME_TTS_ROUTE = "openai"


def _unit_arg(value: str) -> str:
    """Quote/escape one ExecStart argument for the systemd unit grammar.

    systemd splits ExecStart on whitespace and treats ``%`` as a specifier, so a
    path with spaces or ``%`` would corrupt the command. Double quotes preserve
    spaces; ``%`` becomes ``%%`` and ``"`` / ``\\`` are backslash-escaped. This
    matches :func:`reachy.demo_service._unit_arg` exactly.
    """
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _default_python() -> str:
    """The interpreter to launch the module entry with — the running one."""
    return sys.executable


def _default_daemon_cmd() -> str:
    """Resolve the daemon binary: PATH lookup, falling back to the bare name.

    Kept pure (no raising): rendering the unit text must never fail just because
    the binary is not on *this* box — the unit is often authored on one machine
    and started on another. The bare name is a valid ``ExecStart`` that systemd
    resolves at start time.
    """
    return shutil.which(DAEMON_BINARY) or DAEMON_BINARY


# --------------------------------------------------------------------------- #
# ExecStart lines.
# --------------------------------------------------------------------------- #


def daemon_exec_start(daemon_cmd: str | None = None) -> str:
    """ExecStart for the daemon unit: run the ``reachy-mini-daemon`` binary."""
    cmd = daemon_cmd or _default_daemon_cmd()
    return _unit_arg(cmd)


def demo_exec_start(python: str | None = None, config_file: str | None = None) -> str:
    """ExecStart for the idle presence unit: ``<python> -m reachy demo-mode run``.

    ``config_file`` is required by the caller in practice (the installer passes a
    concrete path so the unit never points at a missing file); a ``None`` default
    keeps the signature ergonomic for tests.
    """
    py = python or _default_python()
    cfg = config_file or ""
    return f"{_unit_arg(py)} -m reachy demo-mode run --config {_unit_arg(cfg)}"


def live_exec_start(python: str | None = None) -> str:
    """ExecStart for the live presence unit: the folded live loop, agent-cognition by default.

    ``listen run --live --transcribe --cognition agent --voice-engine harmonic`` runs
    the folded live loop (hearing + pat + cognition + vision + sleep in one loop) with STT
    transcription on and cognition driven by the tool-use ``AgentTurnEngine`` (acting
    through ``speak`` / ``harmonics`` / ``apply_pose`` tool calls rather than the
    ``*emoji*``/``"speech"`` marker convention). ``--voice-engine harmonic`` is passed
    too — inert for ``agent`` mode itself (both the ``tts`` and ``harmonic`` voices are
    always registered as tools there), but it is what the ``marker`` engine would use if
    the unit's ``ExecStart`` were ever edited back to ``--cognition marker``. All three
    — ``--transcribe``, ``--cognition agent``, and ``--voice-engine harmonic`` — stay
    off/at their CLI default (``--cognition`` defaults to ``"marker"``,
    ``--voice-engine`` to ``"tts"``) unless explicitly passed; the unit opts in to all
    three so the on-robot presence hears words, reasons through the tool-use agent, and
    has an offline voice available, out of the box. The flags are implemented
    elsewhere — this only renders the string.
    """
    py = python or _default_python()
    return (
        f"{_unit_arg(py)} -m reachy listen run --live --transcribe "
        "--cognition agent --voice-engine harmonic"
    )


def runtime_exec_start(python: str | None = None) -> str:
    """ExecStart for the runtime presence unit: ``<python> -m reachy behavior engine run``.

    This is the AI-agnostic symbolic runtime (decision c19, issue #70): the
    deterministic 50 Hz ``behavior`` engine loads ``rules.toml`` at boot, ticks,
    evaluates its rules, and sustains declared intents entirely on its own — no
    LLM call, no ``REACHY_OPENAI_*`` endpoint, no ``--export``/cognition flag of
    any kind. An AI agent may attach to this running loop afterwards through its
    seams (the ``agent`` noun: the runtime's event feed in, the intent spool
    out) with **no unit edit and no loop restart** — cognition is an external,
    optional client of the runtime, never wired into its ``ExecStart``.
    """
    py = python or _default_python()
    return f"{_unit_arg(py)} -m reachy behavior engine run"


# --------------------------------------------------------------------------- #
# Full unit texts.
# --------------------------------------------------------------------------- #


def _render(
    *,
    description: str,
    exec_start: str,
    requires: str | None = None,
    after_daemon: bool = False,
    environment: dict[str, str] | None = None,
) -> str:
    """Assemble one ``--user`` unit from its parts (shared skeleton).

    All units share ``Type=simple`` + ``Restart=on-failure`` + ``RestartSec=5``
    + ``WantedBy=default.target``. Presence units additionally ``Requires=`` and
    order ``After=`` the daemon unit so the daemon is up first. *environment*
    (optional) renders one ``Environment=KEY=VALUE`` directive per entry ahead
    of ``ExecStart=`` -- baked into the unit's own text rather than requiring a
    separate ``.d/`` drop-in (see :data:`RUNTIME_TTS_ROUTE_ENV`).
    """
    after = "network-online.target"
    if after_daemon:
        # Daemon before network-online so the presence loop only starts once the
        # robot daemon it talks to is already running.
        after = f"{DAEMON_UNIT} network-online.target"
    requires_line = f"Requires={requires}\n" if requires else ""
    environment_lines = "".join(f"Environment={k}={v}\n" for k, v in (environment or {}).items())
    return (
        "[Unit]\n"
        f"Description={description}\n"
        f"{requires_line}"
        f"After={after}\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{environment_lines}"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def daemon_unit_text(daemon_cmd: str | None = None) -> str:
    """Render ``reachy-daemon.service`` — the local robot daemon process."""
    return _render(
        description="Reachy Mini daemon (robot control process)",
        exec_start=daemon_exec_start(daemon_cmd),
    )


def demo_unit_text(python: str | None = None, config_file: str | None = None) -> str:
    """Render ``reachy-demo-mode.service`` — idle feel-alive presence loop."""
    return _render(
        description="Reachy Mini demo-mode (feel-alive idle motion)",
        exec_start=demo_exec_start(python, config_file),
        requires=DAEMON_UNIT,
        after_daemon=True,
    )


def live_unit_text(python: str | None = None) -> str:
    """Render ``reachy-live.service`` — folded live presence loop (listen --live)."""
    return _render(
        description="Reachy Mini live presence (hearing + pat, folded live loop)",
        exec_start=live_exec_start(python),
        requires=DAEMON_UNIT,
        after_daemon=True,
    )


def runtime_unit_text(python: str | None = None, tts_route: str | None = None) -> str:
    """Render ``reachy-runtime.service`` — the AI-agnostic symbolic runtime presence.

    Boot default per c19: the deterministic ``behavior engine run`` loop (rules,
    reflexes, sustained intents) with zero external AI services required; an
    agent attaches externally afterwards, never wired into this unit.

    Sets ``REACHY_TTS_ROUTE`` EXPLICITLY (*tts_route*, defaulting to
    :data:`DEFAULT_RUNTIME_TTS_ROUTE`) as an ``Environment=`` directive baked
    into this unit's own text, rather than depending on
    ``reachy-live.service.d/tts.conf`` — a drop-in that belongs to
    :data:`LIVE_UNIT`, a unit this arc retires (task t7, decision c27). See the
    module-level comment above :data:`RUNTIME_TTS_ROUTE_ENV` for why the
    default route would otherwise be silently connection-refused on the
    deployed box. This is a TTS route, not an LLM endpoint — it stays inert
    under the shipped ``harmonic`` default voice and only matters once an
    operator opts into ``REACHY_VOICE_ENGINE=tts``.
    """
    return _render(
        description="Reachy Mini symbolic runtime (AI-agnostic rules + reflex presence)",
        exec_start=runtime_exec_start(python),
        requires=DAEMON_UNIT,
        after_daemon=True,
        environment={RUNTIME_TTS_ROUTE_ENV: tts_route or DEFAULT_RUNTIME_TTS_ROUTE},
    )
