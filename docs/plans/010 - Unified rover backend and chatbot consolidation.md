# 010 - Unified rover-control backend + chatbot consolidation

## Goal

One shared, auto-detecting rover-control backend used by **both** the chatbot and the
joystick: **direct serial when running ON the rover (no HTTP), HTTP when remote**. And
**one canonical chatbot** (`agent_chat.py`); retire the other two. Supersedes plan 009's
HTTP-only joystick transport — the joystick now auto-detects (serial on the rover, HTTP on
the laptop).

## Decisions (user)

- Auto-detect backend (serial on rover, HTTP remote). HTTP is acceptable for the remote
  case ("the chatbot should run both inside and outside; HTTP is okay").
- Consolidate to one chatbot: `agent_chat.py` (the `chatbot` launcher already runs it).

## Design

1. **New `rover_backend.py`** — move the dual-backend `RoverCtl` **verbatim and in full**
   (currently inline in `agent_chat.py`, lines ~70-155) and `detect_rover()` into a
   reusable, import-anywhere module (stdlib only at import; `rover_direct` imported lazily
   on the serial path; `rover_backend` must NOT import `agent_chat` — one-way deps).
   - **Preserve the entire surface** `agent_chat.py` relies on (codex #1): attributes
     `backend`, `where`, `pan`, `tilt` and methods `set_camera`, `drive`, `move`, `stop`,
     `estop`, `lights`, `set_torque`, `oled`, `oled_default`, `center`, `photo`, `demo`,
     `close` — move the class as-is, don't trim it.
   - **Torque parity (codex #2):** keep `set_torque(lock)` (agent_chat) AND add
     `servo_torque(lock)` (gamepad) as an alias; test both names.
   - `connect(host=None, timeout=None)`: serial if `rover_direct.detect_port()` exists and
     opens (stops `app.py` to free the port — existing behavior), else HTTP if the host is
     reachable, else `None`. **For HTTP it must `rover_client.set_host(host)` and
     `set_timeout(timeout)` before use (codex #3/#4)** and reflect that host in `where`, or a
     remote `--host` silently targets the default.

2. **`agent_chat.py`** — import `RoverCtl`/`detect_rover` from `rover_backend`; delete the
   inline copies. No behavior change (still the canonical chatbot that runs on both).

3. **`rover_gamepad.py`** — replace direct `rover_client` use with
   `rover_backend.connect(host=args.host, timeout=SEND_TIMEOUT)`. On the rover → serial
   (direct calls, no HTTP); on the laptop → HTTP. The `Sender` calls backend methods (same
   names → unchanged; it uses `servo_torque`).
   - **Snapshot gating (codex #5):** the `Snapshotter` grabs an MJPEG frame from
     `app.py`'s `:5000/video_feed`, which doesn't exist in serial mode (serial stops
     `app.py`). So only start the `Snapshotter` / enable **B** when the backend is HTTP; in
     serial mode B prints "no camera (serial mode)" instead of spawning a failing thread.
   - Update the SAFETY banner: continuous-drive runaway risk applies on **both** backends,
     and serial stops `app.py` (no camera while joysticking on the rover).

4. **Consolidate chatbots:**
   - Retire `rover_chat.py` → `graveyard/` (no importers; `agent_chat.py` covers serial+HTTP
     chat).
   - `chatbot.py` is imported by `list_models.py` + `list_ark_endpoints.py` for LLM-provider
     helpers (`PROVIDERS`, `pick_provider`, `resolve_base_url`, `load_dotenv`). Extract those
     into a new **`llm_config.py`** (dependency-free — no rover imports); repoint both
     listers; then retire `chatbot.py` → `graveyard/`. **`load_dotenv` (codex #6):** unify on
     the superset behavior — load both `~/.env` and the repo `.env` (what `agent_chat.py`
     does today); point `agent_chat.py` at it too. Tested.
   - Update `README.md` (remove the chatbot.py / rover_chat.py sections, point to
     `agent_chat.py`) and `CLAUDE.md` (on-Mac / on-rover notes). The `chatbot` launcher
     already execs `agent_chat.py` — no launcher change; check `install.sh` for a `roverchat`
     launcher that points at `rover_chat.py` and update/remove it.

## Tests (`tests/`, unittest)

- **`test_backend.py`** (new): `connect()` picks serial when `detect_port()` returns an
  existing path (monkeypatched) and the open succeeds; falls back to HTTP when reachable;
  returns `None` when neither. **`connect(host=..., timeout=...)` calls `rover_client.set_host`
  + `set_timeout` for HTTP and `where` reflects the host** (codex #3/#4). `RoverCtl` dispatch
  maps each method to the right serial vs HTTP call (inject fake `rover_direct.Rover` + fake
  `rover_client`), with clamping. **Both `set_torque` and `servo_torque` reach the backend**
  (codex #2). Importing `rover_backend` requires no serial hardware.
- **`test_llm_config.py`** (new): `load_dotenv` reads both `~/.env` and repo `.env` (codex #6).
- **`test_gamepad.py`**: unchanged behavior — the `Sender`/`Dispatcher` call a backend object
  with the same method names the `FakeClient` already mimics; add a `servo_torque` assertion.
- **Import smoke**: `import llm_config`, `import list_models`, `import list_ark_endpoints`,
  `import agent_chat`, `import rover_backend` all succeed (catches the consolidation breakage).
- `ci-local.sh` Python suite green; Go untouched.

## Risks / tradeoffs

- **Breaking the model-listing tools** when retiring `chatbot.py` — mitigated by the
  `llm_config.py` extraction + import-smoke tests.
- **Method-name parity** (`set_torque` vs `servo_torque`) — add the alias.
- **Serial joystick on the rover stops `app.py`** (no camera) — inherent to direct serial;
  documented. The laptop path (HTTP) keeps the camera.
- **Losing `chatbot.py`'s ARK/dashscope chat loop** — intentional (one canonical chatbot);
  its provider config is preserved in `llm_config.py` for the listers.

## Rollout

Pure code; no rover deploy needed for the chatbot (runs from either machine). Joystick: run
`python rover_gamepad.py` on the rover (serial) or `--host <ip>` on the laptop (HTTP).

---

## Plan review

**Gate: PASSED** after revisions (Opus + codex; glm not part of plan gate).

**Opus (Claude):** confirmed the one-way dependency graph (`agent_chat → rover_backend →
rover_client/rover_camera`; `llm_config` dependency-free) avoids cycles, and that retiring
`rover_chat.py` is safe (no importers).

**codex (GPT-5.5) — 6 blocking, all folded in:**
1. Move the **entire** `RoverCtl` surface (`where/backend/pan/tilt/photo/demo/close/…`), not
   a subset.
2. Keep **both** `set_torque` (agent_chat) and `servo_torque` (gamepad); test both.
3. `connect()` must `rover_client.set_host(host)` for HTTP, and reflect it in `where`.
4. `connect(host, timeout)` must set the short HTTP timeout for the gamepad path.
5. Gate the gamepad **Snapshotter on the HTTP backend** — serial mode has no `app.py` camera;
   B should no-op with a message, not spawn a failing thread.
6. Unify `load_dotenv` on the superset (`~/.env` + repo `.env`); test it.
   Nits: keep deps one-way; update the stale "`rover_direct.py` only for `rover_chat.py`"
   comment; `install.sh` only links `chatbot`+`roverctl` (no `roverchat` blocker); add
   host-propagation + timeout tests.

## Code review

**Gate: PASSED.** Reviewers: Opus + codex (glm/opencode not installed → 2-way).

**codex (GPT-5.5) — no blocking issues.** Confirmed the AST extraction left no dangling
refs in `agent_chat.py` (`RoverCtl`/`detect_rover`/`load_dotenv` resolve from the new
modules; no stray `_clamp`), backend interface parity holds, and the gamepad's
`None`-backend / `None`-snapshot paths are guarded. Nits fixed: stale `chatbot.PROVIDERS`
docstring in `list_models.py`, empty "rover backend" comment + unused `socket` import in
`agent_chat.py`. (`list_ark_endpoints.py` needs `volcengine` to import — pre-existing.)

**Opus (Claude):** verified the dependency graph stays one-way (no cycles) and that the
restored `rover_web.py`/`rover_joystick.py` don't collide with the new modules.

## Post-execution report

**Branch:** `feature/unified-rover-backend`. Folds in plan 009 (the gamepad) — both land
together as "unify rover control behind one auto-detect backend."

### What shipped
- **`rover_backend.py`** (new): the dual-backend `RoverCtl` + `connect()` extracted from
  `agent_chat.py` — direct serial on the rover, HTTP remote. Adds `servo_torque` alias and
  `connect(host, timeout)` host/timeout propagation.
- **`llm_config.py`** (new): LLM provider helpers (`PROVIDERS`/`pick_provider`/
  `resolve_base_url`/`load_dotenv`) extracted from `chatbot.py`; `list_models.py` +
  `list_ark_endpoints.py` repointed to it.
- **`agent_chat.py`**: now imports the shared backend + config (AST-removed the inline
  copies); behaviour unchanged — still the one chatbot that runs on rover or laptop.
- **`rover_gamepad.py`**: uses `rover_backend.connect()` (auto-detect serial/HTTP); the
  Snapshotter is HTTP-only (serial has no `app.py` camera); B no-ops with a message on serial.
- **Consolidated chatbots**: `chatbot.py` + `rover_chat.py` → `graveyard/`; `agent_chat.py`
  canonical (the `chatbot` launcher already runs it).
- **Restored from graveyard** (user request): `rover_web.py` (gallery + live view :8080),
  `rover_joystick.py` (on-rover serial gamepad), `roverweb` launcher — refreshed the stale
  "superseded by Go" header.
- Docs: `README.md` + `CLAUDE.md` updated to the consolidated layout.

### Tests / CI
- New `tests/test_backend.py` (connect selection, host/timeout propagation, serial+HTTP
  dispatch, both torque names, import smoke) and `tests/test_llm_config.py` (providers +
  `load_dotenv`). `ci-local.sh`: **PASS** — 56 Python tests, Go untouched (74.6% cov, arm64
  cross-compile).

### Tradeoffs / notes
- Two gamepad entry points by design: `rover_joystick.py` (on-rover, proven, direct serial)
  and `rover_gamepad.py` (laptop/auto-detect). On the rover they overlap (both serial).
- Serial backend stops `app.py` (no camera while driving on the rover); HTTP keeps it.
- `rover_web.py` needs Flask (rover venv has it); not import-tested on the Mac.
- The continuous-drive runaway caveat (supervised use; app.py watchdog follow-up #14) is
  unchanged and applies on both backends.
