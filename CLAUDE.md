# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is (and is not, yet)

`reachy-mini-cli` is an **AgentCulture mesh agent** whose intended domain is
*operating the Reachy Mini expressive robot — device setup, app management, and
runtime ops* (see `culture.yaml` and the README).

**Today the code is still the unmodified clone of `culture-agent-template`.** The
CLI exposes only the template's agent-first introspection verbs (`whoami`,
`learn`, `explain`, `overview`, `doctor`, `cli`). **No Reachy / robot
functionality exists yet** — there is no device, app, or runtime-ops code. When
you add robot features, you are building the real agent on top of this scaffold,
not modifying a finished product. Treat the existing verbs as the *pattern to
copy*, not the *feature set*.

## Critical naming gotcha

The clone was only half-renamed. The names do **not** all agree:

| Thing | Value |
|-------|-------|
| Installed console script (what you actually run) | **`reachy`** |
| Import package | `reachy` |
| Distribution / PyPI name | `reachy-cli` (`__version__` reads this) |
| `prog=` and every help/`learn`/`explain`/README string | `reachy-mini-cli` |

So `uv run reachy whoami` **works**; `uv run reachy-mini-cli whoami` **fails**
(no such binary) — the README's quickstart and `explain`/`learn` text are wrong
about the invocation name. When you run or document the CLI, use `reachy`.
Test assertions and catalog text hard-code the literal `"reachy-mini-cli"`, so
do not "fix" the display name piecemeal; either leave it or do a complete,
deliberate rename across `pyproject.toml` (`name`, `[project.scripts]`),
`prog=`, all `_commands/` + `explain/catalog.py` strings, the README, and the
test assertions in one pass.

## Commands

```bash
uv sync                                              # create .venv, install (dev deps incl. teken)
uv sync --extra daemon                               # + reachy-mini (the reachy-mini-daemon binary)
uv run reachy whoami                                 # run the CLI (NOT `reachy-mini-cli`)
uv run reachy daemon start                           # bring the local daemon up (needs [daemon] extra)
uv run pytest -n auto                                # full suite (parallel)
uv run pytest tests/test_cli.py::test_whoami_text    # a single test
uv run pytest --cov=reachy --cov-report=term         # with coverage (CI gate: fail_under=60)
uv run teken cli doctor . --strict                   # the agent-first rubric gate CI enforces
```

Lint stack (CI `lint` job runs all of these; line length is 100 everywhere):

```bash
uv run black --check reachy tests
uv run isort --check-only reachy tests
uv run flake8 reachy tests
uv run bandit -c pyproject.toml -r reachy             # B101/B404/B603 skipped in pyproject
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
```

## Architecture: the agent-first CLI

Everything routes through `reachy.cli.main()` → `_build_parser()`
(`reachy/cli/__init__.py`). The design exists to satisfy the **teken agent-first
rubric** (`teken cli doctor . --strict`), which gates CI — keep it green when
you touch the CLI.

- **Adding a verb:** create `reachy/cli/_commands/<verb>.py` exposing
  `register(sub)` (add a `--json` flag, `set_defaults(func=...)`), then import it
  and call `<verb>.register(sub)` inside `_build_parser()`. That is the only
  wiring step. Follow `whoami.py` as the canonical example.
- **Noun groups** (a subcommand with its own sub-verbs, like `cli`): when you
  call `p.add_subparsers(...)`, pass `parser_class=type(p)` so nested parse
  errors keep the structured error contract instead of falling back to
  argparse's default `stderr`/exit-2. A noun that has action-verbs must also
  expose an `overview` verb (rubric requirement) — see `cli.py`.
- **Error contract** (`reachy/cli/_errors.py`, `_output.py`): every failure
  raises `CliError(code, message, remediation)`; `_dispatch` catches it and
  wraps *any* other exception so no Python traceback ever leaks. `main()`
  pre-scans argv for `--json` into `_CliArgumentParser._json_hint` so even
  argparse parse-time errors (which fire before `args.json` exists) render as
  JSON when asked. Text errors are always two lines: `error: …` then `hint: …`
  (the `hint:` prefix is rubric-required). Exit policy: `0` success, `1` user
  error, `2` environment error, `3+` reserved.
- **Output split:** `_output.py` enforces results→stdout, errors+diagnostics→
  stderr, **never mixed**, in both text and JSON modes. Every verb takes
  `--json`.
