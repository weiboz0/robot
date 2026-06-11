# 001 - Multi-robot chatbot

## Goal
One chatbot (`agent_chat.py`) that controls **both** the Waveshare UGV rover and the
Dobot MG400 (`192.168.1.6`) through natural language. Same file, pullable from GitHub,
runnable directly (`pip install -r requirements.txt` + `python agent_chat.py`) on:
- the **rover's Raspberry Pi** — rover via direct serial, Dobot via TCP, and
- a **computer** — rover via the rover's HTTP API, Dobot via TCP
(the Dobot can't run code itself).

No per-machine edits: the program **auto-detects** what it can reach.

## Capability detection (at startup)
- **Rover**
  - If a serial device exists (`/dev/ttyAMA0` or `/dev/serial0`) → direct serial
    (`rover_direct.Rover`, stops the web app to take the port). [the Pi]
  - Else if the rover web API (`192.168.1.131:5000`) is reachable → HTTP
    (`rover_client`). [a computer]
  - Else → rover disabled.
- **Dobot**: if `192.168.1.6:29999` is reachable → enabled (`dobot.Dobot`). Else disabled.

Only tools for available robots are offered to the LLM.

## Components
- `dobot.py` — Dobot MG400 TCP-IP client (dashboard `29999`, motion `30003`):
  `mode`, `pose`, `enable`, `disable`, `clear_error`, `move_j(x,y,z,r)`, `move_l`, raw.
- `agent_chat.py` — unified chatbot: env detection, LLM tool-calling (OpenCode /
  minimax-m3 by default), `$` direct commands (`$up 45`, `$dobot GetPose()`), graceful
  chat-off fallback.
- `requirements.txt` — `openai`, `pyserial` (pyserial installs on macOS too; only
  opened on the Pi).
- `.env` — `OPENCODE_API_KEY` (+ optional model/base-url overrides).

## Safety
- Both robots are physical. Keep speeds/moves modest; the Dobot needs **Remote/TCP
  mode** unlocked (DobotStudio Pro) or it returns `-1`. Confirm workspace clear before motion.

## Testing
- `tests/` — no-hardware unit tests: Dobot command formatting (mock socket), rover `$`
  command parsing + camera-angle tracking (mock backend), capability-detection logic.

## Out of scope
- Fixing the rover `config.yaml` `module_type` (needed for gimbal over HTTP) — separate.
- Dobot remote-mode unlock (done on the device).
