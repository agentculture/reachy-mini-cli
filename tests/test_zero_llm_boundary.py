"""Machine-check the zero-LLM property of the presence runtime (task t24).

The whole retire-the-old-AI-first-flow arc rests on one claim::

    reachy-mini-cli is a symbolic robot runtime: the robot's presence is the
    behavior engine driven by rules and configuration, not a hardcoded AI app.

Every deletion in the arc (t20's ``think`` noun, t21's ``listen --live`` root
and cognition engine, t22's ``listen`` noun) was justified by that claim. This
module turns the claim into something a machine enforces, so it cannot silently
regress into a comment that used to be true.

What is checked, and what is deliberately NOT
=============================================

The AI is not banished from the repo. ``agent attach`` survives on purpose
(issue #70 asks to DEMOTE cognition to an optional, external, high-level
orchestrator — not to delete it), and it legitimately reaches
:mod:`reachy.speech.agent_turn` and :mod:`reachy.speech.llm`. What must be
LLM-free is the **presence runtime**: everything reachable from ``behavior
engine run``.

Nor is "no ``reachy.speech`` import anywhere in ``reachy/behavior/``" the right
line to draw, even though the arc's spec (claim ``s1``) recorded a clean grep at
spec time. Two capabilities were deliberately PORTED into the runtime after that
snapshot was taken:

* **t6 gave the runtime a voice** — :mod:`reachy.behavior.speech_act` imports
  :mod:`reachy.speech.harmonic` / :mod:`~reachy.speech.voice` /
  :mod:`~reachy.speech.playback`.
* **t11 gave the runtime ears** — :mod:`reachy.behavior.transcript_sense`
  imports :mod:`reachy.speech.realtime` / :mod:`~reachy.speech.events` /
  :mod:`~reachy.speech.engagement`. (It imported :mod:`reachy.speech.stt` until
  the realtime arc moved endpointing to the server; the HTTP transcriber now
  has no importer inside ``reachy/behavior/`` at all.)

A test that banned all of ``reachy.speech`` would fail on shipped, intended
code. So the boundary below forbids what actually matters — a **language
model**: the LLM client, any in-repo cognition engine, the forge's coder-model
dispatcher, and any third-party LLM SDK — and permits the **synthesis /
playback / transcription** leg through a small, explicitly justified allow-list
(:data:`_BEHAVIOR_SPEECH_ALLOW`). Text-to-speech and speech-to-text are signal
processing with an HTTP hop; they decide nothing about what the robot does.

The one residual LLM edge, pinned rather than hidden
====================================================

:mod:`reachy.speech.engagement` — the #54/#56 layered admission gate that t11's
plan entry explicitly told us to keep — contains a single-shot
"is this utterance addressed to me?" classifier backed by
:func:`reachy.speech.llm.complete`, and therefore ``import``\\ s the LLM client
at module scope. It is reachable from ``behavior engine run``.

That edge is real and it is reported, not papered over. It is bounded in four
ways that keep it out of the decision loop, and every one of those bounds is
asserted below:

1. It is reachable only through the composition root and the transcript sense —
   the engine core (engine / rule engine / intents / arbitration / goto lane /
   pat sense) reaches **nothing** in :mod:`reachy.speech` transitively.
2. It judges only *admission* of heard words. No motion, rule, arbitration or
   pose decision consults it.
3. It is optional: ``REACHY_ENGAGE_HEURISTIC`` skips building it entirely, a
   build fault falls back to the heuristic, and a raising classifier DEGRADEs.
4. It runs on the transcript worker thread, never on the 20 ms tick.

:func:`test_the_only_llm_edge_in_the_presence_runtime_is_the_engagement_gate`
pins that set by **equality**, so it fails in both directions: a new LLM
importer fails it, and so does removing this one — at which point the right move
is to tighten the expectation to the empty set and delete the exception, not to
loosen anything.
"""

from __future__ import annotations

