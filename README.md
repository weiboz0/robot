# rover

Tools for a Waveshare UGV Rover (Raspberry Pi + ESP32 sub-controller) and a Dobot
MG400 robotic arm.

## Multi-robot chatbot (`agent_chat.py`) — recommended
One chatbot that controls **both the rover and the Dobot MG400** in natural language.
Same file, no per-machine edits — it **auto-detects** what it can reach:

- **Rover**: direct serial if a serial port exists (on the Pi), else the Go controller's
  `:8080` API if its serial is up, else the legacy app.py `:5000` API, else disabled.
- **Dobot** (`192.168.1.6`): TCP-IP if reachable, else disabled.

Runs on the rover's Raspberry Pi or any computer (the Dobot can't run it itself).

```bash
pip install -r requirements.txt
echo 'OPENCODE_API_KEY=sk-...' >> .env     # or ~/.env  (LLM = OpenCode/minimax-m3)
python agent_chat.py
```

### Pull from GitHub and run `chatbot` anywhere
After cloning/pulling, run the installer once — then just type `chatbot` from any directory:

```bash
git clone git@github.com:weiboz0/robot.git && cd robot   # or: git pull
pip install -r requirements.txt
echo 'OPENCODE_API_KEY=sk-...' >> .env                   # or ~/.env
./install.sh        # symlinks `chatbot` into ~/.local/bin (one time)
chatbot             # run from anywhere
```

`install.sh` adds `~/.local/bin` to your PATH if it isn't already (open a new terminal
afterward). You can also run it without installing: `./chatbot`.

Chat in plain English, or prefix a line with `$` for a direct command:
`$up 45`, `$cam 0 30`, `$drive 0.2 0.2 1`, `$stop` (rover); `$dobot GetPose()`,
`$dobot EnableRobot()` (raw Dobot); `$help`. Without an API key it still runs —
chat is off but `$` commands work.

> The Dobot must be in **Remote/TCP control mode** (unlock in DobotStudio Pro) or it
> replies `-1`. Run the tests with `python -m unittest discover -s tests -t .`.

## Chatbot config (`.env`)
`agent_chat.py` (above) is the one chatbot — same file on the rover (direct serial) or any
computer (HTTP). It and the model-listing helpers read provider config from `.env` (not
committed) via `llm_config.py`: provider keys, `PROVIDER`, and per-provider `*_MODEL` /
`*_BASE_URL` (ARK / DashScope / OpenCode).

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
rover            # interactive  (launcher -> ~/robot/rover_direct.py)
rover demo       # self-test
```

## Gamepad control (the Go controller)
The gamepad is handled by the Go controller (`rovercontrol/`), which reads a USB gamepad
plugged into the **Pi** directly and also serves the camera + a control API on `:8080`.
Run it on the rover (build + deploy the `rovercontrol-arm64` binary, then start it); the
joystick "just works" and the live view + photo gallery are at `http://192.168.1.131:8080/`.
The Python chatbot drives the same controller over that `:8080` API. (The old Python
joystick scripts now live in `graveyard/`.)

## Running on the rover via git
The rover programs live in this repo; on the rover they're cloned at `~/robot`
and run via the `rover` / `chatbot` launchers. To update:

```bash
ssh rover
cd ~/robot && git pull      # launchers always run the latest
```

ESP32 command reference: motors `{"T":1,"L":..,"R":..}`, gimbal
`{"T":133,"X":pan,"Y":tilt,"SPD":0,"ACC":0}`, module-select `{"T":4,"cmd":2}`.

> Note: the rover's `config.yaml` `module_type` must be `2` (Gimbal) for the
> pan/tilt camera to respond; `0` (None) makes the firmware ignore gimbal commands.
