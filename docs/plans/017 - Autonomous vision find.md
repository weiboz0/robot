# 017 - Autonomous vision find ($screwdriver)

## Goal

Let the chatbot drive the rover autonomously to **find an object and return a
photo of it**, deciding actions from the camera with a vision LLM — no human
input during the run. Command: `$find <object>` (and `$screwdriver` shortcut).
Ship with **camera-only safety** (no distance/cliff sensors exist) that is
best-effort, not a guarantee.

## Hard constraints / honesty
- The rover has **no distance/cliff/proximity sensors** — obstacle & drop-off
  avoidance is vision-only and cannot be guaranteed. First runs must be on a
  flat, enclosed, ledge-free floor.
- **No working vision model is currently reachable** (ARK not activated,
  DashScope key invalid, opencode has no usable image model). So vision is built
  **provider-agnostic** and returns a clear "no vision configured" error until a
  key is supplied. Recommended free option: OpenRouter (`OPENROUTER_API_KEY`,
  free vision models). Any OpenAI-compatible vision endpoint works.
- **The assistant never triggers motion.** All motion happens only when the user
  runs `$find`/`$screwdriver`. Dev/tests use fakes; no real drive is issued by us.

## Architecture (all chatbot-side; NO Go controller change)

Every endpoint needed already exists on the controller (`:8080`): `/move_*?ms=`
(bounded auto-stop nudges), `/camera_*?deg=`, `/camera_aim`, `/snapshot`,
`/photos/{name}`, `/speed`, `/stop`, `/estop`, `/healthz`.

1. **`rovercontrol_client.py`** — add `nudge(direction, ms)`,
   `camera_nudge(direction, deg)`, `get_photo(name) -> bytes` (fetch a frame's
   JPEG for the vision model).
2. **`vision.py`** — provider-agnostic `VisionModel`. Config from env
   (`VISION_PROVIDER|VISION_API_KEY|VISION_BASE_URL|VISION_MODEL`, with sane
   presets for openrouter/gemini/ark/dashscope). `describe(image_bytes, prompt,
   schema=None)` calls an OpenAI-compatible chat with an `image_url` data-URI and
   returns text or a validated JSON dict. Raises `VisionUnavailable` if
   unconfigured. Pure/mockable.
3. **`autodrive.py`**
   - `SafeDriver` — the safety envelope over a controller client:
     - context manager: on enter, snapshot the current speed cap and set a
       **crawl cap** (0.12); on exit ALWAYS `stop()` and restore the cap — even
       on exception/KeyboardInterrupt.
     - motion is **only bounded nudges** (`/move_*?ms`, server auto-stops); never
       continuous `/drive`. If our loop dies, the rover stops on its own.
     - `forward(clearance)` **requires** a `clearance()->bool` callback and moves
       only if it returns True (look-before-move enforced at the API). Returns
       False (no motion) when blocked.
     - `turn_left/right()`, `back()`, `look(pan,tilt)`, `scan()` (pan sweep).
     - hard caps: `max_steps` and `max_seconds`; every motion `_tick()`s the
       budget and raises `SafetyLimit` when exceeded (can't be bypassed by a loop
       bug). `_require_serial()` before each forward (no driving on a dead link).
     - `halt()`, `estop()`.
   - `find_object(driver, vision, target, *, on_frame)` — the loop:
     capture frame → ask vision for structured `{seen, bearing, distance,
     safe_ahead, action, reason}` → safety-gate (never forward unless
     `safe_ahead` AND no person/edge) → execute ONE bounded action → repeat until
     `seen && centered && close` (→ snapshot, return path) or budget exhausted
     (→ give up, stop). Conservative bias: scan/turn over forward.
4. **`agent_chat.py`** — `$find <object>` and `$screwdriver` (= `$find
   screwdriver`) construct a controller client + `SafeDriver` + `VisionModel` and
   run `find_object`; print progress + the final photo path, or a clear
   "vision not configured — set OPENROUTER_API_KEY" message. Guarded so any error
   halts the rover and returns to the prompt.

## Safety layers (defense in depth) — revised per codex crosscheck

