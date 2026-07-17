#!/usr/bin/env python3
"""Enroll one face into the permanent FaceStore from a live camera frame.

Grabs frames from the SDK camera (one held ``MediaSession`` on one ``ReachyMini``
client — the same canonical path the live ``listen`` loop uses), detects the
largest face with the YuNet+SFace :class:`~reachy.vision.face.FaceEngine`, embeds
it, and enrolls the 128-dim embedding into the
:class:`~reachy.vision.face_store.FaceStore` permanent tier under a name. After
this, the folded :class:`~reachy.motion.listen_face.FaceHook` will recognise that
person live and feed ``saw <name>`` cues to cognition.

This is the operator-facing counterpart to
:meth:`~reachy.motion.listen_face.FaceHook.enroll_from_frame` (the in-process
seam a future agent tool would call). There is deliberately no ``reachy face`` CLI
noun in this task.

WHO RUNS THIS: the operator (or the main/coordinating agent), on the robot, live —
the person to enrol must be in front of the camera. It touches the live media
session and writes the shared FaceStore, so the task-authoring agent does NOT run
it.

Safety:
* Hard-bounded. A ``SIGALRM`` fires at ``duration + margin`` and exits, so a hung
  ``get_frame()`` (issue #28) cannot make the run away.
* Read-only w.r.t. motion — it never commands a move.
* A missing ``[sdk]`` extra (no camera) or ``[vision]`` extra (no opencv)
  surfaces as the clean transport/engine ``CliError`` (exit 2), not a traceback.
* On first run the ~37 MB SFace + YuNet model pair auto-downloads under
  ``state_dir()/models`` (a one-time cost; needs network).

Usage:
    uv run python scripts/face_enroll.py --name Ada
    uv run python scripts/face_enroll.py --name Ada --duration 20 --hz 10
    uv run python scripts/face_enroll.py --name Ada --json
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time


def _run_enroll(
    *, name: str, duration: float, hz: float, transport_name: str, as_json: bool
) -> int:
    """Open the media session, find one face, and enroll it. Returns an exit code."""
    import argparse as _argparse

    from reachy.cli._errors import CliError
    from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, get_transport
    from reachy.vision.face import FaceEngine
    from reachy.vision.face_store import FaceStore

    interval = (1.0 / hz) if hz > 0 else 0.0
    engine = FaceEngine()
    store = FaceStore()
    store.load()

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

    frames_seen = 0
    started = time.monotonic()
    deadline = started + duration
    try:
        with session_cm() as session:
            while time.monotonic() < deadline:
                frame = session.get_frame()
                if frame is not None:
                    frames_seen += 1
                    detection = engine.detect(frame)
                    if detection is not None:
                        face_id = store.enroll(name, detection.embedding)
                        elapsed = round(time.monotonic() - started, 3)
                        if as_json:
                            print(
                                json.dumps(
                                    {
                                        "enrolled": True,
                                        "name": name,
                                        "face_id": face_id,
                                        "frames_seen": frames_seen,
                                        "elapsed_s": elapsed,
                                    }
                                )
                            )
                        else:
                            print(
                                f"enrolled '{name}' as face id {face_id} "
                                f"(after {frames_seen} frame(s), {elapsed}s)"
                            )
                        return 0
                if interval:
                    time.sleep(interval)
    except CliError as err:
        print(f"error: {err.message}", file=sys.stderr)
        print(f"hint: {err.remediation}", file=sys.stderr)
        return err.code

    # No face found within the bounded window.
    if as_json:
        print(json.dumps({"enrolled": False, "name": name, "frames_seen": frames_seen}))
    else:
        print(
            f"no face detected in {duration:.0f}s ({frames_seen} frame(s) seen) — "
            "make sure the person is facing the camera, then retry",
            file=sys.stderr,
        )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enroll one face into the FaceStore (SDK camera).")
    parser.add_argument("--name", required=True, help="the name to enroll this face under")
    parser.add_argument(
        "--duration", type=float, default=15.0, help="max seconds to look for a face (default 15)"
    )
    parser.add_argument("--hz", type=float, default=10.0, help="frame poll rate Hz (default 10)")
    parser.add_argument("--transport", default="sdk", help="transport name (default sdk)")
    parser.add_argument(
        "--margin", type=float, default=5.0, help="hard-timeout margin s (default 5)"
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON result line")
    args = parser.parse_args(argv)

    # Hard safety net: a SIGALRM at duration + margin guarantees the process cannot
    # outlive the bound even if a get_frame() call hangs (issue #28).
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
        return _run_enroll(
            name=args.name,
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
