#!/usr/bin/env python3
"""rovercontrold — the single-file Python controller for the Waveshare UGV rover.

A 1:1 port of the Go controller (rovercontrol.go), stdlib-only. Runs ON the
rover. Owns the hardware directly: the ESP32 serial link (motors / lights /
gimbal), the camera (v4l2-ctl / rpicam-vid MJPEG passthrough — frames are never
re-encoded, so streaming stays fast), a gamepad on /dev/input/js0, and the HTTP
API on :8080 where the URL path IS the command. Serves the same embedded web UI
(rovercontrold_page.PAGE, extracted byte-identically from the Go build).

Safety machinery preserved exactly:
- /move_*?ms nudges auto-stop SERVER-SIDE (threading.Timer) — a crashed client
  never leaves the rover moving.
- continuous /drive is watchdog-stopped after 500ms without a refresh.
- e-stop latches: nonzero motion is refused until a zero/stop command.
- movement generation tokens: a stale nudge timer can't stop a newer command.
- SIGINT/SIGTERM: latch e-stop, close serial, drain the server.

Run:  python3 rovercontrold.py [-port 8080] [-photos DIR] [-serial /dev/ttyAMA0]
      [-gamepad /dev/input/js0] [-camera-mode auto|v4l2|rpicam|off] ...
"""
from __future__ import annotations

import argparse
import collections
import errno
import fcntl
import json
import math
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from rovercontrold_page import PAGE

# ───────────────────────── limits (ported from rover_direct.py) ─────────────

SPEED_LIMIT = 0.5    # max |wheel speed|
PAN_MIN, PAN_MAX = -180.0, 180.0
TILT_MIN, TILT_MAX = -45.0, 90.0    # + is up


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def log(msg):
    print(time.strftime("%Y/%m/%d %H:%M:%S ") + msg, flush=True)


# ───────────────────────────── serial link ─────────────────────────────────

class TTYLink:
    """Write-only ESP32 UART, raw 8N1 @115200 with CLOCAL — configured via
    termios (stdlib; mirrors the Go build's stty approach). Opened O_NONBLOCK so
    open() can't block on carrier, then restored to blocking writes."""

    def __init__(self, path, baud=115200):
        import termios
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            speed = {115200: termios.B115200, 9600: termios.B9600}.get(baud, termios.B115200)
            attrs[0] = 0                                     # iflag: raw
            attrs[1] = 0                                     # oflag: raw
            attrs[2] = (termios.CS8 | termios.CREAD | termios.CLOCAL)  # cflag 8N1
            attrs[3] = 0                                     # lflag: raw
            attrs[4] = speed
            attrs[5] = speed
            attrs[6][termios.VMIN] = 0                       # reads never block…
            attrs[6][termios.VTIME] = 1                      # …longer than 100ms
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)  # blocking writes
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def write(self, data: bytes):
        os.write(self.fd, data)

    def read(self, n=4096):
        return os.read(self.fd, n)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class Rover:
    """Serializes all serial writes (HTTP handlers + joystick share it) and
    encodes the same JSON commands base_ctrl.py uses."""

    def __init__(self):
        self._mu = threading.Lock()
        self._link = None
        self._last_err = ""

    def send(self, cmd: dict):
        line = (json.dumps(cmd, separators=(",", ":"), sort_keys=True) + "\n").encode()
        with self._mu:
            if self._link is None:
                raise OSError("serial unavailable")
            self._link.write(line)

    def ok(self):
        with self._mu:
            return self._link is not None

    def link(self):
        with self._mu:
            return self._link

    def status(self):
        with self._mu:
            return self._link is not None, self._last_err

    def set_status(self, link, err_msg):
        with self._mu:
            self._link, self._last_err = link, err_msg

    def close_link(self):
        with self._mu:
            link = self._link
            self._link, self._last_err = None, "shut down"
        if link is not None:
            link.close()

    def drive(self, left, right):
        self.send({"T": 1, "L": clamp(left, -SPEED_LIMIT, SPEED_LIMIT),
                   "R": clamp(right, -SPEED_LIMIT, SPEED_LIMIT)})

    def stop_wheels(self):
        self.send({"T": 1, "L": 0, "R": 0})

    def estop(self):
        """Halt wheels AND gimbal immediately."""
        try:
            self.send({"T": 1, "L": 0, "R": 0})
        finally:
            self.send({"T": 0})

    def aim_camera(self, pan, tilt):
        p = clamp(pan, PAN_MIN, PAN_MAX)
        t = clamp(tilt, TILT_MIN, TILT_MAX)
        self.send({"T": 133, "X": p, "Y": t, "SPD": 0, "ACC": 0})
        return p, t

    def lights(self, front, base):
        """LED PWM 0..255. front = IO5 (head), base = IO4 (chassis)."""
        cl = lambda v: int(clamp(v, 0, 255))
        self.send({"T": 132, "IO4": cl(base), "IO5": cl(front)})

    def gimbal_torque(self, lock):
        self.send({"T": 210, "id": 255, "cmd": 1 if lock else 0})


def init_link(link):
    """Boot sequence directly on a link before it is published: echo off
    FIRST (so the telemetry reader never sees echoed commands), then
    continuous feedback ON (T:1001 telemetry for pose tracking), then the
    Gimbal module (required for pan/tilt)."""
    for cmd in ({"T": 143, "cmd": 0}, {"T": 131, "cmd": 1}, {"T": 4, "cmd": 2}):
        link.write((json.dumps(cmd, separators=(",", ":"), sort_keys=True) + "\n").encode())


# ─────────────────── telemetry: pose dead reckoning ─────────────────────────
# The ESP32 (ugv_base_ros firmware) streams {"T":1001,...} at ~5 Hz with
# cumulative wheel odometry in CENTIMETERS (odl/odr, signed), battery
# (v = volts×100) and servo-reported gimbal angles. The stream's raw gyro is
# BROKEN in this firmware (at rest gz reads 4..20495, stdev ~9200 — a DMP
# FIFO parse bug), so heading comes from DIFFERENTIAL ODOMETRY instead:
# dθ = (Δodr − Δodl) / track_width. Pose is DISPLAY-ONLY — it never feeds
# motion decisions, and the reader shares no locks with the drive path.

ODOM_SIGN = 1.0             # flip if the live calibration drive shows reversed
HEADING_SIGN = 1.0
TRACK_WIDTH_M = 0.172       # firmware's own constant for this chassis
SKID_FACTOR = 1.0           # effective-width multiplier; skid-steer scrubs in
                            # turns, so calibrate against a real 90°/360° spin
ODOM_MAX_STEP_CM = 50.0     # per-sample plausibility bound: beyond this it's a
                            # counter reset/reboot → re-baseline, never integrate
POSE_FRESH_S = 1.5          # /pose "fresh" horizon
TRAIL_MIN_STEP_M = 0.05     # record a trail point every ≥5 cm of travel
TRAIL_MAX = 2000            # trail hard bound (~100 m at 5 cm spacing)


class Pose:
    """Dead-reckoned x/y (m) + heading (deg, CCW+, 0 = +X at reset) from the
    T:1001 stream, all from the wheel encoders. Signed odometry integrates
    both directions (reverse drives x backwards); a per-sample |Δ| bound
    re-baselines across ESP32 reboots/reconnects instead of teleporting."""

    def __init__(self):
        self._mu = threading.Lock()
        self.x = self.y = 0.0
        self.heading = 0.0
        self._odl = self._odr = None      # cumulative baselines (cm)
        self._seen = None
        self.battery_v = None
        self.servo_pan = self.servo_tilt = None
        # driven-path trail for the Map tab; seeded with the origin so a
        # stationary rover still draws a dot, never an empty map
        self._trail = collections.deque([(0.0, 0.0)], maxlen=TRAIL_MAX)

    def rebaseline(self):
        """Link (re)published or counter jump: hold pose, restart deltas."""
        with self._mu:
            self._odl = self._odr = None

    def reset(self):
        with self._mu:
            self.x = self.y = 0.0
            self.heading = 0.0
            # the old trail is in the old frame — clear and re-seed the origin
            self._trail.clear()
            self._trail.append((0.0, 0.0))

    def update(self, odl, odr, now=None):
        now = time.monotonic() if now is None else now
        with self._mu:
            self._seen = now
            if self._odl is None:
                self._odl, self._odr = odl, odr
                return
            dl, dr = odl - self._odl, odr - self._odr
            self._odl, self._odr = odl, odr
            if abs(dl) > ODOM_MAX_STEP_CM or abs(dr) > ODOM_MAX_STEP_CM:
                return                    # reset/reboot: re-baselined above
            dl, dr = ODOM_SIGN * dl / 100.0, ODOM_SIGN * dr / 100.0   # cm → m
            dth = HEADING_SIGN * math.degrees(
                (dr - dl) / (TRACK_WIDTH_M * SKID_FACTOR))
            if dth:
                self.heading = (self.heading + dth + 180.0) % 360.0 - 180.0
            fwd = (dl + dr) / 2.0
            if fwd:
                rad = math.radians(self.heading)
                self.x += fwd * math.cos(rad)
                self.y += fwd * math.sin(rad)
                lx, ly = self._trail[-1]
                if math.hypot(self.x - lx, self.y - ly) >= TRAIL_MIN_STEP_M:
                    self._trail.append((self.x, self.y))

    def set_aux(self, battery_v, pan, tilt):
        with self._mu:
            if battery_v is not None:
                self.battery_v = battery_v
            if pan is not None:
                self.servo_pan, self.servo_tilt = pan, tilt

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        with self._mu:
            return {"x": round(self.x, 3), "y": round(self.y, 3),
                    "heading": round(self.heading, 1),
                    "battery_v": self.battery_v,
                    "fresh": self._seen is not None and now - self._seen < POSE_FRESH_S}

    def trail_snapshot(self):
        with self._mu:
            return [[round(x, 3), round(y, 3)] for x, y in self._trail]


def parse_feedback(line):
    """One serial line → T:1001 fields, or None (junk / other types)."""
    try:
        d = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("T") != 1001:
        return None
    try:
        return {"odl": float(d["odl"]), "odr": float(d["odr"]),
                "v": float(d["v"]) / 100.0 if "v" in d else None,
                "pan": float(d["pan"]) if "pan" in d else None,
                "tilt": float(d["tilt"]) if "tilt" in d else None}
    except (KeyError, ValueError, TypeError):
        return None


TELEM_BUF_MAX = 65536


def run_telemetry(rover, pose, stop_event):
    """Daemon reader: select() with a timeout so it can never block across
    close_link() at shutdown; re-baselines the pose whenever the link object
    changes (reconnect); junk and partial lines are buffered with a hard cap.
    Touches nothing in the drive path."""
    buf = b""
    last_link = None
    while not stop_event.is_set():
        link = rover.link()
        if link is None:
            last_link = None
            if stop_event.wait(0.3):
                return
            continue
        if link is not last_link:
            pose.rebaseline()
            buf = b""
            last_link = link
        try:
            r, _, _ = select.select([link.fd], [], [], 0.25)
            if not r:
                continue
            chunk = link.read()
        except (OSError, ValueError):
            last_link = None
            if stop_event.wait(0.3):
                return
            continue
        if not chunk:
            continue
        buf += chunk
        if len(buf) > TELEM_BUF_MAX:
            buf = b""                     # flood/garbage guard
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            fb = parse_feedback(raw)
            if fb is None:
                continue
            pose.update(fb["odl"], fb["odr"])
            pose.set_aux(fb["v"], fb["pan"], fb["tilt"])


# ─────────────────── movement arbitration + watchdog ───────────────────────

WATCHDOG_TTL = 0.5   # seconds


