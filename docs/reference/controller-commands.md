# rovercontrol — command reference

Everything the single-file Go controller (`rovercontrol`, on the rover at
`~/robot`, served on **`http://192.168.1.131:8080`**) understands: the HTTP API,
the gamepad controls, the CLI flags, and the on-rover operations.

---

## 1. HTTP API (the URL path *is* the command)

Base: `http://<rover-ip>:8080`. State-changing calls are **POST**; reads are
**GET**. Responses are small JSON (`{"ok":true,...}`). Malformed numbers → 400;
serial down → 503. CORS is open (any page/client can call it).

### Movement
| Method · path | Params | Does |
|---|---|---|
| `POST /move_forward` | `?ms=` (default 400) | nudge forward, auto-stop after `ms` |
| `POST /move_back` | `?ms=` | nudge backward |
| `POST /move_left` | `?ms=` | spin left |
| `POST /move_right` | `?ms=` | spin right |
| `POST /drive` | `?l=<-1..1>&r=<-1..1>` | analog continuous drive (scaled by speed cap); refresh to keep moving |
| `POST /stop` | — | stop the wheels |
| `POST /estop` | — | **emergency stop** — wheels + gimbal, latches until a new motion command |
| `POST /speed` | `?cap=<0..0.5>` | set the speed cap |
| `GET /speed` | — | read the speed cap |

### Camera gimbal (pan/tilt)
| Method · path | Params | Does |
|---|---|---|
| `POST /camera_up` / `/camera_down` | `?deg=` (default 15) | tilt up/down by `deg` |
| `POST /camera_left` / `/camera_right` | `?deg=` | pan left/right by `deg` |
| `POST /camera_center` | — | recenter (pan 0, tilt 0) |
| `POST /camera_aim` | `?pan=<-180..180>&tilt=<-45..90>` | aim to absolute angles |

### Lights & gimbal servos
| Method · path | Params | Does |
|---|---|---|
| `POST /light_head` | `?on=0\|1` (omit = toggle) | head/front LED |
| `POST /light_base` | `?on=0\|1` (omit = toggle) | base/chassis LED |
| `POST /gimbal_relax` | — | release the gimbal servos (hand-position the camera) |
| `POST /gimbal_lock` | — | hold the gimbal servos |

### Camera media
| Method · path | Does |
|---|---|
| `GET /video_feed` | live MJPEG stream (`<img src>` or browser) |
| `POST /snapshot` | save the latest frame → `{"ok":true,"name":"rover_….jpg"}` |
| `GET /photos` | `{"photos":[…]}` newest-first |
| `GET /latest` | `{"count":N,"latest":"…jpg"}` (used by the gallery auto-refresh) |
| `GET /photos/<name>` | serve a photo file |
| `POST /delete_photo/<name>` | delete a photo |

### Meta
| Method · path | Does |
|---|---|
| `GET /` | the built-in web UI (drive/camera/lights/video/gallery + browser gamepad) |
| `GET /healthz` | `{"ok":true,"serial":{up,err},"camera":{up,err},"gamepad":{up,mapping}}` |

### curl examples
```bash
B=http://192.168.1.131:8080
curl -X POST "$B/move_forward?ms=300"      # brief forward nudge
curl -X POST "$B/drive?l=0.2&r=0.2"        # analog drive (send repeatedly)
curl -X POST "$B/stop"                      # stop
curl -X POST "$B/estop"                     # emergency stop
curl -X POST "$B/camera_aim?pan=-30&tilt=20"
curl -X POST "$B/light_head?on=1"          # head light on
curl -X POST "$B/snapshot"                  # take a photo
curl -s "$B/healthz"                         # status
# live video: open  http://192.168.1.131:8080/video_feed  in a browser
```

---

## 2. Gamepad controls

Two ways to use a gamepad; both go through the same server-side safety
(clamps, arbitration, 0.5 s drive watchdog).

### A) Plugged into the rover (Pi) — read by the controller
| Control | Action |
|---|---|
| **Left stick** | drive (throttle + steer) |
| **Right stick** | pan / tilt the camera |
| **D-pad ↑ / ↓** | raise / lower the speed cap |
| **A** | stop wheels |
| **Back** | e-stop (wheels + gimbal) |
| **B** | snapshot |
| **X** | head light · **LB** base light |
| **Y** | center camera |
| **L3 / R3** | relax / lock gimbal |
| **RB (hold)** | turbo |
| **LT (hold)** ※ | precision / slow mode |
| **RT (hold)** ※ | boost (max speed) |
| **Start** ※ | **instant e-stop** (extra panic button) |
| **D-pad ← / →** ※ | fine camera pan |

※ The marked controls are **disabled until you run `-calibrate`** (see §4). The
unmarked controls work out of the box but their exact button/axis numbers may
need calibration to match your specific pad.

### B) Plugged into your Mac — via the browser
Open `http://192.168.1.131:8080` in **Chrome**, **press any button** to activate
the gamepad (browser requirement), then the same layout applies. Releasing the
stick, backgrounding the tab, or unplugging the pad stops the rover.

---

## 3. CLI flags (`rovercontrol-arm64 [flags]`)

| Flag | Default | Meaning |
|---|---|---|
| `-port` | `8080` | HTTP listen port |
| `-photos` | `<exe-dir>/photos` | photo directory |
| `-serial` | `/dev/ttyAMA0` | ESP32 serial device |
| `-gamepad` | `/dev/input/js0` | joystick device (`''` to disable) |
| `-gamepad-map` | `<exe-dir>/gamepad.json` | gamepad mapping file (uses defaults if absent) |
| `-camera-mode` | `auto` | `auto` \| `v4l2` (USB) \| `rpicam` (CSI) |
| `-camera-device` | `/dev/video0` | V4L2 device for `v4l2` mode |
| `-width` / `-height` | `1280` / `720` | camera resolution (`0` = let the camera choose) |
| `-fps` | `30` | camera fps (rpicam only) |
| `-gamepad-debug` | off | print live gamepad axis/button numbers, then exit |
| `-calibrate` | off | guided gamepad calibration wizard, then exit |

---

## 4. On-rover operations

```bash
ssh rover                      # = ws@192.168.1.131

# start / restart the controller (it also auto-starts on boot via @reboot)
pkill -f rovercontrol-arm64
cd ~/robot
setsid nohup ./rovercontrol-arm64 -photos ~/robot/photos >> /tmp/rovercontrol.log 2>&1 </dev/null &

# see what a gamepad reports (no driving — just reads input)
./rovercontrol-arm64 -gamepad-debug

# calibrate the gamepad mapping (writes ~/robot/gamepad.json), then restart
./rovercontrol-arm64 -calibrate

# logs
tail -f /tmp/rovercontrol.log
```

**Deploy a new build (from the Mac):**
```bash
cd rovercontrol && GOOS=linux GOARCH=arm64 go build -o ../rovercontrol-arm64 . && cd ..
rsync -z rovercontrol-arm64 rover:robot/      # then restart on the rover (above)
```

---

## Notes
- One process owns the serial port **and** the camera, so the stock ugv_rpi
  `app.py` must not run alongside it (its `@reboot` autostart is disabled).
- The rover's camera is a **USB** camera (`/dev/video0`, MJPG); `-camera-mode auto`
  picks the `v4l2` backend for it.
- Full inventory of the retired stock app: `docs/reference/app.py-superseded.md`.
