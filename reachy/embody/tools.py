"""The embodiment layer's direct-operation action set — and its blast radius.

The layer is cognition with an ungated ear: it hears everyone in the room, so
containment cannot depend on *who* is speaking. It depends on what the layer
can reach at all. That is this module. There are exactly five tools, covering
the four action classes the spec names, and **every one of them wraps a surface
that already exists and already validates**:

======================  ===================================================
tool                    the sanctioned surface it wraps
======================  ===================================================
``goto``                :func:`reachy.behavior.goto_intent.make_goto_handler`
                        (per-axis bounds, the 10 s duration cap) + the
                        ``intents`` command spool
                        (:mod:`reachy.behavior.control`)
``run_behavior``        :class:`reachy.behavior.intents.IntentDriver`'s
                        ``run_behavior`` kind (library-name + param checks,
                        the unbounded-lifetime refusal) + the same spool
``speak`` /             the injected say / harmonics seams — the same
``harmonics``           ``synthesize`` + ``play_audio`` pair ``agent
                        attach`` injects — bounded by the ONE shared
                        :data:`reachy.behavior.rules.MAX_SAY_CHARS`
``create_rule``         the rules overlay
                        (:func:`reachy.behavior.rules.overlay_rules_path`,
                        validated by ``RulesConfig.from_dict`` through
                        :func:`~reachy.behavior.rules.load_rules`) + the
                        reload spool
                        (:func:`reachy.behavior.reload_driver.submit_reload`)
======================  ===================================================

Why the layer validates nothing itself
--------------------------------------
The containment story is "the existing fail-closed validators do the work". A
second copy of a bound is a second number to drift, and a bound that lives only
in the layer is a bound an operator using the CLI does not get. So this module
**routes** and **names**; it does not judge. Concretely:

* motion and behavior actions run the SHIPPED kind handler against inert sinks
  (:class:`_InertLane` / :class:`_InertContext`) as a synchronous pre-flight,
  then submit the identical payload to the spool the engine drains — the same
  code refuses in both places. The pre-flight exists because an LLM that gets
  ``{"ok": null, "submitted": ...}`` back concludes it succeeded; a refusal the
  model cannot see is not a refusal.
* rule authoring builds a candidate overlay, hands it to
  :func:`reachy.behavior.rules.load_rules`, and only ``os.replace``\\ s it into
  place if that returns. The validator IS the gate, so a rules file the engine
  would reject is never written.
* the two voice tools re-check
  :data:`reachy.behavior.rules.MAX_SAY_CHARS` — importing the constant, never
  restating the number — exactly as
  :class:`reachy.behavior.speech_act.SpeechActuator` does, "because a rule is
  not its only caller".

The two policies this module *does* own
--------------------------------------
Both are bounds no downstream validator has ever heard of, so delegation is not
available — there is no other owner.

1. **The ``embody-`` rule-id namespace** (spec c26): layer-authored rules
   PERSIST after the layer stops (a confirmed product decision — the robot
   keeps what it was taught), so the prefix is what makes them enumerable and
   removable as a set. There is deliberately no deletion-on-exit hook here.
2. **The ``catalog``** — the behavior name set this registry was built with.
   ``IntentDriver`` and ``load_rules`` both validate names against the GLOBAL
   :data:`reachy.behavior.library.LIBRARY`; neither knows a registry may have
   been constructed with a narrower one. So a restricted catalog was
   **advisory** until it was enforced here: the tool schema advertised the short
   ``enum`` while the handler admitted anything the full library knew. Measured
   with ``catalog={'nod'}``, before the fix — the advertised enum was
   ``['nod']``, and ``run_behavior{'name': 'shake'}`` returned ``ok: true`` and
   ran, while ``create_rule{'run': 'shake'}`` wrote the rule. A schema ``enum``
   is a hint to a model, never an enforcement boundary: a direct ``dispatch``
   call or a malformed tool-call client never sees it.
   :func:`_require_catalog_name` closes both paths, mirroring
   :func:`reachy.speech.intent_tools._require_known_name`, which the sibling
   cognition root has always applied.

Import boundary
---------------
Follows :mod:`reachy.speech.intent_tools` — this module imports that module's
public ``Tool`` / ``function_tool`` shapes from :mod:`reachy.speech.tools` and
the behavior package's spool/rules/validator surfaces, and **nothing else**. It
never imports :mod:`reachy.speech.tts` / :mod:`~reachy.speech.playback` /
:mod:`~reachy.speech.voice` (the audio stack arrives as two injected
callables), never imports :mod:`reachy.daemon` (state-dir resolution arrives
under the spool and the rules loader), never imports ``reachy_mini``, and
contains no ``subprocess``, no ``os.system``, and no ``eval``/``exec``.
``tests/test_embody_redteam.py`` asserts every one of those by AST.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from reachy import senselog
from reachy.behavior import control as control_mod
from reachy.behavior import library as behavior_library
from reachy.behavior import reload_driver
from reachy.behavior import rules as rules_mod
from reachy.behavior.goto_intent import GOTO, MAX_DURATION_S, make_goto_handler
from reachy.behavior.intents import INTENT_NAMESPACE, RUN_BEHAVIOR, IntentDriver
from reachy.behavior.rules import MAX_SAY_CHARS
from reachy.cli._errors import CliError
from reachy.speech.tools import Tool, function_tool

logger = logging.getLogger(__name__)

#: The senselog stage every tool call logs under — the SAME stage
#: :meth:`reachy.speech.tools.ToolRegistry.dispatch` uses, so one grep finds
#: every action either cognition root took.
STAGE = "action"

#: Tool names for the two voice legs. ``goto`` / ``run_behavior`` reuse the
#: spool kind names verbatim (imported above), because the tool name and the
#: command kind must not be two independently drifting strings.
SPEAK = "speak"
HARMONICS = "harmonics"
CREATE_RULE = "create_rule"

#: The layer's ENTIRE action set, in publication order. Pinned by equality in
#: ``tests/test_embody_tools.py`` — a sixth tool is a deliberate widening of the
#: blast radius, never an incidental one.
ACTION_SET: tuple[str, ...] = (GOTO, SPEAK, HARMONICS, RUN_BEHAVIOR, CREATE_RULE)

#: tool -> the dotted in-repo names it wraps. This is the machine-checked half
#: of the 1:1 claim: every name here is imported and looked up by a test, so a
#: tool that quietly grew its own actuation path (or a surface that was renamed
#: out from under one) fails CI. The voice tools name only the shared say cap —
#: their synthesis and playback legs are injected by composition and therefore
#: have no in-repo name to pin here.
SANCTIONED_SURFACES: dict[str, tuple[str, ...]] = {
    GOTO: (
        "reachy.behavior.goto_intent.make_goto_handler",
        "reachy.behavior.control.submit",
    ),
    RUN_BEHAVIOR: (
        "reachy.behavior.intents.IntentDriver",
        "reachy.behavior.control.submit",
    ),
    SPEAK: ("reachy.behavior.rules.MAX_SAY_CHARS",),
    HARMONICS: ("reachy.behavior.rules.MAX_SAY_CHARS",),
    CREATE_RULE: (
        "reachy.behavior.rules.load_rules",
        "reachy.behavior.rules.overlay_rules_path",
        "reachy.behavior.reload_driver.submit_reload",
    ),
}

#: Required prefix on every rule id the layer authors (spec c26). Layer rules
#: outlive the layer, so this is what keeps them enumerable
#: (:func:`list_embody_rules`) and removable as a set.
RULE_ID_PREFIX = "embody-"

#: Sentinels bracketing the block of the overlay the layer owns. Everything
#: outside them is the operator's, preserved BYTE for byte across every write.
MANAGED_BEGIN = "# >>> embody-managed rules (embody-*) - written by the embodiment layer >>>"
MANAGED_END = "# <<< embody-managed rules - end <<<"

#: Seconds a tool call waits for the engine to confirm a spool command before
#: degrading to "submitted, unconfirmed" (mirrors
#: :data:`reachy.speech.intent_tools.DEFAULT_AWAIT_TIMEOUT`).
DEFAULT_AWAIT_TIMEOUT = 1.0

# --------------------------------------------------------------------------- #
# Named refusals — never a silent no-op                                       #
# --------------------------------------------------------------------------- #

#: The requested tool is not in :data:`ACTION_SET`. This is what a shell
#: request resolves to: there is no such tool, and no way to add one.
REFUSAL_UNKNOWN_TOOL = "unknown-tool"
#: The arguments payload did not decode to a JSON object, or a field had the
#: wrong TYPE (the shape check :mod:`reachy.speech.intent_tools` also does).
REFUSAL_BAD_ARGUMENTS = "bad-arguments"
#: :mod:`reachy.behavior.goto_intent` refused the pose (unknown field,
#: out-of-range axis, non-numeric value, runaway duration).
REFUSAL_GOTO = "goto-refused"
#: The ``run_behavior`` kind refused it (unknown behavior, unknown/non-numeric
#: param, or the unbounded ``looping``-with-no-``duration`` lifetime).
REFUSAL_BEHAVIOR = "behavior-refused"
#: The utterance was empty or over :data:`reachy.behavior.rules.MAX_SAY_CHARS`.
REFUSAL_SAY = "say-refused"
#: :meth:`reachy.behavior.rules.RulesConfig.from_dict` refused the authored rule
#: (a field outside the declarative schema, an unbounded lifetime, a say over
#: the cap, an unknown behavior/predicate, ...).
REFUSAL_RULE = "rule-refused"
#: The rule id was missing, malformed, or outside the ``embody-`` namespace.
REFUSAL_RULE_NAMESPACE = "rule-namespace-refused"
#: No voice seam was injected at composition, so the layer has no mouth.
REFUSAL_NO_VOICE = "no-voice-seam"
#: A handler raised something unexpected. Named rather than propagated, so a
#: bad tool call can never kill the turn loop.
REFUSAL_TOOL_ERROR = "tool-error"

#: Every refusal this module can produce. Exported so the export feed, the
#: operator docs and the tests share ONE vocabulary.
REFUSALS: frozenset[str] = frozenset(
    {
        REFUSAL_UNKNOWN_TOOL,
        REFUSAL_BAD_ARGUMENTS,
        REFUSAL_GOTO,
        REFUSAL_BEHAVIOR,
        REFUSAL_SAY,
        REFUSAL_RULE,
        REFUSAL_RULE_NAMESPACE,
        REFUSAL_NO_VOICE,
        REFUSAL_TOOL_ERROR,
    }
)

#: A voice seam: ``speak(text) -> object``. Composition binds it to the same
#: ``synthesize`` + ``play_audio`` pair ``agent attach`` uses; this module never
#: imports either, so it cannot open an audio device on its own.
SoundSeam = Callable[[str], object]
#: A reload seam: ``reload(timeout) -> dict | None`` — submit a rules reload and
#: report what (if anything) the engine said. Defaults to the real spool.
ReloadSeam = Callable[[float], "dict | None"]


class Refusal(Exception):
    """A refusal with a NAME — the one exception type handlers raise.

    ``reason`` is one of :data:`REFUSALS` and is used verbatim as the
    :func:`reachy.senselog.drop` reason and as the ``refusal`` field of the
    tool-result content, so the log line, the export feed and the model all see
    the same word.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


