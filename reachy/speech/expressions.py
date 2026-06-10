"""Emoji-keyed expression catalog for Reachy Mini.

Loads ``expressions.toml`` (shipped alongside this module) and provides a
``Catalog`` class and module-level helpers for looking up target poses by
emoji.  The TOML file is the **single source of truth** — a developer can
tune pose values by editing it directly, with no code change required.

TOML format (each entry is a TOML table keyed by emoji string)::

    [neutral]               # required fallback; all fields must be 0.0
    head_x = 0.0            # mm, + forward
    head_y = 0.0            # mm, + right
    head_z = 0.0            # mm, + up
    head_roll = 0.0         # deg, + right-ear lean
    head_pitch = 0.0        # deg, + chin-down
    head_yaw = 0.0          # deg, + right turn
    antenna_right = 0.0     # deg, + forward
    antenna_left = 0.0      # deg, + forward
    body_yaw = 0.0          # deg, + right

    ["🤔"]
    head_roll = 8.0
    head_pitch = 6.0
    ...

Any emoji absent from the file falls back to the ``neutral`` pose.

Module-level convenience API:

* :func:`load_catalog` — load (or reload) from a path and return a raw dict.
* :func:`get_pose` — look up an emoji, falling back to neutral.
* :class:`Catalog` — thin wrapper around the default file that supports
  ``len()``, ``in``, and ``.get()``.
* :data:`NEUTRAL_KEY` — the string key for the neutral/fallback entry.
* :class:`ExpressionPose` — frozen dataclass; fields map 1-to-1 onto
  :class:`~reachy.motion.queue.MotionAction` (mm / degrees).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: The TOML key for the neutral/fallback pose.  Always all-zeros.
NEUTRAL_KEY: str = "neutral"

#: Default data file — same directory as this module.
_DEFAULT_TOML: Path = Path(__file__).parent / "expressions.toml"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_FIELDS = (
    "head_x",
    "head_y",
    "head_z",
    "head_roll",
    "head_pitch",
    "head_yaw",
    "antenna_right",
    "antenna_left",
    "body_yaw",
)


@dataclass(frozen=True)
class ExpressionPose:
    """A target pose for one robot expression.

    All numeric fields use the CLI's friendly units — millimetres for head
    translation axes, degrees for all rotation / antenna axes — matching the
    field convention in :class:`~reachy.motion.queue.MotionAction` and
    :func:`~reachy.motion.idle.neutral_pose`.

    The pose is **frozen** (immutable); construct a new instance to change it.
    """

    head_x: float = 0.0  #: head forward/back offset (mm)
    head_y: float = 0.0  #: head left/right offset   (mm)
    head_z: float = 0.0  #: head up/down offset      (mm)
    head_roll: float = 0.0  #: ear-to-shoulder tilt     (deg)
    head_pitch: float = 0.0  #: chin-up / chin-down nod  (deg)
    head_yaw: float = 0.0  #: left / right turn        (deg)
    antenna_right: float = 0.0  #: right antenna angle      (deg)
    antenna_left: float = 0.0  #: left antenna angle       (deg)
    body_yaw: float = 0.0  #: body rotation            (deg)

    def as_head_dict(self) -> dict[str, float]:
        """Return the head sub-dict expected by ``transport.move_goto``."""
        return {
            "x": self.head_x,
            "y": self.head_y,
            "z": self.head_z,
            "roll": self.head_roll,
            "pitch": self.head_pitch,
            "yaw": self.head_yaw,
        }

    def as_antennas_tuple(self) -> tuple[float, float]:
        """Return ``(right, left)`` degrees — the shape ``move_goto`` expects."""
        return (self.antenna_right, self.antenna_left)


# ---------------------------------------------------------------------------
# Neutral singleton — cached after first construction
# ---------------------------------------------------------------------------

_NEUTRAL = ExpressionPose()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _parse_entry(data: dict[str, object]) -> ExpressionPose:
    """Build an :class:`ExpressionPose` from a raw TOML table dict.

    All nine fields are coerced to ``float`` so callers never get ``int``
    values from TOML (``0`` vs ``0.0`` in the file).
    """
    return ExpressionPose(
        **{field: float(data.get(field, 0.0)) for field in _FIELDS}  # type: ignore[arg-type]
    )


def load_catalog(path: Optional[str] = None) -> dict[str, ExpressionPose]:
    """Load the expression catalog from *path* (default: ``expressions.toml``).

    Each TOML top-level table key becomes the dict key; the value is an
    :class:`ExpressionPose` built from the table's fields.  Unknown fields in
    the TOML table are silently ignored so the file can carry developer notes
    as plain string fields without breaking the loader.

    Args:
        path: Filesystem path to a ``.toml`` file.  ``None`` → the default
              file shipped alongside this module.

    Returns:
        A ``dict[str, ExpressionPose]`` with at least a ``"neutral"`` entry.
        The neutral entry is always present; if absent in the file a
        zero-filled fallback is synthesised.
    """
    toml_path = Path(path) if path is not None else _DEFAULT_TOML
    with open(toml_path, "rb") as fh:
        raw: dict[str, object] = tomllib.load(fh)

    catalog: dict[str, ExpressionPose] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            catalog[key] = _parse_entry(value)

    # Guarantee neutral is always present.
    if NEUTRAL_KEY not in catalog:
        catalog[NEUTRAL_KEY] = _NEUTRAL

    return catalog


# ---------------------------------------------------------------------------
# Module-level convenience helpers
# ---------------------------------------------------------------------------

# Lazily-initialised module-level catalog (loaded on first use).
_DEFAULT_CATALOG: Optional[dict[str, ExpressionPose]] = None


def _ensure_default_catalog() -> dict[str, ExpressionPose]:
    global _DEFAULT_CATALOG
    if _DEFAULT_CATALOG is None:
        _DEFAULT_CATALOG = load_catalog()
    return _DEFAULT_CATALOG


def get_pose(emoji: str) -> ExpressionPose:
    """Return the :class:`ExpressionPose` for *emoji*, or neutral if not found.

    Never raises.  Unknown or empty keys return the neutral (all-zeros) pose.

    Args:
        emoji: An emoji string or any lookup key present in the catalog.

    Returns:
        The matching :class:`ExpressionPose`, or the neutral fallback.
    """
    catalog = _ensure_default_catalog()
    return catalog.get(emoji, catalog[NEUTRAL_KEY])


# ---------------------------------------------------------------------------
# Catalog class
# ---------------------------------------------------------------------------


class Catalog:
    """Thin wrapper around the default expression catalog.

    Loads ``expressions.toml`` on construction and exposes a dict-like
    interface (``len``, ``in``, ``.get``).  Construct a new instance to
    force a reload (e.g. in tests that edit the file).

    Example::

        cat = Catalog()
        pose = cat.get("🤔")
        if "😮" in cat:
            ...
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._data: dict[str, ExpressionPose] = load_catalog(path)

    def get(self, emoji: str) -> ExpressionPose:
        """Return the pose for *emoji*, falling back to neutral."""
        return self._data.get(emoji, self._data[NEUTRAL_KEY])

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, item: object) -> bool:
        return item in self._data

    def __iter__(self):
        return iter(self._data)

    def keys(self):
        return self._data.keys()
