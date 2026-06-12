#!/usr/bin/env python3
"""Grab a still photo from the rover's camera.

Two capture paths, tried in order, because the camera can be owned by either:
  1. The ugv_rpi web app (when it's running): pull one JPEG frame from its
     MJPEG stream (/video_feed) over HTTP — no device conflict.
  2. Directly via libcamera (rpicam-still): used when the web app is down and
     the camera device is free (e.g. while rover_joystick / rover_direct own
     the serial port).

Photos land in ./photos next to this file (gitignored).

take_photo() is non-blocking by default: it runs the capture in a background
thread and returns the destination path immediately, so it never freezes a
control loop. Pass wait=True to run synchronously and get "" on failure.
"""
import os
import shutil
import subprocess
import threading
import time
import urllib.request

PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos")
STREAM_HOST = "127.0.0.1"     # web app runs on the same Pi; override per call
STREAM_PORT = 5000


def _tool():
    for t in ("rpicam-still", "libcamera-still"):
        if shutil.which(t):
            return t
    return None


def _grab_from_stream(path: str, url: str, timeout: float) -> bool:
    """Pull one complete JPEG frame from an MJPEG (multipart) stream."""
    deadline = time.time() + timeout
    with urllib.request.urlopen(url, timeout=timeout) as r:
        buf = b""
        while time.time() < deadline:
            chunk = r.read(8192)
            if not chunk:
                break
            buf += chunk
            start = buf.find(b"\xff\xd8")               # JPEG SOI
            end = buf.find(b"\xff\xd9", start + 2)       # JPEG EOI
            if start != -1 and end != -1:
                with open(path, "wb") as f:
                    f.write(buf[start:end + 2])
                return True
    return False


def _capture(path: str, host: str, port: int, timeout: float) -> str:
    """Do the actual capture (blocking). Returns path on success, else ""."""
    os.makedirs(PHOTO_DIR, exist_ok=True)
    # 1) web app stream (camera owned by app.py)
    try:
        if _grab_from_stream(path, f"http://{host}:{port}/video_feed",
                             min(timeout, 5.0)):
            return path
    except Exception:
        pass
    # 2) direct device (camera free, app.py down)
    tool = _tool()
    if tool:
        try:
            subprocess.run([tool, "-n", "-t", "300", "-o", path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return path
        except Exception:
            pass
    return ""


def take_photo(wait: bool = False, host: str = STREAM_HOST,
               port: int = STREAM_PORT, timeout: float = 15.0) -> str:
    """Capture a JPEG into PHOTO_DIR. Returns the destination path.

    Non-blocking by default (capture runs in a daemon thread; path returned
    immediately). With wait=True, blocks and returns "" if capture failed.
    """
    path = os.path.join(PHOTO_DIR, time.strftime("rover_%Y%m%d_%H%M%S.jpg"))
    if wait:
        return _capture(path, host, port, timeout)
    threading.Thread(target=_capture, args=(path, host, port, timeout),
                     daemon=True).start()
    return path


if __name__ == "__main__":
    p = take_photo(wait=True)
    print(f"saved {p}" if p else "capture failed (no stream and no rpicam-still)")
