# 011 - Chatbot drives the Go rovercontrol (:8080); Go owns the joystick

## Goal

Standardize the rover on the **Go `rovercontrol`** (it already does camera + the
Pi-attached joystick + drive, and its lag/stall fixes are in `main`). The **Python chatbot**
(`agent_chat.py`) runs on the computer and drives the rover through rovercontrol's **`:8080`
HTTP API**. Retire the Python joystick programs — the Go controller is the joystick.

## Decisions (user)

- Joystick = Go `rovercontrol` (works; reads `/dev/input/js0` on the Pi).
- Chatbot = Python on the computer, talking to rovercontrol's `:8080` API.
- Remove `rover_gamepad.py` + `rover_joystick.py` (Go is the joystick).

## rovercontrol :8080 API (confirmed from rovercontrol.go)

Query-param POSTs: `POST /drive?l=&r=` (continuous), `POST /stop`, `POST /estop`,
`POST /camera_aim?pan=&tilt=`, `POST /camera_center`, `POST /light_head?on=` /
`POST /light_base?on=` (omit `on` → toggle; `on=0|255` → set), `POST /gimbal_relax` /
`POST /gimbal_lock`, `POST /snapshot`, `GET /video_feed`, `GET /healthz`.

## Design

1. **New `rovercontrol_client.py`** — HTTP client to `:8080`, query-param POSTs (`urllib`):
   - `move(l,r)`→`/drive?l=&r=`; `stop`→`/stop`; `estop`→`/estop`;
     `set_camera(pan,tilt)`→`/camera_aim?pan=&tilt=`; `center`→`/camera_center`;
     `servo_torque(lock)`→`/gimbal_lock` | `/gimbal_relax`.
   - **`drive(l,r,sec)` must REFRESH** (codex): `/drive` is watchdog-managed (~500 ms), so a
     single call + long sleep stops early. Re-POST `/drive` at ~3 Hz for `sec`, then `/stop`.
   - **`lights(front,base)` degrades to on/off** (codex): Go `?on=` is boolean (0=off,
     nonzero=on, omitted=toggle). **Always send `on=0|1`** (never omit → never accidental
     toggle): `on=1 if front>0 else 0`, same for base. Brightness is on/off only here.
   - `set_host`/`set_timeout` like `rover_client`.

2. **`rover_backend.py`** — add a third backend mode **`"rovercontrol"`** (HTTP :8080).
   `RoverCtl` dispatches to `rovercontrol_client` for that mode. **Full surface (codex):**
   - `photo()` → grab one frame from `:8080/video_feed` locally (extend
     `rover_camera.take_photo` with a `port=` arg; default 5000 unchanged).
   - `oled()`/`oled_default()` → **explicitly unsupported** in rovercontrol mode (raise a
     clear error / return a "not supported" message) — the Go controller has no OLED endpoint;
     don't pretend parity. `agent_chat`'s OLED tool surfaces that message.
   - `detect_rover()` order: **serial** (on the Pi, if the port opens — a failed open falls
     through, never fatal; serial mode stops only legacy `app.py`, never `rovercontrol`) →
     **rovercontrol** (`GET :8080/healthz` parses OK **and `serial.up == true`** — reachable
     but serial-down must NOT be selected) → **http/app.py** (`:5000` legacy fallback) → None.
   agent_chat.py is unchanged (uses `RoverCtl`).

3. **Remove the Python joysticks** → `graveyard/`: `rover_gamepad.py`, `rover_joystick.py`,
   and `tests/test_gamepad.py`. (`rover_client.py` stays as the app.py fallback.)

4. **Docs** — README/CLAUDE.md: joystick = Go rovercontrol; chatbot auto-detects and prefers
   `:8080`. Note: with rovercontrol running, the live view + gallery are rovercontrol's own
   `http://<rover>:8080/` (app.py's `:5000` and `rover_web.py`'s embed are not used then).

## Tests (`tests/`, unittest)

- **`test_rovercontrol_client.py`** (new): each method POSTs to the right `:8080` URL with
  the right query params (monkeypatch `urllib.request.urlopen` to capture URL); `set_host`
  changes the target; clamping.
- **`test_backend.py`** (extend): `connect()` selects `"rovercontrol"` when `:8080/healthz`
  reports `serial.up=true`; **falls through to `:5000` when `:8080` is reachable but
  `serial.up=false`** (codex); `RoverCtl("rovercontrol")` dispatch maps to
  `rovercontrol_client`; detection order serial → rovercontrol → app.py.
- Lights: `on=0|1` always present, never the toggle (omitted) form (codex).
- `photo`/`oled` behavior in rovercontrol mode is pinned (photo grabs `:8080`; oled errors).
- Remove `test_gamepad.py` with the joystick files.
- `ci-local.sh` green (Go untouched).

