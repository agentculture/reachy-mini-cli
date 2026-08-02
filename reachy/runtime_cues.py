"""Runtime-event -> perception-cue vocabulary, shared by the two cognition roots.

``agent attach`` (:mod:`reachy.cli._commands.agent`) and the embodiment
layer's cue reader (:mod:`reachy.embody.cues`) each turn the runtime's
exported ``sense``/``rule``/``intent``/``motion`` events
(:mod:`reachy.export.runtime`, ``docs/export-schema.md``'s Runtime Event Feed)
into short first-person perception-cue strings for a tool-use engine or a
duplex turn engine to think about. SonarCloud flagged the two consumers as
duplicated blocks on PR #140 (the ``rule`` mapper, byte-for-byte, and the
``speech``/``pat``/``face`` core of the ``sense`` mapper) — this module is the
ONE owner both now cite, so a future change to the vocabulary cannot land on
only one side and let the two descriptions of the same robot drift apart.

What is genuinely shared, and what stays local to each caller
---------------------------------------------------------------
Every function here is a pure ``dict -> list[str]`` (or narrower) mapping with
no side effects — no logging, no dispatch, no I/O — because the two callers
disagree, **on purpose**, about what happens around the mapping:

* :func:`reachy.embody.cues.cues_for_runtime_event` reports an unrecognised
  ``t`` or a non-dict event as a NAMED :mod:`reachy.senselog` drop (the
  embodiment layer's "no silent no-op" house rule, stated in that module's
  own docstring).
* :func:`reachy.cli._commands.agent._cues_for_runtime_event` stays silent on
  the same inputs (its own docstring: "never raises, so one bad feed line can
  never break the attach loop") — a design that predates the embodiment
  layer, kept as-is here rather than folded into ``cues.py``'s stricter
  behaviour.

That dispatch/observability difference is deliberate and lives in each
caller's own module, not here — this module never imports
:mod:`reachy.senselog`.

One further difference is reported, not flattened: :mod:`reachy.embody.cues`
layers a ``frame_available`` cue (``"a camera frame is available"``) on top
of :func:`sense_cues`'s output; ``agent attach`` does not extend it. Reading
the git history, ``agent attach``'s cue vocabulary (issue #70) predates the
``frame_available`` sense field (issue #75/t13) by several months, while
``reachy/embody/cues.py`` was written after the field existed and documents
the addition deliberately — so this reads as ``agent attach`` never having
been revisited for the newer field, not as an intentional omission. Whoever
owns ``agent attach`` next can decide whether to extend it; this extraction
does not change either caller's observable behaviour.

The two layer-authored families (issue #155)
----------------------------------------------
The runtime publishes four line types (``sense``/``rule``/``intent``/
``motion``). The embodiment layer adds two of its own, and their PHRASING lives
here for the same reason the runtime's does — one owner, so the robot cannot
end up described two ways:

* :data:`LINE_INTERJECTION` — an authorized background source (the worker
  model, a mesh peer, an external system) proposing a sentence for the
  foreground voice. Rendered by :func:`interjection_cue`.
* :data:`LINE_WANTED_TO_SAY` — the measured remainder of a reply a human cut
  off mid-sentence, kept so the next turn can decide whether it is still worth
  saying. Rendered by :func:`wanted_to_say_cue`.

**This extension keeps the closed-vocabulary property, deliberately.** The
value of a closed vocabulary is that equal text means the same fact happened
again, which is what makes the embodiment engine's exact-text coalescing
correct (:meth:`reachy.embody.engine.EmbodyTurnEngine._offer_context`). Both
new families carry FREE TEXT inside a FIXED phrasing, so that direction still
holds: two identical renderings really are the same proposal, or the same
unsaid remainder, arriving twice. What free text costs is the *reverse*
direction — two wordings of one idea no longer coalesce — and that is bounded
here rather than assumed away: an interjection passes a per-source rate bound
before it is ever rendered (:mod:`reachy.embody.interjection`), and a
wanted-to-say artifact is at most one per interrupted response and expires in
turns. A future family whose text is neither closed nor bounded does NOT belong
in a text-keyed park; that is the free-text eviction defect issue #154 names,
and it wants a latest-wins slot instead.

Neither family is produced by the runtime, so neither is a mapper here: they
are constructed in-process by the layer, and :mod:`reachy.embody.cues` refuses
both by name if one ever arrives off the wire.

Import boundary
----------------
This module depends on nothing beyond the standard library (``json``,
``math``). In particular it does NOT import :mod:`reachy.senselog`,
:mod:`reachy.speech`, :mod:`reachy.behavior` or :mod:`reachy.embody` — both
callers need to reach it at module scope (``reachy/embody/cues.py`` always
could; ``reachy/cli/_commands/agent.py`` is forbidden from a module-scope
import of anything under ``reachy.embody``/``reachy.speech``/``reachy.forge``
by ``tests/test_agent_embody.py``'s
``test_no_cognition_or_layer_module_is_imported_at_command_module_scope``),
so this module lives outside all three packages on purpose.
"""