**The one mechanism that survives loop death/hang** (verified in code): the
controller's `/move_*?ms=` nudge schedules a server-side `time.AfterFunc(ms,
stopWheels)` (rovercontrol.go:288) — motion auto-stops after `ms` **independent
of the HTTP client**. Everything else below only works while Python keeps running.

1. **Server-independent auto-stop** — bounded nudges only, never continuous
   `/drive`. If our loop crashes, hangs, or the network drops, the rover stops
   within `ms`. This is the real guarantee.
2. **Short HTTP timeouts on every controller call** (nudge/stop/snapshot/healthz,
   ~3s connect+read). A hung request can't block forever; a timeout *during
   motion* ⇒ best-effort `estop()` + abort the run.
3. **Independent run watchdog** — a `threading.Timer(max_seconds+margin)` armed at
   run start that calls `estop()` if the whole run overruns, even if the main
   loop is wedged (hard caps alone don't fire when code stops executing).
4. **Per-vision-call timeout** — the vision HTTP call has its own timeout; a
   hanging/slow model can't stall the loop indefinitely.
5. **Client-side nudge clamps** — `SafeDriver` enforces tiny maxima itself
   (forward ≤ 250 ms, turn/back capped) — not just the server's 0..5000 clamp.
6. **Forward cooldown** — a minimum interval between forward nudges so a stream of
   "safe" frames can't become effectively continuous motion.
7. **Look-before-move** — `forward()` requires a `clearance()->bool`=True. HONEST
   framing: this is a **software interlock**, not independent validation — the
   clearance comes from the same camera/model, so a false "safe" is still a real
   risk. Mitigated by: require **high-confidence clear floor in the LOWER image
   region** (near-field), tiny nudges, low speed, supervised first runs on a flat
   enclosed floor. Malformed / low-confidence / contradictory vision ⇒ treat as
   UNSAFE (fail closed).
8. **Hard step + wall-clock caps** that RAISE `SafetyLimit` (can't be bypassed by
   a loop bug) — but see (3): these need the loop alive, hence the watchdog.
9. **E-stop latch policy** — preflight detects a latched e-stop and `$find`
   **fails closed** (won't auto-clear it); refuses to run.
10. **Cleanup in `finally`, catching `BaseException`** — `KeyboardInterrupt`/
    `SystemExit` included. On any exit: `stop()`, verify/brief-wait, then restore
    the cap; if `stop()` fails/times out ⇒ `estop()` + report, and do NOT restore
    a higher cap.
11. **`ROVER_FIND_ENABLE=1` gate** — the autonomous command refuses to run unless
    this env flag is set, so it can't be triggered accidentally after deploy.
12. `_require_serial()` before each forward is advisory only (link can die right
    after) — (1) remains the real stop.

## Failure modes explicitly handled (codex)
loop hang mid-nudge → server AfterFunc stops; HTTP hang → per-call timeout →
abort+estop; vision hang → per-call timeout; vision wrong "safe" → near-field
floor check + tiny nudge + supervised; process killed after a nudge → server
stops after ms; KeyboardInterrupt → finally cleanup; e-stop latched → fail closed;
network partition mid-move → timeout → estop attempt + server auto-stop.

## Testing (fakes only — NO real motion, NO live vision)
- `SafeDriver`: crawl cap set on enter + restored on exit (incl. on exception);
  `forward` refuses when clearance False and nudges when True; step & time caps
  raise `SafetyLimit`; exit halts wheels; `_require_serial` blocks on dead link.
- `find_object`: found→returns a photo path; unsafe→never forwards (turns/scans);
  budget→gives up + halts; vision "seen&centered&close"→snapshot. Fake vision +
  fake driver.
- `vision.py`: builds the correct OpenAI-compatible request (mock client);
  `VisionUnavailable` when unconfigured; parses/validates JSON.
- `rovercontrol_client`: `nudge`/`camera_nudge` post the right URLs; `get_photo`.
- `./ci-local.sh` green (Python; Go unchanged).

## Deploy
Python-only ⇒ rover `git pull` (or run from the Mac). No controller rebuild.

## Risks
- Camera-only safety can miss a ledge/obstacle — documented; conservative tuning;
  supervised first runs on flat enclosed floor; the real fix is a ToF/ultrasonic
  sensor (offered separately).
- Vision unvalidated live until a key exists — the loop/JSON parsing is tested
  with mocks; the first live run is supervised and may need a prompt tweak.

## Stages
1. Plan + crosscheck (codex/Opus).
2. Client nudges + vision.py + autodrive.py + agent_chat wiring.
3. Tests (fakes).
4. ci-local + 3-way code review (Opus/codex/glm) — adversarial on safety.
5. PR + merge + deploy (git pull). Then: user supplies a free vision key →
   supervised live validation (rover looks/reasons; user runs the command).

## Reviews
### Plan review (codex + Opus) — both REQUEST-CHANGES → resolved

Both grounded the review in the controller code. Verified TRUE: nudge auto-stop is
server-side + client-independent (rovercontrol.go:288); `ms` clamped 0..5000 +
speed scaled by the server cap; **no Go change needed** (all endpoints exist);
control endpoints 503 on dead serial.

Resolved BLOCKING/should-fix items, now in the design:
- **(Opus) Look where you drive.** `forward()` first aims the gimbal to the travel
  direction (pan 0, downward floor tilt), lets it settle, then requires a FRESH
  clearance frame from that view — never reuses the loop's object-detection frame
  (which may be seconds old and aimed at the object, not the floor). Turns/scan
  re-center the camera afterward. This is the core safety invariant.
- **(codex) Hangs.** Short HTTP timeouts on every controller + vision call; an
  independent `threading.Timer` watchdog that `estop()`s if the run overruns even
  if the loop is wedged; per-forward client-side ms clamp; forward cooldown.
- **(codex) Cleanup** in `finally`, catching `BaseException`; normal exit ends with
  `stop()` (which **clears the e-stop latch** — Opus #3) so manual driving isn't
  left silently refused; `estop()` is reserved for genuine emergencies (watchdog /
  unverifiable stop).
- **(Opus) Vision-first.** Check vision is configured BEFORE constructing
  `SafeDriver` (which POSTs the crawl cap, shared with the gamepad) — an
  unconfigured `$find` is a pure no-op that never touches the rover.
- **(Opus) Snapshot race.** `snapshot()` returns the exact frame name from its
  response; `get_photo(name)` fetches that one (the gamepad can snapshot
  concurrently, so `list_photos()[0]` is unsafe).
- **(codex/Opus) Fail-closed.** Malformed / low-confidence / contradictory vision
  ⇒ treated as UNSAFE. Refuse to start unless `healthz.camera.up` (no camera =
  no sensing = no motion). `ROVER_FIND_ENABLE=1` required to run at all.
- **(Opus) Scan** bounded to a forward arc (±60°), not ±180°.
- **(Opus #4) Honesty added to Risks:** the Go controller process is the sole stop
  authority — the ESP32 holds the last speed with no firmware heartbeat, so if the
  *controller itself* is killed mid-nudge the wheels run until power-off. Real fix:
  a firmware heartbeat (offered alongside the ToF sensor). Python-side failures are
  all covered by the server AfterFunc auto-stop.
### Code review (Opus + codex + glm) — all REQUEST-CHANGES → resolved / documented

**All three agreed the runaway-prevention core is SOUND** (verified): motion is only
bounded `/move_*?ms` nudges that auto-stop server-side; no continuous `/drive`;
`with driver:` cleanup on Exception/BaseException/KeyboardInterrupt; fail-closed
vision; gates ordered so an unset flag / unconfigured vision never touches the rover.

Resolved in code:
- **Gimbal-tilt trust (all 3, top blocker):** the "look-where-you-drive" frame is
  only meaningful if the camera actually tilts down. Resolved by an explicit,
  cited invariant: this Go controller's `initLink` selects the Gimbal module on
  every connect (verified by `TestInitLink`), so on serial-up (preflight-checked)
  the tilt IS honored. The CLAUDE.md `module_type=0` caveat applies to the retired
  stock app.py, not this controller. Documented loudly in `__enter__`.
- **(Opus) Untested movement gate** → `tests/test_autonomous_find.py`: unset flag /
  unconfigured vision / wrong backend each make ZERO rover contact.
- **(glm) Unsafe `back()`** removed (no rear camera). **(glm) HTTP timeout** restored
  on exit. **(glm/Opus) Confidence floor** on "found". **(Opus) Vision-error bail**
  after N failures (no spinning on a dead API). **(codex) Watchdog** cancelled AFTER
  the stop. Message reworded. + KeyboardInterrupt/vision-error/confidence tests.

**Residual gaps — NOT fully solved in code; documented as hard preconditions**
(this is why the feature ships DISABLED-by-default / experimental):
- **(codex C1) Multi-source control.** The nudge auto-stop is superseded if another
  client (`gamepad`, web UI `/drive`) issues a command mid-run. Operator precondition:
  do not drive manually during a `$find` run. The proper fix is a controller-side
  **exclusive-autonomy lock / session TTL** (a Go change) — recommended before real use.
- **(codex C3) Turns aren't floor-gated** — near-in-place on a differential rover
  (minimal translation), kept tiny; forward is the gated path. A side-ledge during a
  turn is unaddressable with one forward camera.
- **No cliff/proximity sensor** — the genuine fix. Camera-only remains best-effort.
- **(Opus) Controller is the sole stop authority** — no ESP32 firmware heartbeat; if
  the controller process itself is killed mid-nudge, wheels run until power-off.

## Post-execution report

**Built** (all chatbot-side, NO Go change): `autodrive.py` (SafeDriver envelope +
find_object loop), `vision.py` (provider-agnostic vision client), client
nudge/camera_nudge/get_photo + snapshot-returns-name, `$find`/`$screwdriver` wiring.
104 Python tests (37 new), ci-local PASS.

**Status: EXPERIMENTAL, DISABLED by default.** It cannot move the rover unless a
user sets `ROVER_FIND_ENABLE=1`, configures a vision model, AND runs the command.
No vision model is reachable yet (needs a free key — OpenRouter recommended), so it
is currently inert. The assistant never runs it; motion happens only on the user's
command. First real use must be supervised, on a flat enclosed ledge-free floor,
with no manual driving during the run, ready to E-STOP.

**Recommended before trusting it near any drop-off:** (1) a controller exclusive-
autonomy lock (closes codex C1), and (2) a ToF/ultrasonic cliff sensor. Both offered
as follow-ups.
