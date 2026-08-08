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

## Limitation — the live `/etc/hosts` pin was NOT executed

`/etc/hosts` is `root:root 0644` and `sudo -n` returns
`sudo: a password is required`. No password was available to this session, so
**the real pin never ran**, and consequently:

- `ssh pollen@reachy-mini` has **not** been shown working on this box. The
  before-state above still stands.
- `c1`/`h17`'s end-to-end claim is verified for every step *except* the pin and
  the resulting name resolution.

This is recorded as unexecuted rather than rounded up. To close it:

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
