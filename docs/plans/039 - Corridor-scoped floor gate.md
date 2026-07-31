# Plan 039 — Corridor-scoped floor gate (+ verdict logging)

## Goal

Live testing (maiden go-to runs, 2026-07-31) proved the floor gate is too
blunt for a lived-in room: today's `FLOOR_PROMPT` nominally scopes to
"the near floor straight ahead … within ~30 cm", but in practice the
model vetoes on salient objects well outside that zone (raw verdicts
captured: hazard "pen on floor" ahead/left, "tripod" right — the pen was
a body-width off the drive line and ~50 cm out) — "boxed in" by a pen.
(Opus wording correction: the old prompt is 30 cm-scoped ON PAPER; the
observed behavior is whole-frame-ish. The new RULE 1 makes animals
stricter than even the paper scope: ANYWHERE in frame, any distance.) The user has directed: make the rover complete "go to the post-it
and photograph it" in this room.

Fix, keeping the fail-closed philosophy INSIDE the path: judge only the
corridor the rover will actually traverse.

## Design (autodrive.py) — REVISED after codex round 1

The original crop idea is DROPPED (codex: unprovable swept-path coverage
without lens/mount calibration; narrowed turn surveys would be WORSE than
today; a cat at the crop edge becomes invisible; the failure-fallback
prompt would lie about the image). Replacement: the model keeps the FULL
frame — full lateral context, zero geometry assumptions — and the prompt
is restructured into a two-rule verdict that is strictly ≥ today's
safety:

0. **Prompts are MOTION-TYPED** (codex round-2 catch — strip semantics
   are right for forward travel and WRONG for in-place turns, which
   sweep the body circle):
   - `FLOOR_PROMPT` (forward pulses only): the two-rule center-strip
     version below.
   - `TURN_PROMPT` (turn surveys, per-pulse turn-direction checks,
     detour side probes): full-frame near-zone strictness — today's
     baseline wording ("any object/edge/step/drop-off within ~30 cm of
     the rover in this view → false") PLUS the explicit animal/person-
     anywhere veto. Turn gating is therefore ≥ today, exactly.
   - Plumbing without breaking the single-callable API or any test
     fake: `SafeDriver` sets `self.motion_context = "forward" | "turn"`
     immediately before each clearance invocation (2 lines in
     `forward()`, turn paths); `make_llm_clearance(vision, capture,
     log=None, driver=None)`'s closure picks `TURN_PROMPT` when
     `driver.motion_context == "turn"`, else `FLOOR_PROMPT`. Fakes that
     ignore the attribute keep working; a test pins the context
     transitions and the prompt selection.

1. **`FLOOR_PROMPT` rewrite (no crop, two rules)**:
   - RULE 1 — dynamic hazards: `clear=false` if a person or animal is
     visible ANYWHERE in the frame, any distance ("a cat may approach
     from the side — treat any animal in view as blocking"). This is a
     strengthening: today an off-side cat merely *usually* fails the
     whole-frame clutter rule; now it always does, explicitly.
   - RULE 2 — static obstacles: `clear=false` for an object, edge, step,
     or drop-off in or overhanging the CENTER DRIVE STRIP — described
     verbally: "the middle portion of the image, roughly one-third of
     the frame wide, from the bottom edge to about 50 cm ahead — the
     strip the rover will drive through." Inanimate objects clearly
     OUTSIDE that strip (left/right edges) must be IGNORED — report them
     in "hazard" text but do not veto.
   - RULE 2 keeps the full enumerated hazard list, explicitly including
     table/desk/chair EDGES and overhangs at rover height in or over the
     strip (Opus round-1: the crop would have blinded exactly this class;
     full-frame keeps it visible and vetoing).
   - Keep: "If unsure whether something touches the strip → false";
     JSON shape `{clear, confidence, hazard}` unchanged; `min_conf`
     unchanged; ANY vision error → False (unchanged).
   The live false-veto case becomes correct under RULE 2 (the pen sits
   left of the strip); the tripod-right probe stays correctly blocked
   when its legs cross the strip of a would-be right turn.
2. **Verdict logging**: `floor_is_clear(vision, img, *, min_conf=0.6,
   log=None, prompt=None)` — every verdict logged (`floor: clear=<bool>
   conf=<f> hazard=<str>`; errors as `floor: vision error — fail
   closed`); `make_llm_clearance(vision, capture, log=None, driver=None)`
   threads both; go_to / come_back pass their run log — runs become
   self-diagnosing.
3. **Unchanged**: no crop code at all; turn survey geometry and views;
   per-pulse cadence; caps/budgets; close criteria.
4. **Evidence step (pre-merge, recorded in the post-exec report)**: run
   the NEW prompts against the live frames captured during the 07-31
   diagnosis (saved on the Mac in the session scratchpad: go2/go3
   forward views, ±40° views) — the pen-ahead frame must flip to clear
   under FLOOR_PROMPT (pen named in hazard text), the tripod-right frame
   must stay blocked under TURN_PROMPT, and an animal-visible probe must
   veto under both — real-image validation, not just prompt pins.

## Deliverables

- autodrive.py: `FLOOR_PROMPT` two-rule rewrite, `floor_is_clear(log=)`,
  `make_llm_clearance(log=)` threading (go_to/come_back pass logs).
- Tests: prompt pins for BOTH prompts (FLOOR: animal-anywhere +
  center-strip + ignore-outside-strip + unsure→false + the enumerated
  EDGE/OVERHANG class — the regression guard for Opus's original
  blocker; TURN: near-zone anywhere strictness + animal-anywhere; no
  stale wording);
  motion-context transitions (forward sets "forward" before its
  clearance call, turn paths set "turn" — ordering asserted on a
  recording fake); prompt selection (a recording fake vision receives
  TURN_PROMPT for survey/side/probe calls, FLOOR_PROMPT for forward);
  floor_is_clear behavioral tests (clear, strip veto, animal veto,
  low-confidence veto, error → fail-closed + logged); logging pins;
  make_llm_clearance log+driver threading through approach/backtrack.
- docs/reference/controller-commands.md: go-to row note updated.

## Risks

- **The model must follow spatial instructions** ("middle third")
  reliably. Mitigations: full context retained (no blind zones — codex's
  crop objections don't apply), unsure→false stays, min_conf stays, and
  the pre-merge evidence step validates against the exact live frames
  that produced the false veto plus a blocked case. Residual risk is a
  RETURN toward over-caution (a false veto) in ambiguous cases; the
  honest under-caution residual (a confidently-misjudged in-strip
  static) is bounded by the strongest mechanism in the system: the
  per-pulse ~10 cm creep with FRESH re-judgment before every pulse — a
  single false-clear costs one tiny pulse, then re-evaluates (Opus:
  named here explicitly as the bound). Animals are stricter than today
  by rule; turn checks keep baseline strictness via TURN_PROMPT.
- Live iteration follows the merge: relaunch the post-it run, feed
  outcomes back (the user's explicit directive).

### Re-verification record

- codex round 2: 4/5 crop blockers structurally resolved by the no-crop
  redesign; partial on turn semantics → the motion-typed prompt split.
  Round 3: PASS (one signature wording nit, fixed).
- Opus round 2: PASS — its blocker resolved by construction; named the
  true residual (confidently-misjudged in-strip static) and its bound
  (per-pulse re-judgment), both now stated in Risks; four cleanups
  folded (goal wording, stage wording, edge/overhang pin, evidence
  frames confirmed existing on the Mac).

## Addendum (post-merge live finding, same plan scope)

First relaunch: target sighted (conf 0.90, pan 0), then the forward gate
FAILED CLOSED on `vision error (Request timed out.)` — a transient
gateway timeout, not a hazard — which consumed the block path, triggered
the probes (correctly blocked: chair left, tripod right), and ended
"boxed in" with zero genuine forward verdicts. Fix: **error-only
retries** in `make_llm_clearance` — an ERROR-class failure (exception /
malformed output) refetches a FRESH frame and retries up to
`CLEARANCE_ERROR_RETRIES = 2` times (each logged); a genuine
`clear=false` VERDICT retries ZERO times (instant stop, unchanged);
persistent errors still fail closed. Internal split
`_floor_verdict(...) -> (ok, is_error)` with `floor_is_clear` kept as
the compatible bool wrapper. Tests: error→clear succeeds with two calls;
persistent error → False after N+1 attempts, logged; real False → one
call, no retry.

## Addendum 2 (live finding, attempt 4)

The sweep's early-accept (conf ≥ 0.85 at ANY pan) grabbed a peripheral
sighting at pan −50 on its FIRST look and committed to a body turn toward
real furniture (turn gate correctly refused: "furniture leg and cables
very close") — when one more look would have found the target dead ahead,
requiring no turn at all (attempt 2: pan 0, conf 0.90). Fix:
`_sweep_for(prefer_center=False)` — when True (search_around/approach
path only), early-accept fires ONLY for sightings within
`BODY_ALIGN_TOL` of center, and final selection prefers the smallest
|pan| (tiebreak: confidence) instead of raw confidence. `find_object`
keeps `prefer_center=False` → byte-identical behavior. Tests: centered
low-conf beats peripheral high-conf; no early-accept at −50; early-accept
at 0; find_object suite untouched.

## Stages

1. Motion-typed prompts + logging + tests (no crop code exists).
2. Gate (fast), CI, PR, merge, deploy.
3. Relaunch the live run; iterate until the photo exists.

## Reviews

### Plan review (adversarial, fast)

- **codex** — round 1 BLOCKED ×5 against the crop design: dishonest
  fallback prompt; unproven swept-path coverage (projection math +
  near-field width); new cat lateral-entry blindness; narrowed turn
  surveys worse than baseline; tests couldn't prove coverage.
- **Opus** — round 1 BLOCKED against the crop design independently: the
  vertical top-crop erased at-camera-height overhangs (table/chair
  edges) — an enumerated hazard class — plus two inaccurate risk claims
  and a missing crop-seam integration test; its cat analysis found the
  horizontal exposure bounded; it suggested prompt wording ("judge the
  CENTER lane… ignore far sides") as the honest alternative.
- **Resolution — design replaced**: the crop is GONE. Full-frame image,
  two-rule prompt: animals/people ANYWHERE veto (stricter than today);
  static obstacles veto only in/overhanging the verbally-described
  center drive strip (with edges/overhangs explicitly enumerated);
  unsure→false and error→false unchanged; verdict logging added;
  pre-merge evidence run against the live false-veto frames. Every
  round-1 blocker is structurally moot (no geometry, no fallback, no
  narrowed views, overhangs visible) and the cat rule is a net
  strengthening.

### Code review

- **codex** — PASS. Verified context ordering at every site, lazy driver
  coupling, the come_back reorder, prompts, fail-closed paths; one test
  nit adopted (exact-list probe-context assertion).
- **glm-5.1** — PASS. Context plumbing exhaustive; noted the 50 cm strip
  depth (intentional, documented), raw turn_left/right not setting
  context (they never call clearance), three low-risk test gaps.
- **Opus** — PASS. Traced all six invocation-site classes, proved no
  stale-context path exists, verified prompts/fail-closed/tests bite;
  NB1 adopted (motion_context reset in __enter__, matching the
  survey-leak pattern).

## Post-execution report

Implemented as gated: motion-typed prompts (FLOOR two-rule drive-strip /
TURN full-frame baseline, animal-anywhere veto in BOTH — stricter than
the old 30 cm scope), motion_context plumbing with no API breaks,
verdict logging threaded through go_to/come_back, __enter__ resets.
Evidence run on the live frames (recorded above expectations): the
current pen-left scene flips to clear=true with the pen named in hazard
text; the older tripod-in-strip frame stays blocked (discriminating);
both turn views stay blocked. Deviations: none. CI green (487).
Live relaunch of the post-it mission follows the deploy (user
directive: iterate until the photo exists).