class Movement:
    """Single source of truth for wheel motion. Generation tokens stop stale
    nudge timers from cancelling newer commands; a watchdog fed by the internal
    drive path stops continuous motion if its source goes quiet. e-stop latches:
    nonzero motion is REFUSED until a zero command releases it."""

    def __init__(self, rover):
        self._mu = threading.Lock()
        self.r = rover
        self._cap = 0.25
        self._gen = 0
        self._moving = False
        self._deadline = 0.0
        self._estopped = False
        self._l = 0.0                # last ACCEPTED wheel command (nudges too),
        self._r_val = 0.0            # so is_moving() covers in-flight nudges
        self.on_nonzero_drive = None  # fired under _mu — must only set an event
        self.on_estop = None          # fired under _mu — must only set an event

    def _apply_drive(self, left, right, continuous):
        """State decision AND serial write under the lock (r's own lock nests
        inside — consistent order), so a stale stop can never interleave ahead
        of a newer command on the wire. Returns the (new) generation."""
        with self._mu:
            if self._estopped:
                if left == 0 and right == 0:
                    self._estopped = False   # zero command releases the latch
                else:
                    return self._gen         # refused while latched
            self._gen += 1
            gen = self._gen
            self._moving = (left != 0 or right != 0) and continuous
            self._l, self._r_val = left, right
            if continuous:
                self._deadline = time.monotonic() + WATCHDOG_TTL
            try:
                self.r.drive(left, right)
            except OSError:
                pass
            if (left != 0 or right != 0) and self.on_nonzero_drive:
                self.on_nonzero_drive()      # accepted nonzero only
            return gen

    def set_drive(self, left, right):
        return self._apply_drive(left, right, True)

    def drive_cap(self, l, r):
        cap = self.get_cap()
        self._apply_drive(clamp(l, -1, 1) * cap, clamp(r, -1, 1) * cap, True)

    def nudge(self, l, r, seconds):
        """Drive at the cap for `seconds`, then stop — SERVER-SIDE timer, so the
        stop happens even if the client dies. Superseded (generation change)
        nudge timers do nothing."""
        cap = self.get_cap()
        gen = self._apply_drive(clamp(l, -1, 1) * cap, clamp(r, -1, 1) * cap, False)

        def stop_if_current():
            with self._mu:
                if gen == self._gen:
                    self._moving = False
                    self._l = self._r_val = 0.0
                    try:
                        self.r.stop_wheels()
                    except OSError:
                        pass
        t = threading.Timer(seconds, stop_if_current)
        t.daemon = True
        t.start()

    def stop(self):
        with self._mu:
            self._gen += 1
            self._moving = False
            self._l = self._r_val = 0.0
            self._estopped = False
            try:
                self.r.stop_wheels()
            except OSError:
                pass

    def do_estop(self):
        with self._mu:
            self._gen += 1
            self._moving = False
            self._l = self._r_val = 0.0
            self._estopped = True
            try:
                self.r.estop()
            except OSError:
                pass
            if self.on_estop:
                self.on_estop()

    def is_estopped(self):
        with self._mu:
            return self._estopped

    def is_moving(self):
        """True while any accepted wheel command — including an in-flight
        nudge — is nonzero."""
        with self._mu:
            return self._l != 0 or self._r_val != 0

    def set_cap(self, c):
        with self._mu:
            self._cap = clamp(c, 0, SPEED_LIMIT)

    def get_cap(self):
        with self._mu:
            return self._cap

    def watchdog_tick(self, now):
        """Stops the wheels if a continuous lease went stale. Returns True when
        it stopped."""
        with self._mu:
            if not self._moving or now < self._deadline:
                return False
            self._moving = False
            self._l = self._r_val = 0.0
            self._gen += 1
            try:
                self.r.stop_wheels()
            except OSError:
                pass
            return True

    def run_watchdog(self, stop_event):
        while not stop_event.wait(0.1):
            if self.watchdog_tick(time.monotonic()):
                log("watchdog: continuous drive went stale; wheels stopped")


# ─────────────────────── camera: hub + MJPEG splitter ──────────────────────

class Hub:
    """Fans camera frames out to any number of MJPEG clients, latest-frame-wins
    (a slow client skips frames instead of building a backlog)."""

    def __init__(self):
        self._mu = threading.Lock()
        self._subs = {}
        self._latest = None
        self._next_id = 0

    def publish(self, frame: bytes):
        with self._mu:
            self._latest = frame
            for q in self._subs.values():
                if q:                    # latest-wins: drop the stale frame
                    q.clear()
                q.append(frame)
            cvs = list(self._cvs.values()) if hasattr(self, "_cvs") else []
        for cv in cvs:
            with cv:
                cv.notify()

    def latest_frame(self):
        with self._mu:
            return self._latest

    def subscribe(self):
        """Returns (get(timeout)->frame|None, cancel)."""
        cv = threading.Condition()
        with self._mu:
            if not hasattr(self, "_cvs"):
                self._cvs = {}
            sid = self._next_id
            self._next_id += 1
            q = []
            self._subs[sid] = q
            self._cvs[sid] = cv
            if self._latest is not None:   # preload so a fresh client paints now
                q.append(self._latest)

        def get(timeout):
            with cv:
                if not q:
                    cv.wait(timeout)
                with self._mu:
                    return q.pop() if q else None

        def cancel():
            with self._mu:
                self._subs.pop(sid, None)
                self._cvs.pop(sid, None)
        return get, cancel


SOI = b"\xff\xd8"


def split_frames(stream, emit, chunk=65536):
    """Split an MJPEG byte stream SOI-to-next-SOI (deliberately no EOI search —
    EOI bytes can occur inside entropy-coded data)."""
    buf = b""
    while True:
        data = stream.read(chunk)
        if not data:
            return
        buf += data
        while True:
            start = buf.find(SOI)
            if start < 0:
                buf = b""
                break
            nxt = buf.find(SOI, start + 2)
            if nxt < 0:
                buf = buf[start:]
                break
            emit(buf[start:nxt])
            buf = buf[nxt:]


DEFAULT_CAM_W, DEFAULT_CAM_H, DEFAULT_CAM_FPS = 1920, 1080, 15
CAM_STALL_TIMEOUT = 5.0


def resolve_camera_mode(mode, device):
    if mode in ("v4l2", "rpicam", "off"):
        return mode
    if mode == "auto":
        return "v4l2" if os.path.exists(device) else "rpicam"
    log(f'camera: unknown -camera-mode "{mode}"; using rpicam')
    return "rpicam"


def build_camera_cmd(mode, device, w, h, fps):
    if mode == "v4l2":
        fmt_arg = "--set-fmt-video=pixelformat=MJPG"
        if w > 0 and h > 0:
            fmt_arg = f"--set-fmt-video=width={w},height={h},pixelformat=MJPG"
        args = ["v4l2-ctl", "-d", device, fmt_arg]
        if fps > 0:
            args.append(f"--set-parm={fps}")
        args += ["--stream-mmap", "--stream-count=0", "--stream-to=-"]
        return args
    args = ["rpicam-vid", "-n", "-t", "0", "--codec", "mjpeg"]
    if w > 0 and h > 0:
        args += ["--width", str(w), "--height", str(h)]
    if fps > 0:
        args += ["--framerate", str(fps)]
    return args + ["-o", "-"]


class Stalled(Exception):
    pass


class Camera:
    """Capture-process manager with a user-visible status, a frame-staleness
    watchdog (respawns a silent-but-alive producer), an unsized-resolution retry
    for v4l2, and exponential backoff on hard failures."""

    def __init__(self, mode, device, width, height, fps):
        self.mode, self.device = mode, device
        self.width, self.height, self.fps = width, height, fps
        self._mu = threading.Lock()
        self._up = False
        self._last_err = ""
        self._last_frame = time.monotonic()
        self.stall_timeout = CAM_STALL_TIMEOUT

    def status(self):
        with self._mu:
            return self._up, self._last_err

    def set_status(self, up, err):
        with self._mu:
            self._up, self._last_err = up, err

    def run(self, stop_event, hub):
        backoff = 1.0
        logged_fail = False
        unsized_exhausted = False
        while not stop_event.is_set():
            start = time.monotonic()
            err = self._run_once(stop_event, hub, self.width, self.height)
            if stop_event.is_set():
                return
            if (err is not None and not isinstance(err, Stalled)
                    and self.mode == "v4l2" and self.width > 0 and self.height > 0
                    and not unsized_exhausted):
                err2 = self._run_once(stop_event, hub, 0, 0)
                if stop_event.is_set():
                    return
                if err2 is None:
                    err = None
                elif isinstance(err2, Stalled):
                    err = err2
                else:
                    unsized_exhausted = True
                    err = OSError(f"sized: {err}; unsized: {err2}")
            if isinstance(err, Stalled):
                self.set_status(False, "no frames; restarting")
                log(f"camera: {self.mode} stalled (no frames for {self.stall_timeout}s); restarting")
                backoff, logged_fail, unsized_exhausted = 1.0, False, False
                if stop_event.wait(backoff):
                    return
                continue
            if err is None and time.monotonic() - start > 3.0:
                backoff, logged_fail, unsized_exhausted = 1.0, False, False
            else:
                if err is None:
                    err = OSError("camera stream ended immediately")
                self.set_status(False, str(err))
                if not logged_fail:
                    log(f"camera: {self.mode} unavailable ({err}); will keep retrying quietly")
                    logged_fail = True
                if backoff < 15.0:
                    backoff *= 2
            if stop_event.wait(backoff):
                return

    def _run_once(self, stop_event, hub, w, h):
        """Returns None (healthy end), Stalled, or an exception-ish error."""
        args = build_camera_cmd(self.mode, self.device, w, h, self.fps)
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            return e
        log(f"camera: {self.mode} started ({w}x{h}, {self.device})"
            if self.mode == "v4l2" else
            f"camera: rpicam started ({w}x{h}@{self.fps}fps)")

        self._last_frame = time.monotonic()
        stalled = threading.Event()

        def watchdog():
            tick = min(max(self.stall_timeout / 2, 0.05), 1.0)
            while proc.poll() is None and not stop_event.is_set():
                if time.monotonic() - self._last_frame > self.stall_timeout:
                    stalled.set()
                    proc.kill()
                    return
                time.sleep(tick)
        wd = threading.Thread(target=watchdog, daemon=True)
        wd.start()

        def stopper():
            stop_event.wait()
            if proc.poll() is None:
                proc.kill()
        threading.Thread(target=stopper, daemon=True).start()

        first = [True]

        def emit(frame):
            self._last_frame = time.monotonic()
            if first[0]:
                first[0] = False
                self.set_status(True, "")
            hub.publish(frame)
        try:
            split_frames(proc.stdout, emit)
        except (OSError, ValueError):
            pass
        stderr_tail = b""
        try:
            stderr_tail = proc.stderr.read() or b""
            stderr_tail = stderr_tail[-2048:]
        except (OSError, ValueError):
            pass
        rc = proc.wait()
        wd.join(timeout=2)
        if stalled.is_set():
            return Stalled()
        if rc != 0 and not stop_event.is_set():
            tail = stderr_tail.decode(errors="replace").strip().splitlines()
            return OSError(f"exit {rc}: {tail[-1] if tail else ''}")
        return None


class CameraAim:
    """Tracks the current pan/tilt so HTTP nudges and the joystick share one
    absolute aim. The serial write happens under the aim lock (aim→rover lock
    order is consistent) so concurrent nudges can't lose updates."""

    def __init__(self, rover):
        self.r = rover
        self._mu = threading.Lock()
        self.pan, self.tilt = 0.0, 0.0

    def _set_locked(self, pan, tilt):
        try:
            p, t = self.r.aim_camera(pan, tilt)
        except OSError:
            p, t = clamp(pan, PAN_MIN, PAN_MAX), clamp(tilt, TILT_MIN, TILT_MAX)
        self.pan, self.tilt = p, t
        return p, t

    def set(self, pan, tilt):
        with self._mu:
            return self._set_locked(pan, tilt)

    def nudge(self, d_pan, d_tilt):
        with self._mu:
            return self._set_locked(self.pan + d_pan, self.tilt + d_tilt)

    def center(self):
        return self.set(0, 0)

    def get(self):
        with self._mu:
            return self.pan, self.tilt


