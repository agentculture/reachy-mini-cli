# t15 — live acceptance of the #155 arc on the deployed robot

Task t15 of `docs/plans/2026-08-02-foreground-gemma-background-qwen-155.md`,
the terminal wave and the PR gate.

**Headline: live acceptance is INCOMPLETE, and the plan's own acceptance
criterion — "all eight scenarios executed live" — is NOT met.** Four scenarios
were verified live, one is blocked on upstream, and three are blocked on the
box having a single audio output (issue #139, which is still open and still
accurate). This file records what actually happened, per the plan's
instruction that blocked scenarios are recorded BLOCKED, never rounded up.

The live pass did earn its keep in one specific way: it found a real defect in
the arc's headline feature that every offline test missed (§3).

## 1. What was running

| Piece | State |
|---|---|
| `reachy-daemon.service` | active |
| `reachy-runtime.service` | active, restarted 07:21 to recover the camera (see §2) |
| `reachy-embody.service` | active, restarted onto this branch |
| `reachy-bus-feed.service` | active (operator-local, bridges MQTT → the layer's feed FIFO) |
| Build under test | branch `spec/foreground-gemma-155` @ v0.47.0 |

The box's `uv tool` install is **editable** against `/home/spark/git/reachy-mini-cli`,
so the deployed layer imports straight from the checkout and a `systemctl --user
restart` is the whole deploy. Worth stating because it cuts both ways: the
services had been running since 2026-08-02 19:23 and were therefore executing
whatever the checkout held at *their* start time, with nothing signalling the
drift.

Only the embodiment layer needed redeploying. **The arc's diff against
`reachy/behavior/` and `reachy/motion/` is empty — zero lines** (§6).

## 2. Camera recovery (issue #138, incidental)

At first restart the layer dropped every clip ask as `clip-stale (age=42041.3s
> 30s)` — the rolling clip was frozen 11.7 h back, at the moment the services
had last started. This is issue #138 (camera pipeline EOS, silently dead until
a manual restart). Restarting `reachy-runtime.service` recovered it: the clip
went from 1 049 229 bytes stamped `Aug 2 19:38` to 2 468 806 bytes stamped
`Aug 3 07:21:46`. #138's restart workaround holds, and the perception lane had
real frames for the rest of this pass.

## 3. The defect live acceptance found (and the fix in this branch)

Once frames were fresh, **every** clip ask was dropped:

