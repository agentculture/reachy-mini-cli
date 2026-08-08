# Live verification — `wireless` discovery (t12)

Task **t12** of `docs/plans/2026-08-08-reachy-mini-cli-finds-your-reachy-mini-wireless-on.md`.
Run on the deployed dev box (`spark-f8a9`) against the live Reachy Mini Wireless,
2026-08-08. Every number below is measured; nothing here is estimated.

## The two units on this box

| | `robot_name` | `wireless_version` | `hardware_id` | address |
|---|---|---|---|---|
| Wireless | `reachy_mini` | `true` | `a89063c05ae79779` | `192.168.1.162` |
| Lite (co-resident) | `reachy_mini` | `false` | `37a38ce3a26e0727` | `127.0.0.1` |

Both report the **same** `robot_name`. This is the ambiguity the noun was
specified against, and it is the normal state of this box, not a contrived case.

## Before — the gap, stated as a measurement

```text
reachy-mini            -> (does not resolve)
reachy-mini.local      -> (does not resolve)
reachy-mini-2.local    -> 192.168.1.162

$ ssh pollen@reachy-mini
ssh: Could not resolve hostname reachy-mini: Temporary failure in name resolution

/etc/hosts: 56 bytes, no managed block
registry:   does not exist
```

Pollen's own documented command fails here. The `-2` suffix is avahi's collision
suffix: the Lite claimed the base mDNS name first.

## Cold discovery — c21, c16

Twenty consecutive `wireless find --json` runs, registry cleared first. Raw data:
`2026-08-08-wireless-discovery-20runs.csv`.

```text
runs: 20   misreported: 0
elapsed min/mean/max: 3.749 / 3.861 / 3.975 s
target < 5 s: PASS (hard bound 10 s never approached; deadline_reached=false)
hosts_total = hosts_probed = 254
```

**254 hosts, not ~459 000.** The box carries seven Docker `172.x/16` bridges, a
`tailscale0 /32`, loopback, and two NICs on the *same* `192.168.1.0/24`. All are
rejected before a host is materialised, and the duplicated `/24` is enumerated
once.

Note the honest number: the first single sample was `3.395 s`; across 20 runs the
mean is **3.861 s**. The 20-run distribution is the claim, not the flattering
sample. Margin to the 5 s target is about one second.

## Warm resolve — c8, c21

```text
$ time reachy wireless list
0.225 s wall clock
mac enriched: 88:a2:9e:8c:fa:bf
```

Under the 500 ms target. No sweep is invoked on this path.

## The wireless filter — h26

Default refuses a non-wireless daemon and says why:

```text
$ reachy wireless find --address 127.0.0.1
error: 1 Reachy daemon(s) answered, but none reports wireless_version=true
hint: re-run with --all to list every Reachy daemon found — a Lite tethered to
      another box on the LAN is discoverable and is not wireless:
      reachy_mini (37a38ce3a26e0727) at 127.0.0.1 [Reachy Mini Lite]
```

`--all` then finds it. The noun's name describes the default, not a limit.

**Be precise about the Lite.** The sweep excludes loopback, so the Lite is *not
discoverable by sweeping at all* — it is reachable only via
`--address 127.0.0.1`. The 0-of-20 result above therefore holds partly by
exclusion and partly by the filter. The sweep does not "distinguish" the two.

## Ambiguity refusal — c23, h28

Running `--all` remembered the Lite, so the registry genuinely held two units:

```text
$ reachy wireless ssh --dry-run
error: more than one known unit matches; refusing to pick one
hint: pass --unit <hardware_id-or-alias> to choose:
      reachy_mini (a89063c05ae79779) at 192.168.1.162 [Reachy Mini Wireless],
      reachy_mini (37a38ce3a26e0727) at 127.0.0.1 [Reachy Mini Lite]
```

With the selector:

```text
$ reachy wireless ssh --unit a89063c05ae79779 --dry-run
ssh -o HostKeyAlias=reachy-mini pollen@192.168.1.162
```

Account `pollen` and alias `reachy-mini` (hyphen) are both correct, and the alias
is not derived from the daemon's underscore `robot_name`.

## `/etc/hosts` pin — c20, c24, h10, h11, h29 (fixture only; see limitation)

Against a fixture reproducing this box's exact hosts file, tabs and the trailing
double-space included:

```text
# BEGIN reachy-mini-cli
# Managed by reachy-mini-cli - 'reachy wireless pin' / 'reachy wireless unpin'.
192.168.1.162 reachy-mini reachy-mini.local
# END reachy-mini-cli
```

- Second pin: `changed: false`, still 6 lines — idempotent, no duplicate.
- `unpin`: result **byte-identical** to the original, verified by `diff`.