# ─────────────────────────────── app state ──────────────────────────────────

SAFE_PHOTO_RE = re.compile(r"^[A-Za-z0-9._-]+\.jpg$")
DET_NAME_RE = re.compile(r"^[a-z0-9_]{1,24}$")
SCAN_NAME_RE = re.compile(r"^scan_\d{8}_\d{6}(_\d+)?\.jpg$")
SCAN_BUILD_TIMEOUT_S = 300.0   # stitcher subprocess hard kill
PANO_VARIANT_NAMES = ("seamcut", "projector", "stitcher")  # scene.VARIANT_BUILDERS order
IDENTIFY_TIMEOUT_S = 420.0     # identify subprocess hard kill (scan already safe;
                               # runs AFTER "done" — only the boxes arrive late)
CHAT_PORT = 8090               # agent_chat --serve (loopback; controller proxies)
CHAT_PROXY_TIMEOUT_S = 5.0     # submit/poll are instant server-side — no
                               # controller thread ever waits on an LLM turn


def safe_photo_name(name):
    return (bool(SAFE_PHOTO_RE.match(name)) and not name.startswith(".")
            and "/" not in name and "\\" not in name and ".." not in name)


class App:
    def __init__(self, rover, move, aim, hub, cam, photo_dir):
        self.rover, self.move, self.aim = rover, move, aim
        self.hub, self.cam = hub, cam
        self.photo_dir = photo_dir
        self._light_mu = threading.Lock()
        self.head_on = self.base_on = False
        self._gp_mu = threading.Lock()
        self._gp_up = False
        self._snap_mu = threading.Lock()
        self._snap_seq = 0
        self._pano_mu = threading.Lock()
        self.pano_state, self.pano_state_at = "", None
        self._scan_active = False               # under _pano_mu
        self._scan_published = False            # under _pano_mu — cancel's
                                                # point of no return
        self._scan_cancel = threading.Event()   # set by estop/drive hooks
        self.scan_settle_s = None               # None → scene.SETTLE_S
        self.scan_build_timeout = SCAN_BUILD_TIMEOUT_S
        self.pano_builder = self._build_pano_subprocess
        self.identify_builder = self.identify_cmd   # None disables (tests)
        self._last_archived = None                  # under _pano_mu
        self._scan_pose = None                      # under _pano_mu — pose at
                                                    # scan start (plan 032)
        self._identify_proc = None                  # under _pano_mu
        self._ident_busy = False                    # under _pano_mu — ONE identify
                                                    # at a time (either kind);
                                                    # try-acquire, never wait
        self._chat_mu = threading.Lock()            # /chat_start TOCTOU guard
        self._chat_proc = None                      # under _chat_mu (reaped there)
        # any accepted drive or an e-stop aborts a running scan; the hooks fire
        # under Movement._mu so they only set the event — the scan thread does
        # the actual abort/killpg itself.
        move.on_nonzero_drive = self._scan_cancel.set
        move.on_estop = self._scan_cancel.set
        self.pose = Pose()
        self.scans_dir = os.path.join(photo_dir, "scans")
        self.mapping = None
        self.map_source = "default"

    # lights: read-compute-write AND the hardware write under one lock so
    # concurrent toggles can't disagree with state.
    def update_lights(self, fn):
        with self._light_mu:
            h, b = fn(self.head_on, self.base_on)
            self.head_on, self.base_on = h, b
            try:
                self.rover.lights(255 if h else 0, 255 if b else 0)
            except OSError as e:
                return h, b, e
            return h, b, None

    def set_lights(self, head, base):
        return self.update_lights(lambda h, b: (head, base))[2]

    def toggle_head(self):
        h, _, err = self.update_lights(lambda h, b: (not h, b))
        return h, err

    def toggle_base(self):
        _, b, err = self.update_lights(lambda h, b: (h, not b))
        return b, err

    def gamepad_present(self):
        with self._gp_mu:
            return self._gp_up

    def set_gamepad(self, up):
        with self._gp_mu:
            self._gp_up = up

    def snapshot(self):
        """Write the latest frame, collision-safe via os.link (never overwrites
        a concurrent same-second capture)."""
        frame = self.hub.latest_frame()
        if frame is None:
            raise OSError("no camera frame yet")
        os.makedirs(self.photo_dir, exist_ok=True)
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix=".snap-", suffix=".tmp", dir=self.photo_dir)
        try:
            os.write(fd, frame)
            os.close(fd)
            stamp = time.strftime("rover_%Y%m%d_%H%M%S_")
            with self._snap_mu:
                for _ in range(1000):
                    self._snap_seq += 1
                    name = f"{stamp}{self._snap_seq:03d}.jpg"
                    try:
                        os.link(tmp, os.path.join(self.photo_dir, name))
                        return name
                    except FileExistsError:
                        continue
            raise OSError(f"no free filename for {stamp}*")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def list_photos(self):
        try:
            entries = os.listdir(self.photo_dir)
        except OSError:
            return []
        names = [e for e in entries if safe_photo_name(e)]
        names.sort(reverse=True)
        return names

    def outlined_photos(self):
        try:
            entries = os.listdir(self.photo_dir)
        except OSError:
            return []
        out = []
        for e in entries:
            if e.endswith(".meta.json") and safe_photo_name(e[:-len(".meta.json")]):
                out.append(e[:-len(".meta.json")])
        return out

    # ── 3D scan (gamepad button / POST /scan) ────────────────────────────────
    # Single-flight authority is the private _scan_active flag under _pano_mu
    # (pano_state is externally writable via POST /pano_status — display only).

    def start_scan(self):
        """Kick off a gimbal sweep + out-of-process stitch. One at a time;
        refused while the wheels are moving. Returns (ok, why-not)."""
        if self.move.is_estopped():
            return False, "e-stopped"       # a zero drive/stop releases it
        if self.move.is_moving():
            return False, "wheels are moving"
        with self._pano_mu:
            if self._scan_active:
                return False, "scan already running"
            self._scan_active = True
            self._scan_published = False
            self.pano_state, self.pano_state_at = "scanning", time.monotonic()
            # wheels can't move during a scan, so the start pose IS the pose
            # of the whole scan; only read while the scan slot is held
            snap = self.pose.snapshot()
            self._scan_pose = {k: snap[k] for k in ("x", "y", "heading")}
            self._scan_cancel.clear()
        # estop/drive that slipped in around the clear() (its cancel-event set
        # would have been erased) → re-check both and abort before any motion
        if self.move.is_estopped():
            self._finish_scan(False)
            return False, "e-stopped"
        if self.move.is_moving():
            self._finish_scan(False)
            return False, "wheels are moving"
        with self._pano_mu:                 # a straggling identify from the
            iproc = self._identify_proc     # PREVIOUS scan must not attach
        if iproc is not None:               # its stale meta to this one
            try:
                os.killpg(iproc.pid, signal.SIGKILL)
            except OSError:
                pass
            log("scan: killed the previous scan's identify")
        threading.Thread(target=self._run_scan, daemon=True).start()
        return True, ""

    def _finish_scan(self, ok):
        with self._pano_mu:
            self._scan_active = False
            self.pano_state = "done" if ok else "failed"
            self.pano_state_at = time.monotonic()

    def cancel_scan(self):
        """User-requested abort (⏹ / POST /scan_cancel): same event the
        e-stop/drive hooks set — sweep stops before the next gimbal command,
        the stitcher process group dies, the result is discarded. Atomic
        check+set under _pano_mu; when idle the event is NOT set. A 200 must
        never lie: once _mark_published() won the lock, cancel is refused —
        the result is no longer discardable. Returns whether it cancelled."""
        with self._pano_mu:
            if not self._scan_active or self._scan_published:
                return False
            self._scan_cancel.set()
            return True

    def _mark_published(self):
        """Atomic point-of-no-return vs cancel_scan: True → the canonical
        publish proceeds and any later cancel gets 409; False → a cancel
        already won and the result must be discarded."""
        with self._pano_mu:
            if self._scan_cancel.is_set():
                return False
            self._scan_published = True
            return True

    def _run_scan(self):
        ok = False
        try:
            import scene
            kw = {}
            if self.scan_settle_s is not None:
                kw["settle_s"] = self.scan_settle_s
            client = _ScanClient(self)
            log("scan: gimbal sweep started")
            frames = scene.scan_frames(client, sleep=client.sleep, **kw)
            with self._pano_mu:
                self.pano_state, self.pano_state_at = "stitching", time.monotonic()
            log(f"scan: {len(frames)} frames captured; stitching…")
            built = bool(self.pano_builder(frames))
            with self._pano_mu:
                published = self._scan_published
                # bind the archive name NOW, before _finish_scan releases the
                # scan slot — read later, a back-to-back scan could have
                # overwritten it and this scan's meta would attach to the
                # wrong archive (plan-032 review catch)
                archived = self._last_archived
            # once published, the scan IS done — a later e-stop/drive event
            # (which may land during the minutes-long identify phase) must
            # not flip the state to failed; before publish, a cancel still
            # discards (the builder's _mark_published gate is the authority)
            ok = built and (published or not self._scan_cancel.is_set())
        except ScanCancelled as e:
            log(f"scan: cancelled ({e})")
        except Exception as e:
            log(f"scan: failed ({e})")
        finally:
            self._finish_scan(ok)
            log("scan: 3D view updated" if ok else "scan: not completed")
        if ok and published and self.identify_builder is not None:
            # scan slot is free; identification is best-effort background work
            self._identify_frames(frames, archived)

    def _try_acquire_identify(self):
        with self._pano_mu:
            if self._ident_busy:
                return False
            self._ident_busy = True
            return True

    def _release_identify(self):
        with self._pano_mu:
            self._ident_busy = False

    def _identify_frames(self, frames, archived):
        """Write the frames to a fresh temp dir, run the identify subprocess,
        publish the meta (live + archive sidecar). Every failure just logs.
        SKIPS (never blocks) if another identify holds the flag — the scan is
        already published; the user can press 🔍 later. `archived` is bound by
        the caller while the scan slot was still held (never read from the
        `_last_archived` singleton here — a newer scan may own it by now)."""
        import shutil
        import tempfile
        if not self._try_acquire_identify():
            log("scan: identify busy — this scan gets no boxes; press 🔍 later")
            return
        td = None
        try:                    # EVERYTHING after acquire is inside try —
            # a mkdtemp failure must not wedge the flag
            td = tempfile.mkdtemp(prefix=".identify-", dir=self.photo_dir)
            for pan, tilt, img in frames:
                name = f"pan{int(pan):+04d}_t{int(tilt):+03d}.jpg"
                with open(os.path.join(td, name), "wb") as fh:
                    fh.write(img)
            src_meta = os.path.join(td, "meta.json")
            self._run_identify(td, src_meta)
            if os.path.exists(src_meta):
                if archived:    # carry the archive-time pose into the boxes meta
                    self._inject_pose(src_meta, self._sidecar_pose(archived))
                live_meta = os.path.join(self.photo_dir, "panorama.meta.json")
                sidecar = (os.path.join(self.scans_dir, archived + ".meta.json")
                           if archived else None)
                # commit under _pano_mu with the same "still newest and no
                # scan mid-flight" guard _identify_archived uses — a
                # straggling identify from scan A must never describe scan
                # B's newer panorama (code-review catch). When our archive
                # failed (archived None) there is no name to compare, so
                # publish live only while _last_archived is still None (a
                # later scan would have overwritten it).
                with self._pano_mu:
                    if archived:
                        newest = (archived == (self.list_scans() or [None])[0]
                                  and not self._scan_active)
                    else:
                        newest = (self._last_archived is None
                                  and not self._scan_active)
                    if newest:
                        os.replace(src_meta, live_meta)
                        if sidecar:
                            # the minimal pose sidecar already exists — a
                            # bare link would FileExistsError and strand the
                            # boxless meta, so unlink-then-link (same pattern
                            # as _identify_archived)
                            _quiet(lambda: os.remove(sidecar))
                            os.link(live_meta, sidecar)
                    elif sidecar:
                        os.replace(src_meta, sidecar)
                        log(f"scan: identify of {archived} landed late — "
                            "sidecar only")
        except OSError as e:
            log(f"scan: meta publish failed ({e})")
        finally:
            if td is not None:
                shutil.rmtree(td, ignore_errors=True)
            self._release_identify()

    def identify_pano_cmd(self, pano_path, out_json, focus):
        """argv/cwd for the archived-scan identify (injectable for tests)."""
        scene_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "scene.py")
        argv = ["nice", "-n", "10", sys.executable, scene_py,
                "identify-pano", pano_path, out_json]
        if focus:
            argv.append(focus)
        return argv, dict(os.environ)

    def start_scan_identify(self, name, focus=None):
        """Identify objects in an ARCHIVED scan (plan 031). Returns
        (ok, why-not); the work happens on a daemon worker with the same
        killpg harness as the scan-time identify."""
        pano = os.path.join(self.scans_dir, name)
        if not os.path.exists(pano):
            return False, "no such scan"
        if not self._try_acquire_identify():
            return False, "an identify is already running"
        try:
            threading.Thread(target=self._identify_archived,
                             args=(name, focus), daemon=True).start()
        except Exception as e:          # thread spawn failure must not wedge
            self._release_identify()
            return False, f"could not start identify: {e}"
        return True, ""

    def _identify_archived(self, name, focus):
        import shutil
        import tempfile
        td = None
        try:
            td = tempfile.mkdtemp(prefix=".identpano-", dir=self.photo_dir)
            out = os.path.join(td, "meta.json")
            argv, env = self.identify_pano_cmd(
                os.path.join(self.scans_dir, name), out, focus)
            # cancel_event=None: drive/e-stop must not kill an ARCHIVED
            # identify (the event stays set until the next scan clears it)
            self._run_ident_proc(argv, env, cancel_event=None)
            if not os.path.exists(out):
                log(f"scan: identify of {name} produced no meta")
                return
            sidecar = os.path.join(self.scans_dir, name + ".meta.json")
            # re-identify replaces the sidecar wholesale — carry the original
            # pose stamp over (legacy pose-less sidecars stay pose-less)
            self._inject_pose(out, self._sidecar_pose(name))
            live = os.path.join(self.photo_dir, "panorama.meta.json")
            # commit-time decision in ONE lock hold (plan-review demand): the
            # live meta is refreshed only if this scan is STILL the newest and
            # no scan is mid-flight — an old scan's boxes must never describe
            # a newer panorama
            with self._pano_mu:
                newest = (name == (self.list_scans() or [None])[0]
                          and not self._scan_active)
                try:
                    if newest:
                        os.replace(out, live)
                        _quiet(lambda: os.remove(sidecar))
                        os.link(live, sidecar)
                    else:
                        os.replace(out, sidecar)
                except OSError as e:
                    log(f"scan: identify meta publish failed ({e})")
                    return
            log(f"scan: identify of {name} done"
                + (" (live view updated)" if newest else " (sidecar only)"))
        finally:
            if td is not None:
                shutil.rmtree(td, ignore_errors=True)
            self._release_identify()

    def pano_build_cmd(self, frames_dir, out_path):
        """argv + env for the stitcher subprocess: niced, thread-capped so the
        build gets at most half the Pi's cores. Separate for test pinning.
        The variants dir (= frames_dir, the temp dir) makes the child build
        ALL merge methods; the best becomes out_path (plan 027)."""
        scene_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scene.py")
        env = dict(os.environ)
        for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                  "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            env[k] = "1"
        return (["nice", "-n", "10", sys.executable, scene_py,
                 "build-pano", frames_dir, out_path, frames_dir], env)

    def identify_cmd(self, frames_dir, out_json):
        """argv + env for the OBJECT-IDENTIFICATION subprocess — separate
        from the build so its failure/timeout can never cost a scan."""
        scene_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scene.py")
        return (["nice", "-n", "10", sys.executable, scene_py,
                 "identify", frames_dir, out_json], dict(os.environ))

    def _build_pano_subprocess(self, frames):
        """cv2 runs OUT of process (own process group) so a native crash or
        memory spike can never touch the controller. The group is killed on
        cancel (e-stop / drive input) or timeout. Returns True on success."""
        import tempfile
        os.makedirs(self.photo_dir, exist_ok=True)
        # temp dir inside photo_dir → final os.replace is same-fs atomic
        with tempfile.TemporaryDirectory(prefix=".scan-", dir=self.photo_dir) as td:
            for pan, tilt, img in frames:
                name = f"pan{int(pan):+04d}_t{int(tilt):+03d}.jpg"
                with open(os.path.join(td, name), "wb") as fh:
                    fh.write(img)
            out = os.path.join(td, "panorama.jpg")
            argv, env = self.pano_build_cmd(td, out)
            with open(os.path.join(td, "stderr.txt"), "w+b") as errf:
                try:
                    proc = subprocess.Popen(argv, env=env, start_new_session=True,
                                            stdout=subprocess.DEVNULL, stderr=errf)
                except OSError as e:
                    log(f"scan: stitcher failed to start ({e})")
                    return False
                deadline = time.monotonic() + self.scan_build_timeout
                while proc.poll() is None:
                    if self._scan_cancel.is_set() or time.monotonic() > deadline:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except OSError:
                            pass
                        proc.wait()
                        log("scan: stitcher killed (cancelled or timed out)")
                        return False
                    time.sleep(0.1)
                errf.seek(0)
                tail = errf.read()[-400:].decode("utf-8", "replace").strip()
            if tail:                          # variant successes/failures with
                log(f"scan: builder said: {tail}")   # reasons — always surface
            if proc.returncode != 0:
                log(f"scan: stitcher exited {proc.returncode}")
                return False
            if not self._mark_published():   # cancel landed after proc exit —
                log("scan: cancelled — result discarded, not published")
                return False                  # atomic vs a racing /scan_cancel
            pano = os.path.join(self.photo_dir, "panorama.jpg")
            try:
                os.replace(out, pano)
            except OSError as e:
                log(f"scan: could not publish panorama ({e})")
                return False
            # the OLD meta must never describe the NEW pano — not even for
            # the minutes identification takes; boxes reappear when fresh
            _quiet(lambda: os.remove(
                os.path.join(self.photo_dir, "panorama.meta.json")))
            # debug variants: publish this run's, delete stale ones for
            # methods that failed this run (a button must never show a
            # previous scan's result). Failures here never fail the scan.
            for name in PANO_VARIANT_NAMES:
                var = "pano_var_" + name + ".jpg"
                src = os.path.join(td, var)
                dst = os.path.join(self.photo_dir, var)
                try:
                    if os.path.exists(src):
                        os.replace(src, dst)
                    else:
                        _quiet(lambda: os.remove(dst))
                except OSError as e:
                    log(f"scan: variant {name} publish failed ({e})")
            # object-identification runs AFTER the publish, in its own
            # subprocess with its own timeout — it can only ever cost the
            # meta, never the scan (plan 029 restructure after the live
            # budget measurements). Meta: live copy + archive sidecar (the
            # SAME file via hard-link, so they can't diverge); a scan with no
            # meta deletes the stale live one.
            live_meta = os.path.join(self.photo_dir, "panorama.meta.json")
            src_meta = os.path.join(td, "meta.json")
            archived = None
            try:
                archived = self.archive_scan(pano)   # history copy; failure is
            except OSError as e:                     # not allowed to fail the scan
                log(f"scan: archive failed ({e}) — latest panorama unaffected")
            # identification happens AFTER this returns (in _run_scan, with
            # the scan slot already released — back-to-back scans must not be
            # refused for the minutes the LLM takes); remember where the
            # archive went so the meta sidecar can attach to it
            del live_meta, src_meta
            with self._pano_mu:
                self._last_archived = archived
            return True

    def _run_identify(self, frames_dir, out_json):
        """Scan-time identify: argv from the injectable builder, then the
        shared subprocess harness. Watches _scan_cancel — a drive/e-stop
        during THIS scan's identify phase should kill it."""
        argv, env = self.identify_builder(frames_dir, out_json)
        self._run_ident_proc(argv, env, cancel_event=self._scan_cancel)

    def _run_ident_proc(self, argv, env, cancel_event=None):
        """Popen/watch/kill core shared by BOTH identify paths (own group,
        IDENTIFY_TIMEOUT_S). cancel_event is watched ONLY when given: the
        scan-time path passes _scan_cancel; the ARCHIVED path passes None —
        drive/e-stop hooks set that event and nothing clears it until the
        next scan, so honoring it here would kill every archived identify
        after any joystick input (a code-review catch). The proc handle is
        stashed so a NEW scan still kills a straggling identify of either
        kind (its stale meta must never attach to the newer panorama)."""
        try:
            proc = subprocess.Popen(argv, env=env, start_new_session=True,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            log(f"scan: identify failed to start ({e})")
            return
        with self._pano_mu:
            self._identify_proc = proc
        try:
            deadline = time.monotonic() + IDENTIFY_TIMEOUT_S
            while proc.poll() is None:
                cancelled = cancel_event is not None and cancel_event.is_set()
                if cancelled or time.monotonic() > deadline:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    proc.wait()
                    log("scan: identify killed (cancelled or timed out)")
                    return
                time.sleep(0.2)
        finally:
            with self._pano_mu:
                self._identify_proc = None
        tail = (proc.stderr.read() or b"")[-300:].decode("utf-8", "replace").strip()
        proc.stderr.close()
        if tail:
            log(f"scan: identify said: {tail}")

    def auto_flash_on(self):
        """Kill switch (plan 031): marker file present = the chatbot may NOT
        auto-enable lights. Missing dir counts as marker-absent = ON."""
        return not os.path.exists(
            os.path.join(self.photo_dir, ".auto_flash_off"))

    def set_auto_flash(self, on):
        marker = os.path.join(self.photo_dir, ".auto_flash_off")
        if on:
            _quiet(lambda: os.remove(marker))
        else:
            os.makedirs(self.photo_dir, exist_ok=True)   # may not exist yet
            with open(marker, "w") as f:
                f.write("auto-flash disabled from the web UI\n")

    def chat_cmd(self):
        """argv + cwd for the chat service (separate for test pinning; tests
        substitute a stub so no real chatbot/LLM ever launches in CI)."""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "agent_chat.py")
        return ([sys.executable, script, "--serve", str(CHAT_PORT)],
                os.path.dirname(script))

    def chat_start(self):
        """Spawn the chat service (agent_chat --serve) detached. The lock
        closes the check-then-spawn TOCTOU; the service's own port bind is
        the cross-process mutex (a racing loser exits on EADDRINUSE).
        Returns (ok, why-not)."""
        import urllib.request
        with self._chat_mu:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{CHAT_PORT}/chat_status", timeout=1.5).read()
                return False, "chat service already running"
            except OSError:
                pass
            if self._chat_proc is not None and self._chat_proc.poll() is None:
                # our child is alive but not yet listening (robot detection
                # runs before the port binds — a real multi-second window):
                # spawning again would orphan-overwrite a live service
                return False, "chat service is starting — wait a moment"
            argv, cwd = self.chat_cmd()
            log_path = os.path.expanduser("~/rover-chat.log")
            try:
                with open(log_path, "ab") as logf:
                    self._chat_proc = subprocess.Popen(
                        argv, cwd=cwd, stdout=logf, stderr=logf,
                        stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                return False, f"chat spawn failed: {e}"
            time.sleep(1.2)                         # early-exit reporting
            if self._chat_proc.poll() is not None:
                return False, ("chat service exited at startup — "
                               "see ~/rover-chat.log")
            return True, ""

    def archive_scan(self, src):
        """Hard-link the just-published panorama into photos/scans/ with a
        collision-safe timestamp name, then drop the pose-stamped minimal meta
        sidecar (plan 032) so the map gets a pin even if identify never runs.
        Returns the archived name."""
        os.makedirs(self.scans_dir, exist_ok=True)
        stamp = time.strftime("scan_%Y%m%d_%H%M%S")
        for n in range(1000):
            name = stamp + ("" if n == 0 else f"_{n}") + ".jpg"
            try:
                os.link(src, os.path.join(self.scans_dir, name))
            except FileExistsError:
                continue
            self._write_min_sidecar(name)
            return name
        raise OSError(f"no free scan name for {stamp}*")

    def _write_min_sidecar(self, name):
        """made + zero objects + the scan-start pose. Best-effort: a failure
        costs the pin, never the archive."""
        with self._pano_mu:
            pose = self._scan_pose
        meta = {"made": time.strftime("%Y-%m-%dT%H:%M:%S"), "objects": []}
        if pose:
            meta["pose"] = pose
        sidecar = os.path.join(self.scans_dir, name + ".meta.json")
        try:
            with open(sidecar + ".tmp", "w") as fh:
                json.dump(meta, fh)
            os.replace(sidecar + ".tmp", sidecar)
        except OSError as e:
            log(f"scan: pose sidecar write failed ({e})")

    def _sidecar_pose(self, name):
        """Pose recorded in an archived scan's sidecar, or None. Tolerates a
        missing, unreadable, or corrupt sidecar — a legacy scan simply has no
        pin, never an aborted identify."""
        try:
            with open(os.path.join(self.scans_dir, name + ".meta.json")) as fh:
                pose = json.load(fh).get("pose")
            return pose if isinstance(pose, dict) else None
        except (OSError, ValueError):
            return None

    def _inject_pose(self, meta_path, pose):
        """Merge the pose key into an identify-produced meta file in place
        (temp + replace). No pose or any failure → meta published as-is."""
        if not pose:
            return
        try:
            with open(meta_path) as fh:
                d = json.load(fh)
            d["pose"] = pose
            with open(meta_path + ".tmp", "w") as fh:
                json.dump(d, fh)
            os.replace(meta_path + ".tmp", meta_path)
        except (OSError, ValueError) as e:
            log(f"scan: pose inject failed ({e})")

    def list_scans(self):
        try:
            entries = os.listdir(self.scans_dir)
        except OSError:
            return []
        names = [e for e in entries if SCAN_NAME_RE.match(e)]
        names.sort(reverse=True)
        return names


class ScanCancelled(RuntimeError):
    pass


class _ScanClient:
    """scene.scan_frames client that gates EVERY gimbal command and frame grab
    on the scan-cancel event — after e-stop or a drive input, no further gimbal
    motion can be issued by the scan, including scan_frames' finally-recenter
    (the raise there is swallowed by its except-pass, so the recenter is
    suppressed rather than sent)."""

    def __init__(self, app):
        self.app = app

    def _check(self):
        if self.app._scan_cancel.is_set():
            raise ScanCancelled("cancelled by e-stop or drive input")

    def set_camera(self, pan, tilt):
        self._check()
        self.app.aim.set(pan, tilt)

    def get_stream_frame(self):
        self._check()
        frame = self.app.hub.latest_frame()
        if frame is None:
            raise OSError("no camera frame")
        return frame

    def sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self._check()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.05, left))


