"""The behavior-rules schema — data-only react/inhibit rules + modes, never code.

Rules are read in TWO LAYERS (see :func:`load_rules` / :func:`merge_rules`):

* the **shipped layer** — ``reachy/behavior/default_rules.toml``, a read-only
  package resource read through :mod:`importlib.resources` (so it resolves
  identically from a source checkout and an installed wheel);
* the **box-local overlay** — ``<state_dir>/behavior/rules.toml`` (see
  :func:`default_rules_path`), the file an operator owns and tunes.

The overlay OVERRIDES the shipped layer **per rule id**; it does not replace it.
That is the only arrangement where an operator's tuning survives an upgrade AND
newly shipped rules reach an already-deployed box. Writing defaults into the
box-local path at install time would do the first badly (it overwrites tuning);
refusing to write them would do the second badly (upgraded boxes silently get
none of the new rules).

A rules file — either layer — has three sections:

* **react rules** (``[[react]]``) — ``when`` a :class:`Predicate` over the live
  sense snapshot holds, ``run`` a named :data:`reachy.behavior.library.LIBRARY`
  entry, with optional parameter overrides;
* **inhibit rules** (``[[inhibit]]``) — ``when`` a predicate holds, ``disable`` a
  named set of behaviors;
* **modes** (``[modes.<name>]``) — named, purely declarative parameter sets, one
  of which may be selected as the file's ``active_mode``.

A file may additionally carry a ``names`` array — the extra names this robot
answers to, ON TOP of the shipped pair
(:data:`reachy.speech.name_match.SHIPPED_NAMES`, spelled in code and never in a
rules file). It is EXTEND-only by construction: :func:`_validate_names` always
returns the shipped names first, so no rules file can take away the name the
robot has always answered to. Bounds: at most
:data:`MAX_CONFIGURED_NAMES` entries, each a letters-only word of at least
:data:`MIN_NAME_LENGTH` characters after lower-casing, de-duplicated silently.

A rule entry may carry ``enabled = false``, which makes it a **tombstone**: it
contributes no rule of its own and DISABLES the rule of that id contributed by
a lower layer. ``id`` is the only field a tombstone needs (the rest of a copied
stanza may stay, and is ignored), so disabling a shipped rule is a one-line
edit in the overlay rather than a fork of its body. ``enabled = true`` is the
implicit default and a plain no-op.

Every rule (react or inhibit) is uniquely ``id``-entified and carries
``cooldown_s`` (minimum seconds between firings, default 5.0) and ``hysteresis``
(the anti-flap margin around a threshold, default 0.0) — both validated
``>= 0`` numbers. A :class:`Predicate` is DATA (``field``/``op``/``value``), never
a string of code: ``field`` is one of :data:`SENSE_FIELDS`
(``doa``/``speech``/``rms``/``rms_ratio``/``pat``/``face``/``frame_available``/
``transcript``/``self_moving``) and ``op`` is one of
:data:`COMPARATORS`.

A REACT rule may additionally carry ``duration_s`` (a validated ``> 0`` number,
react-only — inhibit rules have no lifetime to bound). When present it caps the
admitted behavior's :class:`~reachy.behavior.model.Lifetime` at ``duration_s``
seconds regardless of the target library entry's own default, so a looping
behavior admitted by a rule stops on its own after ``duration_s`` seconds
instead of running until something else evicts it (see
:meth:`reachy.behavior.rule_engine.RuleEngine._build`).

A REACT rule may also carry ``say`` (react-only, a non-empty string of at most
:data:`MAX_SAY_CHARS` characters): TEXT the robot speaks aloud when the rule
fires. ``run`` names what the robot *does*; ``say`` names what it *says*, and
the two are independent halves of one reaction — pairing ``say`` with the
library's ``speak`` head-bob is what "the robot is talking" looks like. The
words are plain data; rendering them is
:class:`reachy.behavior.speech_act.SpeechActuator`'s job, on a background
worker, and this module knows nothing about voices, audio, or threads.

:meth:`RulesConfig.from_dict` is the single validation gate, mirroring
:meth:`reachy.stash.record.StashRecord.from_dict`. It refuses:

* any field outside the fixed declarative schema at any level (top-level,
  rule, predicate, mode) — no ``fn``/``code``/``source``/``exec``/free-form
  fields;
* any value that is not plain JSON-safe data (a callable/lambda/class instance
  anywhere in the structure);
* a ``run``/``disable`` behavior name that is not an existing
  :data:`reachy.behavior.library.LIBRARY` entry;
* a ``run`` parameter override key that is not one of the named behavior's
  declared parameters, or a non-numeric override value;
* an unknown predicate ``field``/``op``, a boolean-op predicate carrying a
  ``value``, or a numeric-op predicate missing/mistyping one;
* a negative ``cooldown_s``/``hysteresis``, or a duplicate rule ``id``;
* a react rule's ``duration_s`` that is not a positive number (``<= 0``,
  non-numeric, or a ``bool``);
* a react rule's ``say`` that is not a non-empty string, or that exceeds
  :data:`MAX_SAY_CHARS` — refused fail-closed, never truncated;
* a react rule that targets a library entry which is looping with no
  ``default_duration`` (an unbounded-looping default) and carries no
  ``duration_s`` of its own — refused FAIL-CLOSED so an admitted behavior can
  never hold a channel forever without an explicit, deliberate opt-in;
* an ``active_mode`` that does not name a defined mode, or defined modes with
  no ``active_mode`` selected;
* a ``names`` value that is not a list, an entry that is not a string, an entry
  that is not letters-only after lower-casing (so ``"no va"``, ``"n0va"``,
  ``"nope!"`` and ``""`` are all refused), an entry shorter than
  :data:`MIN_NAME_LENGTH`, or more than :data:`MAX_CONFIGURED_NAMES` entries —
  each naming the offending entry and the bound it broke.

Every failure raises :class:`~reachy.cli._errors.CliError` (exit-code 1, user
error) with a specific, actionable message — never a bare
``KeyError``/``TypeError``/``tomllib.TOMLDecodeError`` escaping to a caller.
This module deliberately reuses :class:`CliError` rather than introducing a
parallel ``RulesError`` taxonomy, exactly mirroring the stash record's choice.

Loading (:func:`load_rules`, :class:`RulesLoader`) is stdlib-only
(:mod:`tomllib` + :mod:`importlib.resources`, both read-only in the standard
library). A missing overlay is NOT an error — it resolves to the shipped layer
alone ("no local rules configured yet"). :class:`RulesLoader` additionally
keeps the *last good* config in memory: a candidate reload that fails to parse
or validate never clobbers a previously good config, it only records why the
candidate was rejected (:attr:`RulesLoader.last_error`). That retention now
spans both layers — the loader's floor before any successful reload is the
SHIPPED layer, so a malformed overlay degrades to the shipped rules rather than
to nothing.

This module is intentionally PURE: parsing, validation, and dataclasses only.
It has no engine coupling and no evaluation logic — *interpreting* a
:class:`Predicate` against a live sense reading, applying ``cooldown_s``/
``hysteresis`` timing, and actually running/disabling behaviors is the job of
the (separate, dependent) rules evaluator.
"""

