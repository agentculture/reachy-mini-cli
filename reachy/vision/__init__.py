"""On-board vision for Reachy Mini: low-compute pixel senses + opt-in face ID.

Pixel-based perception (no ML, no GPU) intended to run in realtime on a
Raspberry Pi 4:

* :mod:`~reachy.vision.motion` — motion via frame differencing.
* :mod:`~reachy.vision.light` — light/brightness via centroid of bright regions.
* :mod:`~reachy.vision.producer` — the loop that turns detections into smooth
  head/body orients through the existing serial motion queue (mirrors
  :mod:`reachy.motion` / the ``listen`` subsystem).

Frames come from the local SDK/IPC camera path; this is a *local-profile*
capability (the ``[sdk]``/``[daemon]`` extra), not the pure-HTTP remote profile.

A second, heavier capability lives alongside these pixel detectors — basic
face recognition, ported from ``reachy_nova``:

* :mod:`~reachy.vision.face` — ``FaceEngine``: OpenCV YuNet detection + SFace
  128-dim embedding. ``cv2`` is imported lazily, behind the **new** ``[vision]``
  extra (``opencv-python-headless``) — a bare install never pulls it in.
* :mod:`~reachy.vision.face_store` — ``FaceStore``: cosine-similarity matching
  against a temporary (TTL) and a permanent (persisted) tier of embeddings.
  Pure ``numpy`` + stdlib; importable and testable with no ``cv2`` present.

The live ``listen --live`` wiring rides on top: ``reachy/motion/listen_face.py``
``FaceHook`` feeds the ``feed_face`` cue from the shared frame provider, and
``scripts/face_enroll.py`` is the operator enrollment seam.
"""
