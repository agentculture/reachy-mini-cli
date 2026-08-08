# Delivery Summary — wireless unit discovery

plan: `reachy-mini-cli-finds-your-reachy-mini-wireless-on` · run: `partial` · date: `2026-08-08`
baseline: `devague summary skeleton`

## Intent

Ship a `reachy wireless` noun that finds a Reachy Mini Wireless on the LAN,
remembers which unit is the operator's across IP changes, pins a stable
`/etc/hosts` alias, and opens a login shell — plus an agent-facing skill
wrapper. Twelve tasks were fanned out across six dependency waves to isolated
git worktrees, each TDD-gated before merge.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — reachy/discover package skeleton + probe.py: the HTTP identity probe
- `t2` — reachy/discover/sweep.py: interface enumeration and the bounded concurrent sweep
- `t3` — reachy/discover/registry.py: the per-user unit registry
- `t4` — reachy/discover/hosts.py: the recoverable /etc/hosts managed block
- `t5` — reachy/discover/ssh.py: login and the explicit key-install path
- `t6` — tests/conftest.py: the sixth autouse guard against sweeping a live network
- `t7` — reachy/discover/resolve.py: the fast path and multi-unit selection
- `t8` — tests: the stdlib-only import boundary for reachy/discover
- `t9` — reachy/cli/`_commands`/wireless.py: the noun, wired into the parser and the explain catalog
- `t10` — .claude/skills/find-reachy: the agent-facing skill wrapper
- `t11` — docs: operating guide, README and CLAUDE.md sections for the wireless noun
- `t12` — live on-box verification and the measured success signals

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `reachy/discover/{__init__,probe}.py`; 27 tests. `UnitRecord` frozen dataclass; `model` derived from `wireless_version` since the daemon payload carries no `model` field |
| `t2` | delivered | `reachy/discover/sweep.py`; 49 tests. `socket.if_nameindex()` + two ioctls; four independent filters reduce this box's 11-row table to one network, 254 hosts |
| `t3` | delivered | `reachy/discover/registry.py`; 34 tests. `units.json` keyed by `hardware_id`; concurrency proven with real threads and a slowed serializer |
| `t4` | delivered | `reachy/discover/hosts.py`; 53 tests. Backup → atomic write → re-read verify → auto-restore; mutation-checked |
| `t5` | delivered | `reachy/discover/ssh.py`; 44 tests. `authorize` unreachable from the shell path, proven by an AST call-graph walk with a non-vacuity guard |
| `t6` | delivered | Sixth autouse guard `_no_live_lan_sweep`; 4 tests. Proven load-bearing: reverting it made the suite enumerate 254 real hosts |
| `t7` | delivered | `reachy/discover/resolve.py`; 22 tests. Fast path, identity verification, re-pin on mismatch, ambiguity refusal |
| `t8` | delivered | `tests/test_discover_boundary.py`; 7 tests. Non-vacuity proven three ways, including injecting a real `import zeroconf` |
| `t9` | delivered | `reachy/cli/_commands/wireless.py`; 78 tests. Eight verbs, catalog entries, `teken --strict` green |
| `t10` | delivered | `.claude/skills/find-reachy/`; 24 tests. Script holds no discovery logic; `shellcheck` clean |
| `t11` | delivered | Operating guide, `CLAUDE.md`, `README.md`; version `0.48.0` + CHANGELOG + refreshed `uv.lock` |
| `t12` | **partial** | Cold/warm timings, 20-run disambiguation, filter, ambiguity, ssh argv, pin/unpin and the **full end-to-end name→robot path** verified live. Not run: the pin against *this box's own* `/etc/hosts` (sudo password unavailable), and an interactive shell (no key installed) — see Drift |

## Mid-work Decisions

No `devague deviate` records were filed during this run; the decisions below are
captured directly.

- **A structural guard was being evaded; I had it removed instead.** `t3` needed
  the procfs ARP table for MAC lookup, which trips a repo-wide guard reserving
  procfs reads to `reachy/procsup.py`. The agent worked around it by building the
  path as `"/proc" + "/net/arp"` so the literal substring never appears. That
  makes the guard silently stop guarding. The fallback was deleted outright
  (`434ee60`); `ip neigh` is now the only path, which is the modern interface,
  and MAC is explicitly opportunistic so nothing was lost.
