# Build Plan — reachy-mini-cli finds your Reachy Mini Wireless on any network in one command, remembers which unit is yours, and logs you in — with a skill wrapper so an agent can do it too

slug: `reachy-mini-cli-finds-your-reachy-mini-wireless-on` · status: `exported` · from frame: `reachy-mini-cli-finds-your-reachy-mini-wireless-on`

> reachy-mini-cli finds your Reachy Mini Wireless on any network in one command, remembers which unit is yours, and logs you in — with a skill wrapper so an agent can do it too

## Tasks

### t1 — reachy/discover package skeleton + probe.py: the HTTP identity probe

- instruction: Create reachy/discover/{`__init__.py`,probe.py}. probe(host, port=8000, timeout=...) -> UnitRecord|None using urllib only. UnitRecord is a frozen dataclass. Cite reachy/robot/`http_transport.py` for URL shape. Never raise to the caller.
- covers: c6, h5
- acceptance:
  - probe(host) against a fake daemon returning `wireless_version`=true yields a frozen UnitRecord carrying `hardware_id`, `robot_name`, model, wireless, version, `wlan_ip`, address
  - a non-200, a connection refusal, a timeout, and valid JSON that is not a Reachy daemon each return None — the caller never sees an exception
  - the probe issues exactly one GET to /api/daemon/status and no other request, asserted against a recording stub, proving it neither arms motors nor opens a media session

### t2 — reachy/discover/sweep.py: interface enumeration and the bounded concurrent sweep

- instruction: Create reachy/discover/sweep.py. Enumerate interfaces WITHOUT new deps (socket + parsing /proc/net or ioctl; no netifaces). Reject prefixes wider than /24, loopback, and docker/bridge ranges. Dedupe overlapping subnets. ThreadPoolExecutor with a hard cap + one overall deadline. Inject the interface source so tests never touch real NICs.
- depends on: t1
- covers: c7, h1, h2, h3
- acceptance:
  - given a fake interface table containing a /16 bridge, loopback, and a tailscale /32 alongside a real /24, enumeration returns ONLY the /24 hosts — a prefix wider than /24 is never expanded
  - two interfaces on the same subnet (mirroring 192.168.1.157 and 192.168.1.118) yield each host exactly once, and one unit answering on both is returned as a single record
  - with every host blackholing, the sweep returns within its overall deadline and the worker pool never exceeds its cap

### t3 — reachy/discover/registry.py: the per-user unit registry

- instruction: Create reachy/discover/registry.py over `state_dir`()/units.json keyed by `hardware_id`. Mirror reachy/stash/store.py: tempfile + os.replace, degrade-to-empty on corrupt. MAC lookup reads the neighbour table and returns None on any failure.
- depends on: t1
- covers: c9, h6, h7, h8, c26, h31
- acceptance:
  - records persist to `state_dir`()/units.json keyed by `hardware_id`, holding mac, `last_ip`, name, model, wireless and `last_seen`
  - a missing, empty, truncated and syntactically invalid file each load as an empty registry without raising
  - two interleaved writers always leave valid parseable JSON holding one writer's complete state, never a truncated or merged file
  - MAC enrichment returns None when the neighbour table is unavailable or the host is off-segment, and the record remains valid and identifiable without it

### t4 — reachy/discover/hosts.py: the recoverable /etc/hosts managed block

- instruction: Create reachy/discover/hosts.py. Markers '# BEGIN reachy-mini-cli' / '# END reachy-mini-cli'. Backup, write via tempfile+os.replace, re-read and verify localhost resolves, restore on failure. Pin '<ip> reachy-mini reachy-mini.local'. Take the hosts path as a parameter so tests never touch /etc/hosts.
- depends on: t1
- covers: c24, h29
- acceptance:
  - the write touches only a block delimited by BEGIN/END markers; every byte outside it is preserved verbatim, proven against a fixture reproducing this box's two-line hosts file
  - a write failing midway and a write whose post-verification fails both leave the file byte-identical to the pre-write backup
  - re-running the pin is idempotent — no duplicate line — and re-pinning a moved unit replaces the stale address rather than appending
  - unpin removes the managed block and leaves every other line untouched; every test runs against a temp-file stand-in and never the real /etc/hosts
  - the pinned line carries BOTH names — '<ip> reachy-mini reachy-mini.local' — and no test depends on the .local form resolving through /etc/hosts, since nsswitch may route .local to mDNS only
  - a backup is written before the first modification, and post-write verification re-reads the file and asserts localhost still resolves before the write is accepted

