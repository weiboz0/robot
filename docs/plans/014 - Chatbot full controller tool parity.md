# 014 - Chatbot full controller tool parity

## Goal

Make the Python chatbot (`agent_chat.py`) able to do **everything the Go
`rovercontrol` controller can do**, so a natural-language session (and the `$`
direct-command path) exposes the controller's full command surface — not a
subset.

## Gap analysis (controller HTTP routes → chatbot exposure today)

Already exposed as LLM tools **and** `$`-commands (no change needed):
drive (auto-stop), stop, estop, camera aim, lights, snapshot/photo, OLED.

| Controller route | LLM tool today | `$` cmd | Decision |
|---|---|---|---|
| `POST/GET /speed` (speed cap) | none | none | **ADD** end-to-end |
| `GET /healthz` (status) | none | none | **ADD** tool + `$status` |
| `/camera_center` | none | `$center` | **promote** to LLM tool + route to real endpoint |
| `/gimbal_relax` `/gimbal_lock` | none | `$relax`/`$lock` | **promote** to LLM tool |
| `GET /photos` (list) | none | none | **ADD** read-only tool + `$photos` |
| `/drive` continuous | none | `$move` | keep `$`-only — see Risks (no serial/http watchdog) |
| `/move_forward/back/left/right` (nudge) | none | none | **drop** — `rover_drive(l,r,seconds)` is a bounded, auto-stop superset; documented, not a separate tool (avoids tool bloat) |
| `/camera_up/down/left/right` (nudge) | none | `$up/$down/...` | keep `$`-only — `rover_set_camera` (absolute aim) covers it |
| `GET /photos/{name}` (fetch bytes) | n/a | n/a | **drop** — serving image bytes is meaningless in a text chat; `rover_photo` already returns the saved path |
| `POST /delete_photo/{name}` | n/a | n/a | **drop** — destructive gallery op; that's `rover_web.py`'s job, not the chatbot's |
| `GET /video_feed` | n/a | n/a | **drop** — MJPEG stream, not a chat action |

## Design

The chatbot reaches the rover through `RoverCtl` (`rover_backend.py`), which
abstracts three transports: **serial** (`rover_direct`), **rovercontrol**
(`rovercontrol_client`, the Go `:8080` API), **http** (legacy `app.py :5000`).
New capabilities must work across all three (or degrade explicitly, like OLED).

### Speed cap — consistent multiplier semantics across backends

The controller treats the cap as a **multiplier**, not a clamp. The chatbot's
wheel value is `±0.5`; `rovercontrol_client._norm` maps `0.5 → 1.0`, then Go's
`driveCap` computes `actual = norm · cap` (`rovercontrol.go:264`). So on
rovercontrol the effective wheel output is `chatbot_value · 2 · cap`, and the
**max** output (chatbot value `0.5`) is exactly `cap`.

To keep serial/http identical (codex blocker — a plain clamp would make only the
*max* match, not mid-range values), serial/http **scale by the same factor**:

```
effective_wheel = clamp(value, -0.5, 0.5) · (2 · self._cap)   # serial/http only
```

- `self._cap` default = **0.5** ⇒ factor `1.0` ⇒ `effective = value` ⇒ current
  serial/http behaviour is unchanged by default.
- `set_speed(0.25)` ⇒ factor `0.5` ⇒ a `drive(0.2,0.2)` yields `0.1` on **every**
  backend (matches rovercontrol). `cap` = max wheel magnitude on the 0..0.5 scale.
- rovercontrol `drive()/move()` are **left untouched** (server multiplies) — no
  double-cap.

API:
- `rovercontrol_client.py`: add `set_speed(cap)` → `POST /speed?cap=`,
  `get_speed()` → `GET /speed` (parse `cap`). (`healthz()` already exists — reuse.)
- `RoverCtl.set_speed(cap)`: clamp `cap` to `0..0.5`; rovercontrol →
  `client.set_speed`; serial/http → store `self._cap`.
- `RoverCtl.get_speed()`: rovercontrol → `client.get_speed`; serial/http → `self._cap`.

