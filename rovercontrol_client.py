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
    with urllib.request.urlopen(req, timeout=_TIMEOUT if timeout is None else timeout) as r:
        r.read()


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


def light_channel(which: str, on: "bool | None" = None) -> bool:
    """Set or toggle ONE light natively on the controller (it owns the state, so
    this can't drift when the gamepad/web UI also change lights). which is
    'head'|'base'; on=None toggles. Returns the resulting state."""
    if which not in ("head", "base"):
        raise ValueError("which must be 'head' or 'base'")
    url = _base() + "/light_" + which
    if on is not None:
        url += "?on=" + ("1" if on else "0")
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return bool(json.loads(r.read().decode()).get("on"))


def snapshot(timeout: float | None = None) -> str:
    """POST /snapshot → the saved frame's filename (from the response). Returning
    the exact name avoids racing list_photos()[0] against a concurrent gamepad
    snapshot."""
    req = urllib.request.Request(_base() + "/snapshot", method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT if timeout is None else timeout) as r:
        return json.loads(r.read().decode()).get("name", "")


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


_NUDGE_DIRS = ("forward", "back", "left", "right")
_CAM_DIRS = ("up", "down", "left", "right")


def nudge(direction: str, ms: int = 300, timeout: float | None = None) -> None:
    """Bounded wheel nudge — POST /move_<direction>?ms=. The controller auto-stops
    after ms (server-side timer, clamped 0..5000), so motion is bounded even if
    this client dies. Used by the autonomous SafeDriver."""
    if direction not in _NUDGE_DIRS:
        raise ValueError(f"nudge direction must be one of {_NUDGE_DIRS}")
    _post("/move_" + direction, {"ms": max(0, min(5000, int(ms)))}, timeout=timeout)


def camera_nudge(direction: str, deg: float = 15.0, timeout: float | None = None) -> None:
    """Nudge the camera gimbal — POST /camera_<direction>?deg=."""
    if direction not in _CAM_DIRS:
        raise ValueError(f"camera direction must be one of {_CAM_DIRS}")
    _post("/camera_" + direction, {"deg": deg}, timeout=timeout)


def get_stream_frame(timeout: float = 6.0) -> bytes:
    """One JPEG frame from the live MJPEG stream — NOTHING is saved on the
    rover (unlike /snapshot). The autonomous find loop observes with this so a
    run doesn't litter the gallery; only the found photo is snapshotted."""
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(_base() + "/video_feed", timeout=timeout) as r:
        buf = b""
        while time.monotonic() < deadline:
            chunk = r.read(8192)
            if not chunk:
                break
            buf += chunk
            s = buf.find(b"\xff\xd8")                 # JPEG SOI
            e = buf.find(b"\xff\xd9", s + 2)          # JPEG EOI
            if s != -1 and e != -1:
                return buf[s:e + 2]
    raise OSError("no frame from the camera stream")


def set_panorama(jpeg: bytes, timeout: float = 20.0) -> None:
    """Upload the stitched 360° panorama (the scan's '3D space') — POST /panorama.
    The website's 🌐 3D view renders it."""
    req = urllib.request.Request(_base() + "/panorama", data=jpeg, method="POST",
                                 headers={"Content-Type": "image/jpeg"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def set_photo_meta(name: str, meta: dict, timeout: float = 5.0) -> None:
    """Store outline metadata for a photo — POST /photo_meta/<name> with a JSON
    body {target,color,bbox,confidence}. The gallery's ◻ toggle reads it back."""
    req = urllib.request.Request(
        _base() + "/photo_meta/" + urllib.parse.quote(name),
        data=json.dumps(meta).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def get_photo(name: str, timeout: float = 5.0) -> bytes:
    """Fetch a photo's JPEG bytes — GET /photos/<name> (for the vision model)."""
    with urllib.request.urlopen(_base() + "/photos/" + urllib.parse.quote(name),
                                timeout=timeout) as r:
        return r.read()


def healthz(timeout: float = 2.0) -> dict:
    """GET /healthz → parsed JSON (raises on unreachable / bad response)."""
    with urllib.request.urlopen(_base() + "/healthz", timeout=timeout) as r:
        return json.loads(r.read().decode())