# minimal 1x1 grey baseline JPEG served when the camera is down
PLACEHOLDER_FRAME = bytes([
    0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xff, 0xdb, 0x00, 0x43,
    0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
    0x09, 0x08, 0x0a, 0x0c, 0x14, 0x0d, 0x0c, 0x0b, 0x0b, 0x0c, 0x19, 0x12,
    0x13, 0x0f, 0x14, 0x1d, 0x1a, 0x1f, 0x1e, 0x1d, 0x1a, 0x1c, 0x1c, 0x20,
    0x24, 0x2e, 0x27, 0x20, 0x22, 0x2c, 0x23, 0x1c, 0x1c, 0x28, 0x37, 0x29,
    0x2c, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1f, 0x27, 0x39, 0x3d, 0x38, 0x32,
    0x3c, 0x2e, 0x33, 0x34, 0x32, 0xff, 0xc0, 0x00, 0x0b, 0x08, 0x00, 0x01,
    0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xff, 0xc4, 0x00, 0x1f, 0x00, 0x00,
    0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
    0x09, 0x0a, 0x0b, 0xff, 0xc4, 0x00, 0xb5, 0x10, 0x00, 0x02, 0x01, 0x03,
    0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7d,
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
    0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x08,
    0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52, 0xd1, 0xf0, 0x24, 0x33, 0x62, 0x72,
    0x82, 0x09, 0x0a, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44, 0x45,
    0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74, 0x75,
    0x76, 0x77, 0x78, 0x79, 0x7a, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3,
    0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6,
    0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9,
    0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2,
    0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf1, 0xf2, 0xf3, 0xf4,
    0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa, 0xff, 0xda, 0x00, 0x08, 0x01, 0x01,
    0x00, 0x00, 0x3f, 0x00, 0xf7, 0xfa, 0x28, 0xa2, 0x80, 0x3f, 0xff, 0xd9,
])