- **`explain` catalog** (`reachy/explain/`): markdown keyed by command-path
  tuples in `catalog.py`'s `ENTRIES`. `test_every_catalog_path_resolves`
  verifies each catalog entry resolves — but nothing fails if you add a verb
  *without* a catalog entry, so add the `ENTRIES` key yourself when you add a
  verb.
- **Identity (`whoami`) & `doctor`:** `whoami` hand-parses `culture.yaml` with a
  line scanner (no YAML library) and walks up from `__file__` to find it.
  `doctor` re-implements the steward invariants (prompt-file-present,
  backend-consistency `claude`→`CLAUDE.md`, skills-present).
- **`daemon` noun & process module:** `device`/`app`/`move` are *clients* of a
  running daemon; `reachy/cli/_commands/daemon.py` + `reachy/daemon.py` are the
  other half — they start/stop/status the local `reachy-mini-daemon` OS process
  (background spawn + PID/log under `$REACHY_STATE_DIR` / `$XDG_STATE_HOME/reachy`,
  health-poll via `GET /api/daemon/status`). Pure stdlib (`subprocess`/`signal`/
  `urllib`); the daemon *binary* comes from the `[daemon]` extra. Its `overview`
  is hand-built (no `--transport sdk` line) — `daemon` does NOT use a transport,
  so it does not call `_robot.noun_overview`/`get_transport`. A missing binary
  raises a clean exit-2 `CliError` pointing at the `[daemon]` install.

## Hard constraints

- **Zero *base* runtime dependencies.** `pyproject.toml` keeps base
  `dependencies = []` on purpose; `teken` is dev-only. This is why `whoami`
  hand-rolls YAML parsing and `reachy/daemon.py` manages the daemon process with
  stdlib `subprocess`/`urllib` only. The **recommended default install is `pip
  install 'reachy-cli[daemon]'`** (pulls `reachy-mini` for the
  `reachy-mini-daemon` binary); the bare `pip install reachy-cli` is the
  HTTP-only *remote* profile. Keep the *base* dep-free — anything that needs
  `reachy-mini` (the `sdk` transport, the daemon binary) goes behind the
  `[daemon]`/`[sdk]` extras, never into base `dependencies`. Adding a base
  runtime dep needs an explicit decision; it would break the dependency-free
  remote profile the README sells.
- **Python ≥ 3.12** (uses `X | None`, `tomllib`, etc.).
- **Every PR bumps the version**, even docs/config/CI-only changes — the
  `version-check` CI job blocks the merge otherwise (it compares
  `pyproject.toml` version against `origin/main`). Use the `version-bump` skill;
  it also prepends a `CHANGELOG.md` entry. PyPI publish on push to `main` would
  fail on a duplicate version, hence the rule.

## CI / release

- `.github/workflows/tests.yml`: `test` (pytest + coverage + SonarCloud),
  `lint` (the stack above + the rubric gate), `version-check` (PR-only).
- SonarCloud quality gate (`sonar-project.properties`,
  `sonar.qualitygate.wait=true`) fails the `test` job on a red gate — but only
  when `SONAR_TOKEN` is set; token-less repos and fork PRs skip the scan and
  stay green.
- `publish.yml`: TestPyPI dev build on internal PRs, real PyPI publish on push
  to `main`, both via Trusted Publishing (no stored credentials). Dist name is
  `reachy-cli`.

## Skills (`.claude/skills/`)

Vendored **cite-don't-import** from `guildmaster` (provenance + re-sync
procedure in `docs/skill-sources.md`). **Do not edit skill script bodies** — only
the consumer-identifying prose in `SKILL.md` is adapted; lift real changes
upstream into guildmaster and re-vendor. Most relevant for day-to-day work:

- **`cicd`** — the PR lane (create PR, handle review feedback, poll CI/Sonar
  status). Requires `agex` on PATH.
- **`communicate`** — cross-repo issues + Culture mesh messages. Requires
  `agtag` on PATH. Issue posts auto-sign `- reachy-mini-cli (Claude)`.
- **`version-bump`**, **`run-tests`**, **`sonarclaude`**, **`pypi-maintainer`**,
  **`agent-config`**, and the devague chain (`think` → `spec-to-plan` →
  `assign-to-workforce`).
