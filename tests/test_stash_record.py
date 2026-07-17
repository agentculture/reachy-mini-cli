"""Tests for the behavior-stash record schema (:mod:`reachy.stash.record`).

A stash record is DECLARATIVE DATA in the ``LibraryEntry`` mold — never
free-form code. Covers task t4 acceptance criterion 1:

* records are LibraryEntry-shaped: name, explanation, typed params
  (name -> {default, unit, help}), channels (subset of the behavior model's
  channels), stop-class, lifetime, and a generator reference (an existing
  ``reachy.behavior.library.LIBRARY`` entry name);
* schema validation refuses malformed records and anything smelling of code
  (non-declarative fields, callables/lambdas, unknown generator/channel/
  stop-class, invalid lifetime) with a clean, specific error;
* records serialize to/from JSON.
"""

from __future__ import annotations

import json

import pytest

from reachy.cli._errors import CliError
from reachy.stash.record import StashParam, StashRecord

VALID = {
    "name": "gentle-nod",
    "explanation": "A soft, slow nod used to acknowledge that Reachy heard something.",
    "generator": "nod",
    "params": {
        "amp": {"default": 8.0, "unit": "deg", "help": "nod amplitude"},
        "period": {"default": 1.0, "unit": "s", "help": "nod cycle length"},
    },
    "channels": ["head"],
    "stop_class": "stoppable",
    "lifetime": {"looping": True, "duration": None},
}


def _record(**overrides) -> dict:
    data = json.loads(json.dumps(VALID))  # deep copy via JSON round trip
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_record_parses_from_dict():
    record = StashRecord.from_dict(VALID)
    assert record.name == "gentle-nod"
    assert record.generator == "nod"
    assert record.stop_class == "stoppable"
    assert record.channels == frozenset({"head"})
    assert record.params["amp"] == StashParam(default=8.0, unit="deg", help="nod amplitude")
    assert record.lifetime == {"looping": True, "duration": None}


def test_record_round_trips_through_json():
    record = StashRecord.from_dict(VALID)
    blob = json.dumps(record.to_dict())
    restored = StashRecord.from_dict(json.loads(blob))
    assert restored == record


def test_to_dict_is_plain_json_serializable():
    record = StashRecord.from_dict(VALID)
    # Must not raise — every value is a JSON scalar/list/dict.
    json.dumps(record.to_dict())


# ---------------------------------------------------------------------------
# Malformed / code-smell rejection
# ---------------------------------------------------------------------------


def test_unknown_top_level_field_is_rejected():
    data = _record(source="def foo(): pass")
    with pytest.raises(CliError, match="code"):
        StashRecord.from_dict(data)


def test_missing_required_field_is_rejected():
    data = _record()
    del data["explanation"]
    with pytest.raises(CliError, match="explanation"):
        StashRecord.from_dict(data)


def test_lambda_value_is_rejected_as_code_smell():
    data = _record()
    data["params"] = {"amp": {"default": 8.0, "unit": "deg", "help": lambda: 1}}
    with pytest.raises(CliError):
        StashRecord.from_dict(data)


def test_callable_top_level_field_is_rejected():
    data = dict(VALID)
    data["fn"] = lambda t, p, s: None
    with pytest.raises(CliError):
        StashRecord.from_dict(data)


def test_unknown_generator_is_rejected():
    data = _record(generator="does-not-exist-in-library")
    with pytest.raises(CliError, match="generator"):
        StashRecord.from_dict(data)


def test_unknown_channel_is_rejected():
    data = _record(channels=["head", "laser-eyes"])
    with pytest.raises(CliError, match="channel"):
        StashRecord.from_dict(data)


def test_unknown_stop_class_is_rejected():
    data = _record(stop_class="unstoppable-ish")
    with pytest.raises(CliError, match="stop"):
        StashRecord.from_dict(data)


def test_invalid_lifetime_is_rejected():
    # not looping and no duration -> Lifetime.errors() flags it
    data = _record(lifetime={"looping": False, "duration": None})
    with pytest.raises(CliError, match="lifetime"):
        StashRecord.from_dict(data)


def test_param_with_extra_field_is_rejected():
    data = _record()
    data["params"]["amp"]["exec"] = "os.system('rm -rf /')"
    with pytest.raises(CliError):
        StashRecord.from_dict(data)


def test_param_missing_field_is_rejected():
    data = _record()
    del data["params"]["amp"]["unit"]
    with pytest.raises(CliError):
        StashRecord.from_dict(data)


def test_param_default_must_be_numeric_not_bool():
    data = _record()
    data["params"]["amp"]["default"] = True
    with pytest.raises(CliError):
        StashRecord.from_dict(data)


def test_non_string_name_is_rejected():
    data = _record(name=123)
    with pytest.raises(CliError):
        StashRecord.from_dict(data)


def test_empty_explanation_is_rejected():
    data = _record(explanation="   ")
    with pytest.raises(CliError, match="explanation"):
        StashRecord.from_dict(data)


def test_non_mapping_record_is_rejected():
    with pytest.raises(CliError):
        StashRecord.from_dict(["not", "a", "mapping"])


def test_channels_must_be_a_list_not_a_string():
    # A bare string is iterable char-by-char — must not silently "work".
    data = _record(channels="head")
    with pytest.raises(CliError):
        StashRecord.from_dict(data)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
