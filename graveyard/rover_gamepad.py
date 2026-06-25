#!/usr/bin/env python3
"""Drive the rover with a USB gamepad plugged into THIS laptop, over the network.

Unlike the (graveyard) on-rover rover_joystick.py, this runs on your computer and
sends control commands to the rover's running app.py (Flask, :5000) via HTTP, so
the camera + web UI keep working — no serial contention. See docs/plans/009.

  Left stick    drive (throttle + steer)      RB (hold)  turbo
  Right stick   camera pan / tilt             D-pad U/D  speed cap +/-
  A stop wheels   B snapshot   X head light   Y center camera
  LB base light   Back E-STOP  L3 relax       R3 lock gimbal   Start/Ctrl-C quit

SAFETY — SUPERVISED USE ONLY. {"T":1} sets a *continuous* wheel speed; if the
Wi-Fi drops or this process is hard-killed while a stick is held, the rover keeps
moving. Drive with the rover on a stand or in open space, ready to e-stop. A
server-side watchdog (app.py) is the real fix for unsupervised use (see plan 009).

  pip install pygame            # laptop only; the rover is untouched
  python rover_gamepad.py --host 192.168.1.131
  python rover_gamepad.py --debug      # print live axis/button indices to remap
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, replace

# ── gamepad mapping (Xbox-style on SDL2) ────────────────────────────────────
AX_LX, AX_LY = 0, 1     # left stick: steer, throttle
AX_RX, AX_RY = 3, 4     # right stick: camera pan, tilt
BTN_A, BTN_B, BTN_X, BTN_Y = 0, 1, 2, 3
BTN_LB, BTN_RB = 4, 5
BTN_BACK, BTN_START = 6, 7
BTN_L3, BTN_R3 = 9, 10

# ── tunables (mirror the graveyard script) ──────────────────────────────────
DEADZONE = 0.15
SPEED_STEPS = [0.15, 0.20, 0.25, 0.30, 0.40]
SPEED_START = 2
TURBO_SPEED = 0.40
RAMP = 1.2              # max wheel-speed change/sec (slew-rate limit)
PAN_RATE = 90.0         # deg/sec at full stick
TILT_RATE = 70.0
RATE_HZ = 25.0          # control loop tick
DRIVE_HZ = 8.0          # max drive/camera HTTP rate (heartbeat + cap)
SEND_TIMEOUT = 0.5      # short per-request HTTP timeout for control commands


CONTROLS = """controls:
  left stick   drive (throttle + steer)     RB (hold)  turbo
  right stick  camera pan / tilt            D-pad U/D  speed cap +/-
  A stop   B snapshot   X head light   Y center   LB base light
  Back E-STOP   L3 relax gimbal   R3 lock gimbal   Start/Ctrl-C quit"""


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def dz(v: float) -> float:
    return 0.0 if abs(v) < DEADZONE else v


# ── pure state ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PadState:
    axes: tuple = ()
    buttons: tuple = ()
    hats: tuple = ((0, 0),)

    def axis(self, i):
        return self.axes[i] if i < len(self.axes) else 0.0

    def button(self, i):
        return bool(self.buttons[i]) if i < len(self.buttons) else False

    def hat(self, i):
        return self.hats[i] if i < len(self.hats) else (0, 0)


@dataclass(frozen=True)
class ControlState:
    speed_idx: int = SPEED_START
    left: float = 0.0
    right: float = 0.0
    pan: float = 0.0
    tilt: float = 0.0
    head_on: bool = False
    base_on: bool = False
    estopped: bool = False


@dataclass(frozen=True)
class Step:
    left: float = 0.0
    right: float = 0.0
    pan: float = 0.0
    tilt: float = 0.0
    head_on: bool = False
    base_on: bool = False
    light_changed: bool = False
    stop: bool = False
    estop: bool = False
    snapshot: bool = False
    center: bool = False
    relax: bool = False
    lock: bool = False
    quit: bool = False


def compute_step(state: PadState, prev: PadState, ctrl: ControlState,
                 dt: float) -> tuple[Step, ControlState]:
    """Pure: map a gamepad snapshot to a Step + the next ControlState. No I/O,
    no mutation of inputs. While e-stop is latched it emits no drive until the
    sticks recenter."""
    def pressed(b):
        return state.button(b) and not prev.button(b)

    speed_idx = ctrl.speed_idx
    head_on, base_on, estopped = ctrl.head_on, ctrl.base_on, ctrl.estopped
    pan, tilt = ctrl.pan, ctrl.tilt
    left, right = ctrl.left, ctrl.right

    quit_ = pressed(BTN_START)
    center = pressed(BTN_Y)
    if center:
        pan = tilt = 0.0
    stop = pressed(BTN_A)
    light_changed = False
    if pressed(BTN_X):
        head_on = not head_on
        light_changed = True
    if pressed(BTN_LB):
        base_on = not base_on
        light_changed = True
    snapshot = pressed(BTN_B)
    estop = pressed(BTN_BACK)
    if estop:
        left = right = 0.0
        estopped = True
    relax = pressed(BTN_L3)
    lock = pressed(BTN_R3)

    # D-pad (hat) vertical edge → speed cap
    hy, phy = state.hat(0)[1], prev.hat(0)[1]
    if hy != 0 and phy == 0:
        speed_idx = int(clamp(speed_idx + (1 if hy > 0 else -1),
                              0, len(SPEED_STEPS) - 1))

    # drive from the left stick
    top = TURBO_SPEED if state.button(BTN_RB) else SPEED_STEPS[speed_idx]
    throttle = -dz(state.axis(AX_LY))   # stick up = forward
    steer = dz(state.axis(AX_LX))
    if estopped:
        if throttle == 0.0 and steer == 0.0:
            estopped = False            # sticks centered → release latch
        else:
            throttle = steer = 0.0      # hold stop until recentered
    tgt_left = clamp(throttle + steer, -1.0, 1.0) * top
    tgt_right = clamp(throttle - steer, -1.0, 1.0) * top
    step_limit = RAMP * dt              # slew-rate limit
    left = left + clamp(tgt_left - left, -step_limit, step_limit)
    right = right + clamp(tgt_right - right, -step_limit, step_limit)

    # camera from the right stick (integrate into absolute angles)
    pan = clamp(pan + dz(state.axis(AX_RX)) * PAN_RATE * dt, -180.0, 180.0)
    tilt = clamp(tilt + -dz(state.axis(AX_RY)) * TILT_RATE * dt, -45.0, 90.0)

    out = Step(left=left, right=right, pan=pan, tilt=tilt,
               head_on=head_on, base_on=base_on, light_changed=light_changed,
               stop=stop, estop=estop, snapshot=snapshot, center=center,
               relax=relax, lock=lock, quit=quit_)
    nxt = replace(ctrl, speed_idx=speed_idx, left=left, right=right, pan=pan,
                  tilt=tilt, head_on=head_on, base_on=base_on, estopped=estopped)
    return out, nxt


# ── non-blocking, prioritized HTTP sender ───────────────────────────────────
class Sender(threading.Thread):
    """Serializes all control HTTP off the control loop. stop/estop preempt and
    clear any pending drive (latest-wins); drive/camera are coalesced slots so a
    slow link can't build a backlog. The loop only ever updates slots — it never
    blocks on HTTP."""

    def __init__(self, client):
        super().__init__(daemon=True)
        self._c = client
        self._cv = threading.Condition()
        self._urgent = collections.deque()   # one-shots: stop/estop/lights/relax/lock
        self._drive = None                   # (l, r) latest-wins
        self._camera = None                  # (pan, tilt) latest-wins
        self._stopping = False
        self._logged_err = False

    # loop-side API (never blocks on HTTP)
    def emergency(self, item):
        with self._cv:
            self._urgent.clear()             # preempt everything pending
            self._drive = None               # discard queued motion
            self._camera = None
            self._urgent.append(item)
            self._cv.notify()

    def urgent(self, item):
        with self._cv:
            self._urgent.append(item)
            self._cv.notify()

    def set_drive(self, l, r):
        with self._cv:
            self._drive = (l, r)
            self._cv.notify()

    def set_camera(self, pan, tilt):
        with self._cv:
            self._camera = (pan, tilt)
            self._cv.notify()

    def shutdown(self):
        with self._cv:
            self._stopping = True
            self._cv.notify()

    def run(self):
        while True:
            with self._cv:
                while not (self._urgent or self._drive is not None
                           or self._camera is not None or self._stopping):
                    self._cv.wait()
                if self._urgent:
                    kind, item = "urgent", self._urgent.popleft()
                elif self._drive is not None:
                    kind, item, self._drive = "drive", self._drive, None
                elif self._camera is not None:
                    kind, item, self._camera = "camera", self._camera, None
                else:                         # stopping and nothing left
                    return
            self._send(kind, item)            # OUTSIDE the lock (may block ≤ timeout)

    def _send(self, kind, item):
        try:
            if kind == "drive":
                self._c.move(*item)
            elif kind == "camera":
                self._c.set_camera(*item)
            elif kind == "urgent":
                self._dispatch_urgent(item)
        except Exception as e:                # transient link error — don't die
            if not self._logged_err:
                print(f"\n[sender] send failed ({e}); will keep trying", flush=True)
                self._logged_err = True

    def _dispatch_urgent(self, item):
        tag = item[0] if isinstance(item, tuple) else item
        if tag == "estop":
            self._c.estop()
        elif tag == "stop":
            self._c.stop()
        elif tag == "lights":
            self._c.lights(item[1], item[2])
        elif tag == "relax":
            self._c.servo_torque(False)
        elif tag == "lock":
            self._c.servo_torque(True)


# ── dispatch: Step → Sender, with change-gating + heartbeat (≤ DRIVE_HZ) ─────
class Dispatcher:
    def __init__(self, sender, drive_hz=DRIVE_HZ):
        self._s = sender
        self._period = 1.0 / drive_hz
        # Seed last-sent state to neutral so the first idle tick sends nothing
        # (no spurious startup stop or camera-recenter); a real change still sends.
        self._last_drive = (0.0, 0.0)
        self._last_drive_t = -1e9
        self._last_cam = (0.0, 0.0)
        self._last_cam_t = -1e9

    def dispatch(self, step: Step, now: float):
        if step.estop:
            self._s.emergency("estop")
            self._last_drive = (0.0, 0.0)
        elif step.stop:
            self._s.emergency("stop")
            self._last_drive = (0.0, 0.0)
        else:
            d = (round(step.left, 3), round(step.right, 3))
            moving = d != (0.0, 0.0)
            changed = d != self._last_drive
            due = (now - self._last_drive_t) >= self._period
            # Change to neutral → send the stop immediately (safety). Nonzero
            # drive (incl. ramp steps) is rate-limited to DRIVE_HZ — the latest
            # value at each due tick, which also serves as the heartbeat.
            if changed and not moving:
                self._s.set_drive(*d)
                self._last_drive, self._last_drive_t = d, now
            elif moving and due:
                self._s.set_drive(*d)
                self._last_drive, self._last_drive_t = d, now

        if step.center:
            self._s.set_camera(0.0, 0.0)
            self._last_cam, self._last_cam_t = (0.0, 0.0), now
        else:
            cam = (round(step.pan, 1), round(step.tilt, 1))
            if cam != self._last_cam and (now - self._last_cam_t) >= self._period:
                self._s.set_camera(step.pan, step.tilt)
                self._last_cam, self._last_cam_t = cam, now

        if step.light_changed:
            self._s.urgent(("lights", 255 if step.head_on else 0,
                            255 if step.base_on else 0))
        if step.relax:
            self._s.urgent("relax")
        if step.lock:
            self._s.urgent("lock")


# ── snapshot (own thread; grabs a frame from the MJPEG stream) ───────────────
class Snapshotter(threading.Thread):
    def __init__(self, host, port=5000, out_dir="photos"):
        super().__init__(daemon=True)
        self._url = f"http://{host}:{port}/video_feed"
        self._out = out_dir
        self._ev = threading.Event()
        self._quit = False

    def trigger(self):
        self._ev.set()

    def shutdown(self):
        self._quit = True
        self._ev.set()

    def run(self):
        while True:
            self._ev.wait()
            self._ev.clear()
            if self._quit:
                return
            try:
                path = self._grab()
                print(f"\nphoto -> {path}", flush=True)
            except Exception as e:
                print(f"\nsnapshot failed: {e}", flush=True)

    def _grab(self):
        os.makedirs(self._out, exist_ok=True)
        buf = b""
        with urllib.request.urlopen(self._url, timeout=5) as r:
            deadline = time.time() + 5
            while time.time() < deadline:
                chunk = r.read(8192)
                if not chunk:
                    break
                buf += chunk
                s = buf.find(b"\xff\xd8")
                e = buf.find(b"\xff\xd9", s + 2)
                if s != -1 and e != -1:
                    path = os.path.join(self._out,
                                        time.strftime("rover_%Y%m%d_%H%M%S.jpg"))
                    with open(path, "wb") as f:
                        f.write(buf[s:e + 2])
                    return path
        raise RuntimeError("no complete JPEG frame in stream")


# ── pygame shell + main ──────────────────────────────────────────────────────
def _open_pad():
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame
    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        sys.exit("No gamepad found. Plug a controller into THIS computer.")
    js = pygame.joystick.Joystick(0)
    js.init()
    print(f"gamepad: {js.get_name()} "
          f"({js.get_numaxes()} axes, {js.get_numbuttons()} buttons)")
    return pygame, js


def _read_pad(pygame, js):
    """Snapshot the pad into a PadState, or None if the controller vanished
    (deadman → caller stops the wheels)."""
    pygame.event.get()                       # drain queue or state freezes
    if pygame.joystick.get_count() == 0 or not js.get_init():
        return None
    axes = tuple(js.get_axis(i) for i in range(js.get_numaxes()))
    buttons = tuple(bool(js.get_button(i)) for i in range(js.get_numbuttons()))
    hats = tuple(js.get_hat(i) for i in range(js.get_numhats())) or ((0, 0),)
    return PadState(axes=axes, buttons=buttons, hats=hats)


def _debug_loop(pygame, js):
    print("DEBUG: move sticks / press buttons; Ctrl-C to quit.")
    try:
        while True:
            pygame.event.get()
            axes = [round(js.get_axis(i), 2) for i in range(js.get_numaxes())]
            btns = [i for i in range(js.get_numbuttons()) if js.get_button(i)]
            hats = [js.get_hat(i) for i in range(js.get_numhats())]
            print(f"axes {axes}  buttons {btns}  hats {hats}    ",
                  end="\r", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Gamepad → rover (auto-detect: direct serial on the rover, HTTP remote).")
    ap.add_argument("--host", default=os.environ.get("ROVER_HOST", "192.168.1.131"))
    ap.add_argument("--debug", action="store_true",
                    help="print live axis/button/hat indices and exit")
    args = ap.parse_args(argv)

    pygame, js = _open_pad()
    if args.debug:
        _debug_loop(pygame, js)
        return

    import rover_backend
    backend = rover_backend.connect(host=args.host, timeout=SEND_TIMEOUT)
    if backend is None:
        sys.exit(f"rover not found: no local serial and {args.host}:5000 unreachable.")

    print(CONTROLS)
    print("\n*** SUPERVISED USE ONLY — keep a finger near E-STOP (Back). ***\n")
    print(f"driving via {backend.where} ({backend.backend})\n")

    sender = Sender(backend)
    sender.start()
    # Snapshot grabs a frame from app.py's MJPEG stream — HTTP backend only.
    # On serial the web app is stopped, so there's no /video_feed to pull.
    snap = Snapshotter(args.host) if backend.backend == "http" else None
    if snap:
        snap.start()
    disp = Dispatcher(sender)

    ctrl = ControlState()
    prev = PadState()
    dt = 1.0 / RATE_HZ
    try:
        while True:
            state = _read_pad(pygame, js)
            if state is None:                # deadman: controller gone
                sender.emergency("stop")
                print("\ncontroller disconnected — stopped.", flush=True)
                break
            step, ctrl = compute_step(state, prev, ctrl, dt)
            prev = state
            if step.quit:
                break
            disp.dispatch(step, time.monotonic())
            if step.snapshot:
                if snap:
                    snap.trigger()
                else:
                    print("\nno camera (serial mode)", flush=True)
            time.sleep(dt)
    except KeyboardInterrupt:
        pass
    finally:
        # Route the final stop THROUGH the sender so it can't race a queued or
        # in-flight drive: emergency() discards pending motion and queues estop;
        # shutdown() lets run() drain that estop; join() waits so the estop is
        # the genuinely last command on the wire.
        sender.emergency("estop")
        sender.shutdown()
        sender.join(timeout=2.0)
        if snap:
            snap.shutdown()
        print("\nstopped.")


if __name__ == "__main__":
    main()