## Risks / tradeoffs

- **Deploy needed**: rovercontrol must run on the rover (build arm64 + scp + start; disable
  the app.py cron, enable rovercontrol). The lag/stall fix is already in `main`.
- **app.py retired** for control; `rover_client.py`/`:5000` kept only as a detection
  fallback. `rover_web.py`'s live view embeds `:5000`, so it's redundant with rovercontrol's
  `:8080` UI when rovercontrol runs — left in place but noted.
- Wi-Fi link is still the latency ceiling (unchanged).

---

## Plan review

**Gate: PASSED** after revisions (Opus + codex; glm not part of plan gate).

**codex (GPT-5.5) — 3 blocking, all folded in:**
1. Detect rovercontrol via `GET /healthz` and only select it when `serial.up == true`
   (reachable-but-serial-down would 503 every control call) — else fall through to `:5000`.
2. Preserve the full `RoverCtl` surface: `photo()` grabs from `:8080/video_feed`
   (`rover_camera` gains a `port=` arg); `oled()`/`oled_default()` are explicitly unsupported
   in rovercontrol mode (Go has no OLED endpoint) rather than faking parity.
3. Light `?on=` is boolean — always send `on=0|1` (never omit → never accidental toggle);
   brightness degrades to on/off in rovercontrol mode.
   Nits: serial-open failure must fall through (never fatal); never stop `rovercontrol` during
   detection; `drive(l,r,sec)` must refresh `/drive` (watchdog ~500 ms); add negative tests
   (serial-down fall-through, lights on=0/1, photo/oled pinned).

## Code review

**Gate: PASSED** after revisions (Opus + codex; glm not installed → 2-way).

**codex (GPT-5.5) — 2 blocking, both fixed:**
1. *Drive double-capping* — `/drive` takes **normalized −1..1** (Go scales by its speed cap),
   but the client clamped to ±0.5, making chatbot speeds far too slow. Fixed: `_norm()` maps
   the chatbot's −0.5..0.5 → ±1 (×2, clamped); rovercontrol's own speed cap governs absolute
   top speed (documented).
2. *Short-duration `drive()` overshoot* — fixed 0.3 s sleep overshot and re-posted after the
   deadline. Fixed: initial post + refresh only while in-window, sleep bounded by remaining
   time, never a fresh nonzero after the deadline.
   Nits (detection probe timeout hardcoded; OLED/lights mapping) — lights/OLED confirmed
   correct; left detection probes at a fixed 2 s (the `timeout` arg is the per-request control
   timeout, not the detection probe).

## Post-execution report

**Branch:** `feature/unified-rover-backend` (carries 009 + 010 + 011).

### What shipped
- **`rovercontrol_client.py`** (new): Python client for the Go controller's `:8080` API —
  `/drive` (normalized, watchdog-refreshed), `/camera_aim`, `/light_*` (PWM→on/off),
  `/gimbal_*`, `/stop`, `/estop`, `/camera_center`, `/healthz`.
- **`rover_backend.py`**: `RoverCtl` now has **3 modes** (serial / rovercontrol / app.py).
  `detect_rover()` order: serial → **rovercontrol** (`/healthz` with `serial.up`) → app.py
  `:5000`. OLED raises `NotImplementedError` on rovercontrol (no Go endpoint); `photo()` grabs
  from the active backend's `:port/video_feed`.
- **Removed** the Python joysticks → `graveyard/`: `rover_gamepad.py`, `rover_joystick.py`,
  `test_gamepad.py`. The **Go controller is the joystick** (reads the Pi gamepad on `:8080`).
- Docs: README + CLAUDE.md updated (joystick = Go; chatbot prefers `:8080`).

### Tests / CI
- New `tests/test_rovercontrol_client.py` (endpoint/param mapping, normalization, lights
  on/off, drive refresh) and rovercontrol cases in `tests/test_backend.py` (healthz-gated
  selection, serial-down fall-through, dispatch, OLED unsupported). `ci-local.sh`: **PASS** —
  45 Python tests, Go untouched.

### To actually use it
- **Deploy the Go controller** to the rover (build `rovercontrol-arm64`, scp, run; disable the
  app.py cron, enable rovercontrol). Its lag/stall fix is already in `main`.
- Then the gamepad works on the Pi, and `python agent_chat.py` on a computer auto-detects and
  drives via `:8080`.

### Tradeoffs
- rovercontrol absolute top speed = its speed cap (≈0.25 default), not the serial ±0.5 — the
  cap is gamepad-adjustable; documented.
- `app.py` `:5000` kept only as a detection fallback; `rover_web.py`'s `:5000` live-view embed
  is redundant with rovercontrol's `:8080` UI when rovercontrol runs.