from __future__ import annotations

import re
import logging
import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from reachy.behavior import library as behavior_library
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.daemon import state_dir
from reachy.speech.name_match import SHIPPED_NAMES

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Schema constants                                                            #
# --------------------------------------------------------------------------- #

RULES_SUBDIR = "behavior"
RULES_FILENAME = "rules.toml"

#: Where the SHIPPED layer lives, addressed as a package resource (never as a
#: filesystem path relative to ``__file__`` — that only works from a source
#: checkout, and would silently ship nothing from a wheel).
SHIPPED_RULES_PACKAGE = "reachy.behavior"
SHIPPED_RULES_RESOURCE = "default_rules.toml"

#: The Sense-snapshot fields a predicate may test. Deliberately small and
#: hand-picked to match the perception fields :mod:`reachy.behavior.sense`
#: knows how to carry — NOT a placeholder for every attribute a future engine
#: might ever track. This is the SCHEMA-ACCEPTED vocabulary only: a field
#: being valid here does not mean the current engine composition feeds it a
#: live reading. ``reachy.behavior.sense.FED_SENSE_FIELDS`` is the (usually
#: smaller) subset actually wired today, and ``behavior rules check`` warns on
#: any rule keyed outside it — see that module for why the two can differ.
SENSE_FIELDS: frozenset[str] = frozenset(
    {
        "doa",
        "speech",
        "rms",
        "rms_ratio",
        "pat",
        "face",
        "frame_available",
        "transcript",
        "self_moving",
        # Latched for ONE tick by the transcript sense when an utterance was
        # admitted BY NAME (the engagement gate's ``name`` label) — never by a
        # context-only admission.
        "name_mentioned",
    }
)

#: Sense fields whose bare reading is measured to be too noisy to key a rule on
#: BY ITSELF. Today that is ``speech`` alone.
#:
#: Measured on the deployed robot in a QUIET room with nobody speaking, 120
#: samples over 60 s (``docs/verification/2026-07-20-retire-old-flow-baseline.md``
#: section 2): ``speech_detected`` read True **55/120 = 45.8 %** of the time,
#: while the bearing wandered essentially the full 0.000-3.124 rad range. A rule
#: keyed on it fires on roughly a coin flip, pointed at nothing.
#:
#: A :class:`Rule` carries exactly ONE ``when`` predicate — the schema has no
#: conjunction — so ANY rule whose ``when.field`` is in this set is by
#: construction keyed on the BARE signal. That is why the check downstream needs
#: no cross-predicate analysis, and it is also the honest reason this is a lint
#: rather than a schema refusal: refusing would amount to deleting ``speech``
#: from :data:`SENSE_FIELDS` outright, which is a product decision, not a
#: validator's call.
UNCORROBORATED_SENSE_FIELDS: frozenset[str] = frozenset({"speech"})

#: The measured at-rest true-rate for the fields above, quoted verbatim in the
#: warning so an operator sees EVIDENCE rather than an assertion.
UNCORROBORATED_AT_REST_RATE = "45.8% (55/120 samples, quiet room, nobody speaking)"

#: Fields that DO carry corroboration, named in the warning as the fix. Each is
#: either already gated (``transcript`` is an utterance that cleared the layered
#: engagement gate) or a physical measurement (``rms_ratio``/``rms``/``pat``/
#: ``face``). ``rms_ratio`` leads the loudness pair deliberately: an absolute
#: ``rms`` threshold is corroborating only in the room it was calibrated in —
#: the deployed 0.02 sat under the measured NIGHT background, where 99.1 % of
#: still-room samples cleared it (issue #102) — while a ratio over the rolling
#: background means the same thing in every room.
CORROBORATING_SENSE_FIELDS: tuple[str, ...] = ("transcript", "rms_ratio", "rms", "pat", "face")

#: Ordered numeric comparators — require a numeric ``value``.
_ORDERED_OPS: frozenset[str] = frozenset({"lt", "gt", "ge", "le"})
#: Equality comparators — require a ``value`` (any JSON scalar).
_EQUALITY_OPS: frozenset[str] = frozenset({"eq", "ne"})
#: Boolean-presence comparators — take NO ``value``.
_BOOLEAN_OPS: frozenset[str] = frozenset({"is_true", "is_false"})
#: "Has this field been missing/absent for at least N seconds" — a duration op.
_DURATION_OPS: frozenset[str] = frozenset({"absent_for"})
#: The full set of valid predicate comparators.
COMPARATORS: frozenset[str] = _ORDERED_OPS | _EQUALITY_OPS | _BOOLEAN_OPS | _DURATION_OPS

