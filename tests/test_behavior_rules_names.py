"""The ``names`` table in the rules schema — t2 of configurable-robot-names #177.

The shipped pair (``reachy``/``robot``) is spelled ONCE, in code
(:data:`reachy.speech.name_match.SHIPPED_NAMES`), and the rules file may only
EXTEND it. That asymmetry is the whole design: an operator adds the name a peer
harness answers to, and can never take away the names the robot has always
answered to (a rules file that could delete ``reachy`` would produce a robot
that stops responding to its own name after a typo in a TOML list).

``nova`` appears throughout this file as a CONFIGURED value only — the example
of a peer's name an operator writes into their own overlay. It is never a
default, and nothing under ``reachy/`` spells it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reachy.behavior import rules as rules_mod
from reachy.behavior.rules import (
    MAX_CONFIGURED_NAMES,
    MIN_NAME_LENGTH,
    RulesConfig,
    RulesLoader,
    load_rules,
    merge_rules,
)
from reachy.cli._errors import CliError
from reachy.speech.name_match import SHIPPED_NAMES


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "rules.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 1. The shipped pair is the floor, and additions extend it                   #
# --------------------------------------------------------------------------- #


def test_the_default_names_are_exactly_the_shipped_pair() -> None:
    """A config nobody configured still answers to the names it shipped with."""
    assert RulesConfig().names == SHIPPED_NAMES


def test_a_configured_name_extends_the_shipped_pair_in_order(tmp_path: Path) -> None:
    """Shipped names come FIRST, in order, then the operator's additions."""
    path = _write(tmp_path, 'names = ["nova", "Nova", "nova"]\n')
    config = load_rules(path)
    assert config.names == (*SHIPPED_NAMES, "nova")


def test_duplicates_of_the_shipped_pair_are_deduplicated_silently(tmp_path: Path) -> None:
    """Re-stating a shipped name is a no-op, not an error and not a duplicate."""
    path = _write(tmp_path, 'names = ["Reachy", "robot", "nova"]\n')
    assert load_rules(path).names == (*SHIPPED_NAMES, "nova")


def test_a_missing_table_yields_exactly_the_shipped_pair(tmp_path: Path) -> None:
    path = _write(tmp_path, "# no names table here\n")
    assert load_rules(path).names == SHIPPED_NAMES


def test_an_overlay_alone_still_yields_the_shipped_pair(tmp_path: Path) -> None:
    """The CONSTANT is the source of the shipped names, not the shipped TOML.

    ``include_shipped=False`` skips ``default_rules.toml`` entirely, and the
    names must survive that: they are spelled in code precisely so no packaging
    or file-content accident can take them away.
    """
    path = _write(tmp_path, "# an overlay with no names table\n")
    assert load_rules(path, include_shipped=False).names == SHIPPED_NAMES


def test_the_shipped_file_declares_no_names_table() -> None:
    """The shipped layer documents the table; it must not USE it."""
    text = rules_mod.shipped_rules_text() or ""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "names" not in body


def test_no_peer_name_is_spelled_in_the_shipped_layer() -> None:
    text = (rules_mod.shipped_rules_text() or "").lower()
    assert "nova" not in text


# --------------------------------------------------------------------------- #
# 2. Fail-closed validation — the whole file is refused, and the error says    #
#    which entry broke which rule                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("toml_text", "needle", "bound"),
    [
        ('names = ["ab"]\n', "'ab'", str(MIN_NAME_LENGTH)),
        ('names = ["no va"]\n', "'no va'", "letters"),
        ('names = ["nova!"]\n', "'nova!'", "letters"),
        ('names = ["n0va"]\n', "'n0va'", "letters"),
        ('names = [""]\n', "''", "letters"),
        ('names = "nova"\n', "'nova'", "list"),
        ("names = [42]\n", "42", "string"),
    ],
)
def test_a_malformed_names_entry_refuses_the_whole_file(
    tmp_path: Path, toml_text: str, needle: str, bound: str
) -> None:
    path = _write(tmp_path, toml_text)
    with pytest.raises(CliError) as excinfo:
        load_rules(path)
    message = excinfo.value.message
    assert needle in message, message
    assert bound in message, message


