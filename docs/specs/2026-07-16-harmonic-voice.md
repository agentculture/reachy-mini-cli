# harmonic voice

> Reachy Mini now speaks with a harmonic voice: alongside its Chatterbox TTS speech engine, a new in-process harmonic voice backend (the harmonics-cli PyPI package, import package 'harmonics') renders each spoken sentence as a pleasant non-speech sonic gesture — a word-tracking melodic contour in Reachy's own identity signature — played through the robot speaker via the existing playback leg. Selectable on say, think, and listen --live via a voice-engine flag/env; cognition, emoting (pose expressions), hearing, and the export feed are untouched, so in a live conversation the robot hears words, thinks, emotes, and speaks harmonically in parallel.
> instruction: Verify on-robot: stop the reachy-live user unit first (single SDK owner), run say run --voice-engine harmonic for an audible motif, then hold a short conversation via listen run --live --transcribe --voice-engine harmonic; restore the unit after. In CI: full suite + rubric gate green offline.

## Audience

- Ori and anyone operating a Reachy Mini through reachy-mini-cli (say/think/listen operators); secondarily the mesh/panel consumers of the export feed, which is unchanged
  - instruction: Verify: fresh venv, pip install 'reachy-mini-cli[voice,daemon]' suffices to run the harmonic voice end-to-end.

## Before → After

- Before: The robot's only voice is Chatterbox TTS over HTTP (model-gear, REACHY_TTS_URL); when that service is wedged the robot is effectively mute (audio_optional just degrades to silence), and it has no non-speech sonic identity at all
  - instruction: Context only — no action; cites PR #53 audio_optional behaviour.
- After: A live conversation where the robot hears words (--transcribe), thinks (cognition), emotes (pose expressions), and voices every spoken sentence as a harmonic gesture in its own 'reachy' identity signature: listen run --live --transcribe --voice-engine harmonic; say and think support the same engine switch
  - instruction: On-robot: run the exact live command; confirm harmonic responses to addressed speech.

## Why it matters

- A recognizable non-speech voice makes the robot present and legible by ear — fully offline (no TTS service dependency), deterministic, pleasant for ambient presence — and gives Reachy an identity motif distinct from any TTS voice
  - instruction: Test: harmonic synthesize path performs no urlopen (stub/forbid network in test).

## Requirements

- New in-process voice backend reachy/speech/harmonic.py: text -> PCM16 via the harmonics import package (harmonics-cli dist) — render_notes composition (parse_emphasis -> infer_axes -> identity signature -> text_contour -> axis-shading -> stress -> variation) + harmonics.audio.render_wav; exposed as a synthesize-shaped callable (text in, PCM16 out) so it plugs into CognitionEngine's injectable synthesize= seam (reachy/speech/cognition.py:229) and say's _synthesize alias without engine changes
  - instruction: Create reachy/speech/harmonic.py: synthesize(text, *, identity, articulation) -> PCM16 bytes at a module HARMONIC_SAMPLE_RATE; lazy-import harmonics (missing -> CliError exit-2 naming [voice]); compose render_notes + harmonics.audio.render_wav; strip the WAV container to raw PCM via stdlib wave. Unit-test determinism, non-empty output, and the missing-dep error offline.
  - honesty: reachy.speech.harmonic exposes a synthesize-compatible callable: given plain text it returns non-empty PCM16 bytes fully offline (no audio device, network, or robot) and plugs into CognitionEngine(synthesize=...) with zero engine changes
- Voice-engine selection: --voice-engine {tts,harmonic} on say run, think run, and listen run --live, plus REACHY_VOICE_ENGINE env default; default stays tts so bare commands are byte-identical to today
  - instruction: Add --voice-engine {tts,harmonic} (default = REACHY_VOICE_ENGINE env, else tts) to say run, think run, and listen run (honoured only with --live); select the synthesize callable + playback samplerate at the three composition sites (_commands/say.py, _commands/think.py engine build, _commands/listen.py folded-engine kwargs). Test flag threading, env fallback, and that defaults stay byte-identical.
  - honesty: Bare say run / think run / listen run --live stay behaviourally identical to today (default tts, existing tests untouched); --voice-engine harmonic and REACHY_VOICE_ENGINE=harmonic thread the harmonic callable into all three composition sites
