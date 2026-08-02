# Bench-harness reconnaissance — the browser end t14 will converse with

Recorded 2026-08-02, during wave 2 of the `embodiment-layer` build. This is
**reconnaissance, not a probe**: nothing was executed against the harness or a
live session here. Every claim below is sourced from reading checked-out code
at `/home/spark/git/lobes-cli/site/`, plus two unauthenticated HTTP requests to
the local gateway. It exists because t14 (bench acceptance) is the plan's
least-verified task — it depends on a harness this repo does not own — and
finding out in wave 6 that the counterpart does not do what we assumed would be
expensive.

## The harness exists and is the right shape

`site/src/pages/index.astro` describes itself as a

> "Local-only browser harness for driving the lobes /v1/realtime WebSocket
> surface: mic in, live event stream, audio out."

That is precisely the counterpart t14 needs: a second realtime client that
captures microphone audio, holds a `/v1/realtime` session, and plays response
audio back out loud. The three `dev-*` pages (`dev-mic`, `dev-events`,
`dev-connection`) are **fixture-driven** preview pages — `dev-mic.astro` says
its events are synthetic, "driven by synthetic server events" — so t14 must
drive `index.astro`, not the dev pages, to get a genuinely live session.

Supporting pieces that matter for the bench profile: `site/public/worklets/`
(AudioWorklet capture, i.e. the browser's own AEC path — which is what makes
the monitor-speakers + webcam-mic bench arrangement viable at all) and
`site/proxy/` (the dev server proxies the gateway, so the page's relative
`/v1/realtime` resolves).

## Independent cross-validation of the t3 wire

`site/src/components/MicIsland.astro` — the mic capture + playback island —
handles `response.created`, `response.audio.delta` and `response.interrupted`,
and sends `input_audio_buffer.append`. That is the same event family task t3
added to `reachy/speech/realtime_wire.py`, arrived at by a **separate
implementation in a different language against the same gateway**.

Two implementations converging on one event set is meaningfully stronger
evidence than our own tests passing against our own fake server, which can only
prove self-consistency. `site/src/scripts/realtime-connection.ts` is the
reference implementation of the session lifecycle if a future task needs to
check sequencing.

## A named failure mode we did not distinguish

`site/src/scripts/realtime-connection.ts` probes `GET /v1/capabilities` and
reads `stt.feasible`. When that lane is declared off it warns:

```text
stt lane declared off — /v1/realtime will 404 role_infeasible
```

So **HTTP 404 on the realtime handshake is a configuration state, not a
transient outage.** The remedy is an operator switching the STT lane on; no
amount of reconnecting will fix it.

`reachy/speech/realtime.py` currently resolves this to
`handshake-refused (HTTP 404)` (`REASON_HANDSHAKE_REFUSED`, set wherever the
handshake status is not 101) and then backs off and retries on its normal
schedule. The *behaviour* is right — retrying is correct, since the lane may be
switched on mid-run — but the *diagnosis* is misleading: an operator reading
the journal sees a flaky gateway when the actual answer is a switched-off lane.

- Routed to **t9** mid-flight, to be named distinctly in the new duplex client.
- Filed as an issue against the runtime's existing hearing leg, which has the
  same misleading diagnosis and is outside this arc's scope.

Deliberately **not** adopted: a `/v1/capabilities` pre-flight before connecting.
A probe that can itself fail is a second failure surface, and the handshake
already returns the fact we need.

## What this does not establish

- Nothing was run. The harness was read, not started; no session was opened.
- The gateway at `localhost:8001` answers `GET /health` with 200, and both
  `/v1/models` and `/v1/capabilities` answer **401** without a bearer token, so
  the live `stt.feasible` value here is unverified — t14/t15 must read it with
  the configured key.
- Whether the browser's AEC is sufficient against Reachy's speaker in the same
  room is exactly what t14 measures. This note does not predict it.
