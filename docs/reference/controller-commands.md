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
| `POST /scan` | start a 3D room scan (gimbal sweep → panorama built on the Pi); `409` while one runs or the wheels are moving; e-stop or any drive input aborts it |
| `GET /scans` | `{"scans":[…]}` archived 3D scans, newest first (every successful scan is kept) |
| `GET /scans/<name>` | serve an archived scan (`scan_YYYYmmdd_HHMMSS[_N].jpg`) |
| `POST /delete_scan/<name>` | delete an archived scan |
| `GET /pose` | dead-reckoned `{"x","y","heading","pan","tilt","battery_v","fresh"}` in self-set coordinates (wheel-encoder odometry; shown in the page's top-right badge); always `200` — `fresh:false` when telemetry is stale |
| `POST /pose_reset` | set the current spot as origin (0,0), heading 0° |
| `POST /scan_cancel` | abort a running scan and discard it (⏹ button / gamepad STOP-SCAN, default button 8); `409` when idle or already publishing |
| `POST /chat` | submit a chatbot turn `{"text":…}` → `{"turn":N}` immediately (`409 busy` while one runs); the chat service (agent_chat `--serve`, loopback :8090) runs it |
| `GET /chat_poll?turn=N` | `{"done":false}` or `{"done":true,"reply":…}` — the page polls this; nothing ever blocks on the LLM |
| `GET /chat_status` | chat service health (always `200`; `{"up":false}` when not running) |
| `POST /chat_start` | launch the chat service detached (logs → `~/rover-chat.log`); `409` if already up |
| `GET /pano_meta` | objects identified in the live panorama `{"objects":[{name,color,lon,lat,w,h}]}` (404 if none) |
| `GET /scan_meta/<name>` | objects identified in an archived scan (the 3D viewer draws these as boxes; `boxes on|off|all|<names>` in the web command box controls them) |
| `POST /scan_identify/<name>[?focus=…]` | identify objects in a SAVED scan (202; runs in the background; 🔍 button per scan card; the chatbot's `rover_identify_scan` tool uses it — "add a box for the books on the 2nd-last 3D view") |
| `GET/POST /auto_flash[?on=0\|1]` | the chatbot auto-flashlight kill switch (🔦 button); when off, the chatbot may never enable lights automatically; persists across restarts |

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
| **Start** | 3D scan (room panorama; e-stop or driving aborts it) |
| **Guide/Select (btn 8)** | stop a running 3D scan and discard it |
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

## 5. Chatbot exposure (`agent_chat.py`)

The Python chatbot reaches the rover through `RoverCtl` (auto-detecting serial /
rovercontrol / app.py) and now covers the controller's full **control** surface —
as LLM tools and `$`-commands:

| Capability | LLM tool | `$` command |
|---|---|---|
| drive (auto-stop), stop, e-stop | `rover_drive` / `rover_stop` / `rover_estop` | `drive` `fwd` `back` `spinl` `spinr` `stop` `estop` |
| camera aim / center | `rover_set_camera` / `rover_center_camera` | `cam` `up` `down` `left` `right` `center` |
| lights | `rover_lights` | `light` |
| gimbal lock / relax | `rover_gimbal_torque` | `relax` `lock` |
| **speed cap** (`/speed`) | `rover_set_speed` | `speed [CAP]` |
| **status** (`/healthz`) | `rover_get_status` | `status` |
| photo + **list** (`/snapshot`, `/photos`) | `rover_photo` / `rover_list_photos` | `photo` `photos` |
| OLED (serial/app.py only) | `rover_oled` | `oled` `oledclear` |

Deliberately **not** LLM tools: continuous `move` (no auto-stop; serial/app.py have
no server watchdog — `$move` stays human-only), and photo *delete*/byte-fetch
(gallery work — that's `rover_web.py`). The speed cap is the safe global throttle;
on rovercontrol it is **shared with the gamepad**, so it isn't exclusively the
chatbot's.

### Name parity (plan 019) — both surfaces accept both vocabularies

| Command | Chatbot `$` | Website box | Note |
|---|---|---|---|
| `up/down/left/right [DEG]` = `camera_up/...` | ✅ both | ✅ both | camera nudge (bare `left/right` = CAMERA everywhere; wheels = `spinl/spinr`/`move_*`) |
| `cam P T` = `camera_aim P T` | ✅ both | ✅ both | |
| `center` = `camera_center` | ✅ both | ✅ both | |
| `photo` = `snapshot`/`snap` | ✅ both | ✅ both | |
| `relax`/`lock` = `gimbal_relax`/`gimbal_lock` | ✅ both | ✅ both | |
| `light_head`/`light_base [on\|off]` | ✅ both | ✅ both | single channel; no arg = toggle |
| `light F B` | ✅ (PWM 0-255) | ✅ (degrades: >0 = on) | |
| `move_forward/back/left/right [MS]` | ✅ both | ✅ both | bounded nudge; serial emulates at full-cap for MS |
| `spinl/spinr [S]` | ✅ (gentle 0.2) | ✅ (→ `move_*`, **at the cap** — brisker) | duration matches; magnitude differs |
| `speed CAP` / `stop` / `estop` | ✅ both | ✅ both | |

**Same word, DIFFERENT meaning (deliberately not remapped):**
- `drive` — chatbot: `L R [seconds]`, speeds −0.5..0.5, auto-stop after seconds;
  website: `l r` normalized −1..1, ONE ~0.5 s watchdog pulse.
- `fwd`/`back` — chatbot: drive straight for **seconds**; website: alias of
  `move_forward/move_back` in **ms**.
- `move` — chatbot: **continuous** until `stop`; website: alias of `drive` = one pulse.

**Chatbot-only** (no controller endpoint / Python-only): `oled`, `oledclear`,
`demo`, `status`, `photos`, `find <obj>`/`screwdriver`, `dobot`.

## Notes
- One process owns the serial port **and** the camera, so the stock ugv_rpi
  `app.py` must not run alongside it (its `@reboot` autostart is disabled).
- The rover's camera is a **USB** camera (`/dev/video0`, MJPG); `-camera-mode auto`
  picks the `v4l2` backend for it.
- Full inventory of the retired stock app: `docs/reference/app.py-superseded.md`.