from __future__ import annotations

import json
import math

# ---------------------------------------------------------------------------
# Sound-direction band + loudness threshold + pat phrasing
# ---------------------------------------------------------------------------
# Mirrors reachy.speech.events' DoA convention (0 = left, pi/2 = front, pi =
# right; ~15 degree "ahead" band) and its loud-sound floor — restated here
# rather than imported, since reachy.speech is off the table for both callers
# (see the module docstring's import-boundary note).

AHEAD_BAND_RAD: float = 0.26
LOUD_RMS_THRESHOLD: float = 0.02

#: Touch phrasing — keys match the strings reachy.motion.pat.PatDetector emits.
PAT_KIND_PHRASE: dict[str, str] = {"scratch": "scratch", "side_pat": "sideways nudge"}
PAT_LEVEL_INTENSITY: dict[str, str] = {"level1": "gentle", "level2": "firm"}

# ---------------------------------------------------------------------------
# The two layer-authored line types (issue #155)
# ---------------------------------------------------------------------------
# The ``t`` discriminator each family carries on the wire, named here rather
# than typed as a bare string at each use site — the same discipline the
# embodiment tool registry keeps for its command kinds ("the tool name and the
# command kind must not be two independently drifting strings").

#: An authorized background source proposing a sentence for the foreground voice.
LINE_INTERJECTION = "interjection"
#: The unsaid remainder of a reply a human cut off mid-sentence.
LINE_WANTED_TO_SAY = "wanted_to_say"

#: Both of them, for a caller that needs to ask "is this one of the layer's
#: own families?" without listing them again. Deliberately DISJOINT from the
#: runtime's four line types: nothing here is something the runtime publishes.
EMBODY_LINE_TYPES: tuple[str, ...] = (LINE_INTERJECTION, LINE_WANTED_TO_SAY)


def interjection_cue(text: str, source: str) -> str:
    """The ONE phrasing for a proposed interjection, source named in the line.

    The source is in the rendered text, not only in the event's metadata,
    because the mind reading this line is being asked to say someone else's
    words out loud — "who wants this said" is part of the fact, not a footnote.
    It also keeps two identical suggestions from two different sources distinct
    under exact-text coalescing, which is correct: they are two facts.
    """
    return f'{source} suggests saying: "{text}"'


def wanted_to_say_cue(text: str) -> str:
    """The ONE phrasing for the unsaid remainder of an interrupted reply.

    The interrupted response's id is deliberately NOT in the line: it is
    attribution the artifact carries for the layer's own record, and a hex id
    in the middle of a perception is noise to the model. Two identical
    remainders therefore coalesce with a count, which reads correctly — the
    robot was cut off saying the same thing twice.
    """
    return f'I was interrupted before saying: "{text}"'


