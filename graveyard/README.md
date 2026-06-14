# graveyard

Retired files, kept for reference instead of deleted. Superseded by
`rovercontrol` (the single-file Go controller — `docs/plans/002`).

| file | was | replaced by |
|---|---|---|
| `rover_web.py` | Python Flask photo gallery + live-view server | controller's built-in web UI + `/video_feed` |
| `roverweb` | launcher for `rover_web.py` | `roverctl` (launches `rovercontrol-arm64`) |
| `test_web.py` | tests for `rover_web.py` | `rovercontrol/rovercontrol_test.go` |
| `rover_joystick.py` | Python gamepad driver (read pad on the Pi) | controller's built-in joystick |

Nothing in the live codebase imports these. `rover_joystick.py` is kept as the
reference gamepad mapping until the Go joystick is verified on the actual Pi
(raw `/dev/input/js0` indices differ from the SDL/pygame ones it used).

**Not moved here** (still used, so not unused): `rover_direct.py` and
`rover_camera.py` are imported by the LLM chatbot (`rover_chat.py`,
`agent_chat.py`), which is kept as a client. They're marked SUPERSEDED in place
and will move here once the chatbot is repointed to the controller's HTTP API.
