"""The forge dispatch client — hand a goal to a coder model, stage what comes back.

:class:`ForgeClient` dispatches a natural-language goal (plus sensory context, and
optionally an existing skill to improve) to a configurable OpenAI-compatible coder-model
endpoint (``FORGE_BASE_URL`` / ``FORGE_MODEL`` / ``FORGE_API_KEY``), parses the two
fenced files the prompt asks for (``SKILL.md`` + ``executor.py``), writes them under the
staging root and runs them through the AST validator (:mod:`reachy.forge.validator`)
before they are ever eligible for activation.

The whole network round-trip runs on a daemon worker thread — :meth:`dispatch` returns
immediately (returning the already-started thread so tests/callers can ``.join()`` it),
and *every* failure path (unreachable endpoint, timeout, non-200, unparseable reply, a
missing fence, a bad name, a failed stage, a rejecting or raising or unavailable
validator, or even an unexpected internal bug) resolves to a loud ``forge/rejected``
event plus a ``logging.warning`` — never an exception on the caller's thread, and never
a hang.

Cited (cite-don't-import) from ``reachy_nova/skill_forge.py``. Deviations from nova, all
deliberate:

* the split — dispatch/parse here, the AST gate in :mod:`reachy.forge.validator`, the
  disk + event layer in :mod:`reachy.forge.lifecycle` — rather than one module;
* ``FORGE_BASE_URL`` defaults to the lobes gateway (:data:`DEFAULT_FORGE_BASE_URL`)
  rather than being required; an unset OR unreachable endpoint both resolve to the same
  loud ``forge/rejected`` via the transport-failure path (nothing hits the network
  without a listener, and tests inject the transport); and
* the sanctioned ``ctx`` surface is injectable (``allowed_ctx_attrs=``), threaded into
  the default validator — the final surface is a later task's (t13) call.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path

from reachy.forge import lifecycle

logger = logging.getLogger(__name__)

#: Default coder endpoint: the local lobes gateway (its cortex/coder route on :8001).
DEFAULT_FORGE_BASE_URL = "http://localhost:8001/v1"
#: Default model name sent in the chat-completions request (matches nova).
DEFAULT_FORGE_MODEL = "qwen3"
DEFAULT_TIMEOUT = 120.0

PROMPT_TEMPLATE = (
    "You are the skill-forge for Reachy Mini, a physical robot. Given a goal "
    "and sensory context, respond with EXACTLY two fenced code blocks and "
    "nothing else outside them:\n\n"
    "1. A block fenced as ```SKILL.md``` containing YAML frontmatter with a "
    "`name` field (lowercase, hyphenated, e.g. `wave-hello`) and a "
    "`description` field, followed by a short markdown body describing when "
    "to use the skill.\n\n"
    "2. A block fenced as ```executor.py``` containing a single Python "
    "function `def execute(params, ctx):` implementing the skill's "
    "behavior using only the primitives available on `ctx`.\n\n"
    "Output nothing else: no prose before, between, or after the two fenced "
    "blocks."
)

_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")
_DASH_COLLAPSE_RE = re.compile(r"-+")

#: The literal value local OpenAI-compatible servers use to mean "no auth" — the
#: repo-wide convention (:mod:`reachy.speech.llm`); the forge auth resolution below
#: matches it so ``FORGE_API_KEY=EMPTY`` / ``REACHY_OPENAI_API_KEY=EMPTY`` never send a
#: ``Authorization: Bearer EMPTY`` header.
_NO_KEY_SENTINEL = "EMPTY"

#: ``validate(skill_dir) -> (ok, reasons)`` — a 1-arg validator seam.
ValidatorFn = Callable[[Path], "tuple[bool, list[str]]"]
#: ``factory() -> ValidatorFn | None`` — lazily resolves the default validator.
ValidatorFactory = Callable[[], "ValidatorFn | None"]
#: ``transport(url, payload, headers, timeout) -> parsed JSON response dict``.
TransportFn = Callable[[str, dict, "dict[str, str]", float], dict]


def _default_transport(url: str, payload: dict, headers: dict[str, str], timeout: float) -> dict:
    """Real HTTP transport: POST JSON via urllib, return the parsed JSON body.

    Raises on any transport failure (connection error, timeout, non-2xx status,
    malformed JSON) — the caller catches those and turns them into forge/rejected.
    """
    import urllib.request  # local import keeps the module import stdlib-cost-free

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        body = response.read().decode("utf-8")
    return json.loads(body)


def _resolve_forge_api_key() -> str | None:
    """Resolve the forge auth key, honouring the repo-wide "EMPTY" == no-auth
    convention (:mod:`reachy.speech.llm`) for BOTH the dedicated ``FORGE_API_KEY`` and
    the shared ``REACHY_OPENAI_API_KEY`` fallback.

    ``FORGE_API_KEY`` wins when it is set to a real (non-"EMPTY") value — "one gateway,
    one key, unless a dedicated key overrides it" (see the module docstring). A
    ``FORGE_API_KEY`` of exactly ``"EMPTY"`` is not a real override, so resolution falls
    through to ``REACHY_OPENAI_API_KEY``; if that is unset or also ``"EMPTY"``, the
    request goes out with no ``Authorization`` header at all, matching the local
    OpenAI-compatible-server convention instead of literally sending
    ``Bearer EMPTY``.
    """
    for env_name in ("FORGE_API_KEY", "REACHY_OPENAI_API_KEY"):
        value = os.environ.get(env_name)
        if value and value != _NO_KEY_SENTINEL:
            return value
    return None


def _default_validator_factory() -> ValidatorFn | None:
    """Lazy-import the in-package AST validator; return ``None`` if it is unavailable."""
    try:
        from reachy.forge.validator import validate
    except ImportError:  # pragma: no cover - the validator ships in this package
        return None
    return validate


def _build_messages(goal: str, context: dict, improve: str | None) -> list[dict]:
    """Build the OpenAI-compatible chat messages for a dispatch."""
    user_lines = [f"Goal: {goal}"]
    if context:
        user_lines.append("Sensory context:")
        user_lines.append(json.dumps(context, indent=2, default=str))
    if improve:
        user_lines.append("Improve this existing skill (address feedback, keep what works):")
        user_lines.append(improve)
    return [
        {"role": "system", "content": PROMPT_TEMPLATE},
        {"role": "user", "content": "\n\n".join(user_lines)},
    ]


def _iter_fences(content: str):
    """Yield ``(label, body)`` for every fenced code block in *content*.

    A deterministic, linear string scan over three-backtick fences — no regex, so no
    backtracking risk (this replaces a lazy-dot regex on live, untrusted network
    replies: SonarCloud S8786, super-linear worst case on adversarial input). It
    reproduces the fence grammar the previous regex implemented: a fence opens at a
    literal triple-backtick, its label runs up to the first newline or backtick, and
    the body runs up to the next triple-backtick occurrence, with exactly one
    immediately-preceding newline (if present) excluded from the body — matching the
    old pattern's optional trailing ``\\n`` before the close fence.
    """
    pos = 0
    length = len(content)
    while True:
        start = content.find("```", pos)
        if start == -1:
            return
        header_start = start + 3
        idx = header_start
        while idx < length and content[idx] not in "`\n":
            idx += 1
        if idx >= length or content[idx] != "\n":
            # No bare newline closes the label before EOF/backtick — this start
            # position can't open a fence; retry one character to the right.
            pos = start + 1
            continue
        label = content[header_start:idx]
        body_start = idx + 1
        close = content.find("```", body_start)
        if close == -1:
            pos = start + 1
            continue
        body_end = close
        if body_end > body_start and content[body_end - 1] == "\n":
            body_end -= 1
        yield label, content[body_start:body_end]
        pos = close + 3


def _extract_fences(content: str) -> dict[str, str]:
    """Defensively pull the SKILL.md and executor.py fenced blocks out of a reply.

    Matches fences labeled with the filename directly (```SKILL.md``` /
    ```executor.py```). Any fence whose label doesn't look filename-shaped is classified
    by content-sniffing (frontmatter ``---`` for SKILL.md, ``def execute(`` for
    executor.py) so a slightly-off label from the coder model doesn't sink an
    otherwise-good reply.
    """
    found: dict[str, str] = {}
    unlabeled: list[str] = []

    for label, body in _iter_fences(content):
        label_lower = label.strip().lower()
        if lifecycle.SKILL_FILENAME.lower() in label_lower:
            found.setdefault(lifecycle.SKILL_FILENAME, body)
        elif lifecycle.EXECUTOR_FILENAME.lower() in label_lower:
            found.setdefault(lifecycle.EXECUTOR_FILENAME, body)
        else:
            unlabeled.append(body)

    for body in unlabeled:
        stripped = body.strip()
        if lifecycle.SKILL_FILENAME not in found and stripped.startswith("---"):
            found[lifecycle.SKILL_FILENAME] = body
        elif lifecycle.EXECUTOR_FILENAME not in found and "def execute(" in body:
            found[lifecycle.EXECUTOR_FILENAME] = body

    return found


def _extract_and_sanitize_name(skill_md: str) -> str | None:
    """Pull ``name:`` out of a SKILL.md's frontmatter and sanitize to ``[a-z0-9-]``.

    Sanitizing to that charset also closes off path traversal — a name like
    ``../../etc/passwd`` cannot survive with a '/' or '.' in it, so the staged folder
    can never escape the staging root.
    """
    # Plain line scan, not a MULTILINE regex — same S8786 shape the sibling
    # frontmatter regexes in activate.py were flagged for.
    raw = None
    for line in skill_md.splitlines():
        if line.startswith("name:"):
            raw = line[len("name:") :]
            break
    if raw is None:
        return None
    raw = raw.strip().strip("\"'").lower()
    raw = raw.replace("_", "-").replace(" ", "-")
    sanitized = _SANITIZE_RE.sub("", raw)
    sanitized = _DASH_COLLAPSE_RE.sub("-", sanitized).strip("-")
    if not sanitized or sanitized in (".", ".."):
        return None
    return sanitized


class ForgeClient:
    """Dispatch skill-generation goals to a coder model and stage validated results.

    Every transition emits an event through ``publish`` (``forge/staged`` /
    ``forge/activated`` / ``forge/rejected``). The network round-trip runs on a daemon
    worker thread so a slow or unreachable coder rig never blocks the caller.

    Parameters
    ----------
    publish:
        ``publish(event_type, payload)`` — the observability seam.
    validator:
        A 1-arg ``validate(skill_dir) -> (ok, reasons)`` callable. When ``None`` the
        client lazily resolves one via ``validator_factory`` and, if that yields
        ``None``, fails CLOSED (rejects with ``"validator unavailable"``).
    validator_factory:
        Resolves the default validator lazily (default: the in-package AST validator).
        Injected by tests to exercise the validator-unavailable fail-closed path.
    allowed_ctx_attrs:
        The sanctioned ``ctx`` attribute surface, threaded into the DEFAULT validator
        only. Ignored when an explicit ``validator`` is injected.
    staging_root / transport / timeout:
        Injection seams for tests; defaults resolve the state-dir staging root, the real
        urllib transport, and a 120s round-trip timeout.
    """

    def __init__(
        self,
        publish: lifecycle.PublishFn,
        *,
        validator: ValidatorFn | None = None,
        validator_factory: ValidatorFactory | None = None,
        allowed_ctx_attrs: "set[str] | None" = None,
        staging_root: Path | str | None = None,
        transport: TransportFn | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self._publish = publish
        self._validator = validator
        self._validator_factory = validator_factory or _default_validator_factory
        self._allowed_ctx_attrs = allowed_ctx_attrs
        self._staging_root = Path(staging_root) if staging_root is not None else None
        self._transport = transport or _default_transport
        self._timeout = timeout

        # Lazy-resolution cache for the default validator (tried at most once).
        self._lazy_checked = False
        self._lazy_validator: ValidatorFn | None = None

    # -- public API ----------------------------------------------------------

    def dispatch(
        self,
        goal: str,
        context: dict | None = None,
        improve: str | None = None,
    ) -> threading.Thread:
        """Kick off a forge round-trip on a daemon thread; return it (already started)."""
        thread = threading.Thread(
            target=self._run,
            args=(goal, context or {}, improve),
            daemon=True,
            name="forge-dispatch",
        )
        thread.start()
        return thread

    # -- worker thread body --------------------------------------------------

    def _run(self, goal: str, context: dict, improve: str | None) -> None:
        try:
            self._run_inner(goal, context, improve)
        except Exception as err:  # noqa: BLE001 - last-resort safety net
            self._reject(None, [f"internal error: {err}"])

    def _run_inner(self, goal: str, context: dict, improve: str | None) -> None:
        base_url = os.environ.get("FORGE_BASE_URL") or DEFAULT_FORGE_BASE_URL
        model = os.environ.get("FORGE_MODEL") or DEFAULT_FORGE_MODEL
        # One gateway, one key: the coder endpoint shares the lobes gateway
        # with cognition, so the LLM key authenticates forge too unless a
        # dedicated FORGE_API_KEY overrides it. The literal "EMPTY" means no
        # key on either variable (matches reachy.speech.llm's convention).
        api_key = _resolve_forge_api_key()

        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {"model": model, "messages": _build_messages(goal, context, improve)}

        response = self._transport_or_reject(url, payload, headers)
        if response is None:
            return

        content = self._content_or_reject(response)
        if content is None:
            return

        skill_md, executor_py = self._fences_or_reject(content)
        if skill_md is None or executor_py is None:
            return

        name = _extract_and_sanitize_name(skill_md)
        if not name:
            self._reject(None, ["invalid or missing skill name"])
            return

        skill_dir = self._stage_files_or_reject(name, skill_md, executor_py)
        if skill_dir is None:
            return

        self._validate_and_finish(name, skill_dir)

    # -- worker steps (each turns its own failure into a forge/rejected) ------

    def _transport_or_reject(self, url: str, payload: dict, headers: dict[str, str]):
        try:
            return self._transport(url, payload, headers, self._timeout)
        except TimeoutError as err:
            self._reject(None, [f"request timed out: {err}"])
        except Exception as err:  # noqa: BLE001 - any transport failure is a rejection
            self._reject(None, [f"endpoint unreachable: {err}"])
        return None

    def _content_or_reject(self, response: dict):
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            self._reject(None, ["unparseable reply"])
            return None

    def _fences_or_reject(self, content: str):
        fences = _extract_fences(content)
        skill_md = fences.get(lifecycle.SKILL_FILENAME)
        executor_py = fences.get(lifecycle.EXECUTOR_FILENAME)
        if not skill_md or not skill_md.strip():
            self._reject(None, ["missing or empty SKILL.md fence"])
            return None, None
        if not executor_py or not executor_py.strip():
            self._reject(None, ["missing or empty executor.py fence"])
            return None, None
        return skill_md, executor_py

    def _stage_files_or_reject(self, name: str, skill_md: str, executor_py: str):
        try:
            return lifecycle.write_artifacts(
                name, skill_md, executor_py, staging_root=self._staging_root
            )
        except OSError as err:
            self._reject(name, [f"failed to stage: {err}"])
            return None

    def _validate_and_finish(self, name: str, skill_dir: Path) -> None:
        validator = self._resolve_validator()
        if validator is None:
            self._reject(name, ["validator unavailable"], skill_dir)
            return
        try:
            ok, reasons = validator(skill_dir)
        except Exception as err:  # noqa: BLE001 - a buggy validator must not activate anything
            self._reject(name, [f"validator error: {err}"], skill_dir)
            return
        if not ok:
            self._reject(name, reasons or ["validation failed"], skill_dir)
            return
        # staged fires ONLY here — strictly after the gate passed.
        lifecycle.stage(self._publish, name, skill_dir)

    # -- helpers -------------------------------------------------------------

    def _resolve_validator(self) -> ValidatorFn | None:
        if self._validator is not None:
            return self._validator
        if not self._lazy_checked:
            self._lazy_checked = True
            base = self._validator_factory()
            if base is None:
                self._lazy_validator = None
            elif self._allowed_ctx_attrs is not None:
                self._lazy_validator = partial(base, allowed_ctx_attrs=self._allowed_ctx_attrs)
            else:
                self._lazy_validator = base
        return self._lazy_validator

    def _reject(self, name: str | None, reasons: list[str], skill_dir: Path | None = None) -> None:
        lifecycle.reject(self._publish, name, reasons, skill_dir, staging_root=self._staging_root)
