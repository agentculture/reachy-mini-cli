"""The stash record schema — LibraryEntry-shaped declarative data, never code.

A :class:`StashRecord` mirrors :class:`reachy.behavior.library.LibraryEntry`'s shape
but carries no function: it names an existing *generator* template in
:data:`reachy.behavior.library.LIBRARY` (the record *parameterizes* that template)
plus a typed parameter set, the channels it claims (a subset of
:data:`reachy.behavior.model.CHANNELS`), a stop-class, a lifetime, and a
natural-language ``explanation`` — the text embedded for semantic search.

:meth:`StashRecord.from_dict` is the single validation gate. It refuses:

* any field outside the fixed declarative schema (no ``fn``/``code``/``source``/
  ``exec`` etc. — "smells of code");
* any value that is not plain JSON-safe data (a callable/lambda/class instance
  anywhere in the structure);
* a ``generator`` that is not an existing :data:`reachy.behavior.library.LIBRARY`
  entry name;
* a ``channels`` entry outside :data:`reachy.behavior.model.CHANNELS`;
* a ``stop_class`` that is not a valid :class:`reachy.behavior.model.StopClass` value;
* a ``lifetime`` that fails :meth:`reachy.behavior.model.Lifetime.errors`.

Every failure raises :class:`~reachy.cli._errors.CliError` (exit-code 1, user error)
with a specific, actionable message — never a bare ``KeyError``/``TypeError``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from reachy.behavior import library as behavior_library
from reachy.behavior.model import CHANNELS, Lifetime, StopClass
from reachy.cli._errors import EXIT_USER_ERROR, CliError

# The fixed declarative schema — nothing else is allowed at the top level.
_TOP_LEVEL_FIELDS = frozenset(
    {"name", "explanation", "generator", "params", "channels", "stop_class", "lifetime"}
)
# One param entry is exactly {default, unit, help} — mirrors reachy.behavior.library.Param.
_PARAM_FIELDS = frozenset({"default", "unit", "help"})
_LIFETIME_FIELDS = frozenset({"looping", "duration"})

# Plain JSON scalar types. Anything outside (str, list, dict) + these is a code smell
# (a function, a class instance, a lambda, ...).
_JSON_SCALARS = (str, int, float, type(None))

_VALID_CHANNELS = frozenset(CHANNELS)
_VALID_STOP_CLASSES = frozenset(c.value for c in StopClass)


def _error(message: str, remediation: str = "") -> CliError:
    return CliError(code=EXIT_USER_ERROR, message=message, remediation=remediation)


def _reject_code_smell(value: object, *, path: str) -> None:
    """Recursively reject anything that isn't plain JSON-safe declarative data.

    ``bool`` is deliberately excluded from the scalar allowlist for *numeric*
    fields (see :func:`_validate_param`) but is fine as general JSON data here —
    the top-level structural walk only needs to catch non-JSON types (callables,
    class instances, sets, bytes, ...).
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
        "(stash records must contain no code — no functions, lambdas, or objects)",
        remediation="stash records are plain JSON-serializable data only",
    )


def _require_str(data: Mapping, key: str, *, allow_blank: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not allow_blank and not value.strip()):
        raise _error(
            f"stash record field {key!r} must be a non-empty string (got {value!r})",
            remediation=f"provide a string value for {key!r}",
        )
    return value


def _validate_generator(name: str) -> str:
    if name not in behavior_library.LIBRARY:
        raise _error(
            f"stash record generator {name!r} is not a known behavior.library.LIBRARY entry",
            remediation=f"use one of: {', '.join(sorted(behavior_library.LIBRARY))}",
        )
    return name


def _validate_channels(raw: object) -> frozenset[str]:
    if not isinstance(raw, list) or not raw:
        raise _error(
            f"stash record field 'channels' must be a non-empty list of channel names "
            f"(got {raw!r})",
            remediation=f"choose from: {', '.join(sorted(_VALID_CHANNELS))}",
        )
    channels: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or item not in _VALID_CHANNELS:
            raise _error(
                f"stash record has an unknown channel {item!r}",
                remediation=f"channels must be a subset of: {', '.join(sorted(_VALID_CHANNELS))}",
            )
        channels.add(item)
    return frozenset(channels)


def _validate_stop_class(raw: object) -> str:
    if not isinstance(raw, str) or raw not in _VALID_STOP_CLASSES:
        raise _error(
            f"stash record has an unknown stop_class {raw!r}",
            remediation=f"use one of: {', '.join(sorted(_VALID_STOP_CLASSES))}",
        )
    return raw


