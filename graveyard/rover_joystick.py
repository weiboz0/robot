#!/usr/bin/env python3
# SUPERSEDED by rovercontrol (single-file Go controller, docs/plans/002), which
# reads the gamepad itself. Kept as the reference gamepad mapping until the Go
# joystick is verified on the Pi, then removed (see docs/plans/002 cleanup).
"""Drive the rover with a USB gamepad plugged into the rover — direct serial.

Runs ON the rover. Uses rover_direct (UART control) so it needs no HTTP service.
Left stick drives the wheels, right stick aims the camera.

  Left stick    : throttle (up/down) + steering (left/right)
  Right stick   : camera pan / tilt
  RB (hold)     : turbo (higher top speed)
  D-pad up/down : raise / lower the speed cap
  A button      : stop wheels
  B button      : take a photo (saved to ./photos)
  X button      : toggle head light
  Y button      : center camera
  LB button     : toggle base (chassis) light
  Back button   : emergency stop (wheels + gimbal)
  L3 (click L)  : relax gimbal (release servos to hand-position the camera)
  R3 (click R)  : lock gimbal
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
import rover_camera

# --- gamepad mapping (Xbox 360 pad on Linux/SDL2) --------------------------
AX_LX, AX_LY = 0, 1     # left stick: steer, throttle
AX_RX, AX_RY = 3, 4     # right stick: camera pan, tilt
BTN_A, BTN_B, BTN_X, BTN_Y = 0, 1, 2, 3
BTN_LB, BTN_RB = 4, 5
BTN_BACK, BTN_START = 6, 7
BTN_L3, BTN_R3 = 9, 10
# --- tunables --------------------------------------------------------------
DEADZONE = 0.15
SPEED_STEPS = [0.15, 0.20, 0.25, 0.30, 0.40]   # selectable normal speed caps
SPEED_START = 2          # index into SPEED_STEPS (-> 0.25)
TURBO_SPEED = 0.40      # while RB held
RAMP = 1.2              # max wheel-speed change per second (slew-rate limit):
                        # ramps speed gradually to avoid motor current spikes
                        # that can brown out / reset the Pi
PAN_RATE = 90.0         # deg/sec at full stick
TILT_RATE = 70.0
RATE_HZ = 25.0
# ---------------------------------------------------------------------------

CONTROLS = """controls:
  left stick    drive (throttle + steer)      RB (hold)  turbo
  right stick   camera pan / tilt             D-pad U/D  speed cap +/-
  A  stop wheels        B  take photo         X  head light    Y  center camera
  LB base light         Back  E-STOP          L3 relax gimbal  R3 lock gimbal
  Start / Ctrl-C  quit
"""

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
    print(f"connected on {rover.port}.\n" + CONTROLS)

    pan, tilt = 0.0, 0.0
    left, right = 0.0, 0.0   # current wheel speeds (ramped toward target)
    head_on = False          # X -> head light
    base_on = False          # LB -> base/chassis light
    speed_idx = SPEED_START
    estopped = False         # Back latches a hard stop until sticks recenter
    prev = {}
    prev_hat = (0, 0)
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
                head_on = not head_on
                rover.lights(255 if head_on else 0, 255 if base_on else 0)
                print(f"head light {'on' if head_on else 'off'}        ")
            if pressed(BTN_LB):
                base_on = not base_on
                rover.lights(255 if head_on else 0, 255 if base_on else 0)
                print(f"base light {'on' if base_on else 'off'}        ")
            if pressed(BTN_B):
                path = rover_camera.take_photo()
                print(f"photo -> {path}        " if path
                      else "no camera tool (rpicam-still) found        ")
            if pressed(BTN_BACK):
                left = right = 0.0
                estopped = True
                rover.estop()
                print("EMERGENCY STOP (recenter sticks to resume)        ")
            if pressed(BTN_L3):
                rover.servo_torque(False)
                print("gimbal relaxed (hand-position it)        ")
            if pressed(BTN_R3):
                rover.servo_torque(True)
                print("gimbal locked        ")

            # D-pad (hat): up/down change the speed cap (rising-edge per nudge)
            hat = js.get_hat(0) if js.get_numhats() else (0, 0)
            if hat[1] != 0 and prev_hat[1] == 0:
                speed_idx = clamp(speed_idx + (1 if hat[1] > 0 else -1),
                                  0, len(SPEED_STEPS) - 1)
                print(f"speed cap -> {SPEED_STEPS[speed_idx]:.2f}        ")
            prev_hat = hat

            # driving from left stick (throttle + steer -> differential)
            top = TURBO_SPEED if js.get_button(BTN_RB) else SPEED_STEPS[speed_idx]
            throttle = -dz(js.get_axis(AX_LY))   # stick up = forward
            steer = dz(js.get_axis(AX_LX))
            if estopped:
                if throttle == 0.0 and steer == 0.0:
                    estopped = False             # sticks centered -> release
                else:
                    throttle = steer = 0.0       # hold stop until recentered
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
