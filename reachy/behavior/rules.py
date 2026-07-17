"""The behavior-rules schema — data-only react/inhibit rules + modes, never code.

A rules file (``rules.toml``, by default under
``<state_dir>/behavior/rules.toml`` — see :func:`default_rules_path`) has three
sections:

* **react rules** (``[[react]]``) — ``when`` a :class:`Predicate` over the live
  sense snapshot holds, ``run`` a named :data:`reachy.behavior.library.LIBRARY`
  entry, with optional parameter overrides;
* **inhibit rules** (``[[inhibit]]``) — ``when`` a predicate holds, ``disable`` a
  named set of behaviors;
* **modes** (``[modes.<name>]``) — named, purely declarative parameter sets, one
  of which may be selected as the file's ``active_mode``.

Every rule (react or inhibit) is uniquely ``id``-entified and carries
``cooldown_s`` (minimum seconds between firings, default 5.0) and ``hysteresis``
(the anti-flap margin around a threshold, default 0.0) — both validated
``>= 0`` numbers. A :class:`Predicate` is DATA (``field``/``op``/``value``), never
a string of code: ``field`` is one of :data:`SENSE_FIELDS`
(``doa``/``speech``/``rms``/``pat``/``face``) and ``op`` is one of
:data:`COMPARATORS`.

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
* an ``active_mode`` that does not name a defined mode, or defined modes with
  no ``active_mode`` selected.

Every failure raises :class:`~reachy.cli._errors.CliError` (exit-code 1, user
error) with a specific, actionable message — never a bare
``KeyError``/``TypeError``/``tomllib.TOMLDecodeError`` escaping to a caller.
This module deliberately reuses :class:`CliError` rather than introducing a
parallel ``RulesError`` taxonomy, exactly mirroring the stash record's choice.

Loading (:func:`load_rules`, :class:`RulesLoader`) is stdlib-only
(:mod:`tomllib`, read-only in the standard library). A missing rules file is
NOT an error — it resolves to an empty, inert :class:`RulesConfig` ("no rules
configured yet"). :class:`RulesLoader` additionally keeps the *last good*
config in memory: a candidate reload that fails to parse or validate never
clobbers a previously good config, it only records why the candidate was
rejected (:attr:`RulesLoader.last_error`).

This module is intentionally PURE: parsing, validation, and dataclasses only.
It has no engine coupling and no evaluation logic — *interpreting* a
:class:`Predicate` against a live sense reading, applying ``cooldown_s``/
``hysteresis`` timing, and actually running/disabling behaviors is the job of
the (separate, dependent) rules evaluator.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from reachy.behavior import library as behavior_library
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.daemon import state_dir

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Schema constants                                                            #
# --------------------------------------------------------------------------- #

RULES_SUBDIR = "behavior"
RULES_FILENAME = "rules.toml"

#: The Sense-snapshot fields a predicate may test. Deliberately small and
#: hand-picked to match the live perception fields the sense pipeline actually
#: produces today — NOT a placeholder for every attribute a future engine
#: might ever track.
SENSE_FIELDS: frozenset[str] = frozenset({"doa", "speech", "rms", "pat", "face"})

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

_PREDICATE_FIELDS = frozenset({"field", "op", "value"})
_TOP_LEVEL_FIELDS = frozenset({"active_mode", "react", "inhibit", "modes"})
_REACT_FIELDS = frozenset({"id", "when", "run", "params", "cooldown_s", "hysteresis"})
_INHIBIT_FIELDS = frozenset({"id", "when", "disable", "cooldown_s", "hysteresis"})
_REACT_REQUIRED = frozenset({"id", "when", "run"})
_INHIBIT_REQUIRED = frozenset({"id", "when", "disable"})

# Plain JSON scalar types. Anything outside (str, list, dict) + these is a code
# smell (a function, a class instance, a lambda, ...).
_JSON_SCALARS = (str, int, float, type(None))


def default_rules_path() -> Path:
    """The default rules-file location: ``<state_dir>/behavior/rules.toml``."""
    return state_dir() / RULES_SUBDIR / RULES_FILENAME


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
    """

    id: str
    kind: str
    when: Predicate
    cooldown_s: float = DEFAULT_COOLDOWN_S
    hysteresis: float = DEFAULT_HYSTERESIS
    behavior: str | None = None
    params: dict[str, float] = field(default_factory=dict)
    disable: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class RulesConfig:
    """A fully validated rules file: react rules, inhibit rules, and modes.

    Construct via :meth:`from_dict` (never directly) so every instance is
    guaranteed to have passed schema validation. The all-empty default is
    exactly what :func:`load_rules` returns for a not-yet-created rules file —
    "no rules configured" is a valid, inert state, not an error.
    """

    react: tuple[Rule, ...] = ()
    inhibit: tuple[Rule, ...] = ()
    modes: dict[str, Mode] = field(default_factory=dict)
    active_mode: str | None = None

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

        react_rules = tuple(_validate_react_rule(r, index=i) for i, r in enumerate(react_raw))
        inhibit_rules = tuple(_validate_inhibit_rule(r, index=i) for i, r in enumerate(inhibit_raw))

        all_ids = [r.id for r in react_rules] + [r.id for r in inhibit_rules]
        duplicates = sorted({rule_id for rule_id in all_ids if all_ids.count(rule_id) > 1})
        if duplicates:
            raise _error(
                f"rules file has duplicate rule id(s): {duplicates} — every rule id must be "
                "unique across react + inhibit",
                remediation="rename one of the duplicated rules",
            )

        modes = _validate_modes(data.get("modes"))
        active_mode = _validate_active_mode(data.get("active_mode"), modes)

        return cls(react=react_rules, inhibit=inhibit_rules, modes=modes, active_mode=active_mode)


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

    has_value = "value" in raw
    value = raw.get("value")

    if op in _BOOLEAN_OPS:
        if has_value and value is not None:
            raise _error(
                f"{path}.when: op {op!r} takes no 'value' (got {value!r})",
                remediation="remove 'value' for is_true/is_false predicates",
            )
        value = None
    elif op in _ORDERED_OPS or op in _DURATION_OPS:
        if not has_value or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _error(
                f"{path}.when: op {op!r} requires a numeric 'value' (got {value!r})",
                remediation="provide a numeric 'value'",
            )
        if value < 0:
            raise _error(f"{path}.when: 'value' for op {op!r} must be >= 0 (got {value!r})")
        value = float(value)
    else:  # equality ops
        if not has_value:
            raise _error(f"{path}.when: op {op!r} requires a 'value' field")
        if isinstance(value, (dict, list)):
            raise _error(f"{path}.when.value must be a scalar for op {op!r} (got {value!r})")

    return Predicate(field=field_name, op=op, value=value)