# ───────────────────────── joystick (raw /dev/input/js0) ────────────────────

JS_EVENT_BUTTON, JS_EVENT_AXIS, JS_EVENT_INIT = 0x01, 0x02, 0x80

DEADZONE = 0.15
TURBO = 0.40
RAMP = 1.2
PAN_RATE, TILT_RATE = 90.0, 70.0
JS_RATE_HZ = 25.0
SPEED_STEPS = [0.15, 0.20, 0.25, 0.30, 0.40]
PRECISION_CAP = 0.15
FINE_NUDGE_DEG = 10.0
CALIB_THRESHOLD = 0.7


def parse_js_event(b: bytes):
    """(value, etype_masked, is_init, number) or None."""
    if len(b) < 8:
        return None
    _, value, etype, number = struct.unpack("<IhBB", b)
    return value, etype & ~JS_EVENT_INIT, bool(etype & JS_EVENT_INIT), number


def _axis_map(index=0, invert=False):
    return {"index": index, "invert": invert}


def _btn(i):
    return {"kind": "button", "index": i}


def _none():
    return {"kind": "none"}


def default_mapping():
    """EXACTLY the historical constants (mirrors the Go defaultMapping)."""
    return {
        "throttle": _axis_map(1, True), "steer": _axis_map(0, False),
        "pan": _axis_map(3, False), "tilt": _axis_map(4, True),
        "turbo": _btn(5), "stop": _btn(0), "snapshot": _btn(1),
        "head_light": _btn(2), "center": _btn(3), "base_light": _btn(4),
        "estop": _btn(6), "relax": _btn(9), "lock": _btn(10),
        "scan": _btn(7), "scan_stop": _btn(8),
        "hat": {"kind": "axis", "axis": _axis_map(7, True)},
        "precision": _none(), "boost": _none(), "panic_stop": _none(),
        "hat_x": {"kind": "none"},
    }


CONTROL_KEYS = ("turbo", "stop", "estop", "head_light", "base_light",
                "center", "snapshot", "relax", "lock", "scan", "scan_stop",
                "precision", "boost", "panic_stop")
AXIS_KEYS = ("throttle", "steer", "pan", "tilt")


def _parse_control(v, default):
    """Ported UnmarshalJSON semantics: null → keep default; bare int → button;
    object → as-is (with axis sub-object defaults)."""
    if v is None:
        return default
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return _btn(int(v))
    if isinstance(v, dict):
        out = {"kind": v.get("kind", ""), "index": int(v.get("index", 0))}
        ax = v.get("axis") or {}
        out["axis"] = _axis_map(int(ax.get("index", 0)), bool(ax.get("invert", False)))
        return out
    raise ValueError(f"bad control value {v!r}")


def _parse_hat(v, default):
    if v is None:
        return default
    if not isinstance(v, dict):
        raise ValueError(f"bad hat value {v!r}")
    ax = v.get("axis") or {}
    return {"kind": v.get("kind", ""),
            "axis": _axis_map(int(ax.get("index", 0)), bool(ax.get("invert", False))),
            "up": int(v.get("up", 0)), "down": int(v.get("down", 0))}


def validate_mapping(m):
    for k in AXIS_KEYS:
        if m[k]["index"] < 0:
            raise ValueError(f"negative stick axis {m[k]['index']}")
    for hk in ("hat", "hat_x"):
        h = m[hk]
        kind = h.get("kind", "")
        if kind == "axis" and h["axis"]["index"] < 0:
            raise ValueError(f"negative hat axis {h['axis']['index']}")
        if kind == "buttons" and (h.get("up", 0) < 0 or h.get("down", 0) < 0):
            raise ValueError("negative hat button index")
        if kind not in ("axis", "buttons", "none", ""):
            raise ValueError(f'invalid hat kind "{kind}"')
    for ck in CONTROL_KEYS:
        c = m[ck]
        kind = c.get("kind", "")
        if kind == "button" and c.get("index", 0) < 0:
            raise ValueError(f"negative button index {c['index']}")
        if kind == "axis" and c.get("axis", {}).get("index", 0) < 0:
            raise ValueError(f"negative axis index {c['axis']['index']}")
        if kind not in ("button", "axis", "none", ""):
            raise ValueError(f'invalid control kind "{kind}"')
    if m["estop"].get("kind") == "none" and m["panic_stop"].get("kind") == "none":
        log("gamepad: WARNING — no e-stop button is bound (both estop and panic_stop are disabled)")
    seen = set()
    for ck in CONTROL_KEYS:
        c = m[ck]
        if c.get("kind") != "button":
            continue
        if c["index"] in seen:
            log(f"gamepad: warning — button {c['index']} is bound to more than one action")
        seen.add(c["index"])


def load_mapping(path):
    """(mapping, source). Missing file → default. Present → merged OVER the
    default (partial JSON keeps defaults) then validated; malformed/invalid
    raises so the caller DISABLES the gamepad."""
    m = default_mapping()
    try:
        raw = open(path, "rb").read()
    except FileNotFoundError:
        return m, "default"
    data = json.loads(raw)          # raises on malformed
    for k in AXIS_KEYS:
        if data.get(k) is not None:
            ax = data[k]
            m[k] = _axis_map(int(ax.get("index", 0)), bool(ax.get("invert", False)))
    for k in CONTROL_KEYS:
        if k in data:
            m[k] = _parse_control(data[k], m[k])
    for k in ("hat", "hat_x"):
        if k in data:
            m[k] = _parse_hat(data[k], m[k])
    validate_mapping(m)             # raises on invalid
    return m, "config"


def axis_signed(state, a):
    v = state.axis(a["index"])
    return -v if a["invert"] else v


def control_held(c, state):
    kind = c.get("kind", "")
    if kind == "button":
        return state.button(c["index"])
    if kind == "axis":
        return axis_signed(state, c["axis"]) > 0.5
    return False


def hat_direction(h, state):
    kind = h.get("kind", "")
    if kind == "axis":
        v = axis_signed(state, h["axis"])
        if v > 0.5:
            return 1
        if v < -0.5:
            return -1
    elif kind == "buttons":
        if state.button(h["up"]):
            return 1
        if state.button(h["down"]):
            return -1
    return 0


def dz(v):
    return 0.0 if -DEADZONE < v < DEADZONE else v


def ramp_toward(cur, tgt, step):
    return cur + clamp(tgt - cur, -step, step)


def drive_mix(throttle, steer, top):
    return clamp(throttle + steer, -1, 1) * top, clamp(throttle - steer, -1, 1) * top


def top_speed(idx, turbo_held, boost_held, precision_held):
    """Precedence: step → turbo → boost → precision LAST (slow mode always wins)."""
    top = SPEED_STEPS[idx]
    if turbo_held:
        top = TURBO
    if boost_held:
        top = SPEED_LIMIT
    if precision_held and top > PRECISION_CAP:
        top = PRECISION_CAP
    return top