import ast
import collections
import json
import subprocess  # nosec B404 — fixed argv, sys.executable, never shell=True
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_ROOT = _REPO_ROOT / "reachy"
_MOTION_DIR = _PKG_ROOT / "motion"

#: The composition root of the presence runtime: ``behavior engine run`` lives
#: here, so its transitive import closure IS "everything the runtime can load".
_RUNTIME_ROOT = "reachy.cli._commands.behavior"

#: The engine's decision loop — the modules that choose what the robot does on
#: any given tick. These must reach NO speech/vision/forge module at all, which
#: is a strictly stronger statement than the allow-list below.
_ENGINE_CORE = (
    "reachy.behavior.engine",
    "reachy.behavior.rule_engine",
    "reachy.behavior.rules",
    "reachy.behavior.intents",
    "reachy.behavior.control",
    "reachy.behavior.arbitration",
    "reachy.behavior.goto_lane",
    "reachy.behavior.goto_intent",
    "reachy.behavior.pat_sense",
    "reachy.behavior.sense",
    "reachy.behavior.library",
    "reachy.behavior.model",
)

# --------------------------------------------------------------------------- #
# What counts as "an LLM"                                                     #
# --------------------------------------------------------------------------- #

#: The repo's one LLM client: streaming + single-shot chat completions against
#: an OpenAI-compatible endpoint.
_LLM_CLIENT = "reachy.speech.llm"

#: In-repo modules that put a model in charge of what happens next. These are
#: the ``agent attach`` surface (kept deliberately, issue #70) and the forge's
#: coder-model dispatcher — neither may be reachable from the presence runtime.
_COGNITION_MODULES = (
    "reachy.speech.llm",  # the client itself
    "reachy.speech.agent_turn",  # AgentTurnEngine — the tool-use turn loop
    "reachy.speech.tools",  # the tool surface a model calls through
    "reachy.speech.intent_tools",  # runtime intents published TO a model
    "reachy.forge",  # qwen3 self-extension: a model writing new skills
)

#: Third-party LLM SDKs. None is a dependency of this project today (see
#: ``tests/test_dependencies.py``); this list makes "someone pip-installs one
#: and imports it into the runtime" a test failure rather than a review miss.
_THIRD_PARTY_LLM_PACKAGES = (
    "openai",
    "anthropic",
    "langchain",
    "langchain_openai",
    "langchain_core",
    "litellm",
    "llama_cpp",
    "ollama",
    "transformers",
    "sentence_transformers",
    "huggingface_hub",
    "google.generativeai",
    "google.genai",
    "mistralai",
    "cohere",
    "vllm",
    "boto3",  # the Bedrock route a sibling project uses
)

# --------------------------------------------------------------------------- #
# The allow-list: what reachy/behavior/ MAY take from reachy.speech, and why   #
# --------------------------------------------------------------------------- #

#: Every ``reachy.speech.*`` submodule ``reachy/behavior/`` is permitted to
#: import, mapped to the reason it is not a language model. Anything else is a
#: test failure. Entries are load-bearing documentation: adding one is a
#: deliberate act that must be justified here, in this table, in the same change.
_BEHAVIOR_SPEECH_ALLOW = {
    # ---- the runtime's VOICE (ported by t6) — synthesis + playback ----------
    "reachy.speech.harmonic": (
        "the in-process, offline note-melody synth that is the runtime's SHIPPED "
        "default voice; pure DSP over harmonics-cli, no network and no model"
    ),
    "reachy.speech.voice": (
        "the VoiceEngine enum + resolver picking WHICH synth callable to use; a "
        "pure selection helper that its own docstring states imports no LLM"
    ),
    "reachy.speech.playback": (
        "pushes finished PCM at the speaker over the sdk/http transport; audio "
        "output only, decides nothing"
    ),
    # ---- the runtime's HEARING (ported by t11) — words in, admission ---------
    "reachy.speech.realtime": (
        "the lobes /v1/realtime session client: a microphone on a WebSocket. "
        "Speech-to-TEXT with the VAD upstream — it reports what was said and "
        "when it arrived, and decides nothing. It reaches no model: its only "
        "imports are senselog, cli._errors, robot.audio_shape, realtime_wire, "
        "numpy and the stdlib. It REPLACED the reachy.speech.stt entry when the "
        "capture half moved to server-side endpointing (issue #115) — nothing "
        "in reachy/behavior/ imports the HTTP transcriber any more, though "
        "reachy/sleep/wakeword.py still does, outside this boundary"
    ),
    "reachy.speech.events": (
        "``_doa_direction`` only — a pure bearing-to-'from the left' formatter"
    ),
    "reachy.speech.engagement": (
        "the #54/#56 layered addressed-vs-ambient admission gate, kept by plan "
        "task t11. THIS IS THE ONE ENTRY THAT REACHES AN LLM (its optional "
        "single-shot classifier calls reachy.speech.llm.complete). It is "
        "optional, off-tick, fail-open to a heuristic, and gates only whether "
        "heard words enter the sense snapshot — never what the robot does with "
        "them. See this module's docstring and the equality pin in "
        "test_the_only_llm_edge_in_the_presence_runtime_is_the_engagement_gate"
    ),
}

