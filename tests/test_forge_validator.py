"""Tests for the AST-only, fail-closed forge validator (:mod:`reachy.forge.validator`).

Covers task t12 acceptance criterion 2: the validator parses ``executor.py`` with
:mod:`ast` and never imports/compiles/execs it, and rejection is fail-closed with a
reason for every violation class. There is a NEGATIVE test per rejection class here
(import allow-list, forbidden names, ctx-attr allow-list, dunder access, call-target
allow-list, line cap, missing/misshaped ``execute``), plus the positive counterparts
and the "validator never runs generated code" proof.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reachy.forge.validator import DEFAULT_ALLOWED_CTX_ATTRS, MAX_EXECUTOR_LINES, validate

_VALID_SKILL_MD = (
    "---\nname: wave-hello\ndescription: wave a friendly hello\n---\n\nWave when greeted.\n"
)

_VALID_EXECUTOR = (
    "import math\n"
    "\n"
    "\n"
    "def execute(params, ctx):\n"
    '    angle = math.sin(params["t"])\n'
    '    ctx.express("happy")\n'
    '    ctx.speak("hello there")\n'
    "    return angle\n"
)


def _write(tmp_path, *, executor=_VALID_EXECUTOR, skill_md=_VALID_SKILL_MD):
    d = tmp_path / "skill"
    d.mkdir(parents=True, exist_ok=True)
    if skill_md is not None:
        (d / "SKILL.md").write_text(skill_md)
    if executor is not None:
        (d / "executor.py").write_text(executor)
    return d


# ---------------------------------------------------------------------------
# Positive / happy path
# ---------------------------------------------------------------------------


def test_valid_skill_passes(tmp_path):
    ok, reasons = validate(_write(tmp_path))
    assert ok is True
    assert reasons == []


@pytest.mark.parametrize(
    "imp",
    [
        "import numpy",
        "import math",
        "import time",
        "import typing",
        "from dataclasses import dataclass",
    ],
)
def test_allowed_imports_pass(tmp_path, imp):
    src = f"{imp}\n\n\ndef execute(params, ctx):\n    return params\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is True, reasons


def test_default_ctx_attrs_pass(tmp_path):
    lines = "\n".join(f"    ctx.{attr}" for attr in sorted(DEFAULT_ALLOWED_CTX_ATTRS))
    src = f"def execute(params, ctx):\n{lines}\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is True, reasons


def test_safe_builtin_calls_pass(tmp_path):
    src = "def execute(params, ctx):\n    return len(list(range(int(params['n']))))\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is True, reasons


def test_local_function_call_passes(tmp_path):
    src = "def helper():\n    return 1\n\n\ndef execute(params, ctx):\n    return helper()\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is True, reasons


# ---------------------------------------------------------------------------
# Negative: file-shape rejection classes
# ---------------------------------------------------------------------------


def test_missing_executor_rejects(tmp_path):
    ok, reasons = validate(_write(tmp_path, executor=None))
    assert ok is False
    assert any("executor.py missing" in r for r in reasons)


def test_empty_skill_md_rejects(tmp_path):
    ok, reasons = validate(_write(tmp_path, skill_md="   \n"))
    assert ok is False
    assert any("SKILL.md" in r for r in reasons)


def test_missing_skill_md_rejects(tmp_path):
    ok, reasons = validate(_write(tmp_path, skill_md=None))
    assert ok is False
    assert any("SKILL.md" in r for r in reasons)


def test_syntax_error_rejects(tmp_path):
    ok, reasons = validate(_write(tmp_path, executor="def execute(params, ctx)\n    pass\n"))
    assert ok is False
    assert any("syntax error" in r for r in reasons)


def test_line_cap_rejects(tmp_path):
    body = "\n".join(f"    x{i} = {i}" for i in range(MAX_EXECUTOR_LINES + 5))
    src = f"def execute(params, ctx):\n{body}\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("exceeds" in r and "lines" in r for r in reasons)


# ---------------------------------------------------------------------------
# Negative: import allow-list
# ---------------------------------------------------------------------------


def test_disallowed_import_rejects(tmp_path):
    src = "import json\n\n\ndef execute(params, ctx):\n    return params\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("json" in r and "not allowed" in r for r in reasons)


def test_disallowed_import_from_rejects(tmp_path):
    src = "from json import loads\n\n\ndef execute(params, ctx):\n    return loads('{}')\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("not allowed" in r for r in reasons)


# ---------------------------------------------------------------------------
# Negative: forbidden names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "exec",
        "eval",
        "compile",
        "__import__",
        "open",
        "getattr",
        "setattr",
        "globals",
        "locals",
        "os",
        "sys",
        "subprocess",
        "socket",
        "shutil",
        "pathlib",
        "urllib",
        "requests",
        "http",
        "importlib",
        "ctypes",
        "pickle",
        "marshal",
        "breakpoint",
        "__builtins__",
    ],
)
def test_forbidden_name_rejects(tmp_path, name):
    src = f"def execute(params, ctx):\n    return {name}\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any(name in r and "forbidden" in r for r in reasons)


# ---------------------------------------------------------------------------
# Negative: dunder attribute access (anywhere)
# ---------------------------------------------------------------------------


def test_dunder_attribute_access_rejects(tmp_path):
    src = "def execute(params, ctx):\n    return params.__class__\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("dunder" in r for r in reasons)


# ---------------------------------------------------------------------------
# Negative: ctx-attribute allow-list (and its injectability)
# ---------------------------------------------------------------------------


def test_ctx_attr_outside_allowlist_rejects(tmp_path):
    src = "def execute(params, ctx):\n    ctx.dance()\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("ctx.dance" in r for r in reasons)


def test_ctx_allowlist_is_injectable(tmp_path):
    src = "def execute(params, ctx):\n    ctx.wiggle()\n"
    ok_default, _ = validate(_write(tmp_path, executor=src))
    assert ok_default is False
    ok_injected, reasons = validate(_write(tmp_path, executor=src), allowed_ctx_attrs={"wiggle"})
    assert ok_injected is True, reasons


def test_injected_allowlist_rejects_default_names(tmp_path):
    src = "def execute(params, ctx):\n    ctx.speak('hi')\n"
    ok, reasons = validate(_write(tmp_path, executor=src), allowed_ctx_attrs={"only_this"})
    assert ok is False
    assert any("ctx.speak" in r for r in reasons)


# ---------------------------------------------------------------------------
# Negative: call-target allow-list
# ---------------------------------------------------------------------------


def test_unsanctioned_call_rejects(tmp_path):
    src = "def execute(params, ctx):\n    do_thing()\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("do_thing" in r for r in reasons)


# ---------------------------------------------------------------------------
# Negative: call-target allow-list — fail-closed on non-Name callees
#
# Qodo finding: the call-target check only validated calls whose callee is a
# plain ``ast.Name``. A call through a subscript, a lambda result, or a
# chained attribute off a non-allowed base slipped past the allow-list
# entirely (the old ``_check_call`` just ``return``ed for anything that
# wasn't an ``ast.Name``). Every shape below must now be rejected.
# ---------------------------------------------------------------------------


def test_subscript_callee_call_rejects(tmp_path):
    """``d["k"]()`` — a call whose callee is a Subscript, not a Name/Attribute."""
    src = (
        "def execute(params, ctx):\n" "    ops = {'go': len}\n" "    return ops['go']([1, 2, 3])\n"
    )
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("call target" in r and "line" in r for r in reasons)


def test_lambda_result_call_rejects(tmp_path):
    """``(lambda: 1)()`` — a call whose callee is a Lambda expression."""
    src = "def execute(params, ctx):\n    return (lambda: 1)()\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("call target" in r and "line" in r for r in reasons)


def test_chained_attribute_call_on_non_allowed_base_rejects(tmp_path):
    """``params.helper.compute()`` — attribute call whose root is neither ``ctx``
    nor an allowed-import alias."""
    src = "def execute(params, ctx):\n    return params.helper.compute()\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("outside the sanctioned surface" in r for r in reasons)


def test_builtins_by_name_rejects(tmp_path):
    """``__builtins__`` referenced directly is forbidden (not previously listed)."""
    src = "def execute(params, ctx):\n    return __builtins__\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("__builtins__" in r and "forbidden" in r for r in reasons)


def test_builtins_subscript_import_chain_rejects(tmp_path):
    """The literal exploit from the finding: ``__builtins__["__import__"]("os")``
    combines a forbidden name AND a subscript-callee call — both must fire."""
    src = 'def execute(params, ctx):\n    return __builtins__["__import__"]("os")\n'
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("__builtins__" in r and "forbidden" in r for r in reasons)
    assert any("call target" in r for r in reasons)


# ---------------------------------------------------------------------------
# Negative: execute() shape
# ---------------------------------------------------------------------------


def test_missing_execute_rejects(tmp_path):
    src = "def other(params, ctx):\n    return params\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("execute(params, ctx)" in r for r in reasons)


def test_execute_wrong_arity_rejects(tmp_path):
    src = "def execute(params):\n    return params\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("execute(params, ctx)" in r for r in reasons)


def test_execute_with_varargs_rejects(tmp_path):
    src = "def execute(params, ctx, *args):\n    return params\n"
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False
    assert any("execute(params, ctx)" in r for r in reasons)


# ---------------------------------------------------------------------------
# The validator never runs generated code, and the package has no exec/eval
# ---------------------------------------------------------------------------


def test_validator_does_not_execute_module_level_code(tmp_path):
    marker = tmp_path / "pwned.txt"
    src = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "\n"
        "\n"
        "def execute(params, ctx):\n"
        "    return 1\n"
    )
    ok, reasons = validate(_write(tmp_path, executor=src))
    assert ok is False  # 'open' is forbidden ...
    assert reasons
    assert not marker.exists()  # ... and crucially the source was never executed


def test_no_exec_or_eval_in_forge_package():
    import re

    import reachy.forge as pkg

    pkg_dir = Path(pkg.__file__).parent
    # Genuine dynamic-execution calls only. `compile(` is matched via a negative
    # look-behind so `re.compile(...)` (regex compilation, not the builtin) is exempt.
    banned = [
        re.compile(r"\bexec\("),
        re.compile(r"\beval\("),
        re.compile(r"(?<![\w.])compile\("),
        re.compile(r"__import__\("),
    ]
    offenders = []
    for py in sorted(pkg_dir.glob("*.py")):
        text = py.read_text()
        for pattern in banned:
            if pattern.search(text):
                offenders.append((py.name, pattern.pattern))
    assert offenders == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
