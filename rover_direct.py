#!/usr/bin/env python3
# SUPERSEDED by rovercontrol (single-file Go controller, docs/plans/002) — kept
# only because the LLM chatbot (rover_chat.py) still imports it. To be repointed
# to the controller's HTTP API or removed in a follow-up.
"""Direct serial control of the Waveshare UGV rover — no HTTP service needed.

Runs ON the rover (ssh ws@192.168.1.131). Talks straight to the ESP32 sub-
controller over the UART, sending the same line-delimited JSON commands that
ugv_rpi/base_ctrl.py uses:

    motors :  {"T":1,  "L":<left>, "R":<right>}        # wheel speeds
    gimbal :  {"T":133,"X":<pan>, "Y":<tilt>, "SPD":0, "ACC":0}   # absolute aim
    gimbal :  {"T":141,"X":<pan>, "Y":<tilt>, "SPD":<spd>}        # continuous
    e-stop :  {"T":0}
    lights :  {"T":132,"IO4":<a>,"IO5":<b>}
    module :  {"T":4,  "cmd":2}   # 0:None 1:RoArm 2:Gimbal — selects pan/tilt

The serial port can only be held by one process. app.py (the web service) grabs
it at boot, so by default this script stops app.py first. Restore the web
service with a reboot, or:  ~/ugv_rpi/ugv-env/bin/python ~/ugv_rpi/app.py

Run with the env that has pyserial:
    ~/ugv_rpi/ugv-env/bin/python ~/rover_direct.py            # interactive
    ~/ugv_rpi/ugv-env/bin/python ~/rover_direct.py demo       # quick self-test
    ~/ugv_rpi/ugv-env/bin/python ~/rover_direct.py --keep-app # don't stop app.py
"""
import json
import os
import subprocess
import sys
import time

import serial

BAUD = 115200
# Pan/tilt and wheel limits (match base_ctrl conventions; clamped for safety).
PAN_LIMIT = (-180.0, 180.0)
TILT_LIMIT = (-45.0, 90.0)   # + is up
SPEED_LIMIT = 0.5            # max |wheel speed| we allow by default


def detect_port() -> str:
    """Pi 5 uses /dev/ttyAMA0; earlier Pis use /dev/serial0 (same as app.py)."""
    try:
        with open("/proc/cpuinfo") as f:
            if "Raspberry Pi 5" in f.read():
                return "/dev/ttyAMA0"
    except OSError:
        pass
    return "/dev/serial0"


