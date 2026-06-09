# rover_direct.py — command reference

Direct serial control of the Waveshare UGV rover (motors + camera), no HTTP.
Runs on the rover. Optional `[ARG]` items have defaults; `ARG` items are required.

## Launch modes

| Invocation | Function |
|---|---|
| `rover` | Interactive prompt (stops the web app, inits the base, opens `rover>`). |
| `rover demo` | Self-test: camera sweep **and** a small drive/spin. |
| `rover camtest` | Camera-only slow sweep (no driving). |
| `rover --keep-app` | Don't stop the web app first (use only if the serial port is already free). |

(`rover` = `~/bin/rover`, which runs `~/ugv_rpi/ugv-env/bin/python ~/rover_direct.py`.)

## Interactive commands

### Camera (gimbal)
| Command | Function |
|---|---|
| `up [DEG]` | Tilt camera up `DEG` degrees (default 15). |
| `down [DEG]` | Tilt camera down `DEG` degrees (default 15). |
| `left [DEG]` | Pan camera left `DEG` degrees (default 15). |
| `right [DEG]` | Pan camera right `DEG` degrees (default 15). |
| `cam PAN TILT` | Aim to absolute angles. pan −180..180, tilt −45..90 (+ = up). |
| `center` | Level the camera (pan 0, tilt 0). |

### Motors
| Command | Function |
|---|---|
| `drive L R [SECS]` | Wheel speeds `L`,`R` (−0.5..0.5) for `SECS` seconds (default 1), then auto-stop. |
| `move L R` | Wheel speeds `L`,`R` continuously — no auto-stop (use `stop`). |
| `fwd [SECS]` | Drive forward (speed 0.2) for `SECS` (default 1), then stop. |
| `back [SECS]` | Drive backward for `SECS` (default 1), then stop. |
| `spinl [SECS]` | Spin left in place for `SECS` (default 0.6), then stop. |
| `spinr [SECS]` | Spin right in place for `SECS` (default 0.6), then stop. |
| `stop` | Stop the wheels. |

### Other
| Command | Function |
|---|---|
| `estop` | Stop wheels **and** gimbal immediately. |
| `relax` | Release the gimbal servos so you can hand-position the camera. |
| `lock` | Re-lock the gimbal servos. |
| `light F B` | LED brightness, PWM 0..255. `F`=front/head, `B`=base/chassis. |
| `oled LINE TEXT...` | Write `TEXT` to OLED line `LINE` (0–3). |
| `oledclear` | Restore the OLED's default status screen. |
| `demo` | Run the motor+camera self-test. |
| `help` | Print the command list. |
| `quit` / `exit` | Exit (releases the serial port). |

## Python API (for scripting / import)

```python
from rover_direct import Rover
r = Rover()                  # opens serial, runs init_base() (echo off, gimbal module)
```

| Method | Function |
|---|---|
| `Rover(port=None, baud=115200, init=True)` | Connect. Auto-detects port (Pi5 `/dev/ttyAMA0` else `/dev/serial0`). |
| `send(cmd: dict)` | Send one raw JSON command. |
| `init_base()` | Echo off, feedback stream off, select gimbal module. |
| `select_module(module)` | 0:None 1:RoArm 2:Gimbal. |
| `drive(left, right)` | Set wheel speeds (continuous; clamped ±0.5). |
| `stop()` | Stop wheels. |
| `drive_for(left, right, seconds)` | Drive then stop (seconds clamped 0..10). |
| `forward(speed=0.2, seconds=1.0)` | Drive straight forward. |
| `backward(speed=0.2, seconds=1.0)` | Drive straight back. |
| `spin_left(speed=0.2, seconds=0.6)` | Turn in place left. |
| `spin_right(speed=0.2, seconds=0.6)` | Turn in place right. |
| `set_camera(pan, tilt, speed=0, acc=0)` | Aim gimbal to absolute angles (clamped). |
| `gimbal_continuous(pan, tilt, speed=200)` | Move gimbal at a velocity (T:141). |
| `center_camera()` | Level the camera. |
| `gimbal_stop()` | Stop gimbal motion (T:0). |
| `estop()` | Stop wheels and gimbal. |
| `lights(front=0, base=0)` | LED PWM 0..255. |
| `oled(line, text)` | Write OLED line 0–3. |
| `oled_default()` | Restore default OLED screen. |
| `servo_torque(lock: bool, servo_id=255)` | Lock/release bus servos (255 = all). |
| `close()` | Stop and close the serial port. |

## Limits / conventions
- Pan: −180°..180° (0 = forward). Tilt: −45°..90° (+ = up, 0 = level).
- Wheel speed clamped to ±0.5; `drive_for`/timed moves capped at 10 s.
- Only one process can own the serial port — `rover` stops the web app on start;
  restore it by rebooting or rerunning `app.py`.

## Raw ESP32 command codes (what gets sent over serial)
| Code | Command |
|---|---|
| `{"T":1,"L":l,"R":r}` | Wheel speeds |
| `{"T":133,"X":pan,"Y":tilt,"SPD":s,"ACC":a}` | Gimbal absolute aim |
| `{"T":141,"X":pan,"Y":tilt,"SPD":s}` | Gimbal continuous |
| `{"T":0}` | Gimbal emergency stop |
| `{"T":132,"IO4":base,"IO5":front}` | Lights |
| `{"T":3,"lineNum":n,"Text":t}` | OLED write |
| `{"T":-3}` | OLED default screen |
| `{"T":210,"id":id,"cmd":1/0}` | Servo torque lock/release |
| `{"T":4,"cmd":0/1/2}` | Select module (None/RoArm/Gimbal) |
| `{"T":143,"cmd":0}` | Serial echo off |
| `{"T":131,"cmd":0/1}` | Feedback stream off/on |
