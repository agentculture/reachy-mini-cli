# reachy-mini-cli finds your Reachy Mini Wireless on any network in one command, remembers which unit is yours, and logs you in — with a skill wrapper so an agent can do it too

> reachy-mini-cli finds your Reachy Mini Wireless on any network in one command, remembers which unit is yours, and logs you in — with a skill wrapper so an agent can do it too

## Audience

- The operator driving a Reachy Mini Wireless from a dev box that is NOT the robot (this box already hosts a Lite), plus mesh agents that need the robot's address without a human typing it

## Before → After

- Before: Finding the unit is a manual forensic exercise: ip addr to learn the subnet, a 254-host ping sweep, avahi-browse, then a curl against :8000 to tell Wireless from Lite. It took ~6 tool calls in a live session, mDNS resolve timed out twice, and the answer (an IP) is volatile DHCP state that is lost by the next session
- After: One command prints the unit's IP, model, wireless flag and hardware id; a second invocation answers from a remembered unit in well under a second; and one more command opens a shell on it — no IP typed by hand, ever

## Why it matters

- Every robot session starts with 'where is it', and today that question costs minutes and re-derives knowledge the box already had. The unit's identity is stable; only its IP moves. Discovery should be a solved, cached fact, not a rediscovery ritual

## Requirements

- Ground truth is an HTTP probe of GET :8000/api/daemon/status, not mDNS. The payload self-identifies the unit (`wireless_version`, `hardware_id`, `robot_name`, version, `wlan_ip`, `camera_specs_name`), which is the ONLY thing observed to reliably tell a Wireless from a Lite. mDNS is at most an optional accelerator
  - instruction: Add reachy/discover/probe.py: a stdlib urllib GET of http://<host>:8000/api/daemon/status with a short per-host timeout, parsed into a frozen UnitRecord (`hardware_id`, `robot_name`, model, `wireless_version`, version, `wlan_ip`, address). Any non-200, timeout, or non-Reachy JSON is a miss, never an exception to the caller
  - honesty: The probe is read-only and safe against a LIVE robot: GET /api/daemon/status neither arms motors nor claims the single-consumer media session, so discovery can run beside a live behavior engine without disturbing it
- Discovery falls back to a bounded concurrent sweep of the local IPv4 subnet(s), probing the daemon port on each host with a short timeout and a capped thread pool
  - instruction: Add reachy/discover/sweep.py: enumerate local IPv4 interfaces, keep only prefixes /24 or narrower, drop loopback and Docker/bridge ranges, dedupe overlapping subnets to a set of unique hosts, then fan out probe() over a bounded ThreadPoolExecutor under one overall deadline. Cite the hazard in the module docstring: this box carries seven 172.x /16 bridges
  - honesty: The sweep enumerates ONLY /24-or-smaller subnets. This box carries seven Docker bridge networks on 172.x /16 (docker0 plus six br-\*); enumerating those naively is ~459k hosts and would hang the CLI indefinitely. Bridges and any prefix wider than /24 must be excluded by construction, not by luck
  - honesty: The sweep has a hard overall deadline and a capped worker pool, so it always terminates and always terminates fast, whatever the subnet size or how many hosts blackhole
  - honesty: Two local interfaces on the SAME subnet (this box has 192.168.1.157 and 192.168.1.118 on wlP9s9 and wlx90de80db7994) enumerate that subnet exactly once, and a unit reachable via both is reported as one unit, not two
- A remembered unit is tried FIRST by its last-known IP; a hardware-id match short-circuits the whole sweep. Only a miss escalates to a full scan, after which the unit's new IP is re-pinned
  - instruction: In reachy/discover/resolve.py, order the lookup: remembered unit's last-known IP (short timeout) -> identity check on `hardware_id` -> return on match; on mismatch or miss, escalate to sweep and re-pin the record
  - honesty: The fast path is verified, not assumed: a remembered IP that now answers as a DIFFERENT unit (DHCP handed the address to another device) is detected by identity mismatch and rejected, not returned
  - honesty: The fast path never makes the miss case slower in a way the operator notices: probing a remembered IP that is now dark costs a short bounded timeout before escalating to the sweep, not a full connect timeout per remembered unit