def _validate_behavior_name(name: object, *, path: str) -> str:
    if not isinstance(name, str) or name not in behavior_library.LIBRARY:
        raise _error(
            f"{path}: unknown behavior {name!r}",
            remediation=f"use one of: {', '.join(sorted(behavior_library.LIBRARY))}",
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

    return Rule(
        id=rule_id,
        kind=KIND_REACT,
        when=when,
        cooldown_s=cooldown_s,
        hysteresis=hysteresis,
        behavior=behavior_name,
        params=params,
        disable=frozenset(),
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


def load_rules(path: Path | None = None) -> RulesConfig:
    """Read + validate a rules TOML file at *path* (default :func:`default_rules_path`).

    A MISSING file is NOT an error — it resolves to an empty
    :class:`RulesConfig` ("no rules configured yet"). A PRESENT but malformed
    file (bad TOML syntax, or content failing :meth:`RulesConfig.from_dict`)
    raises :class:`~reachy.cli._errors.CliError` naming the problem.
    """
    target = path if path is not None else default_rules_path()
    if not target.is_file():
        return RulesConfig()

    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError as err:
        raise _error(
            f"rules file {target} could not be read: {err}",
            remediation="check file permissions",
        ) from err

    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as err:
        raise _error(
            f"rules file {target} is not valid TOML: {err}",
            remediation="fix the TOML syntax",
        ) from err

    return RulesConfig.from_dict(data)


class RulesLoader:
    """A stateful :func:`load_rules` wrapper with last-good retention.

    :meth:`reload` re-reads and re-validates the rules file. On success the new
    config becomes :attr:`current` and :attr:`last_error` is cleared. On any
    failure — bad TOML syntax, a schema-validation rejection, or an unreadable
    file — :attr:`current` is left untouched (the previously good config, or
    the all-empty default if there has never been a good one yet) and
    :attr:`last_error` records why the candidate was rejected. ``reload`` never
    raises, so a caller (e.g. a live-running engine) can poll it on an interval
    without a momentarily-broken rules file ever taking rules away.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else default_rules_path()
        self._current: RulesConfig = RulesConfig()
        self._last_error: str | None = None

    @property
    def path(self) -> Path:
        """The rules file this loader reads."""
        return self._path

    @property
    def current(self) -> RulesConfig:
        """The last successfully validated config (the all-empty default until
        the first successful :meth:`reload`)."""
        return self._current

    @property
    def last_error(self) -> str | None:
        """Why the most recent :meth:`reload` candidate was rejected, or ``None``
        if the most recent reload succeeded (or none has been attempted yet)."""
        return self._last_error

    def reload(self) -> RulesConfig:
        """(Re)load :attr:`path`, keeping the last-good config on any failure.

        Returns the resulting :attr:`current` config either way — never raises.
        """
        try:
            candidate = load_rules(self._path)
        except CliError as err:
            self._last_error = err.message
            logger.warning(
                "rules reload: keeping last-good config for %s (%s)", self._path, err.message
            )
            return self._current

        self._current = candidate
        self._last_error = None
        return self._current