# --------------------------------------------------------------------------- #
# Pre-flight — the SHIPPED kind handlers, run against inert sinks             #
# --------------------------------------------------------------------------- #


class _InertLane:
    """A :class:`~reachy.behavior.goto_lane.GotoLane`-shaped sink that queues nothing.

    ``make_goto_handler`` validates *everything* before it calls ``submit``, so
    handing it a lane that records and returns is a pre-flight over the exact
    shipped validator — not a copy of it.
    """

    def submit(self, _spec: object) -> str:
        # `_spec` is deliberately unused and underscore-named: this stub exists
        # to satisfy the lane Protocol during pre-flight, and the shipped
        # validator has already run by the time it is called.
        return "preflight"


class _InertContext:
    """A ``TickContext``-shaped stub: admits nothing, evicts nothing, emits nothing.

    ``IntentDriver._apply_run_behavior`` validates the name, the params and the
    lifetime and only then calls ``ctx.admit``. Against this stub the admission
    is a no-op, so the call is a pure validation pass.
    """

    now = 0.0
    tick = 0

    def admit(self, behavior: object) -> dict:
        return {"ok": True, "id": getattr(behavior, "id", None)}

    def evict(self, name: str) -> dict:
        return {"ok": True, "name": name}

    def active_names(self) -> tuple[str, ...]:
        return ()

    def emit(self, event: Mapping) -> None:
        """Swallow the event: a pre-flight must never publish."""


