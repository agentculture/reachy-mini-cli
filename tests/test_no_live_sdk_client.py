"""The suite must never construct a real ``reachy_mini.ReachyMini`` (conftest guard).

2026-09-06: a full suite run opened 38 SDK sessions against the deployed Wireless
in 67 s and released its media once per second. See ``tests/conftest.py``'s
``_no_live_sdk_client``.
"""

from __future__ import annotations

import pytest


def test_constructing_the_sdk_client_is_refused_suite_wide():
    reachy_mini = pytest.importorskip("reachy_mini")
    with pytest.raises(ConnectionRefusedError):
        reachy_mini.ReachyMini()
    with pytest.raises(ConnectionRefusedError):
        reachy_mini.ReachyMini(media_backend="no_media")