def stop_http_service() -> bool:
    """Kill the ugv_rpi web app so we can own the serial port. Returns True if
    something was stopped. It won't auto-restart until reboot (it's an @reboot
    cron job)."""
    try:
        out = subprocess.run(["pgrep", "-f", "ugv_rpi/app.py"],
                             capture_output=True, text=True)
        pids = [p for p in out.stdout.split() if p]
        if not pids:
            return False
        subprocess.run(["pkill", "-f", "ugv_rpi/app.py"])
        time.sleep(1.5)  # let it release the port
        return True
    except Exception as e:
        print(f"[stop_http_service] {e}")
        return False


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Rover:
    def __init__(self, port: str = None, baud: int = BAUD, init: bool = True):
        self.port = port or detect_port()
        self.ser = serial.Serial(self.port, baud, timeout=1)
        self.pan = 0.0    # tracked camera angles (for relative nudges)
        self.tilt = 0.0
        time.sleep(0.2)
        if init:
            self.init_base()

    # -- low level ----------------------------------------------------------
    def send(self, cmd: dict) -> None:
        """Send one JSON command (newline-terminated) to the ESP32."""
        self.ser.write((json.dumps(cmd, separators=(",", ":")) + "\n").encode("utf-8"))

    def init_base(self) -> None:
        """Boot config for a write-only controller (like app.py, minus the
        feedback stream, which we don't read)."""
        self.send({"T": 143, "cmd": 0})   # serial echo off
        self.send({"T": 131, "cmd": 0})   # feedback stream off (we don't read it)
        self.select_module(2)             # 2 = Gimbal (enables pan/tilt)

    def select_module(self, module: int) -> None:
        """0:None 1:RoArm-M2-S 2:Gimbal. The pan/tilt camera needs module 2."""
        self.send({"T": 4, "cmd": module})

    # -- motors -------------------------------------------------------------
    def drive(self, left: float, right: float) -> None:
        """Set wheel speeds. left/right roughly -0.5..0.5 (negative reverses)."""
        self.send({"T": 1,
                   "L": _clamp(left, -SPEED_LIMIT, SPEED_LIMIT),
                   "R": _clamp(right, -SPEED_LIMIT, SPEED_LIMIT)})

    def stop(self) -> None:
        self.send({"T": 1, "L": 0, "R": 0})

    def drive_for(self, left: float, right: float, seconds: float) -> None:
        """Drive at the given speeds for `seconds`, then stop."""
        self.drive(left, right)
        time.sleep(max(0.0, min(10.0, seconds)))
        self.stop()

    def forward(self, speed: float = 0.2, seconds: float = 1.0) -> None:
        self.drive_for(abs(speed), abs(speed), seconds)

    def backward(self, speed: float = 0.2, seconds: float = 1.0) -> None:
        self.drive_for(-abs(speed), -abs(speed), seconds)

    def spin_left(self, speed: float = 0.2, seconds: float = 0.6) -> None:
        self.drive_for(-abs(speed), abs(speed), seconds)

    def spin_right(self, speed: float = 0.2, seconds: float = 0.6) -> None:
        self.drive_for(abs(speed), -abs(speed), seconds)

    # -- camera gimbal ------------------------------------------------------
    def set_camera(self, pan: float, tilt: float, speed: int = 0, acc: int = 0) -> None:
        """Aim the camera to absolute angles. pan=left/right, tilt=up/down (+up)."""
        self.pan = _clamp(pan, *PAN_LIMIT)
        self.tilt = _clamp(tilt, *TILT_LIMIT)
        self.send({"T": 133, "X": self.pan, "Y": self.tilt, "SPD": speed, "ACC": acc})

    def gimbal_continuous(self, pan: float, tilt: float, speed: int = 200) -> None:
        """Move the gimbal at a velocity (T:141) rather than to an angle."""
        self.send({"T": 141, "X": pan, "Y": tilt, "SPD": speed})

    def center_camera(self) -> None:
        self.set_camera(0, 0)

    def gimbal_stop(self) -> None:
        self.send({"T": 0})

    def estop(self) -> None:
        """Stop wheels AND gimbal immediately."""
        self.send({"T": 1, "L": 0, "R": 0})
        self.send({"T": 0})

    # -- misc ---------------------------------------------------------------
    def lights(self, front: int = 0, base: int = 0) -> None:
        """LED PWM 0..255. front=IO5 (head), base=IO4 (chassis)."""
        self.send({"T": 132, "IO4": base, "IO5": front})

    def oled(self, line: int, text: str) -> None:
        """Write text to an OLED line (0-3)."""
        self.send({"T": 3, "lineNum": line, "Text": text})

    def oled_default(self) -> None:
        """Restore the OLED's default status screen."""
        self.send({"T": -3})

    def servo_torque(self, lock: bool, servo_id: int = 255) -> None:
        """Lock (True) or release (False) bus servos; 255 = all. Releasing lets
        you hand-position the gimbal; lock to hold it."""
        self.send({"T": 210, "id": servo_id, "cmd": 1 if lock else 0})

    def close(self) -> None:
        try:
            self.stop()
            self.ser.close()
        except Exception:
            pass


# --------------------------------------------------------------------- demo
def demo(r: Rover) -> None:
    print("camera: up 45"); r.set_camera(0, 45); time.sleep(2)
    print("camera: down -30"); r.set_camera(0, -30); time.sleep(2)
    print("camera: pan left -45"); r.set_camera(-45, 0); time.sleep(2)
    print("camera: center"); r.center_camera(); time.sleep(1)
    print("motors: nudge forward 0.6s @0.15"); r.forward(0.15, 0.6)
    print("motors: spin right 0.5s"); r.spin_right(0.2, 0.5)
    print("demo done")


# ---------------------------------------------------------------- interactive
HELP = """commands:
  camera:
    up/down/left/right [DEG]   nudge camera (DEG degrees, default 15)
    cam PAN TILT               aim to absolute angles (tilt + = up)
    center                     level the camera
  motors:
    drive L R [SECS]           wheel speeds, auto-stop after SECS (default 1)
    move L R                   wheel speeds, continuous (no auto-stop)
    fwd/back [SECS]            drive straight
    spinl/spinr [SECS]         turn in place (default 0.6s)
    stop                       stop wheels
  other:
    estop                      stop wheels AND gimbal now
    relax / lock               release / lock the gimbal servos (hand-position)
    light F B                  LED PWM 0..255 (front, base)
    oled LINE TEXT...          write text to OLED line 0-3
    oledclear                  restore default OLED screen
    demo                       motor+camera self-test
    help / quit
"""


