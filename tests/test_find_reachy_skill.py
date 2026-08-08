"""Structural checks for the ``.claude/skills/find-reachy/`` skill wrapper (task t10).

``find-reachy`` is the agent-facing front for ``reachy wireless`` — a mesh agent's
way to locate a Reachy Mini Wireless without a human typing an IP. The one hard
rule from the plan (t10's acceptance criterion) is that the wrapper script
contains **no discovery logic of its own**: it only resolves the CLI and shells
out to it, exactly like ``.claude/skills/think/scripts/think.sh`` resolves
``devague``. Discovery lives in exactly one place — ``reachy/discover/``, wrapped
by ``reachy wireless`` (``reachy/cli/_commands/wireless.py``) — so this skill can
never drift from the tool it wraps.

These are static/text-level checks in the style of the repo's other structural
tests (e.g. ``tests/test_zero_llm_boundary.py``, ``tests/test_dep_freeze.py``):
no subprocess is spawned, no real CLI or network is touched.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "find-reachy"
SKILL_MD = SKILL_DIR / "SKILL.md"
FIND_SH = SKILL_DIR / "scripts" / "find.sh"

#: Mechanisms find.sh must never contain — each one would be a SECOND discovery
#: implementation, drifting from reachy/discover/ the moment either changes.
#: Named individually (not one combined pattern) so a failure says exactly
#: which forbidden mechanism was found.
_FORBIDDEN_TOKENS = {
    "/proc filesystem parsing": re.compile(r"/proc/net|/proc/\$"),
    "the `ip` command": re.compile(r"(?<![\w./-])ip\s+(addr|route|neigh|link)\b"),
    "the `arp` command": re.compile(r"(?<![\w./-])arp\s"),
    "avahi-browse / avahi-resolve": re.compile(r"avahi-(browse|resolve)"),
    "nmap": re.compile(r"(?<![\w./-])nmap\b"),
    "raw socket construction": re.compile(r"\bsocket\("),
    "netcat": re.compile(r"(?<![\w./-])nc\s+-"),
    "curl/wget against the daemon": re.compile(r"\b(curl|wget)\b"),
    "python inline scripting": re.compile(r"\bpython3?\s+-c\b"),
}


def _skill_md_frontmatter() -> dict[str, str]:
    """Parse the ``---``-delimited YAML-ish frontmatter without a YAML dependency.

    Only needs to pull flat ``key: value`` pairs (``name``, ``type``); the repo's
    other SKILL.md files use the same simple shape, and ``description`` may be a
    folded block (``description: >``) which this helper does not need to parse.
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must open with a --- frontmatter fence"
    end = text.index("\n---", 4)
    block = text[4:end]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line or line[0] in " \t#":
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


# --------------------------------------------------------------------------- #
# 1. SKILL.md exists with valid frontmatter carrying name + description        #
# --------------------------------------------------------------------------- #


def test_skill_md_exists():
    assert SKILL_MD.is_file(), f"missing {SKILL_MD}"


def test_skill_md_frontmatter_has_name_and_description():
    fields = _skill_md_frontmatter()
    assert fields.get("name") == "find-reachy"
    # `description` here is a folded block scalar (`description: >`); confirm
    # the key is present and the body that follows is non-trivial prose.
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "description:" in text
    end = text.index("\n---", 4)
    block = text[4:end]
    desc_start = block.index("description:")
    description_body = block[desc_start:]
    assert len(description_body) > 100, "description body looks empty/truncated"


def test_skill_md_description_states_when_to_use_it():
    """The brief requires the description to name concrete trigger phrases."""
    text = SKILL_MD.read_text(encoding="utf-8").lower()
    end = text.index("\n---", 4)
    frontmatter = text[:end]
    for phrase in (
        "find my reachy",
        "where is the robot",
        "what's the robot's ip",
        "ssh into the robot",
        "base_url",
    ):
        assert phrase in frontmatter, f"SKILL.md frontmatter never mentions {phrase!r}"


def test_skill_md_type_is_command_matching_sibling_skills():
    fields = _skill_md_frontmatter()
    assert fields.get("type") == "command"


# --------------------------------------------------------------------------- #
# 2. scripts/find.sh exists and is executable                                  #
# --------------------------------------------------------------------------- #


def test_find_sh_exists():
    assert FIND_SH.is_file(), f"missing {FIND_SH}"


def test_find_sh_is_executable():
    mode = FIND_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/find.sh must be chmod +x (owner-executable)"


def test_find_sh_has_bash_shebang():
    first_line = FIND_SH.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!"), "find.sh must start with a shebang"
    assert "bash" in first_line


def test_find_sh_uses_strict_mode():
    text = FIND_SH.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text, "find.sh must run under bash strict mode"


# --------------------------------------------------------------------------- #
# 3. The script contains NO discovery logic of its own                        #
# --------------------------------------------------------------------------- #