def test_more_than_eight_entries_is_refused_naming_the_bound(tmp_path: Path) -> None:
    nine = [f"name{chr(ord('a') + i)}" for i in range(MAX_CONFIGURED_NAMES + 1)]
    path = _write(tmp_path, "names = [" + ", ".join(f'"{n}"' for n in nine) + "]\n")
    with pytest.raises(CliError) as excinfo:
        load_rules(path)
    message = excinfo.value.message
    assert str(MAX_CONFIGURED_NAMES) in message
    assert "9" in message


def test_the_bound_counts_the_table_not_the_result(tmp_path: Path) -> None:
    """Exactly ``MAX_CONFIGURED_NAMES`` entries is accepted (the bound is <=)."""
    eight = [f"name{chr(ord('a') + i)}" for i in range(MAX_CONFIGURED_NAMES)]
    path = _write(tmp_path, "names = [" + ", ".join(f'"{n}"' for n in eight) + "]\n")
    assert load_rules(path).names == (*SHIPPED_NAMES, *eight)


def test_a_nested_structure_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, 'names = [["nova"]]\n')
    with pytest.raises(CliError):
        load_rules(path)


def test_a_bad_names_table_keeps_the_loader_last_good(tmp_path: Path) -> None:
    """``RulesLoader`` degrades exactly as it does for any other bad overlay."""
    path = tmp_path / "rules.toml"
    path.write_text('names = ["nova"]\n', encoding="utf-8")
    loader = RulesLoader(path)
    assert loader.reload().names == (*SHIPPED_NAMES, "nova")

    path.write_text('names = ["n0va"]\n', encoding="utf-8")
    kept = loader.reload()
    assert kept.names == (*SHIPPED_NAMES, "nova")
    assert loader.last_error is not None
    assert "n0va" in loader.last_error


# --------------------------------------------------------------------------- #
# 3. Merging — the union, in order                                            #
# --------------------------------------------------------------------------- #


def test_merge_unions_names_shipped_first_then_base_then_overlay() -> None:
    base = RulesConfig.from_dict({"names": ["alpha"]})
    overlay = RulesConfig.from_dict({"names": ["beta", "alpha"]})
    assert merge_rules(base, overlay).names == (*SHIPPED_NAMES, "alpha", "beta")


def test_merge_with_an_unconfigured_overlay_keeps_the_base_additions() -> None:
    base = RulesConfig.from_dict({"names": ["alpha"]})
    assert merge_rules(base, RulesConfig()).names == (*SHIPPED_NAMES, "alpha")


# --------------------------------------------------------------------------- #
# 4. The predicate field a later task wires                                    #
# --------------------------------------------------------------------------- #


def test_name_mentioned_is_a_schema_accepted_predicate_field() -> None:
    assert "name_mentioned" in rules_mod.SENSE_FIELDS


def test_a_rule_may_key_on_name_mentioned() -> None:
    config = RulesConfig.from_dict(
        {
            "react": [
                {
                    "id": "answer-to-my-name",
                    "when": {"field": "name_mentioned", "op": "is_true"},
                    "run": "nod",
                    "duration_s": 2.0,
                }
            ]
        }
    )
    assert config.react[0].when.field == "name_mentioned"


# --------------------------------------------------------------------------- #
# 5. The names are spelled in ONE place                                        #
# --------------------------------------------------------------------------- #


def test_rules_does_not_respell_the_shipped_pair() -> None:
    source = Path(rules_mod.__file__).read_text(encoding="utf-8")
    assert '"reachy", "robot"' not in source
    assert "nova" not in source.lower()