def _preflight(op: str, payload: dict) -> None:
    """Run *payload* through the SHIPPED handler for *op*; raise on a refusal.

    Builds the registry the way ``_compose_run_seam`` builds the live one — the
    :class:`~reachy.behavior.intents.IntentDriver`'s own four kinds plus
    ``GOTO`` registered into that same registry — so a payload this accepts is
    one the running engine's registry accepts too (modulo engine STATE, e.g. an
    inhibition, which only the live engine can know and which it reports back
    through the spool result).

    A fresh driver per call on purpose: no shared mutable state, no sequence
    counter to leak between turns, safe from any thread.
    """
    driver = IntentDriver()
    driver.registry.register(GOTO, make_goto_handler(_InertLane()))
    outcome = driver.registry.dispatch({"op": op, **payload}, _InertContext())
    if outcome.get("ok"):
        return
    reason = REFUSAL_GOTO if op == GOTO else REFUSAL_BEHAVIOR
    raise Refusal(reason, str(outcome.get("error") or f"{op}: refused"))


def _submit(op: str, payload: dict, *, spool_root: Path | None, timeout: float) -> dict:
    """Pre-flight, then drop the identical command in the spool and await a result.

    The engine validates AGAIN on drain — that is the point of a spool any
    process may write. This call is the synchronous half, so the model learns of
    a refusal in the same turn it made the request.
    """
    _preflight(op, payload)
    cmd_id = control_mod.submit(op, namespace=INTENT_NAMESPACE, root=spool_root, **payload)
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=spool_root, timeout=timeout
    )
    if result is None:
        return {
            "ok": None,
            "submitted": cmd_id,
            "note": "engine did not confirm in time — is the behavior engine running?",
        }
    return result