def _script_body_without_comments() -> str:
    """Strip full-line and trailing '#' comments so prose in comments (which
    legitimately NAMES the forbidden mechanisms, e.g. this docstring's own
    module comment) never produces a false positive against the executable
    body. Line-by-line, since '#' never has special meaning inside a bash
    single-quoted heredoc/string in this script and none is used here for
    anything containing these tokens outside of comments/docs.
    """
    lines = []
    for line in FIND_SH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


@pytest.mark.parametrize("label", sorted(_FORBIDDEN_TOKENS))
def test_find_sh_contains_no_discovery_mechanism(label):
    """Each forbidden mechanism is checked and reported individually.

    A failure names exactly which rule was broken, per the task brief: the
    script must shell out to the CLI and nothing else — never parse /proc,
    call ip/arp/avahi-browse/nmap, open sockets, curl the daemon directly, or
    reimplement any filtering.
    """
    pattern = _FORBIDDEN_TOKENS[label]
    body = _script_body_without_comments()
    match = pattern.search(body)
    matched_text = match.group(0) if match else ""
    assert match is None, (
        f"find.sh appears to contain discovery logic of its own: {label!r} "
        f"matched {matched_text!r} — discovery must live only "
        "in reachy/discover/, wrapped by 'reachy wireless'"
    )


def test_find_sh_never_reads_proc_or_sys_network_files():
    body = _script_body_without_comments()
    assert "/proc/net" not in body
    assert "/sys/class/net" not in body


def test_find_sh_never_imports_or_execs_a_second_cli():
    """The only external command this script may invoke (besides shell
    builtins/uv) is the reachy CLI itself — never a second implementation."""
    body = _script_body_without_comments()
    # Every invoked "command" is one of: reachy, reachy-mini-cli, uv, or a
    # shell builtin used for CLI resolution (command, dirname, cat, exec).
    allowed_commands = {
        "command",
        "dirname",
        "cat",
        "exec",
        "uv",
        "reachy",
        "reachy-mini-cli",
        "grep",
        "return",
    }
    # A light heuristic: collect bareword tokens that look like invoked
    # commands (start of a statement) and ensure none names a network tool.
    forbidden_commands = {
        "ip",
        "arp",
        "nmap",
        "curl",
        "wget",
        "avahi-browse",
        "avahi-resolve",
        "nc",
    }
    tokens = set(re.findall(r"(?m)^\s*([a-zA-Z][\w.-]*)\b", body))
    overlap = tokens & forbidden_commands
    assert not overlap, f"find.sh invokes forbidden command(s) as a statement: {overlap}"
    assert forbidden_commands.isdisjoint(allowed_commands)  # sanity: lists don't collide


# --------------------------------------------------------------------------- #
# 4. The script references the real CLI verbs (wireless find at minimum)      #
# --------------------------------------------------------------------------- #


def test_find_sh_references_wireless_find_verb():
    body = FIND_SH.read_text(encoding="utf-8")
    assert "wireless find" in body, "find.sh must shell out to 'reachy wireless find'"
    assert "--json" in body, "the default invocation should be agent-friendly JSON"


def test_find_sh_references_every_wireless_verb():
    """Every verb the plan lists for 'reachy wireless' should be documented or
    forwarded somewhere in the script (usage text counts, since forwarding is
    verbatim for anything beyond the default)."""
    body = FIND_SH.read_text(encoding="utf-8")
    for verb in ("find", "list", "ssh", "authorize", "pin", "unpin", "forget", "overview"):
        assert verb in body, f"find.sh never mentions the {verb!r} verb"


def test_find_sh_resolves_the_cli_portably():
    """Mirrors think.sh's resolution contract: installed-first, uv fallback,
    then an actionable install hint — never a hardcoded absolute path."""
    body = FIND_SH.read_text(encoding="utf-8")
    assert "command -v reachy" in body
    assert "uv run reachy" in body
    assert "pip install" in body
    # Never a hardcoded path into this developer's checkout.
    assert str(REPO_ROOT) not in body
    assert os.sep + "home" + os.sep not in body


def test_find_sh_execs_never_backgrounds():
    """Mirrors think.sh: the CLI invocation is `exec`'d, replacing this
    process rather than being spawned as a child the wrapper then parses."""
    body = FIND_SH.read_text(encoding="utf-8")
    assert re.search(
        r'exec\s+"\$\{REACHY\[@\]\}"', body
    ), "find.sh should exec the resolved CLI, not merely call it"


# --------------------------------------------------------------------------- #
# Layout parity with the run-tests template                                    #
# --------------------------------------------------------------------------- #


def test_skill_layout_mirrors_run_tests_template():
    """SKILL.md + scripts/<name>.sh, same as .claude/skills/run-tests/."""
    run_tests_dir = REPO_ROOT / ".claude" / "skills" / "run-tests"
    assert (run_tests_dir / "SKILL.md").is_file()
    assert (run_tests_dir / "scripts" / "test.sh").is_file()
    assert SKILL_MD.is_file()
    assert FIND_SH.is_file()
    assert FIND_SH.parent.name == "scripts"
