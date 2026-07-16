# Delivery Summary — harmonic voice

plan: `harmonic-voice` · run: `partial` · date: `2026-07-16`
baseline: `devague summary skeleton`

## Intent

Give Reachy Mini a second, non-TTS voice: the harmonics-cli package renders
each spoken sentence in-process to a note melody in Reachy's own identity
signature, played through the existing playback leg — selectable on
`say`/`think`/`listen --live`, with the boot presence unit defaulting to it —
so a live conversation hears words, thinks, emotes, and answers in melody.
Executed as the converged 10-task / 6-wave plan
(`docs/plans/2026-07-16-harmonic-voice.md`) via /assign-to-workforce: 8
worktree agents (sonnet) over 4 waves, TDD-gated merges, plus two operator
waves.

## Planned Work

Quoted verbatim from the `devague summary` skeleton (truncated to the task
heads; full text in the plan artifact):

- `t1` — Harmonic voice backend: new reachy/speech/harmonic.py (synthesize(text) -> PCM16 @ HARMONIC_SAMPLE_RATE=16000 via harmonics render_notes + render_wav …) + new reachy/speech/voice.py resolver … New test files tests/test_harmonic.py + tests/test_voice.py only
- `t2` — Base dependency: pyproject.toml dependencies gains harmonics-cli>=0.8 (alongside numpy) … uv.lock regenerated via uv lock. Touches pyproject.toml + uv.lock only
- `t3` — say wiring: --voice-engine {tts,harmonic} on say run … Touches reachy/cli/_commands/say.py + NEW test file tests/test_say_voice_engine.py only
- `t4` — think wiring: --voice-engine on think run AND think demo; think status --json gains voice_engine field; startup banner names the active engine; playback kwargs carry the harmonic samplerate … Touches reachy/cli/_commands/think.py + NEW test file tests/test_think_voice_engine.py only
- `t5` — listen --live wiring: --voice-engine on listen run (honoured only with --live …) … Touches reachy/cli/_commands/listen.py + NEW test file tests/test_listen_voice_engine.py only
- `t6` — Live boot unit goes harmonic: exec_start_live() in reachy/service/units.py appends --voice-engine harmonic … unit-renderer test asserts the flag
- `t7` — Explain catalog: reachy/explain/catalog.py ENTRIES updated for say run / think run / think demo / listen run …
- `t8` — Docs: README + docs/operating-reachy.md … + CLAUDE.md (hard-constraints base-dep text …)
- `t9` — CI verification + boundary audit (operator task at merge time): full suite + coverage gate + teken rubric + lint stack green; git diff vs origin/main shows ZERO changes to the protected files …
- `t10` — On-robot verification (operator …): stop reachy-live user unit; say run --voice-engine harmonic plays an audible motif; think demo --voice-engine harmonic plays gestures; short live conversation …; record render timing; re-run service enable live …; restore/verify unit active

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `reachy/speech/harmonic.py` + `reachy/speech/voice.py` + 23 tests; commit `e7cd9a6` |
| `t2` | delivered | `harmonics-cli>=0.8` base dep + `uv.lock`; three guard tests updated per approved deviation `d1`; commit `b4316b7` |
| `t3` | delivered | `say run --voice-engine`, tts-only flags documented/ignored, 12 tests; commit `e53b1b8` |
| `t4` | delivered | `think run`/`demo` engine wiring, `status --json` `voice_engine` (sidecar file), banner, mute-window fix, 23 tests; commit `caccea8` |
| `t5` | delivered | `listen run --live --voice-engine` (exit-1 without `--live`), folded-engine + self-mute samplerate threading, banner, 16 tests; commit `cbcc114` |
| `t6` | delivered | live unit ExecStart gains `--voice-engine harmonic`; renderer test updated; commit `d27861f` |
| `t7` | delivered | say/think/listen catalog entries; service entry closed by operator polish `ef2ff06`; commit `02efdeb` |
| `t8` | delivered | README, operating guide ("The harmonic voice" + boot revert gotcha: baked flag beats env), CLAUDE.md two-base-dep constraint; commit `e026b20` |
| `t9` | delivered | suite 1742 passed, coverage 91.97 % (gate 60), teken strict PASS, lint stack clean, protected-file diff audit CLEAN; v0.31.0 bump `8e7426a`; PR `#63` opened |
| `t10` | partial | On-robot: harmonic `say` audible (97 424 PCM bytes played), `think demo` 3 gestures + 3 harmonic phrases, boot unit re-enabled and active with harmonic ExecStart (journal banner `(voice: harmonic)` at 02:30:52). **Missing: the live-conversation check — requires the user to address the robot by voice.** |

## Mid-work Decisions

