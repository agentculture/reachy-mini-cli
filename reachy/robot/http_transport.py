"""HTTP transport — drive the Reachy daemon REST API with the stdlib only.

Speaks the daemon's ``/api/...`` routes (default ``http://localhost:8000``)
using :mod:`urllib`, so this flavor adds **no** third-party runtime dependency.
Every failure is mapped to a :class:`CliError` so no traceback ever leaks:

* connection refused / DNS / timeout → environment error (exit 2);
* HTTP 4xx → user error (exit 1); HTTP 5xx → environment error (exit 2).
"""

from __future__ import annotations

import contextlib
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, TargetSink, Transport


def _head_to_si(head: dict[str, float]) -> dict[str, float]:
    """Friendly head offset (mm + degrees) -> the daemon's metres + radians."""
    return {
        "x": head["x"] / 1000.0,
        "y": head["y"] / 1000.0,
        "z": head["z"] / 1000.0,
        "roll": math.radians(head["roll"]),
        "pitch": math.radians(head["pitch"]),
        "yaw": math.radians(head["yaw"]),
    }


class HttpTransport(Transport):
    """Talk to the Reachy daemon over its REST API."""

    name = "http"

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        # Validate via the parsed scheme (not a literal prefix) so the daemon
        # URL is constrained to http/https before it reaches urlopen.
        if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"base URL must use an http or https scheme (got {base_url!r})",
                remediation="pass --base-url as an http(s) URL or set REACHY_BASE_URL",
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # --- core request ----------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> object:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        eff_timeout = timeout if timeout is not None else self.timeout
        try:
            with urllib.request.urlopen(req, timeout=eff_timeout) as resp:  # nosec B310
                raw = resp.read()
        except urllib.error.HTTPError as err:
            raise self._http_error(err) from err
        except OSError as err:
            # urllib.error.URLError is a subclass of OSError (so is a refused
            # connection / DNS failure / timeout).
            raise self._unreachable(err) from err
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as err:
            # json.JSONDecodeError / UnicodeDecodeError both subclass ValueError.
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"daemon returned a non-JSON response from {path}",
                remediation="check the daemon is a compatible reachy-mini-daemon version",
            ) from err

    def _http_error(self, err: urllib.error.HTTPError) -> CliError:
        detail = ""
        try:
            payload = json.loads(err.read())
            if isinstance(payload, dict):
                detail = str(payload.get("detail", ""))
        except Exception:  # noqa: BLE001 - best-effort detail extraction
            detail = ""
        suffix = f": {detail}" if detail else ""
        is_user = 400 <= err.code < 500
        return CliError(
            code=EXIT_USER_ERROR if is_user else EXIT_ENV_ERROR,
            message=f"daemon returned HTTP {err.code}{suffix}",
            remediation=("check the command arguments" if is_user else "check the daemon logs"),
        )

    def _unreachable(self, err: Exception) -> CliError:
        return CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot reach the Reachy daemon at {self.base_url} ({err})",
            remediation=(
                "start it with 'reachy daemon start' (install the daemon: "
                "pip install 'reachy-cli[daemon]'), or set REACHY_BASE_URL / pass --base-url"
            ),
        )

    # --- device ----------------------------------------------------------
    def daemon_status(self) -> object:
        return self._request("GET", "/api/daemon/status")

    def robot_state(self) -> object:
        return self._request("GET", "/api/state/full")

    def doa(self, *, timeout: float | None = None) -> object:
        # Returns {angle, speech_detected} (angle in radians, 0=left/π=right), or a
        # JSON null on a unit with no working mic — read_doa maps both gracefully.
        return self._request("GET", "/api/state/doa", timeout=timeout)

    # --- apps ------------------------------------------------------------
    def apps_list(self) -> object:
        return self._request("GET", "/api/apps/list-available")

    def app_status(self) -> object:
        return self._request("GET", "/api/apps/current-app-status")

    def app_start(self, name: str) -> object:
        quoted = urllib.parse.quote(name, safe="")
        return self._request("POST", f"/api/apps/start-app/{quoted}")

    def app_stop(self) -> object:
        return self._request("POST", "/api/apps/stop-current-app")

    # --- move ------------------------------------------------------------
    def move_goto(
        self,
        *,
        head: dict[str, float] | None = None,
        antennas: tuple[float, float] | None = None,
        body_yaw: float | None = None,
        duration: float,
        interpolation: str,
    ) -> object:
        body: dict[str, object] = {"duration": duration, "interpolation": interpolation}
        if head is not None:
            body["head_pose"] = _head_to_si(head)
        if antennas is not None:
            body["antennas"] = [math.radians(antennas[0]), math.radians(antennas[1])]
        if body_yaw is not None:
            body["body_yaw"] = math.radians(body_yaw)
        return self._request("POST", "/api/move/goto", body)

    def wake(self) -> object:
        return self._request("POST", "/api/move/play/wake_up")

    def sleep(self) -> object:
        return self._request("POST", "/api/move/play/goto_sleep")

    # --- streaming / immediate target ------------------------------------
    def set_target(
        self,
        *,
        head: dict[str, float] | None = None,
        antennas: tuple[float, float] | None = None,
        body_yaw: float | None = None,
    ) -> object:
        # Immediate target: POST /api/move/set_target. The daemon *ignores* this
        # while an interpolated goto/play move is running, so a streaming loop must
        # own motion exclusively (no concurrent 'move goto'/wake while it runs).
        body: dict[str, object] = {}
        if head is not None:
            body["target_head_pose"] = _head_to_si(head)
        if antennas is not None:
            body["target_antennas"] = [math.radians(antennas[0]), math.radians(antennas[1])]
        if body_yaw is not None:
            body["target_body_yaw"] = math.radians(body_yaw)
        return self._request("POST", "/api/move/set_target", body)

    @contextlib.contextmanager
    def streaming(self) -> Iterator[TargetSink]:
        # HTTP is stateless, so there is no session to hold open — the transport is
        # its own sink (it has ``set_target``) and POSTs each tick (one request per
        # pose; fine on loopback). The context manager exists so the engine can use
        # one uniform ``with transport.streaming() as sink`` for both flavors.
        yield self
