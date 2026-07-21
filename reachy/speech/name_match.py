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

**Four structural guards supplement the score:**

1. *Prefix guard* — if the word is a strict prefix of a name (e.g. ``"reach"``
   starts ``"reachy"``) the word is treated as a truncation and skipped; a
   truncation is never a mishearing.
2. *Superstring guard* — if any canonical name is a literal substring of the
   word (e.g. ``"reachy"`` ⊂ ``"preachy"``, ``"robot"`` ⊂ ``"robots"``) the
   word is a morphological extension, not a mishearing, and is skipped.
3. *Initial guard* — a *fuzzy* match (not an exact one) must share its first
   letter with the name.  Both canonical names start with ``r``; an STT
   mishearing of "reachy" almost never drops the leading phoneme entirely
   ("richie"/"reachie"/"richy" all start with ``r``), whereas non-name
   homophones that collide on the raw similarity score ("speech", "each",
   "beach") start with a different letter.  Kept as a cheap early-out; the
   phonetic guard below now strictly subsumes it (a Soundex code leads with the
   literal first letter).
4. *Phonetic guard* (issue #104) — a fuzzy match must agree with the name on a
   **Soundex consonant skeleton** (:func:`_phonetic_code`).  See below.

**Why the phonetic guard exists — the recurring collision class (#104)**

Guards 1-3 are *orthographic*: they ask "does this word look like the name?".
They are pairwise-blind to the fact that a word can look like the name while
being an ordinary English word that nobody would mishear for it.  Because
``difflib``'s ratio rewards any shared characters in order, and because both
canonical names start with the very common initial ``r``, a large family of
everyday words cleared the whole ladder on the deployed robot::

    really 0.667   ready  0.606   rachel 0.667   root   0.711 (vs "robot")
    reality 0.527  reader 0.500   room   0.533   robust 0.606 (vs "robot")
    reason 0.500   recent 0.500   record 0.500   route  0.600 (vs "robot")

``"That was really good."`` engaged the robot mid-conversation on 2026-07-21
with nobody addressing it.  The name path is the *fast* path — it engages with
**zero classifier calls** — so a false positive here has no second opinion to
catch it, and (before the sibling #105 fix) it also seeded the classifier's
conversation context, making the next false accept more likely.

A stoplist of common words was considered and rejected: the table above was
found by sweeping a few dozen words, the leak is open-ended, and a stoplist
would have to grow forever — exactly the whack-a-mole this defect class already
demonstrates.

The discriminating signal is not orthographic but **phonetic**.  An STT
mishearing is by construction a *phonetic* confusion, so it preserves the
name's consonant skeleton:

    reachy richie reachie richy reechy rachy reachee reachi richey  → all R200
    robot  robbot                                                   → both R130

while the false positives lose it, because they are different words that merely
share letters:

    really R400   reality R430   ready R300   reader R360   reason R250
    recent R253   record R263    rachel R240
    room   R500   robe    R100   route R300   root   R300   robust R123

So the guard is: *a fuzzy match must sound like the name, not merely look like
it.*  Soundex is chosen because it is the standard, well-understood algorithm
for exactly this job (name matching under phonetic mis-transcription), and it
is ~20 lines of pure stdlib — this module still adds no dependency.

The guard is deliberately a **filter, not a replacement**: it is coarse enough
that "rich" (R200) and "ricky" (R200) also share "reachy"'s code, and those are
still rejected by the similarity threshold.  Soundex says "could plausibly be
heard as"; the score still says "is close enough to actually be".

**Chosen default threshold: 0.50**

Empirically verified across the required accept/reject table:

  accept  — "reachy"  (1.000), "robot"   (1.000), "reachie" (0.659),
             "richy"   (0.606), "richie"  (0.500)
  reject  — "reach"   (prefix guard), "preachy" (superstring guard),
             "rich"    (0.400), "hello"   (0.200), "robotics" (superstring guard),
             "robots"  (superstring guard), "speech"  (initial guard),
             "each"    (initial guard), "beach"   (initial guard),
             "really"  (phonetic guard), "reality" (phonetic guard)

0.50 is the tightest value that still accepts "richie" (the farthest-from-name
mishearing in the required table) while keeping "rich" (0.40) below the line;
the initial guard removes the same-length same-score collisions ("speech") that
the threshold alone cannot separate from "richie"; the phonetic guard removes
the common-word collisions ("really") that score *above* "richie" and which no
orthographic guard can separate from it.
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


#: Soundex consonant classes.  Letters absent from this map (the vowels plus
#: ``h``/``w``/``y``) carry no code of their own — see :func:`_phonetic_code`.
_SOUNDEX_CODES: dict[str, str] = {
    **dict.fromkeys("bfpv", "1"),
    **dict.fromkeys("cgjkqsxz", "2"),
    **dict.fromkeys("dt", "3"),
    "l": "4",
    **dict.fromkeys("mn", "5"),
    "r": "6",
}

#: Letters that SEPARATE two same-coded consonants, so both are coded
#: ("record" → r, c=2, r=6, d=3 → R263).  Standard Soundex treats ``y`` as a
#: vowel here.
_SOUNDEX_VOWELS = frozenset("aeiouy")

#: Letters that are TRANSPARENT: they neither carry a code nor break a run of
#: same-coded consonants.  This is the rule that makes "reachy" and "richie"
#: agree — the ``h`` after ``c`` is skipped without resetting the run.
_SOUNDEX_TRANSPARENT = frozenset("hw")

#: Number of consonant digits retained after the leading letter (standard
#: Soundex is a 4-character code: one letter plus three digits).
_SOUNDEX_DIGITS = 3


def _phonetic_code(word: str) -> str:
    """The word's Soundex code — its leading letter plus a consonant skeleton.

    Standard American Soundex: keep the first letter, map each following
    consonant to its class digit, collapse runs of the same digit (``h``/``w``
    are transparent and do not break a run; vowels do), and pad or truncate to
    four characters.

    Two words share a code when they share a *pronunciation shape*, which is
    precisely the relation an STT mishearing preserves and an orthographic
    look-alike does not::

        _phonetic_code("reachy")  == _phonetic_code("richie")  == "r200"
        _phonetic_code("really")  == "r400"   # ≠ "r200"
        _phonetic_code("robot")   == _phonetic_code("robbot")  == "r130"
        _phonetic_code("root")    == "r300"   # ≠ "r130"

    Returns ``""`` for a word with no letters, which can never equal a real
    name's code.  Never raises.
    """
    letters = [ch for ch in word if ch.isalpha()]
    if not letters:
        return ""
    first = letters[0]
    digits: list[str] = []
    # Seed with the first letter's own class so an immediately-following letter
    # of the same class is collapsed into it ("robbot" → one '1', not two).
    previous = _SOUNDEX_CODES.get(first, "")
    for char in letters[1:]:
        code = _SOUNDEX_CODES.get(char, "")
        if code:
            if code != previous:
                digits.append(code)
                if len(digits) == _SOUNDEX_DIGITS:
                    break
            previous = code
        elif char in _SOUNDEX_VOWELS:
            previous = ""  # a vowel breaks the run; h/w leave `previous` alone
    return (first + "".join(digits)).ljust(1 + _SOUNDEX_DIGITS, "0")


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
    # Phonetic guard (#104): a fuzzy match must SOUND like the name, not merely
    # look like it. "really"/"reality"/"root" all clear the guards above and the
    # score, but their consonant skeletons (R400/R430/R300) differ from the
    # name's — they are ordinary words, not mis-transcriptions. See the module
    # docstring for the measured collision table this closes.
    if _phonetic_code(word) != _phonetic_code(name):
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
