"""Static validator for forged skills — AST-only, never imports or runs code.

A forged skill (generated at runtime by the forge client, :mod:`reachy.forge.client`)
may only compose the sanctioned reaction primitives exposed on the injected ``ctx``
object. This module is the gate in front of activation: it parses ``executor.py`` with
the stdlib :mod:`ast` module and rejects anything outside the allow-list — it never
imports, compiles-to-exec, or otherwise runs the generated code.

Rejection is fail-closed: a skill folder that cannot be positively verified (missing
files, syntax error, oversized, unknown constructs) is rejected with reasons, never
waved through.

Cited (cite-don't-import) from ``reachy_nova/forge_validator.py``. Two deviations from
nova, both deliberate:

* the ``ctx`` attribute allow-list is **injectable** (``allowed_ctx_attrs=``, default
  :data:`DEFAULT_ALLOWED_CTX_ATTRS`) rather than a hard-coded module constant — the
  final ``ctx`` surface belongs to a later task (t13), so the gate must not pin it; and
* the default surface names this project's sanctioned seams
  (``speak`` / ``harmonics`` / ``express`` / ``state_get`` / ``state_update``) instead
  of nova's ``gesture`` / ``vocalize`` / ``say`` / ``inject`` / ``emotion``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from reachy.forge import lifecycle

#: Top-level module names generated code may import.
ALLOWED_IMPORTS = {"numpy", "math", "time", "typing", "dataclasses"}

#: The default sanctioned reaction surface on the injected ``ctx`` object. This is a
#: DEFAULT only — the caller injects the real surface (t13); see module docstring.
DEFAULT_ALLOWED_CTX_ATTRS = frozenset(
    {
        "speak",
        "harmonics",
        "express",
        "state_get",
        "state_update",
    }
)

#: Names whose mere appearance is a rejection — dangerous builtins and the dangerous
#: stdlib roots, so an aliased or indirect use is still caught.
FORBIDDEN_NAMES = {
    "exec",
    "eval",
    "compile",
    "__import__",
    "open",
    "input",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "breakpoint",
    "exit",
    "quit",
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
    "__builtins__",
}

#: Builtin callables plain enough to allow in generated code.
SAFE_BUILTIN_CALLS = {
    "abs",
    "bool",
    "dict",
    "enumerate",
    "float",
    "format",
    "int",
    "isinstance",
    "len",
    "list",
    "max",
    "min",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
}

MAX_EXECUTOR_LINES = 200


def validate(
    skill_dir: Path | str,
    allowed_ctx_attrs: Iterable[str] | None = None,
) -> tuple[bool, list[str]]:
    """Statically validate a staged skill folder.

    Args:
        skill_dir: folder expected to contain ``SKILL.md`` and ``executor.py``.
        allowed_ctx_attrs: the sanctioned ``ctx`` attribute surface. Defaults to
            :data:`DEFAULT_ALLOWED_CTX_ATTRS`.

    Returns:
        ``(ok, reasons)`` — ``ok`` is True only when every check passes; ``reasons``
        lists every violation found (empty when ok).
    """
    allowed_ctx = (
        set(allowed_ctx_attrs) if allowed_ctx_attrs is not None else set(DEFAULT_ALLOWED_CTX_ATTRS)
    )
    skill_dir = Path(skill_dir)
    reasons: list[str] = []

    skill_md = skill_dir / lifecycle.SKILL_FILENAME
    executor = skill_dir / lifecycle.EXECUTOR_FILENAME
    if not skill_md.is_file() or not skill_md.read_text().strip():
        reasons.append(f"{lifecycle.SKILL_FILENAME} missing or empty")
    if not executor.is_file():
        reasons.append(f"{lifecycle.EXECUTOR_FILENAME} missing")
        return False, reasons

    source = executor.read_text()
    if len(source.splitlines()) > MAX_EXECUTOR_LINES:
        reasons.append(f"executor.py exceeds {MAX_EXECUTOR_LINES} lines — too large to trust")
        return False, reasons

    try:
        tree = ast.parse(source)
    except SyntaxError as err:
        reasons.append(f"executor.py has a syntax error: {err.msg} (line {err.lineno})")
        return False, reasons

    reasons.extend(_walk(tree, allowed_ctx))

    if not _has_execute(tree):
        reasons.append("executor.py must define execute(params, ctx)")

    return (not reasons), reasons


def _walk(tree: ast.AST, allowed_ctx: set[str]) -> list[str]:
    """Collect every allow-list violation in the parsed executor."""
    reasons: list[str] = []
    local_funcs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    import_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            _check_import(node, reasons, import_aliases)
        elif isinstance(node, ast.ImportFrom):
            _check_import_from(node, reasons, import_aliases)
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                reasons.append(f"use of '{node.id}' is forbidden (line {node.lineno})")
        elif isinstance(node, ast.Attribute):
            _check_attribute(node, reasons, allowed_ctx)
        elif isinstance(node, ast.Call):
            _check_call(node, reasons, local_funcs, import_aliases, allowed_ctx)

    return reasons


def _check_import(node: ast.Import, reasons: list[str], import_aliases: set[str]) -> None:
    for alias in node.names:
        root = alias.name.split(".")[0]
        if root not in ALLOWED_IMPORTS:
            reasons.append(f"import '{alias.name}' is not allowed (line {node.lineno})")
        else:
            import_aliases.add(alias.asname or root)


def _check_import_from(node: ast.ImportFrom, reasons: list[str], import_aliases: set[str]) -> None:
    root = (node.module or "").split(".")[0]
    if root not in ALLOWED_IMPORTS:
        reasons.append(f"import from '{node.module}' is not allowed (line {node.lineno})")
    else:
        for alias in node.names:
            import_aliases.add(alias.asname or alias.name)


def _check_attribute(node: ast.Attribute, reasons: list[str], allowed_ctx: set[str]) -> None:
    if node.attr.startswith("__"):
        reasons.append(f"dunder attribute access '.{node.attr}' is forbidden (line {node.lineno})")
    base = _attribute_base(node)
    if base == "ctx" and node.attr not in allowed_ctx:
        reasons.append(
            f"ctx.{node.attr} is outside the sanctioned primitive surface (line {node.lineno})"
        )


def _check_call(
    node: ast.Call,
    reasons: list[str],
    local_funcs: set[str],
    import_aliases: set[str],
    allowed_ctx: set[str],
) -> None:
    """Fail-closed call-target check.

    A call is sanctioned ONLY when its callee is exactly one of:

    (a) a plain ``ast.Name`` that resolves against the allow-lists (safe builtin,
        local function, or an allowed-import alias);
    (b) a ``ctx.<attr>`` attribute call where ``<attr>`` is on the sanctioned surface; or
    (c) an attribute call on an ALLOWED import (e.g. ``math.sin(...)``,
        ``time.monotonic()``, ``numpy.array(...)``).

    Anything else — a call through a subscript (``d["k"]()``), a lambda/call result
    (``(lambda: ...)()``), or a chained attribute off a base that is neither ``ctx`` nor
    an allowed import — is REJECTED outright. Previously this function returned silently
    for any callee that wasn't a plain ``ast.Name``, which let calls through attributes
    and subscripts slip past the allow-list entirely (e.g.
    ``__builtins__["__import__"]("os")``).
    """
    func = node.func

    if isinstance(func, ast.Name):
        name = func.id
        allowed = name in SAFE_BUILTIN_CALLS or name in local_funcs or name in import_aliases
        # FORBIDDEN_NAMES is already flagged via the Name branch — don't double-report.
        if not allowed and name not in FORBIDDEN_NAMES:
            reasons.append(
                f"call to '{name}' is outside the sanctioned surface (line {node.lineno})"
            )
        return

    if isinstance(func, ast.Attribute):
        base = _attribute_base(func)
        if base == "ctx" and func.attr in allowed_ctx:
            return
        if base is not None and base in import_aliases:
            return
        target = f"{base}.{func.attr}" if base is not None else f"<expr>.{func.attr}"
        reasons.append(f"call to '{target}' is outside the sanctioned surface (line {node.lineno})")
        return

    # Any other call-target shape (subscript, lambda, call-result, ...): fail closed.
    reasons.append(f"call target is not a sanctioned name or attribute call (line {node.lineno})")


def _attribute_base(node: ast.Attribute) -> str | None:
    """Resolve the root Name of an attribute chain (``a.b.c`` -> ``a``)."""
    value = node.value
    while isinstance(value, ast.Attribute):
        value = value.value
    if isinstance(value, ast.Name):
        return value.id
    return None


def _has_execute(tree: ast.AST) -> bool:
    """True when the module defines a top-level ``execute`` taking exactly two args."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            args = node.args
            n = len(args.args) + len(args.posonlyargs)
            if n == 2 and not args.vararg and not args.kwarg:
                return True
    return False
