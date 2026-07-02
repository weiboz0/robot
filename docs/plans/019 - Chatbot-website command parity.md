# 019 - Chatbot ⇄ website command parity (union: both understand both names)

## Goal

The chatbot's `$`-commands and the website command box currently express the
same capabilities under different names (`up 15` vs `camera_up 15`, `photo` vs
`snapshot`, `cam 30 0` vs `camera_aim 30 0`, …). Make **each side accept the
other's names as aliases** so a user can type either vocabulary in either place.
Nothing anyone types today breaks.

## Alias map

### Chatbot learns the website names (agent_chat.rover_command)

| New alias | Behaves as | Notes |
|---|---|---|
| `camera_up/camera_down/camera_left/camera_right [DEG]` | `up/down/left/right [DEG]` | relative gimbal nudge |
| `camera_aim P T` | `cam P T` | absolute aim |
| `camera_center` | `center` | |
| `snapshot`, `snap` | `photo` | |
| `gimbal_relax` / `gimbal_lock` | `relax` / `lock` | |
| `light_head [on\|off]`, `light_base [on\|off]` | one-channel light set/toggle | tracks per-channel state in RoverCtl (PWM 255/0); no arg = toggle |
| `move_forward/move_back/move_left/move_right [MS]` | bounded wheel nudge | **new RoverCtl.nudge(direction, ms)**: rovercontrol → `POST /move_*?ms` (server auto-stop); serial/http → emulate with drive_for(cap-scaled speeds, ms/1000), ms clamped 0..5000 |

### Website learns the chatbot names (htmlPage CMD_ALIAS/parseCmd)

| New alias | Maps to | Notes |
|---|---|---|
| `up/down/left/right [DEG]` | `camera_up/...` | pure alias rows in CMD_ALIAS |
| `cam P T` | `camera_aim` | |
| `center` | `camera_center` | |
| `photo` | `snapshot` | |
| `move L R` | `drive L R` | website drive = normalized pulse; closest equivalent |
| `spinl [S]` / `spinr [S]` | `move_left/move_right?ms=S*1000` | seconds→ms conversion (clamped 0..5000); no arg = 600 ms (chatbot default 0.6 s) |
| `light F B` | `light_head on/off` + `light_base on/off` | PWM degrades to on(>0)/off(0), same as rovercontrol_client.lights |

### Deliberately NOT remapped (conflicting semantics — keep native, document)
- `drive`: chatbot = `L R [seconds]` wheel speeds −0.5..0.5 with auto-stop;
  website = `l r` normalized −1..1 single ~0.5 s pulse. Same word, different
  contract on each side — remapping would silently change magnitudes.
- `fwd` / `back`: chatbot = drive straight for **seconds**; website aliases to
  `move_forward/move_back` in **ms**. Keep each side's native meaning.
- Chatbot-only (no controller endpoint or Python-only): `oled`, `oledclear`,
  `demo`, `status`, `photos`, `speed`-query form, `find`/`screwdriver`, `dobot`.
  Stay chatbot-only; documented in the parity table.

## Implementation

1. **`rover_backend.py`**: add `RoverCtl.nudge(direction, ms)` —
   rovercontrol → `rovercontrol_client.nudge` (exists); serial/http → map
   direction to (l,r) at 0.2·(2·cap-scaled? no —) use the same speed the
   chatbot's fwd/back/spin commands use (0.2) through the existing `_scale`
   path, `drive(l, r, ms/1000)` so clamping + cap all apply. Clamp ms 0..5000.
   Add per-channel light state (`_head`, `_base`) + `light_channel(which, on)`
   (on=None toggles) built on the existing `lights()`.
2. **`agent_chat.py` `rover_command`**: normalize an ALIASES dict first
   (`camera_up→up`, `snapshot→photo`, `gimbal_relax→relax`, …), then handle the
   new distinct commands (`camera_aim`, `camera_center` fold into `cam`/`center`
   via the dict; `light_head/light_base [on|off]` → `r.light_channel`;
   `move_* [MS]` → `r.nudge`). Update `$help`.
3. **`rovercontrol.go` htmlPage**: extend `CMD_ALIAS` with the pure aliases
   (`up:'camera_up'`, `down:'camera_down'`, `left:'camera_left'`,
   `right:'camera_right'`, `center:'camera_center'`, `photo:'snapshot'`,
   `cam:'camera_aim'`, `move:'drive'`); add `spinl/spinr` (seconds→ms) and
   `light F B` (two requests) as small special cases in `parseCmd`. Update the
   ❔ Commands panel with an "also accepts" line.
   **Note**: `left/right` were previously unknown words in the box; as aliases
   they now mean CAMERA left/right (matching the chatbot), not wheel turns —
   called out in the help text to avoid surprise motion confusion.
4. **`docs/reference/controller-commands.md`**: one parity table for both
   surfaces, including the not-remapped and chatbot-only rows.

## Testing (fakes only, no motion)
- `tests/test_rover_cmd.py`: alias dispatch — `$camera_up 10` ≡ `$up 10`,
  `$snapshot` calls photo, `$camera_aim/camera_center/gimbal_*` route,
  `$light_head on/off/toggle` sets one channel and preserves the other,
  `$move_forward 300` calls nudge with clamped ms, unknown still errors.
- `tests/test_backend.py`: `nudge()` per backend — rovercontrol → client.nudge;
  serial → drive_for with ms/1000 and cap-scaled speed; ms clamp.