**Correction — this check had a blind spot, found in review, not by these
tests.** The fixture above ends in a newline, as this box's real `/etc/hosts`
does. A Qodo review of PR #161 found that `pin` appended a separator newline to
a file **not** ending in one, and `unpin` removed only the block — leaving that
byte behind. So the byte-identical guarantee held for the file measured here and
was false in general, and all 53 hosts tests at the time shared the blind spot.
Fixed by making the managed block carry the document's own final-newline
property, with a parametrized round-trip property now covering ten body shapes
(no trailing newline, CRLF without a final CRLF, bare-CR terminators,
whitespace-only, trailing blank lines, …), all asserted on `read_bytes()`. The
claim above is now true as stated for the round trip.

**Arbitrary-write refusal (measured 2026-08-08).** `pin --hosts-path` pointed at
a shadow-format file and at an `authorized_keys` was refused in both cases with
exit 2, `does not parse as a hosts file, or does not resolve 'localhost', before
any change — refusing to modify it`. Both files were byte-unchanged and **no
backup was written** — the refusal precedes every write. This is what bounds
`--hosts-path` under `sudo`.

## The end-to-end claim, verified — c1 / h17

The pin was executed for real, against the real path `/etc/hosts`, with the real
robot on the network. It ran inside a throwaway container sharing the **host's
network namespace** (so `192.168.1.162` is genuinely reachable) but holding its
**own** `/etc/hosts` — a regular file at the real path. The code, the path and
glibc's resolver are all real; only the mount namespace differs. The box's own
`/etc/hosts` was never written (re-verified after: still 56 bytes, no `.bak`,
`reachy-mini` still does not resolve there).

Before, and after, the same command:

```text
$ ssh pollen@reachy-mini true          # BEFORE the pin
ssh: Could not resolve hostname reachy-mini: Temporary failure in name resolution

$ reachy wireless pin --address 192.168.1.162
changed: True   pinned_address: 192.168.1.162
re-pin -> changed: False               # idempotent

$ getent hosts reachy-mini
192.168.1.162   reachy-mini reachy-mini.local

$ ssh -o HostKeyAlias=reachy-mini pollen@reachy-mini true    # AFTER
Warning: Permanently added 'reachy-mini' (ED25519) to the list of known hosts.
pollen@reachy-mini: Permission denied (publickey,password).
```

That argv is exactly what the CLI builds (`ssh.ssh_argv("reachy-mini")` →
`ssh -o HostKeyAlias=reachy-mini pollen@reachy-mini`).

**What this proves:** the name resolves, and it resolves to *this* robot — the
daemon answered over the pinned name, `http://reachy-mini:8000/api/daemon/status`
returning `robot_name=reachy_mini wireless=True hardware_id=a89063c05ae79779`.
SSH resolved the name, completed a key exchange with the robot's real sshd
(ED25519 host key), and was refused at **authentication**, which is the expected
outcome with no key installed and `BatchMode=yes` forbidding a password prompt.

**What it does not prove:** an interactive shell. That needs either the factory
password typed at a prompt or `wireless authorize` to install a key — both
recorded below as unexercised.

Then `unpin` returned the file **byte-identical** to its pre-pin bytes (`cmp`).

### A new finding from this run

On a **bind-mounted** `/etc/hosts` — which is what every Docker container has by
default — `os.replace` fails with `EBUSY: Device or resource busy`, and the tool
raises a clean `CliError` naming the failure rather than corrupting the file or
falling back to a non-atomic write. Worth knowing before anyone runs `wireless
pin` inside a container.

## Limitation — the pin was NOT executed against this box's own `/etc/hosts`

`/etc/hosts` here is `root:root 0644` and `sudo -n` returns
`sudo: a password is required`. No password was available to this session, so
the pin was verified in the namespace described above rather than on the box's
own file. What that leaves genuinely open is narrow and worth stating exactly:

- The box's own `/etc/hosts` is unmodified, so `ssh pollen@reachy-mini` still
  fails **on the host shell** with a resolution error. The operator gets the
  documented command working by running the pin once.
- Not covered by the namespace run: that this box's `/etc/hosts` is writable by
  root in the ordinary way (it is a plain regular file, not a bind mount — the
  EBUSY case above does not apply here), and whatever `sudo` does to `HOME` and
  therefore to registry resolution, which is why `--address` is the form
  documented.

Everything about the code path — the write, the verification, the resolution,
the reachability of the robot by the pinned name, and the byte-identical
`unpin` — is verified above. To close the remaining step on this box:

```bash
sudo reachy-mini-cli wireless pin --address 192.168.1.162
ssh pollen@reachy-mini
```

`pin --address` is the deterministic form: it skips unit resolution, so it
neither reads nor writes a registry and cannot be confused by `sudo` resolving a
different `HOME`.

## Not exercised live

- `wireless authorize` (the `ssh-copy-id` path). It would push a key to the
  robot using the factory-default password; not run unattended. Covered offline
  by 44 tests including the confirmation gate and the structural
  unreachability-from-`ssh` assertion.
- Any unit reachable only over IPv6, and any non-default daemon port.

## Suite state at time of writing

```text
5307 passed, 7 skipped        (7 pre-existing: missing cv2, unreachable LLM gateway)
teken cli doctor . --strict   exit 0
markdownlint-cli2             0 errors
version                       0.48.0
```