QUIT = "__QUIT__"


def exec_command(r: Rover, line: str) -> str:
    """Run one direct command line. Returns a status string, "" for no output,
    or the QUIT sentinel. Never raises — returns an error string instead.
    Shared by the standalone REPL and the chatbot's `$` direct-command path."""
    parts = line.split()
    if not parts:
        return ""
    c = parts[0].lower()
    args = parts[1:]
    try:
        if c in ("quit", "exit"):
            return QUIT
        if c == "help":
            return HELP
        if c == "cam":
            r.set_camera(float(args[0]), float(args[1]))
            return f"camera -> pan={r.pan}, tilt={r.tilt}"
        if c in ("up", "down", "left", "right"):
            step = float(args[0]) if args else 15.0   # optional degrees
            pan, tilt = r.pan, r.tilt
            if c == "up":
                tilt += step
            elif c == "down":
                tilt -= step
            elif c == "left":
                pan -= step
            else:
                pan += step
            r.set_camera(pan, tilt)
            return f"camera -> pan={r.pan}, tilt={r.tilt}"
        if c == "center":
            r.center_camera()
            return "camera centered"
        if c == "drive":
            r.drive_for(float(args[0]), float(args[1]),
                        float(args[2]) if len(args) > 2 else 1.0)
            return f"drove L={args[0]} R={args[1]}, stopped"
        if c == "move":
            r.drive(float(args[0]), float(args[1]))
            return f"moving L={args[0]} R={args[1]} (continuous)"
        if c == "fwd":
            r.forward(0.2, float(args[0]) if args else 1.0)
            return "forward"
        if c == "back":
            r.backward(0.2, float(args[0]) if args else 1.0)
            return "back"
        if c == "spinl":
            r.spin_left(0.2, float(args[0]) if args else 0.6)
            return "spin left"
        if c == "spinr":
            r.spin_right(0.2, float(args[0]) if args else 0.6)
            return "spin right"
        if c == "stop":
            r.stop()
            return "stopped"
        if c == "estop":
            r.estop()
            return "emergency stopped"
        if c == "relax":
            r.servo_torque(False)
            return "gimbal relaxed"
        if c == "lock":
            r.servo_torque(True)
            return "gimbal locked"
        if c == "light":
            r.lights(int(args[0]), int(args[1]))
            return f"lights front={args[0]} base={args[1]}"
        if c == "oled":
            r.oled(int(args[0]), " ".join(args[1:]))
            return "oled written"
        if c == "oledclear":
            r.oled_default()
            return "oled reset"
        if c == "demo":
            demo(r)
            return "demo done"
        return f"?? unknown command '{c}' — type 'help'"
    except (IndexError, ValueError):
        return "bad args — type 'help'"


def repl(r: Rover) -> None:
    print(HELP)
    while True:
        try:
            line = input("rover> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if not line:
            continue
        out = exec_command(r, line)
        if out == QUIT:
            break
        if out:
            print(out)


def camtest(r: "Rover") -> None:
    """Slow, visible camera-only sweep (no driving)."""
    print("camera UP 45 ..."); r.set_camera(0, 45); time.sleep(2.5)
    print("camera DOWN -30 ..."); r.set_camera(0, -30); time.sleep(2.5)
    print("camera PAN LEFT -45 ..."); r.set_camera(-45, 0); time.sleep(2.5)
    print("camera PAN RIGHT 45 ..."); r.set_camera(45, 0); time.sleep(2.5)
    print("camera CENTER ..."); r.center_camera(); time.sleep(1)
    print("camtest done")


def main() -> None:
    keep_app = "--keep-app" in sys.argv
    run_demo = "demo" in sys.argv
    run_camtest = "camtest" in sys.argv

    if not keep_app:
        if stop_http_service():
            print("stopped ugv_rpi/app.py to free the serial port "
                  "(reboot or rerun app.py to restore the web service).")

    port = detect_port()
    try:
        r = Rover(port)
    except serial.SerialException as e:
        sys.exit(f"Could not open {port}: {e}\n"
                 "If the web service still holds it, run without --keep-app.")
    print(f"connected on {port} @ {BAUD}")
    try:
        if run_demo:
            demo(r)
        elif run_camtest:
            camtest(r)
        else:
            repl(r)
    finally:
        r.close()


if __name__ == "__main__":
    main()
