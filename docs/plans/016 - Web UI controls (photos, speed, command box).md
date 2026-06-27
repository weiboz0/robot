# 016 - Web UI controls: clear photos, speed value, command box

## Goal

Enrich the controller's built-in web UI (the `htmlPage` constant in
`rovercontrol.go`) with three user-requested controls. **Front-end only** — every
control maps to an existing HTTP endpoint, so there is no new Go handler, no
serial passthrough, and all server-side safety (speed clamp, 0.5 s watchdog,
e-stop latch, NaN rejection) is preserved unchanged.

## Scope (decided with the user)

1. **Clear all photos** — a button that deletes every photo (the per-photo `del`
   button already exists). Loops `POST /delete_photo/{name}` over `GET /photos`,
   behind a confirm.
2. **Type-in speed value** — a number `<input>` (0..0.5, fine step) next to the
   existing slider so an exact cap (e.g. `0.15`) can be typed; both stay in sync
   and show the current value. Reads the live cap from `GET /speed` on load.
3. **Direct command box** — a text input + Send that parses the controller's own
   command vocabulary and maps each to its existing endpoint (the user chose the
   "controller commands (safe)" option, NOT raw serial). Examples:
   `drive 0.2 0.2`, `move_forward 300`, `camera_aim 30 0`, `camera_up 10`,
   `light_head on`, `speed 0.15`, `relax`, `lock`, `stop`, `estop`, `snapshot`.

Note: speed is a normalized cap (0..0.5 wheel magnitude), **not literally m/s** —
labelled "speed cap"; a real m/s conversion would need field calibration (out of
scope, offered as a follow-up).

## Command vocabulary → endpoint mapping

| Typed | Endpoint |
|---|---|
| `drive L R` | `POST /drive?l=L&r=R` (normalized −1..1, scaled by cap; auto-stops via the 0.5 s watchdog) |
| `move_forward\|back\|left\|right [MS]` | `POST /move_*?ms=MS` (nudge, self-stops) |
| `stop` / `estop` | `POST /stop` / `/estop` |
| `camera_aim PAN TILT` | `POST /camera_aim?pan=&tilt=` |
| `camera_up\|down\|left\|right [DEG]` | `POST /camera_*?deg=` |
| `camera_center` | `POST /camera_center` |
| `light_head\|light_base [on\|off]` | `POST /light_*` (no arg = toggle; on/off → `?on=1\|0`) |
| `gimbal_relax`/`relax`, `gimbal_lock`/`lock` | `POST /gimbal_relax` / `/gimbal_lock` |
| `speed CAP` | `POST /speed?cap=` |
| `snapshot`/`snap` | `POST /snapshot` |

Aliases: `relax`→`gimbal_relax`, `lock`→`gimbal_lock`, `snap`→`snapshot`,
`fwd`→`move_forward`, `back`→`move_back`. Unknown command or bad/missing numeric
args → a friendly inline message, no request sent.

## Design notes / safety

- **No new endpoints, no Go logic change** — only the `htmlPage` string changes.
  The command box is a thin client over the same endpoints the buttons already
  use; clamps/watchdog/e-stop all still apply server-side.
- **XSS**: the command-feedback line is written with `textContent` (never
  `innerHTML`) since it echoes user input. The gallery keeps its existing
  server-controlled (`safePhotoName`) names.
