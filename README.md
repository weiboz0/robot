# rover

Tools for a Waveshare UGV Rover (Raspberry Pi + ESP32 sub-controller).

## Chatbot (`chatbot.py`)
A terminal chatbot that chats and drives the rover via natural language. Works
with OpenAI-compatible providers (ARK / DashScope / OpenCode) selected from
`.env`. Rover actions are exposed as tools (`set_camera`, `drive`, `stop`) that
POST to the rover's web app.

```bash
python3 -m venv .venv && ./.venv/bin/pip install openai
cp .env.example .env   # then fill in keys
./.venv/bin/python chatbot.py
```

Config lives in `.env` (not committed): provider keys, `PROVIDER`, and per-provider
`*_MODEL` / `*_BASE_URL`.

## Helpers
- `list_models.py <provider>` — list a provider's model catalog.
- `list_ark_endpoints.py` — list your ARK inference endpoints (`ep-…`); needs
  `VOLC_ACCESSKEY` / `VOLC_SECRETKEY`.

## Direct control (`rover_direct.py`)
Runs **on the rover** and controls motors + camera straight over the UART
(no HTTP service). It stops the web app to take the serial port and selects the
gimbal module so pan/tilt works.

```bash
# on the rover:
~/ugv_rpi/ugv-env/bin/python ~/rover_direct.py          # interactive
~/ugv_rpi/ugv-env/bin/python ~/rover_direct.py demo     # self-test
```

ESP32 command reference: motors `{"T":1,"L":..,"R":..}`, gimbal
`{"T":133,"X":pan,"Y":tilt,"SPD":0,"ACC":0}`, module-select `{"T":4,"cmd":2}`.

> Note: the rover's `config.yaml` `module_type` must be `2` (Gimbal) for the
> pan/tilt camera to respond; `0` (None) makes the firmware ignore gimbal commands.