- Discovered units persist to a per-user registry under reachy.daemon.`state_dir`() — user-specific machine state, never committed to the repo — recording identity, MAC when observable, last-known IP and last-seen time
  - instruction: Add reachy/discover/registry.py: a JSON file at reachy.daemon.`state_dir`()/units.json keyed by `hardware_id`, holding mac, `last_ip`, name, model, wireless, `last_seen`. Load degrades to empty on missing/corrupt (mirror StashStore.index.json). MAC enrichment reads the neighbour table opportunistically and yields None when unavailable
  - honesty: A missing, empty or corrupt registry file degrades to 'start fresh' and never raises — the same discipline StashStore already applies to its index.json
  - honesty: MAC is recorded opportunistically, never required. It is only observable for a host on the same L2 segment via the ARP/neighbour table, so identity must remain correct when MAC is unavailable (different subnet, or a non-Linux box with no ip-neigh)
  - honesty: No unit-specific identity (MAC, hardware id, IP, hostname) is ever written into the repository or into a committed file — it lives only under the per-user state dir
- A login verb resolves the remembered unit and opens an SSH session to it, so the operator never types an address
  - instruction: Add 'reachy wireless ssh': resolve the unit, then os.execvp into ssh, passing -o HostKeyAlias=<alias>. User resolves as --user > `REACHY_WIRELESS_SSH_USER` > a documented default
  - honesty: The SSH login account is resolvable and overridable: a documented default plus a flag and an env var, since a wrong hard-coded username turns the headline verb into an authentication failure with a misleading message
- A skill wrapper under .claude/skills/ exposes the same discovery to an agent, following the run-tests scripts/<name>.sh pattern
  - instruction: Add .claude/skills/find-reachy/ with SKILL.md plus scripts/find.sh on the run-tests pattern; the script shells out to 'reachy wireless find --json' and contains no discovery logic of its own
  - honesty: The skill wrapper adds no second implementation. It shells out to the same CLI verbs, so discovery logic exists in exactly one place and the skill cannot drift from the tool it wraps — the cite-don't-import discipline the vendored skills already follow
- The suite must never sweep a real network. tests/conftest.py already carries FIVE autouse guards (daemon media, realtime gateway, event broker, voice out, audio tee) and pins `REACHY_BASE_URL` to an unreachable address — but a sweep reads NONE of them: it enumerates interfaces and probes hosts directly, so a discovery test would scan the live LAN in CI and could reach the deployed robot. Add a sixth autouse guard forcing subnet enumeration to an empty/injected set by default (challenge pass / observability+containment lens: tests/conftest.py:91-301)
  - honesty: The guard is autouse and default-on, exactly like the five it joins, so a new test author cannot forget it: with the guard active, subnet enumeration yields the injected set and never a real interface, and a test written to sweep the live LAN fails loudly rather than quietly scanning it
- Selection must be explicit when more than one unit is known — which is this operator's NORMAL case, not an edge: a Lite answers on localhost and the Wireless on the LAN, and both report `robot_name`=`reachy_mini`. 'unit ssh' with an ambiguous registry must refuse and name the candidates, or resolve via an explicit default/--unit selector, never silently pick one (challenge pass / overlooked-actors lens: live mDNS + daemon status from both units)
  - instruction: In resolve.py, raise a CliError listing every candidate alias plus `hardware_id` when the registry holds more than one match and no selector was given; accept --unit <`hardware_id`-or-alias> and an optional registry default flag
  - honesty: With two units in the registry, ssh invoked with no selector exits non-zero and names both candidates; it never picks one. Proven by a test over a two-entry registry, not by inspection
