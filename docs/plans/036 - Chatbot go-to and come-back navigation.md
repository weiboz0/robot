# Plan 036 — Chatbot navigation: go to an object, detail photo, come back

## Goal

User story, verbatim: *"go to the suitcase and take a photo of a wheel"* —
the chatbot drives to a visible object and photographs a named detail of
it; *"come back"* — it backtracks to where it started. Two new tools:

- `rover_go_to {target, photo_of?}` — approach a VISIBLE object with the
  existing safety envelope; on arrival, optionally center the camera on the
  named detail (`photo_of`) and take the photo.
- `rover_come_back {}` — drive back along the recorded pose trail to the
  position where the last `rover_go_to` started.

## What exists (verified in code — this plan is mostly reuse)

- `autodrive.SafeDriver` (autodrive.py:50): crawl speed cap, tiny bounded
  nudges (forward ≤400 ms, turns ≤600 ms), forward cooldown, step + time
  caps, an independent watchdog timer that ESTOPS if the run overruns,
  context-manager stop/estop + cap restore, serial/camera preconditions.
- `SafeDriver.forward(clearance)` — look-where-you-drive: gimbal aims
  forward+down and a FRESH `floor_is_clear` vision verdict gates every
  single forward pulse. (The cat is why this stays mandatory.)
- `find_object` — gimbal sweep + bounded in-place rotation until the target
  is sighted (no forward motion; plan 022 deliberately removed approach
  from *find*); `_refine_center` centers the camera on a bbox;
  `look_for`/`FIND_PROMPT` (bbox/bearing/close/confidence), CV fast path
  for color targets; close criteria `CLOSE_BBOX_DIM`/`CLOSE_BBOX_BOTTOM`
  (tuned on the real $pen failure).
- Pose: `get_pose()` client+backend passthrough (plan 033), server trail
  `/pose_trail` (plan 032, 5 cm spacing, origin-seeded).
- Gate precedent: `ROVER_FIND_ENABLE=1` + rovercontrol backend, checked
  BEFORE any rover call (agent_chat.autonomous_find).

## Design

### autodrive.py additions

0. **Turn clearance — defined geometry** (codex adversarial catch, round
   2 spec). An in-place differential turn sweeps ≈ the rover's own
   bounding circle; the camera can see the FRONT half of that circle
   (three floor-tilt views), and can never see the rear (no rear camera —
   the same physical limit that forbids `back()`).
   - **Turn-zone survey**: at the start of every turn SEQUENCE (one
     `turn_to_heading` run, or one alignment turn series), capture fresh
     `clearance()` verdicts at `pan=-40°, 0°, +40°` (all `floor_tilt`) —
     ALL three must be clear. A survey is valid for the sequence only:
     invalidated by any forward pulse or `TURN_SURVEY_TTL_S = 10` seconds.
   - **Per-pulse re-check**: within a valid survey, each individual turn
     nudge (`SafeDriver.turn_gated(direction, ms, clearance)`) still
     requires ONE fresh `clearance()` on the turn-direction view
     (`pan=±40°`) immediately before the nudge.
   - **Residual blind zone, stated plainly**: the rear half of the swept
     circle — including the rear corner that swings INTO the turn (Opus
     precision: the ±40° view sees the front corner swinging in, never
     the rear-swinging quarter) — is unobservable with this hardware. Mitigations: pulses are
     tiny (≤600 ms at crawl cap), the survey caught anything approaching
     from the front seconds earlier, and rotation-in-place is the
     pre-existing consented behavior class (`find_object` has rotated
     un-gated since plan 022 — this plan is strictly safer than that
     baseline). This residual risk is accepted and documented, not hidden.
   Approach and backtrack use ONLY gated turns. Every autonomous wheel
   nudge in this plan — forward AND turn — has a fresh floor check.

