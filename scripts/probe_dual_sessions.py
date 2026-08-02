"""h8: the layer's duplex session beside the runtime's transcription session.

Muted by construction (play=None): this proves COEXISTENCE, not conversation.
"""

import os
import time

os.environ["REACHY_EMBODY_MEDIA_PROFILE"] = "robot"
from reachy.embody import media  # noqa: E402
from reachy.speech.realtime_duplex import RealtimeDuplexSession  # noqa: E402

RUN_S = float(os.environ.get("RUN_S", "300"))
src = media.build_media().source
sess = RealtimeDuplexSession(
    read_audio=src.read,
    sample_rate=src.sample_rate,
    play=None,  # muted: no sound, no actuation
    arm_on_connect=False,  # do NOT ask the model to reply — coexistence only
)
sess.start()
t0 = time.monotonic()
last = 0.0
while time.monotonic() - t0 < RUN_S:
    u = sess.take_utterance()
    if u is not None:
        print(f"  [{time.monotonic()-t0:6.1f}s] utterance: {getattr(u, 'text', '?')!r}", flush=True)
    now = time.monotonic() - t0
    if now - last >= 60:
        last = now
        print(
            f"  [{now:6.1f}s] sessions={sess.sessions} chunks_sent={sess.chunks_sent} "
            f"bytes={sess.bytes_sent} utt={sess.utterances} "
            f"connected={sess.connected} down={sess.session_down}",
            flush=True,
        )
    time.sleep(0.02)
print(
    f"FINAL sessions={sess.sessions} connect_failures={sess.connect_failures} "
    f"chunks_sent={sess.chunks_sent} bytes_sent={sess.bytes_sent} "
    f"utterances={sess.utterances} connected={sess.connected} "
    f"session_down={sess.session_down} lane_unavailable={sess.lane_unavailable}"
)
sess.close()
src.close()
