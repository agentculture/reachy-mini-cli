#!/usr/bin/env python3
"""Bounded live camera-frame soak for the SDK-canonical camera path.

Runs the repaired camera surface against a REAL Reachy Mini for a bounded window
(default 30 s) and reports how the frames actually flow: total frames, how many
came back ``None`` (no frame ready this instant), the frame shape/dtype, and the
realised FPS. This is the merge-time live check for task t6 — the camera seam in
``reachy/robot/sdk_transport.py`` is unit-tested with fakes, but only a live run
can prove the 1.9.x SDK actually yields non-``None`` frames now that the version
skew (installed SDK vs daemon) is aligned.

WHO RUNS THIS: the main/coordinating agent (or an operator), on the robot, at
merge time. It touches the live media session, so the task-authoring agent does
NOT run it.

It exercises the CANONICAL loop path — one held ``MediaSession`` on one
``ReachyMini`` client (the issue-#51 fix), the same path the live ``listen``
loop uses — not the per-frame ``SdkTransport.get_frame`` throwaway-client path.

Safety:
* Hard-bounded. A ``SIGALRM`` fires at ``duration + margin`` and force-prints the
  partial summary then exits, so even a hung ``get_frame()`` (issue #28) cannot
  make the soak run away.
* Read-only w.r.t. motion — it never commands a move; it only opens the media
  session and reads frames.
* A missing SDK / missing camera surfaces as the clean transport ``CliError``
  (exit 2), not a traceback.

Usage:
    uv run python scripts/camera_soak.py                 # 30 s soak, ~30 Hz poll
    uv run python scripts/camera_soak.py --duration 10   # shorter
    uv run python scripts/camera_soak.py --hz 15         # slower poll
    uv run python scripts/camera_soak.py --json          # machine-readable summary
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import dataclass, field


@dataclass
class SoakStats:
    """Running tally of a soak."""

    frames_total: int = 0
    frames_none: int = 0
    frames_ok: int = 0
    shapes: set = field(default_factory=set)
    dtypes: set = field(default_factory=set)
    started: float = 0.0
    elapsed: float = 0.0

    def as_dict(self) -> dict:
        fps = (self.frames_ok / self.elapsed) if self.elapsed > 0 else 0.0
        return {
            "frames_total": self.frames_total,
            "frames_ok": self.frames_ok,
            "frames_none": self.frames_none,
            "none_ratio": (self.frames_none / self.frames_total) if self.frames_total else 0.0,
            "shapes": sorted(str(s) for s in self.shapes),
            "dtypes": sorted(self.dtypes),
            "elapsed_s": round(self.elapsed, 3),
            "fps": round(fps, 2),
        }


def _print_summary(stats: SoakStats, *, as_json: bool) -> None:
    """Emit the soak summary (stdout)."""
    if as_json:
        print(json.dumps(stats.as_dict()))
        return
    d = stats.as_dict()
    print("")
    print("camera soak summary")
    print("-------------------")
    print(f"  elapsed        : {d['elapsed_s']} s")
    print(f"  frames total   : {d['frames_total']}")
    print(f"  frames OK      : {d['frames_ok']}")
    print(f"  frames None    : {d['frames_none']}  ({d['none_ratio'] * 100:.1f}% None)")
    print(f"  shapes seen    : {', '.join(d['shapes']) or '(none)'}")
    print(f"  dtypes seen    : {', '.join(d['dtypes']) or '(none)'}")
    print(f"  effective FPS  : {d['fps']}  (OK frames / elapsed)")
    if d["frames_ok"] == 0:
        print("")
        print("  WARNING: zero non-None frames — the camera path is still not delivering.")
        print("  Check: daemon up (owns the camera IPC endpoint), SDK/daemon versions")
        print("  aligned (1.9.x), and connection_mode 'localhost_only' on the robot.")


def _run_soak(*, duration: float, hz: float, transport_name: str, as_json: bool) -> int:
    """Open the media session and read frames until the bounded deadline."""
    # Imported lazily so --help works without the SDK extra installed.
    import argparse as _argparse

    from reachy.cli._errors import CliError
    from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, get_transport

    stats = SoakStats()
    interval = (1.0 / hz) if hz > 0 else 0.0

    transport = get_transport(
        _argparse.Namespace(
            transport=transport_name,
            base_url=DEFAULT_BASE_URL,
            timeout=DEFAULT_TIMEOUT,
        )
    )
    session_cm = getattr(transport, "media_session", None)
    if session_cm is None:
        print(
            f"error: transport '{transport_name}' has no media_session (use --transport sdk)",
            file=sys.stderr,
        )
        return 2

    stats.started = time.monotonic()
    deadline = stats.started + duration
    try:
        with session_cm() as session:
            while time.monotonic() < deadline:
                frame = session.get_frame()
                stats.frames_total += 1
                if frame is None:
                    stats.frames_none += 1
                else:
                    stats.frames_ok += 1
                    shape = getattr(frame, "shape", None)
                    dtype = getattr(frame, "dtype", None)
                    if shape is not None:
                        stats.shapes.add(tuple(shape))
                    if dtype is not None:
                        stats.dtypes.add(str(dtype))
                if interval:
                    time.sleep(interval)
    except CliError as err:
        stats.elapsed = time.monotonic() - stats.started
        print(f"error: {err.message}", file=sys.stderr)
        print(f"hint: {err.remediation}", file=sys.stderr)
        return err.code
    finally:
        if stats.elapsed == 0.0:
            stats.elapsed = time.monotonic() - stats.started

    _print_summary(stats, as_json=as_json)
    return 0 if stats.frames_ok > 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded live camera-frame soak (SDK path).")
    parser.add_argument("--duration", type=float, default=30.0, help="soak seconds (default 30)")
    parser.add_argument("--hz", type=float, default=30.0, help="target poll rate Hz (default 30)")
    parser.add_argument("--transport", default="sdk", help="transport name (default sdk)")
    parser.add_argument(
        "--margin", type=float, default=5.0, help="hard-timeout margin s (default 5)"
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON summary")
    args = parser.parse_args(argv)

    # Hard safety net: a SIGALRM at duration + margin guarantees the process
    # cannot outlive the bound even if a get_frame() call hangs (issue #28).
    hard_cap = max(1.0, args.duration + args.margin)

    def _on_timeout(_signum, _frame):  # noqa: ANN001 - signal handler
        print(
            f"\nerror: hard timeout ({hard_cap:.0f}s) reached — get_frame likely hung",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _on_timeout)
        signal.setitimer(signal.ITIMER_REAL, hard_cap)

    try:
        return _run_soak(
            duration=args.duration,
            hz=args.hz,
            transport_name=args.transport,
            as_json=args.json,
        )
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    raise SystemExit(main())
