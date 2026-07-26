# Plan 037 — Go-to v2: scan the whole area first, detour around obstacles

## Goal

User request, verbatim: *"make it so that it scans whole area first then
goes to it (and makes sure it doesn't bump into any objects while going
there?)"*. Two upgrades to plan 036's `rover_go_to`:

1. **Search-first**: today the approach only sweeps the gimbal (±50°) from
   the current spot — a target behind the rover is "not visible from
   here". New: sweep, and if not sighted, rotate in place (GATED turns,
   unlike `find_object`'s legacy ungated rotation) and sweep again —
   approximately a full circle, as the search-phase time cap allows —
   before driving anywhere. Coverage is best-effort within the cap and
   the tool says so (never "guaranteed 360°").
2. **Obstacle detours**: today a blocked floor ahead = full stop. New: a
   bounded detour — probe the left/right floor views, turn (gated) toward
   a clear side, take one gated forward pulse, re-sight the target, and
   resume; the normal bearing-correction steering then curves the path
   back toward the target. Boxed in on all sides or out of detour budget →
   stop honestly, exactly as today.

Anti-bump remains what it always was, now with MORE coverage, never less:
every forward pulse and every turn still requires a fresh vision floor
verdict (the `FLOOR_PROMPT` fails closed on ANY object/edge/drop within
~30 cm); detours never bypass a gate — they add gated alternatives where
plan 036 simply gave up.

## Design (autodrive.py — everything inside the existing SafeDriver caps)

### `search_around(driver, looker, capture, clearance, *, max_rotations=5,
phase_cap_s=SEARCH_PHASE_S, log)` → `(obs, pan, tilt, why) `

Budget-realistic search (codex catch — the original arithmetic didn't
close: 6 full sweeps × 10 looks × 20-45 s/LLM-look dwarfs any wall
budget):

- **Coarse search grid**: search sweeps use `SEARCH_PANS = (-50, 0, 50)` ×
  `SEARCH_TILTS = (-18,)` — 3 looks per viewpoint, not 10. The fine
  10-look grid stays for the plan-036 stationary sweep (search=False) and
  for `find_object`. With the CV fast path (color targets) a look is
  milliseconds and the full circle completes easily; with the LLM path the
  phase cap governs.
- **Own phase cap**: `SEARCH_PHASE_S = 240` inside the run's overall
  budget — when it expires without a sighting, return honestly with
  `why = "couldn't spot it in the time I had — I looked around as far as
  time allowed"`. Coverage is BEST-EFFORT within the cap, and the tool
  reply says so; the cap is authoritative, never the coverage claim.
- Not sighted at a viewpoint → `turn_gated(ROTATE_MS)` (nominally ≈60°;
  **the rotation angle is unverified** — battery/floor/slip can under- or
  over-rotate, so all wording is "look around, approximately a full
  circle", never "guaranteed 360°" (codex catch); pose-verified rotation
  is noted as follow-up work) → sweep again; up to `max_rotations`.
- A dirty turn view → stop the search with `why = "floor not clear for
  turning — couldn't look further around"`; never an ungated turn.
- **Per-rotation re-survey is intended** (Opus N1): a sweep takes longer
  than the 10 s survey TTL, so every search rotation pays the full
  3-view survey — that is the safe outcome and its cost is inside the
  search phase cap. The ±40° survey views vs a ~60° rotation widen the
  unsurveyed arc relative to plan 036's small alignment nudges — stated
  plainly; still strictly safer than `find_object`'s existing UNGATED
  550 ms rotation, the consented baseline.
- `find_object` itself is untouched (its ungated rotation is pre-existing
  consented behavior under ROVER_FIND_ENABLE; migrating it is separate
  follow-up work).

### Detours in `approach_object`

New constants: `DETOUR_MAX = 3` (per approach run), `DETOUR_TURN_MS = 350`
(≈45° at default gain), `DETOUR_PROBE_PAN = 40.0`.