#: The single expected LLM importer in the runtime's closure. Pinned by equality
#: (see the module docstring) so both a new edge AND the removal of this one are
#: loud failures.
_EXPECTED_LLM_IMPORTERS = {"reachy.speech.engagement"}

# --------------------------------------------------------------------------- #
# AST helpers — never grep. `import x.y`, `import x.y as z`, `from x import y`,#
# function-local and TYPE_CHECKING-guarded forms all count.                   #
# --------------------------------------------------------------------------- #


def _module_name(path: Path) -> str:
    """``reachy/behavior/engine.py`` -> ``reachy.behavior.engine``."""
    parts = list(path.relative_to(_REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _source_modules() -> dict[str, Path]:
    """Every importable module in the ``reachy`` package, by dotted name."""
    return {_module_name(p): p for p in sorted(_PKG_ROOT.rglob("*.py"))}


def _imported_names(path: Path, dotted: str) -> set[str]:
    """Every dotted name *path* imports, in any syntactic form.

    ``ast.walk`` covers nested (function-local, class-body, ``if TYPE_CHECKING``)
    imports as well as module scope — a lazy import is still an import edge, and
    the runtime's own speech/playback import is exactly that shape.

    ``from a.b import c`` contributes BOTH ``a.b`` and ``a.b.c``, so a
    ``from reachy.speech import llm`` reads as an ``reachy.speech.llm`` edge and
    cannot slip past a check written against the submodule's full name.
    Relative imports are resolved against *dotted*'s own package.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: `from . import x` / `from ..pkg import y`
                base = dotted.split(".")[: -node.level] or []
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if not module:
                continue
            names.add(module)
            for alias in node.names:
                names.add(f"{module}.{alias.name}")
    return names


def _import_graph() -> dict[str, set[str]]:
    return {name: _imported_names(path, name) for name, path in _source_modules().items()}


def _resolve(dotted: str, modules: dict[str, Path]) -> str | None:
    """Map an imported dotted name onto the repo module that provides it.

    ``reachy.speech.engagement.ConversationGate`` -> ``reachy.speech.engagement``
    (walk up until a real module is found); a non-repo name -> ``None``.
    """
    candidate = dotted
    while candidate and candidate not in modules:
        candidate = candidate.rsplit(".", 1)[0] if "." in candidate else ""
    return candidate or None


def _reachable_from(start: str) -> dict[str, str | None]:
    """BFS the static import closure of *start*; module -> the module that pulled it."""
    modules = _source_modules()
    graph = _import_graph()
    assert start in modules, f"{start} is not a module of this repo"

    parents: dict[str, str | None] = {start: None}
    queue = collections.deque([start])
    while queue:
        current = queue.popleft()
        for dep in graph.get(current, ()):
            if not dep.startswith("reachy"):
                continue
            resolved = _resolve(dep, modules)
            if resolved is None or resolved in parents:
                continue
            parents[resolved] = current
            queue.append(resolved)
    return parents


def _chain(target: str, parents: dict[str, str | None]) -> str:
    """A human-readable ``a -> b -> c`` import path to *target*, for failure text."""
    hops = [target]
    while parents.get(hops[-1]) is not None:
        hops.append(parents[hops[-1]])  # type: ignore[arg-type]
    return " -> ".join(reversed(hops))


def _matches(name: str, forbidden: str) -> bool:
    """``name`` is ``forbidden`` or lives underneath it."""
    return name == forbidden or name.startswith(forbidden + ".")


def _behavior_modules() -> dict[str, Path]:
    return {n: p for n, p in _source_modules().items() if n.startswith("reachy.behavior.")}


def _pulls_into_sys_modules(dotted: str, forbidden: str) -> bool:
    """True if importing *dotted* in a FRESH interpreter loads *forbidden*.

    Subprocess, never in-process: evicting/re-importing modules in this
    interpreter splits module identity and breaks unrelated suites (the lesson
    ``tests/test_sleep_boundary.py`` records).
    """
    code = f"import sys; import {dotted}; print({forbidden!r} in sys.modules)"
    proc = subprocess.run(  # nosec B603 — fixed argv, sys.executable, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    return proc.stdout.strip() == "True"


# --------------------------------------------------------------------------- #
# 0. Vacuity guards — a test that passes because it scanned nothing is worse   #
#    than no test at all.                                                     #
# --------------------------------------------------------------------------- #


def test_the_scan_actually_has_modules_to_scan() -> None:
    """Fail loudly if a refactor empties the sets every check below iterates."""
    modules = _source_modules()
    assert len(modules) > 100, f"only {len(modules)} reachy modules found — scan is broken"
    behavior = _behavior_modules()
    assert len(behavior) >= 25, f"only {len(behavior)} reachy.behavior modules found"
    closure = _reachable_from(_RUNTIME_ROOT)
    assert len(closure) >= 40, f"runtime closure collapsed to {len(closure)} modules"


def test_the_llm_client_and_an_agent_engine_still_exist_to_be_excluded() -> None:
    """The forbidden things must be REAL, or every ban below bans nothing.

    ``agent attach`` keeps cognition alive on purpose (#70). If these modules
    ever go away the bans become vacuous, and this test says so instead of
    quietly going green.
    """
    modules = _source_modules()
    for name in ("reachy.speech.llm", "reachy.speech.agent_turn", "reachy.speech.tools"):
        assert name in modules, (
            f"{name} no longer exists — the zero-LLM bans below are now vacuous. "
            "Either restore the module or delete the ban and say so in the docs."
        )


# --------------------------------------------------------------------------- #
# 1. reachy/behavior/ imports no LLM — and only the allow-listed speech leg    #
# --------------------------------------------------------------------------- #


def test_no_behavior_module_imports_an_llm_or_cognition_engine() -> None:
    """Criterion 1: the LLM ban over the whole ``reachy/behavior/`` package."""
    offences: list[str] = []
    for name, path in _behavior_modules().items():
        for imported in sorted(_imported_names(path, name)):
            for forbidden in _COGNITION_MODULES:
                if _matches(imported, forbidden):
                    offences.append(f"{name} imports {imported}")
    assert not offences, (
        "the presence runtime gained a language model:\n  "
        + "\n  ".join(offences)
        + "\nreachy/behavior/ is symbolic — rules and configuration decide what "
        "the robot does. Cognition belongs behind `agent attach`, external and "
        "optional (issue #70)."
    )


def test_no_behavior_module_imports_a_third_party_llm_sdk() -> None:
    """An LLM smuggled in as a vendor SDK is the same defect wearing a hat."""
    offences: list[str] = []
    for name, path in _behavior_modules().items():
        for imported in sorted(_imported_names(path, name)):
            for package in _THIRD_PARTY_LLM_PACKAGES:
                if _matches(imported, package):
                    offences.append(f"{name} imports {imported}")
    assert not offences, "third-party LLM SDK in reachy/behavior/:\n  " + "\n  ".join(offences)


def test_behavior_speech_imports_are_confined_to_the_allow_list() -> None:
    """Every ``reachy.speech.*`` edge out of ``reachy/behavior/`` is justified.

    The allow-list is not a loosening of the LLM ban above — it is the honest
    statement of which non-model parts of the speech package the runtime owns:
    a voice (t6) and ears (t11). See this module's docstring.
    """
    unexpected: list[str] = []
    for name, path in _behavior_modules().items():
        for imported in sorted(_imported_names(path, name)):
            if not imported.startswith("reachy.speech"):
                continue
            resolved = _resolve(imported, _source_modules())
            if resolved in (None, "reachy.speech"):  # the bare package: no content
                continue
            if resolved not in _BEHAVIOR_SPEECH_ALLOW:
                unexpected.append(f"{name} imports {resolved}")
    assert not unexpected, (
        "reachy/behavior/ reached into reachy.speech outside the allow-list:\n  "
        + "\n  ".join(unexpected)
        + "\nIf the new edge is genuinely not a language model, add it to "
        "_BEHAVIOR_SPEECH_ALLOW with the reason. If it is, it does not belong "
        "in the presence runtime."
    )


def test_the_speech_allow_list_has_no_dead_entries() -> None:
    """An allow-list that outlives its use quietly re-widens the boundary.

    Every entry must correspond to an edge that actually exists today, so the
    table stays a description of the code rather than a wish about it.
    """
    used: set[str] = set()
    modules = _source_modules()
    for name, path in _behavior_modules().items():
        for imported in _imported_names(path, name):
            resolved = _resolve(imported, modules)
            if resolved in _BEHAVIOR_SPEECH_ALLOW:
                used.add(resolved)
    dead = sorted(set(_BEHAVIOR_SPEECH_ALLOW) - used)
    assert not dead, (
        f"_BEHAVIOR_SPEECH_ALLOW permits {dead} but nothing in reachy/behavior/ "
        "imports them any more — delete the entries so the boundary stays tight."
    )


def test_only_the_voice_and_hearing_modules_touch_the_speech_package() -> None:
    """Name the two doors, so a third one opening is visible in the diff.

    28 of the 30 ``reachy/behavior/`` modules import nothing from
    :mod:`reachy.speech` at all. Pinning the two that do keeps "the runtime has
    a voice and ears" a bounded claim rather than a spreading one.
    """
    doors = {
        name
        for name, path in _behavior_modules().items()
        if any(imported.startswith("reachy.speech") for imported in _imported_names(path, name))
    }
    assert doors == {
        "reachy.behavior.speech_act",  # the voice (t6)
        "reachy.behavior.transcript_sense",  # the ears (t11)
    }, f"unexpected reachy/behavior/ modules reaching into reachy.speech: {sorted(doors)}"


# --------------------------------------------------------------------------- #
# 2. Nothing reachable from `behavior engine run` is a cognition engine        #
# --------------------------------------------------------------------------- #


def test_no_module_reachable_from_behavior_engine_run_is_a_cognition_engine() -> None:
    """Criterion 2, the transitive half: no agent engine, tool surface or forge.

    ``reachy.speech.llm`` is handled separately (and by equality) in the next
    test, because exactly one deliberately-kept edge reaches it.
    """
    parents = _reachable_from(_RUNTIME_ROOT)
    offences = [
        f"{module}  (via {_chain(module, parents)})"
        for module in sorted(parents)
        for forbidden in _COGNITION_MODULES
        if forbidden != _LLM_CLIENT and _matches(module, forbidden)
    ]
    assert not offences, (
        "`behavior engine run` can load a cognition engine:\n  "
        + "\n  ".join(offences)
        + "\nThe presence loop must stay symbolic; cognition attaches from "
        "outside over the runtime feed (`agent attach`)."
    )


def test_the_only_llm_edge_in_the_presence_runtime_is_the_engagement_gate() -> None:
    """Criterion 2, pinned by EQUALITY — the honest statement of today's truth.

    ``reachy.speech.llm`` IS reachable from ``behavior engine run``, through
    exactly one edge: :mod:`reachy.speech.engagement`, the addressed-vs-ambient
    admission gate that plan task t11 explicitly told us to keep when the
    hearing capability was ported into the runtime.

    Equality, not subset, so this fails in BOTH directions:

    * a NEW module importing the LLM client fails it, naming the import chain;
    * REMOVING the engagement classifier also fails it — at which point tighten
      the expectation to ``set()`` and delete the exception from
      :data:`_BEHAVIOR_SPEECH_ALLOW`. That is a real improvement to the zero-LLM
      property and should be recorded, not silently absorbed.
    """
    modules = _source_modules()
    parents = _reachable_from(_RUNTIME_ROOT)
    importers = {
        module
        for module in parents
        if any(
            _resolve(imported, modules) == _LLM_CLIENT
            for imported in _imported_names(modules[module], module)
        )
    }
    assert importers == _EXPECTED_LLM_IMPORTERS, (
        "the set of LLM importers reachable from `behavior engine run` changed.\n"
        f"  expected: {sorted(_EXPECTED_LLM_IMPORTERS)}\n"
        f"  actual:   {sorted(importers)}\n"
        + "\n".join(f"  {m}: {_chain(m, parents)}" for m in sorted(importers))
    )


def test_building_the_cli_parser_loads_no_cognition_module() -> None:
    """Building the CLI parser must not drag cognition into EVERY invocation.

    ``_build_parser()`` imports every command module, so a single module-scope
    import anywhere in the tree lands in the import path of ``say run``,
    ``daemon status`` and even ``--help``.

    This WAS a real defect, found by this suite and fixed in the same arc. Two
    module-scope imports put the LLM client and the cognition event bus on that
    path: ``behavior.transcript_sense`` -> ``speech.engagement`` -> ``speech.llm``
    (the engagement classifier's ``complete_fn`` default argument), and
    ``_commands/agent.py`` -> ``speech.events`` for ``SenseCue``. Both are now
    resolved lazily at first use.

    It was not merely cosmetic: ``tests/test_say.py``'s dumb-pipe boundary test
    failed deterministically in a fresh pytest worker and survived ``-n auto``
    only because an earlier test in the same worker imported the modules first —
    an order-dependent flake in an out-of-scope noun.

    Pinned by EQUALITY over the whole set, so re-widening fails loudly with the
    offending module named.
    """
    forbidden = (
        "reachy.speech.llm",
        "reachy.speech.events",
        "reachy.speech.agent_turn",
        "reachy.speech.tools",
        "reachy.forge",
    )
    code = (
        "import sys, json; from reachy.cli import _build_parser; _build_parser();"
        f"print(json.dumps([m for m in {forbidden!r} if m in sys.modules]))"
    )
    proc = subprocess.run(  # nosec B603 — fixed argv, sys.executable, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    loaded = json.loads(proc.stdout.strip())
    assert loaded == [], (
        "building the CLI parser now loads cognition module(s): "
        f"{loaded}. A module-scope import has crept back in; make it "
        "function-local (or TYPE_CHECKING-only) so every reachy invocation "
        "does not pay for cognition it will not use."
    )


def test_no_module_reachable_from_behavior_engine_run_imports_a_third_party_llm_sdk() -> None:
    parents = _reachable_from(_RUNTIME_ROOT)
    modules = _source_modules()
    offences: list[str] = []
    for module in sorted(parents):
        for imported in sorted(_imported_names(modules[module], module)):
            for package in _THIRD_PARTY_LLM_PACKAGES:
                if _matches(imported, package):
                    offences.append(f"{module} imports {imported}")
    assert not offences, "third-party LLM SDK reachable from the runtime:\n  " + "\n  ".join(
        offences
    )


def test_the_engine_decision_core_reaches_no_speech_module_at_all() -> None:
    """The strong claim: what CHOOSES the robot's next move is speech-free.

    Stronger than the allow-list, and the reason the engagement exception above
    is tolerable: the engine, the rule engine, intents, arbitration, the goto
    lane, pat sense and the sense snapshot reach NOTHING in
    :mod:`reachy.speech`, :mod:`reachy.forge` or :mod:`reachy.vision`
    transitively. Voice and ears hang off the composition root, not off the
    decision loop.
    """
    for core in _ENGINE_CORE:
        parents = _reachable_from(core)
        leaked = sorted(
            module
            for module in parents
            if module.startswith(("reachy.speech", "reachy.forge", "reachy.vision"))
        )
        assert not leaked, (
            f"{core} now reaches {leaked} — the engine's decision loop must be "
            f"free of the speech/vision/forge stacks.\n"
            + "\n".join(f"  {m}: {_chain(m, parents)}" for m in leaked)
        )


def test_importing_the_engine_does_not_load_the_llm_client() -> None:
    """The runtime check behind the static one: a fresh interpreter agrees."""
    assert not _pulls_into_sys_modules("reachy.behavior.engine", "reachy.speech.llm"), (
        "importing reachy.behavior.engine pulled reachy.speech.llm into "
        "sys.modules — the engine must not load a language model"
    )


def test_importing_the_rule_engine_does_not_load_the_llm_client() -> None:
    assert not _pulls_into_sys_modules("reachy.behavior.rule_engine", "reachy.speech.llm")


def test_importing_the_engine_does_not_load_the_agent_turn_engine() -> None:
    assert not _pulls_into_sys_modules("reachy.behavior.engine", "reachy.speech.agent_turn")


# --------------------------------------------------------------------------- #
# 3. Surviving reachy/motion/listen*.py modules                               #
# --------------------------------------------------------------------------- #


def test_no_folded_listen_hook_module_survives() -> None:
    """Criterion 3 as an assertion of ABSENCE, which is what it really is.

    t21 deleted every ``reachy/motion/listen_<sense>.py`` hook (think, vision,
    sleep, face, scene, transcribe) and t22 deleted ``listen_pat.py``. Iterating
    that glob for forbidden imports would be a test that checks nothing, so the
    check is inverted: the glob must stay EMPTY. If a folded hook ever comes
    back, this fails and the sibling test below starts covering it.
    """
    survivors = sorted(p.name for p in _MOTION_DIR.glob("listen_*.py"))
    assert survivors == [], (
        f"folded listen hooks reappeared in reachy/motion/: {survivors}. "
        "The `listen --live` composition root was retired by t21/t22; a sense "
        "that needs folding belongs on the behavior engine's tick seam."
    )


def test_every_surviving_listen_module_is_free_of_cognition() -> None:
    """The non-vacuous half: whatever ``listen*.py`` DOES survive is checked.

    Today that is exactly one module — ``reachy/motion/listen.py``, the
    ``ListenProducer`` t22 deliberately kept when it retired the ``listen``
    noun. The assertion that the set is non-empty is deliberate: if these
    modules all disappear, this test must fail rather than pass on an empty
    loop.
    """
    paths = sorted(_MOTION_DIR.glob("listen*.py"))
    assert paths, (
        "no reachy/motion/listen*.py module remains — this check has become "
        "vacuous. Delete it, or point it at whatever replaced ListenProducer."
    )

    offences: list[str] = []
    for path in paths:
        name = _module_name(path)
        parents = _reachable_from(name)
        for module in sorted(parents):
            for forbidden in (*_COGNITION_MODULES, "reachy.speech"):
                if _matches(module, forbidden):
                    offences.append(f"{name} reaches {module} via {_chain(module, parents)}")
    assert not offences, (
        "a surviving reachy/motion/listen*.py module reaches cognition:\n  "
        + "\n  ".join(offences)
        + "\nListenProducer is a pure decision object over Sense; it must stay "
        "one."
    )


def test_the_surviving_listen_producer_is_the_one_t22_kept() -> None:
    """Guards the previous test's subject against silent substitution."""
    survivors = sorted(p.name for p in _MOTION_DIR.glob("listen*.py"))
    assert survivors == ["listen.py"], f"unexpected listen modules in reachy/motion/: {survivors}"


# --------------------------------------------------------------------------- #
# 4. The residual LLM edge is genuinely optional at runtime                    #
# --------------------------------------------------------------------------- #


@pytest.mark.offline
def test_the_engagement_classifier_is_skipped_entirely_by_the_escape_hatch(monkeypatch) -> None:
    """``REACHY_ENGAGE_HEURISTIC=1`` means no classifier object is even built.

    The static edge above is an import; this is the behavioural bound that makes
    it optional. A box that wants a provably zero-LLM presence sets this and the
    runtime never constructs anything that could call a model.
    """
    from reachy.cli._commands.behavior import _engagement_classifier

    monkeypatch.setenv("REACHY_ENGAGE_HEURISTIC", "1")
    assert _engagement_classifier() is None


@pytest.mark.offline
def test_the_transcript_gate_defaults_to_no_classifier() -> None:
    """Constructing the hearing sense makes no LLM call, and needs none.

    ``classifier`` defaults to ``None``, and the driver then builds NO
    :class:`~reachy.speech.engagement.ConversationGate` at all — the LLM leg is
    opt-in composition, not a default of the sense itself. Asserted in the
    offline lane, where any socket would raise.
    """
    from reachy.behavior.transcript_sense import TranscriptSenseDriver

    driver = TranscriptSenseDriver(media=None)
    try:
        assert driver._gate is None, "the hearing sense built an LLM gate by default"
    finally:
        driver.close()


def test_the_engagement_classifier_build_is_lazy_and_defensive() -> None:
    """A missing/broken LLM leg must degrade hearing, never disable it.

    The composition root imports :mod:`reachy.speech.engagement` INSIDE
    :func:`_engagement_classifier` and swallows a build fault. Checked
    structurally so the property survives a refactor of the function body: a
    module-scope import would load the LLM client on every ``behavior``
    invocation, ``--help`` included.
    """
    import inspect
    import textwrap

    from reachy.cli._commands.behavior import _engagement_classifier

    tree = ast.parse(textwrap.dedent(inspect.getsource(_engagement_classifier)))
    local_imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "reachy.speech.engagement" in local_imports, (
        "_engagement_classifier no longer imports reachy.speech.engagement "
        f"locally (found {sorted(local_imports)}) — the LLM client must stay "
        "off the module-scope import path of every `behavior` command."
    )
    assert any(isinstance(node, ast.Try) for node in ast.walk(tree)), (
        "_engagement_classifier must build the classifier defensively: a build "
        "fault leaves the gate on the heuristic instead of killing hearing."
    )


# --------------------------------------------------------------------------- #
# 5. The wider repo: cognition survives, but only where it is supposed to      #
# --------------------------------------------------------------------------- #


def test_cognition_survives_only_behind_the_agent_noun() -> None:
    """Issue #70 DEMOTES cognition to an optional orchestrator; it does not delete it.

    So the LLM client must still be reachable from ``agent attach`` — if it is
    not, the arc has over-deleted and the agent path is broken. This is the
    complement of every ban above, and it keeps them honest: they constrain the
    presence runtime, not the repo.
    """
    parents = _reachable_from("reachy.cli._commands.agent")
    assert _LLM_CLIENT in parents, (
        "`agent attach` can no longer reach the LLM client — issue #70 demotes "
        "cognition to an external orchestrator, it does not remove it."
    )


def test_the_runtime_and_the_agent_do_not_share_a_cognition_engine_module() -> None:
    """The agent's engine must not have leaked into the runtime's closure."""
    runtime = set(_reachable_from(_RUNTIME_ROOT))
    assert "reachy.speech.agent_turn" not in runtime
    assert "reachy.speech.agent_turn" in set(_reachable_from("reachy.cli._commands.agent"))