### t5 — reachy/discover/ssh.py: login and the explicit key-install path

- instruction: Create reachy/discover/ssh.py. Build argv only; inject the exec/subprocess seam so tests assert argv without spawning ssh. Always include -o HostKeyAlias=reachy-mini. Default user 'pollen'. authorize() requires an explicit confirm callback returning True before ssh-copy-id.
- depends on: t1
- covers: c10, h14, c25, h30
- acceptance:
  - the login account resolves as --user then `REACHY_WIRELESS_SSH_USER` then a documented default, asserted for all three precedence levels
  - the ssh invocation always carries -o HostKeyAlias=<alias> alongside the resolved IP, so host-key identity holds with no /etc/hosts pin and no privilege
  - authorize refuses without an explicit confirmation naming the target `hardware_id`, and a declined confirmation invokes ssh-copy-id exactly zero times
  - authorize is never reachable from find or ssh — asserted structurally, not just behaviourally — and reports plainly when the key was already installed
  - the documented default account is 'pollen' and the default HostKeyAlias is 'reachy-mini'; the alias is never derived from the daemon's `robot_name` field, which reports the underscore form `reachy_mini`
  - authorize tolerates an interactive password prompt (the unit ships the factory default) and never logs, echoes or stores the password

### t6 — tests/conftest.py: the sixth autouse guard against sweeping a live network

- instruction: Add a sixth autouse fixture to tests/conftest.py beside `_no_live_event_broker`, following its docstring style. It must neutralise sweep.py's injected interface source process-wide.
- depends on: t2
- covers: c22, h27
- acceptance:
  - the guard is autouse and default-on, matching the five existing guards, so a new test author cannot forget it
  - under the guard, interface enumeration yields the injected set and never a real interface — asserted by calling the real enumeration entry point inside a test
  - a test that attempts to sweep the live LAN fails loudly rather than silently scanning it

### t7 — reachy/discover/resolve.py: the fast path and multi-unit selection

- instruction: Create reachy/discover/resolve.py composing registry+probe+sweep. Fast path first, verify `hardware_id`, escalate on mismatch, re-pin. Ambiguity raises CliError naming candidates.
- depends on: t2, t3
- covers: c8, h9, h15, c23, h28
- acceptance:
  - a remembered unit answering at its last-known IP with a matching `hardware_id` is returned without any sweep being invoked
  - a remembered IP now answering as a DIFFERENT `hardware_id` is rejected, escalates to the sweep, and the record is re-pinned to the new address
  - a dark remembered IP costs one short bounded timeout before escalating, not a full per-unit connect timeout
  - with two units in the registry, resolution with no selector exits non-zero naming both candidates and never picks one

### t8 — tests: the stdlib-only import boundary for reachy/discover

- instruction: Add tests/`test_discover_boundary.py` in the AST style of tests/`test_zero_llm_boundary.py`. Assert stdlib-only imports across reachy/discover/\*. Do not modify pyproject.toml.
- depends on: t4, t5, t6, t2, t3, t7
- covers: c12, h22
- acceptance:
  - an AST walk in the style of tests/`test_zero_llm_boundary.py` asserts every module under reachy/discover imports only the standard library
  - pyproject's base dependency list still holds exactly the three entries tests/`test_dep_freeze.py` pins, and that test passes unmodified

### t9 — reachy/cli/`_commands`/wireless.py: the noun, wired into the parser and the explain catalog

- instruction: Create reachy/cli/`_commands`/wireless.py with register(sub), import + register it in reachy/cli/`__init__.py` `_build_parser`(), add ENTRIES keys in reachy/explain/catalog.py. Follow device.py for structure and `_errors.py` for the error contract. Keep teken cli doctor --strict green.
- depends on: t6, t7, t4, t5
- covers: c2, h18, c15, h23, c29, h32
- acceptance:
  - verbs find / list / ssh / authorize / pin / unpin / forget / overview each register, each accept --json, and errors render as the two-line error:/hint: contract in both modes
  - find defaults to `wireless_version`=true units and a documented flag reveals every Reachy daemon the sweep saw, so the noun's name describes the default rather than a limit
  - the whole noun works on a bare install with neither the \[sdk\] nor the \[daemon\] extra present
  - `DEFAULT_BASE_URL` is still <http://localhost:8000> and reachy/robot/transport.py is unmodified — the existing transport tests pass untouched
  - reachy/explain/catalog.py carries an ENTRIES key per new verb, and 'uv run teken cli doctor . --strict' stays green
  - the overview states the IPv4-and-default-port boundary, and a v6-only unit stays usable by explicit address
  - 'wireless find' emits the resolved `base_url` so an agent can pass it straight to --base-url or `REACHY_BASE_URL` without reformatting