- **A live module-shadowing bug was found at merge and fixed.** `t2` discovered
  that re-exporting a function named `sweep` from `__init__.py` rebinds the
  package attribute from module to function, breaking every `monkeypatch.setattr`
  seam. Checking it, `probe` had **already shipped with the same bug** —
  `import reachy.discover.probe as m` was returning the function. Both dropped;
  the rule is generalized in the package docstring (`e51527f`).
- **A stale doc claim was corrected outside plan scope.** `docs/operating-reachy.md`
  still said there were "exactly two base runtime deps"; `events-cli` became the
  third on 2026-07-24. Left alone it would have contradicted the new discovery
  section's "no new dependency" claim on the same page (`4cb1757`).
- **`t11`'s worktree predated `t10`'s merge**, so the skill was undocumented; a
  section was added at integration (`4cb1757`).
- **Two timeouts, deliberately different.** `probe.DEFAULT_TIMEOUT` stays 1.0 s
  for the single-address fast path; `sweep` defines its own 0.5 s because it asks
  254 hosts of which ~253 never answer.
- **`t4` refreshes the backup before *every* write**, not only the first, so the
  `.bak` always holds the exact pre-write bytes the rollback criterion names.
- **`t6` chose an empty return over a raise** for the guard, because
  `enumerate_hosts`/`sweep` both catch and fold their source's exceptions into an
  empty result — a raising stub would be silently swallowed on the exact path the
  guard protects.
