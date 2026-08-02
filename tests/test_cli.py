"""Smoke tests for the reachy-mini-cli CLI entry point and its verbs."""

from __future__ import annotations

import argparse
import json

import pytest

from reachy import __version__
from reachy.cli import _build_parser, main
from reachy.explain import known_paths


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    assert "usage: reachy-mini-cli" in capsys.readouterr().out


def test_unknown_command_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- whoami ---------------------------------------------------------------


def test_whoami_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nick: reachy-mini-cli" in out
    assert "backend: claude" in out
    assert "model:" in out


def test_whoami_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["nick"] == "reachy-mini-cli"
    assert payload["version"] == __version__
    assert payload["backend"] == "claude"


# --- learn ----------------------------------------------------------------


def test_learn_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn"])
    assert rc == 0
    out = capsys.readouterr().out
    assert len(out) >= 200
    assert "reachy-mini-cli" in out
    assert "Exit-code policy" in out
    assert "--json" in out
    assert "explain" in out


def test_learn_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "reachy-mini-cli"
    assert payload["version"] == __version__
    assert payload["json_support"] is True


# --- quickstart -----------------------------------------------------------


def test_quickstart_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["quickstart"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "uv tool install" in out
    assert "reachy-mini-cli[daemon]" in out
    assert "daemon start" in out


def test_quickstart_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["quickstart", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload["profiles"], list)
    assert payload["profiles"]


# --- explain --------------------------------------------------------------


def test_explain_root(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain"])
    assert rc == 0
    assert "# reachy-mini-cli" in capsys.readouterr().out


def test_explain_self(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "reachy-mini-cli"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("#")


def test_explain_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == ["whoami"]
    assert "reachy-mini-cli whoami" in payload["markdown"]


def test_explain_unknown_path_errors(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "nonexistent"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "hint:" in captured.err


def test_every_catalog_path_resolves(capsys: pytest.CaptureFixture[str]) -> None:
    for path in known_paths():
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()


# --- explain catalog <-> CLI argparse tree agreement -----------------------
#
# The test above only walks ENTRIES and asserts each key resolves *within the
# catalog itself* — it never consults the live argparse tree, so it stays green
# even if a verb is deleted from the CLI while its catalog entry is left behind
# (explain would then document a command that no longer exists), and it stays
# green even if a brand-new verb is added with no catalog entry at all. The two
# tests below close both gaps by walking `_build_parser()`'s subparsers tree
# directly and comparing it against `known_paths()` in both directions.

#: Self-reference aliases for the catalog root entry (see the module docstring
#: of ``reachy/explain/catalog.py``): ``()`` is the actual root path in the
#: argparse tree (the top-level parser itself), while ``("reachy",)`` and
#: ``("reachy-mini-cli",)`` are documented stand-ins for it — the installed
#: console-script name and the display name used throughout help text — so that
#: ``explain reachy`` / ``explain reachy-mini-cli`` (the rubric's
#: ``explain_self`` check) resolve to the same root markdown. No subcommand
#: literally named "reachy" or "reachy-mini-cli" is ever registered, so these
#: two keys can never appear as a live argparse path; they are exempted from
#: the "every catalog key is a live path" direction below, not because they are
#: stale, but because they were never meant to be verbs.
_ROOT_ALIASES: frozenset[tuple[str, ...]] = frozenset({(), ("reachy",), ("reachy-mini-cli",)})


def _iter_argparse_paths(parser: argparse.ArgumentParser) -> set[tuple[str, ...]]:
    """Walk *parser*'s subparsers tree and return every reachable command path.

    A path is the tuple of verb tokens from the root to a (sub)parser, e.g.
    ``("behavior", "rules", "check")``; the root parser itself is ``()``. This
    walks ``argparse._SubParsersAction.choices`` recursively — the same
    structure ``_build_parser()`` populates via nested ``add_subparsers()`` /
    ``add_parser()`` calls — so it reflects exactly what
    ``reachy-mini-cli <path>`` accepts, with no need to duplicate the noun
    catalog by hand.
    """
    paths: set[tuple[str, ...]] = set()

    def walk(p: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        paths.add(prefix)
        for action in p._actions:  # argparse has no public walk API
            if isinstance(action, argparse._SubParsersAction):
                for name, subparser in action.choices.items():
                    walk(subparser, prefix + (name,))

    walk(parser, ())
    return paths


def test_catalog_entries_resolve_to_live_argparse_paths() -> None:
    """Every ``ENTRIES`` key must name a command path that actually exists.

    Catches a verb being deleted from the CLI while its catalog entry is left
    behind: without this, ``reachy-mini-cli explain <deleted verb>`` would keep
    printing full documentation for a command ``reachy-mini-cli <deleted
    verb>`` itself now rejects, and CI would stay green.
    """
    live_paths = _iter_argparse_paths(_build_parser())
    stale = sorted(
        path for path in known_paths() if path not in _ROOT_ALIASES and path not in live_paths
    )
    assert not stale, (
        "explain catalog entries name no live CLI command (stale after a verb was "
        f"deleted from _build_parser()): {stale!r}. Fix: remove these keys from "
        "reachy/explain/catalog.py's ENTRIES (and their markdown body, if unused "
        "elsewhere)."
    )


def test_every_registered_verb_has_catalog_entry() -> None:
    """Every live CLI command path must have an ``ENTRIES`` key.

    Catches a new verb/noun being registered in ``_build_parser()`` with no
    matching catalog entry: without this, ``reachy-mini-cli explain <path>``
    would raise "no explain entry for" on a command that otherwise works fine,
    and CI would stay green.
    """
    live_paths = _iter_argparse_paths(_build_parser())
    entry_keys = set(known_paths())
    undocumented = sorted(path for path in live_paths if path not in entry_keys)
    assert not undocumented, (
        "CLI command paths have no explain catalog entry (missing docs for a new "
        f"or renamed verb): {undocumented!r}. Fix: add a key for each path to "
        "reachy/explain/catalog.py's ENTRIES."
    )