class GpPrev:
    def __init__(self):
        self.ctrl = {}
        self.hat = 0
        self.hat_x = 0
        self.panic = False


def compute_joystick(m, state, prev):
    """Pure per-tick decision — 1:1 with the Go computeJoystick."""
    def ctrl_edge(name, c):
        now = control_held(c, state)
        fired = now and not prev.ctrl.get(name, False)
        prev.ctrl[name] = now
        return fired
    a = {}
    a["stop"] = ctrl_edge("stop", m["stop"])
    a["head"] = ctrl_edge("head", m["head_light"])
    a["base"] = ctrl_edge("base", m["base_light"])
    a["snap"] = ctrl_edge("snap", m["snapshot"])
    a["center"] = ctrl_edge("center", m["center"])
    a["relax"] = ctrl_edge("relax", m["relax"])
    a["lock"] = ctrl_edge("lock", m["lock"])
    a["scan"] = ctrl_edge("scan", m["scan"])
    a["scan_stop"] = ctrl_edge("scan_stop", m["scan_stop"])
    a["turbo"] = control_held(m["turbo"], state)
    a["boost"] = control_held(m["boost"], state)
    a["precision"] = control_held(m["precision"], state)
    panic_now = control_held(m["panic_stop"], state)
    panic_edge = panic_now and not prev.panic
    prev.panic = panic_now
    a["estop"] = ctrl_edge("estop", m["estop"]) or panic_edge
    hd = hat_direction(m["hat"], state)
    a["hat_delta"] = hd if (hd != 0 and prev.hat == 0) else 0
    prev.hat = hd
    hx = hat_direction(m["hat_x"], state)
    a["pan_nudge"] = hx if (hx != 0 and prev.hat_x == 0) else 0
    prev.hat_x = hx
    a["throttle"] = dz(axis_signed(state, m["throttle"]))
    a["steer"] = dz(axis_signed(state, m["steer"]))
    a["pan"] = dz(axis_signed(state, m["pan"]))
    a["tilt"] = dz(axis_signed(state, m["tilt"]))
    return a


def drive_gate(tgt_l, tgt_r, cur_l, cur_r, was_active):
    """Idle gamepad goes silent so it doesn't override HTTP /drive; exactly one
    final stop is emitted on the active→idle transition."""
    active = tgt_l != 0 or tgt_r != 0 or cur_l != 0 or cur_r != 0
    return (active or was_active), active


class Gamepad:
    def __init__(self):
        self._mu = threading.Lock()
        self._axes = {}
        self._buttons = {}

    def apply(self, value, etype, number):
        with self._mu:
            if etype == JS_EVENT_AXIS:
                self._axes[number] = value / 32767.0
            elif etype == JS_EVENT_BUTTON:
                self._buttons[number] = value != 0

    def axis(self, n):
        with self._mu:
            return self._axes.get(n, 0.0)

    def button(self, n):
        with self._mu:
            return self._buttons.get(n, False)


def joystick_loop(app, gp, stop_event):
    dt = 1.0 / JS_RATE_HZ
    left = right = 0.0
    was_active = False
    speed_idx = 2
    prev = GpPrev()
    while not stop_event.wait(dt):
        a = compute_joystick(app.mapping, gp, prev)
        if a["center"]:
            app.aim.center()
        if a["stop"]:
            app.move.stop()
        if a["head"]:
            app.toggle_head()
        if a["base"]:
            app.toggle_base()
        if a["snap"]:
            threading.Thread(target=lambda: _quiet(app.snapshot), daemon=True).start()
        if a["estop"]:
            left = right = 0.0
            app.move.do_estop()
        if a["relax"]:
            _quiet(lambda: app.rover.gimbal_torque(False))
        if a["lock"]:
            _quiet(lambda: app.rover.gimbal_torque(True))
        if a["scan"]:
            ok, why = app.start_scan()
            log("scan: started from gamepad" if ok else f"scan: refused ({why})")
        if a["scan_stop"]:
            log("scan: stopped from gamepad" if app.cancel_scan()
                else "scan: stop pressed, nothing to stop")
        if a["hat_delta"]:
            speed_idx = int(clamp(speed_idx + a["hat_delta"], 0, len(SPEED_STEPS) - 1))
            app.move.set_cap(SPEED_STEPS[speed_idx])
        if a["pan_nudge"]:
            app.aim.nudge(a["pan_nudge"] * FINE_NUDGE_DEG, 0)
        top = top_speed(speed_idx, a["turbo"], a["boost"], a["precision"])
        tgt_l, tgt_r = drive_mix(a["throttle"], a["steer"], top)
        step = RAMP * dt
        left = ramp_toward(left, tgt_l, step)
        right = ramp_toward(right, tgt_r, step)
        emit, active = drive_gate(tgt_l, tgt_r, left, right, was_active)
        if emit:
            app.move.set_drive(left, right)
        was_active = active
        if a["pan"] != 0 or a["tilt"] != 0:
            app.aim.nudge(a["pan"] * PAN_RATE * dt, a["tilt"] * TILT_RATE * dt)


def _quiet(fn):
    try:
        fn()
    except Exception:
        pass


def run_gamepad(app, dev_path, stop_event):
    """Reader + control loop; a device read failure (unplug) stops the wheels."""
    try:
        f = open(dev_path, "rb", buffering=0)
    except OSError as e:
        log(f"gamepad: {dev_path} unavailable ({e}); HTTP-only control")
        return
    log(f"gamepad: reading {dev_path}")
    gp = Gamepad()
    app.set_gamepad(True)
    local_stop = threading.Event()

    def reader():
        try:
            while not stop_event.is_set():
                b = f.read(8)
                if not b or len(b) < 8:
                    break
                ev = parse_js_event(b)
                if ev:
                    value, etype, _, number = ev
                    gp.apply(value, etype, number)
        except (OSError, ValueError):
            pass
        finally:
            local_stop.set()
    threading.Thread(target=reader, daemon=True).start()

    combined = threading.Event()

    def watch():
        while not (stop_event.is_set() or local_stop.is_set()):
            time.sleep(0.1)
        combined.set()
    threading.Thread(target=watch, daemon=True).start()
    joystick_loop(app, gp, combined)
    app.set_gamepad(False)
    app.move.stop()
    try:
        f.close()
    except OSError:
        pass


def run_gamepad_debug(path):
    try:
        f = open(path, "rb", buffering=0)
    except OSError as e:
        sys.exit(f"gamepad-debug: {e}")
    log("gamepad-debug: move sticks / press buttons (Ctrl-C to quit)")
    while True:
        b = f.read(8)
        if not b or len(b) < 8:
            return
        ev = parse_js_event(b)
        if ev:
            value, etype, is_init, number = ev
            if not is_init:
                kind = "button" if etype == JS_EVENT_BUTTON else "axis"
                print(f"{kind} #{number} = {value}")


# ── calibration wizard (1:1 with the Go runCalibrate) ───────────────────────

def _read_events(f, timeout):
    """Yield parsed non-init events until `timeout` elapses with no event."""
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            return
        r, _, _ = select.select([f], [], [], remain)
        if not r:
            return
        b = f.read(8)
        if not b or len(b) < 8:
            raise EOFError
        ev = parse_js_event(b)
        if ev and not ev[2]:
            yield ev
            deadline = time.monotonic() + timeout


def _wait_neutral(f):
    for _ in _read_events(f, 0.25):
        pass


def _capture_axis(f, prompt):
    _wait_neutral(f)
    print(f"  {prompt} ... ", end="", flush=True)
    try:
        for value, etype, _, number in _read_events(f, 12.0):
            if etype == JS_EVENT_AXIS:
                v = value / 32767.0
                if abs(v) > CALIB_THRESHOLD:
                    inv = v < 0
                    print(f"axis {number}{' (inverted)' if inv else ''}")
                    return _axis_map(number, inv)
    except EOFError:
        print("(device closed)")
        return None
    print("(skipped, kept default)")
    return None


def _capture_button(f, prompt):
    _wait_neutral(f)
    if prompt:
        print(f"  {prompt} ... ", end="", flush=True)
    try:
        for value, etype, _, number in _read_events(f, 12.0):
            if etype == JS_EVENT_BUTTON and value != 0:
                print(f"button {number}")
                return number
    except EOFError:
        print("(device closed)")
        return None
    print("(skipped, kept default)")
    return None


def _capture_hat(f, pos_prompt, label):
    _wait_neutral(f)
    print(f"  {pos_prompt} (or wait to skip the {label} D-pad) ... ", end="", flush=True)
    try:
        for value, etype, _, number in _read_events(f, 10.0):
            if etype == JS_EVENT_AXIS:
                v = value / 32767.0
                if abs(v) > CALIB_THRESHOLD:
                    print(f"axis {number}")
                    return {"kind": "axis", "axis": _axis_map(number, v < 0)}
            if etype == JS_EVENT_BUTTON and value != 0:
                print(f"button {number}")
                neg = _capture_button(f, "Now press the OPPOSITE D-pad direction")
                if neg is not None:
                    return {"kind": "buttons", "up": number, "down": neg}
                return {"kind": "none"}
    except EOFError:
        return {"kind": "none"}
    print("(skipped)")
    return {"kind": "none"}


def _capture_control(f, prompt):
    _wait_neutral(f)
    print(f"  {prompt} (or wait to skip) ... ", end="", flush=True)
    try:
        for value, etype, _, number in _read_events(f, 12.0):
            if etype == JS_EVENT_AXIS:
                v = value / 32767.0
                if abs(v) > CALIB_THRESHOLD:
                    print(f"axis {number}{' (inverted)' if v < 0 else ''}")
                    return {"kind": "axis", "index": 0, "axis": _axis_map(number, v < 0)}
            if etype == JS_EVENT_BUTTON and value != 0:
                print(f"button {number}")
                return _btn(number)
    except EOFError:
        print("(device closed)")
        return None
    print("(skipped, stays disabled)")
    return None


def run_calibrate(js_path, map_path):
    f = open(js_path, "rb", buffering=0)
    print("Gamepad calibration — follow each prompt. A step auto-skips")
    print("(keeps the default) after ~12s if you don't move that control.\n")
    m = default_mapping()
    for key, prompt in (("throttle", "Push LEFT stick UP (forward)"),
                        ("steer", "Push LEFT stick RIGHT"),
                        ("pan", "Push RIGHT stick RIGHT (camera pan right)"),
                        ("tilt", "Push RIGHT stick UP (camera tilt up)")):
        a = _capture_axis(f, prompt)
        if a is not None:
            m[key] = a
    for key, prompt in (("turbo", "Press/hold TURBO (higher top speed)"),
                        ("stop", "Press STOP wheels"),
                        ("estop", "Press EMERGENCY STOP"),
                        ("head_light", "Press HEAD light toggle"),
                        ("base_light", "Press BASE light toggle"),
                        ("center", "Press CENTER camera"),
                        ("snapshot", "Press SNAPSHOT"),
                        ("relax", "Press RELAX gimbal (e.g. L2)"),
                        ("lock", "Press LOCK gimbal (e.g. R2)"),
                        ("scan", "Press 3D-SCAN (starts a room panorama scan)"),
                        ("scan_stop", "Press STOP-SCAN (aborts + discards a running scan)")):
        c = _capture_control(f, prompt)
        m[key] = c if c is not None else _none()   # skip → disabled
    m["hat"] = _capture_hat(f, "Press D-pad UP", "speed cap")
    for key, prompt in (("precision", "Hold the PRECISION (slow) trigger/button"),
                        ("boost", "Hold the BOOST (fast) trigger/button"),
                        ("panic_stop", "Press your INSTANT-STOP (panic) button")):
        c = _capture_control(f, prompt)
        if c is not None:
            m[key] = c
    m["hat_x"] = _capture_hat(f, "Press D-pad RIGHT", "camera pan")
    if m["estop"].get("kind") == "none" and m["panic_stop"].get("kind") == "none":
        print("  ⚠️  WARNING: you bound NO e-stop button — there will be no panic stop!")
    tmp = map_path + ".tmp"
    with open(tmp, "w") as out:
        json.dump(m, out, indent=2)
        out.write("\n")
    os.rename(tmp, map_path)
    print(f"\nWrote mapping to {map_path} — restart rovercontrold to use it.")
    f.close()