- **The `sudo`/registry interaction was documented, not patched.** Under `sudo`,
  `pin` may see root's empty registry; `pin --address <ip>` skips unit resolution
  entirely and is documented as the deterministic form.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t12` | The pin was not executed against *this box's own* `/etc/hosts` — it is `root:root 0644` and `sudo -n` returns `sudo: a password is required`. It WAS executed for real against the real `/etc/hosts` path in a host-networked namespace, proving resolution, reachability of this robot by the pinned name, and a byte-identical `unpin`. What remains unverified is narrower than first recorded: the host's own file, and an interactive shell (ssh reached the robot's sshd and was refused at authentication, having no key) | needs-follow-up |
| `t3` | Shipped with a procfs fallback that evaded a structural guard by string concatenation; removed post-merge rather than kept | acceptable |
| `t1` | Shipped a `probe` re-export that had already broken the module injection seam; corrected during the `t2` merge | acceptable |
| `t10` | Shipped failing `black` and `flake8 E501`, which CI would have blocked; fixed at integration (`ebaa53b`) | acceptable |
| `t4` | Shipped a byte-identical guarantee that was **false for any hosts file not ending in a newline** — `pin` inserted a separator newline that `unpin` never removed. All 53 tests, and the live check reported in the evidence doc, used a fixture ending in `\n` and shared the blind spot. Found by Qodo review on PR #161, not by this plan's own verification. Fixed by making the block carry the document's final-newline property; +33 tests over ten body shapes | needs-follow-up |
| `t1` | Shipped a `probe()` that never bracketed IPv6 literals, so a remembered v6 `last_ip` produced an unparseable URL and silently degraded to "not found" — `wireless ssh`/`authorize`/`pin` were unusable for a v6-only unit. Found by Qodo review, not by this plan's verification (v6 is a documented out-of-scope boundary, but the code accepted and persisted v6 addresses regardless). Fixed in `probe()`, the single chokepoint; +19 tests | needs-follow-up |

## Evidence

- tests: full suite `uv run pytest -n auto -q` — **5307 passed, 7 skipped**
  (the 7 are pre-existing: missing `cv2`, unreachable LLM gateway)
- tests added by this run: 342 across
  `tests/test_discover_{probe,sweep,registry,hosts,ssh,resolve,boundary,sweep_guard}.py`,
  `tests/test_wireless_cli.py`, `tests/test_find_reachy_skill.py`
- lint: `black --check` / `isort --check-only` / `flake8` — clean
- lint: `bandit -c pyproject.toml -r reachy` — Low 0, Medium 0, High 0
- lint: `markdownlint-cli2` over all 46 tracked markdown files — 0 errors
- rubric: `uv run teken cli doctor . --strict` — exit 0
- commits: `30ef881..ebaa53b` (26 commits on `feat/wireless-discovery`)
- live evidence: `docs/evidence/2026-08-08-wireless-discovery-verification.md`
  and `docs/evidence/2026-08-08-wireless-discovery-20runs.csv`

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| Cold discovery answers under the 5 s target | high | 20 runs, mean 3.861 s, max 3.975 s — `docs/evidence/2026-08-08-wireless-discovery-20runs.csv` |
| Warm resolve answers under the 500 ms target | high | 0.225 s measured live — evidence doc, "Warm resolve" |
| The sweep never expands a prefix wider than /24 | high | 254 hosts probed live with seven Docker /16 bridges present · `tests/test_discover_sweep.py` (49) |
| The Lite is never misreported as the Wireless unit | high | 0 of 20 runs; live filter refusal reproduced — evidence doc |
| Ambiguity is refused, never guessed | high | reproduced live with two real units · `tests/test_discover_resolve.py` (22) |
| The suite can never sweep a live LAN | high | `tests/test_discover_sweep_guard.py` (4); reverting the guard made the suite enumerate 254 real hosts |
| `reachy/discover/` adds no dependency | high | `tests/test_discover_boundary.py` (7), non-vacuity proven by injecting `import zeroconf` |
| The `/etc/hosts` block is idempotent and fully reversible | medium | round-trip property over ten body shapes incl. no-trailing-newline, CRLF and bare-CR · `tests/test_discover_hosts.py` (111). **This claim was overstated until review** — see Drift `t4` |
| `pin` refuses any file that is not already a hosts document | high | measured: a shadow-format file and an `authorized_keys` both refused exit-2, byte-unchanged, no backup written — evidence doc, "Arbitrary-write refusal" |
| `authorize` cannot be reached from the shell path | high | AST call-graph walk with non-vacuity guard · `tests/test_discover_ssh.py` (44) |
| The noun works on a bare install with no extras | high | subprocess test in a fresh interpreter asserting `reachy_mini` never enters `sys.modules` |
| `ssh pollen@reachy-mini` resolves and reaches this robot | high | pin run for real against the real `/etc/hosts` path in a host-networked namespace: before = `Could not resolve hostname`; after = `getent` → 192.168.1.162, `http://reachy-mini:8000` returned `hardware_id=a89063c05ae79779`, ssh completed an ED25519 key exchange with the robot's sshd — evidence doc, "The end-to-end claim" |
| An interactive shell opens over that name | **unverified** | ssh was refused at *authentication* (`publickey,password`) with no key installed — needs `authorize` or the factory password |
| The box's OWN `/etc/hosts` is pinned | **unverified** | sudo password unavailable; the host file is deliberately unmodified (still 56 bytes, no `.bak`) |
| `wireless authorize` installs a key on the real robot | **unverified** | not run unattended against the live unit — offline tests only |

## Remaining Work / Follow-up

- **`t12` — run the live pin and confirm the end-to-end claim.** Owner: operator
  (needs sudo).

  ```bash
  sudo reachy-mini-cli wireless pin --address 192.168.1.162
  ssh pollen@reachy-mini
  ```

  Until this runs, two delivery claims stay `unverified` and the before-state in
  the evidence doc still stands.
- **Exercise `wireless authorize` against the live unit.** It would push a key
  using the factory-default password; deliberately not run unattended.
- **Never exercised live:** an IPv6-only unit, and a non-default daemon port.
  Both are documented boundaries (`c29`), not gaps.
- **Parked, unchanged by this run:** `v2` (whether to build an mDNS accelerator —
  revisit only if the sweep misses its target; it did not), `v4` (a first-contact
  trust mechanism on shared networks, with `c28`'s trusted-network assumption
  recorded rather than solved).
- **Security follow-up for the operator, not this codebase:** the unit ships the
  factory-default password `root` for the `pollen` account and its daemon
  endpoint is unauthenticated. Discovery makes it easier to find. Changing that
  password is the first move.
