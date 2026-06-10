# Manual Verification Checklist — think body-expression

**Feature:** `reachy-mini-cli think` gestures while speaking — expression markers in the
LLM stream drive calm body poses that arrive timed to the robot's spoken thoughts.

**How to run the demo:**

```bash
# Prerequisites: daemon running, TTS reachable, SDK extra installed.
reachy-mini-cli daemon start
reachy-mini-cli think demo                   # built-in 3-gesture script
# or, to run the live cognition loop:
reachy-mini-cli think run --max-turns 5
```

The demo drives a fixed scripted stream through the same path the live cognition
loop uses: `MarkerParser` → `ExpressionProducer` enqueues gestures on the serial
motion queue → TTS speaks each quoted phrase.

---

## Gate 1 — Physical setup

- [ ] The robot is powered on and the daemon is running
      (`reachy-mini-cli device status` exits 0).
- [ ] TTS is reachable (`reachy-mini-cli say run "test" --transport sdk` plays
      audio through the robot speaker).
- [ ] No other motion loop (`listen`, `vision`, `demo-mode`) is running.

---

## Gate 2 — Demo run (scripted verification)

Run `reachy-mini-cli think demo` and observe:

### c1 / h1 — Movement is timed to thoughts, not random

- [ ] The **first gesture** (`🤔`) fires **before or with** the first spoken
      phrase ("I wonder what that sound was."), not after it is finished.
- [ ] The **second gesture** (`👂`) fires **before or with** the second phrase
      ("There it is again, to my left."), not before the first phrase starts.
- [ ] The **third gesture** (`🙂`) fires **before or with** the third phrase
      ("Ah — it's just the fan.").
- [ ] No gesture fires **during silence** between phrases.

### c5 / h5 — Body is calmer than full idle / listen

- [ ] Gesture amplitudes are **small and deliberate** — the robot does not lurch
      or sweep to large angles. Each pose is a gentle offset from neutral.
- [ ] Between gestures (while TTS is playing / brief pauses) the robot holds
      its pose rather than continuing to idle-wander. The posture reads as
      "still and thinking", not "restless".

### c7 / h7 — Distinct expressions are distinguishable by sight and match their thought

- [ ] The `🤔` (pondering) gesture looks **visually different** from the `👂`
      (listening) gesture. At minimum, the head tilt / antenna angle or both
      differ in a way a bystander could name ("that was a head-tilt", "antennas
      perked up").
- [ ] The `👂` (listening) gesture is directionally biased **toward the side**
      indicated by the phrase ("to my left") — i.e. antennas or head lean left,
      not right or neutral.
- [ ] The `🙂` (satisfied / calm) gesture looks more **settled** than the `🤔`
      pose — e.g. the head is more level, antennas are relaxed.
- [ ] All three gestures are **distinct from each other** — a bystander can tell
      them apart without being told which emoji drove which pose.

---

## Gate 3 — Live cognition loop (`think run`)

Run `reachy-mini-cli think run --max-turns 3` near an active sound source:

### c1 — Movement timing in live mode

- [ ] Each time the robot speaks, at least one gesture fires **during that
      thought** (marker arrival precedes or coincides with the spoken phrase it
      belongs to), not before the thought starts or after it ends.
- [ ] If the robot produces multiple thoughts, the gestures for each thought are
      distinct in timing from the gestures of the previous thought.

### c5 / h5 — Calmer body than full idle

- [ ] Between thoughts (when `think` is listening / accumulating cues) the robot
      **does not wander** with large idle sweeps. It holds its position.
- [ ] When `listen` is running concurrently (or after `think run` finishes),
      its idle motion resumes — confirming the `cognition_active` signal
      suppressed it while `think` was running.

### motion-off vs motion-on readability

- [ ] Without `--transport sdk` installed, running
      `reachy-mini-cli think demo --transport http` still **speaks** all three
      phrases (TTS + playback work over HTTP), but the robot body does **not**
      move (no SDK transport → motion executor degrades silently). This confirms
      the motion and speech legs are independent.

---

## Notes

- **Coalescing behaviour:** if the LLM emits two markers in rapid succession
  before the motion executor drains the queue, only the **latest** pending
  expression fires (by design — EXPRESSION_KEY coalescing). On the demo's
  scripted sequence this does not occur because TTS serializes between phrases.
  In the live loop, rapid bursts are expected and coalescing is correct.
- **Rubric path:** `reachy-mini-cli explain think demo` should return the demo
  verb's documentation.
- **JSON result:** `reachy-mini-cli think demo --json` should output a JSON
  object with `"status": "ok"`, `"expressed"` (list of 3 emojis), and
  `"spoken"` (list of 3 phrase strings).
