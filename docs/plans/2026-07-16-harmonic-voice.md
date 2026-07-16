# Build Plan — harmonic voice

slug: `harmonic-voice` · status: `exported` · from frame: `harmonic-voice`

> Reachy Mini now speaks with a harmonic voice: alongside its Chatterbox TTS speech engine, a new in-process harmonic voice backend (the harmonics-cli PyPI package, import package 'harmonics') renders each spoken sentence as a pleasant non-speech sonic gesture — a word-tracking melodic contour in Reachy's own identity signature — played through the robot speaker via the existing playback leg. Selectable on say, think, and listen --live via a voice-engine flag/env; cognition, emoting (pose expressions), hearing, and the export feed are untouched, so in a live conversation the robot hears words, thinks, emotes, and speaks harmonically in parallel.

## Tasks

### t1 — Harmonic voice backend: new reachy/speech/harmonic.py (synthesize(text) -> PCM16 @ HARMONIC_SAMPLE_RATE=16000 via harmonics render_notes + render_wav, raw-text in, WAV header stripped via stdlib wave, empty->b'', lazy import w/ CliError exit-2, env overrides REACHY_HARMONIC_IDENTITY/REACHY_HARMONIC_ARTICULATION, defaults reachy/smooth) + new reachy/speech/voice.py resolver (resolve_voice_engine(name|None) reads REACHY_VOICE_ENGINE, returns engine record: synthesize callable, samplerate, label). New test files tests/test_harmonic.py + tests/test_voice.py only

- covers: c2, h1, c5, h4, c22, h20, c25, h22, c4
- acceptance:
  - test_harmonic.py: same sentence renders byte-identical PCM twice; non-empty for plain text; b'' for empty/whitespace; 'reachy' signature differs from harmonics default identity; env overrides honoured
  - test_harmonic.py: a monkeypatched-urlopen test proves the harmonic path makes zero network calls
  - test_harmonic.py: timed render of a 15+ word sentence completes under 1 s (soft-report, hard-fail over 3 s)
  - test_voice.py: resolve_voice_engine(None) with no env -> tts engine (tts.synthesize, 24000); 'harmonic' -> harmonic engine (16000); unknown name -> CliError exit 1; env fallback works
  - No edits to any existing file; only the four new files

### t2 — Base dependency: pyproject.toml dependencies gains harmonics-cli>=0.8 (alongside numpy), pyproject comment updated to explain why a second base dep is allowed (pure-stdlib wheel, zero transitive deps, org-owned); uv.lock regenerated via uv lock. Touches pyproject.toml + uv.lock only (CLAUDE.md text lands in the docs task)

- covers: c32, h27
- acceptance:
  - uv sync in a clean env installs harmonics-cli and 'import harmonics' succeeds
  - uv.lock diff contains harmonics-cli; uv run pytest -n auto collects and passes existing suite

### t3 — say wiring: --voice-engine {tts,harmonic} on say run (default from resolver/env); harmonic engine synthesizes via reachy.speech.harmonic and passes samplerate=16000 in playback kwargs; --voice/--speed/--tts-url help text marked tts-only and ignored under harmonic. Touches reachy/cli/_commands/say.py + NEW test file tests/test_say_voice_engine.py only

- depends on: t1, t2
- covers: c3, h2, c27, h24
- acceptance:
  - say run --voice-engine harmonic 'hi' plays harmonic PCM (monkeypatched playback receives 16000-rate PCM); default/bare say run behaviour byte-identical (existing say tests pass unchanged)
  - say run --voice-engine harmonic --voice x --speed 2 runs the harmonic leg ignoring both flags; help text marks them tts-only
  - REACHY_VOICE_ENGINE=harmonic env selects harmonic without the flag; --voice-engine tts overrides env back

### t4 — think wiring: --voice-engine on think run AND think demo (both use the same _synthesize/_tts_kwargs path); think status --json gains voice_engine field; startup banner names the active engine; playback kwargs carry the harmonic samplerate so _guarded_play's mute math covers harmonic clip durations (regression test). Touches reachy/cli/_commands/think.py + NEW test file tests/test_think_voice_engine.py only

- depends on: t1, t2
- covers: c3, c26, h23, c30, h26, c7, h6
- acceptance:
  - think run --voice-engine harmonic builds CognitionEngine with the harmonic synthesize callable + 16000 playback samplerate; bare think run unchanged (existing think tests pass)
  - think demo --voice-engine harmonic drives the scripted marker stream through the harmonic leg (monkeypatched playback receives harmonic-rate PCM, no LLM, no TTS HTTP)
  - think status --json includes voice_engine; startup banner (stderr) names the engine
  - regression: _guarded_play stamps mute[until] >= clip duration computed as len(pcm)/2/16000 for a harmonic clip

### t5 — listen --live wiring: --voice-engine on listen run (honoured only with --live: without --live it is rejected exit-1 like --export); folded-engine kwargs get the harmonic synthesize + samplerate; _make_self_mute_play_audio math verified for harmonic-rate clips (regression test); live banner (stderr) names the active engine. Touches reachy/cli/_commands/listen.py + NEW test file tests/test_listen_voice_engine.py only

