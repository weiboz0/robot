#!/usr/bin/env python3
"""Drive the rover with a USB gamepad plugged into the rover — direct serial.

Runs ON the rover. Uses rover_direct (UART control) so it needs no HTTP service.
Left stick drives the wheels, right stick aims the camera.

  Left stick    : throttle (up/down) + steering (left/right)
  Right stick   : camera pan / tilt
  RB (hold)     : turbo (higher top speed)
  A button      : stop wheels
  Y button      : center camera
  X button      : toggle head light
  Start button  : quit

Run:
  ~/ugv_rpi/ugv-env/bin/python ~/robot/rover_joystick.py
  ... --debug   : print live axis/button indices (no serial), to verify/remap
"""
import os
import sys
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame

import rover_direct

# --- gamepad mapping (Xbox 360 pad on Linux/SDL2) --------------------------
AX_LX, AX_LY = 0, 1     # left stick: steer, throttle
AX_RX, AX_RY = 3, 4     # right stick: camera pan, tilt
BTN_A, BTN_X, BTN_Y = 0, 2, 3
BTN_RB, BTN_START = 5, 7
# --- tunables --------------------------------------------------------------
DEADZONE = 0.15
MAX_SPEED = 0.25        # normal wheel-speed cap (lowered to ease current draw)
TURBO_SPEED = 0.40      # while RB held
RAMP = 1.2              # max wheel-speed change per second (slew-rate limit):
                        # ramps speed gradually to avoid motor current spikes
                        # that can brown out / reset the Pi
PAN_RATE = 90.0         # deg/sec at full stick
TILT_RATE = 70.0
RATE_HZ = 25.0
# ---------------------------------------------------------------------------

DEBUG = "--debug" in sys.argv


def dz(v: float) -> float:
    return 0.0 if abs(v) < DEADZONE else v


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def open_pad():
    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        sys.exit("No gamepad found. Is the controller plugged into the rover?")
    js = pygame.joystick.Joystick(0)
    js.init()
    print(f"gamepad: {js.get_name()} "
          f"({js.get_numaxes()} axes, {js.get_numbuttons()} buttons)")
    return js


def debug_loop(js):
    print("DEBUG: move sticks/press buttons; Ctrl-C to quit.")
    dt = 1.0 / 10
    while True:
        pygame.event.get()   # drain the event queue (don't just pump)
        axes = [round(js.get_axis(i), 2) for i in range(js.get_numaxes())]
        btns = [i for i in range(js.get_numbuttons()) if js.get_button(i)]
        print(f"axes {axes}  buttons {btns}        ", end="\r", flush=True)
        time.sleep(dt)


def main():
    js = open_pad()
    if DEBUG:
        debug_loop(js)
        return

    keep_app = "--keep-app" in sys.argv
    if not keep_app and rover_direct.stop_http_service():
        print("stopped ugv_rpi/app.py to free the serial port.")
    rover = rover_direct.Rover()
    print(f"connected on {rover.port}. Left stick = drive, right stick = camera. "
          "Start button or Ctrl-C to quit.\n")

    pan, tilt = 0.0, 0.0
    left, right = 0.0, 0.0   # current wheel speeds (ramped toward target)
    light_on = False
    prev = {}
    dt = 1.0 / RATE_HZ
    last_cam = None
    try:
        while True:
            pygame.event.get()   # drain the event queue each frame, or the
                                 # joystick state freezes once the queue fills

            def pressed(b):  # rising edge
                now = js.get_button(b)
                fired = now and not prev.get(b, False)
                prev[b] = now
                return fired

            # buttons
            if pressed(BTN_START):
                break
            if pressed(BTN_Y):
                pan = tilt = 0.0
                rover.center_camera()
            if pressed(BTN_A):
                rover.stop()
            if pressed(BTN_X):
                light_on = not light_on
                rover.lights(255 if light_on else 0, 0)

            # driving from left stick (throttle + steer -> differential)
            top = TURBO_SPEED if js.get_button(BTN_RB) else MAX_SPEED
            throttle = -dz(js.get_axis(AX_LY))   # stick up = forward
            steer = dz(js.get_axis(AX_LX))
            tgt_left = clamp(throttle + steer, -1.0, 1.0) * top
            tgt_right = clamp(throttle - steer, -1.0, 1.0) * top
            # slew-rate limit: ramp toward target to avoid current spikes
            step = RAMP * dt
            left += clamp(tgt_left - left, -step, step)
            right += clamp(tgt_right - right, -step, step)
            rover.drive(left, right)

            # camera from right stick (integrate into absolute angles)
            pan += dz(js.get_axis(AX_RX)) * PAN_RATE * dt
            tilt += -dz(js.get_axis(AX_RY)) * TILT_RATE * dt
            pan = clamp(pan, -180.0, 180.0)
            tilt = clamp(tilt, -45.0, 90.0)
            now_cam = (round(pan, 1), round(tilt, 1))
            if now_cam != last_cam:
                rover.set_camera(pan, tilt)
                last_cam = now_cam

            time.sleep(dt)
    except KeyboardInterrupt:
        pass
    finally:
        rover.close()
        print("\nstopped.")


if __name__ == "__main__":
    main()