```text
[SENSE stage=embody source=clip event=092b358e] dropped reason=clip-answer-unstructured (```json
{" summary": "The camera is positioned in a room, capturing a view of a desk, a chair, and a window.", "entities
[SENSE stage=embody source=clip event=42ebbfb7] dropped reason=clip-answer-unstructured (```json
{" summary": "A modern, minimalist studio apartment is shown with a large desk and a rolling chair.", "entities"
[SENSE stage=embody source=clip event=1c3f7ce9] dropped reason=clip-answer-unstructured (```json
{" summary": "A wide shot shows a spacious, brightly lit room with a large desk, a chair, and a laptop.", "entit
```

The cause is **not** the code fence — `parse_perception_answer` already spans
first-`{` to last-`}`. The deployed senses model renders our own requested shape
back with **padded keys**: `{" summary": ...}`, not `{"summary": ...}`.
`DEFAULT_CLIP_PROMPT` asks for the unpadded form; the model reformats it.

Live proof of the failure and of the fix, from one real gateway call against
the deployed clip:

```text
RAW ANSWER:
{" summary": "The camera is positioned in a studio apartment, showing a workspace
with a desk, chairs, and a large window.", "entities": ["desk", " chair",
" window", " fridge"], " confidence": 0.9}

PARSED (after the fix):
('The camera is positioned in a studio apartment, showing a workspace with a
desk, chairs, and a large window.', ('desk', 'chair', 'window', 'fridge'), 0.9)
```

Three keys were padded (`summary`, `confidence`, and the entity *values*).

**Severity, stated precisely.** This was a *degrade*, not a total loss:
`_ClipAsker._ask_about` falls back to `summary=answer`, so the snapshot was
still populated — with the raw fenced JSON blob as its summary text, and with
`entities=()` and `confidence=None`. So the robot's perception lane was
carrying an unreadable blob and had lost its structure entirely. #153's
mechanism was working; its *output* was junk.

**Fix:** `_normalized_perception_keys` in `reachy/cli/_commands/agent.py`
strips whitespace and case from keys before lookup, and nothing else — a
genuinely different key (`description`) still misses, because tolerating a
sloppy rendering of our contract is not the same as guessing at a synonym for
it. Two tests, written failing first:
`test_parse_perception_answer_tolerates_a_reformatted_key` and
`test_parse_perception_answer_still_refuses_a_genuinely_different_key`.

After the fix the drops stopped entirely (a successful parse logs nothing —
the drop was the only observable).

## 4. Scenario results

| # | Scenario | Issue | Result |
|---|---|---|---|
| 1 | What-can-you-see, answered from the snapshot | #153 | **PARTIAL** — snapshot verified structured and live (§3); the spoken ask-and-answer half needs a voice in the room |
| 2 | Ambient speech produces no audible reply | #149 | **BLOCKED-ON-UPSTREAM** (§5) |
| 3 | Attention window tunable without a code edit | #150 | **PASS** (§4.1) |
| 4 | Verbose perception never displaces runtime facts | #154 | **NOT VERIFIED LIVE** — offline tests only |
| 5 | Background scope injection without speech | #155 | **NOT VERIFIED LIVE** |
| 6 | Honest attribution of Qwen results | #155 | **NOT VERIFIED LIVE** |
| 7 | Chunked long answers, clean mid-speech interruption | #151 | **BLOCKED** — needs a second audio output (#139) |
| 8 | Post-interruption continuation from updated state | #155 | **BLOCKED** — same |

### 4.1 Attention knob (#150) — PASS

```text
env var name      : REACHY_EMBODY_ATTENTION_WINDOW
default           : 45.0
resolved from env : 7.0     (REACHY_EMBODY_ATTENTION_WINDOW=7)
flag beats env    : 3.5     (explicit argument wins)
```

`--attention-window SECONDS` is present on `agent embody --help`, and the env
var was confirmed reaching the deployed service's process environment via a
temporary drop-in (since removed; the box is back to a clean environment). The
*behavioural* half — a window actually expiring mid-conversation — needs a
voice and was not run.

### 4.2 Connect-time voice conventions (h8 wire half) — PASS

The session URL carries t9's `system_prompt` verbatim on every connect:

```text
session up url=ws://localhost:8001/v1/realtime?input_sample_rate=16000&system_prompt=
You+are+Reachy+Mini%2C+a+small+expressive+desk+robot%2C+speaking+your+replies+aloud
+through+a+text-to-speech+voice+%E2%80%94+never+write+markdown%2C+bullet+points%2C
+code+fences+or+emoji...
```

Whether it changes *spoken* behaviour is h8's other half and needs an ear in
the room. Not run.

### 4.3 Risk r6 (AEC self-cut) — no failure observed

The tail cut is armed and `mute_during_playback` defaults to **False**,
trusting Reachy's hardware AEC. If the AEC were insufficient, the robot's own
voice would fire `speech_started` and it would cut itself off.

```text
07:25:08  response started id=resp_69d59e70a8e64ec58f99182e
07:25:48  response done   id=resp_69d59e70a8e64ec58f99182e chars=239 audio=541440B
```

541 440 bytes at 16 kHz mono s16le ≈ **16.9 s of audio**, and **no
`speech started` fired anywhere in that 40-second window**. So no self-cut.
This is one episode, not a law, and it does not establish that the audio was
audible in the room — that needs an ear (#139).

## 5. Blocked on upstream — issue #149

The layer's #149 fix (per-utterance arming, task t8) is wired, fails closed,
and **says so by name** on every connect:

```text
dropped reason=one-shot-arming-unsupported (the gateway announced no one-shot
arming, so this session arms once and it will answer every utterance
(lobes-cli#170 item 1))
```

The deployed gateway does not announce one-shot arming, so the session latches
and the pre-existing behaviour stands. It reproduced live, twice, in the exact
shape #149 describes — the mind refusing while the mouth answers anyway:

```text
07:25:08  utterance chars=5
07:25:08  dropped reason=not-addressed-cold ("Yeah.")      <- attention gate refused it
07:25:08  response started id=resp_69d59e70a8e64ec58f99182e <- session answered regardless
07:25:48  response done ... chars=239 audio=541440B
```

(and, on the pre-restart build, the same four-line pattern with `"Okay."` and
180 480 B.)

**#149 must not be closed by this arc.** The client half is done and inert;
closing it depends on `agentculture/lobes-cli#170` item 1.

## 6. Structural gate (the PR-time checks)

| Check | Result |
|---|---|
| `tests/test_zero_llm_boundary.py` + `tests/test_embody_redteam.py` | **62 passed** |
| Runtime diff `reachy/behavior/` + `reachy/motion/` vs `main` | **empty — zero lines changed** |
| Deletions inside those directories | **0** |
| Full suite | 4954 passed, 8 skipped |

The eighth skip is environmental, not a regression: `test_vision_scene_integration.py`
skipped itself with `scene endpoint failed for a non-#132 reason: vlm-unreachable
… timed out` — the gateway was busy serving this pass's own clip asks. It
passes (4955/7) when the endpoint is free.

The previous arc's headline was "3 files, 6 hunks, 1486 insertions, 0
deletions" inside the runtime. This arc touched it **not at all**: 34 files and
~14 700 insertions, none of them in the presence runtime's decision loop.

## 7. What still needs an operator, and why

Three scenarios (7, 8, and the spoken halves of 1, 2 and 4.2) need **a second
conversational party able to make sound in the room**. Issue #139 is still
accurate and still open.

I re-checked it during this pass and briefly believed it had lifted —
`output:hdmi-stereo` reported `available: yes` and an HDMI sink was present.
That was a monitor being momentarily awake. Minutes later the same profile read
`available: no`, the sink was gone from `pactl list sinks`, and `paplay` to it
timed out exactly as #139 documents. **There is still exactly one audio output
on this box.** Correcting that reading here so the next pass does not chase it.

To finish: attach a USB or Bluetooth speaker (either appears as its own
PipeWire sink immediately), then re-run scenarios 1, 2, 4.2, 7 and 8 with a
person in the room.

## 8. Issue disposition

| Issue | Disposition |
|---|---|
| #150 | Closable — knob verified live (§4.1) |
| #153 | Mechanism verified live and a real defect fixed (§3); the spoken half unverified |
| #154 | Offline only — do not close on this evidence |
| #151 | Blocked on #139 |
| #149 | **Keep open** — blocked on lobes-cli#170 item 1 |
| #155 | **Keep open** — scenarios remain |
| #138 | Recurred and was worked around by restart (§2) — unchanged, still open |
| #139 | Confirmed still accurate (§7) |