- `rovercontrol_webui_test.go`: page contains the new aliases (`up:'camera_up'`,
  `photo:'snapshot'`, `spinl`, `light F B` help line); existing non-404 route
  test already covers the targets.
- `./ci-local.sh` green.

## Risks
- Word collisions: chatbot `left/right` (camera) vs any wheel intuition — both
  sides now consistently mean CAMERA for bare `left/right`; wheel turns are
  `spinl/spinr`/`move_left/move_right`. Documented in both helps.
- Serial/http `move_*` emulation is time-based (blocking drive_for), unlike the
  server nudge — same observable behavior (bounded move then stop).
- Website `light F B` issues two sequential requests — tiny lag between
  channels; acceptable.

## Stages
1. Plan + 2-way plan review.
2. Backend nudge + light_channel; chatbot aliases; tests.
3. Website aliases + help; Go test.
4. Docs parity table.
5. ci-local, 3-way code review, PR, AUTO_MERGE, deploy (pull + rebuild/restart).

## Reviews
### Plan review (Opus + codex) — both REQUEST-CHANGES → resolved

**codex** (2 blocking):
- `move→drive` on the website is a pulse, not continuous → resolved: kept as an
  alias but labelled "ONE ~0.5s pulse, not continuous" in the help and CMD_ALIAS
  comment; also added `move` to the documented-conflicts list (Opus N3).
- Chatbot `light_head/light_base` must NOT layer local toggle state over
  `lights()` on rovercontrol → resolved: `rovercontrol_client.light_channel()`
  hits the native single-channel `/light_*` endpoint (server owns state, no
  drift); local `_light_*` tracking is the serial/http fallback only.

**Opus** (3 blocking + strong nits, grounded in code):
- B1 (same as codex #2, plus): on serial/http, `lights()` and `light_channel()`
  must share state → resolved: `lights()` now records `_light_head/_light_base`,
  so a `light F B` never leaves `light_channel` acting on stale values.
- B2: website `spinl 2` would make the sequencer's `motionMs` read "2" as 2 ms
  and under-wait → resolved: `motionMs` reads `ms` from the PARSED path (where
  seconds→ms conversion happened), not the raw token.
- B3: `light F B` needs two requests, breaking `parseCmd`'s single-path contract
  → resolved: a `multi` return shape handled explicitly in `sendCommand`
  (Promise.all) and inert in `motionMs` (no path → 0) / `addStep` (validates OK).
- N1: nudge emulation at 0.2 would be ~40% of the server's full-cap nudge →
  resolved: serial/http emulation uses ±0.5 (→ `_scale` yields exactly the cap,
  mirroring the server's ±1×cap).
- N2: website `spinl` (full-cap) ≠ chatbot `spinl` (gentle) → documented in the
  website help ("spins are gentler on the chatbot") and the docs table.
- N4/N5 confirmed: bare `left/right`=camera is the safer choice; keeping
  `drive/fwd/back` native is right.
- N6: JS special cases aren't executable-tested from Go (pre-existing) →
  acknowledged; logic-bearing pieces are unit-tested on the Python side and the
  motionMs/spinl behavior is asserted via page-content + code-review re-verify.
### Code review (Opus + codex + glm) — APPROVE (1 Opus blocker fixed + re-verified)

- **codex: APPROVE** — verified all five of its plan-blocker resolutions in code
  (native single-channel lights, move=pulse labelling, full-cap nudge emulation,
  spinl parsed-path ms, light-F-B multi handling).
- **glm: APPROVE** — traced alias tables, s↔ms conversions, light-state recording,
  clamps, error paths; nits only (duplicated clamp in a message, `indexOf('move_')`
  breadth, unreachable state mirror).
- **Opus: REQUEST-CHANGES → APPROVE.** Caught a real regression in the B2 fix:
  `Number(null)===0` is finite, so a bare `move_forward`/`fwd`/`back` made
  `motionMs` return 0 instead of the 400 ms server default — under-waiting the
  sequencer against a 400 ms nudge. Fixed (`q===null → NaN → 400` fallback) and
  re-verified by Opus: RESOLVED; also confirmed the `?ms=` empty-string edge is
  unreachable and flagged the fix hunk was unstaged (staged before commit).
- Verified clean across reviewers: CMD_ALIASES purely additive (no collisions),
  no Go raw-string/backtick hazards, XSS-safe.

## Post-execution report

**Implemented** the union parity per plan: the chatbot accepts the website
vocabulary (camera_*, camera_aim/center, snapshot/snap, gimbal_*, light_head/base
[on|off], move_* [MS] via new `RoverCtl.nudge`/`light_channel`) and the website
accepts the chatbot's (up/down/left/right=camera, cam, center, photo, move=drive
pulse, spinl/spinr seconds→ms, light F B). `drive`/`fwd`/`back`/`move` conflicts
kept native and documented; chatbot-only commands unchanged. Docs parity table
added; both helps updated.

**Deviations:** none material — all review resolutions were design tightenings
(native single-channel lights, full-cap nudge emulation ±0.5, motionMs parsed-path
+ null-guard, lights() state recording).

**Tests/CI:** 114 Python tests (10 new: ParityAliasTest, ParityBackendTest) + Go
web-UI assertions; `ci-local.sh` PASS. The JS special cases are not executable
from Go tests (known N6) — covered by the three-reviewer trace + re-verify.

**Deploy:** Python via git pull; Go page via rebuild/restart. No motion command
issued during dev, test, or deploy.
