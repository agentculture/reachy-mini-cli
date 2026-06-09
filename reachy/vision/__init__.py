"""Rudimentary, low-compute on-board vision for Reachy Mini.

Pixel-based perception (no ML, no GPU) intended to run in realtime on a
Raspberry Pi 4:

* :mod:`~reachy.vision.motion` — motion via frame differencing.
* :mod:`~reachy.vision.light` — light/brightness via centroid of bright regions.
* :mod:`~reachy.vision.producer` — the loop that turns detections into smooth
  head/body orients through the existing serial motion queue (mirrors
  :mod:`reachy.motion` / the ``listen`` subsystem).

Frames come from the local SDK/IPC camera path; this is a *local-profile*
capability (the ``[sdk]``/``[daemon]`` extra), not the pure-HTTP remote profile.
"""