KIND_REACT = "react"
KIND_INHIBIT = "inhibit"

DEFAULT_COOLDOWN_S = 5.0
DEFAULT_HYSTERESIS = 0.0

#: Hard ceiling on a react rule's ``say`` text, in characters. Bounded
#: FAIL-CLOSED (refused, never truncated) for the same reason
#: :mod:`reachy.behavior.goto_intent` caps a goto's duration: a rules file is
#: operator data, and an actuator with a real-world cost — here, seconds of the
#: robot holding the room — must not be handed an unbounded one. This is also
#: the bound :class:`reachy.behavior.speech_act.SpeechActuator` re-checks (it
#: imports THIS constant, so there is one number), because a rule is not its
#: only caller.
MAX_SAY_CHARS = 500

#: The alphabet a configured name may use: the matcher tokenises ``[A-Za-z]``.
_ASCII_NAME_RE = re.compile(r"[a-z]+")

#: How many entries a rules file's ``names`` table may carry. Bounded rather
#: than open-ended for the same fail-closed reason :data:`MAX_SAY_CHARS` is:
#: every configured name is another word the fuzzy matcher tests every heard
#: utterance against, so a pasted word list must not be able to make the robot
#: answer to half the dictionary.
MAX_CONFIGURED_NAMES = 8

#: The shortest word that may be configured as a name. Below this there is not
#: enough word left for a fuzzy match to separate a name from ordinary speech.
MIN_NAME_LENGTH = 3

_PREDICATE_FIELDS = frozenset({"field", "op", "value"})
_TOP_LEVEL_FIELDS = frozenset({"active_mode", "react", "inhibit", "modes", "names"})
_REACT_FIELDS = frozenset(
    {"id", "enabled", "when", "run", "params", "cooldown_s", "hysteresis", "duration_s", "say"}
)
_INHIBIT_FIELDS = frozenset({"id", "enabled", "when", "disable", "cooldown_s", "hysteresis"})
_REACT_REQUIRED = frozenset({"id", "when", "run"})
_INHIBIT_REQUIRED = frozenset({"id", "when", "disable"})

# Plain JSON scalar types. Anything outside (str, list, dict) + these is a code
# smell (a function, a class instance, a lambda, ...).
_JSON_SCALARS = (str, int, float, type(None))


def overlay_rules_path() -> Path:
    """The BOX-LOCAL overlay's location: ``<state_dir>/behavior/rules.toml``.

    This is the operator's own file — the upper of the two layers. The shipped
    DEFAULTS are not here; they are a package resource
    (:data:`SHIPPED_RULES_RESOURCE`), deliberately, so an upgrade can never
    overwrite what is written at this path.
    """
    return state_dir() / RULES_SUBDIR / RULES_FILENAME


#: Long-standing alias for :func:`overlay_rules_path` — kept because it is the
#: name the CLI verbs, the engine composition, and the docs already use. The
#: newer name says which of the two layers it means.
default_rules_path = overlay_rules_path


def shipped_rules_text() -> str | None:
    """Read the SHIPPED layer's TOML text, or ``None`` if it is not present.

    Goes through :mod:`importlib.resources`, so it resolves identically from a
    source checkout and from an installed wheel (where the file may live inside
    a zip and have no filesystem path at all). A missing/unreadable resource is
    not an error here — it degrades to "no shipped rules"; see
    :func:`load_shipped_rules`.
    """
    try:
        handle = resources.files(SHIPPED_RULES_PACKAGE).joinpath(SHIPPED_RULES_RESOURCE)
        if not handle.is_file():
            return None
        return handle.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, OSError):  # pragma: no cover - defensive
        return None


def _error(message: str, remediation: str = "") -> CliError:
    return CliError(code=EXIT_USER_ERROR, message=message, remediation=remediation)


