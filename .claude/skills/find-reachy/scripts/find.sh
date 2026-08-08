#!/usr/bin/env bash
# find.sh — the agent-facing front for `reachy wireless` (the /find-reachy skill).
#
# This script contains NO discovery logic of its own — no /proc parsing, no
# `ip`/`arp`/`avahi-browse`/`nmap`, no sockets, no curl against the daemon, no
# reimplemented filtering. Discovery lives in exactly one place: the CLI's
# `reachy.discover` package, wrapped by `reachy wireless {find,list,ssh,
# authorize,pin,unpin,forget,overview}`. This wrapper only resolves the CLI
# portably and forwards to it, on the same cite-don't-import discipline the
# `.claude/skills/think/scripts/think.sh` wrapper follows for `devague`.
#
# Origin: authored in agentculture/reachy-mini-cli, first-party (this is the
# tool's own skill, not a vendored one).

set -euo pipefail

# ── resolve the reachy CLI (installed-first, then local-dev fallback) ───────
REACHY=()
resolve_reachy() {
    if command -v reachy >/dev/null 2>&1; then
        REACHY=(reachy)              # installed console script — the normal case
        return 0
    fi
    if command -v reachy-mini-cli >/dev/null 2>&1; then
        REACHY=(reachy-mini-cli)     # the same entry point under its dist name
        return 0
    fi
    # Local-dev fallback: inside the reachy-mini-cli checkout, run via uv.
    local dir="$PWD"
    while [ -n "$dir" ] && [ "$dir" != "/" ]; do
        if [ -f "$dir/pyproject.toml" ] \
            && grep -q '^name = "reachy-mini-cli"' "$dir/pyproject.toml" 2>/dev/null; then
            if command -v uv >/dev/null 2>&1; then
                REACHY=(uv run reachy)
                return 0
            fi
            break
        fi
        dir=$(dirname "$dir")
    done
    cat >&2 <<'EOF'
error: reachy CLI not found.
hint: install it with `pip install 'reachy-mini-cli[daemon]'` (or the bare
      `pip install reachy-mini-cli` for the HTTP-remote profile — wireless
      discovery needs neither the [sdk] nor the [daemon] extra), or run from
      inside the reachy-mini-cli checkout with `uv` available.
      https://github.com/agentculture/reachy-mini-cli
EOF
    return 1
}

usage() {
    cat <<'EOF'
find.sh — find, remember and log into a Reachy Mini (the /find-reachy skill).

Usage:
  find.sh                       find the robot: `reachy wireless find --json`
  find.sh <verb> [args...]      forward one `reachy wireless` verb verbatim
  find.sh help                  this help

Verbs (forwarded to `reachy wireless <verb>`; run `find.sh overview` for the
full noun summary):
  find        sweep the LAN (or one --address) for Reachy daemons, remember them
  list        the remembered units, from the registry alone (no network)
  ssh         open a shell on the resolved unit (never types an address)
  authorize   install this box's SSH key on the unit, after explicit confirmation
  pin         pin the unit's address to a stable /etc/hosts alias (needs sudo)
  unpin       remove that managed /etc/hosts block
  forget      drop a remembered unit from the registry (no network)
  overview    the wireless noun's own summary

With NO arguments this script runs `reachy wireless find --json` — the
single most useful default for an agent: a cold sweep answers in a few
seconds, a warm resolve from the registry in well under a second, and every
unit in the result carries a ready-made `base_url` to pass to --base-url or
REACHY_BASE_URL. Every other verb is forwarded exactly as typed, including
--json, so `find.sh list --json` / `find.sh ssh --unit reachy-mini --dry-run`
etc. all work without editing this script.

This script performs NO discovery itself — it only resolves the `reachy` CLI
and execs it. All discovery logic lives in `reachy/discover/` and is wrapped
by the `reachy wireless` noun; see that noun's own `overview` for the
IPv4-and-default-port boundary and the trusted-network cost.
EOF
}

main() {
    case "${1:-}" in
        help | -h | --help)
            usage
            return 0
            ;;
        "")
            # No verb given: the one default an agent almost always wants.
            resolve_reachy
            exec "${REACHY[@]}" wireless find --json
            ;;
        *)
            # Forward every other verb to `reachy wireless` verbatim, so a new
            # wireless verb works here without editing this script.
            resolve_reachy
            exec "${REACHY[@]}" wireless "$@"
            ;;
    esac
}

main "$@"
