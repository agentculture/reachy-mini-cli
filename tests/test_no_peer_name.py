"""This repo never learns a peer harness's name (issue #177, operator decision on #175).

The robot's canonical names are configuration — the overlay's ``names`` table —
and the shipped pair is spelled once in ``reachy.speech.name_match.SHIPPED_NAMES``.
A peer's name (``nova`` today) may appear in tests and docs as an example of a
*configured* value, and in comments/docstrings as PROVENANCE (much of
``reachy/vision`` and ``reachy/forge`` is cite-don't-import ported from
``reachy_nova`` and says so). What it must never be is a VALUE in the source
package — a string constant, an identifier, a TOML entry — where it would be a
default, a prompt or a tuple literal the peer could not opt out of.

The one allowed value is the harness's OWN MQTT topic namespace
(``reachy/export/mind_presence.py``): that names the peer's *process*, cited
from its bus contract, not a name the robot answers to.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REACHY_SRC = Path(__file__).resolve().parents[1] / "reachy"

#: Names this repo must not know as values. Extend when a new peer harness appears.
PEER_NAMES = ("nova",)

#: String constants that legitimately carry a peer's name: the harness's own bus
#: topics, cited from reachy_nova's contract. Each entry names its reason.
ALLOWED_VALUES: dict[str, str] = {
    "nova/harness/state": "the harness's OWN retained availability topic (mind_presence.py)",
    "reachy/state/nova/online": "the harness's legacy presence key, documented for contrast",
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
    return ids


def _value_hits(path: Path, pattern: re.Pattern[str]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings or node.value in ALLOWED_VALUES:
                continue
            if pattern.search(node.value):
                hits.append(f"{path.name}:{node.lineno}: string {node.value!r}")
        elif isinstance(node, ast.Name) and pattern.search(node.id):
            hits.append(f"{path.name}:{node.lineno}: identifier {node.id}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if pattern.search(node.name):
                hits.append(f"{path.name}:{node.lineno}: def {node.name}")
    return hits


def test_no_peer_name_is_a_value_anywhere_in_the_source_package() -> None:
    offenders: list[str] = []
    for name in PEER_NAMES:
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        for path in REACHY_SRC.rglob("*.py"):
            offenders.extend(_value_hits(path, pattern))
        for path in REACHY_SRC.rglob("*.toml"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if pattern.search(code):
                    offenders.append(f"{path.name}:{lineno}: toml {code.strip()!r}")
    assert not offenders, (
        "a peer's name is a VALUE in reachy/ — it belongs in the overlay's names table:\n"
        + "\n".join(offenders)
    )


def test_the_allow_list_is_not_dead() -> None:
    """Every allowed value must still be used somewhere, or the exception is stale."""
    corpus = "".join(p.read_text(encoding="utf-8") for p in REACHY_SRC.rglob("*.py"))
    for value, reason in ALLOWED_VALUES.items():
        assert value in corpus, f"stale allow-list entry {value!r} ({reason})"


def test_the_shipped_pair_is_spelled_once() -> None:
    literal = re.compile(r"\(\s*[\"']reachy[\"']\s*,\s*[\"']robot[\"']\s*\)")
    hits = [
        str(path.relative_to(REACHY_SRC.parent))
        for path in REACHY_SRC.rglob("*.py")
        if literal.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == ["reachy/speech/name_match.py"], hits