- The /etc/hosts write is recoverable, not merely careful: take a timestamped backup before the first write, verify after writing that the file still parses and still resolves localhost, roll back automatically if it does not, and ship an explicit unpin verb that removes the managed block. This box's entire hosts file is two lines (127.0.0.1 localhost / 127.0.0.1 spark-f8a9), so a botched rewrite that drops localhost breaks name resolution box-wide (challenge pass / reversibility+blast-radius lens: /etc/hosts, 56 bytes, root-owned, unmanaged by NetworkManager or systemd)
  - instruction: Back up to <hosts>.reachy-mini-cli.bak before the first write; after os.replace, re-read the file and assert localhost still resolves and the marker block parses, restoring the backup if either check fails
  - honesty: A write that fails midway, and a write whose result fails verification, both leave /etc/hosts byte-identical to the pre-write backup; unpin removes the managed block and leaves every byte outside it untouched. Both are proven against a temp-file stand-in, never against the real /etc/hosts in tests
- 'unit authorize' names the exact target and its `hardware_id`, and requires explicit operator confirmation before pushing a key, because the target was chosen by scanning rather than typed. The registry write and the ssh-copy-id call must be atomic-per-unit and never triggered by find or ssh (challenge pass / security lens: ssh-copy-id present at /usr/bin/ssh-copy-id; daemon endpoint is unauthenticated)
  - instruction: In the authorize verb, print the resolved target's alias, IP and `hardware_id` and require an explicit affirmative before invoking ssh-copy-id; the initial run authenticates with the factory password, so the flow must tolerate a password prompt and must not log or store it
  - honesty: authorize refuses without an explicit confirmation naming the target `hardware_id`, and a declined confirmation invokes ssh-copy-id zero times
- Registry writes are atomic and concurrency-safe (tempfile + os.replace), since a human shell and a mesh agent can both run discovery at once; h6 covers recovering from a corrupt file but not creating one (challenge pass / concurrency lens: reachy/stash/store.py index.json precedent)
  - instruction: Write units.json via tempfile + os.replace in the same directory, matching reachy/stash/store.py's index.json discipline
  - honesty: Two interleaved writers always leave valid parseable JSON holding one writer's complete state — never a truncated or merged file

## Honesty conditions

- The headline is verifiable end to end on real hardware: from this dev box, with the registry cleared, the CLI finds the Wireless unit at its live address, distinguishes it from the co-resident Lite, remembers it, resolves it again without a sweep, and opens a shell on it
- The feature works on the bare HTTP remote profile — a box with neither the \[sdk\] nor the \[daemon\] extra installed — since the audience is defined as operating a robot the box is not hosting. And every verb carries --json, so a mesh agent consumes the address without scraping human text
- The stated cost is drawn from an actual recorded session on this box, not estimated: the manual route really did take multiple probes, avahi-resolve really did time out twice on the wireless unit's TXT record, and the HTTP probe really did answer first attempt
- Both halves are measured on real hardware rather than asserted: the cold path's time-to-answer and the warm path's are each recorded, and the warm path is demonstrably the registry short-circuit rather than a faster sweep
- The design actually keys on the stable fact and treats the volatile one as disposable: a unit that changes IP between two invocations is still found, and re-pinned, with no manual step from the operator
- Machine-checkable, not merely intended: pyproject's base dependency list still holds exactly the three entries tests/`test_dep_freeze.py` pins, and every new discovery module's imports resolve to the standard library alone
- No existing default moves. `DEFAULT_BASE_URL` stays <http://localhost:8000>, `REACHY_BASE_URL` and --base-url keep their current precedence, and the existing transport tests pass untouched — discovery only supplies a value the operator or agent may pass in
- A co-resident Lite daemon is never reported as a LAN unit. The local Lite advertises `_reachy`-mini.`_tcp` on loopback AND on all seven Docker bridges with address=127.0.0.1; loopback/bridge-local and `wireless_version`=false results are excluded or clearly labelled as local
- Key install is a separate, explicitly-invoked verb that never runs as a side effect of finding or logging in. It appends to the robot's `authorized_keys` via ssh-copy-id semantics, never truncating or replacing existing keys, and reports plainly when the key was already present
- The /etc/hosts write is confined to a delimited managed block with begin/end markers, written atomically via a temp file plus os.replace, and is idempotent — re-running never duplicates a line and never disturbs any entry outside the block
- Staleness is actively defeated, not merely tolerated: every successful find refreshes the block to the freshly-verified IP. A pinned name that no longer answers as the right `hardware_id` is corrected or removed rather than left pointing at whatever now owns that address
- The hosts write NEVER happens implicitly. It is an explicitly-invoked verb (or explicit flag), it degrades to a clean actionable error when not privileged rather than a traceback or a silent no-op, and plain discovery without privileges keeps working in full
- Every number is recorded from a real run against the live unit at 192.168.1.162 with the seven Docker bridges present, not from a mocked fixture — and if a target proves unreachable on real hardware the number is revised in the open rather than the claim quietly dropped
- The IPv4 boundary is visible, not silent: the noun's overview states it, and a unit reachable only over IPv6 stays usable by explicit address rather than being reported as absent
- The noun's name never lies and never silently discards a capability. The same sweep also finds a Lite tethered to another box on the LAN — discoverable, and not wireless — so 'wireless' filters to `wireless_version`=true by DEFAULT while a documented flag reveals every Reachy daemon the sweep saw. The name describes the default, not a limit of the mechanism
- Stable host-key identity must NOT depend on privilege. ssh passes -o HostKeyAlias=<alias> so `known_hosts` keys on the alias whether or not /etc/hosts was ever pinned — otherwise the feature silently breaks h12's promise that unprivileged discovery keeps working in full, since the hosts pin needs sudo and the alias would exist only for privileged operators