### t10 — .claude/skills/find-reachy: the agent-facing skill wrapper

- instruction: Create .claude/skills/find-reachy/{SKILL.md,scripts/find.sh} mirroring .claude/skills/run-tests/ layout. find.sh only shells out to the CLI.
- depends on: t8, t9
- covers: c11, h16
- acceptance:
  - SKILL.md plus scripts/find.sh follow the run-tests layout and resolve the CLI portably
  - the script shells out to 'reachy wireless find --json' and contains no discovery logic of its own — asserted by a grep-style test over the script body

### t11 — docs: operating guide, README and CLAUDE.md sections for the wireless noun

- instruction: Update docs/operating-reachy.md, README.md and CLAUDE.md (noun catalog + a wireless internals section). Run markdownlint-cli2 and the version-bump skill.
- depends on: t8, t9
- covers: c3, h19, c5, h21
- acceptance:
  - docs/operating-reachy.md gains a discovery section covering the manual before-state, the sudo cost of pinning, and the trusted-network assumption
  - CLAUDE.md gains a wireless entry in the noun catalog naming the module map and the stdlib-only constraint
  - markdownlint-cli2 passes on every changed file, and the version is bumped with a CHANGELOG entry via the version-bump skill
  - the docs state plainly that the unit ships with a factory-default password, that discovery makes it easier to find on the LAN, and that changing the password is the operator's first move

### t12 — live on-box verification and the measured success signals

- instruction: OPERATOR-RUN, not delegated: needs the powered unit, the LAN and sudo. Record results under docs/evidence/2026-08-08-wireless-discovery-verification.md.
- depends on: t8, t10, t9
- covers: c1, h17, c4, h20, c16, h4, c21, h24
- acceptance:
  - from this dev box with the registry cleared, the CLI finds the Wireless unit at its live address, distinguishes it from the co-resident Lite, remembers it, resolves it again without a sweep, and opens a shell on it
  - cold discovery on the /24 answers under 5s and is hard-bounded at 10s; a warm resolve answers under 500ms; both recorded from real runs with the seven Docker bridges present
  - the Lite is misreported as the Wireless unit in 0 of 20 consecutive runs
  - measurements land under docs/evidence/ on the existing on-box verification pattern, and any target that proves unreachable is revised in the open rather than dropped
  - 'ssh pollen@reachy-mini' succeeds from this box after pinning — it currently fails with a name-resolution error, which is the before/after this feature is measured by

## Risks

- [unknown_nonblocking] The SSH login account on the shipped Reachy Mini Wireless image is unconfirmed — a probe as reachy@ was refused with publickey,password, which proves sshd is live but never confirms an account exists. t7 needs a documented default; de-risked by the --user / env override, but the default may ship wrong (task t7)
- [unknown_nonblocking] The stable alias string is undecided (parked v3). t6 and t7 both need it — hosts pin target and HostKeyAlias value — and it must be operator-chosen, since the advertised reachy-mini-2.local is an avahi collision artifact that can move (task t6)
- [unknown_nonblocking] t12 needs the physical Wireless unit powered and on the LAN, plus sudo for the pin path. It cannot run in a worktree fan-out and must be executed by the operator on this box — the only task in the plan that is not offline-verifiable (task t12)
- [follow_up] Whether to build an mDNS accelerator at all stays deferred (parked v2): the robot advertises, but resolving its TXT record timed out twice live while the HTTP probe answered first attempt. Revisit only if t12 shows the sweep exceeding its 5s target
- [follow_up] No first-contact trust mechanism is specified (parked v4). On a shared LAN the sweep will surface robots that are not the operator's, and c28 records this as an accepted assumption rather than a solved problem