# --------------------------------------------------------------------------- #
# Rules overlay authoring — atomic, namespaced, operator-preserving           #
# --------------------------------------------------------------------------- #


def _split_overlay(text: str) -> tuple[str, str, str]:
    """Split overlay *text* into ``(operator_head, managed_block, operator_tail)``.

    The head is everything before :data:`MANAGED_BEGIN` and the tail everything
    after :data:`MANAGED_END` — both returned VERBATIM, which is the whole
    mechanism behind the byte-identity guarantee. A file with no sentinels is
    all head. A file with a BEGIN and no END (a half-written block from a
    crashed process that somehow survived the rename) is treated as head +
    managed, so the next write repairs it rather than nesting a second block.
    """
    if MANAGED_BEGIN not in text:
        return text, "", ""
    head, _, rest = text.partition(MANAGED_BEGIN)
    managed, sentinel, tail = rest.partition(MANAGED_END)
    if not sentinel:
        return head, managed, ""
    return head, managed, tail


def _managed_entries(managed: str) -> list[dict]:
    """The react entries currently inside the managed block, in file order.

    Parsed with :mod:`tomllib` (the same reader
    :mod:`reachy.behavior.rules` uses) because the block is TEXT on disk that an
    operator may have hand-edited. Unparseable content is dropped with a named
    :func:`reachy.senselog.drop` rather than silently — the layer's own rules
    are recoverable, an operator's file is not, and the head/tail are untouched
    either way.
    """
    if not managed.strip():
        return []
    try:
        data = tomllib.loads(managed)
    except tomllib.TOMLDecodeError as err:
        senselog.drop(STAGE, CREATE_RULE, "overlay", f"managed-block-unparseable: {err}")
        return []
    entries = data.get("react", [])
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


#: Key order for a rendered rule — the shipped ``default_rules.toml`` reads in
#: this order, so a hand inspection of the managed block looks like the file it
#: lives in. Keys outside the list are appended sorted (and then refused by
#: ``RulesConfig.from_dict``, which is exactly what should happen to them).
_RULE_KEY_ORDER = ("id", "when", "run", "params", "duration_s", "cooldown_s", "hysteresis", "say")


def _toml_scalar(value: object, *, path: str) -> str:
    """Render one JSON-decoded value as TOML, refusing anything unrenderable.

    Deliberately narrow: this serializes an LLM's JSON arguments, so the only
    inputs are the JSON types. Anything else is a bug or an attack and is
    refused here rather than written out and hoped about — a serializer that
    cannot serialize must say so.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # JSON string escapes are valid TOML basic-string escapes
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(v, path=f"{path}[]") for v in value) + "]"
    if isinstance(value, Mapping):
        return "{ " + ", ".join(_toml_pairs(value, path=path)) + " }"
    raise Refusal(
        REFUSAL_RULE,
        f"{path}: a value of type {type(value).__name__!r} cannot be written to a rules file",
    )


def _toml_pairs(table: Mapping, *, path: str) -> list[str]:
    pairs: list[str] = []
    for key, value in table.items():
        if not isinstance(key, str):
            raise Refusal(REFUSAL_RULE, f"{path}: rule keys must be strings (got {key!r})")
        if value is None:
            continue  # JSON null means "field absent", not "field is null"
        rendered_key = key if key.replace("_", "").replace("-", "").isalnum() else json.dumps(key)
        pairs.append(f"{rendered_key} = {_toml_scalar(value, path=f'{path}.{key}')}")
    return pairs


def _render_rule(entry: Mapping) -> str:
    ordered = [k for k in _RULE_KEY_ORDER if k in entry]
    ordered += sorted(k for k in entry if k not in _RULE_KEY_ORDER)
    lines = ["[[react]]"]
    for key in ordered:
        lines.extend(_toml_pairs({key: entry[key]}, path="react"))
    return "\n".join(lines)


def _render_managed(entries: list[dict]) -> str:
    """The managed block, with NO leading or trailing newline of its own.

    Separation from the operator's head and tail is
    :func:`_join_overlay`'s job, and it is idempotent there — a block that
    carried its own padding would grow the file by a blank line on every single
    write, which is unbounded growth in a file the robot re-reads forever.
    """
    body = "\n\n".join(_render_rule(entry) for entry in entries)
    return f"{MANAGED_BEGIN}\n{body}\n{MANAGED_END}"


def _join_overlay(head: str, managed: str, tail: str) -> str:
    """Reassemble the overlay with a FIXED-POINT separation.

    Reassembling ``_split_overlay``'s own output must reproduce it exactly, or
    every write leaks another newline. So the separators are computed from what
    the head/tail already end/start with rather than appended unconditionally,
    and the operator's own bytes are never trimmed to achieve it.
    """
    if head and not head.endswith("\n\n"):
        head += "\n" if head.endswith("\n") else "\n\n"
    if not tail.startswith("\n"):
        tail = "\n" + tail
    return head + managed + tail


def _merge_entry(existing: list[dict], entry: dict) -> list[dict]:
    """Replace the layer's own rule of the same id, else append. Never touches others."""
    rule_id = entry["id"]
    merged = [dict(e) for e in existing if e.get("id") != rule_id]
    merged.append(entry)
    return sorted(merged, key=lambda e: str(e.get("id", "")))


