"""Block-type selection parser for the ``--export-blocks`` flag.

Defines the canonical set of exportable block types and provides
:func:`parse_blocks` to turn a comma-separated CLI value into an
immutable :class:`Selection` that other subsystems can query with
:meth:`Selection.allows`.

Usage::

    from reachy.export.blocks import BLOCKS, parse_blocks

    sel = parse_blocks("thinking,emotion")   # from CLI flag value
    if sel.allows("thinking"):
        ...
"""

from __future__ import annotations

from typing import Iterable

from reachy.cli._errors import EXIT_USER_ERROR, CliError

#: Canonical exportable block-type strings, in declaration order.
BLOCKS: tuple[str, ...] = ("thinking", "message", "emotion")

_VALID = frozenset(BLOCKS)
_VALID_HINT = ", ".join(BLOCKS)


class Selection:
    """Immutable holder of a set of selected block-type names.

    Construct directly or via :meth:`all` / :func:`parse_blocks`.

    Args:
        names: Iterable of block-type strings (need not be validated here;
               validation is :func:`parse_blocks`'s responsibility).
    """

    def __init__(self, names: Iterable[str]) -> None:
        self._selected: frozenset[str] = frozenset(names)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def allows(self, block: str) -> bool:
        """Return ``True`` iff *block* is in the selected set."""
        return block in self._selected

    @classmethod
    def all(cls) -> "Selection":
        """Return a :class:`Selection` that includes every block in :data:`BLOCKS`."""
        return cls(BLOCKS)

    # ------------------------------------------------------------------
    # Conveniences
    # ------------------------------------------------------------------

    def __iter__(self):
        return iter(sorted(self._selected))

    def __repr__(self) -> str:  # stable, test-friendly
        joined = ", ".join(sorted(self._selected))
        return f"Selection({{{joined}}})"


def parse_blocks(csv: str) -> Selection:
    """Parse a comma-separated list of block-type names into a :class:`Selection`.

    Args:
        csv: Comma-separated block names, e.g. ``"thinking,emotion"``.
             Leading/trailing whitespace around each token is stripped.
             Duplicate valid tokens are silently deduplicated.

    Returns:
        A :class:`Selection` of the requested block types.

    Raises:
        :class:`reachy.cli._errors.CliError`: exit-code 1 (user error) if
            *csv* is empty/whitespace-only, or any token is not in :data:`BLOCKS`.
    """
    tokens = [t.strip() for t in csv.split(",")]
    # Filter out empty tokens that arise from whitespace-only input
    tokens = [t for t in tokens if t]

    if not tokens:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--export-blocks requires at least one block type",
            remediation=f"valid block types: {_VALID_HINT}",
        )

    unknown = [t for t in tokens if t not in _VALID]
    if unknown:
        bad = ", ".join(repr(u) for u in unknown)
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown block type(s): {bad}",
            remediation=f"valid block types: {_VALID_HINT}",
        )

    return Selection(tokens)