# ───────────────────────────── HTTP layer ──────────────────────────────────

STREAM_WRITE_TIMEOUT = 5.0
STREAM_SEND_BUFFER = 256 << 10
NUDGE_DEFAULT_MS = 400


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "rovercontrold"

        # quiet the default per-request stderr logging
        def log_message(self, fmt, *args):
            pass

        # ---- helpers ----
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
            self.send_header("Access-Control-Allow-Headers", "*")

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _err(self, code, msg):
            self._json(code, {"ok": False, "error": msg})

        def _fparam(self, q, name, default):
            """floatParam: absent → default; malformed / NaN / Inf → ValueError."""
            vals = q.get(name)
            if not vals or vals[0] == "":
                return default
            try:
                v = float(vals[0])
            except ValueError:
                raise ValueError(f'bad {name}="{vals[0]}"')
            if math.isnan(v) or math.isinf(v):
                raise ValueError(f'bad {name}="{vals[0]}"')
            return v

        def _require_serial(self):
            if not app.rover.ok():
                self._err(503, "serial unavailable")
                return False
            return True

        def _read_body(self, limit):
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                self.close_connection = True   # unread body would desync keep-alive
                return None
            return self.rfile.read(n) if n else b""

        def _chat_proxy(self, method, path_q, body=None):
            """Pass a request through to the loopback chat service. Every call
            is a ≤5 s submit/poll — never a whole LLM turn (plan 030). Returns
            False when the service is unreachable (caller picks the shape)."""
            import urllib.request
            import urllib.error
            req = urllib.request.Request(
                f"http://127.0.0.1:{CHAT_PORT}{path_q}", data=body,
                method=method, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=CHAT_PROXY_TIMEOUT_S) as r:
                    self._raw_json(r.status, r.read())
                    return True
            except urllib.error.HTTPError as e:      # 4xx pass through
                self._raw_json(e.code, e.read())
                return True
            except OSError:
                return False

        def _raw_json(self, code, data):
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_file(self, path, ctype):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                self._err(404, "not found")
                return
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        # ---- dispatch ----
        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            if p == "/":
                body = PAGE.encode()
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            elif p == "/healthz":
                cam_up, cam_err = app.cam.status()
                ser_up, ser_err = app.rover.status()
                with app._light_mu:
                    lights = {"head": app.head_on, "base": app.base_on}
                self._json(200, {"ok": True,
                                 "serial": {"up": ser_up, "err": ser_err},
                                 "camera": {"up": cam_up, "err": cam_err},
                                 "gamepad": {"up": app.gamepad_present(),
                                             "mapping": app.map_source},
                                 "lights": lights})
            elif p == "/speed":
                self._json(200, {"ok": True, "cap": app.move.get_cap()})
            elif p == "/video_feed":
                self._video_feed()
            elif p == "/latest":
                photos = app.list_photos()
                self._json(200, {"count": len(photos),
                                 "latest": photos[0] if photos else None,
                                 "outlined": len(app.outlined_photos())})
            elif p == "/pose":
                # ungated on purpose: last-known pose + fresh:false is the
                # useful answer when serial is down (badge greys out)
                snap = app.pose.snapshot()
                pan, tilt = app.aim.get()
                snap.update({"ok": True, "pan": pan, "tilt": tilt})
                self._json(200, snap)
            elif p == "/pose_trail":
                # one fetch per map tick: the driven trail + the same pose
                # dict /pose serves
                snap = app.pose.snapshot()
                pan, tilt = app.aim.get()
                snap.update({"ok": True, "pan": pan, "tilt": tilt})
                self._json(200, {"trail": app.pose.trail_snapshot(),
                                 "pose": snap})
            elif p == "/auto_flash":
                self._json(200, {"on": app.auto_flash_on()})
            elif p == "/chat_status":
                if not self._chat_proxy("GET", "/chat_status"):
                    self._json(200, {"up": False})    # down is an answer
            elif p == "/chat_poll":
                if not self._chat_proxy("GET", "/chat_poll?" + u.query):
                    self._err(503, "chat service not running")
            elif p == "/pano_meta":
                self._serve_file(os.path.join(app.photo_dir, "panorama.meta.json"),
                                 "application/json")
            elif p.startswith("/scan_meta/"):
                name = p[len("/scan_meta/"):]
                if not SCAN_NAME_RE.match(name):
                    self._err(400, "bad scan name")
                    return
                self._serve_file(os.path.join(app.scans_dir, name + ".meta.json"),
                                 "application/json")
            elif p == "/scans":
                self._json(200, {"scans": app.list_scans()})
            elif p.startswith("/scans/"):
                name = p[len("/scans/"):]
                if not SCAN_NAME_RE.match(name):
                    self._err(400, "bad scan name")
                    return
                self._serve_file(os.path.join(app.scans_dir, name), "image/jpeg")
            elif p == "/photos":
                self._json(200, {"photos": app.list_photos(),
                                 "outlined": app.outlined_photos()})
            elif p.startswith("/photos/"):
                name = p[len("/photos/"):]
                if not safe_photo_name(name):
                    self._err(400, "bad photo name")
                    return
                self._serve_file(os.path.join(app.photo_dir, name), "image/jpeg")
            elif p.startswith("/photo_meta/"):
                name = p[len("/photo_meta/"):]
                if not safe_photo_name(name):
                    self._err(400, "bad photo name")
                    return
                mp = os.path.join(app.photo_dir, name + ".meta.json")
                if not os.path.exists(mp):
                    self._err(404, "no meta")
                    return
                self._serve_file(mp, "application/json")
            elif p == "/panorama":
                fp = os.path.join(app.photo_dir, "panorama.jpg")
                if not os.path.exists(fp):
                    self._err(404, "no panorama yet — run a scene scan")
                    return
                self._serve_file(fp, "image/jpeg")
            elif p == "/pano_status":
                with app._pano_mu:
                    st, at = app.pano_state, app.pano_state_at
                age = -1.0 if at is None else time.monotonic() - at
                self._json(200, {"state": st, "age_s": age})
            elif p.startswith("/pano_variant/"):
                name = p[len("/pano_variant/"):]
                if not DET_NAME_RE.match(name):
                    self._err(400, "bad variant name")
                    return
                fp = os.path.join(app.photo_dir, "pano_var_" + name + ".jpg")
                if not os.path.exists(fp):
                    self._err(404, "no such variant yet — run $panotest in the chatbot")
                    return
                self._serve_file(fp, "image/jpeg")
            elif p.startswith("/det_image/"):
                name = p[len("/det_image/"):]
                if not DET_NAME_RE.match(name):
                    self._err(400, "bad model name")
                    return
                fp = os.path.join(app.photo_dir, "det_" + name + ".jpg")
                if not os.path.exists(fp):
                    self._err(404, "no result for this model yet — run $detect in the chatbot")
                    return
                self._serve_file(fp, "image/jpeg")
            elif p == "/tour_feed":
                self._tour_feed(q)
            else:
                self._err(404, "unknown path")

        def do_POST(self):
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            try:
                if p in ("/move_forward", "/move_back", "/move_left", "/move_right"):
                    l, r = {"/move_forward": (1, 1), "/move_back": (-1, -1),
                            "/move_left": (-1, 1), "/move_right": (1, -1)}[p]
                    try:
                        ms = self._fparam(q, "ms", NUDGE_DEFAULT_MS)
                    except ValueError as e:
                        self._err(400, str(e))
                        return
                    if not self._require_serial():
                        return
                    app.move.nudge(l, r, clamp(ms, 0, 5000) / 1000.0)
                    self._json(200, {"ok": True})
                elif p == "/stop":
                    if not self._require_serial():
                        return
                    app.move.stop()
                    self._json(200, {"ok": True})
                elif p == "/estop":
                    if not self._require_serial():
                        return
                    app.move.do_estop()
                    self._json(200, {"ok": True})
                elif p == "/drive":
                    try:
                        l = self._fparam(q, "l", 0)
                        r = self._fparam(q, "r", 0)
                    except ValueError as e:
                        self._err(400, str(e))
                        return
                    if not self._require_serial():
                        return
                    app.move.drive_cap(l, r)
                    self._json(200, {"ok": True})
                elif p in ("/camera_up", "/camera_down", "/camera_left", "/camera_right"):
                    dp, dt = {"/camera_up": (0, 1), "/camera_down": (0, -1),
                              "/camera_left": (-1, 0), "/camera_right": (1, 0)}[p]
                    try:
                        deg = self._fparam(q, "deg", 15)
                    except ValueError as e:
                        self._err(400, str(e))
                        return
                    if not self._require_serial():
                        return
                    pan, tilt = app.aim.nudge(dp * deg, dt * deg)
                    self._json(200, {"ok": True, "pan": pan, "tilt": tilt})
                elif p == "/camera_center":
                    if not self._require_serial():
                        return
                    pan, tilt = app.aim.center()
                    self._json(200, {"ok": True, "pan": pan, "tilt": tilt})
                elif p == "/camera_aim":
                    try:
                        pan = self._fparam(q, "pan", 0)
                        tilt = self._fparam(q, "tilt", 0)
                    except ValueError as e:
                        self._err(400, str(e))
                        return
                    if not self._require_serial():
                        return
                    pan, tilt = app.aim.set(pan, tilt)
                    self._json(200, {"ok": True, "pan": pan, "tilt": tilt})
                elif p in ("/light_head", "/light_base"):
                    which = "head" if p == "/light_head" else "base"
                    try:
                        on = self._fparam(q, "on", -1)
                    except ValueError as e:
                        self._err(400, str(e))
                        return
                    if not self._require_serial():
                        return
                    if on < 0:
                        state, err = (app.toggle_head() if which == "head"
                                      else app.toggle_base())
                    else:
                        state = on != 0
                        if which == "head":
                            err = app.set_lights(state, app.base_on)
                        else:
                            err = app.set_lights(app.head_on, state)
                    if err is not None:
                        self._err(500, str(err))
                        return
                    self._json(200, {"ok": True, "on": state})
                elif p == "/gimbal_relax":
                    if not self._require_serial():
                        return
                    _quiet(lambda: app.rover.gimbal_torque(False))
                    self._json(200, {"ok": True})
                elif p == "/gimbal_lock":
                    if not self._require_serial():
                        return
                    _quiet(lambda: app.rover.gimbal_torque(True))
                    self._json(200, {"ok": True})
                elif p == "/speed":
                    try:
                        cap = self._fparam(q, "cap", 0.25)
                    except ValueError as e:
                        self._err(400, str(e))
                        return
                    app.move.set_cap(cap)
                    self._json(200, {"ok": True, "cap": app.move.get_cap()})
                elif p == "/snapshot":
                    try:
                        name = app.snapshot()
                    except OSError as e:
                        self._err(503, str(e))
                        return
                    self._json(200, {"ok": True, "name": name})
                elif p == "/scan":
                    if not self._require_serial():
                        return
                    ok, why = app.start_scan()
                    if not ok:
                        self._err(409, why)
                        return
                    self._json(200, {"ok": True, "state": "scanning"})
                elif p == "/scan_cancel":
                    # ungated: touches no hardware, only aborts+discards
                    if not app.cancel_scan():
                        self._err(409, "no scan running (or already publishing)")
                        return
                    self._json(200, {"ok": True})
                elif p.startswith("/delete_photo/"):
                    name = p[len("/delete_photo/"):]
                    if not safe_photo_name(name):
                        self._err(400, "bad photo name")
                        return
                    _quiet(lambda: os.remove(os.path.join(app.photo_dir, name)))
                    _quiet(lambda: os.remove(os.path.join(app.photo_dir, name + ".meta.json")))
                    self._json(200, {"ok": True})
                elif p == "/pose_reset":
                    app.pose.reset()
                    self._json(200, {"ok": True})
                elif p == "/chat":
                    b = self._read_body(64 << 10)
                    if b is None:
                        self._err(400, "chat body too large")
                        return
                    if not self._chat_proxy("POST", "/chat", b):
                        self._err(503, "chat service not running — press start")
                elif p == "/auto_flash":
                    v = (q.get("on") or ["1"])[0]
                    app.set_auto_flash(v not in ("0", "false", "off"))
                    self._json(200, {"ok": True, "on": app.auto_flash_on()})
                elif p.startswith("/scan_identify/"):
                    name = p[len("/scan_identify/"):]
                    if not SCAN_NAME_RE.match(name):
                        self._err(400, "bad scan name")
                        return
                    focus = (q.get("focus") or [""])[0][:100] or None
                    ok, why = app.start_scan_identify(name, focus)
                    if not ok:
                        self._err(409 if "running" in why else 404, why)
                        return
                    self.send_response(202)
                    self._cors()
                    body = json.dumps({"ok": True, "started": True}).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif p == "/chat_start":
                    ok, why = app.chat_start()
                    if ok:
                        self._json(200, {"ok": True})
                    else:
                        self._err(409, why)
                elif p.startswith("/delete_scan/"):
                    name = p[len("/delete_scan/"):]
                    if not SCAN_NAME_RE.match(name):
                        self._err(400, "bad scan name")
                        return
                    _quiet(lambda: os.remove(os.path.join(app.scans_dir, name)))
                    _quiet(lambda: os.remove(
                        os.path.join(app.scans_dir, name + ".meta.json")))
                    self._json(200, {"ok": True})
                elif p.startswith("/photo_meta/"):
                    self._post_photo_meta(p[len("/photo_meta/"):])
                elif p == "/panorama":
                    b = self._read_body(8 << 20)
                    if b is None or len(b) < 4 or b[:2] != SOI:
                        self._err(400, "body must be a JPEG (max 8MB)")
                        return
                    # chatbot publish path bypasses the scan subprocess — a
                    # stale meta would draw a previous scan's boxes on it
                    _quiet(lambda: os.remove(
                        os.path.join(app.photo_dir, "panorama.meta.json")))
                    self._write_photo_file("panorama.jpg", b, {"ok": True, "bytes": len(b)})
                elif p == "/pano_status":
                    st = (q.get("state") or [""])[0]
                    if st not in ("scanning", "recording", "describing", "stitching",
                                  "uploading", "done", "failed", "idle"):
                        self._err(400, "bad state")
                        return
                    with app._pano_mu:
                        app.pano_state, app.pano_state_at = st, time.monotonic()
                    self._json(200, {"ok": True})
                elif p.startswith("/pano_variant/"):
                    name = p[len("/pano_variant/"):]
                    if not DET_NAME_RE.match(name):
                        self._err(400, "bad variant name")
                        return
                    b = self._read_body(12 << 20)
                    if b is None or len(b) < 4 or b[:2] != SOI:
                        self._err(400, "body must be a JPEG")
                        return
                    self._write_photo_file("pano_var_" + name + ".jpg", b, {"ok": True})
                elif p.startswith("/det_image/"):
                    name = p[len("/det_image/"):]
                    if not DET_NAME_RE.match(name):
                        self._err(400, "bad model name")
                        return
                    b = self._read_body(8 << 20)
                    if b is None or len(b) < 4 or b[:2] != SOI:
                        self._err(400, "body must be a JPEG")
                        return
                    self._write_photo_file("det_" + name + ".jpg", b, {"ok": True})
                elif p == "/tour":
                    b = self._read_body(64 << 20)
                    if b is None or len(b) < 4 or b[:2] != SOI:
                        self._err(400, "body must be concatenated JPEG frames (max 64MB)")
                        return
                    self._write_photo_file("tour.mjpeg", b, {"ok": True, "bytes": len(b)})
                else:
                    self._err(404, "unknown path")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _write_photo_file(self, name, data, ok_obj):
            try:
                os.makedirs(app.photo_dir, exist_ok=True)
                with open(os.path.join(app.photo_dir, name), "wb") as fh:
                    fh.write(data)
            except OSError as e:
                self._err(500, str(e))
                return
            self._json(200, ok_obj)

        def _post_photo_meta(self, name):
            """Sanitized re-marshal: arbitrary client JSON is never stored."""
            if not safe_photo_name(name):
                self._err(400, "bad photo name")
                return
            b = self._read_body(4096)
            if b is None:
                self._err(400, "bad meta body")
                return
            try:
                data = json.loads(b)
                bbox = [float(v) for v in data.get("bbox", [])]
                conf = float(data.get("confidence") or 0)
            except (ValueError, TypeError):
                self._err(400, "bad meta body")
                return
            if math.isnan(conf):
                self._err(400, "confidence must be a number")
                return
            if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
                self._err(400, "bbox must be [x1,y1,x2,y2] fractions")
                return
            for v in bbox:
                if math.isnan(v) or v < 0 or v > 1:
                    self._err(400, "bbox values must be 0..1")
                    return
            out = {"target": str(data.get("target", ""))[:100],
                   "label": str(data.get("label", ""))[:32],
                   "color": str(data.get("color", ""))[:40],
                   "bbox": bbox,
                   "confidence": clamp(conf, 0, 1)}
            try:
                with open(os.path.join(app.photo_dir, name + ".meta.json"), "w") as fh:
                    json.dump(out, fh)
            except OSError as e:
                self._err(500, str(e))
                return
            self._json(200, {"ok": True})

        # ---- streaming ----
        def _video_feed(self):
            boundary = "rovercamframe"
            self.close_connection = True   # unbounded multipart: no keep-alive
            try:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                           STREAM_SEND_BUFFER)
                self.connection.settimeout(STREAM_WRITE_TIMEOUT)
            except OSError:
                pass
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=" + boundary)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "HEAD":
                return
            get, cancel = app.hub.subscribe()
            try:
                while True:
                    frame = get(1.0)   # 1s keepalive tick, same as the Go build
                    if frame is None:
                        frame = app.hub.latest_frame() or PLACEHOLDER_FRAME
                    up, _ = app.cam.status()
                    if not up:
                        frame = PLACEHOLDER_FRAME
                    hdr = (f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                           f"Content-Length: {len(frame)}\r\n\r\n").encode()
                    self.wfile.write(hdr)
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                return
            finally:
                cancel()

        def _tour_feed(self, q):
            self.close_connection = True   # multipart stream: close when done
            fp = os.path.join(app.photo_dir, "tour.mjpeg")
            try:
                blob = open(fp, "rb").read()
            except OSError:
                self._err(404, "no tour yet — record one ($record in the chatbot)")
                return
            frames = []
            i = 0
            while i + 1 < len(blob):
                j = blob.find(SOI, i + 2)
                if j < 0:
                    frames.append(blob[i:])
                    break
                frames.append(blob[i:j])
                i = j
            if not frames:
                self._err(404, "tour file has no frames")
                return
            loops = 1 if (q.get("loops") or [""])[0] == "1" else 1 << 30
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=tourframe")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                for _ in range(loops):
                    for fr in frames:
                        self.wfile.write((f"--tourframe\r\nContent-Type: image/jpeg\r\n"
                                          f"Content-Length: {len(fr)}\r\n\r\n").encode())
                        self.wfile.write(fr)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                        if loops > 1:
                            time.sleep(1.0 / 14)
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                return
    return Handler


