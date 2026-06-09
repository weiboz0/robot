"""Thin client for the Waveshare UGV rover's web API.

The rover runs app.py (Flask) on :5000, which owns the serial port to the
ESP32. We send JSON control commands through its /send_command endpoint as
`base -c {json}` strings instead of opening the serial port ourselves.
"""
import json
import time
import urllib.parse
import urllib.request

ROVER_HOST = "192.168.1.131"
ENDPOINT = f"http://{ROVER_HOST}:5000/send_command"


def _send_json(cmd: dict) -> None:
    """POST one ESP32 JSON command to the running rover app.

    The rover parses `base -c {json}` by splitting on spaces and reading the
    third token, so the JSON must be COMPACT (no spaces) or it won't parse.
    """
    compact = json.dumps(cmd, separators=(",", ":"))
    body = urllib.parse.urlencode({"command": "base -c " + compact}).encode()
    urllib.request.urlopen(ENDPOINT, data=body, timeout=4).read()


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