**Survey invalidation on evidence of a hazard** (Opus B1 — a new
collision path unique to detours): a blocked `forward()` returns False
BEFORE reaching the line that voids `_survey_at`, so the detour's first
`turn_gated` could reuse a ≤10 s-old survey whose center view the failed
floor check has just contradicted — and an in-place rotation sweeps the
bounding circle. Fix (in `SafeDriver.forward`): when the clearance
verdict is False, **void `_survey_at`** — a fresh hazard sighting
invalidates every cached clearance. The detour's turn therefore always
runs a full fresh 3-view survey. (Plan 036 never exposed this: a blocked
forward was always a terminal stop.) An ordering test pins it.

When `driver.forward(clearance)` returns False (path ahead blocked):
1. If `detours == DETOUR_MAX` → return "path blocked — out of detour
   attempts" (honest stop, wheels stopped).
2. **Side probe** (camera only): aim `(−40°, floor_tilt)` → fresh
   `clearance()`; aim `(+40°, floor_tilt)` → fresh `clearance()`. Neither
   clear → "boxed in — stopped" (no nudge). One/both clear → pick the
   clear side; both clear → the side closer to the target's last bearing
   (bbox cx < 0.5 → left, else right).
3. `turn_gated(side, DETOUR_TURN_MS, clearance)` → one
   `forward(clearance)` (both still individually gated — the probe result
   is advisory, the gates remain authoritative; per the B1 fix this turn
   ALWAYS runs a fresh full survey). Either refusing → honest stop.
   `DETOUR_TURN_MS` is **open-loop** (Opus N2, disclosed): the true angle
   on this never-calibrated rover is a guess; safety holds because
   `forward()` re-gates whatever heading results — only detour
   *effectiveness* is probabilistic, and the honest-stop paths cover the
   misses. (A pose-closed detour turn is noted as follow-up.)