def _reject_code_smell(value: object, *, path: str) -> None:
    """Recursively reject anything that isn't plain JSON-safe declarative data.

    Mirrors :func:`reachy.stash.record._reject_code_smell`. ``bool`` is fine as
    general JSON data here (numeric fields reject it separately); the walk only
    needs to catch non-JSON types (callables, class instances, sets, bytes, ...).
    """
    if isinstance(value, bool):
        return
    if isinstance(value, _JSON_SCALARS):
        return
    if isinstance(value, Mapping):
        for key, val in value.items():
            if not isinstance(key, str):
                raise _error(f"{path}: dict keys must be strings (got {key!r})")
            _reject_code_smell(val, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _reject_code_smell(item, path=f"{path}[{i}]")
        return
    raise _error(
        f"{path}: value of type {type(value).__name__!r} is not declarative JSON data "
        "(rules files must contain no code — no functions, lambdas, or objects)",
        remediation="rules files are plain TOML/JSON-serializable data only",
    )


def _require_str(data: Mapping, key: str, *, path: str, allow_blank: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not allow_blank and not value.strip()):
        raise _error(
            f"{path}.{key} must be a non-empty string (got {value!r})",
            remediation=f"provide a string value for {key!r}",
        )
    return value


def _validate_nonneg_float(raw: object, *, name: str, path: str, default: float) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _error(f"{path}.{name} must be a number (got {raw!r})")
    value = float(raw)
    if value < 0:
        raise _error(f"{path}.{name} must be >= 0 (got {value!r})")
    return value


def _validate_positive_float(raw: object, *, name: str, path: str) -> float | None:
    """Validate an optional strictly-positive number: ``None`` when absent.

    Mirrors :func:`_validate_nonneg_float`'s posture (reject non-numeric and
    ``bool``), but for a field with no meaningful default — ``duration_s`` is
    either omitted entirely or a real positive number of seconds; ``0`` or
    negative makes no sense as a duration.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise _error(f"{path}.{name} must be a number (got {raw!r})")
    value = float(raw)
    if value <= 0:
        raise _error(f"{path}.{name} must be > 0 (got {value!r})")
    return value


# --------------------------------------------------------------------------- #
# Dataclasses                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Predicate:
    """A data-only perception predicate over one Sense-snapshot field.

    ``field`` is one of :data:`SENSE_FIELDS`; ``op`` is one of
    :data:`COMPARATORS`; ``value`` is the operand — ``None`` for
    ``is_true``/``is_false``, a non-negative number of seconds for
    ``absent_for``, and any JSON scalar for every other comparator. This is
    DATA, never a string of code — a predicate is only ever *interpreted* by
    the (separate) rules evaluator.
    """

    field: str
    op: str
    value: object = None


@dataclass(frozen=True)
class Mode:
    """A named, purely declarative parameter set — one of a rules file's modes.

    ``params`` is a flat ``name -> number`` map; what the names mean is up to
    whatever rules/evaluator choose to read them (this module assigns no
    meaning beyond "a mode is a named bag of numbers").
    """

    name: str
    params: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    """One validated rule — either a REACT rule or an INHIBIT rule.

    ``kind`` (:data:`KIND_REACT` or :data:`KIND_INHIBIT`) discriminates the two
    flavors sharing this one dataclass:

    * REACT — when :attr:`when` holds, run :attr:`behavior` (an existing
      :data:`reachy.behavior.library.LIBRARY` entry) with :attr:`params`
      overriding its defaults. :attr:`disable` is always empty.
    * INHIBIT — when :attr:`when` holds, disable every behavior named in
      :attr:`disable`. :attr:`behavior`/:attr:`params` are unused
      (``None``/empty).

    Every rule carries :attr:`cooldown_s` (minimum seconds between two
    firings/state-changes of this rule; validated ``>= 0``) and
    :attr:`hysteresis` (the anti-flap margin around a threshold the evaluator
    uses; validated ``>= 0``). This module only carries the validated numbers —
    *acting* on cooldown/hysteresis timing is the evaluator's job.

    :attr:`duration_s` is REACT-ONLY (always ``None`` on an INHIBIT rule): an
    optional, validated ``> 0`` number of seconds bounding the admitted
    behavior's lifetime, overriding the target library entry's own default
    duration. See :meth:`RulesConfig.from_dict`'s fail-closed refusal of a
    react rule that targets an unbounded-looping entry with no
    :attr:`duration_s`.

    :attr:`say` is likewise REACT-ONLY: optional TEXT the robot speaks aloud
    when the rule fires, at most :data:`MAX_SAY_CHARS` characters. It is the
    AUDIBLE half of a reaction, dispatched to an injected speech seam by
    :class:`reachy.behavior.rule_engine.RuleEngine` and rendered off the tick
    thread by :class:`reachy.behavior.speech_act.SpeechActuator`; :attr:`behavior`
    remains the visible half (pair it with the library's ``speak`` head-bob to
    get a robot that looks like it is talking). It is plain data — a string in
    a TOML file, never a template, an expression, or a path to code — exactly
    like every other field here.
    """

    id: str
    kind: str
    when: Predicate
    cooldown_s: float = DEFAULT_COOLDOWN_S
    hysteresis: float = DEFAULT_HYSTERESIS
    behavior: str | None = None
    params: dict[str, float] = field(default_factory=dict)
    disable: frozenset[str] = field(default_factory=frozenset)
    duration_s: float | None = None
    say: str | None = None


@dataclass(frozen=True)
class RulesConfig:
    """A fully validated rules file: react rules, inhibit rules, and modes.

    Construct via :meth:`from_dict` (never directly) so every instance is
    guaranteed to have passed schema validation. The all-empty default is
    exactly what :func:`load_rules` returns for a not-yet-created rules file
    with an empty shipped layer — "no rules configured" is a valid, inert
    state, not an error.

    :attr:`disabled` carries the ids this layer TOMBSTONED (``enabled =
    false``). Within a single layer a tombstone simply means "this rule is not
    in force"; across layers :func:`merge_rules` uses it to remove the rule of
    that id contributed by a lower layer.
    """

    react: tuple[Rule, ...] = ()
    inhibit: tuple[Rule, ...] = ()
    modes: dict[str, Mode] = field(default_factory=dict)
    active_mode: str | None = None
    disabled: frozenset[str] = frozenset()
    names: tuple[str, ...] = SHIPPED_NAMES

    @classmethod
    def from_dict(cls, data: object) -> "RulesConfig":
        """Validate *data* (a parsed TOML/JSON mapping) against the rules schema.

        Raises :class:`~reachy.cli._errors.CliError` (exit-code 1) with a
        specific, actionable message on anything malformed or smelling of code
        — never a bare ``KeyError``/``TypeError``/``AttributeError``.
        """
        if not isinstance(data, Mapping):
            raise _error(f"a rules file must be a TOML/JSON object (got {type(data).__name__!r})")

        unknown = set(data) - _TOP_LEVEL_FIELDS
        if unknown:
            raise _error(
                f"rules file has unexpected top-level field(s) {sorted(unknown)} — rules "
                "files are declarative-only data (no code/source/lambdas/free-form fields)",
                remediation=f"the allowed sections are: {', '.join(sorted(_TOP_LEVEL_FIELDS))}",
            )

        # Structural code-smell sweep BEFORE any semantic validation — catches a
        # lambda/callable/class-instance anywhere in the tree with one clean error.
        _reject_code_smell(dict(data), path="rules")

        react_raw = data.get("react", [])
        if not isinstance(react_raw, list):
            raise _error(f"'react' must be a list of rule tables (got {react_raw!r})")
        inhibit_raw = data.get("inhibit", [])
        if not isinstance(inhibit_raw, list):
            raise _error(f"'inhibit' must be a list of rule tables (got {inhibit_raw!r})")

        react_rules, react_off = _partition_entries(
            react_raw, kind=KIND_REACT, validate=_validate_react_rule
        )
        inhibit_rules, inhibit_off = _partition_entries(
            inhibit_raw, kind=KIND_INHIBIT, validate=_validate_inhibit_rule
        )
        disabled = react_off | inhibit_off

        all_ids = [r.id for r in react_rules] + [r.id for r in inhibit_rules] + sorted(disabled)
        duplicates = sorted({rule_id for rule_id in all_ids if all_ids.count(rule_id) > 1})
        if duplicates:
            raise _error(
                f"rules file has duplicate rule id(s): {duplicates} — every rule id must be "
                "unique across react + inhibit",
                remediation="rename one of the duplicated rules",
            )

        modes = _validate_modes(data.get("modes"))
        active_mode = _validate_active_mode(data.get("active_mode"), modes)
        names = _validate_names(data.get("names"))

        return cls(
            react=react_rules,
            inhibit=inhibit_rules,
            modes=modes,
            active_mode=active_mode,
            disabled=disabled,
            names=names,
        )


# --------------------------------------------------------------------------- #
# Field-level validators                                                     #
# --------------------------------------------------------------------------- #


def _validate_predicate(raw: object, *, path: str) -> Predicate:
    if not isinstance(raw, Mapping):
        raise _error(f"{path}.when must be an object (got {raw!r})")
    unknown = set(raw) - _PREDICATE_FIELDS
    if unknown:
        raise _error(
            f"{path}.when has unexpected field(s) {sorted(unknown)}",
            remediation=f"allowed fields: {', '.join(sorted(_PREDICATE_FIELDS))}",
        )

    field_name = raw.get("field")
    if not isinstance(field_name, str) or field_name not in SENSE_FIELDS:
        raise _error(
            f"{path}.when.field is unknown (got {field_name!r})",
            remediation=f"use one of: {', '.join(sorted(SENSE_FIELDS))}",
        )

    op = raw.get("op")
    if not isinstance(op, str) or op not in COMPARATORS:
        raise _error(
            f"{path}.when.op is unknown (got {op!r})",
            remediation=f"use one of: {', '.join(sorted(COMPARATORS))}",
        )

    value = _validate_predicate_value(raw, op=op, path=path)
    return Predicate(field=field_name, op=op, value=value)


def _validate_predicate_value(raw: Mapping, *, op: str, path: str) -> object:
    """Normalize + validate the ``value`` field against *op*'s comparator class."""
    has_value = "value" in raw
    value = raw.get("value")

    if op in _BOOLEAN_OPS:
        if has_value and value is not None:
            raise _error(
                f"{path}.when: op {op!r} takes no 'value' (got {value!r})",
                remediation="remove 'value' for is_true/is_false predicates",
            )
        return None
    if op in _ORDERED_OPS or op in _DURATION_OPS:
        if not has_value or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _error(
                f"{path}.when: op {op!r} requires a numeric 'value' (got {value!r})",
                remediation="provide a numeric 'value'",
            )
        if value < 0:
            raise _error(f"{path}.when: 'value' for op {op!r} must be >= 0 (got {value!r})")
        return float(value)
    # equality ops
    if not has_value:
        raise _error(f"{path}.when: op {op!r} requires a 'value' field")
    if isinstance(value, (dict, list)):
        raise _error(f"{path}.when.value must be a scalar for op {op!r} (got {value!r})")
    return value


def _validate_say(raw: object, *, path: str) -> str | None:
    """Validate an optional react-rule ``say``: ``None`` when absent.

    A rule that carries the field at all must carry real, bounded text — a
    blank string, a number, a list, or anything over :data:`MAX_SAY_CHARS` is
    REFUSED rather than coerced or truncated, matching this module's posture on
    every other optional field (and ``goto``'s fail-closed axis bounds).
    ``bool`` is rejected explicitly: ``say = true`` is a mistake, not an
    utterance, and would otherwise slip through a bare ``isinstance(str)``
    check unnoticed only because TOML has no such coercion.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, str) or not raw.strip():
        raise _error(
            f"{path}.say must be a non-empty string (got {raw!r})",
            remediation="give say the words to speak, or remove the field entirely",
        )
    if len(raw) > MAX_SAY_CHARS:
        raise _error(
            f"{path}.say is {len(raw)} characters, over the {MAX_SAY_CHARS}-character limit",
            remediation=f"shorten the utterance to at most {MAX_SAY_CHARS} characters",
        )
    return raw


def _validate_behavior_name(name: object, *, path: str) -> str:
    if not isinstance(name, str) or name not in behavior_library.LIBRARY:
        raise _error(
            f"{path}: unknown behavior {name!r}",
            remediation=f"use one of: {', '.join(sorted(behavior_library.LIBRARY))}",
        )
    owner = behavior_library.LIFECYCLE_OWNED.get(name)
    if owner is not None:
        # Fail-closed at LOAD, like the unbounded-lifetime refusal: a rule
        # cannot admit a behavior whose lifetime a dedicated intent kind owns.
        raise _error(
            f"{path}: {name!r} is owned by the {owner!r} intent kind and cannot be run by a rule",
            remediation=f"drive it through {owner!r} (and its release) instead",
        )
    return name


def _validate_run_params(raw: object, *, entry: behavior_library.LibraryEntry, path: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _error(f"{path}.params must be an object (got {raw!r})")
    params: dict[str, float] = {}
    for key, value in raw.items():
        if key not in entry.params:
            raise _error(
                f"{path}.params has unknown parameter {key!r} for behavior {entry.name!r}",
                remediation=f"valid params: {', '.join(sorted(entry.params)) or '(none)'}",
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _error(f"{path}.params[{key!r}] must be a number (got {value!r})")
        params[key] = float(value)
    return params


def _validate_disable(raw: object, *, path: str) -> frozenset[str]:
    if not isinstance(raw, list) or not raw:
        raise _error(
            f"{path}.disable must be a non-empty list of behavior names (got {raw!r})",
            remediation=f"choose from: {', '.join(sorted(behavior_library.LIBRARY))}",
        )
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or item not in behavior_library.LIBRARY:
            raise _error(
                f"{path}.disable has an unknown behavior {item!r}",
                remediation=f"choose from: {', '.join(sorted(behavior_library.LIBRARY))}",
            )
        names.add(item)
    return frozenset(names)


def _is_tombstone(raw: Mapping, *, path: str) -> bool:
    """Is this entry a ``enabled = false`` tombstone? Validates the flag's type."""
    if "enabled" not in raw:
        return False  # absent flag: enabled, the default
    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        raise _error(
            f"{path}.enabled must be a boolean (got {enabled!r})",
            remediation="use enabled = false to disable a rule of this id from a lower layer",
        )
    return not enabled


def _partition_entries(
    raw_entries: list, *, kind: str, validate
) -> tuple[tuple[Rule, ...], frozenset[str]]:
    """Split a ``[[react]]``/``[[inhibit]]`` list into live rules and tombstones.

    A tombstone (``enabled = false``) contributes no :class:`Rule` — only its
    id, which :func:`merge_rules` uses to remove a lower layer's rule of that
    id. It is validated only for the things that still mean something on a
    disabled entry: an ``id``, and no unknown fields (so a typo is still
    caught). The rest of a copied-and-disabled stanza is deliberately NOT
    semantically validated — it is inert data the operator kept for reference,
    and refusing it would make disabling a shipped rule harder than writing it.
    """
    allowed = _REACT_FIELDS if kind == KIND_REACT else _INHIBIT_FIELDS
    rules: list[Rule] = []
    disabled: set[str] = set()
    for index, raw in enumerate(raw_entries):
        path = f"{kind}[{index}]"
        if not isinstance(raw, Mapping):
            raise _error(f"{path} must be an object (got {raw!r})")
        unknown = set(raw) - allowed
        if unknown:
            raise _error(
                f"{path} has unexpected field(s) {sorted(unknown)}",
                remediation=f"allowed fields: {', '.join(sorted(allowed))}",
            )
        if _is_tombstone(raw, path=path):
            disabled.add(_require_str(raw, "id", path=path))
            continue
        rules.append(validate(raw, index=index))
    return tuple(rules), frozenset(disabled)


def _validate_react_rule(raw: object, *, index: int) -> Rule:
    path = f"react[{index}]"
    if not isinstance(raw, Mapping):
        raise _error(f"{path} must be an object (got {raw!r})")
    unknown = set(raw) - _REACT_FIELDS
    if unknown:
        raise _error(
            f"{path} has unexpected field(s) {sorted(unknown)}",
            remediation=f"allowed fields: {', '.join(sorted(_REACT_FIELDS))}",
        )
    missing = _REACT_REQUIRED - set(raw)
    if missing:
        raise _error(f"{path} is missing required field(s): {sorted(missing)}")

    rule_id = _require_str(raw, "id", path=path)
    behavior_name = _validate_behavior_name(raw.get("run"), path=f"{path}.run")
    entry = behavior_library.LIBRARY[behavior_name]
    when = _validate_predicate(raw["when"], path=path)
    params = _validate_run_params(raw.get("params"), entry=entry, path=path)
    cooldown_s = _validate_nonneg_float(
        raw.get("cooldown_s"), name="cooldown_s", path=path, default=DEFAULT_COOLDOWN_S
    )
    hysteresis = _validate_nonneg_float(
        raw.get("hysteresis"), name="hysteresis", path=path, default=DEFAULT_HYSTERESIS
    )
    duration_s = _validate_positive_float(raw.get("duration_s"), name="duration_s", path=path)
    say = _validate_say(raw.get("say"), path=path)

    if duration_s is None and entry.looping and entry.default_duration is None:
        raise _error(
            f"{path} (id={rule_id!r}) runs {behavior_name!r}, which loops with no default "
            "duration (unbounded looping default) and carries no duration_s — admitting it "
            "would let it hold its channel forever",
            remediation=f"add duration_s = <seconds> to react rule {rule_id!r}",
        )

    return Rule(
        id=rule_id,
        kind=KIND_REACT,
        when=when,
        cooldown_s=cooldown_s,
        hysteresis=hysteresis,
        behavior=behavior_name,
        params=params,
        disable=frozenset(),
        duration_s=duration_s,
        say=say,
    )


def _validate_inhibit_rule(raw: object, *, index: int) -> Rule:
    path = f"inhibit[{index}]"
    if not isinstance(raw, Mapping):
        raise _error(f"{path} must be an object (got {raw!r})")
    unknown = set(raw) - _INHIBIT_FIELDS
    if unknown:
        raise _error(
            f"{path} has unexpected field(s) {sorted(unknown)}",
            remediation=f"allowed fields: {', '.join(sorted(_INHIBIT_FIELDS))}",
        )
    missing = _INHIBIT_REQUIRED - set(raw)
    if missing:
        raise _error(f"{path} is missing required field(s): {sorted(missing)}")

    rule_id = _require_str(raw, "id", path=path)
    when = _validate_predicate(raw["when"], path=path)
    disable = _validate_disable(raw.get("disable"), path=path)
    cooldown_s = _validate_nonneg_float(
        raw.get("cooldown_s"), name="cooldown_s", path=path, default=DEFAULT_COOLDOWN_S
    )
    hysteresis = _validate_nonneg_float(
        raw.get("hysteresis"), name="hysteresis", path=path, default=DEFAULT_HYSTERESIS
    )

    return Rule(
        id=rule_id,
        kind=KIND_INHIBIT,
        when=when,
        cooldown_s=cooldown_s,
        hysteresis=hysteresis,
        behavior=None,
        params={},
        disable=disable,
    )


def _validate_mode(name: str, raw: object) -> Mode:
    path = f"modes.{name}"
    if not isinstance(raw, Mapping):
        raise _error(f"{path} must be an object (got {raw!r})")
    params: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise _error(f"{path}: parameter keys must be strings (got {key!r})")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _error(f"{path}.{key} must be a number (got {value!r})")
        params[key] = float(value)
    return Mode(name=name, params=params)


def _validate_modes(raw: object) -> dict[str, Mode]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _error(f"'modes' must be an object (got {raw!r})")
    modes: dict[str, Mode] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise _error(f"'modes' has an invalid mode name {name!r}")
        modes[name] = _validate_mode(name, value)
    return modes


def _dedupe(names: tuple[str, ...]) -> tuple[str, ...]:
    """Order-preserving de-duplication — the first spelling of a name wins."""
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return tuple(ordered)


def _validate_names(raw: object) -> tuple[str, ...]:
    """Validate the optional ``names`` table: the SHIPPED pair plus additions.

    The shipped names (:data:`reachy.speech.name_match.SHIPPED_NAMES`) are
    spelled in CODE and always come first, in order. A rules file may only
    EXTEND them: there is deliberately no way to remove one, because a robot
    that stops answering to its own name after a typo in a TOML list is a robot
    an operator cannot talk their way back to.

    Everything here is fail-closed and refuses the WHOLE file, naming the
    offending entry and the bound it broke — the same posture as ``say``'s
    length cap and ``goto``'s axis bounds. An absent table means "no additions",
    never "no names".
    """
    if raw is None:
        return SHIPPED_NAMES
    if not isinstance(raw, list):
        raise _error(
            f"'names' must be a list of strings (got {raw!r})",
            remediation='write names = ["<name>"] — a TOML array, even for one name',
        )
    if len(raw) > MAX_CONFIGURED_NAMES:
        raise _error(
            f"'names' has {len(raw)} entries, over the {MAX_CONFIGURED_NAMES}-entry limit",
            remediation=(
                f"keep at most {MAX_CONFIGURED_NAMES} names — every one is a word every "
                "heard utterance is matched against"
            ),
        )

    extra: list[str] = []
    for index, item in enumerate(raw):
        path = f"names[{index}]"
        if isinstance(item, bool) or not isinstance(item, str):
            raise _error(
                f"{path}: every name must be a string (got {item!r})",
                remediation="quote each name in the array",
            )
        name = item.strip().lower()
        # ASCII a-z ONLY — ``str.isalpha`` would admit an accented name the
        # matcher's ``[A-Za-z]`` tokeniser can never extract, so it would be
        # accepted here and unreachable everywhere else (#177 review).
        if not _ASCII_NAME_RE.fullmatch(name):
            raise _error(
                f"{path}: name {item!r} must be letters only (a-z) after lower-casing",
                remediation="a name is one word: no digits, spaces, punctuation, or blanks",
            )
        if len(name) < MIN_NAME_LENGTH:
            raise _error(
                f"{path}: name {item!r} is shorter than the {MIN_NAME_LENGTH}-character minimum",
                remediation=f"configure a name of at least {MIN_NAME_LENGTH} letters",
            )
        extra.append(name)

    return _dedupe((*SHIPPED_NAMES, *extra))


def _validate_active_mode(raw: object, modes: dict[str, Mode]) -> str | None:
    if raw is None:
        if modes:
            raise _error(
                f"rules file defines mode(s) {sorted(modes)} but no 'active_mode' is selected",
                remediation=f"set active_mode to one of: {', '.join(sorted(modes))}",
            )
        return None
    if not isinstance(raw, str) or raw not in modes:
        raise _error(
            f"'active_mode' {raw!r} is not a defined mode",
            remediation=f"use one of: {', '.join(sorted(modes)) or '(no modes defined)'}",
        )
    return raw


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #


def _parse_rules_text(text: str, *, origin: str) -> RulesConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise _error(
            f"rules file {origin} is not valid TOML: {err}",
            remediation="fix the TOML syntax",
        ) from err
    return RulesConfig.from_dict(data)


def load_shipped_rules() -> RulesConfig:
    """Read + validate the SHIPPED layer (the read-only package resource).

    An ABSENT resource resolves to an empty :class:`RulesConfig`; malformed
    shipped content raises :class:`~reachy.cli._errors.CliError`, so a
    packaging/content regression fails loudly in CI. At runtime the raise is
    caught one level up (:func:`_shipped_layer`) — a defect in what WE ship
    must never make an operator's own rules unloadable.
    """
    text = shipped_rules_text()
    if text is None:
        return RulesConfig()
    return _parse_rules_text(text, origin=f"{SHIPPED_RULES_PACKAGE}/{SHIPPED_RULES_RESOURCE}")


def _shipped_layer() -> RulesConfig:
    """:func:`load_shipped_rules`, degrading to empty (with one warning) on failure."""
    try:
        return load_shipped_rules()
    except CliError as err:
        logger.warning(
            "shipped rules layer (%s/%s) is unusable, ignoring it: %s",
            SHIPPED_RULES_PACKAGE,
            SHIPPED_RULES_RESOURCE,
            err.message,
        )
        return RulesConfig()


def merge_rules(base: RulesConfig, overlay: RulesConfig) -> RulesConfig:
    """Layer *overlay* over *base*, resolving collisions per RULE ID.

    Precedence rules, all keyed on the rule ``id`` (which is already unique
    across react + inhibit within one layer):

    * an id in BOTH layers → the overlay's entry wins **wholesale**, keeping
      the base's ordering position. Whole-entry replacement, never a
      field-by-field merge: a rule's ``when``/``run``/``params`` are one
      thought, and a half-shipped/half-local hybrid would be a rule nobody
      wrote and no one can reason about. (The overlay may therefore also change
      an id's kind from react to inhibit or back.)
    * an id only in *base* → carried through untouched. This is what lets an
      upgraded box gain newly shipped rules without touching its own file.
    * an id only in *overlay* → appended after the base's entries.
    * an id in ``overlay.disabled`` (an ``enabled = false`` tombstone) → removed
      entirely. A tombstone naming an id no layer defines is inert, not an
      error: a rule REMOVED upstream must not brick a box that had disabled it.

    Modes merge by name (overlay wins per name), and the overlay's
    ``active_mode`` wins only if it selects one.
    """
    overlay_by_id = {rule.id: rule for rule in (*overlay.react, *overlay.inhibit)}
    disabled = base.disabled | overlay.disabled

    ordered: list[Rule] = []
    seen: set[str] = set()
    for rule in (*base.react, *base.inhibit, *overlay.react, *overlay.inhibit):
        if rule.id in seen:
            continue
        seen.add(rule.id)
        winner = overlay_by_id.get(rule.id, rule)
        if winner.id in disabled and winner.id not in overlay_by_id:
            continue
        ordered.append(winner)

    modes = {**base.modes, **overlay.modes}
    active_mode = overlay.active_mode if overlay.active_mode is not None else base.active_mode
    if active_mode is not None and active_mode not in modes:  # pragma: no cover - defensive
        active_mode = None

    return RulesConfig(
        react=tuple(r for r in ordered if r.kind == KIND_REACT),
        inhibit=tuple(r for r in ordered if r.kind == KIND_INHIBIT),
        modes=modes,
        active_mode=active_mode,
        disabled=frozenset(rule_id for rule_id in disabled if rule_id not in overlay_by_id),
        names=_dedupe((*base.names, *overlay.names)),
    )


def load_rules(path: Path | None = None, *, include_shipped: bool = True) -> RulesConfig:
    """Read + validate the rules for this box: shipped layer ⊕ box-local overlay.

    *path* is the OVERLAY (default :func:`default_rules_path`). A MISSING
    overlay is NOT an error — it resolves to the shipped layer alone ("no local
    rules configured yet"). A PRESENT but malformed overlay (bad TOML syntax,
    or content failing :meth:`RulesConfig.from_dict`) raises
    :class:`~reachy.cli._errors.CliError` naming the problem — unchanged, so
    ``behavior rules check`` still reports an operator's typo instead of
    quietly hiding it behind the shipped layer. Degrading to the shipped layer
    on a bad overlay is :class:`RulesLoader`'s job, where "keep serving
    something" is the contract.

    ``include_shipped=False`` reads the overlay alone — for tooling that needs
    to see exactly what the box-local file says.
    """
    base = _shipped_layer() if include_shipped else RulesConfig()

    target = path if path is not None else default_rules_path()
    if not target.is_file():
        return base

    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError as err:
        raise _error(
            f"rules file {target} could not be read: {err}",
            remediation="check file permissions",
        ) from err

    overlay = _parse_rules_text(raw_text, origin=str(target))
    return merge_rules(base, overlay)


class RulesLoader:
    """A stateful :func:`load_rules` wrapper with last-good retention.

    :meth:`reload` re-reads and re-validates BOTH layers (shipped ⊕ overlay).
    On success the merged config becomes :attr:`current` and
    :attr:`last_error` is cleared. On any failure — bad TOML syntax, a
    schema-validation rejection, or an unreadable overlay — :attr:`current` is
    left untouched and :attr:`last_error` records why the candidate was
    rejected. ``reload`` never raises, so a caller (e.g. a live-running engine)
    can poll it on an interval without a momentarily-broken rules file ever
    taking rules away.

    The two-layer model moves the FLOOR of that retention: :attr:`current`
    starts at the SHIPPED layer, not at the all-empty config. So a box whose
    overlay is malformed on the very first load — nothing good to fall back to
    — still runs the shipped rules rather than nothing at all.

    The shipped layer is read once, at construction: it is packaged data that
    cannot change under a running process. The overlay is re-read on every
    :meth:`reload`, which is the thing an operator actually edits live.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else default_rules_path()
        self._shipped: RulesConfig = _shipped_layer()
        self._current: RulesConfig = self._shipped
        self._last_error: str | None = None

    @property
    def shipped(self) -> RulesConfig:
        """The shipped layer alone — this loader's fallback floor."""
        return self._shipped

    @property
    def path(self) -> Path:
        """The rules file this loader reads."""
        return self._path

    @property
    def current(self) -> RulesConfig:
        """The last successfully validated config (the SHIPPED layer until the
        first successful :meth:`reload`)."""
        return self._current

    @property
    def last_error(self) -> str | None:
        """Why the most recent :meth:`reload` candidate was rejected, or ``None``
        if the most recent reload succeeded (or none has been attempted yet)."""
        return self._last_error

    def reload(self) -> RulesConfig:
        """(Re)load both layers, keeping the last-good config on any failure.

        Returns the resulting :attr:`current` config either way — never raises.
        A rejected candidate leaves the previously good merged config in force,
        or — if none was ever good — the shipped layer.
        """
        try:
            candidate = merge_rules(self._shipped, load_rules(self._path, include_shipped=False))
        except CliError as err:
            self._last_error = err.message
            logger.warning(
                "rules reload: keeping last-good config for %s (%s)", self._path, err.message
            )
            return self._current

        self._current = candidate
        self._last_error = None
        return self._current