- **No motion is triggered by this change itself** — a user can type a drive
  command, exactly as they can press the existing drive buttons; that's intended.
  The cat-safety rule (I don't send motion) is unaffected: testing is by serving
  the page and asserting its contents, never by issuing a drive.
- Keep the page's terse style (compact inline JS, same `.bar`/`button` classes).

## Deliverables
- `rovercontrol.go`: extended `htmlPage` (Clear-all button + handler, speed number
  input + sync + load-time read, command box + parser + feedback line).
- `rovercontrol_*_test.go`: assert `GET /` is 200 and the body contains the new
  controls (`id="cmdin"`, the clear-all control, `id="capNum"`); assert all
  endpoints the command box targets exist in `routes()` (guard against typos).

## Testing
- Go: serve `/` via the test harness; assert 200 + presence of the new element
  ids/labels. A table test that every command-box target path resolves to a
  non-404 route (using the existing `do()` harness with representative params).
- `./ci-local.sh` (Go race + coverage ≥70, Python unchanged).
- Manual (post-deploy, by the user in a browser): clear-all, type a speed, type a
  few commands. I will only `curl` the page (a GET) to confirm it serves.

## Risks
- A command-parser bug could send an unintended request — mitigated by validating
  numeric args before building the URL and by the server's own clamps; worst case
  is a clamped value, never an unclamped one.
- `Clear all` is destructive — gated behind a `confirm()`; only deletes photos
  (gallery data), nothing else.

## Stages
1. Plan + 2-way plan review.
2. Implement htmlPage additions.
3. Go tests (page contents + route existence).
4. ci-local, 3-way code review, PR, merge.
5. Deploy (rebuild arm64, rsync, restart, verify `/healthz` + `GET /`).

## Reviews
### Plan review (Opus + codex) — both APPROVE, no blockers

Both verified every endpoint/param in the mapping table exists exactly, the
front-end-only design preserves clamp/watchdog/estop, and textContent +
`safePhotoName` make XSS safe. Should-fix nits folded into implementation:
- `ms`/`deg` are **optional** (server defaults 400/15) — omit the param when
  absent, don't reject (Opus+codex).
- Numeric validation via `Number()` + `Number.isFinite` (reject `10abc`, NaN,
  Inf, empty); reject extra args for fixed-arity commands (Opus+codex).
- Build queries with `URLSearchParams`; normalize input (trim, `split(/\s+/)`,
  lowercase the command + `on/off` literal) (codex/Opus).
- After `/speed`, sync slider+number from the **response** `cap` (server clamps),
  and read the live cap from `GET /speed` on load (Opus+codex).
- Echo the HTTP result (ok vs `{"ok":false,"error":…}` / 503) in the feedback
  line, not a blind "sent" (Opus). errJSON shape confirmed: `{ok:false,error}`.
### Code review (Opus + codex + glm) — all APPROVE, no blockers

All three verified every command-box route + param + response key matches
`routes()`, the parser's required/optional/no-arg arity and aliases are correct,
`Number.isFinite` blocks junk, XSS is avoided (`textContent` for echoed input;
numeric-only/server-controlled `innerHTML`), and the speed sync has no feedback
loop. Nits folded in: corrected the `cnum` comment (`Number('')` is `0`, but
arity checks gate empty tokens), and tidied `setCap` to check finite-before-clamp
and build the query via `URLSearchParams`. Deferred (cosmetic): `clearAll`
per-item failure reporting.

## Post-execution report

**Implemented** all three controls, front-end only (the `htmlPage` constant) —
no new Go endpoints, server safety untouched:
- **Clear all photos** button (confirm-gated; loops the existing
  `/photos`→`/delete_photo` endpoints); per-photo `del` kept.
- **Type-in speed cap**: a number input synced with the slider, current value
  shown, read from `GET /speed` on load, applied via `/speed?cap=` and re-synced
  from the server's clamped response. Labelled "0..0.5, not m/s".
- **Command box**: type controller commands (`drive 0.2 0.2`, `camera_aim 30 0`,
  `move_forward 300`, `light_head on`, `speed 0.15`, `relax`, `stop`, …) → mapped
  to existing endpoints with arity/number validation, aliases, and an inline
  ✓/✗ result echoed via `textContent`.

**Deviations:** none of substance. Per review, `ms`/`deg` treated as optional
(omitted when absent); query strings via `URLSearchParams`.

**Tests/CI:** added `rovercontrol_webui_test.go` (page contains the new controls;
every command-box target route is non-404). `ci-local.sh` PASS — Go race-clean,
coverage 74.8%, 67 Python tests. The client-side JS parser logic is intentionally
not unit-tested in Go (it's verified by review + the route-existence test).

**Safety:** front-end only; all clamps/watchdog/e-stop remain server-side; the
command box cannot bypass them (no raw serial). No motion command was sent during
implementation/test — the page was only served and asserted (cat-safety).