- depends on: t1, t2
- covers: c3, h2, c7, h6, c30, h26
- acceptance:
  - listen run --live --voice-engine harmonic composes the folded ThinkHook engine with the harmonic callable + 16000 samplerate (asserted via injected fakes); bare listen run and listen run --live unchanged (existing listen tests pass)
  - listen run --voice-engine harmonic without --live exits 1 with a hint naming --live
  - regression: the self-mute wrapper stamps mute[until] for clip_seconds computed from the harmonic samplerate + margin
  - banner line to stderr names the active voice engine; with --export stdout stays pure JSONL

### t6 — Live boot unit goes harmonic: exec_start_live() in reachy/service/units.py appends --voice-engine harmonic (ExecStart = '<python> -m reachy listen run --live --transcribe --voice-engine harmonic'); unit-renderer test asserts the flag. Touches reachy/service/units.py + its existing unit-renderer test file only

- depends on: t5
- covers: c24, h11
- acceptance:
  - Rendered live unit text contains 'listen run --live --transcribe --voice-engine harmonic'; daemon/demo units unchanged
  - Existing service/manager tests pass unchanged

### t7 — Explain catalog: reachy/explain/catalog.py ENTRIES updated for say run / think run / think demo / listen run to document --voice-engine, the harmonic engine, env vars (REACHY_VOICE_ENGINE, REACHY_HARMONIC_IDENTITY, REACHY_HARMONIC_ARTICULATION). Touches reachy/explain/catalog.py only (test_every_catalog_path_resolves covers resolution)

- depends on: t3, t4, t5
- covers: c8
- acceptance:
  - reachy explain say run / think run / listen run mention --voice-engine and the harmonic voice; test_every_catalog_path_resolves passes

### t8 — Docs: README + docs/operating-reachy.md (say/think/live sections + service section for the harmonic boot default + a 'harmonic voice' subsection: what it is, identity signature, env vars, before/after vs TTS-only) + CLAUDE.md (hard-constraints base-dep text now names harmonics-cli and why; say/think/listen/service noun rows mention the voice engine). Touches README.md, docs/operating-reachy.md, CLAUDE.md only

- depends on: t2, t3, t4, t5, t6
- covers: c8, h7, c19, h17, c21, h19
- acceptance:
  - markdownlint-cli2 green on all three files; operating guide documents the boot default and how to revert (service enable after removing the flag/env)
  - CLAUDE.md hard-constraints section states the two base deps (numpy, harmonics-cli) with the installability rationale; before/after narrative cites the TTS-unreachable degrade behaviour

### t9 — CI verification + boundary audit (operator task at merge time): full suite + coverage gate + teken rubric + lint stack green; git diff vs origin/main shows ZERO changes to reachy/speech/playback.py, reachy/motion/listen.py tiers/listen_transcribe.py/engagement.py/name_match.py, markers.py/expressions.toml/expressions.py/motion/expression.py, reachy/export//docs/export-schema.md; say boundary test unchanged-and-green

- depends on: t1, t2, t3, t4, t5, t6, t7, t8
- covers: c9, h8, c10, h12, c11, h13, c12, h14, c13, h15, c14, h16, c23
- acceptance:
  - uv run pytest -n auto green; coverage >= 60%; uv run teken cli doctor . --strict green; black/isort/flake8/bandit/markdownlint green
  - Protected-file list absent from the PR diff (name-only check); existing say import-boundary test passes unchanged

### t10 — On-robot verification (operator, main checkout, robot at localhost:8000): stop reachy-live user unit (single SDK owner); say run --voice-engine harmonic 'hello, tests are green' plays an audible motif; think demo --voice-engine harmonic plays gestures for the scripted stream; short live conversation via listen run --live --transcribe --voice-engine harmonic (address it by name, confirm harmonic response + emote); record render timing; re-run service enable live so the harmonic boot unit deploys; restore/verify unit active

- depends on: t9
- covers: c1, h10, c20, h18, c23, h21, c25, h22, h3, c4
- acceptance:
  - Audible harmonic motif from the robot speaker on say; live conversation answers addressed speech with harmonic gestures + pose emote; measured render time recorded in the delivery summary
  - reachy-live.service re-enabled and active with the harmonic ExecStart after the test

## Risks

- [unknown_nonblocking] harmonics-cli 1.x could relocate render_notes (it lives under a _commands module today); pinned >=0.8 with no cap per repo style — revisit at harmonics 1.0
- [unknown_nonblocking] On-robot session contends with the running reachy-live unit for the single-consumer SDK media session — t10 must stop the unit first and restore it after (procedural, from memory of the sleep-mode work) (task t10)
- [unknown_nonblocking] Same-wave file overlap is designed out (new test files per wiring task; CLAUDE.md edits consolidated in t8) — if a workforce agent strays outside its task's file list, the merge gate must catch it
