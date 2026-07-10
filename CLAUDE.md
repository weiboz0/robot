# CLAUDE.md

Guidance to remember for every request in this workspace.

## Development workflow

Use **branch-based development**: never commit directly to `main`. Create a feature
branch (`git checkout -b <topic>`), do the work there, and merge via PR. Keep `main`
deployable. **Commit and push only when asked** — don't push to remotes on your own.
**Never commit secrets** (`.env` holds API keys and is gitignored; use `.env.example`).

Non-trivial work follows these stages, in order:

1. **Plan** — Write a plan under `docs/plans/` with a numbered prefix, e.g.
   `docs/plans/002 - Rover control.md` (use the next number above the highest existing).
2. **Plan review (gate)** — Before writing code, get the plan reviewed by **two**
   reviewers and resolve blocking feedback:
   - **Opus** (Claude)
   - **GPT-5.5 via codex**: `codex exec --skip-git-repo-check "Review this plan: <…>"`
3. **Implementation** — Write the code on the feature branch.
4. **Testing** — Write test code covering the change.
5. **Code review (gate)** — Before merging, run a **3-way review** and resolve blocking
   findings:
   - **Opus** (Claude)
   - **codex** (GPT-5.5): `codex exec --skip-git-repo-check "Review this diff: <…>"`
   - **glm-5.1 via opencode**: `opencode run -m opencode-go/glm-5.1 "Review this diff: <…>"`
6. **PR & merge** — Open a PR; merge to `main` only after the code-review gate passes.

Notes: scope one logical change per branch/PR; codex/opencode are external agents
(network + token cost) — confirm before delegating large reviews.

## Projects in this workspace
- **`rover-chatbot/`** — tools for a Waveshare UGV Rover (Raspberry Pi 5 + ESP32) and helper
  agents. Mirrored to GitHub `weiboz0/robot`, and cloned on the rover at `~/robot`
  (deploy by `git pull` there; the `rover` / `roverjoy` launchers run the pulled code).
  - Control goes through one auto-detecting backend (`rover_backend.py`): **serial** on the
    Pi → **rovercontrol** (the controller's `:8080` API, `rovercontrol_client.py`) →
    **app.py** (`:5000`, `rover_client.py`, legacy fallback).
  - Chatbot: `agent_chat.py` (the one chatbot; runs on rover or any computer). LLM provider
    config in `llm_config.py` (used by `list_models.py` / `list_ark_endpoints.py` too).
  - Gamepad + camera: the controller `rovercontrold.py` (single-file Python, stdlib
    only; reads the Pi gamepad, serves camera + control on `:8080`). Ported 1:1
    from the retired Go build; the old Python joysticks are in `graveyard/`.
  - On-rover serial tools: `rover_direct.py` (motors/camera).

## Rover facts worth remembering (hard-won)
- Rover SSH: `ssh rover` (= `ws@192.168.1.131`). Pi 5 → serial is `/dev/ttyAMA0` @ 115200.
- The pan/tilt camera only moves if the **Gimbal module is selected** (`{"T":4,"cmd":2}`);
  `config.yaml` `module_type` was `0` (None), which silently ignores gimbal commands.
- Over the HTTP `base -c {json}` path (app.py `/send_command`), the JSON must be
  **compact (no spaces)** or the rover drops it (and still returns HTTP 200).
- Only one process can own the serial port — the web app (`app.py`) vs. the direct tools
  are mutually exclusive; the direct tools stop the web app on launch.

## Style
- Match the surrounding code's conventions; keep changes minimal and focused.
- State outcomes plainly; if something failed or was skipped, say so.