def direction_word(doa: object) -> str | None:
    """Map a DoA angle (radians) to ``"left"`` / ``"ahead"`` / ``"right"``, or ``None``.

    Convention: ``0`` = left, ``pi/2`` = front, ``pi`` = right. A ``None`` or
    unparseable angle yields ``None``.
    """
    if doa is None:
        return None
    try:
        angle = float(doa)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    front = math.pi / 2.0
    if angle < front - AHEAD_BAND_RAD:
        return "left"
    if angle > front + AHEAD_BAND_RAD:
        return "right"
    return "ahead"


def is_number(value: object) -> bool:
    """Whether *value* is a real number (excludes ``bool``, a ``int`` subtype)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Runtime-event -> perception-cue mapping (one function per line type)
# ---------------------------------------------------------------------------


def sense_cues(event: dict) -> list[str]:
    """The shared ``sense`` cue core: speech/loud-sound, pat, face.

    Deliberately does NOT cover ``frame_available`` — see the module
    docstring's "what stays local" section.
    :func:`reachy.embody.cues` layers that cue on top of this function's
    output; ``agent attach`` uses this output verbatim.
    """
    cues: list[str] = []
    direction = direction_word(event.get("doa"))
    rms = event.get("rms")
    if event.get("speech"):
        cues.append(f"speech from the {direction}" if direction else "speech nearby")
    elif is_number(rms) and rms >= LOUD_RMS_THRESHOLD:
        cues.append(f"loud sound {direction}" if direction else "loud sound nearby")

    pat = event.get("pat")
    if isinstance(pat, (list, tuple)) and len(pat) == 2:
        phrase = PAT_KIND_PHRASE.get(pat[0])
        intensity = PAT_LEVEL_INTENSITY.get(pat[1])
        if phrase and intensity:
            cues.append(f"felt a {intensity} {phrase} on the head")

    face = event.get("face")
    if isinstance(face, str) and face.strip():
        cues.append(f"saw {face.strip()}")
    return cues


def rule_cues(event: dict) -> list[str]:
    """Cues for a ``rule`` runtime event (a rule fire/suppress decision).

    A ``fire`` never maps to an empty cue: the robot's own react/inhibit
    decision is always worth narrating, whether or not it names a behavior.
    """
    rule = str(event.get("rule") or "a rule")
    action = event.get("action")
    if action == "fire":
        behavior = event.get("behavior")
        disable = event.get("disable") or []
        if behavior:
            return [f"a behavior rule fired ({rule}): now doing {behavior}"]
        if disable:
            joined = ", ".join(str(d) for d in disable)
            return [f"a behavior rule fired ({rule}): stopping {joined}"]
        return [f"a behavior rule fired ({rule})"]
    if action == "suppress":
        return [f"a behavior rule held off ({rule})"]
    return []


def intent_cues(event: dict) -> list[str]:
    """Cues for an ``intent`` runtime event (declare / update / clear).

    ``applied`` / ``blocked`` are the IntentDriver's own status emissions —
    recognised as a runtime event elsewhere, but deliberately silent here (the
    declare/update/clear the status describes already produced its own cue
    moments earlier).
    """
    action = event.get("action")
    name = str(event.get("name") or "").strip()
    if action == "clear":
        return ["a standing intent was cleared"]
    if action in ("declare", "update"):
        verb = "set" if action == "declare" else "updated"
        return [
            f"a standing intent was {verb}: {name}" if name else f"a standing intent was {verb}"
        ]
    return []


def motion_cues(event: dict) -> list[str]:
    """Cues for a ``motion`` runtime event (admit / evict).

    A low-level ``goto`` is not surfaced — it would flood turns with
    keyframe-level noise instead of the higher-level behavior admissions that
    actually matter to a conversation.
    """
    action = event.get("action")
    label = str(event.get("behavior") or "a body behavior")
    if action == "admit":
        return [f"started moving: {label}"]
    if action == "evict":
        return [f"stopped moving: {label}"]
    return []  # a low-level goto keyframe is not surfaced as a cue


def parse_runtime_line(line: str) -> dict | None:
    """Parse one JSONL runtime-feed line into an event dict, or ``None`` for junk/blank."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None