def _checked_rule_id(raw: object) -> str:
    """The one policy this module owns: the ``embody-`` namespace (spec c26)."""
    if not isinstance(raw, str) or not raw.strip():
        raise Refusal(REFUSAL_RULE_NAMESPACE, "'id' must be a non-empty string")
    if not raw.startswith(RULE_ID_PREFIX):
        raise Refusal(
            REFUSAL_RULE_NAMESPACE,
            f"rule id {raw!r} must start with {RULE_ID_PREFIX!r} — layer-authored rules are "
            "namespaced so they stay enumerable and removable as a set, and so a layer write "
            "can never collide with an operator-authored rule",
        )
    return raw


def _validate_candidate(path: Path, text: str) -> None:
    """Write *text* to a sibling temp file, validate it, then ``os.replace`` it in.

    The temp file IS the validation subject, so a rules file the engine would
    reject never exists at the real path even momentarily — and the rename is
    atomic (same directory, therefore same filesystem), so a concurrent reader
    sees the old file or the new one, never a half-written one.

    :func:`reachy.behavior.rules.load_rules` does the judging: TOML syntax,
    ``RulesConfig.from_dict``'s whole declarative-only schema, and the merge
    against the shipped layer. The layer adds no rule of its own here.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        tmp.write_text(text, encoding="utf-8")
        rules_mod.load_rules(tmp)
    except CliError as err:
        detail = err.message if not err.remediation else f"{err.message} ({err.remediation})"
        _unlink(tmp)
        raise Refusal(REFUSAL_RULE, detail) from err
    except OSError as err:
        _unlink(tmp)
        raise Refusal(REFUSAL_RULE, f"could not write the rules overlay: {err}") from err
    os.replace(tmp, path)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:  # pragma: no cover - defensive
        pass


def list_embody_rules(path: Path | None = None) -> tuple[str, ...]:
    """Every ``embody-`` rule id currently in the overlay, sorted.

    Prefix-scanned over the VALIDATED overlay rather than over the managed
    block, because the prefix — not the sentinel comments — is the contract
    (spec c26): this is what an operator's ``grep`` finds, and what a later
    ``agent embody`` verb would list. A missing overlay is an empty tuple; an
    unreadable one is a named drop and an empty tuple, never a raise.
    """
    target = path if path is not None else rules_mod.overlay_rules_path()
    if not target.is_file():
        return ()
    try:
        config = rules_mod.load_rules(target, include_shipped=False)
    except CliError as err:
        senselog.drop(STAGE, CREATE_RULE, "overlay", f"overlay-unreadable: {err.message}")
        return ()
    ids = [
        rule.id for rule in (*config.react, *config.inhibit) if rule.id.startswith(RULE_ID_PREFIX)
    ]
    return tuple(sorted(ids))


def _default_reload_seam(timeout: float) -> dict | None:
    """Submit a rules reload into the SAME spool ``behavior reload`` writes."""
    cmd_id = reload_driver.submit_reload()
    return reload_driver.await_result(cmd_id, timeout=timeout)


# --------------------------------------------------------------------------- #
# Handlers                                                                    #
# --------------------------------------------------------------------------- #


def _require_text(arguments: dict) -> str:
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Refusal(REFUSAL_SAY, "a non-empty 'text' string is required")
    if len(text) > MAX_SAY_CHARS:
        raise Refusal(
            REFUSAL_SAY,
            f"the utterance is {len(text)} characters, over the {MAX_SAY_CHARS}-character "
            "limit — the same bound a rule's say field carries",
        )
    return text


def _make_goto_handler(spool_root: Path | None, timeout: float) -> Callable[[dict], str]:
    def handler(arguments: dict) -> str:
        return json.dumps(_submit(GOTO, arguments, spool_root=spool_root, timeout=timeout))

    return handler


def _require_catalog_name(name: object, *, catalog: Mapping, refusal: str, what: str) -> str:
    """Refuse a behavior name outside *catalog*, naming the valid set.

    This is the ONE bound the layer must own, and it does not contradict the
    module docstring's "why the layer validates nothing itself" — it completes
    it. Every other bound belongs to a shipped validator, so restating it here
    would create a second number to drift. The **catalog** is different: it is a
    layer-owned restriction, and no downstream validator has ever heard of it.
    ``IntentDriver`` checks names against the global
    :data:`reachy.behavior.library.LIBRARY`, so a registry built with a
    RESTRICTED catalog was advisory only — the tool schema advertised the short
    ``enum`` while the handler happily admitted anything the full library knew.

    Measured before the fix, with ``catalog={'nod'}``: the advertised enum was
    ``['nod']`` and ``run_behavior{'name': 'shake'}`` returned ``ok: true`` and
    ran; ``create_rule{'run': 'shake'}`` wrote the rule. A schema ``enum`` is a
    hint to a model, not an enforcement boundary — a malformed client, or a
    direct ``dispatch`` call, never sees it.

    Mirrors :func:`reachy.speech.intent_tools._require_known_name`, which the
    sibling cognition root already applies for exactly this reason; the embody
    port dropped it. Raises :class:`Refusal` rather than ``ValueError`` so the
    outcome reaches the model as one of this module's NAMED refusals.
    """
    if not isinstance(name, str) or name not in catalog:
        valid = ", ".join(sorted(catalog)) or "(the catalog is empty)"
        raise Refusal(refusal, f"unknown {what} {name!r}; valid: {valid}")
    return name


def _make_run_behavior_handler(
    spool_root: Path | None, timeout: float, catalog: Mapping
) -> Callable[[dict], str]:
    def handler(arguments: dict) -> str:
        _require_catalog_name(
            arguments.get("name"), catalog=catalog, refusal=REFUSAL_BEHAVIOR, what="behavior"
        )
        duration = arguments.get("duration")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, (int, float))
        ):
            raise Refusal(REFUSAL_BAD_ARGUMENTS, "'duration' must be a number")
        payload = {
            "name": arguments.get("name"),
            "params": arguments.get("params") or {},
            "lifetime": {} if duration is None else {"duration": float(duration)},
        }
        return json.dumps(_submit(RUN_BEHAVIOR, payload, spool_root=spool_root, timeout=timeout))

    return handler


def _make_voice_handler(seam: SoundSeam | None, name: str) -> Callable[[dict], str]:
    def handler(arguments: dict) -> str:
        text = _require_text(arguments)
        if seam is None:
            raise Refusal(
                REFUSAL_NO_VOICE,
                f"{name} is unavailable: no voice seam was injected at composition",
            )
        seam(text)
        return json.dumps({"ok": True, "voice": name, "chars": len(text)})

    return handler


def _make_create_rule_handler(
    rules_path: Path | None, reload_seam: ReloadSeam, timeout: float, catalog: Mapping
) -> Callable[[dict], str]:
    def handler(arguments: dict) -> str:
        entry = dict(arguments)
        entry["id"] = _checked_rule_id(entry.get("id"))
        # The rule's `run` crosses the same catalog boundary as run_behavior's
        # `name`, and matters MORE: a rule outlives the layer (spec c26), so an
        # out-of-catalog behavior authored here keeps firing long after the
        # process that wrote it is gone.
        _require_catalog_name(
            entry.get("run"), catalog=catalog, refusal=REFUSAL_RULE, what="behavior"
        )
        target = rules_path if rules_path is not None else rules_mod.overlay_rules_path()

        text = target.read_text(encoding="utf-8") if target.is_file() else ""
        head, managed, tail = _split_overlay(text)
        entries = _merge_entry(_managed_entries(managed), entry)
        _validate_candidate(target, _join_overlay(head, _render_managed(entries), tail))

        reload_result = reload_seam(timeout)
        return json.dumps(
            {
                "ok": True,
                "id": entry["id"],
                "path": str(target),
                "rules": list(list_embody_rules(target)),
                "reload": reload_result,
            }
        )

    return handler


# --------------------------------------------------------------------------- #
# Tool definitions                                                            #
# --------------------------------------------------------------------------- #


def _goto_tool(spool_root: Path | None, timeout: float) -> Tool:
    return function_tool(
        name=GOTO,
        description=(
            "Move Reachy's head, antennas and/or body to a target pose, interpolating "
            "over 'duration' seconds. Name at least one of head / antennas / body_yaw. "
            "Every axis is bounded and every bound is fail-closed: an out-of-range "
            "target is REFUSED with the limit named, never silently clamped."
        ),
        parameters={
            "type": "object",
            "properties": {
                "head": {
                    "type": "object",
                    "description": (
                        "Head offsets: x/y/z in mm, roll/pitch/yaw in degrees. "
                        "Any subset of the six axes."
                    ),
                },
                "antennas": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[right, left] antenna angles in degrees.",
                },
                "body_yaw": {"type": "number", "description": "Body rotation in degrees."},
                "duration": {
                    "type": "number",
                    "description": (
                        f"Seconds to reach the target (0 < duration <= {MAX_DURATION_S})."
                    ),
                },
                "label": {"type": "string", "description": "A short name for this move."},
            },
            "required": [],
        },
        handler=_make_goto_handler(spool_root, timeout),
    )


def _run_behavior_tool(spool_root: Path | None, timeout: float, catalog: Mapping) -> Tool:
    return function_tool(
        name=RUN_BEHAVIOR,
        description=(
            "Run a named body behavior — a whole set of coordinated movements — once, "
            "for a bounded time. Pass 'duration' for any behavior that loops by "
            "default; an unbounded run is REFUSED, because it would hold its channel "
            "forever with nothing to stop it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": sorted(catalog),
                    "description": "The behavior to run. Must be one of 'enum'.",
                },
                "params": {
                    "type": "object",
                    "description": "Numeric parameter overrides for the behavior.",
                },
                "duration": {
                    "type": "number",
                    "description": "Seconds to run; required for looping behaviors.",
                },
            },
            "required": ["name"],
        },
        handler=_make_run_behavior_handler(spool_root, timeout, catalog),
    )


def _voice_tool(name: str, description: str, seam: SoundSeam | None) -> Tool:
    return function_tool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": f"What to say (at most {MAX_SAY_CHARS} characters).",
                }
            },
            "required": ["text"],
        },
        handler=_make_voice_handler(seam, name),
    )


def _create_rule_tool(
    rules_path: Path | None, reload_seam: ReloadSeam, timeout: float, catalog: Mapping
) -> Tool:
    return function_tool(
        name=CREATE_RULE,
        description=(
            "Teach Reachy a NEW standing reaction: when a sense condition holds, run a "
            "behavior and optionally say something. The rule is written into the robot's "
            "own rules file and reloaded live, so it keeps firing on its own with no "
            "further call from you — and it SURVIVES you being switched off. Rule ids "
            f"must start with {RULE_ID_PREFIX!r}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": f"Unique rule id; must start with {RULE_ID_PREFIX!r}.",
                },
                "when": {
                    "type": "object",
                    "description": (
                        "The trigger: {'field': one of "
                        f"{sorted(rules_mod.SENSE_FIELDS)}, 'op': one of "
                        f"{sorted(rules_mod.COMPARATORS)}, 'value': the operand "
                        "(omit it for is_true/is_false)}."
                    ),
                },
                "run": {
                    "type": "string",
                    "enum": sorted(catalog),
                    "description": "The behavior to run when the trigger holds.",
                },
                "params": {"type": "object", "description": "Numeric overrides for 'run'."},
                "say": {
                    "type": "string",
                    "description": f"Optional words to speak (max {MAX_SAY_CHARS} chars).",
                },
                "duration_s": {
                    "type": "number",
                    "description": "Seconds the behavior runs; required if it loops.",
                },
                "cooldown_s": {
                    "type": "number",
                    "description": "Minimum seconds between two firings (default 5).",
                },
                "hysteresis": {"type": "number", "description": "Anti-flap margin (default 0)."},
            },
            "required": ["id", "when", "run"],
        },
        handler=_make_create_rule_handler(rules_path, reload_seam, timeout, catalog),
    )


# --------------------------------------------------------------------------- #
# The registry — closed by construction                                       #
# --------------------------------------------------------------------------- #


class EmbodyToolRegistry:
    """Publishes the layer's action set and dispatches calls to it. Nothing else.

    Duck-compatible with :class:`reachy.speech.tools.ToolRegistry` where a turn
    engine touches it (:meth:`tools`, :meth:`dispatch`, :meth:`names`) and
    deliberately NOT where it widens: there is no ``register``. ``ToolRegistry``
    exposes one because the forge hot-registers a generated skill into it at
    runtime; the embodiment layer must not have that door, and "must not" is
    worth more as an absent method than as a comment.

    Parameters
    ----------
    speak / harmonics:
        The two voice seams, ``seam(text) -> object`` (composition binds them to
        the same ``synthesize`` + ``play_audio`` pair ``agent attach`` uses).
        ``None`` leaves the tool ADVERTISED but refusing with a named
        :data:`REFUSAL_NO_VOICE` — the layer's tool set must not change shape
        with the box's audio configuration, or the model learns a different
        robot on every start.
    spool_root:
        Overrides the state-dir root the intents spool writes under (mainly test
        isolation), exactly like
        :func:`reachy.speech.intent_tools.register_intent_tools`'s ``spool_dir``.
    rules_path:
        Overrides the rules overlay path (default:
        :func:`reachy.behavior.rules.overlay_rules_path`, resolved per CALL so a
        state-dir change is picked up).
    await_timeout:
        Seconds to wait for the engine to confirm a spool command, and for the
        reload seam.
    catalog:
        The behavior name set advertised + validated against (default: the full
        :data:`reachy.behavior.library.LIBRARY`).
    reload_seam:
        ``reload(timeout) -> dict | None``. Default: submit into the real reload
        spool and await the running engine's answer.
    """

    def __init__(
        self,
        *,
        speak: SoundSeam | None = None,
        harmonics: SoundSeam | None = None,
        spool_root: Path | None = None,
        rules_path: Path | None = None,
        await_timeout: float = DEFAULT_AWAIT_TIMEOUT,
        catalog: Mapping | None = None,
        reload_seam: ReloadSeam | None = None,
    ) -> None:
        lib = catalog if catalog is not None else behavior_library.LIBRARY
        reload_fn = reload_seam if reload_seam is not None else _default_reload_seam
        tools = (
            _goto_tool(spool_root, await_timeout),
            _voice_tool(SPEAK, "Speak text aloud in Reachy's spoken (TTS) voice.", speak),
            _voice_tool(
                HARMONICS,
                "Render text as a short melodic phrase in Reachy's harmonic voice "
                "(chirp/sing) — an expressive, non-speech vocalization.",
                harmonics,
            ),
            _run_behavior_tool(spool_root, await_timeout, lib),
            _create_rule_tool(rules_path, reload_fn, await_timeout, lib),
        )
        self._tools: dict[str, Tool] = {tool.name: tool for tool in tools}
        # The action set is a CONSTANT, not whatever happened to be built: if the
        # two ever disagree the layer's containment claim is already false.
        assert tuple(self._tools) == ACTION_SET  # nosec B101 - composition invariant

    def names(self) -> list[str]:
        """The action set, in publication order."""
        return list(self._tools)

    def tools(self) -> list[dict]:
        """The OpenAI ``tools=`` array — one definition per action."""
        return [tool.definition for tool in self._tools.values()]

    def dispatch(
        self,
        name: str,
        arguments_json: str | Mapping | None = None,
        tool_call_id: str | None = None,
    ) -> dict:
        """Execute *name* and return an OpenAI tool-result message. Never raises.

        Every outcome is one of two shapes, and both are JSON objects so a
        consumer never has to guess: ``{"ok": true|null, ...}`` for a performed
        or submitted action, and ``{"ok": false, "refusal": <name>, "error":
        <the validator's own words>}`` for a refusal. The refusal name is also
        the :func:`reachy.senselog.drop` reason, so the log line and the export
        feed agree.
        """
        event_id = tool_call_id or uuid.uuid4().hex[:8]
        senselog.stage(STAGE, name, event_id, "tool call dispatched")

        tool = self._tools.get(name)
        if tool is None:
            return self._refuse(
                tool_call_id,
                event_id,
                name,
                REFUSAL_UNKNOWN_TOOL,
                f"unknown tool {name!r}; the embodiment layer offers exactly: "
                f"{', '.join(ACTION_SET)}",
            )

        try:
            arguments = _parse_arguments(arguments_json)
        except (ValueError, TypeError) as err:
            return self._refuse(
                tool_call_id,
                event_id,
                name,
                REFUSAL_BAD_ARGUMENTS,
                f"malformed arguments for {name!r}: {err}",
            )

        try:
            content = tool.handler(arguments)
        except Refusal as err:
            return self._refuse(tool_call_id, event_id, name, err.reason, err.message)
        except Exception as err:  # noqa: BLE001 - a bad tool call must never kill the turn
            logger.warning("[embody] handler for %r raised: %s", name, err)
            return self._refuse(
                tool_call_id, event_id, name, REFUSAL_TOOL_ERROR, f"{name!r} failed: {err}"
            )
        return _tool_message(tool_call_id, content)

    @staticmethod
    def _refuse(
        tool_call_id: str | None, event_id: str, name: str, reason: str, detail: str
    ) -> dict:
        senselog.drop(STAGE, name, event_id, reason)
        return _tool_message(
            tool_call_id, json.dumps({"ok": False, "refusal": reason, "error": detail})
        )


def _parse_arguments(arguments_json: str | Mapping | None) -> dict:
    """Coerce the OpenAI ``function.arguments`` payload to a plain dict."""
    if arguments_json is None or arguments_json == "":
        return {}
    if isinstance(arguments_json, Mapping):
        return dict(arguments_json)
    parsed = json.loads(arguments_json)
    if not isinstance(parsed, dict):
        raise ValueError("arguments must decode to a JSON object")
    return parsed


def _tool_message(tool_call_id: str | None, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