- Harmonic clips play through the existing reachy.speech.playback.play_audio unchanged, passing the render sample rate — play_audio (playback.py:304) already accepts arbitrary-samplerate PCM16 and resamples on the sdk path
  - instruction: When harmonic is selected, set playback kwargs samplerate=HARMONIC_SAMPLE_RATE; make zero edits to reachy/speech/playback.py; test that playback kwargs carry the harmonic rate.
  - honesty: The harmonic leg passes its render sample rate to play_audio and reaches the speaker via the sdk resample path with zero diffs in reachy/speech/playback.py
- Reachy gets its own recognizable voice identity: harmonics.identity signature derived from 'reachy' (motif/key/instrument per agent), articulation default 'smooth' (harmonics say's own default), both overridable via env
  - instruction: Default identity 'reachy' via harmonics.identity; default articulation 'smooth'; env overrides REACHY_HARMONIC_IDENTITY / REACHY_HARMONIC_ARTICULATION. Test that the reachy signature differs from harmonics' default identity and overrides are honoured.
  - honesty: The same sentence renders byte-identical audio across runs, and the 'reachy' identity signature differs audibly (different root/instrument) from harmonics' built-in default identity
- Self-mute keeps holding: harmonic clips flow through the existing guarded play wrappers (think.py _guarded_play ~line 500; listen.py _make_self_mute_play_audio ~line 697) so mute[until] is stamped from clip duration and the robot never transcribes its own harmonic voice
  - instruction: No new mute code — add a regression test proving the existing wrappers stamp mute[until] from len(pcm)/2/samplerate using the harmonic samplerate for a harmonic-length clip.
  - honesty: A harmonic clip played under --live --transcribe stamps mute[until] for the clip's duration + margin through the existing wrapper (duration computed from PCM length / sample rate), verified by a test
- Explain catalog ENTRIES, README, docs/operating-reachy.md, and CLAUDE.md noun rows document the new flag/engine; version bump + CHANGELOG per the every-PR rule
  - instruction: Update reachy/explain/catalog.py ENTRIES for the changed verbs; README + docs/operating-reachy.md (say/think/live + service sections); CLAUDE.md noun rows; version-bump skill (minor) + CHANGELOG entry.
  - honesty: reachy explain resolves docs for the new surface; README + operating guide + CLAUDE.md rows updated; markdownlint and teken rubric gates green
- Offline tests, no audio device or robot: harmonics' core is pure-stdlib and deterministic (same input -> byte-identical WAV), so tests assert on PCM/notes output of the backend plus flag-threading composition tests in say/think/listen
  - instruction: tests/test_harmonic.py (determinism, offline no-net assert, missing-dep error) + per-noun composition tests + live-unit renderer test; full suite via uv run pytest -n auto stays green with no audio device.
  - honesty: New tests assert on deterministic PCM/notes output and on flag threading in say/think/listen composition, and run green in CI with no audio device
- User decision: the boot presence goes harmonic — reachy/service/units.py's live-unit ExecStart (currently '<python> -m reachy listen run --live --transcribe') gains --voice-engine harmonic, so 'reachy service enable live' boots the robot into the harmonic-voiced conversation loop; unit renderer tests and the operating guide follow
  - instruction: Edit exec_start_live() in reachy/service/units.py to append --voice-engine harmonic; assert the flag in the unit-renderer test; document in docs/operating-reachy.md service section.
  - honesty: The rendered live unit text contains 'listen run --live --transcribe --voice-engine harmonic' (unit renderer test asserts it), and service enable live deploys a unit that runs the harmonic loop
- think demo honours --voice-engine too (it drives _synthesize directly at think.py:666) — giving a no-LLM on-robot harmonic verification path (think demo --voice-engine harmonic), mirroring how think demo verified the expression path in v0.14
  - instruction: Thread --voice-engine through think demo's synthesize call (think.py:666 uses the same _tts_kwargs/_synthesize path); on-robot check: think demo --voice-engine harmonic plays harmonic gestures for the scripted stream.
  - honesty: think demo --voice-engine harmonic drives the scripted marker stream through the harmonic leg on-robot with no LLM
- say run's TTS-only flags (--voice, --speed, --tts-url) are documented as tts-engine-only and ignored under --voice-engine harmonic; help text says so — no hard error, matching how --speed is already a documented no-op
  - honesty: say run --voice-engine harmonic --voice x --speed 2 runs the harmonic leg, ignores both flags, and the help text marks them tts-only
- Observability: the live/think startup banner (stderr) and think status --json report the active voice engine, so an operator can tell which voice a running loop uses without reading unit files
  - honesty: The live/think startup banner names the active engine; think status --json carries a voice_engine field
- User decision (supersedes the [voice]-extra choice in q1): harmonics-cli>=0.8 is a BASE runtime dependency alongside numpy — it is the user's own code, AI-free, pure-stdlib with zero transitive deps, so it installs everywhere including the bare remote profile. pyproject dependencies gains harmonics-cli>=0.8; uv.lock regenerated in the same PR; CLAUDE.md's 'numpy is the only base dependency' hard-constraint text and pyproject's comment are updated to name the second base dep and why it is allowed (pure wheel, no system libs). A defensive lazy import keeps a clean exit-2 CliError for genuinely broken envs; no degrade-to-tts path is needed since the dep is guaranteed present
  - instruction: pyproject: dependencies = ['numpy>=1.24', 'harmonics-cli>=0.8']; run uv lock; update CLAUDE.md hard-constraints section + pyproject comment; remediation text for the broken-env CliError says reinstall reachy-mini-cli
  - honesty: pip install reachy-mini-cli in a fresh venv makes --voice-engine harmonic work with no extra; uv.lock diff shows harmonics-cli; CI green on bare uv sync

## Honesty conditions

- The announced conversation works on the physical robot: it hears addressed words, thinks, emotes, and voices sentences harmonically — while every default (tts) path stays byte-identical
- The feature branch diff contains zero modifications to reachy/speech/playback.py
- Zero diffs in ListenProducer tiers, TranscribeHook/STT logic, engagement.py, or name_match.py — only composition threading in _commands/listen.py
- Zero diffs in markers.py, expressions.toml, expressions.py, or motion/expression.py
- Zero diffs in reachy/export/ or docs/export-schema.md; existing export tests unchanged and green
- The existing say import-boundary test passes unchanged with reachy/speech/harmonic.py in the import graph
- harmonics-cli 0.8.0's render_notes imports from harmonics.cli._commands.say and renders ('hello, tests are green', agent='reachy') to a non-empty NoteEvent sequence in-process — verified against the installed wheel before build
- The whole feature is operable from the reachy CLI alone with the [voice] extra installed — no new external service, model, or daemon route
- The exact command 'listen run --live --transcribe --voice-engine harmonic' starts the folded loop and voices sentences harmonically on-robot (the boot unit runs the same line)
- Today, with REACHY_TTS_URL unreachable, live speech degrades to silence (audio_optional, PR #53) — the robot has no voice without the external TTS service
- The harmonic synthesize path makes zero network calls — asserted by a test that stubs/forbids urlopen in the harmonic leg
- Every listed signal maps to a named test or a scripted on-robot check recorded in the delivery summary
- A timed test (or on-robot measurement in the delivery summary) shows a 15-word sentence rendering under 1 s; coverage report stays >= 60%

## Success signals

- On-robot: say run --voice-engine harmonic plays an audible motif through the speaker; a live conversation (listen run --live --transcribe --voice-engine harmonic) answers addressed speech with harmonic gestures + emotes. In CI: full suite green offline, rubric gate green, bare commands unchanged (default tts)
  - instruction: CI: run-tests skill (uv run pytest -n auto) + teken rubric. On-robot: scripted check — stop reachy-live unit, say run --voice-engine harmonic (audible motif), live conversation smoke, re-enable unit.
- Measurable: rendering a 15-word sentence to PCM completes in under 1 second on the robot box (pure-stdlib synth budget), and the CI coverage gate stays at or above the existing 60% fail_under
  - instruction: Add a timed render test (soft assert/report) and record the on-robot render time in the delivery summary; check coverage >= 60% in CI output.

## Scope / boundaries

- reachy/speech/playback.py is not modified — it already takes arbitrary-samplerate PCM16 (samplerate param, sdk-path resample); the harmonic leg is a producer, not a playback change
  - instruction: Reviewer check: playback.py absent from the PR diff.
- The hearing side is untouched: listen's ListenProducer tiers, TranscribeHook/STT, and the layered engagement gate (engagement.py, name_match.py) do not change
  - instruction: Reviewer check: no diffs in motion/listen.py tiers, listen_transcribe.py, speech/engagement.py, speech/name_match.py.
- The emote path is untouched: markers.py MarkerParser, expressions.toml catalog, and motion/expression.py already run expressions in parallel with speech off the marker stream — harmonic speech rides the same ('speak', text) channel TTS does
  - instruction: Reviewer check: no diffs in speech/markers.py, speech/expressions.toml, speech/expressions.py, motion/expression.py.
- The export feed schema (docs/export-schema.md, reachy/export/) is unchanged — message blocks carry the sentence text regardless of which engine voices it
  - instruction: Reviewer check: no diffs under reachy/export/ or docs/export-schema.md.
- say stays a dumb pipe: reachy/speech/harmonic.py imports no reachy.speech.llm / reachy.speech.events, so the tested say import boundary holds
  - instruction: Existing say boundary test must pass unchanged; harmonic.py imports only harmonics + stdlib (+ reachy errors).

## Non-goals

- No vendoring or forking of harmonics-cli — it is consumed as a PyPI dependency (its offline text->notes core is the packaged product); the dep is harmonics-cli, never the unrelated 'harmonics' dist that squats the same import name on PyPI
- No simultaneous TTS+harmonic mixing: 'in parallel' means emotes run alongside harmonic speech (as they already do for TTS); the selected engine replaces the other for the session
- No LLM or network call in the harmonic path: axes inference is harmonics' static rule table, rendering is pure stdlib — the harmonic voice works fully offline, unlike the Chatterbox TTS HTTP leg

## Assumptions

- Importing harmonics' render_notes from harmonics.cli._commands.say is acceptable in-process (it is the shared composition its own demo reuses); if a future harmonics release moves it, the 7-step composition can be mirrored from public modules (parse_emphasis, infer_axes, signature_for, text_contour, apply_stress, apply_variation are all public) — pin harmonics-cli>=0.8

## Scope exploration

- `s1` — `harmonics-cli 0.8.0 wheel (PyPI, import package 'harmonics')`: Pure-stdlib deterministic text->notes->WAV core: render_notes (in harmonics/cli/_commands/say.py, reused by its demo) composes parse_emphasis -> infer_axes -> signature_for/derive_signature -> text_contour -> axis-shading -> apply_stress -> apply_variation; harmonics.audio.render_wav(seq, sample_rate=44100, articulation in {discrete,speechy,smooth,alien}) yields mono PCM16 WAV bytes, byte-identical for same inputs; zero base deps (sounddevice/numpy only behind its [audio] extra, which we do not need)
  - seeds: `c2`, `c5`, `c9`, `c18`
- `s2` — `PyPI dist 'harmonics' 0.0.0 (matias-ceau, music analysis)`: An unrelated package squats the bare name with heavy deps (scipy/pandas/seaborn); the dependency must be harmonics-cli, never 'harmonics'
  - seeds: `c15`
- `s3` — `reachy/speech/cognition.py`: CognitionEngine.__init__ (line 224) already takes injectable synthesize= and play_audio= callables plus tts_kwargs/playback_kwargs — the harmonic backend is a drop-in synthesize callable; no engine change needed; audio_optional semantics apply to it identically
  - seeds: `c2`, `c3`
- `s4` — `reachy/speech/playback.py`: play_audio (line 304) takes raw PCM16 mono at any samplerate and resamples to the speaker rate on the sdk path (uploads WAV header on http) — the harmonic leg reuses it untouched
  - seeds: `c4`, `c10`
- `s5` — `reachy/cli/_commands/say.py`: --voice already means the TTS voice identifier (forwarded to tts.synthesize), so the engine selector needs a distinct flag; say aliases _synthesize/_play_audio at module top for test monkeypatching; the no-llm/no-events import boundary is test-asserted
  - seeds: `c3`, `c14`
- `s6` — `reachy/cli/_commands/think.py`: Engine composition at lines 522-532 passes synthesize=_synthesize into CognitionEngine; _guarded_play (lines ~500-510) stamps mute[until] after each clip — a harmonic synthesize slots into the same wiring and inherits the self-mute guard
  - seeds: `c3`, `c7`
- `s7` — `reachy/cli/_commands/listen.py (--live composition)`: Folded ThinkHook engine kwargs (lines 438-440) and _make_self_mute_play_audio (line 697, stamps mute for clip duration + margin) are the --live seams; threading a voice-engine choice through here covers the live conversation loop
  - seeds: `c3`, `c7`
- `s8` — `pyproject.toml`: Base dependencies are numpy-only by hard constraint, and line 41's comment says 'Do NOT promote any engine package to base dependencies' — harmonics-cli therefore lands as an optional extra despite being pure-stdlib; memory records the uv.lock gotcha: bump + new dep without uv lock breaks CI
  - seeds: `c6`
- `s9` — `reachy/speech/markers.py + expressions catalog (per CLAUDE.md think internals)`: Expressions are driven off *emoji* markers on the producer thread, independent of the speak worker — emotes already run in parallel with whatever voices the ('speak', text) channel, so the emote path needs no change
  - seeds: `c12`, `c16`
- `s10` — `docs/export-schema.md + reachy/export/`: message blocks carry the spoken text and emotion blocks the pose — neither encodes which engine voiced the text, so the export wire contract is unaffected
  - seeds: `c13`
- `s11` — `challenge pass / adjacent-systems lens: reachy/cli/_commands/think.py demo verb`: think demo synthesizes via the same _synthesize alias as run; if the engine switch skipped demo, the scripted on-robot check would silently test only TTS
  - seeds: `c26`
- `s12` — `challenge pass / adjacent-systems lens: reachy/cli/_commands/say.py flag surface`: --voice/--speed/--tts-url only make sense for the tts engine; unhandled combination would confuse operators
  - seeds: `c27`
- `s13` — `challenge pass / cheap-probe lens: harmonics-cli wheel render timing (scratchpad)`: 17-word sentence: render_notes instant; render_wav 0.085 s @16k / 0.237 s @44.1k smooth; deterministic byte-identical across runs; empty text -> 0 notes -> valid empty WAV; 'reachy' signature = root 64 / pulse, distinct from other identities — retires the c25 latency unknown with measured numbers and verifies h9 in-process
  - seeds: `c28`
- `s14` — `challenge pass / failure-mode lens: reachy/service/units.py + manager.py restart policy`: All units carry Restart=on-failure + RestartSec=5; an exit-2 at startup of the harmonic live loop means an infinite crash loop on boot — the highest-blast-radius failure this feature can cause, since the user chose harmonic as the boot default
  - seeds: `c29`
- `s15` — `challenge pass / data-flow lens: markers.py quoted-speech channel + harmonics stress.parse_emphasis (probe)`: Probe: 'well... *really* well done' -> clean text + stressed word index [1]; '🎉' passes through as one word-note; clean_for_tts reuse would destroy the emphasis feature
  - seeds: `c31`
- `s16` — `challenge pass / concurrency lens: cognition speak worker + GIL + single media session`: Clean pass at measured magnitudes: harmonic render is an 85-240 ms CPU burst on the speak-worker thread (vs an HTTP wait for TTS); the producer/motion threads tolerate bursts of that size today; the media session usage is identical to the TTS leg. Residual risk parked for much longer sentences or slower boxes
- `s17` — `challenge pass / security lens: supply chain of the new dependency`: harmonics-cli is org-owned (AgentCulture), pure-stdlib at runtime, published via Trusted Publishing; pinned >=0.8 with no upper cap, matching the repo's reachy-mini>=1.0 pinning style
- `s18` — `challenge pass / reversibility lens: engine switch surface`: Fully reversible: flag/env selection is per-process, the boot unit reverts by re-running service enable after a one-line units.py change, and the feature writes no persistent state
- `s19` — `challenge pass / overlooked-actors lens: sleep wake path + reTerminal export consumers`: sleep's stimulus classifier already excludes self-speaker output via the shared self-mute; export consumers see identical message/emotion blocks — no changes needed for either actor

## Decisions

- HARMONIC_SAMPLE_RATE = 16000: probe on this box (the robot's compute) rendered a 17-word sentence at 16 kHz/smooth in 0.085 s (44.1 kHz: 0.237 s) for 4.14 s of audio — 3x cheaper, matches the speaker's real 16 kHz output rate so the sdk resample is an identity, and the motif's partials sit far below the 8 kHz Nyquist
- The harmonic backend receives the RAW quoted speech text (no clean_for_tts): harmonics' own parse_emphasis turns the LLM's *emphasis* into musical stress (probe-verified) and clean_for_tts would strip those markers; an emoji inside quotes becomes one hashed note — accepted. Empty/whitespace text returns b'' mirroring tts.synthesize's contract
