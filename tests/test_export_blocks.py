"""Tests for reachy.export.blocks — --export-blocks selection parser.

Covers:
* All valid single and multi-block selections
* Unknown token → CliError (exit-code 1, remediation names valid blocks)
* Empty / whitespace-only input → CliError (exit-code 1)
* Selection.allows() semantics (True for selected, False for others)
* Selection.all() covers every block in BLOCKS
* No bare exceptions / tracebacks escape (only CliError)
"""

from __future__ import annotations

import pytest

from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.export.blocks import BLOCKS, Selection, parse_blocks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_subsets_nonempty(items: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Return every non-empty subset of *items* (2^n - 1 entries)."""
    result = []
    n = len(items)
    for mask in range(1, 1 << n):
        result.append(tuple(items[i] for i in range(n) if mask & (1 << i)))
    return result


# ---------------------------------------------------------------------------
# BLOCKS constant
# ---------------------------------------------------------------------------


def test_blocks_constant_order() -> None:
    assert BLOCKS == ("thinking", "message", "emotion")


# ---------------------------------------------------------------------------
# Valid inputs — all non-empty subsets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subset", _all_subsets_nonempty(BLOCKS))
def test_valid_subset(subset: tuple[str, ...]) -> None:
    csv = ",".join(subset)
    sel = parse_blocks(csv)
    for block in subset:
        assert sel.allows(block), f"expected allows({block!r}) for csv={csv!r}"
    for block in BLOCKS:
        if block not in subset:
            assert not sel.allows(block), f"expected not allows({block!r}) for csv={csv!r}"


def test_whitespace_stripped() -> None:
    sel = parse_blocks("  thinking , message  ,  emotion  ")
    for block in BLOCKS:
        assert sel.allows(block)


def test_duplicates_allowed() -> None:
    sel = parse_blocks("thinking,thinking,message")
    assert sel.allows("thinking")
    assert sel.allows("message")
    assert not sel.allows("emotion")


# ---------------------------------------------------------------------------
# Selection.all()
# ---------------------------------------------------------------------------


def test_selection_all_covers_every_block() -> None:
    sel = Selection.all()
    for block in BLOCKS:
        assert sel.allows(block)


def test_selection_all_rejects_unknown() -> None:
    sel = Selection.all()
    assert not sel.allows("foo")
    assert not sel.allows("")


# ---------------------------------------------------------------------------
# Selection.allows() — direct construction
# ---------------------------------------------------------------------------


def test_allows_single_block() -> None:
    sel = Selection(["thinking"])
    assert sel.allows("thinking")
    assert not sel.allows("message")
    assert not sel.allows("emotion")


def test_allows_two_blocks() -> None:
    sel = Selection(["message", "emotion"])
    assert not sel.allows("thinking")
    assert sel.allows("message")
    assert sel.allows("emotion")


# ---------------------------------------------------------------------------
# Error cases — unknown token
# ---------------------------------------------------------------------------


def test_unknown_token_raises_cli_error() -> None:
    with pytest.raises(CliError) as exc_info:
        parse_blocks("thinking,foo")
    err = exc_info.value
    assert err.code == EXIT_USER_ERROR
    # remediation must mention the valid blocks
    for block in BLOCKS:
        assert block in err.remediation, f"expected {block!r} in remediation"


def test_completely_unknown_raises_cli_error() -> None:
    with pytest.raises(CliError) as exc_info:
        parse_blocks("bogus")
    assert exc_info.value.code == EXIT_USER_ERROR


def test_unknown_token_no_bare_exception() -> None:
    """Ensure only CliError escapes — no ValueError / bare Exception."""
    with pytest.raises(CliError):
        parse_blocks("bad_block")


# ---------------------------------------------------------------------------
# Error cases — empty / whitespace-only input
# ---------------------------------------------------------------------------


def test_empty_string_raises_cli_error() -> None:
    with pytest.raises(CliError) as exc_info:
        parse_blocks("")
    assert exc_info.value.code == EXIT_USER_ERROR


def test_whitespace_only_raises_cli_error() -> None:
    with pytest.raises(CliError) as exc_info:
        parse_blocks("   ")
    assert exc_info.value.code == EXIT_USER_ERROR


def test_empty_has_remediation() -> None:
    with pytest.raises(CliError) as exc_info:
        parse_blocks("")
    err = exc_info.value
    assert err.remediation  # non-empty hint
    for block in BLOCKS:
        assert block in err.remediation


# ---------------------------------------------------------------------------
# No traceback leak — all errors are CliError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_csv", ["", "   ", "foo", "thinking,bar", "THINKING"])
def test_only_cli_error_escapes(bad_csv: str) -> None:
    with pytest.raises(CliError):
        parse_blocks(bad_csv)
