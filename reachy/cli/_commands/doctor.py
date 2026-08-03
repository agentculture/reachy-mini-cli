"""``reachy-mini-cli doctor`` — check the agent-identity invariants.

Mirrors the two invariants ``steward doctor`` verifies for a mesh agent:

* **prompt-file-present** — the repo declares an agent in ``culture.yaml`` and
  has the matching prompt file on disk;
* **backend-consistency** — the declared ``backend`` matches the prompt file
  (``claude`` → ``CLAUDE.md``, ``acp`` → ``AGENTS.md``, ``gemini`` → ``GEMINI.md``).

Plus a **skills-present** check (the vendored ``.claude/skills/`` kit), a
**sense_extras** check (issue #120a) — whether the ``[vision]`` extra (opencv)
is importable, which is what the behavior runtime's ``face`` /
``frame_available`` senses need — and a **model_pair** check (issue #155,
spec assumption c47). Those last two are this module's cross-cutting concerns:
`doctor` is otherwise identity-only and imports nothing from
``reachy.behavior`` / ``reachy.robot`` / ``reachy.embody``, so each stays a
light probe — ``importlib.util.find_spec`` for one, a read of the process
environment for the other — rather than pulling in the stack it reports on.
Read-only.

**Why sense_extras exists.** A box installed without ``[vision]`` degrades
``face``/``frame_available`` to permanently ``None`` after exactly one latched
boot warning (``reachy/behavior/face_sense.py``) — the camera hardware is
healthy the whole time, but nothing observable says so. That single boot line
was the only evidence anywhere on a deployed robot for weeks (issue #120).
``doctor`` now makes it a standing, queryable diagnostic instead.

**Why model_pair exists.** The embodiment layer's two-tempo split names Gemma
in TWO configurations that live in different places: the lobes realtime
service's own ``OPENAI_MODEL`` decides which model SPEAKS through the duplex
floor, and :data:`ENV_SENSES_MODEL` decides which model answers the layer's
perception questions. Nothing makes them move together, so the voice and the
perception lane can silently run different models — a robot that describes one
scene and talks about another. The BACKGROUND lane
(:data:`ENV_WORKER_MODEL`) is deliberately a different model and is therefore
named but never compared: flagging it would be telling the operator to undo
the architecture.

The check's reach is honestly bounded, and the message says so: it reads THIS
process's environment. The gateway holds its own ``OPENAI_MODEL`` in its own
service environment, which is not readable from here, so an unset value is
reported as "not visible", never as "not configured".

Reports the rubric-shaped contract
``{healthy, checks: [{id, passed, severity, message, remediation}]}`` so the
agent-first rubric's bundle 7 passes. When run from a wheel install (no
``culture.yaml`` alongside the package), it reports a single info check and
exits 0 — there is nothing to diagnose.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from importlib.util import find_spec as _find_spec
from typing import Callable

from reachy.cli._commands.whoami import find_culture_yaml, read_agent_fields
from reachy.cli._output import emit_result

# backend → required prompt file (the backend-consistency mapping).
_PROMPT_FILE = {
    "claude": "CLAUDE.md",
    "acp": "AGENTS.md",
    "gemini": "GEMINI.md",
}

# The two working install forms for the [vision] extra, cited verbatim in the
# sense_extras remediation. `uv tool install` has NO `--extra` flag — it fails
# with `unexpected argument '--extra'` — so extras must live inside the
# requirement spec instead; the natural-looking `--extra vision` silently
# isn't a thing, which is how the deployed box in issue #120 ended up without
# the extra in the first place. Keep both forms; do not "simplify" to one.
_PIP_INSTALL_FORM = "pip install 'reachy-mini-cli[vision]'"
_UV_TOOL_INSTALL_FORM = 'uv tool install --force --editable ".[daemon,vision]"'

# --------------------------------------------------------------------------- #
# model_pair — the three env names, RESTATED rather than imported             #
# --------------------------------------------------------------------------- #
# This module is imported by ``_build_parser()``, and ``reachy.embody.engine``
# — the one real home of the two layer names below — reaches
# ``reachy.speech.llm``. Importing it here would put the LLM client in the
# import path of every ``--help``, which
# ``tests/test_zero_llm_boundary.py::test_building_the_cli_parser_loads_no_
# cognition_module`` pins against by equality. So the names are restated as
# literals and ``tests/test_doctor.py`` pins them equal to the layer's own,
# exactly as ``DEFAULT_PERCEPTION_STALE_AFTER_S`` is pinned to
# ``DEFAULT_CLIP_STALE_AFTER_S`` across the same kind of boundary.

#: The lobes realtime service's own setting: which model SPEAKS through the
#: duplex floor. Not this repo's variable — it lives in the gateway's service
#: environment — which is why an absent value reads as "not visible here".
ENV_VOICE_MODEL = "OPENAI_MODEL"
#: The embodiment layer's perception lane (``reachy.embody.engine``).
ENV_SENSES_MODEL = "REACHY_EMBODY_SENSES_MODEL"
#: The embodiment layer's background lane. Named, never compared.
ENV_WORKER_MODEL = "REACHY_EMBODY_WORKER_MODEL"

#: The layer's ROLE names. lobes' ``resolve_model`` accepts a role, so these
#: are routing aliases the gateway resolves — not served model ids. Comparing
#: one against a served id as a string would warn on the configuration the
#: layer documents as correct, so a role alias is never read as divergence.
MODEL_ROLE_ALIASES: tuple[str, ...] = ("worker", "senses")


def _cv2_available() -> bool:
    """Whether the ``[vision]`` extra (opencv) is importable.

    An import PROBE via :func:`importlib.util.find_spec` — never a real
    ``import cv2`` — so calling this costs nothing even on the default,
    identity-only ``doctor`` path. Mirrors the probe
    ``reachy/behavior/face_sense.py``'s :func:`build_face_recognition` uses.
    A malformed parent package on the path can raise from ``find_spec``
    itself; that is treated the same as "not installed" rather than crashing
    ``doctor``.
    """
    try:
        return _find_spec("cv2") is not None
    except (ImportError, ValueError):
        return False


def _model_pair_check(env: Mapping[str, str]) -> dict[str, object]:
    """The ``model_pair`` check (issue #155, spec assumption c47).

    Divergence is deliberately NARROW: it needs BOTH halves set, neither of
    them a role alias, and the two values different. Every other state passes
    — including the one every deployed box is in today, where the gateway
    holds its own ``OPENAI_MODEL`` and the layer resolves the ``senses`` role.
    That narrowness is what makes this check additive: a box that was healthy
    before this shipped is still healthy after it.
    """
    voice = (env.get(ENV_VOICE_MODEL) or "").strip()
    senses = (env.get(ENV_SENSES_MODEL) or "").strip()
    worker = (env.get(ENV_WORKER_MODEL) or "").strip()

    def _named(value: str, *, absent: str) -> str:
        return repr(value) if value else absent

    comparable = [value for value in (voice, senses) if value and value not in MODEL_ROLE_ALIASES]
    diverged = len(comparable) == 2 and comparable[0] != comparable[1]

    detail = (
        f"{ENV_VOICE_MODEL}={_named(voice, absent='not visible in this environment')} "
        f"(the voice, held by the lobes realtime service); "
        f"{ENV_SENSES_MODEL}={_named(senses, absent='unset -> the senses role')} "
        f"(the layer's perception lane); "
        f"{ENV_WORKER_MODEL}={_named(worker, absent='unset -> the worker role')} "
        f"(the background lane — a different model on purpose, never compared)"
    )
    return {
        "id": "model_pair",
        "passed": not diverged,
        "severity": "warning",
        "message": (
            f"the voice and the perception lane name DIFFERENT models — {detail}"
            if diverged
            else f"voice/perception model pair — {detail}"
        ),
        "remediation": (
            ""
            if not diverged
            else (
                f"point {ENV_VOICE_MODEL} (in the gateway's environment) and "
                f"{ENV_SENSES_MODEL} (in the layer process's environment) at the same "
                f"model, or leave {ENV_SENSES_MODEL} unset and let the gateway resolve "
                f"the 'senses' role; do NOT align {ENV_WORKER_MODEL} — the background "
                "mind is a different model by design"
            )
        ),
    }


def _diagnose(
    *,
    cv2_probe: Callable[[], bool] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Run every check and return the rubric-shaped report.

    ``env`` is the same kind of injection seam as ``cv2_probe`` below, for the
    ``model_pair`` check: it defaults to ``os.environ`` (read, never written)
    so a test can drive a whole configuration without mutating the process
    environment other workers share under ``pytest -n auto``.

    ``cv2_probe`` is an injection seam for the ``sense_extras`` check
    (issue #120a): pass a fake callable in a test instead of simulating a
    missing module by evicting ``sys.modules`` entries, which pollutes
    sibling tests under ``pytest -n auto``. Left ``None`` (the default), the
    module-level :func:`_cv2_available` is looked up **fresh at call time**
    (a plain name reference in the function body, not a bound default
    argument) — so a test may equally ``monkeypatch.setattr`` that module
    attribute and drive the real CLI entry point (``cmd_doctor`` / ``main``)
    end to end, still without touching ``sys.modules``.
    """
    cfg = find_culture_yaml()
    if cfg is None:
        check = {
            "id": "source_checkout",
            "passed": True,
            "severity": "info",
            "message": "no culture.yaml found alongside the package; identity checks skipped",
            "remediation": "",
        }
        return {"healthy": True, "checks": [check]}

    root = cfg.parent
    fields = read_agent_fields()
    backend = fields["backend"]
    checks: list[dict[str, object]] = []

    # 1. backend-consistency: the prompt file for the declared backend exists.
    expected = _PROMPT_FILE.get(backend)
    if expected is None:
        checks.append(
            {
                "id": "backend_consistency",
                "passed": False,
                "severity": "error",
                "message": f"unknown backend '{backend}' in culture.yaml",
                "remediation": f"set backend to one of: {', '.join(sorted(_PROMPT_FILE))}",
            }
        )
    else:
        present = (root / expected).is_file()
        checks.append(
            {
                "id": "prompt_file_present",
                "passed": present,
                "severity": "error",
                "message": (
                    f"backend '{backend}' requires {expected} — "
                    + ("present" if present else "missing")
                ),
                "remediation": "" if present else f"create {expected} at the repo root",
            }
        )

    # 2. skills-present: the vendored skill kit is on disk.
    skills_dir = root / ".claude" / "skills"
    has_skills = skills_dir.is_dir() and any(skills_dir.iterdir())
    checks.append(
        {
            "id": "skills_present",
            "passed": has_skills,
            "severity": "warning",
            "message": (
                ".claude/skills/ vendored" if has_skills else ".claude/skills/ missing or empty"
            ),
            "remediation": (
                "" if has_skills else "vendor the skill kit (see docs/skill-sources.md)"
            ),
        }
    )

    # 3. sense_extras: the [vision] extra (opencv) the face/frame_available
    # senses need (issue #120a — see the module docstring for why this exists).
    probe = cv2_probe if cv2_probe is not None else _cv2_available
    has_cv2 = bool(probe())
    checks.append(
        {
            "id": "sense_extras",
            "passed": has_cv2,
            "severity": "warning",
            "message": (
                "[vision] extra (opencv) installed; face/frame_available senses available"
                if has_cv2
                else "[vision] extra (opencv) missing; face/frame_available senses stay "
                "permanently unavailable (issue #120)"
            ),
            "remediation": (
                ""
                if has_cv2
                else (
                    f"{_PIP_INSTALL_FORM}; on a `uv tool install` deploy, note it has no "
                    f"--extra flag — put extras inside the requirement spec instead: "
                    f"{_UV_TOOL_INSTALL_FORM}"
                )
            ),
        }
    )

    # 4. model_pair: the voice and the perception lane name the same model
    # (issue #155 spec assumption c47 — see the module docstring).
    checks.append(_model_pair_check(env if env is not None else os.environ))

    healthy = all(c["passed"] for c in checks)
    return {"healthy": healthy, "checks": checks}


def cmd_doctor(args: argparse.Namespace) -> int:
    report = _diagnose()
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(report, json_mode=True)
    else:
        status = "healthy" if report["healthy"] else "unhealthy"
        lines = [f"reachy-mini-cli doctor: {status}", ""]
        for check in report["checks"]:
            mark = "ok" if check["passed"] else "FAIL"
            lines.append(f"[{mark}] {check['id']}: {check['message']}")
            if not check["passed"] and check["remediation"]:
                lines.append(f"  hint: {check['remediation']}")
        emit_result("\n".join(lines), json_mode=False)
    return 0 if report["healthy"] else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "doctor",
        help="Check the agent-identity invariants (prompt-file-present, backend-consistency).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_doctor)