1. **`approach_object(driver, vision, target, *, capture, log, look=None)`**
   — the resurrected approach phase, floor-gated end to end:
   - Phase A: sight the target with a sweep. `find_object` returns only a
     photo name AND opens its own `with driver:` context (both reviewers —
     calling it nested would poison `_entered` and the caps), so the sweep
     core is extracted into `_sweep_for(driver, looker, capture, ...) →
     (obs, pan, tilt) | None` which does NO context management (requires
     an already-entered driver) and **NO base rotation** (Opus contract
     pin — the between-sweep `turn_left(ROTATE_MS)` stays in
     `find_object`; a rotating `_sweep_for` would smuggle ungated turns
     past this plan's gated-only guarantee); `find_object` keeps its own
     context, rotation, and behavior (existing tests unchanged),
     `approach_object` runs `_sweep_for` inside its own single context —
     gimbal-sweep-only from the current spot. Not sighted → honest
     failure.
   - Phase A2 — **body alignment** (codex catch: a sighting at camera
     pan=50° must never trigger a body-forward pulse — that vector is
     wrong): while the sighting pan is outside `BODY_ALIGN_TOL = 10°`,
     `turn_gated` toward it in bounded steps (~pan/2 clamped to turn_ms),
     re-aim the camera at pan 0 / sighting tilt, re-sight. Forward pulses
     are permitted ONLY when |camera pan| ≤ 10° AND bbox center-x is inside
     `BEAR_LEFT..BEAR_RIGHT`.
   - Phase B loop (budget-ticked): capture → `look_for` → bbox off-center →
     small `turn_gated` toward it, re-sight; centered → `driver.forward`
     (fresh `floor_is_clear` per pulse, unchanged); floor blocked → stop
     "path blocked".
   - Stop when **close** (existing criteria: bbox dim ≥ `CLOSE_BBOX_DIM`
     or bottom ≥ `CLOSE_BBOX_BOTTOM`) → halt, `_refine_center`, return the
     final observation. Lost sight `LOST_MAX=3` consecutive looks → honest
     stop. All under SafeDriver caps + watchdog.

2. **`turn_to_heading(driver, get_pose, target_deg, *, tol_deg=12,
   max_pulses=10, clearance)`** — closed-loop in-place turn on pose
   feedback, gated turns only. **Sign auto-detection, fully specified**
   (codex catch — a tiny/noisy first delta must not lock in a wrong
   mapping): calibration pulse at full `turn_ms`; require
   |Δheading| ≥ `MIN_CAL_DELTA_DEG = 3°`; smaller → ONE retry at 1.5×
   pulse; still smaller → `SafetyLimit` ("pose isn't registering turns —
   refusing to navigate blind"), fail-closed. The second pulse must agree
   with the detected sign; disagreement → `SafetyLimit`. Only then does
   the convergence loop run. Stale pose (`fresh: false`) at ANY read →
   `SafetyLimit`, mid-loop included.

3. **`backtrack(driver, get_pose, trail, home, *, wp_spacing=0.4,
   arrive_m=0.3, clearance)`** — return-to-home along the *recorded*
   trail (the path it actually drove — known passable minutes ago — beats
   a straight line through unknown floor):
   - **Segmentation by INDEX, not nearest-point** (codex catch — a path
     that crosses near home would let nearest-point matching pick the
     wrong segment): `rover_go_to` records `home = {pose,
     "trail_len": len(get_trail())}` before driving; backtrack uses ONLY
     `trail[home.trail_len - 1:]` (the points recorded since). If the
     bounded deque evicted into that segment (>100 m driven — detectable
     when the slice start no longer matches home within 0.5 m), fall back
     to the surviving suffix with `home.pose` as final waypoint and say so
     in the report. Bearings use `atan2(dy, dx)` in the pose frame
     (heading CCW+, x fwd at reset — same frame, pinned in tests).
   - Waypoints: that segment reversed, subsampled to ≥ `wp_spacing`;
     `home.pose` is always the final waypoint.
   - Per waypoint: bearing from current pose → `turn_to_heading` → floor-
     gated `forward` pulses while the waypoint distance decreases; distance
     increasing for 3 consecutive pulses → re-aim (turn again); waypoint
     done at `arrive_m`. **Every pose read used for a motion decision
     checks `fresh`** (Opus N2 — not just turn entry): stale →
     `SafetyLimit` stop with the partial report.
   - The rover drives FORWARD along the reversed path (turn around first),
     so look-where-you-drive holds the whole way; every forward pulse
     re-checks the floor (the path was clear earlier — the cat may be on
     it now).
   - Ends: within `arrive_m` of home → success ("back where I started,
     ~0.2 m off"); caps/blocked/stale-pose → stop + honest partial report
     with the remaining distance.

### Backend surface (Opus B1 — the trail has NO client-side fetch path)

- `rovercontrol_client.get_pose_trail()` → GET `/pose_trail` parsed;
  `RoverCtl.get_pose_trail()` — rovercontrol only, `RuntimeError`
  sentinel; both pinned in `RealBackendSurfaceTest`.

### agent_chat.py

- **Gate — its own flag** (Opus B4, cat-safety: `ROVER_FIND_ENABLE` was
  consented for camera-sweep + in-place rotation ONLY; forward driving
  across the floor is a materially higher risk class and must not
  piggyback): both new tools require **`ROVER_GO_ENABLE=1`** AND the
  rovercontrol backend, checked before ANY rover call (same pattern as
  `autonomous_find`). The refusal message says exactly what the flag
  authorizes ("drives across the floor on its own") so enabling is
  informed consent. `ROVER_FIND_ENABLE` keeps its rotation-only meaning.
- **Nav home state**: module-level
  `_NAV_HOME = {"pose": None, "trail_len": None}` (codex round-2 catch —
  the earlier wording stored only the pose while backtrack depends on the
  index). `rover_go_to` records BOTH `get_pose()` (fresh required) and
  `len(get_pose_trail()["trail"])` BEFORE driving. `rover_come_back`
  refuses politely when unset ("I haven't driven anywhere yet this
  session") or when pose is stale.
- **`rover_go_to {target, photo_of?}`**: gate → record home → SafeDriver →
  `approach_object` → on arrival: if `photo_of`, `_refine_center` on the
  detail target (e.g. "wheel of the suitcase" — the full phrase passed to
  the vision looker) then snapshot; else snapshot the arrival view. Reply:
  what happened, distance driven (pose delta), photo name, and "say 'come
  back' to return".
- **`rover_come_back {}`**: gate → trail+pose → `backtrack` → reply with
  the outcome and remaining offset. Clears `_NAV_HOME` on success.
- **Invocation routing hardened** (codex catch — descriptions alone are
  weak, and the system prompt currently tells the model to use
  `rover_find_object` for any "find/look for" phrasing while its stale
  description still claims it "drives toward" things):
  - System-prompt rule added: wheel-motion tools (`rover_go_to`,
    `rover_come_back`, `rover_find_object`) require the USER's message to
    explicitly ask for travel ("go to", "drive to", "come back");
    "find / look for / where is" language routes to the non-driving tools
    (`rover_where_is`, `rover_scan_for`) — never wheel motion.
  - `rover_find_object`'s description corrected to match its actual
    rotation-only behavior (the approach phase was removed in plan 022).
  - `rover_go_to`/`rover_come_back` descriptions: PHYSICALLY DRIVES; only
    on an explicit user request to go somewhere; refuses when disabled.
  HELP_TEXT line added.
- Turn budget: `SCANFOR`-style single monotonic deadline is already inside
  SafeDriver (`max_seconds` + watchdog); the chat turn holds as with the
  other long tools.

### Non-goals

- No obstacle avoidance beyond the floor-clear gate (no depth sensor).
- No arbitrary go-to-coordinates; targets are visible objects only.
- No reversing (camera can't see backward — unchanged autodrive rule).
- Streaming progress → plan for chat streaming later.

## Deliverables

- autodrive.py: `SafeDriver.turn_gated`, `_sweep_for` extraction,
  `approach_object`, `turn_to_heading`, `backtrack` (+ constants).
- rovercontrol_client.py + rover_backend.py: `get_pose_trail`
  passthroughs (+ surface pins).
- agent_chat.py: both tools + `ROVER_GO_ENABLE` gate + `_NAV_HOME` +
  system-prompt routing rule + corrected `rover_find_object` description +
  HELP_TEXT.
- docs/reference/controller-commands.md: tool mentions.
- Tests (below). NO live driving during dev/test — fakes only (standing
  cat-safety rule; the user enables and fires the real thing themselves).

## Testing (fakes only, patterned on tests/test_autodrive.py)

Highest-risk assertions first (codex's list, adopted verbatim):
- **No wheel nudge of ANY kind without a fresh clearance call** — a fake
  client that records call order proves clearance→nudge pairing for every
  forward AND every gated turn in approach and backtrack.
- **Turn-zone survey semantics** — a turn sequence begins with the 3-view
  survey (all three aims observed in the fake's call log); a survey is NOT
  reused after a forward pulse or TTL expiry (new survey demanded); one
  dirty view (clearance false at pan=+40°) → zero turn nudges.
- **Camera-pan sighting never forwards until the body is aligned** — a
  scripted sighting at pan=50° must produce gated turns (and zero forward
  nudges) until the re-sight lands within 10°.
- **Tiny/no heading delta fails closed** — calibration pulse with a fake
  pose that doesn't move → one retry → SafetyLimit, zero further nudges.
- **Trail crossover picks the go-to segment** — a trail that loops back
  near home must still backtrack the POST-home segment (index slicing).
- **Routing refuses ambiguous language** — the system prompt carries the
  explicit-travel rule (string pin), and `rover_find_object`'s corrected
  description no longer claims driving.

Plus:
- `approach_object`: centered → forward with clearance EVERY pulse; floor
  blocked → stop, no nudge; close-by-size and close-by-bottom both stop;
  lost sight 3× → honest stop; caps → SafetyLimit; `photo_of` runs
  `_refine_center` with the detail phrase.
- `turn_to_heading`: correct-sign convergence; INVERTED-sign fake pose
  (auto-detect flips, still converges); sign-disagreement on pulse 2 →
  SafetyLimit; stale pose mid-loop → SafetyLimit; max_pulses cap.
- `backtrack`: waypoints subsampled + followed in reverse; distance
  increase → re-aim; arrival tolerance; blocked floor mid-way → partial
  report with remaining distance; stale pose MID-DRIVE → SafetyLimit stop
  (codex: freshness horizon is 1.5 s — realistic); evicted-segment
  fallback; empty/short trail → home-only.
- `_sweep_for` extraction: `find_object` behavior unchanged (existing
  FindObjectTest still green).
- Tools: `ROVER_GO_ENABLE` gate refuses with no env (ZERO client calls —
  pin with a fake that raises on any call), and `ROVER_FIND_ENABLE=1`
  alone does NOT unlock the driving tools (the conflation Opus blocked);
  no-home come_back refusal; home records pose AND trail length before
  driving; success replies contain the photo name / offset.
- `RealBackendSurfaceTest`: `get_pose_trail` pinned on both surfaces.

## Risks

- **This is chatbot-commanded wheel motion** — the highest-risk feature in
  the repo. Mitigations are the existing proven envelope (SafeDriver caps,
  watchdog-to-estop, floor-gated pulses, crawl speed), the env gate, and
  tool descriptions that force explicit user intent. Reviews get
  adversarial prompts.
- **Odometry trust**: signs still uncalibrated → `turn_to_heading` auto-
  detects at runtime; distances are relative (drift over a short indoor
  run is acceptable at 0.3 m arrival tolerance).
- **Trail may include pre-go-to wandering** — backtrack follows only the
  segment recorded since `home`, sliced by the trail INDEX captured at
  go-to start (never nearest-point matching — see design).
- **Cumulative turn scrub** (Opus N4): a 180° turnaround plus per-waypoint
  corrections is many in-place pulses; each is now floor-gated
  (`turn_gated`), the global step cap and watchdog bound the total, and
  skid scrub between pulses is small — accepted with eyes open.

## Stages

1. `turn_to_heading` + tests (pure logic, no vision).
2. `approach_object` + tests.
3. `backtrack` + tests.
4. Tools + gate + docs.
5. CI, adversarial review gate, PR.

## Reviews

### Plan review (adversarial by design — wheel motion + cat)

- **codex** — round 1: BLOCKED ×5: (1) turns not floor-gated; (2) approach
  forwards on a camera-frame-centered bbox while the sighting pan ≠ 0 —
  drives off-target; (3) sign auto-detect unspecified for tiny/noisy
  deltas; (4) nearest-point trail segmentation picks the wrong segment on
  path crossover; (5) routing/prompt too weak vs LLM auto-invocation, and
  `rover_find_object`'s description still claims driving it no longer
  does. **Resolution**: `turn_gated` (look-where-you-turn) for every
  autonomous turn; Phase A2 body alignment (forward only at |pan| ≤ 10°);
  MIN_CAL_DELTA + one retry + fail-closed + second-pulse agreement;
  index-sliced segmentation; system-prompt travel-verb rule + corrected
  descriptions; the five highest-risk test assertions adopted verbatim.
- **Opus** — round 1: BLOCKED ×5: (B1) NO client/backend trail fetch
  exists — the plan's core input had no code path; (B2) `find_object`
  returns only a photo name AND owns its driver context — the claimed
  reuse can't nest; (B3) same off-target approach bug as codex, traced to
  `forward()` snapping the gimbal to pan 0; (B4) gate conflation —
  `ROVER_FIND_ENABLE` consents to rotation-only, floor-driving must not
  piggyback (cat rule); (B5) tests as planned would PASS the B3 bug.
  Non-blocking: min-delta guard (adopted), fresh-pose on every motion
  decision (adopted), world-frame atan2 pinned (adopted), cumulative turn
  scrub noted (risk section). Confirmed: caps/watchdog envelope, in-place
  turn odometry, gate-before-any-call mechanism. **Resolution**:
  `get_pose_trail` passthroughs + pins added; `_sweep_for` extraction with
  explicit no-context contract; body-alignment phase; new
  `ROVER_GO_ENABLE` flag with informed-consent refusal text; pan≠0 and
  conflation tests added.
- **Re-verification** — codex round 2: 8/10 RESOLVED, still blocked on
  turn-footprint geometry (heuristic, not defined) and a `_NAV_HOME`
  wording inconsistency → fixed with the 3-view turn-zone survey spec
  (TTL, forward-invalidated, per-pulse directional re-check, documented
  rear blind zone + survey-semantics tests) and the pose+trail_len home.
  codex round 3: PASS on both. Opus round 2: PASS — all five blockers
  line-confirmed against code; two clarifications adopted (rear-swinging
  quarter wording; `_sweep_for` NO-base-rotation contract pin).

### Code review (adversarial)

- **glm-5.1** — PASS. Verified all five safety invariants enforced and that
  each safety test bites; non-blocking: uncalibrated ALIGN_MS_PER_DEG
  (honest lost-sight stop on mismatch, accepted), pan=50 test partly
  vacuous (strengthened: now requires ≥2 post-alignment forwards),
  get_pose None guard (added), eviction-suffix waypoint truthfulness
  (safety independent of it — accepted).
- **codex** — round 1 BLOCKED ×3, all real: (B1) backtrack ignored a
  non-converged turn and forwarded misaligned; (B2) the calibration retry
  could exceed max_pulses; (B3) the turn survey leaked across SafeDriver
  re-entry. **Fixed + pinned** (convergence checked at both call sites;
  hard pulse cap; `__enter__` clears the survey; three new biting tests +
  sign-disagreement test). Round 2: all three confirmed; caught one
  regression (a test asserting the replaced "search while driving" text) —
  fixed, CI green. 
- **Opus** — PASS. PROVED the no-nudge-without-fresh-clearance invariant
  (grep of every motion primitive + hand-traces + instrumented test
  verification); line-confirmed all ten plan blockers delivered.
  Non-blocking notes: come_back retry first retraces inbound trail points
  (safe, wasteful — deferred), repeated go_to re-homes to the latest start
  (documented semantics), tilt never re-optimized during approach
  (bbox-bottom criterion covers proximity), reuse-mode turn stuck-detection
  absorbed by backtrack's convergence check.

## Post-execution report

Implemented as revised through three plan-gate rounds and two code-gate
rounds. autodrive: `turn_gated` + 3-view turn-zone survey (TTL,
forward-voided, re-entry-cleared), `_sweep_for` (no context, no rotation),
`approach_object` (body-align before any forward, floor-gated everything,
close-criteria stop, honest lost/blocked/budget ends), `turn_to_heading`
(fail-closed runtime sign calibration, hard pulse cap), `backtrack`
(index-sliced waypoints, convergence-before-forward, fresh-pose per
decision, NaN-safe). Tools `rover_go_to`/`rover_come_back` behind the NEW
`ROVER_GO_ENABLE` consent (separate from find's rotation-only flag),
motion routing rule in the system prompt, corrected stale descriptions.
36 new tests (test_goto) incl. every review-demanded biting assertion.

Deferred: come-back-retry retracing inbound points (Opus NB — safe,
wasteful); ALIGN_MS_PER_DEG calibration; tilt re-optimization during
approach. NOT live-fired — driving requires the user to set
ROVER_GO_ENABLE=1 on the rover and explicitly ask in chat (cat rule).

Outcomes: plan gate codex BLOCKED→BLOCKED→PASS + Opus BLOCKED→PASS; code
gate glm PASS + codex BLOCKED→PASS + Opus PASS (invariant proven). CI
green (452 tests).
