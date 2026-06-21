"""Fuzzy name matcher — recognise the robot's name even when STT mishears it.

The robot's name ("reachy") and its generic label ("robot") are short phonetic
words that a speech-to-text model can transcribe as near-homophones: "Richie",
"Reachie", "Richy", etc.  A pure whole-word equality check (used by the
engagement gate before this module) misses every mishearing.

This module provides :func:`is_name_match`, which tokenises an utterance into
words (same ``[A-Za-z]+(?:'[A-Za-z]+)?`` regex the listen/transcribe pipeline
uses) and, for each word, checks whether it is close enough to any of the
robot's canonical names via a combined similarity score:

    score = difflib_ratio(word, name) × length_ratio(word, name)

where ``length_ratio = min(len)/max(len)`` penalises large length mismatches so
that short fragments like "reach" (a prefix of "reachy") or "rich" do not
score above the threshold even when their character-overlap ratio is high.

**Two structural guards supplement the score:**

1. *Prefix guard* — if the word is a strict prefix of a name (e.g. ``"reach"``
   starts ``"reachy"``) the word is treated as a truncation and skipped; a
   truncation is never a mishearing.
2. *Superstring guard* — if any canonical name is a literal substring of the
   word (e.g. ``"reachy"`` ⊂ ``"preachy"``, ``"robot"`` ⊂ ``"robots"``) the
   word is a morphological extension, not a mishearing, and is skipped.
3. *Initial guard* — a *fuzzy* match (not an exact one) must share its first
   letter with the name.  Both canonical names start with ``r``; an STT
   mishearing of "reachy" almost never drops the leading phoneme entirely
   ("richie"/"reachie"/"richy" all start with ``r``), whereas same-length
   non-name homophones that collide on the raw similarity score ("speech",
   "each", "beach") start with a different letter.  This is what keeps the very
   common word "speech" — central to a hearing feature — from false-triggering
   the name path.

**Chosen default threshold: 0.50**

Empirically verified across the required accept/reject table:

  accept  — "reachy"  (1.000), "robot"   (1.000), "reachie" (0.659),
             "richy"   (0.606), "richie"  (0.500)
  reject  — "reach"   (prefix guard), "preachy" (superstring guard),
             "rich"    (0.400), "hello"   (0.200), "robotics" (superstring guard),
             "robots"  (superstring guard), "speech"  (initial guard),
             "each"    (initial guard), "beach"   (initial guard)

0.50 is the tightest value that still accepts "richie" (the farthest-from-name
mishearing in the required table) while keeping "rich" (0.40) below the line;
the initial guard removes the same-length same-score collisions ("speech") that
the threshold alone cannot separate from "richie".
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

# Same word-tokenisation pattern used by listen_transcribe.py
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

#: Default similarity threshold for :func:`is_name_match`.
#:
#: Set to 0.50 — the tightest value that accepts "richie" (score 0.500) against
#: "reachy" while rejecting "rich" (score 0.400).  Callers may lower this to be
#: more permissive or raise it to be stricter.
DEFAULT_THRESHOLD: float = 0.50


def _combined_score(word: str, name: str) -> float:
    """Combined similarity: difflib ratio × length ratio.

    ``difflib.SequenceMatcher.ratio()`` measures character-sequence overlap
    (0..1).  Multiplying by the length ratio (shorter/longer) penalises pairs
    that differ substantially in length, which matters for "rich" (4 chars) vs
    "reachy" (6 chars) — the length penalty pulls the score below 0.50 even
    though the character overlap alone is 0.60.
    """
    seq_ratio = difflib.SequenceMatcher(None, word, name).ratio()
    len_ratio = min(len(word), len(name)) / max(len(word), len(name))
    return seq_ratio * len_ratio


def _word_matches_name(word: str, name: str, threshold: float) -> bool:
    """Whether one tokenised *word* matches one canonical *name*.

    The guard ladder (documented on :func:`is_name_match`): an exact match always
    accepts; the prefix, superstring, and initial guards each reject this pair (so
    the caller moves on to the next name/word); otherwise the combined similarity
    score decides. Factored out of :func:`is_name_match` so the public function
    stays a flat ``any(...)`` over word/name pairs.
    """
    if word == name:
        return True  # exact whole-word match — always accept
    if name.startswith(word):
        return False  # prefix guard: "reach" is a strict prefix of "reachy" → truncation
    if name in word:
        return False  # superstring guard: "reachy" in "preachy" → morphological extension
    # Initial guard: a fuzzy match must share the name's first letter. STT mishearings
    # of "reachy" keep the leading phoneme ("richie", "reachie"); same-length score
    # collisions ("speech") do not. (``name[:1]`` is a safe single-char prefix — "" for
    # an empty name — so ``startswith`` never raises.)
    if not word.startswith(name[:1]):
        return False
    return _combined_score(word, name) >= threshold


def is_name_match(
    text: str,
    names: Iterable[str] = ("reachy", "robot"),
    threshold: float = DEFAULT_THRESHOLD,
) -> bool:
    """Return ``True`` when *text* contains a word that plausibly names the robot.

    The function tokenises *text* into words and, for each word, checks every
    canonical name in *names*.  A word matches when any of the following hold:

    * **Exact match** — the word equals the name (case-insensitive).  Always
      passes regardless of threshold.
    * **Fuzzy match** — after two structural guards (prefix and superstring)
      are applied, the combined similarity score
      ``difflib_ratio × length_ratio`` meets or exceeds *threshold*.

    Structural guards (applied before the fuzzy score):

    * *Prefix guard*: if the word is a strict prefix of the name (e.g.
      ``"reach"`` is a prefix of ``"reachy"``), skip it — it is a truncation.
    * *Superstring guard*: if the name is a literal substring of the word
      (e.g. ``"reachy" ⊂ "preachy"``), skip it — it is a morphological
      extension, not a mishearing.

    Parameters
    ----------
    text:
        The utterance to check (may be a full sentence or a single word).
    names:
        Canonical names to match against.  Defaults to ``("reachy", "robot")``.
        All comparisons are case-insensitive.
    threshold:
        Minimum combined similarity score to accept a fuzzy match.
        Defaults to :data:`DEFAULT_THRESHOLD` (0.50).

    Returns
    -------
    bool
        ``True`` if any word in *text* is an exact or close-enough match for
        any name in *names*.
    """
    words = _WORD_RE.findall(text.lower())
    name_list = [n.lower() for n in names]
    return any(_word_matches_name(word, name, threshold) for word in words for name in name_list)
