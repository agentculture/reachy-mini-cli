"""Distinctness scoring for Reachy Mini expression poses.

Provides a weighted Euclidean distance between two :class:`ExpressionPose`
instances and a catalog scanner that flags expression pairs whose poses are
too similar to be meaningfully distinct.

Weighting rationale
-------------------
The nine pose parameters span two different physical units — millimetres (head
translation axes ``head_x/y/z``) and degrees (head rotation axes
``head_roll/pitch/yaw``, antenna angles, ``body_yaw``).  Without normalisation a
large-scale mm value would dominate degree-scale rotation values even when the
actual robot movement is smaller.  We normalise by a *representative amplitude*
for each axis (the rough half-range at which the robot makes a clearly visible
movement):

+-----------------------+------------------+-----------+
| Axis group            | Amplitude σ      | Weight    |
+=======================+==================+===========+
| head_x, head_y, head_z| 5 mm             | 1/5 = 0.2 |
+-----------------------+------------------+-----------+
| head_roll             | 10 deg           | 1/10 = 0.1|
| head_pitch            | 10 deg           | 1/10 = 0.1|
| head_yaw              | 10 deg           | 1/10 = 0.1|
+-----------------------+------------------+-----------+
| antenna_right         | 30 deg           | 1/30 ≈ 0.033|
| antenna_left          | 30 deg           | 1/30 ≈ 0.033|
+-----------------------+------------------+-----------+
| body_yaw              | 10 deg           | 1/10 = 0.1|
+-----------------------+------------------+-----------+

These are intentionally conservative (not tight per-axis max-range
normalisation) so a deviation that is small in any axis is not over-penalised.
The resulting distance is dimensionless and scale-independent.

Default threshold
-----------------
:data:`DEFAULT_THRESHOLD` is set to ``0.5``.  The shipped starter set's closest
pair sits at ≈0.71 (neutral vs 😐), so the default keeps the starter catalog
clean.  A genuine duplicate (identical or near-identical pose) scores ≈0 and is
comfortably flagged.  Tune this constant — or pass a custom *threshold* argument
to :func:`find_too_similar` — when the expression catalog grows or the robot is
re-calibrated.
"""

from __future__ import annotations

import math

from reachy.speech.expressions import NEUTRAL_KEY, Catalog, ExpressionPose

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Default distance threshold below which two expressions are considered
#: too similar.  See module docstring for the rationale behind this value.
DEFAULT_THRESHOLD: float = 0.5

# ---------------------------------------------------------------------------
# Per-axis weights (order matches ExpressionPose field declaration order)
# ---------------------------------------------------------------------------

# sigma values (representative amplitude, same units as each axis)
_SIGMA_HEAD_TRANSLATION: float = 5.0  # mm
_SIGMA_HEAD_ROTATION: float = 10.0  # deg
_SIGMA_ANTENNA: float = 30.0  # deg
_SIGMA_BODY_YAW: float = 10.0  # deg

_WEIGHTS: tuple[float, ...] = (
    1.0 / _SIGMA_HEAD_TRANSLATION,  # head_x
    1.0 / _SIGMA_HEAD_TRANSLATION,  # head_y
    1.0 / _SIGMA_HEAD_TRANSLATION,  # head_z
    1.0 / _SIGMA_HEAD_ROTATION,  # head_roll
    1.0 / _SIGMA_HEAD_ROTATION,  # head_pitch
    1.0 / _SIGMA_HEAD_ROTATION,  # head_yaw
    1.0 / _SIGMA_ANTENNA,  # antenna_right
    1.0 / _SIGMA_ANTENNA,  # antenna_left
    1.0 / _SIGMA_BODY_YAW,  # body_yaw
)


def _pose_vector(pose: ExpressionPose) -> tuple[float, ...]:
    """Extract the nine pose values as a flat tuple in field-declaration order."""
    return (
        pose.head_x,
        pose.head_y,
        pose.head_z,
        pose.head_roll,
        pose.head_pitch,
        pose.head_yaw,
        pose.antenna_right,
        pose.antenna_left,
        pose.body_yaw,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def distance(a: ExpressionPose, b: ExpressionPose) -> float:
    """Compute a weighted Euclidean distance between two expression poses.

    Each axis is normalised by a representative amplitude (see module docstring
    and :data:`_WEIGHTS`) so that millimetre translation values and degree
    rotation values are compared on a common dimensionless scale.

    The result is always ≥ 0.  Identical poses return 0.0.

    Args:
        a: First expression pose.
        b: Second expression pose.

    Returns:
        A non-negative float representing the normalised distance between the
        two poses.  Smaller values mean more similar poses.
    """
    vec_a = _pose_vector(a)
    vec_b = _pose_vector(b)
    return math.sqrt(sum((w * (ai - bi)) ** 2 for ai, bi, w in zip(vec_a, vec_b, _WEIGHTS)))


def find_too_similar(
    catalog: dict[str, ExpressionPose] | Catalog,
    threshold: float | None = None,
) -> list[tuple[str, str, float]]:
    """Scan a catalog and return expression pairs that are too similar.

    Two expressions are considered *too similar* when their :func:`distance`
    is strictly less than *threshold*.  The neutral entry is **excluded** from
    pairwise comparison because it is the universal fallback/reset pose, not an
    expression in its own right — flagging "neutral looks like X" is noise, not
    a catalogue quality signal.

    Args:
        catalog: Either a raw ``dict[str, ExpressionPose]`` (as returned by
                 :func:`~reachy.speech.expressions.load_catalog`) or a
                 :class:`~reachy.speech.expressions.Catalog` instance.
        threshold: Distance below which a pair is flagged.  ``None`` uses
                   :data:`DEFAULT_THRESHOLD`.

    Returns:
        A list of ``(emoji_a, emoji_b, score)`` triples — one per flagged pair —
        sorted by ascending score (most similar first).  Empty list means all
        expressions are sufficiently distinct.
    """
    if threshold is None:
        threshold = DEFAULT_THRESHOLD

    # Normalise to a plain dict of {emoji: pose}, excluding neutral.
    if isinstance(catalog, Catalog):
        items: dict[str, ExpressionPose] = {
            k: catalog.get(k) for k in catalog.keys() if k != NEUTRAL_KEY
        }
    else:
        items = {k: v for k, v in catalog.items() if k != NEUTRAL_KEY}

    keys = list(items.keys())
    flagged: list[tuple[str, str, float]] = []

    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            key_a, key_b = keys[i], keys[j]
            score = distance(items[key_a], items[key_b])
            if score < threshold:
                flagged.append((key_a, key_b, score))

    flagged.sort(key=lambda t: t[2])
    return flagged
