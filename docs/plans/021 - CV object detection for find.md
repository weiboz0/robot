# 021 - Local CV object detection for $find (no LLM in the detect path)

## Goal
Replace the LLM as the TARGET DETECTOR in the autonomous find loop with local
OpenCV color-object detection: milliseconds instead of 20-45s/look, no flaky
gateway, deterministic. The LLM stays ONLY for the floor-safety gate (unchanged
- CV cannot judge stairs/people). User test flow (explicitly requested):
camera-only test first (find + photograph + hitbox meta for the green pen,
wheels untouched), then reset the robot so `$pen` works for the user.

## Evidence (live-prototyped before this plan)
Tuned against real rover frames with the actual pen; verified BY EYE (frame +
box inspected). Key findings baked into the design:
- The pen is DARK green (V down to ~20) with a black clip that SPLITS the green
  blob → no morphological open; close(13x13) bridges the clip; min area 0.00015.
- Room distractors: screwdriver's green handle, wall-art greens, dark shadow
  strips, the rover's own hull. Beaten by: floor-region filter (bbox bottom
  >=0.35), foreground/hull filter (top of bbox <=0.78), edge-clip filter,
  elongation scoring, and MEAN-SATURATION scoring (shadows are weakly saturated,
  the pen is strongly saturated: meanS 158 vs 112).
- Final frame: pen wins (score .392 vs .38 next) — bbox visually exact.

## Design
- `detector.py`: `detect_color_object(jpeg_bytes, color) -> {bbox, score,
  elong, mean_sat} | None`. HSV ranges for green/blue/red/yellow. cv2/numpy
  imported lazily; `available()` reports. Pure function of bytes → dict.
- `autodrive.find_object(..., look=None)`: `look(name, img) -> obs` injectable;
  default stays LLM `look_for`. New `obs_from_detection(det, color)` builds the
  same obs shape ({seen,bbox,bearing,close,color,confidence}) reusing the
  existing bbox→bearing/close override (CLOSE_BBOX_DIM / CLOSE_BBOX_BOTTOM).
  Confidence = detection score mapped into 0..1 (found gate unchanged).
- `agent_chat.autonomous_find`: if the target names a known color AND the
  detector is available → CV look; else LLM look (fallback preserved). Message
  says which detector ran. `$pen` shortcut becomes "a green pen" (the user's pen;
  color is what CV keys on).
- Floor gate: UNCHANGED (LLM `floor_is_clear`, fail-closed). All SafeDriver
  safety unchanged.
- requirements.txt: + opencv-python-headless, numpy (detector deps; graceful
  LLM fallback if missing).

## Testing
- Unit (skipUnless cv2): synthetic frames — draw a green elongated bar on a
  beige background → detected with correct bbox/color; distractor cases (low
  saturation strip loses; wall-region blob filtered; edge-clipped filtered);
  none → None. Loop: injected fake `look` drives find_object (found/approach
  paths); obs_from_detection maps bbox→bearing/close identically to look_for.
- Live camera-only acceptance (user-specified): pan-sweep detect → photograph →
  POST hitbox meta → verify GET; wheels never commanded. Then reset: camera
  centered, /stop (zero motion, clears latch), healthz verified.

## Risks
- Color detection is lighting/scene dependent (tuned for this room; thresholds
  are constants — easy to retune). LLM fallback remains for colorless targets.
- The floor gate still costs one LLM call per forward — acceptable (few calls).

## Stages
1. Plan + 2-way review. 2. detector.py + autodrive look injection + agent_chat
wiring + tests. 3. ci-local + 3-way code review. 4. Merge, deploy (pip install
on Mac; rover falls back to LLM if no cv2). 5. Camera-only live acceptance +
robot reset. 

## Reviews
### Plan review (codex) — REQUEST-CHANGES → resolved
(Opus's plan scrutiny was folded into its combined plan+code review below.)
codex blockers, all resolved in code: (1) CV score→confidence under-specified →
hard MIN_MEAN_SAT=115 (measured shadow 112 vs pen 158) + MIN_SCORE=0.15 filters
INSIDE the detector; any returned detection is trustworthy, conf=0.5+score ≥
FOUND_MIN_CONF by construction, junk → None → "not seen". (2) floor-LLM must
gate before rover contact even in CV mode → holds (VisionModel constructed
before SafeDriver; existing no-contact test). (3) look error semantics → bad
frame = None = not-seen (transient); detector exception fail-closes the run
with wheels stopped. + shortcut test/help updated.

### Code review (Opus + codex + glm) — all APPROVE
codex: RESOLVED + APPROVE (all 3 blockers verified fixed). glm: APPROVE —
filters compose correctly, CV path funnels through the same _apply_bbox, CV
exceptions fail-close to a stop, no safety regression. Opus: APPROVE — traced
that the detect path never calls the LLM, motion gating unweakened, detector
math guarded; nits taken: red-wrap test + a CV-mode floor-gate test (the one
untested safety seam). Deferred nits: MAX_FRAC comment for compact targets,
det_kind print vs log.

## Post-execution report

**Built**: detector.py (local OpenCV color-object detection, live-tuned +
eye-verified), look-injection in find_object with shared _apply_bbox,
agent_chat CV-first wiring with LLM fallback, $pen → "a green pen". 141 tests.

**Live camera-only acceptance PASSED** (user-specified): pan sweep → the green
pen detected (score .48, elong 5.3), photographed
(rover_20260704_221929_065.jpg), hitbox meta stored + verified, the boxed frame
visually confirmed dead-on the pen. Wheels never commanded; robot reset after
(camera centered, /stop, healthz ok).

**Detect latency**: milliseconds (was 20-45s + gateway flakiness per look). The
LLM remains only in the floor-safety gate (fail-closed, unchanged).