def _validate_lifetime(raw: object) -> dict:
    if not isinstance(raw, Mapping):
        raise _error(f"stash record field 'lifetime' must be an object (got {raw!r})")
    extra = set(raw) - _LIFETIME_FIELDS
    if extra:
        raise _error(f"stash record 'lifetime' has unexpected field(s): {sorted(extra)}")
    missing = _LIFETIME_FIELDS - set(raw)
    if missing:
        raise _error(f"stash record 'lifetime' is missing field(s): {sorted(missing)}")
    looping = raw["looping"]
    duration = raw["duration"]
    if not isinstance(looping, bool):
        raise _error(f"stash record lifetime.looping must be a bool (got {looping!r})")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, (int, float))
    ):
        raise _error(f"stash record lifetime.duration must be a number or null (got {duration!r})")
    lifetime = Lifetime(looping=looping, duration=float(duration) if duration is not None else None)
    problems = lifetime.errors()
    if problems:
        raise _error(f"stash record has an invalid lifetime: {'; '.join(problems)}")
    return {"looping": looping, "duration": float(duration) if duration is not None else None}


def _validate_param(name: str, raw: object) -> "StashParam":
    if not isinstance(raw, Mapping):
        raise _error(f"stash record param {name!r} must be an object (got {raw!r})")
    extra = set(raw) - _PARAM_FIELDS
    if extra:
        raise _error(
            f"stash record param {name!r} has unexpected field(s) {sorted(extra)} — "
            "params are declarative-only {default, unit, help}, no code",
            remediation="remove any field beyond default/unit/help",
        )
    missing = _PARAM_FIELDS - set(raw)
    if missing:
        raise _error(f"stash record param {name!r} is missing field(s): {sorted(missing)}")
    default = raw["default"]
    if isinstance(default, bool) or not isinstance(default, (int, float)):
        raise _error(f"stash record param {name!r}.default must be a number (got {default!r})")
    unit = raw["unit"]
    if not isinstance(unit, str):
        raise _error(f"stash record param {name!r}.unit must be a string (got {unit!r})")
    help_text = raw["help"]
    if not isinstance(help_text, str):
        raise _error(f"stash record param {name!r}.help must be a string (got {help_text!r})")
    return StashParam(default=float(default), unit=unit, help=help_text)


def _validate_params(raw: object) -> dict[str, "StashParam"]:
    if not isinstance(raw, Mapping):
        raise _error(f"stash record field 'params' must be an object (got {raw!r})")
    return {name: _validate_param(name, value) for name, value in raw.items()}


@dataclass(frozen=True)
class StashParam:
    """One typed, declarative parameter — mirrors :class:`reachy.behavior.library.Param`."""

    default: float
    unit: str
    help: str

    def to_dict(self) -> dict:
        return {"default": self.default, "unit": self.unit, "help": self.help}


@dataclass(frozen=True)
class StashRecord:
    """A validated, LibraryEntry-shaped stash record — pure declarative data.

    Construct via :meth:`from_dict` (never directly) so every instance is
    guaranteed to have passed schema validation. ``explanation`` is the
    natural-language text embedded for semantic search (see
    :meth:`reachy.stash.store.StashStore.add`).
    """

    name: str
    explanation: str
    generator: str
    params: dict[str, StashParam] = field(default_factory=dict)
    channels: frozenset[str] = field(default_factory=frozenset)
    stop_class: str = "stoppable"
    lifetime: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "StashRecord":
        """Validate *data* against the declarative stash schema and build a record.

        Raises :class:`~reachy.cli._errors.CliError` (exit-code 1) with a specific,
        actionable message on anything malformed or smelling of code — never a bare
        ``KeyError``/``TypeError``/``AttributeError``.
        """
        if not isinstance(data, Mapping):
            raise _error(f"a stash record must be a JSON object (got {type(data).__name__!r})")

        unknown = set(data) - _TOP_LEVEL_FIELDS
        if unknown:
            raise _error(
                f"stash record has unexpected field(s) {sorted(unknown)} — stash records are "
                "declarative-only data (no code/source/lambdas/free-form fields)",
                remediation=f"the allowed fields are: {', '.join(sorted(_TOP_LEVEL_FIELDS))}",
            )
        missing = _TOP_LEVEL_FIELDS - set(data)
        if missing:
            raise _error(f"stash record is missing required field(s): {sorted(missing)}")

        # Structural code-smell sweep BEFORE any semantic validation — catches a
        # lambda/callable/class-instance anywhere in the tree with one clean error.
        _reject_code_smell(dict(data), path="record")

        name = _require_str(data, "name")
        explanation = _require_str(data, "explanation")
        generator = _validate_generator(_require_str(data, "generator"))
        params = _validate_params(data["params"])
        channels = _validate_channels(data["channels"])
        stop_class = _validate_stop_class(data["stop_class"])
        lifetime = _validate_lifetime(data["lifetime"])

        return cls(
            name=name,
            explanation=explanation,
            generator=generator,
            params=params,
            channels=channels,
            stop_class=stop_class,
            lifetime=lifetime,
        )

    def to_dict(self) -> dict:
        """Serialize back to plain JSON-safe data (the mirror of :meth:`from_dict`)."""
        return {
            "name": self.name,
            "explanation": self.explanation,
            "generator": self.generator,
            "params": {name: p.to_dict() for name, p in self.params.items()},
            "channels": sorted(self.channels),
            "stop_class": self.stop_class,
            "lifetime": dict(self.lifetime),
        }
