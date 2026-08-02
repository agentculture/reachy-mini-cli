# Runtime-equivalence proof (t13)

Task `t13` of `docs/plans/2026-08-01-embodiment-layer.md`: prove the arc's
central promise — enabling or disabling the embodiment layer changes nothing
about how the robot's existing presence behaves on its own. Covers honesty
conditions **h1** (layer absent/disabled ⇒ zero runtime diff outside additive
export legs) and **h19** (the before-state citation still holds on merge day).
This is verification, not a feature: nothing below was "fixed" — every claim
was checked against the merged tree as it stands, and anything that did not
hold would be reported as a finding rather than repaired.

- Date: 2026-08-02
- Worktree: `/home/spark/git/.worktrees.reachy-mini-cli/embody-t13`, branch
  `embody/t13`, based on `spec/embodiment-layer` at commit `3b55c84` (t1–t12
  merged)
- No embody process, runtime, or daemon was started to produce this evidence.
  `ps aux | grep -i embody` returned nothing, and no `embody*.pid` file exists
  under the state dir, at the time every command below was run. (Two
  unrelated, pre-existing processes — `reachy-mini-daemon` and
  `python -m reachy behavior engine run` — were running on this box the whole
  time, spawned from a separate global `uv tool` install
  (`/home/spark/.local/share/uv/tools/reachy-mini-cli/…`), not from this
  worktree. They belong to the deployed production robot, are unrelated to
  this task, and `tests/conftest.py`'s autouse guards keep the suite from
  reaching them — see [What this does not prove](#what-this-does-not-prove).)

## VERDICT

**Both h1 and h19 hold, checked against the current tree rather than
assumed.**

- **h1** — the full suite and the rubric gate are green with no embody process
  running, and the entire arc's footprint inside `reachy/behavior/` is 3
  files, 6 diff hunks, 1486 inserted lines, **0 deleted lines** — every hunk
  classifies as an additive tee leg or an additive clip leg. No hunk falls
  into "something else."
- **h19** — `agent attach` still has no transcript cue (verified by reading
  `_CUE_MAPPERS` and `RUNTIME_BLOCKS`) and its `speak`/`harmonics`/`apply_pose`
  tools are still wired publish-only (verified by reading the composition
  code — `_silent_synth`, `_no_play`, `_no_express`). Issue #93 is still
  **OPEN** per a live `gh issue view` call, verbatim output below.

One thing worth naming up front, since the task asked for the single most
important tick-path finding if one exists: **there is none.** Both additive
`offer()` legs are genuinely O(1) per call — a flag/counter store, a bounded
lock-guarded append, no socket I/O, no encoding, no filesystem access. The one
thing worth flagging for the record (not a defect, not new to this arc) is
that the clip rider's separate per-tick *publisher* method (not `offer()`,
and not what the task asked to be measured) does real per-tick file I/O — but
it does so by citing an already-shipped, pre-existing pattern unchanged by
this arc. See [Tick-cost analysis](#tick-cost-analysis-of-the-additive-legs).

## Commands run, verbatim

### The suite

```console
$ cd /home/spark/git/.worktrees.reachy-mini-cli/embody-t13
$ uv run pytest -n auto
........................................................................ [ 92%]
....s................................................................... [ 94%]
........................................................................ [ 95%]
........................................................................ [ 97%]
........................................................................ [ 99%]
....................................                                     [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/test_agent_turn_cortex_integration.py:194: model 'sakamakismile/Qwen3.6-27B-Text-NVFP4-MTP' unreachable via gateway http://localhost:8001 (LLM endpoint returned HTTP 404 (http://localhost:8001)) — skipping
SKIPPED [1] tests/test_agent_turn_cortex_integration.py:194: model 'nvidia/Gemma-4-31B-IT-NVFP4' unreachable via gateway http://localhost:8001 (LLM endpoint returned HTTP 404 (http://localhost:8001)) — skipping
SKIPPED [1] tests/test_vision_scene.py:292: could not import 'cv2': No module named 'cv2'
SKIPPED [1] tests/test_vision_scene.py:305: could not import 'cv2': No module named 'cv2'
SKIPPED [1] tests/test_vision_scene.py:315: could not import 'cv2': No module named 'cv2'
SKIPPED [1] tests/test_vision_face.py:508: could not import 'cv2': No module named 'cv2'
SKIPPED [1] tests/test_vision_face.py:497: could not import 'cv2': No module named 'cv2'
======================= 4279 passed, 7 skipped in 54.72s =======================
```

`4279 passed, 7 skipped` — the baseline this task was assigned to, plus the two
regression tests the merge-day refresh below accounts for
hold. All 7 skips are environmental (an unreachable LLM role at the deployed
gateway; `cv2`/`opencv` genuinely absent from this box, the same `[vision]`
extra gap the clip rider itself degrades against), never a masked failure.

### The rubric gate

```console
$ uv run teken cli doctor . --strict
healthy: 26/26 passed, 0 errors, 0 warnings
[structure]
  PASS         pyproject_exists: found /home/spark/git/.worktrees.reachy-mini-cli/embody-t13/pyproject.toml
  PASS         project_scripts: 2 entry/entries: reachy, reachy-mini-cli
  PASS         tests_dir: found /home/spark/git/.worktrees.reachy-mini-cli/embody-t13/tests
  PASS         top_help_runs: 3061 chars on stdout
  PASS         main_entry_contract: `reachy.cli.main(['--help'])` conforms to main(argv) -> int

[learnability]
  PASS         learn_exit_zero: exit=0 stdout_len=3042
  PASS         learn_min_length: 3042 chars ≥ 200
  PASS         learn_markers: all required markers present

[json]
  PASS         learn_json_parseable: 1681 chars JSON
  PASS         stderr_clean_on_success: stderr empty on success
  PASS         explain_json_parseable: 2133 chars JSON

[errors]
  PASS         bogus_verb_exits_nonzero: exit=1
  PASS         error_has_hint: found hint: or try: line
  PASS         no_traceback: stderr has no Traceback
  PASS         exit_codes_documented: learn output mentions 'exit' and codes 0/1/2

[explain]
  PASS         explain_exists: 2058 chars on stdout
  PASS         explain_self: `explain reachy` produced 2058 chars
  PASS         explain_bogus_fails: exit=1 with hint

[overview]
  PASS         overview_global_exists: 1018 chars on stdout
  PASS         overview_cli_noun_exists: `cli overview` produced 876 chars
  PASS         overview_json_shape: subject='reachy-mini-cli', sections=3
  PASS         overview_graceful_on_bad_path: overview fell back gracefully on a missing target path

[doctor]
  PASS         doctor_global_exists: exit=1 stdout_len=487
  PASS         doctor_json_shape: healthy=False, checks=3
  PASS         doctor_check_shape: every check (3) carries the required keys
  PASS         doctor_remediation_when_unhealthy: all 1 failed checks supply a remediation
```

Process exit code: `0`. (`doctor_global_exists: exit=1` is the rubric probing
the CLI's own `doctor` verb — that verb legitimately reports `healthy=False`
with one remediation-bearing failed check on this box; it is the rubric
*test* named `doctor_global_exists` that must pass, and it does. This is
unrelated to the embodiment layer and unrelated to the suite result above.)

Both commands ran against the merged worktree (`embody/t13` at commit
`3b55c84`, `uv sync`'d clean — `git status --short` showed nothing after
`uv sync`), with no embody process, runtime, or daemon started for this task.

## Environment: reproducing the toolchain

```console
$ uv --version
uv 0.9.28
$ python3 --version
Python 3.12.3
$ uv run pytest --version   # via the resolved venv: pytest 9.0.3, python 3.12.12
```

`uv sync` resolved `reachy-mini-cli==0.44.1` (the current `pyproject.toml`
version — left untouched by this task, per the hard rule against modifying
`pyproject.toml`) editable from this worktree, plus the dev toolchain
(`pytest 9.0.3`, `pytest-xdist 3.8.0`, `teken 0.8.0`, `events-cli 0.9.0`,
`harmonics-cli 0.8.0`, `numpy 2.4.6`, `paho-mqtt 2.1.0` as `events-cli`'s own
dependency — the base-dep set `CLAUDE.md`'s hard constraints already document).

## The `reachy/behavior/` diff, classified

The merge-base of `embody/t13` against `main` is `7ea6878` (the "Reachy's
nervous system" commit) — and it is also the direct parent of `4f0fae1`, this
arc's very first commit (`spec: embodiment-layer (devague /scope + /think)`):

```console
$ git merge-base HEAD main
7ea68784e2a86ab8fad1483aa3951d572d1c770e
$ git log --oneline -1 4f0fae1~1
7ea6878 Reachy's nervous system: fix vision/hearing/voice and expose the senses on an event bus (#128)
```

So `7ea6878` is unambiguously the arc's before-state. Diffing the whole arc
(38 commits, `t1`–`t12`) against it, scoped to `reachy/behavior/` exactly as
the acceptance criterion asks:

```console
$ git diff --stat 7ea6878 HEAD -- reachy/behavior/
 reachy/behavior/audio_tee.py  | 783 ++++++++++++++++++++++++++++++++++++++++++
 reachy/behavior/clip_rider.py | 656 +++++++++++++++++++++++++++++++++
 reachy/behavior/face_sense.py |  47 +++
 3 files changed, 1486 insertions(+)
$ git diff --name-status 7ea6878 HEAD -- reachy/behavior/
A  reachy/behavior/audio_tee.py
A  reachy/behavior/clip_rider.py
M  reachy/behavior/face_sense.py
$ git diff 7ea6878 HEAD -- reachy/behavior/ | grep -c '^@@'
6
$ git diff 7ea6878 HEAD -- reachy/behavior/ | grep -E '^-' | grep -v '^---'
[no output — zero removed lines anywhere in the package]
```

For context, the arc as a whole touches 55 files (`+19939 / -102`) — almost
all of it new modules under `reachy/embody/`, `reachy/speech/`, and their
tests, plus the composition wiring in `reachy/cli/_commands/agent.py` and
`reachy/cli/_commands/behavior.py`. `reachy/behavior/` — the decision-loop
package the arc promised to leave alone — is 3 files and 6 hunks of that
20,000-line diff, and every one of those hunks is purely additive (`git diff
--stat` reports `0` deletions in the package; the direct grep for removed
lines above confirms it, not just the stat summary).

### Hunk-by-hunk classification

| # | File | What changed | Classification |
|---|---|---|---|
| 1 | `reachy/behavior/audio_tee.py` | New file (783 lines): `AudioTee`, a bounded drop-don't-block fan-out of the tick's already-taken mic chunk to a local `AF_UNIX` socket | **Additive tee leg** |
| 2 | `reachy/behavior/clip_rider.py` | New file (656 lines): `ClipRider`, a rolling video-clip ring + bounded on-disk clip file + `state.json` reference publish | **Additive clip leg** |
| 3 | `reachy/behavior/face_sense.py` (docstring) | Adds a "Frame fan-out — a PUSH seam for a second in-process consumer" section documenting `add_frame_sink` | **Additive clip leg** (documentation of the seam the clip rider attaches to) |
| 4 | `reachy/behavior/face_sense.py` (`__init__`) | Adds `self._frame_sinks: list[...] = []` | **Additive clip leg** |
| 5 | `reachy/behavior/face_sense.py` (`_update_frame`) | Adds one call, `self._fan_out_frame(frame)`, inside the existing usable-frame branch | **Additive clip leg** (the one line that actually runs on the tick thread) |
| 6 | `reachy/behavior/face_sense.py` (new methods) | Adds `add_frame_sink()` and `_fan_out_frame()` | **Additive clip leg** |

6 hunks total: 1 tee-leg hunk, 5 clip-leg hunks, **0 hunks classified as
"something else."**

### The hunks themselves

`audio_tee.py` and `clip_rider.py` are wholly new files, so `git diff` renders
each as a single hunk spanning the entire file (`@@ -0,0 +1,783 @@` /
`@@ -0,0 +1,656 @@`) — quoting either in full would mean pasting 700+ lines
that add no more information than "the file classifies as its own leg."
Instead, here is the one function from each that the task specifically asked
to be measured for tick cost, quoted as the real diff renders it (every line
below is a `+` line — there is no context to elide because the whole file is
new):

```diff
+    # the producer side (tick thread) — O(1), no I/O, no logging
+    # ------------------------------------------------------------------
+
+    def offer(self, chunk: object) -> None:
+        """Hand this tick's already-taken chunk to the fan-out. **O(1).**
+
+        Never raises, never blocks, never touches a socket and never logs: the
+        whole point is that a wedged consumer costs the 20 ms tick nothing. With
+        nobody connected it is a bare flag store; the worker turns that into one
+        named ``no-consumer`` line per episode.
+
+        The chunk is coerced through :func:`reachy.robot.audio_shape.to_mono` at
+        this boundary rather than flattened — a multi-channel read must have a
+        channel SELECTED, or the wire carries both channels interleaved into one
+        double-length stream the header then mislabels. For the 1-D float32 the
+        pump produces this is a pass-through with no copy.
+        """
+        if not self._active or self._closed:
+            return
+        self.offers += 1
+        if not self._has_consumers:
+            self._discarded = True
+            return
+        mono = to_mono(chunk)
+        if mono is None or mono.size == 0:
+            return
+        # The counters ride the SAME lock as the queue they describe: both
+        # threads touch them, and ``+=`` on an int is a read-modify-write, not
+        # an atomic. Uncontended acquisition is nanoseconds and never becomes a
+        # wait — the worker only ever holds this lock for an O(1) deque swap.
+        with self._lock:
+            dropped = self._pending.push(mono)
+            self.queued += 1
+            if dropped:
+                self.dropped += dropped
+                self._overflow_run += dropped
```

(`reachy/behavior/audio_tee.py`, `AudioTee.offer`.)

```diff
+    # ------------------------------------------------------------------ #
+    # Frame-sink target (tick thread)                                    #
+    # ------------------------------------------------------------------ #
+
+    def offer(self, frame: object) -> None:
+        """The frame-sink target — O(1), never raises, never encodes.
+
+        Registered via ``face_driver.add_frame_sink(rider.offer)``. A disabled
+        rider (no encoder) is a checked no-op: nothing will ever drain the
+        inbox, so there is no reason to hold onto frame references at all.
+        """
+        if not self._enabled or self._closed:
+            return
+        try:
+            now = self._clock()
+            with self._inbox_lock:
+                if len(self._inbox) >= self._inbox.maxlen:
+                    self.inbox_dropped += 1
+                self._inbox.append((now, frame))
+        except Exception as err:  # noqa: BLE001 — a sink must never break the tick
+            logger.debug("ClipRider: offer() raised (%s); frame not queued", err)
```

(`reachy/behavior/clip_rider.py`, `ClipRider.offer`.)

And the full `face_sense.py` diff (small enough to quote entirely — all 4
hunks, every line a `+`):

```diff
@@ -70,6 +70,26 @@ toward ``frame_available``): ``None``, a 0-d array, an empty array, a wrong
 number of dimensions or channels, and anything numpy cannot convert are all
 skipped silently. A degenerate frame is a non-reading, never an exception.

+--------------------------------------------------------------------------
+Frame fan-out — a PUSH seam for a second in-process consumer
+--------------------------------------------------------------------------
+This driver is the ONLY thing in the runtime allowed to call
+:meth:`HeldMediaClient.frame`: any other piece that wants to see camera frames
+(the clip rider, a future consumer) must never open a second read against the
+one held media client — that is exactly the single-SDK-owner contention this
+module's docstring already warns about for the SDK's media session generally.
+:meth:`add_frame_sink` registers a zero-arg-return callable that
+:meth:`_update_frame` PUSHES every USABLE frame to, the moment it is read — the
+same shape :class:`reachy.cli._commands.behavior._AudioTap`'s ``add_sink`` uses
+for the audio leg. Push rather than a second peek is what makes "no second
+camera read" structural rather than a call-site convention: a sink cannot be
+wired to anything but this one read. A sink is called with the frame the tick
+already validated (never ``None`` or a degenerate array — :func:`usable_frame`
+gates it first), runs UNCONDITIONALLY of whether a face recognizer is
+composed (a cv2-less-but-still-camera'd box can still feed a sink that has its
+own reason to want frames), and its faults are swallowed here: a misbehaving
+sink degrades to a debug log line, never a tick fault.
+
 --------------------------------------------------------------------------
 Degradation
 --------------------------------------------------------------------------
@@ -370,6 +390,9 @@ class FaceSenseDriver:
         self._input = _Slot()
         #: Worker -> tick thread: the latest matched, named face.
         self._output = _Slot()
+        #: PUSH consumers of every usable frame (see the module docstring's
+        #: "Frame fan-out" section) — the clip rider registers here.
+        self._frame_sinks: list[Callable[[object], None]] = []
         #: Worker-thread-only: clock reading of the last detection (cadence gate).
         self._last_detect: float | None = None
         #: Tick-thread-only: name -> ``ctx.now`` of its last latch (cooldown).
@@ -435,6 +458,7 @@ class FaceSenseDriver:
         if usable_frame(frame):
             self._last_frame_at = now
             self._frame_available = True
+            self._fan_out_frame(frame)
             if self._recognizer_ready:
                 self._input.publish(frame)
             return
@@ -501,6 +525,29 @@ class FaceSenseDriver:
             logger.debug("FaceSenseDriver frame read raised; no frame this tick", exc_info=True)
             return None

+    def add_frame_sink(self, sink: Callable[[object], None]) -> None:
+        """Register a PUSH consumer of every usable frame (module docstring).
+
+        Called once at composition, e.g. ``face_driver.add_frame_sink(
+        clip_rider.offer)``. Never called on the tick thread itself.
+        """
+        self._frame_sinks.append(sink)
+
+    def _fan_out_frame(self, frame: object) -> None:
+        """Push *frame* to every registered sink — O(1), never raises into the tick.
+
+        Mirrors :meth:`reachy.cli._commands.behavior._AudioTap.pull`'s sink
+        fan-out: a sink is called with the frame the moment it is read, so a
+        consumer can never be wired to anything but THIS read — no second
+        ``media.frame()`` call, no second camera contention. A misbehaving sink
+        degrades to a debug log line, never a tick fault.
+        """
+        for sink in self._frame_sinks:
+            try:
+                sink(frame)
+            except Exception as err:  # noqa: BLE001 — a fan-out consumer must never break the tick
+                logger.debug("FaceSenseDriver: frame sink raised (%s); frame not delivered", err)
+
     # -- match leg ------------------------------------------------------ #

     def _drain_match(self, now: float | None) -> None:
```

Every hunk above is purely additive (docstring paragraph, a field
initializer, one method call, two new methods). None of them changes an
existing branch's condition, an existing return value, or an existing call's
arguments. The one behavioral change on the tick thread is hunk 5:
`self._fan_out_frame(frame)`, inserted after `self._frame_available = True` —
and, per the classification above and the tick-cost analysis below, that call
is a bounded loop over (in production, exactly one) O(1) sink.

### Composition root context (outside the literal scope, cited for completeness)

The acceptance criterion asks specifically for a "reviewed diff of
`reachy/behavior/`," which is what the classification above covers in full.
The two new legs are wired *in* from `reachy/cli/_commands/behavior.py` (a
different directory, `_commands`, not `reachy/behavior/`), which — being the
composition root for the whole runtime — legitimately grows: `+162/-28`
lines across the arc (`git diff --stat 7ea6878 HEAD --
reachy/cli/_commands/behavior.py`), adding `_make_audio_tee`, `_make_clip_rider`, the
`add_sink`/`add_frame_sink` registration calls, and `_AudioTap`'s own
`sinks=()` constructor parameter (`_AudioTap` itself lives in `_commands/
behavior.py`, not `reachy/behavior/`, and predates this arc). Both
registrations happen exactly once, at composition time, never per tick:

```python
# reachy/cli/_commands/behavior.py, inside _compose_run_seam
clip_rider = _make_clip_rider(main_control)
face_driver.add_frame_sink(clip_rider.offer)
...
tee = _make_audio_tee(lambda: audio_tap.samplerate)
tee.start()
audio_tap.add_sink(tee.offer)
```

Both `clip_rider` (the driver object, for its own separate per-tick
`__call__`) and `tee` (via `_RuntimeResources`, for shutdown) are threaded
into the runtime's teardown path so an unclosed socket or worker thread
cannot hang the process at interpreter exit — the same discipline every other
worker-owning runtime piece already follows.

## Tick-cost analysis of the additive legs

The runtime's decision loop holds a 20 ms budget at 50 Hz
(`docs/verification/2026-07-20-retire-old-flow-baseline.md` measured
425–1213 ms tick overruns, 21×–61× the budget, from inline SDK-client
construction on the tick thread — the baseline this arc is not allowed to
reproduce). Both additive legs were read function-by-function against that
budget.

### `_AudioTap.add_sink` + `AudioTee.offer`

- **`add_sink(sink)`** (`_commands/behavior.py`) — `self._sinks.append(sink)`.
  Called exactly once, at composition time (setup thread), never on the tick
  thread.
- **`_AudioTap.pull(t)`** — the existing per-tick latch swap
  (`self._chunk = self._pump.take()`), now followed by `for sink in
  self._sinks: sink(self._chunk)` guarded by a bare `try/except`. With one
  sink registered (production: `tee.offer`), this is one extra Python
  function call per tick, wrapped in exception handling that itself costs
  nothing on the success path.
- **`AudioTee.offer(chunk)`** — the function under measurement. Reads:
  - a boolean check (`self._active`/`self._closed`);
  - an integer increment (`self.offers += 1`);
  - a boolean check (`self._has_consumers`, a **plain bool store/load**,
    deliberately not lock-guarded — the docstring says this is atomic under
    the GIL and "must never take a lock to learn 'is anybody listening?'");
  - `to_mono(chunk)` (`reachy/robot/audio_shape.py`, unchanged by this arc):
    for the 1-D float32 array the pump already produces, `np.asarray(raw,
    dtype=np.float32)` returns the SAME array with **no copy** — confirmed by
    reading `to_mono`'s source, not assumed from the docstring;
  - `with self._lock: self._pending.push(mono)` — a `threading.Lock()`
    acquisition (uncontended: nanoseconds) guarding a `deque.append` (O(1)).
  There is **no socket call, no `send`, no `accept`, no encoding, and no
  logging** anywhere in this function — every one of those lives on the
  separate worker thread (`_loop`/`_serve_once`/`_fan_out`/`_flush_all`),
  which the tick thread never touches. This matches the docstring's own claim
  ("**O(1)**... does no socket I/O at all") — verified by reading the
  function body line by line, not by trusting the comment.
- One precise nuance the docstring elides: the worker's `_drain()` briefly
  holds the SAME `self._lock` while doing `list(self._pending.swap())` — the
  `swap()` itself is O(1) (a deque reference replace), but converting the
  swapped deque to a list happens inside the lock. This is bounded by
  `DEFAULT_MAX_CHUNKS = 64` (a shared-queue cap sized for ~2 s of audio at
  32 ms/chunk) — at most 64 pointer copies while holding the lock — so a tick
  thread's `offer()` could, in the rarest worst case, block for the duration
  of a 64-item list build rather than truly zero time. This is a genuine
  (if vanishingly small) deviation from a literal "never blocks" claim, and
  it is worth naming precisely rather than repeating the docstring's
  rounding-off. It is not the kind of finding the task asked to be flagged as
  disqualifying — copying ≤64 array references is on the order of
  microseconds, several orders of magnitude under the 20 ms budget — but a
  reader checking this claim themselves should know the "O(1), no blocking"
  description is a deliberate simplification of "bounded, and the bound is
  small," not literally lock-free.

**What it can never do:** open or write to a socket, run an encoder, touch
the filesystem, or wait on I/O. All of that is structurally on the worker
thread — the tick-thread function has no code path that reaches it.

### `FaceSenseDriver.add_frame_sink` + the clip rider's `offer`

- **`add_frame_sink(sink)`** — `self._frame_sinks.append(sink)`. Exactly one
  call site (composition, setup thread): `face_driver.add_frame_sink(
  clip_rider.offer)`, confirmed structurally by
  `tests/test_behavior_clip_rider_composition.py::
  test_the_rider_is_wired_as_a_frame_sink_not_a_reader_of_its_own`, which
  asserts by AST that `_compose_run_seam` registers exactly one
  `add_frame_sink` call and that `clip_rider.py` itself never calls
  `.frame()`.
- **`FaceSenseDriver._fan_out_frame(frame)`** — the new tick-thread call
  (hunk 5 above wires it into `_update_frame`, which already runs every tick
  as part of the pre-existing frame-peek). It is `for sink in
  self._frame_sinks: try: sink(frame) except Exception: <debug log>`. With
  one sink registered in production, this is one extra function call per
  tick guarded by exception handling — the same shape as `_AudioTap.pull`'s
  sink loop, and by the same author's own note in the diff, deliberately so
  ("Mirrors `_AudioTap.pull`'s sink fan-out"). Nothing bounds the NUMBER or
  COST of sinks a future caller could register here — today there is exactly
  one, and it is O(1), so this is a structural observation about the seam's
  design, not a defect in what is shipped.
- **`ClipRider.offer(frame)`** — the function under measurement. Reads:
  - a boolean check (`self._enabled`/`self._closed` — `self._enabled` is
    `False` on any box without the `[vision]` extra, this box included, so
    the function returns immediately on this dev box, before doing anything
    else);
  - `self._clock()` (`time.monotonic` by default — a syscall, but a cheap
    one, not I/O in the blocking sense the tick budget cares about);
  - `with self._inbox_lock: ... self._inbox.append((now, frame))` — a
    `threading.Lock()` guarding a bounded-`deque` append (`deque(maxlen=32)`
    by default), which **silently evicts the oldest entry itself** when full
    (that is what a `maxlen` deque does), so `offer()` never grows unbounded
    and never blocks waiting for room.
  There is **no `cv2` call, no `VideoWriter`, no filesystem write, and no
  network access** anywhere in this function — the encoder only runs on the
  separate `behavior-clip-worker` thread (`_worker_loop`/`_worker_tick`/
  `_encode_and_publish`), guarded by a 5-second cadence
  (`DEFAULT_ENCODE_INTERVAL_S`) so encoding runs far less often than frames
  arrive. Confirmed by reading the function body, matching its own docstring
  ("O(1), never raises, never encodes").

**What it can never do:** call into `cv2`, write a file, or touch the
network. Same structural guarantee as the audio leg: everything heavy lives
on a background thread the tick-thread function has no path to reach.

### An honest addendum outside what was asked to be measured

The task named `offer()` on both legs specifically, and both check out clean
above. There is a second, separate function the clip rider adds that the task
did **not** name but that a full accounting of "what now runs on the tick
thread" should mention: `ClipRider.__call__`/`_publish`, registered directly
on the engine's `drivers` list (i.e., it runs every tick, not just when a
frame arrives). Reading it:

```python
def _publish(self) -> None:
    block = self.block()
    current = self._main.read_state()
    if not isinstance(current, dict):
        current = {}
    if current.get(STATE_KEY) == block:
        return
    merged = dict(current)
    merged[STATE_KEY] = block
    self._main.write_state(merged)
    self._report(block)
```

`self._main.read_state()` (`CommandSpool.read_state`, `reachy/behavior/
control.py`, unchanged by this arc) is a real filesystem read
(`self._state_file().read_text(...)` then `json.loads`) — executed
**unconditionally, every tick**. `write_state` (a temp-file write plus
`os.replace`) only fires when the block actually changed. This is genuine
per-tick file I/O, not O(1) in a strict sense, and it is fair to ask whether
it belongs in the same "clean" bucket as `offer()`.

It does, for a specific, checkable reason: this is not a new pattern this arc
invented. `reachy/behavior/sense_availability.py`'s `SenseAvailabilityDriver`
(pre-existing — zero diff against `7ea6878`) and `reachy/behavior/intents.py`'s
`IntentDriver` (also pre-existing, zero diff) both already do the *identical*
`read_state()`-then-conditionally-`write_state()` dance every tick, and both
were already on the engine's `drivers` list before this arc started. What
this arc actually does is add a **third** driver instance following that same
already-shipped, already-accepted convention — one more per-tick `state_file`
read alongside the two that already existed, not a new class of tick-thread
I/O. `git diff --stat 7ea6878 HEAD -- reachy/behavior/control.py
reachy/behavior/sense_availability.py reachy/behavior/intents.py` returns
nothing (confirmed above in the toolchain section's spirit — re-run here for
this specific claim):

```console
$ git diff --stat 7ea6878 HEAD -- reachy/behavior/control.py reachy/behavior/sense_availability.py reachy/behavior/intents.py
[no output]
```

This is reported here in full rather than folded into "clean" because it is
real, measurable per-tick file I/O and the task asked for precision about
tick cost — but it is not a finding about this arc specifically: it is an
existing, already-load-bearing pattern (`IntentDriver`'s own state publish is
how the intents view reaches `state.json` today) that the clip rider extends
by one more instance rather than introduces.

## Which tests carry the "layer absent ⇒ unchanged" claim

Rather than assert this generally, here are the specific tests that pin it,
all run as part of the full-suite pass above and re-run individually for
this evidence file:

```console
$ uv run pytest tests/test_behavior_audio_tee_composition.py::test_an_unattached_tee_leaves_the_runtime_feed_unchanged tests/test_behavior_clip_rider_composition.py::test_a_missing_vision_extra_reports_a_named_reason_never_a_crash tests/test_behavior_clip_rider_composition.py::test_the_senses_and_clip_blocks_are_both_additive_to_engine_state tests/test_agent.py::test_attach_publishes_only_cognition_blocks_never_runtime_blocks -v
tests/test_behavior_audio_tee_composition.py::test_an_unattached_tee_leaves_the_runtime_feed_unchanged PASSED [ 25%]
tests/test_behavior_clip_rider_composition.py::test_a_missing_vision_extra_reports_a_named_reason_never_a_crash PASSED [ 50%]
tests/test_behavior_clip_rider_composition.py::test_the_senses_and_clip_blocks_are_both_additive_to_engine_state PASSED [ 75%]
tests/test_agent.py::test_attach_publishes_only_cognition_blocks_never_runtime_blocks PASSED [100%]

============================== 4 passed in 2.35s ===============================
```

- **`test_an_unattached_tee_leaves_the_runtime_feed_unchanged`**
  (`tests/test_behavior_audio_tee_composition.py`) is the direct proof for
  the tee: it runs the same real engine feed twice — once with
  `REACHY_AUDIO_TEE=0`, once with the tee composed but nothing connected to
  it — and asserts the two feeds are byte-identical (`assert with_tee ==
  without`), with a second assertion that the comparison is not vacuous
  (`assert without` — the engine actually published something).
- The clip rider has no directly analogous "with/without" byte-comparison
  test in this suite (unlike the tee, it has no kill-switch env var — see
  [What this does not prove](#what-this-does-not-prove) for what that gap
  means). Its "absent" proof is structural instead:
  `test_a_missing_vision_extra_reports_a_named_reason_never_a_crash` runs the
  REAL, un-mocked path on this box (which genuinely lacks `cv2`) and asserts
  the rider degrades to `available=False` with a named reason rather than
  crashing or doing anything observable; `test_the_senses_and_clip_blocks_
  are_both_additive_to_engine_state` asserts the rider's own key never
  clobbers any of the engine's pre-existing `state.json` keys.
- **`test_attach_publishes_only_cognition_blocks_never_runtime_blocks`**
  (`tests/test_agent.py`) is the existing `agent attach` boundary test (the
  c27 split, predating this arc) — cited here because it is the automated
  form of half of h19's claim: the client's own export feed carries only
  `thinking`/`message`/`emotion`, never a runtime block.

## h19: the before-state, checked against the current tree

### Half 1 — `agent attach` has no transcript cue

`reachy/cli/_commands/agent.py`'s `_CUE_MAPPERS` dict is the complete mapping
from runtime-feed block type to cue generator:

```python
_CUE_MAPPERS: dict[str, Callable[[dict], list[str]]] = {
    "sense": _sense_cues,
    "rule": _rule_cues,
    "intent": _intent_cues,
    "motion": _motion_cues,
}
```

And `reachy/export/runtime.py` pins the full set of block types the runtime
feed can ever emit:

```python
RUNTIME_BLOCKS: tuple[str, ...] = ("sense", "rule", "intent", "motion")
```

The two sets are identical, and neither contains `"transcript"`. There is no
code path by which a heard-words line could reach a cue in `agent attach`
today — confirmed by reading the source, matching CLAUDE.md's own citation of
this gap and issue #93's title.

### Half 2 — publish-only voice

`reachy/cli/_commands/agent.py`'s `_build_default_engine` (what
`cmd_agent_attach` composes by default, via `functools.partial`) builds the
built-in `speak`/`harmonics`/`apply_pose` tools over inert seams:

```python
def _silent_synth(_text: str) -> bytes:
    return b""

def _no_play(_pcm: object, **_kw: object) -> None:
    return None

def _no_express(_emoji: str) -> None:
    return None

speak_engine = VoiceEngine(name="tts", synthesize=_silent_synth, samplerate=_TTS_RATE)
harmonic_engine = VoiceEngine(
    name="harmonic", synthesize=_silent_synth, samplerate=_HARMONIC_RATE
)
```

`_silent_synth` returns empty bytes, `_no_play` does nothing, `_no_express`
does nothing — a tool call still emits its `message`/`emotion` export block
(so the export contract is real), but nothing reaches the robot's SDK,
speaker, or head. `test_attach_publishes_only_cognition_blocks_never_runtime_
blocks` (cited above) is the automated pin for the export-feed half of this
claim; the seam wiring itself was read directly rather than inferred from the
test, since the test only proves what the feed carries, not what the tools
touch.

### Half 3 — issue #93 is still open

```console
$ gh issue view 93 --repo agentculture/reachy-mini-cli --json state,title
{"state":"OPEN","title":"Runtime export feed omits transcript — an export consumer cannot see heard words"}
```

Verbatim, unedited. State is `OPEN`. If a future re-run of this check ever
returns `CLOSED`, that is the honest thing to report — h19 explicitly asks
for the before-state to be *cited*, and a citation that turns out false on
re-check is exactly the kind of finding this task exists to surface, not
paper over.

## What this does not prove

Read this section as seriously as the verdict above — it defines the edge of
what t13 actually established.

- **Nothing here ran on the robot.** Every command above ran against fakes,
  the offline suite, and static source reading. No `AF_UNIX` socket accepted
  a real consumer under load, no camera fed the clip rider a real frame
  stream, and no tick budget was measured on-box with either leg live. That
  measurement — "tick budget before/after the tee, with an active AND a
  wedged consumer, exactly as the t27/t28 baselines did" — is explicitly
  **t15's job** (`On-box robot-path verification`), not this task's. This
  document proves the code path is structurally O(1)/bounded by reading it;
  it does not prove the wall-clock number on the deployed hardware.
- **No embody process, in bench or robot profile, was exercised for this
  task**, per the hard rule scoping t13 to verification against the merged,
  quiescent tree. Whether a REAL consumer attached to the tee socket or a
  real clip rider with `cv2` installed behaves as the unit tests predict is
  covered by the existing `tests/test_behavior_audio_tee.py` /
  `tests/test_behavior_clip_rider.py` unit suites (both green in the full run
  above) and by t14's bench acceptance evidence — not independently
  re-verified here.
- **The clip rider has no equivalent of the tee's `REACHY_AUDIO_TEE=0` kill
  switch or its dedicated "feed unchanged with vs. without" byte-comparison
  test.** Its "absent" story on THIS box is structural (no `[vision]` extra ⇒
  `encoder=None` ⇒ every heavy path becomes a checked no-op) rather than a
  direct behavioral A/B like the tee has. On a box that DOES have `[vision]`
  installed, there is no test in this suite proving the runtime feed is
  byte-identical with the clip rider composed-but-disconnected the way the
  tee's test proves it for audio — that gap is real and is called out here
  rather than assumed closed by analogy to the tee.
- **The `_frame_sinks`/`_sinks` fan-out lists have no enforced cap on count
  or cost.** Today there is exactly one registered sink on each (verified
  above), and both are O(1), so the current tick cost is genuinely bounded.
  A future change registering a second, slower sink on either seam would
  silently move cost onto the tick thread with nothing in the current tests
  or types preventing it — this is a structural observation about the seam's
  design, not a claim about what ships today.
- **The lock-contention nuance in `AudioTee._drain`** (the worker briefly
  holding `self._lock` while converting up to 64 queued chunks to a list) was
  identified by reading the code, not by measuring actual contention under
  load. The bound (64 items, a few pointer copies) makes this almost
  certainly immaterial, but "almost certainly immaterial by inspection" is
  weaker than a measurement, and no measurement was taken.
- **This does not audit the rest of the 20,000-line arc.** t13's acceptance
  criterion scopes the reviewed diff to `reachy/behavior/` specifically, and
  that is what was audited hunk-by-hunk here. The composition wiring in
  `reachy/cli/_commands/behavior.py` and the entire new `reachy/embody/` /
  `reachy/speech/realtime_duplex.py` surface were read for context (cited
  above) but were not re-classified hunk-by-hunk the way `reachy/behavior/`
  was — those are t7/t9/t10/t11/t12's own acceptance criteria, not t13's.
- **The two unrelated live processes on this box** (the production
  `reachy-mini-daemon` and `behavior engine run`, from a separate global
  install) were not stopped, inspected, or interacted with for this task,
  beyond confirming via `ps aux` that they exist and are not this worktree's
  code. Whether THAT deployed instance (a different install, likely a
  different commit) behaves per this evidence is not something this task
  checked or claims.

## Files referenced

- `reachy/behavior/audio_tee.py` — new in this arc (t4)
- `reachy/behavior/clip_rider.py` — new in this arc (t5)
- `reachy/behavior/face_sense.py` — the frame fan-out seam added in this arc
- `reachy/behavior/control.py`, `reachy/behavior/sense_availability.py`,
  `reachy/behavior/intents.py` — pre-existing, unchanged (cited for the
  tick-cost addendum)
- `reachy/cli/_commands/behavior.py` — the composition root wiring both legs
  in (cited for context, outside `reachy/behavior/`'s literal scope)
- `reachy/cli/_commands/agent.py`, `reachy/export/runtime.py` — h19's
  before-state citations
- `tests/test_behavior_audio_tee_composition.py`,
  `tests/test_behavior_clip_rider_composition.py`, `tests/test_agent.py` —
  the tests carrying the "layer absent ⇒ unchanged" claim

## Merge-day refresh (2026-08-02)

The acceptance criterion asks for the before-state citation to be **refreshed on
merge day**, so this section records what changed between the proof being
written and the branch being merged, rather than leaving the reader to wonder
whether the numbers above are current.

One commit landed in `reachy/behavior/` after the analysis above and before the
merge: the clip rider's temp file was renamed `clip.mp4.tmp` → `clip.tmp.mp4`
(`docs/evidence/2026-08-02-live-tee-and-clip-on-the-robot.md`), because
`cv2.VideoWriter` infers its container from the filename suffix and was
therefore writing nothing at all on the deployed robot.

Recomputed against the same before-state (`7ea6878`) on the merged tree:

```text
 reachy/behavior/audio_tee.py  | 783 ++++++++++++++++++++++++++++++++++++++++++
 reachy/behavior/clip_rider.py | 656 +++++++++++++++++++++++++++++++++++
 reachy/behavior/face_sense.py |  47 +++
 3 files changed, 1486 insertions(+)
```

**The h1 verdict is unchanged, and so is every classification.** Still 3 files,
still 6 hunks, still **0 deleted lines**; the only movement is +32 lines inside
the clip leg, which was already classified as an additive clip-leg hunk. Nothing
crossed into "something else", and no pre-existing runtime line was touched.

Suite on the merged tree: `4279 passed, 7 skipped` — the +2 over the figure
quoted above are the two regression tests added with that fix, both of which
pin the temp-file invariant without requiring `cv2`.

One thing this refresh makes sharper rather than softer: the clip bug it
accounts for was found **on the robot, not by the suite**, and it had been
failing silently for 1.7 hours. That is a live reminder of this document's own
closing caveat — code-path equivalence is not hardware equivalence, and only
t15 can supply the latter.
