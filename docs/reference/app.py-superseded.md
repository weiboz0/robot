# app.py — SUPERSEDED reference

> ⚠️ **SUPERSEDED / basically useless.** The stock Waveshare `ugv_rpi/app.py`
> (and its `base_ctrl.py` / `cv_ctrl.py`) is **no longer used** for controlling
> this rover — `rovercontrol` (the single-file Go HTTP controller, plan 002)
> replaces it for movement, lights, camera (video + snapshots), and the
> joystick. `app.py` is **kept on the rover for reference only**, not deleted.
> Its `@reboot` autostart is disabled so it doesn't fight rovercontrol for the
> camera/serial. This file is a frozen inventory of what it did, so we never
> have to re-derive its surface.

## Provenance

Fetched from the rover (`~/ugv_rpi/`) on 2026-06-13; SHA-256 of the analyzed
copies (in case the rover's files drift later):

| file | sha256 |
|---|---|
| app.py | `a933d5d537161a4fc8bebafa3a8b3b48005219c1bc6589baa4ff0c7487e2ae37` |
| base_ctrl.py | `7d20b18f0a457d61711e34945dd2fe5b9cfd292827b1a91e85ce6e34ca95bd57` |
| cv_ctrl.py | `4625a8a9a03d2172945f995dca2c07128802153d69da30fe8c72edd108b00735` |

Inventory produced with codex over those files and spot-verified against the
source lines cited below.

## Flask routes

| route | purpose |
|---|---|
| `GET /` | render index.html, play "connected" audio (app.py:149) |
| `GET /config` | return raw config.yaml (app.py:154) |
| `GET /<path:filename>` | serve files from templates (app.py:161) |
| `GET /get_photo_names` | list saved pictures (app.py:165) |
| `POST /delete_photo` | delete a picture by form filename (app.py:170) |
| `GET /videos/<filename>` | serve saved videos (app.py:180) |
| `GET /get_video_names` | list saved .mp4 videos (app.py:184) |
| `POST /delete_video` | delete a video (app.py:193) |
| `POST /offer` | WebRTC offer handler (app.py:421) |
| `GET /video_feed` | MJPEG camera stream (app.py:426) |
| `POST /send_command` | text command-line gateway (app.py:430) |
| `GET /getAudioFiles` | list uploaded audio (app.py:441) |
| `POST /uploadAudio` | upload audio (app.py:446) |
| `POST /playAudio` | play selected audio (app.py:458) |
| `POST /stop_audio` | stop audio (app.py:465) |
| `GET /settings/<filename>` | serve files from templates (app.py:470) |

## Socket.IO

- `/json` event `json` → forwards raw JSON to the serial command queue
  (`base.base_json_ctrl`, app.py:477)
- `/ctrl` event `message` → parses `"A"` field, dispatches a UI action from
  `cmd_actions` (app.py:553)

## `/ctrl` UI actions (the `"A"` codes)

Camera zoom x1/x2/x4 (app.py:73); still picture capture (app.py:77); start/stop
video recording (app.py:78); CV modes — none, motion, face, object, color,
MediaPipe hand, auto-drive line-following, MediaPipe face, MediaPipe pose
(app.py:81); detection reactions none/photo/record (app.py:91); motion/gimbal
lock & unlock (app.py:95); head light off/auto/on (app.py:98); servo release,
set pan/tilt servo ID, set servo midpoint (app.py:102); base light off/on/toggle
and head-light toggle (app.py:107).

## `/send_command` command-line gateway

`base -c JSON` (arbitrary rover JSON passthrough, app.py:271); `base -r on/off`
(received-info overlay, app.py:275); `audio -s/-v/-p` (speech/volume/play,
app.py:281); ESP-NOW `send -a/-rm/-b/-g`/MAC send (app.py:289); `cv -r/-s`
(target colour, app.py:307); `video -q` (MJPEG quality, app.py:331);
`line -r/-s` (line-tracking colour/params, app.py:339); `track` (pan/tilt track
params, app.py:371); `timelapse -s/-e` (app.py:374); `p`/`s` (product
version/config, app.py:389); `test` (inject test base data, app.py:416).

## Serial JSON commands (base_ctrl.py / cv_ctrl.py)

E-stop `{"T":0}` (base_ctrl.py:189); motors `{"T":1,"L":,"R":}` (base_ctrl.py:194);
gimbal `{"T":133,"X":,"Y":,"SPD":,"ACC":}` (base_ctrl.py:199); gimbal
`{"T":141,...}` (base_ctrl.py:204); OLED `{"T":3,...}` / `{"T":-3}`
(base_ctrl.py:209/214); servo id/torque/midpoint (base_ctrl.py:219/225/231);
lights `{"T":132,"IO4":,"IO5":}` (base_ctrl.py:237); version `{"T":900,...}`
(app.py:251); ESP-NOW T303–306 (app.py:289); boot setup T142/131/143/4/300
(app.py:571); arm pose (app.py:599); CV gimbal-track `{"T":133,...}`
(cv_ctrl.py:400); CV auto-drive `{"T":13,"X":,"Z":}` (cv_ctrl.py:779);
timelapse drive `{"T":1,...}` (cv_ctrl.py:972).

## The four capabilities (all SUPPORTED in app.py — now provided by rovercontrol)

| capability | app.py evidence | rovercontrol replacement |
|---|---|---|
| Movement | `{"T":1,"L":,"R":}` base_ctrl.py:194 | `POST /move_*`, `/drive`, joystick |
| Lights | `{"T":132,...}` base_ctrl.py:237 | `POST /light_head`, `/light_base` |
| Camera video | `GET /video_feed` MJPEG app.py:426 | `GET /video_feed` (rpicam-vid) |
| Camera picture | `pic_cap` → `cv2.imwrite` cv_ctrl.py:220 | `POST /snapshot` |

## Intentionally NOT carried into rovercontrol (non-goals)

WebRTC `/offer`; audio upload/playback/TTS; all CV modes (motion/face/object/
colour/hand/pose detection, line-following auto-drive); **video recording**
(`.mp4`); OLED boot text/versioning; ESP-NOW mesh; servo ID/midpoint config;
websocket telemetry overlay; timelapse missions; Jupyter. These are deliberate
drops (plan 002 "Non-goals"); revisit individually if ever needed.