## Success signals

- Cold start on an unfamiliar network: one command names the Wireless unit's IP in a few seconds. Warm start: the same command answers from the registry in well under a second. The co-resident Lite is NEVER reported as the Wireless unit
  - instruction: Cover with tests: a fake daemon responding `wireless_version`=true vs false, a loopback/bridge-local advertisement, and a /16 interface that must not be enumerated
- Measured on this box against the live unit: cold discovery on a /24 answers in under 5 s wall clock and is hard-bounded at 10 s; a warm resolve from the registry answers in under 500 ms; the co-resident Lite is misreported as the Wireless unit in 0 of 20 consecutive runs; and a sweep with the seven Docker /16 bridges present still finishes inside the same 10 s bound
  - instruction: Record the measurements under docs/evidence/ following the existing on-box verification pattern, and assert the 10 s hard bound in an offline test with a fake slow/blackholing host set

## Scope / boundaries

- No new base runtime dependency. Discovery is stdlib-only (socket/urllib/concurrent.futures); zeroconf/python-zeroconf stays OUT, keeping the three-base-dep rule intact. Any mDNS leg must be either hand-rolled stdlib or an optional system-binary accelerator that degrades silently
  - instruction: Keep pyproject dependencies at the three pinned entries. Add a test asserting every module under reachy/discover/ imports only stdlib, in the AST style of tests/`test_zero_llm_boundary.py`
- Does not replace `REACHY_BASE_URL` / --base-url. Discovery POPULATES that address for the operator; the existing transport contract is untouched
  - instruction: Do not touch reachy/robot/transport.py. Discovery emits an address; 'reachy unit find --json' gives an agent the `base_url` to pass onward
