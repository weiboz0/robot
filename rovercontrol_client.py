"""HTTP client for the Go `rovercontrol` controller's :8080 API.

Mirrors rover_client.py's interface so rover_backend can swap transports. The Go
controller owns the serial + camera and exposes query-param POST endpoints; we
drive it from a remote machine.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

ROVER_HOST = "192.168.1.131"
_PORT = 8080
_TIMEOUT = 4.0


def _base() -> str:
    return f"http://{ROVER_HOST}:{_PORT}"


def set_host(host: str) -> None:
    global ROVER_HOST
    ROVER_HOST = host


def set_timeout(seconds: float) -> None:
    global _TIMEOUT
    _TIMEOUT = float(seconds)


def _post(path: str, params: dict = None, timeout: float | None = None) -> None:
    url = _base() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="POST")
    urllib.request.urlopen(req, timeout=_TIMEOUT if timeout is None else timeout).read()


def _norm(v: float) -> float:
    """Map the chatbot's -0.5..0.5 wheel value to rovercontrol's normalized
    -1..1 /drive input (it then scales by its own speed cap). So 0.5 == "full"
    and the controller's speed-cap governs the absolute top speed."""
    return max(-1.0, min(1.0, float(v) * 2.0))


def move(left: float, right: float) -> None:
    """Continuous drive (chatbot -0.5..0.5, mapped to normalized -1..1). NOTE:
    rovercontrol's /drive is watchdog-managed (~500 ms) — refresh to keep moving
    (see drive())."""
    _post("/drive", {"l": _norm(left), "r": _norm(right)})


def stop() -> None:
    _post("/stop")


def estop() -> None:
    _post("/estop")


def drive(left: float, right: float, seconds: float = 1.0) -> None:
    """Drive for `seconds` then stop. /drive auto-stops after the controller's
    ~500 ms watchdog, so refresh at ~3 Hz for the whole interval, then stop."""
    l, r = _norm(left), _norm(right)
    seconds = max(0.0, min(5.0, seconds))
    deadline = time.monotonic() + seconds
    _post("/drive", {"l": l, "r": r})              # initial
    while time.monotonic() < deadline:
        time.sleep(min(0.3, deadline - time.monotonic()))
        if time.monotonic() < deadline:            # refresh only if still in-window
            _post("/drive", {"l": l, "r": r})
    stop()                                          # never a fresh nonzero after the deadline


def set_camera(pan: float = 0.0, tilt: float = 0.0) -> None:
    pan = max(-180.0, min(180.0, pan))
    tilt = max(-45.0, min(90.0, tilt))
    _post("/camera_aim", {"pan": pan, "tilt": tilt})


def center() -> None:
    _post("/camera_center")


def lights(front: int = 0, base: int = 0) -> None:
    """rovercontrol's /light_*?on= is boolean (omitted=toggle, 0=off, nonzero=on).
    PWM brightness degrades to on/off here. We ALWAYS send on=0|1 so a 0 never
    becomes an accidental toggle."""
    _post("/light_head", {"on": 1 if int(front) > 0 else 0})
    _post("/light_base", {"on": 1 if int(base) > 0 else 0})


def servo_torque(lock: bool, servo_id: int = 255) -> None:
    _post("/gimbal_lock" if lock else "/gimbal_relax")


def snapshot() -> None:
    _post("/snapshot")


def set_speed(cap: float) -> None:
    """Set the controller's speed cap (max wheel magnitude, 0..0.5). The
    controller scales /drive by this and shares it with the gamepad."""
    _post("/speed", {"cap": max(0.0, min(0.5, float(cap)))})


def get_speed(timeout: float = 2.0) -> float:
    """GET /speed → current cap (float)."""
    with urllib.request.urlopen(_base() + "/speed", timeout=timeout) as r:
        return float(json.loads(r.read().decode()).get("cap", 0.0))


def list_photos(timeout: float = 2.0) -> list:
    """GET /photos → list of photo filenames (newest first, as the controller
    orders them)."""
    with urllib.request.urlopen(_base() + "/photos", timeout=timeout) as r:
        return list(json.loads(r.read().decode()).get("photos", []))


def healthz(timeout: float = 2.0) -> dict:
    """GET /healthz → parsed JSON (raises on unreachable / bad response)."""
    with urllib.request.urlopen(_base() + "/healthz", timeout=timeout) as r:
        return json.loads(r.read().decode())