- `d1` — extend t2's file scope by three guard-test updates (test_export_decoupling.py, test_sleep_boundary.py, test_listen_live.py) to expect exactly `['numpy>=1.24', 'harmonics-cli>=0.8']` — three prior PRs baked in numpy-only base-dep guards; the challenge-gate base-dep decision trips all three and no plan task covered them (recorded via /deviate, approved).
- t4 forwards an explicit `--voice-engine` from `think start`/`restart` to the spawned run via the inherited environment (`REACHY_VOICE_ENGINE`, restored after spawn) instead of extending `supervisor.build_run_command` — the plan's file scope forbids touching `supervisor.py`. No deviation record: below plan granularity (acceptance criteria met as written).
- t4 discovered standalone `think`'s `_guarded_play` had **no** clip-duration term at all (the plan assumed one existed to fix); it added the duration term uniformly for both engines.
- t7 deliberately skipped the `service` noun catalog entry (its worktree predated t6's merge; documenting an unmerged boot default would have been false) — closed post-merge by operator commit `ef2ff06`.
- Deploy reality differed from the plan's assumption: the box's systemd units run from a **uv tool env** (`~/.local/share/uv/tools/reachy-mini-cli/`), and a machine-local `panel.conf` drop-in overrides ExecStart to pipe `--export -` into the reTerminal bridge. The tool env was refreshed to 0.31.0 from the local checkout (`[daemon]` extra preserved) and the drop-in updated to `listen run --live --transcribe --voice-engine harmonic --export -` (backup kept: `panel.conf.bak-20260717`). Machine config, not repo state.
- During t10 the robot speaker was unreachable: USB enumeration had shifted the Reachy audio device to card 2 while `~/.asoundrc` hard-coded card 1. Fixed by referencing the card by name (`hw:Audio,0`; backup `~/.asoundrc.bak-20260717`). Machine config, not repo state.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t2` (`d1`) | three prior PRs baked in numpy-only base-dep guard tests; the user's challenge-gate decision to make harmonics-cli a base dep trips all three and no plan task covered updating them | acceptable |
| `t10` | the live-conversation acceptance check inherently requires the user to speak to the robot; all other t10 checks completed. The deployed unit's effective ExecStart lives in a local drop-in (panel bridge), updated to carry the planned flags | needs-follow-up |

## Evidence

- tests: `uv run pytest -n auto -q` — **1742 passed** (baseline 1668 + 74 new); coverage 91.97 % (`--cov=reachy`, gate 60 %)
- lint: black / isort / flake8 / bandit / markdownlint — clean; `uv run teken cli doctor . --strict` — PASS
- boundary audit: `git diff main...HEAD --name-only` contains none of: `reachy/speech/playback.py`, `reachy/motion/listen.py`, `reachy/motion/listen_transcribe.py`, `reachy/speech/engagement.py`, `reachy/speech/name_match.py`, `reachy/speech/markers.py`, `reachy/speech/expressions.*`, `reachy/motion/expression.py`, `reachy/export/`, `docs/export-schema.md`
- commits: `8e0da70..8e7426a` (spec, plan, 8 task commits, 8 merge commits, operator polish, version bump)
- PR: `#63` — <https://github.com/agentculture/reachy-mini-cli/pull/63>
- on-robot: `say run --voice-engine harmonic` played 97 424 PCM bytes (16 kHz ≈ 3.0 s audio, 1.09 s wall); `think demo --voice-engine harmonic` — "expressed 3 gesture(s), spoke 3 phrase(s)" in 5.0 s; `journalctl --user -u reachy-live.service` 2026-07-17 02:30:52: banner `… (voice: harmonic) [export: stdout]`; `systemctl --user is-active reachy-live.service` → active
- render timing (probe, this box = the robot's compute): 17-word sentence → 17 notes instantly; `render_wav` 0.085 s @ 16 kHz smooth (0.237 s @ 44.1 kHz)

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The harmonic backend renders deterministic, offline PCM16 @ 16 kHz from raw text | high | file `reachy/speech/harmonic.py` · tests `tests/test_harmonic.py` (15) · commit `e7cd9a6` |
| `--voice-engine {tts,harmonic}` + `REACHY_VOICE_ENGINE` work on say / think run / think demo / listen --live, defaults byte-identical | high | tests `tests/test_{say,think,listen}_voice_engine.py` (51) · commits `e53b1b8`, `caccea8`, `cbcc114` |
| harmonics-cli>=0.8 is a base dependency and the suite stays green | high | commit `b4316b7` · `uv.lock` · 1742 passed |
| The robot audibly speaks in melody and emotes in parallel, with no LLM or TTS service | high | on-robot `say` + `think demo` runs (Evidence above) |
| The boot presence unit boots the harmonic-voiced conversation loop | high | unit `ExecStart` + drop-in text · journal banner `(voice: harmonic)` · unit active |
| A live conversation gets harmonic answers to addressed speech | unverified | robot is live and listening; awaiting the user to address it — not claimed done |
| Per-sentence render cost is well under the 1 s budget on the robot's compute | high | probe timings above (0.085 s / 17 words @ 16 kHz) |

## Remaining Work / Follow-up

- `t10` tail — the user talks to the robot (it is live now: address it as "Reachy"); confirm harmonic response + emote, then this claim flips to verified.
- PR `#63` — human review + merge (gate 3). A background watcher polls CI/Sonar readiness; GitHub's authenticated API was intermittently returning 503s ("Unicorn" page) during the run, and the `gh` keyring token reports invalid (`gh auth login -h github.com` will re-auth if it persists).
- reTerminal panel at 192.168.1.173 unreachable (no route) — the export bridge retries harmlessly; power the panel to resume the e-paper feed.
- Parked (frame/plan, non-blocking): mapping cognition's own state onto harmonics axes directly (v1); articulation/pre-chunk fallback if long sentences ever stretch the render burst (v3); revisit the harmonics-cli `<1.0` cap when its 1.0 approaches — `render_notes` lives under a `_commands` module (v4 / plan risk r1).