- Scope is IPv4 and the default daemon port. The unit also holds a global IPv6 address and a non-default port is possible, but v6 sweeping and port discovery are out of scope for this version; the port is overridable and a v6 unit remains reachable by explicit address (challenge pass / data-flow lens: unit's v6 addr 2a0d:6fc2:...:756b observed alongside its v4)
  - instruction: State the IPv4-and-default-port boundary in the noun's overview text and accept an explicit --base-url/address for a v6-only or non-default-port unit

## Non-goals

- Not wifi onboarding. Getting a factory-fresh unit ONTO a network (captive portal, SSID/PSK provisioning, BLE pairing) is a different, larger job; this feature assumes the unit is already on the LAN
- Not a general network scanner. The sweep is bounded to local /24-or-smaller IPv4 subnets and to the daemon port; it is not a port scanner and reports only hosts that answer as a Reachy daemon

## Assumptions

- The unit's human-readable name is NOT stable and NOT unique. Both robots report `robot_name`=`reachy_mini`; the wireless unit's advertised `reachy_mini`-2 / reachy-mini-2.local carries avahi's collision suffix, awarded because the local Lite claimed the base name first. That suffix can move if the Lite is absent at boot or the claim order flips — so no name may be used as an identity key, and the /etc/hosts alias must be operator-chosen rather than harvested from mDNS (challenge pass / unstated-assumptions lens: Lite TXT `robot_name`=`reachy_mini` vs wireless daemon status `robot_name`=`reachy_mini`)
- Discovery assumes a TRUSTED network. The daemon's status endpoint is unauthenticated, so on a shared LAN — a lab, an office, a conference — a sweep will find, and offer to remember, pin and log into, a Reachy that is not the operator's. Nothing in the protocol distinguishes 'my robot' from 'a robot' on first contact (challenge pass / security lens: GET /api/daemon/status returns full identity with no auth)
- The robot ships with the factory-default password 'root' for the pollen account, and this unit still has it. So c28's trusted-network assumption is stronger than 'the daemon is unauthenticated': anyone who can reach the unit on the LAN can also log into it with a published default credential. Discovery makes the robot easier to find, and therefore makes changing that password the operator's first move (operator-stated, 2026-08-08)

## Scope exploration

- `s1` — `challenge pass / observability+containment lens: tests/conftest.py`: Five autouse network guards exist and `REACHY_BASE_URL` is already pinned unreachable, but none constrains interface enumeration — a sweep bypasses all of them. Seeded the sixth-guard requirement
  - seeds: `c22`
- `s2` — `challenge pass / adjacent-systems lens: tailscale0 + reachy/service/units.py + reachy/explain/catalog.py`: Tailscale is live with five peers (orin direct via 192.168.1.138); its 100.x/32 interface must be excluded from enumeration, and its existence vindicates `hardware_id` over MAC since a Tailscale-reachable robot has no ARP entry. units.py owns UNIT for systemd - seeded q1. catalog.py ENTRIES needs a key per new verb, and nothing fails if it is forgotten
  - seeds: `c27`
- `s3` — `challenge pass / security lens: unauthenticated GET /api/daemon/status + /usr/bin/ssh-copy-id`: Full unit identity is served with no auth, so first contact cannot distinguish my robot from a robot; ssh-copy-id is present, so key push is buildable. Seeded the explicit-confirmation requirement and the trusted-network assumption
  - seeds: `c25`
- `s4` — `challenge pass / reversibility lens: /etc/hosts`: 56 bytes, root-owned, two lines, not managed by NetworkManager or systemd - so a managed block is safe to add, but losing the localhost line breaks the box. Seeded the backup/verify/rollback/unpin requirement
  - seeds: `c24`
- `s5` — `challenge pass / migration lens: reachy/discover (new package)`: Clean pass - this is a new surface with no prior on-disk format, no deployed state to migrate, and no existing consumer to break. Residual risk is only forward: units.json gains a schema that a later version must read

## Decisions

- Identity is keyed on the daemon-reported `hardware_id`, with MAC stored alongside as an opportunistic attribute. `hardware_id` arrives over plain HTTP so it works off-subnet and on boxes with no ARP table; MAC enriches the record when the unit is on the same L2 segment (user decision, 2026-08-08)
- The surface is a new top-level 'unit' noun — reachy unit find / list / ssh / forget / overview — matching the daemon's own `unit_id` vocabulary and keeping volatile network discovery out of the device status noun (user decision, 2026-08-08)
- Login includes an explicit one-time key-install verb wrapping ssh-copy-id, so subsequent logins are passwordless, in addition to the verb that opens the shell (user decision, 2026-08-08)
  - instruction: Add 'reachy wireless authorize': runs ssh-copy-id for the resolved unit after explicit confirmation, never invoked implicitly by find or ssh, and reports plainly when the key was already installed
- Discovery additionally maintains an /etc/hosts entry for the remembered unit, so any tool on the box — not just this CLI — resolves it by name (user decision, 2026-08-08; taken with the sudo and staleness costs stated and accepted)
  - instruction: Add reachy/discover/hosts.py: rewrite a block delimited by '# BEGIN reachy-mini-cli' / '# END reachy-mini-cli' via tempfile + os.replace, preserving every line outside it, with a pre-write backup and a post-write verification that localhost still resolves. Exposed as 'reachy wireless pin' / 'wireless unpin'; a non-writable /etc/hosts is a clean exit-2 CliError naming sudo
- The discovery noun is named 'wireless', superseding c18's 'unit'. It avoids reachy/service/units.py's systemd UNIT vocabulary and declares its scope in its own name (user decision, 2026-08-08, resolving q1)
  - instruction: Create reachy/cli/`_commands`/wireless.py exposing register(sub) with verbs find / list / ssh / authorize / pin / unpin / forget / overview, wire it in `_build_parser`(), and add an ENTRIES key per verb in reachy/explain/catalog.py. Keep 'teken cli doctor . --strict' green — a noun with action verbs must expose overview
- SSH host-key identity is pinned to a stable operator-chosen alias, not the unit's IP, so DHCP movement never triggers a host-key mismatch (user decision, 2026-08-08, resolving q2)
  - instruction: In the ssh verb, always pass -o HostKeyAlias=<alias> alongside the resolved IP; the alias defaults to a documented stable string and is overridable per unit in the registry
- The SSH account is 'pollen' — the documented Pollen default (`reachy_mini` docs quickstart.md:21 and troubleshooting.md:349 both use 'ssh pollen@reachy-mini'), confirmed by the operator. This resolves parked v1; --user and `REACHY_WIRELESS_SSH_USER` remain the overrides (user decision, 2026-08-08)
- The stable alias is 'reachy-mini' — the name Pollen's own documentation tells operators to use. Verified on this box: 'reachy-mini' and 'reachy-mini.local' BOTH fail to resolve while 'reachy-mini-2.local' resolves to 192.168.1.162, so ssh pollen@reachy-mini currently fails with a name-resolution error here. Pinning the alias is therefore what makes the documented command work, not a convenience — it also confirms c27, since the Lite holds the base mDNS name and the Wireless carries avahi's -2 suffix. This resolves parked v3 (user decision, 2026-08-08)
- The alias is spelled 'reachy-mini' with a HYPHEN, while the daemon's own `robot_name` field reports '`reachy_mini`' with an UNDERSCORE. These are different strings for different purposes and must never be conflated: the alias is operator-chosen and hyphenated (matching Pollen's docs and DNS convention), and it must NOT be derived from `robot_name` by string munging — doing so would regenerate the very name the Lite already claims in mDNS, which is the c27 collision (user decision, 2026-08-08)
- The managed block pins BOTH names on one line — '<ip> reachy-mini reachy-mini.local' — since the operator accepts either. The plain 'reachy-mini' is PRIMARY and the '.local' form is an additional convenience, because .local is the mDNS domain and some nsswitch configurations route it exclusively to mDNS, bypassing /etc/hosts entirely. Correctness must therefore never depend on the .local form resolving through files (user decision, 2026-08-08)

## Open parks

- [unknown_nonblocking] The SSH login account on the shipped Reachy Mini Wireless image is unconfirmed. A probe as reachy@ was refused with 'publickey,password', which proves sshd is live and accepts both methods but does not confirm the account exists — sshd never leaks that. Resolvable at build time by asking the operator or reading the Pollen image docs; de-risked meanwhile by h14 making the account overridable
- [unknown_nonblocking] Whether to build an mDNS accelerator at all. The robot does advertise `_reachy`-mini.`_tcp`, but resolving the wireless unit's TXT record timed out twice in a live session while the HTTP probe answered first time, and the box's two same-subnet interfaces make multicast unreliable here. Recommendation: ship the probe-and-sweep path alone, and treat mDNS as a later optimisation only if the sweep proves too slow in practice
- [unknown_nonblocking] Which hostname the /etc/hosts block should pin. The unit advertises itself as reachy-mini-2.local, but a stable local alias (e.g. reachy-mini) is friendlier to type and survives the unit being renamed
- [unknown_nonblocking] Whether discovery should ever refuse to act on a unit it has not seen before on a network it does not recognise. c28 records the trusted-network assumption, but no mechanism is specified for a first-contact trust decision; deferred rather than designed, since the operator's networks are home and lab today
