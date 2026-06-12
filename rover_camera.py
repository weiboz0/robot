#!/usr/bin/env python3
"""Grab a still photo from the rover's camera via libcamera (rpicam-still).

Works only when the camera is free — i.e. the ugv_rpi web app is stopped. The
direct tools (rover_joystick / rover_direct / agent_chat) already stop it to
take the serial port, which frees the camera too.

Photos land in ./photos next to this file (gitignored). take_photo() is
fire-and-forget by default so it never freezes a control loop; pass wait=True
to block until the file is written.
"""
import os
import shutil
import subprocess
import time

PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos")


def available() -> bool:
    return _tool() is not None


def _tool():
    for t in ("rpicam-still", "libcamera-still"):
        if shutil.which(t):
            return t
    return None


def take_photo(wait: bool = False, timeout: float = 15.0) -> str:
    """Capture a JPEG into PHOTO_DIR and return its path.

    Returns "" if no capture tool is installed. Non-blocking by default
    (Popen); set wait=True to run synchronously and confirm the file exists.
    """
    tool = _tool()
    if not tool:
        return ""
    os.makedirs(PHOTO_DIR, exist_ok=True)
    path = os.path.join(PHOTO_DIR, time.strftime("rover_%Y%m%d_%H%M%S.jpg"))
    cmd = [tool, "-n", "-t", "300", "-o", path]   # -n no preview, 300ms warmup
    if wait:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout)
    else:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


if __name__ == "__main__":
    p = take_photo(wait=True)
    print(f"saved {p}" if p else "no capture tool (rpicam-still/libcamera-still) found")