4. **Reacquire mini-sweep** (both reviewers — a ~45° side turn + pulse
   moves the target well off the single pan-0 view; "bearing correction
   will curve back" was an overclaim): after the detour pulse, sweep the
   camera **biased OPPOSITE the detour turn** (turned right → look left
   first): `(∓55°, ∓25°, 0°, ±25°)` at the sighting tilt; first sighting
   wins, the main loop resumes from that pan (body alignment then zeroes
   it), and the `lost` counter RESETS on reacquisition (losses must not
   compound across detours — Opus N3). Still not seen → the normal
   lost-counter path (honest stop). `detours += 1`.
5. **Disclosed behavior change**: v2 can stop FARTHER from the start than
   v1 would have (v1 stopped at the first block; v2 may spend a detour
   before stopping). Accepted: every position it stops at was reached
   fully gated, the reply says exactly what happened, and `rover_come_back`
   still returns to the recorded home (which a detour never changes).

The step/time caps and watchdog bound the whole thing exactly as before;
a detour consumes normal budget ticks.

### `approach_object` signature

Gains `search=False` and `detours=0` keywords (defaults keep plan-036
behavior and its tests byte-stable — codex round-2 catch: with detours
always on, a blocked forward would return different wording and the
"existing tests untouched" claim would be false); `rover_go_to` passes
`search=True, detours=DETOUR_MAX`.

### agent_chat.py

- `rover_go_to` passes `search=True` and a bigger budget for the richer
  run: `SafeDriver(client, max_steps=80, max_seconds=480)` with the
  search phase internally capped at `SEARCH_PHASE_S = 240` — so at least
  half the wall budget always remains for the approach + detours (codex
  budget-arithmetic catch; the watchdog margin scales with max_seconds as
  designed, and the chat turn holds as with every long tool).
- Tool description updated: "looks all around first (turning in place),
  and will steer around obstacles it can see; still stops and says so when
  boxed in". Same `ROVER_GO_ENABLE` consent — the risk class is unchanged
  (gated floor driving), no silent expansion (the flag's refusal text
  already says "drives across the floor on its own").
- HELP_TEXT line touched accordingly.

## Deliverables

- autodrive.py: `search_around`, detour logic + constants,
  `approach_object(search=)`.
- agent_chat.py: go_to wiring + description + HELP_TEXT.
- docs/reference/controller-commands.md: the go-to row mention.
- Tests (below).

## Testing (fakes only — no hardware, no motion; test_goto.py patterns)

- **search_around**: target visible only "behind" (sighted after ≥3
  rotations) → found, every rotation nudge clearance-paired (ordering
  proof), rotation count ≤ max_rotations; dirty turn view → None, ZERO
  nudges after the dirty verdict; never sighted → None once the rotation
  count or phase cap is exhausted (no full-circle claim asserted).
- **approach with search=True**: behind-target end-to-end — search
  rotations, then body-align, then forwards, arrival; the
  clear-before-every-nudge invariant holds across ALL phases.
- **Detours**: blocked ahead + right probe clear → gated right turn + one
  forward + re-sight + eventual arrival (scripted); both probes dirty →
  "boxed in", zero nudges after the block; DETOUR_MAX exhausted → honest
  stop; probe results advisory only (a probe-clear side whose GATE then
  fails → no nudge — pin that the gate stays authoritative).
- **Search phase cap bites**: a never-sighting looker with a stepping
  clock → search returns its honest why at the cap, rotations stop, and
  the remaining budget is still available to the caller (assert driver
  elapsed < max_seconds).
- **Post-detour reacquire**: target off the pan-0 view after the detour
  but visible opposite the turn → reacquired (no lost-stop) AND the lost
  counter is reset; never visible in the mini-sweep → lost path, honest
  stop (the disclosed v2 behavior).
- **Fresh survey after a blocked forward** (pins Opus B1): ordering proof
  that the detour's turn is preceded by a full fresh 3-view survey — a
  reused pre-block survey must fail the test.
- **Probe tie-break**: both probes clear + bbox cx<0.5 → LEFT turn chosen
  (and the mirror case).
- **Coverage honesty**: no test may assert "guaranteed full circle" —
  rotation-count and cap bounds only (wording pin in the tool
  description: "looks around, approximately a full circle, as time
  allows").
- **Regression**: search=False behavior identical (existing ApproachTest
  untouched and green); find_object untouched (existing suites green).
- Tool: description pins ("looks all around", "steer around obstacles",
  honesty wording), budget values pinned (480/240 split).

## Risks

- Same risk class as plan 036, same envelope, same flag. Detours add
  MOTION VARIETY but no new ungated paths — the invariant "no wheel nudge
  without a fresh clearance verdict" must survive review verbatim.
- A detour can wander if re-sighting keeps failing → bounded by
  DETOUR_MAX, LOST_MAX, step/time caps, watchdog.
- Search rotations near walls: each rotation is gated by the 3-view
  survey + per-pulse check; the rear blind zone note from plan 036
  applies unchanged and is already documented.

## Stages

1. `search_around` + tests.
2. Detour logic + tests.
3. Tool wiring + docs.
4. CI, adversarial review gate, PR.

## Reviews

### Plan review (adversarial)

- **codex** — round 1: BLOCKED ×3: (1) search budget arithmetic didn't
  close (6 fine sweeps × 10 LLM looks ≫ any wall budget) — resolved with
  the coarse 3-look search grid + `SEARCH_PHASE_S=240` inside a 480 s
  budget; (2) post-detour pan-0-only re-sight can strand the rover
  farther than v1 — resolved with the reacquire mini-sweep + explicit
  disclosure (+ home unchanged, come_back still works); (3) "full
  circle/whole area" overclaims unverified rotations — wording downgraded
  everywhere, pose-verified rotation noted as follow-up. Confirmed sound:
  probe-vs-gate chain, detour orientation chain, TTL-forced re-surveys.
- **Opus** — round 1: BLOCKED ×3: (B1, NEW) a blocked `forward()` doesn't
  void `_survey_at`, so the detour's turn could reuse a ≤10 s survey the
  failed floor check just contradicted — resolved: forward voids the
  survey on a False clearance verdict, detour turns always fresh-survey,
  ordering test pinned; (B2) same reacquire overclaim as codex — resolved
  as above, with the sweep biased opposite the turn and the lost counter
  reset on reacquisition; (B3) same budget arithmetic — resolved as
  above. Non-blocking adopted: per-rotation re-survey stated as intended
  with the ±40°/60° arc gap named; open-loop detour angle disclosed;
  probe tie-break + detour-lost tests added.
- **Re-verification** — Opus round 2: PASS (all three resolved; one test
  nuance adopted — the fresh-survey ordering test pins the exact 3-view
  pattern). codex rounds 2–4: caught two leftovers (a stale full-circle
  phrase in the goal + test text; detours-always-on contradicting the
  search=False regression claim) → fixed (honest wording everywhere;
  `detours=0` default keeps plan-036 byte-stable, go_to opts in) → PASS.

### Code review (adversarial)

- **codex** — round 1 BLOCKED ×2: the phase cap wasn't enforced inside a
  sweep (post-cap sightings accepted) and abort wording lied about
  timeouts. **Fixed**: `_sweep_for` gained a strict driver-clock deadline
  checked before AND after every look (post-cap sightings discarded;
  find_object's wall-clock semantics untouched) + distinct
  `abort_time/abort_capture/abort_vision` states with honest mapping.
  Round 2 caught my staging lag (the fixes weren't in the index) —
  content confirmed resolved, index re-synced and verified.
- **glm-5.1** — round 1 BLOCKED ×1, the catch of the session: for a
  STATIC dead-ahead obstacle the detour's fresh survey re-checks the same
  physical view that just blocked → the detour can never fire in the
  common case, and the success tests "proved" it only via a physically
  impossible verdict flip in the aim-based fake. **Fixed honestly**:
  wording downgraded everywhere ("try to go around … helps when something
  transient moves away — a fixed obstacle dead ahead stops it, and it
  says so", with a test pinning the absence of the old overclaim); a
  static-obstacle honest-stop test added; the success test reframed as
  the transient-obstacle case; the unreachable "lost after detour"
  terminal (dead code — `lost` is provably 0 there) removed. Non-blocking
  notes: default-driver sizing documented in the docstring; the
  one-directional search noted. Re-verification: PASS point-by-point.
- **Opus** — PASS on the final tree: B1-void unspoofable (only
  turn_survey writes `_survey_at`), phase cap authoritative both sides of
  every look, detour cycle hand-traced fully gated, AimClearance's
  conflation produces no false passes (the safety property is pinned by
  the exact-counting test), regression byte-stable, budgets wired and
  pinned; 467/467 green. Also flagged the staged-vs-tree divergence
  (administrative; fixed before commit).

## Post-execution report

Implemented as revised through four plan-gate rounds and three code-gate
rounds. `search_around` (coarse 3-look grid, gated rotations only, strict
240 s phase cap inside the 480 s go-to budget, honest why strings),
bounded detours (side probes advisory, gates authoritative, blocked
forward voids the turn survey so detour turns always re-survey, biased
reacquire mini-sweep, lost-counter reset), `search=False/detours=0`
defaults keeping plan 036 byte-stable. The honest capability statement
stands in the tool description and docs: detours help TRANSIENT
obstacles; a fixed obstacle dead ahead stops the rover with an honest
message (the per-pulse floor gate — the actual anti-bump mechanism — is
unchanged and now also covers every search rotation).

Outcomes: plan gate codex BLOCKED→…→PASS (4 rounds) + Opus BLOCKED→PASS;
code gate codex BLOCKED→PASS + glm BLOCKED→PASS + Opus PASS. CI green
(467 tests). Not live-fired (ROVER_GO_ENABLE + explicit user ask
required, as with plan 036).
