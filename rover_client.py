"""Thin client for the Waveshare UGV rover's web API.

The rover runs app.py (Flask) on :5000, which owns the serial port to the
ESP32. We send JSON control commands through its /send_command endpoint as
`base -c {json}` strings instead of opening the serial port ourselves.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

ROVER_HOST = "192.168.1.131"
_PORT = 5000
_TIMEOUT = 4.0


def _endpoint() -> str:
    return f"http://{ROVER_HOST}:{_PORT}/send_command"


# Back-compat for callers that import ENDPOINT directly; set_host() keeps it current.
ENDPOINT = _endpoint()


def set_host(host: str) -> None:
    """Point all subsequent commands at a different rover host (e.g. from --host)."""
    global ROVER_HOST, ENDPOINT
    ROVER_HOST = host
    ENDPOINT = _endpoint()


def set_timeout(seconds: float) -> None:
    """Set the default per-request HTTP timeout (the gamepad uses a short one so a
    stuck link can't back commands up)."""
    global _TIMEOUT
    _TIMEOUT = float(seconds)


def _send_json(cmd: dict, timeout: float | None = None) -> None:
    """POST one ESP32 JSON command to the running rover app.

    The rover parses `base -c {json}` by splitting on spaces and reading the
    third token, so the JSON must be COMPACT (no spaces) or it won't parse.
    The URL is rebuilt from ROVER_HOST each call so set_host() takes effect.
    """
    compact = json.dumps(cmd, separators=(",", ":"))
    body = urllib.parse.urlencode({"command": "base -c " + compact}).encode()
    urllib.request.urlopen(_endpoint(), data=body,
                           timeout=_TIMEOUT if timeout is None else timeout).read()


def set_camera(pan: float = 0.0, tilt: float = 0.0) -> None:
    """Aim the gimbal. pan=left/right (-180..180), tilt=up/down (-45..90, + is up)."""
    pan = max(-180.0, min(180.0, pan))
    tilt = max(-45.0, min(90.0, tilt))
    _send_json({"T": 133, "X": pan, "Y": tilt, "SPD": 0, "ACC": 0})


def drive(left: float, right: float, seconds: float = 1.0) -> None:
    """Drive wheels at left/right speed (-0.5..0.5) for `seconds`, then stop."""
    left = max(-0.5, min(0.5, left))
    right = max(-0.5, min(0.5, right))
    seconds = max(0.0, min(5.0, seconds))
    _send_json({"T": 1, "L": left, "R": right})
    time.sleep(seconds)
    _send_json({"T": 1, "L": 0, "R": 0})


def stop() -> None:
    """Stop the wheels immediately."""
    _send_json({"T": 1, "L": 0, "R": 0})


def move(left: float, right: float) -> None:
    """Set wheel speeds continuously (no auto-stop). Use stop() to halt."""
    left = max(-0.5, min(0.5, left))
    right = max(-0.5, min(0.5, right))
    _send_json({"T": 1, "L": left, "R": right})


def lights(front: int = 0, base: int = 0) -> None:
    """LED PWM 0..255. front=IO5 (head), base=IO4 (chassis)."""
    front = max(0, min(255, int(front)))
    base = max(0, min(255, int(base)))
    _send_json({"T": 132, "IO4": base, "IO5": front})


def estop() -> None:
    """Stop wheels and gimbal immediately."""
    _send_json({"T": 1, "L": 0, "R": 0})
    _send_json({"T": 0})


def servo_torque(lock: bool, servo_id: int = 255) -> None:
    """Lock (True) or release (False) bus servos; 255 = all."""
    _send_json({"T": 210, "id": int(servo_id), "cmd": 1 if lock else 0})


def oled(line: int, text: str) -> None:
    """Write text to an OLED line (0-3). NOTE: the rover's `base -c` HTTP path
    splits on spaces, so multi-word text won't survive over HTTP (use serial)."""
    _send_json({"T": 3, "lineNum": int(line), "Text": text})


def oled_default() -> None:
    """Restore the OLED's default status screen."""
    _send_json({"T": -3})