Documented divergence (reviewer nit, non-blocking): the **default** cap differs —
serial/http default `0.5` (full), rovercontrol's *server* default is `0.25`.
`get_speed()`/`status()` report the live value truthfully; we do not force a
default on connect (that would stomp the gamepad's shared cap, `rovercontrol.go:1415`).

### Status — one stable shape across all backends

`RoverCtl.status()` always returns the same keys; unknown fields are explicit:

```python
{"backend": "serial|rovercontrol|http",
 "where":   "<serial port | http url>",
 "serial":  {"up": bool},
 "camera":  {"up": bool | None},     # None = unknown on this backend
 "gamepad": {"up": bool | None},
 "speed_cap": float}
```

- rovercontrol → fill `serial/camera/gamepad` from `healthz()`; `speed_cap` from `get_speed()`.
- serial → `serial.up` from the live link (`self._r.ser.is_open`); camera/gamepad `None`.
- http → `serial.up=True` (reachable); camera/gamepad `None`.

### Camera center — real endpoint (codex blocker)

`RoverCtl.center()` currently calls `set_camera(0,0)`, so rovercontrol hits
`/camera_aim` not `/camera_center`. Fix: rovercontrol → `rovercontrol_client.center()`;
serial/http → `set_camera(0,0)` (no distinct endpoint; aiming to 0,0 is identical).
Either way reset tracked `self.pan/tilt = 0`.

### Photo list

`RoverCtl.list_photos()` → list[str] (newest first): rovercontrol → `GET /photos`;
serial/http → list `rover_camera.PHOTO_DIR`.

### Chatbot wiring (`agent_chat.py`)

- New LLM tools: `rover_set_speed` (cap 0..0.5), `rover_get_status`,
  `rover_center_camera`, `rover_gimbal_torque` (`lock`: bool), `rover_list_photos`.
- `run_tool` handlers for each.
- New `$` commands: `$speed [cap]` (no arg → print current), `$status`, `$photos`.
  (`$center`, `$relax`, `$lock` already exist.)
- Update `$help` and `SYSTEM`: mention speed cap (lowering it slows everything;
  note it is shared with the gamepad on rovercontrol so it isn't exclusively
  the bot's) and status.

## Deliverables

- `rovercontrol_client.py`: `set_speed`, `get_speed`, `list_photos`.
- `rover_backend.py`: `self._cap`, `set_speed`, `get_speed`, `status`,
  `list_photos`, scale-aware `drive`/`move` (serial/http), real `center()`.
- `agent_chat.py`: 5 new tools + handlers + 3 new `$` cmds + help/SYSTEM updates.
- Tests in `tests/` (fakes; no hardware).
- `docs/reference/controller-commands.md`: note the **chatbot now exposes**
  `/speed`, `/healthz`, `/camera_center`, gimbal torque, `/photos`.

## Testing

- `tests/test_rovercontrol_client.py`: `set_speed` posts `/speed?cap=`;
  `get_speed` parses `cap`; `list_photos` parses `photos` (monkeypatch urlopen).
- `tests/test_backend.py`:
  - `set_speed`/`get_speed`/`status` route per backend; `status()` returns the
    stable shape on each backend.
  - serial/http `drive`/`move` **scale** by `2·cap` (e.g. cap 0.25 → 0.2 sent ⇒
    0.1 effective); assert rovercontrol `drive()` does **not** apply `self._cap`
    (no double-cap — the client is called with the raw value).
  - rovercontrol `center()` calls `client.center()` (not `set_camera`).
- `tests/test_rover_cmd.py`: `$speed`/`$status`/`$photos` parse + dispatch (fake).
- `agent_chat` tests: `build_tools()` includes the 5 new tools; `run_tool()`
  dispatches each (fake rover).
- Run `./ci-local.sh` (unit; integration needs the live rover, optional).

## Risks

- **Continuous `move` as an LLM tool**: deliberately NOT added. `/drive` is only
  watchdog-protected on the rovercontrol backend (`applyDrive(...,true)` arms a
  ~500 ms watchdog, `rovercontrol.go:258`); serial/http have no server watchdog,
  so an LLM-issued continuous speed could strand the rover. `$move` stays a
  human-only escape hatch; bounded `rover_drive` is the LLM's drive path.
- **Double-capping** on rovercontrol: avoided — `self._cap` scaling is serial/http only.
- **Shared cap on rovercontrol**: the gamepad also writes the cap; `rover_set_speed`
  is not exclusive. SYSTEM prompt says so.
- **Cat safety**: code change only; every test uses fakes — no motion command is
  sent during implementation or testing.

## Stages

1. Client (`rovercontrol_client.py`): `set_speed`/`get_speed`/`list_photos` + tests.
2. Backend (`rover_backend.py`): cap/status/list/center + scale-aware drive + tests.
3. Chatbot (`agent_chat.py`): tools + `$` cmds + help/SYSTEM + tests.
4. Docs.
5. ci-local, 3-way review, PR, merge.

## Reviews

### Plan review (Opus + codex) — both REQUEST-CHANGES → resolved

**Opus** (APPROVE-leaning; blockers were gap-completeness):
- BLOCKING: gap analysis omitted `/move_*` wheel nudges → resolved: documented as
  dropped (superset = `rover_drive`).
- BLOCKING: gap analysis omitted photo routes → resolved: added read-only
  `list_photos`; explicitly dropped fetch-bytes / delete / video_feed with reasons.
- Confirmed (grounded in code): speed-cap = multiplier with max = cap, the
  `$move`-only safety call, and no double-cap. Nits (default divergence, shared
  gamepad cap, reflect real serial link in status, no-double-cap test) folded in.

**codex** (REQUEST-CHANGES):
- BLOCKING: speed-cap clamp vs multiplier inconsistency → resolved: serial/http
  now **scale** by `2·cap` to match rovercontrol mid-range, not just at max.
- BLOCKING: `status()` shape contradictory → resolved: one fixed shape, explicit
  `None` for unknown fields.
- BLOCKING: `center()` doesn't hit `/camera_center` → resolved: rovercontrol
  routes to `client.center()`.
- Nits (healthz reuse wording, build_tools/run_tool tests, doc wording) folded in.

### Code review (Opus + codex + glm-5.1) — all APPROVE

All three traced the speed-cap invariant through `rovercontrol.go` and confirmed:
serial/http scale by `2·cap`, rovercontrol passes raw values (server applies the
single cap) — **no double-cap**; default 0.5 leaves serial/http unchanged;
`center()` hits `/camera_center`; `status()` shape is stable with no AttributeError
on the fake serial path; continuous `move` is absent from `build_tools` (LLM)
and stays `$`-only. No BLOCKING findings.

Nits resolved:
- (Opus) `rover_set_speed` had a stray `0.25` default → now requires `cap`
  explicitly (returns a hint if omitted).
- (glm) `status()` rovercontrol path: a `/speed` failure could clobber a good
  `healthz` → `get_speed()` moved to its own guarded try.

Nits deferred (documented, harmless): `status()` http `serial.up=True` is
reachability-optimistic (pre-existing); `list_photos` local ordering relies on
the time-sortable filename scheme; `_scale` output clamp is unreachable; `$speed`
network errors propagate like other `$` commands.

## Post-execution report

**Implemented** exactly as the revised plan. End-to-end across all three backends:
speed cap (`set_speed`/`get_speed`, multiplier-consistent), `status()` (stable
shape), real `center()` routing, gimbal torque + camera-center promoted to LLM
tools, and read-only `list_photos`. Chatbot surface: 5 new LLM tools
(`rover_set_speed`, `rover_get_status`, `rover_center_camera`,
`rover_gimbal_torque`, `rover_list_photos`) + 3 new `$` commands (`speed`,
`status`, `photos`) + help/SYSTEM updates.

**Deviations:** none material. Photo *fetch-bytes*/*delete* and `/video_feed`
were explicitly dropped (not chat actions / gallery's job), and continuous `move`
kept `$`-only for the no-watchdog safety reason — both decided at plan review.

**Tests/CI:** 60 Python unit tests (12 new) + Go suite pass; `ci-local.sh` PASS
(Go coverage 74.3% ≥ 70 floor). No hardware touched — every test uses fakes, so
no motion command was sent (cat-safety honoured).

**Out of scope / follow-ups:** the `status()`/`list_photos` nits above; aligning
the rovercontrol default cap (0.25) vs serial/http (0.5) if a single default is
ever wanted.