# ─────────────────────────────── startup ────────────────────────────────────

def default_photo_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos")


def default_map_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "gamepad.json")


def open_serial_with_retry(rover, path, stop_event):
    while not stop_event.is_set():
        try:
            link = TTYLink(path)
        except OSError as e:
            rover.set_status(None, str(e))
            log(f"serial: {path} unavailable ({e}); retrying")
        else:
            try:
                init_link(link)
            except OSError as e:
                link.close()
                rover.set_status(None, "init failed: " + str(e))
                log(f"serial: init failed on {path}: {e}; retrying")
            else:
                if stop_event.is_set():
                    link.close()   # shutting down — don't publish a dead link
                    return
                rover.set_status(link, "")
                log(f"serial: connected on {path} @115200")
                return
        if stop_event.wait(3.0):
            return


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rovercontrold", add_help=True)
    ap.add_argument("-port", "--port", default="8080")
    ap.add_argument("-photos", "--photos", default=default_photo_dir())
    ap.add_argument("-serial", "--serial", default="/dev/ttyAMA0")
    ap.add_argument("-gamepad", "--gamepad", default="/dev/input/js0")
    ap.add_argument("-width", "--width", type=int, default=DEFAULT_CAM_W)
    ap.add_argument("-height", "--height", type=int, default=DEFAULT_CAM_H)
    ap.add_argument("-fps", "--fps", type=int, default=DEFAULT_CAM_FPS)
    ap.add_argument("-camera-mode", "--camera-mode", default="auto")
    ap.add_argument("-camera-device", "--camera-device", default="/dev/video0")
    ap.add_argument("-gamepad-debug", "--gamepad-debug", action="store_true")
    ap.add_argument("-gamepad-map", "--gamepad-map", default=default_map_path())
    ap.add_argument("-calibrate", "--calibrate", action="store_true")
    args = ap.parse_args(argv)

    if args.gamepad_debug:
        run_gamepad_debug(args.gamepad)
        return
    if args.calibrate:
        try:
            run_calibrate(args.gamepad, args.gamepad_map)
        except OSError as e:
            sys.exit(f"calibrate: {e}")
        return

    mode = resolve_camera_mode(args.camera_mode, args.camera_device)
    log(f"camera: backend {mode} (device {args.camera_device})")
    rover = Rover()
    hub = Hub()
    cam = Camera(mode, args.camera_device, args.width, args.height, args.fps)
    move = Movement(rover)
    aim = CameraAim(rover)
    app = App(rover, move, aim, hub, cam, args.photos)

    # gamepad mapping: missing → default; malformed/invalid → DISABLE the pad.
    try:
        app.mapping, app.map_source = load_mapping(args.gamepad_map)
        log(f"gamepad: mapping {app.map_source}")
    except (ValueError, json.JSONDecodeError, OSError) as e:
        app.mapping, app.map_source = None, "invalid"
        log(f"gamepad: mapping {args.gamepad_map} invalid ({e}) — joystick DISABLED; fix the file")

    stop_event = threading.Event()

    if args.serial:
        threading.Thread(target=open_serial_with_retry,
                         args=(rover, args.serial, stop_event), daemon=True).start()
        threading.Thread(target=run_telemetry,
                         args=(rover, app.pose, stop_event), daemon=True).start()
    else:
        rover.set_status(None, "disabled")
        log("serial: disabled (-serial '')")

    if mode != "off":
        threading.Thread(target=cam.run, args=(stop_event, hub), daemon=True).start()
    else:
        cam.set_status(False, "disabled")
        log("camera: disabled (-camera-mode off)")

    if args.gamepad and app.mapping is not None:
        threading.Thread(target=run_gamepad,
                         args=(app, args.gamepad, stop_event), daemon=True).start()

    threading.Thread(target=move.run_watchdog, args=(stop_event,), daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", int(args.port)), make_handler(app))
    server.daemon_threads = True

    def shutdown(signum, frame):
        log("rovercontrold: signal received, shutting down")
        stop_event.set()
        # doEstop (not stop): latch e-stop so a /drive racing the shutdown
        # window is refused; then close serial and drain the server.
        move.do_estop()
        rover.close_link()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log(f"rovercontrold: http://0.0.0.0:{args.port}  (photos: {args.photos})")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    log("rovercontrold: stopped")


if __name__ == "__main__":
    main()
