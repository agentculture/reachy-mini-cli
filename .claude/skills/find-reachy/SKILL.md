---
name: find-reachy
type: command
description: >
  Find a Reachy Mini Wireless on the local network without a human typing an
  IP: sweep the LAN, remember which unit is yours, and log into it over SSH.
  Wraps `reachy wireless` (`reachy/discover/`) — a stdlib-only HTTP probe plus
  a bounded, capped, deadline-limited subnet sweep — so an agent gets the same
  discovery a human operator has. Use when the user says "find my reachy",
  "where is the robot", "what's the robot's IP", "ssh into the robot", "pin
  the robot's hostname", or when an agent needs the robot's `base_url` before
  driving it with `--base-url` / `REACHY_BASE_URL`.
---

# find-reachy — locate, remember and log into a Reachy Mini

The skill is named **`find-reachy`**; the CLI it drives is **`reachy wireless`**
(module: `reachy/discover/`). Every robot session starts with "where is it" —
this skill answers that without a human running `ip addr` / a ping sweep /
`avahi-browse` / a manual `curl :8000` by hand.

This is a **thin wrapper**, on purpose. `scripts/find.sh` contains **no
discovery logic of its own** — no `/proc` parsing, no `ip`/`arp`/
`avahi-browse`/`nmap`, no sockets, no direct `curl` against the daemon, no
reimplemented filtering. It only resolves the `reachy` CLI portably and execs
it, exactly the way `.claude/skills/think/scripts/think.sh` resolves
`devague`. All discovery logic lives in exactly one place —
`reachy/discover/` — so this skill can never drift from the tool it wraps.

## How to run

```bash
bash .claude/skills/find-reachy/scripts/find.sh                # find the robot
bash .claude/skills/find-reachy/scripts/find.sh <verb> [args...]
```

With **no arguments** the script runs `reachy wireless find --json` — the one
default an agent almost always wants: a sweep of the local network that
returns every Reachy Mini Wireless it saw, each carrying a ready-made
`base_url` (`http://<ip>:<port>`) to pass straight to `--base-url` or
`REACHY_BASE_URL`. Every other verb is **forwarded verbatim**, so
`find.sh <verb> --json` etc. all work without editing this script:

| Verb | What it does |
|------|--------------|
| `find` | sweep the LAN (or one `--address`) for Reachy daemons and remember them |
| `list` | the remembered units, from the registry alone (no network) |
| `ssh` | open a shell on the resolved unit (never types an address) |
| `authorize` | install this box's SSH key on the unit, after explicit confirmation |
| `pin` | pin the unit's address to a stable `/etc/hosts` alias (needs sudo) |
| `unpin` | remove that managed `/etc/hosts` block |
| `forget` | drop a remembered unit from the registry (no network) |
| `overview` | the `wireless` noun's own summary |

It resolves the CLI portably — an installed `reachy` (or `reachy-mini-cli`) on
`PATH`, falling back to `uv run reachy` inside the `reachy-mini-cli` checkout.
If neither resolves it prints an install hint
(`pip install 'reachy-mini-cli[daemon]'`).

## Why `find` defaults to `--json`

This skill is agent-facing, so the no-args default is JSON, not the
human-readable text a person would want in a terminal. Every unit in the
payload carries the fields an agent needs to act immediately:

```json
{"units":[{"hardware_id":"a89063c05ae79779","robot_name":"reachy_mini","model":"Reachy Mini Wireless",
"wireless":true,"version":"1.9.0","wlan_ip":"192.168.1.162","address":"192.168.1.162","port":8000,
"base_url":"http://192.168.1.162:8000"}],"count":1,"found_total":1,"wireless_only":true}
```

`find` filters to `wireless_version=true` units by default — a Lite tethered
to another box on the LAN is discoverable too, but is not wireless. Pass
`--all` (forwarded through this script, e.g. `find.sh find --all`) to see
every Reachy daemon the sweep answered for.

## Measured behaviour

- **Cold discovery** (no remembered unit, a full LAN sweep): ~3.7 s on a box
  with seven Docker bridge interfaces present, hard-bounded well inside the
  CLI's own 10 s deadline.
- **Warm resolve** (a remembered unit tried first at its last-known IP, verified
  by `hardware_id`): ~0.2 s — no sweep runs at all unless the fast path misses.
- `pin` (and only `pin`/`unpin`) needs `sudo`, because it writes a managed
  block in `/etc/hosts`; every other verb — `find`, `list`, `ssh`, `authorize`,
  `forget`, `overview` — needs no privilege at all.
- The unit ships with a factory-default password. Discovery makes it easier to
  find on the LAN, so changing that password is the operator's first move —
  `find-reachy` does not do this for you.

## Worked example

```bash
f() { bash .claude/skills/find-reachy/scripts/find.sh "$@"; }

f                                   # sweep + remember: reachy wireless find --json
f list --json                       # what's already remembered (no network)
f ssh --dry-run                     # show the resolved unit + ssh argv, don't connect
f ssh                               # actually open a shell (ssh pollen@reachy-mini by default)
f pin --json                        # pin /etc/hosts so `ssh pollen@reachy-mini` works everywhere
f overview --json                   # the wireless noun's own summary, structured
```

## Output contract

Same contract as the CLI it wraps: results to **stdout**, errors and
diagnostics to **stderr**, never mixed. Exit code `0` on success, `1` on a
user error (nothing found, an ambiguous selector, a declined confirmation),
`2` on an environment error (an unwritable `/etc/hosts`, a missing `ssh`
client). Registry state lives under the CLI's own state dir, keyed by
`hardware_id` — nothing here is committed to the repo.

## Provenance

This is a **first-party** skill, authored in `agentculture/reachy-mini-cli`
alongside the `reachy wireless` noun it wraps — it is not vendored from
guildmaster/steward like the sibling skills under `.claude/skills/`. The
`cite, don't import` policy still applies inside this repo: the script cites
the CLI by shelling out to it, never by re-implementing any part of
`reachy/discover/`.
