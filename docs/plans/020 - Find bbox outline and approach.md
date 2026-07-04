# 020 - $find: bounding-box outline, closer approach, hard stop on found

## Goal (user feedback from a real `$pen` run)

1. **Stop the instant it finds the target** (explicit halt, not just loop exit).
2. **Show an outline of the found object** on the photo in the website, **toggleable
   on/off**.
3. **Get closer before declaring found** (better view → better identification).
4. Fix misidentification (user placed a **green** pen; the model reported a
   **black** pen): hold the model to the description and surface the object's
   color so mismatches are visible.

## Design

### Vision: bounding box (the keystone)
`FIND_PROMPT` v2 asks for a **bbox** and a color, and requires a description match:
```
{"seen": bool, "bbox": [x1,y1,x2,y2]|null, "bearing": ..., "close": bool,
 "color": str, "confidence": 0..1, "reason": str}
```
bbox = fractions of image width/height (0..1). "Only set seen=true if the object
clearly matches the description '{target}' (shape AND color if the description
names one). Report the object's actual color."
Qwen-VL-class models ground boxes well; `look_for()` sanitizes: bbox must be a
4-list of finite 0..1 numbers with x1<x2, y1<y2, else treated as absent
(fall back to the model's bearing/close — full backward compatibility).

### Loop: bbox-driven approach ("get closer")
When a valid bbox exists it **overrides** the model's coarse flags:
- `bearing` from bbox center-x: <0.35 left, >0.65 right, else center.
- `close` = bbox height ≥ `CLOSE_BBOX_H` (0.25 of frame) — a measurable "big
  enough for a good look" criterion, so the rover keeps creeping (floor-gated,
  unchanged) until the target fills a quarter of the frame instead of trusting
  a vague `close` bool.
- Found condition unchanged otherwise (seen + centered + close + conf floor).

### Found: hard stop + the photo IS the analyzed frame
- On found: `driver.halt()` explicitly (belt over the loop-exit stop), **no
  camera recenter** — the found photo is the very observation frame the model
  just analyzed (`capture()` already saves every frame as a snapshot), so the
  bbox matches the photo exactly AND the old recenter-then-shoot bug (camera
  tilts up, pen half-leaves the frame) disappears.
- `find_object` gains `on_found(name, obs)` (optional callback; return value
  stays the photo name — existing tests unaffected).

### Website: toggleable outline (Go)
- **New endpoints** (photoDir sidecars, name validated by `safePhotoName`):
  - `POST /photo_meta/{name}` — body `{target,bbox,color,confidence}` → writes
    `{name}.meta.json` (size-capped body, bbox re-validated server-side).
  - `GET /photo_meta/{name}` — serves it; 404 if absent.
- **Gallery**: each card gets a small `◻` toggle. On first click it fetches
  `/photo_meta/{name}`; if a bbox exists it overlays an absolutely-positioned
  lime-border div (bbox fractions → CSS %) over the thumbnail; clicking again
  hides it. No meta → button reports "no outline". No polling cost (lazy fetch,
  cached). Overlay is pure CSS on the existing `<img>` — the JPEG is untouched,
  so the toggle is genuinely on/off (user requirement).
- `$find` prints the photo URL + a hint that the outline toggle is on the card.

### Chatbot wiring
- `rovercontrol_client.set_photo_meta(name, meta)` (JSON-body POST helper).
- `autonomous_find` passes `on_found` that writes the meta (best-effort) and
  includes the model-reported **color** in the success message, e.g.
  `found a green pen (color: green) → rover_x.jpg — outline toggle in gallery`,
  so a green/black mismatch is immediately visible.
- MisID guidance: `$find a green pen` works today; `$pen` stays "a pen". Help
  text notes: name the color for stricter matching.

## Testing (fakes; no motion)
- Python: bbox sanitizer (garbage/out-of-range/reversed → ignored); bbox-driven
  bearing/close (small centered bbox → approaches via forward; tall bbox →
  found); found → `halt()` called, no recenter, `on_found` gets (name, obs);
  prompt mentions bbox+color; client `set_photo_meta` posts JSON to the right
  path. Existing FakeVision dicts (no bbox) keep passing (fallback path).
- Go: `photo_meta` POST→GET roundtrip; bad names 400; oversized/invalid bbox
  rejected; GET absent → 404; page contains the outline toggle JS.
- ci-local green.

## Risks
- Bbox quality varies by model — sanitize hard, fall back to flags; the outline
  is cosmetic (safety never consumes bbox — the floor gate is unchanged).
- Meta writes race a concurrent delete — harmless (404 on fetch).
- Slightly longer runs (approach until bbox big) — same step/time budgets bound it.

## Stages
1. Plan + 2-way review. 2. vision/loop changes + Python tests. 3. Go endpoints +
gallery toggle + tests. 4. ci-local, 3-way review, PR, merge, deploy (pull +
rebuild/restart). No motion during dev/test.

## Reviews
### Plan review (Opus + codex) — both REQUEST-CHANGES → resolved

- **Opus B1 (blocking):** bbox *height* is the wrong "close" proxy for an
  elongated floor object — a pen lying sideways is wide but tiny in height, so
  "close" might never trigger and the budget burns. → Resolved: "close" uses the
  **larger bbox dimension** (`CLOSE_BBOX_DIM`), plus a **max-approach fallback**
  (`MAX_APPROACHES=6`): after 6 centered, floor-gated approaches the rover shoots
  anyway (counter resets if sight is lost).
- **codex B1 (blocking):** naive % overlay misaligns under `object-fit:cover`
  when the capture isn't 4:3. → Resolved: `coverPct()` maps image fractions →
  container fractions through the actual cover crop (`naturalWidth/Height` vs
  container), so the outline aligns for any aspect; resize-safe since the
  container keeps its ratio; falls back to naive % if the image isn't loaded.
- Opus verified: no-recenter is strictly better (the old center-then-shoot tilts
  the target toward the frame edge); sidecar naming can't traverse
  (safePhotoName); bbox never feeds the floor-safety gate; halt-on-found is
  additive. Required hardening applied: `http.MaxBytesReader` on the meta POST,
  server-side bbox re-validation, JSON-only consumption with `title`-property
  (no innerHTML) rendering. Color enforcement stays prompt-level (hard-matching
  color words is brittle — Opus N4); the closer look is the durable misID fix.
- codex nits folded: sanitizer rejects (not normalizes) bad bboxes; meta body
  size/validation explicit; gallery renders numbers + text properties only.
### Code review (Opus + codex + glm) — all APPROVE, no blockers

- **codex**: coverPct verified correct for object-fit:cover (its plan blocker);
  fallback acceptable. Nit: meta can be written for a nonexistent photo (harmless).
- **glm**: sanitization, bounded approach loop, cover math, title-only rendering,
  Go validation all sound. Nit folded in: `str(None)` → "" for the stored color.
- **Opus**: traced all plan-review resolutions (larger-dimension close; exactly 6
  approach-fallback forwards; counter reset; MaxBytesReader + re-validation +
  sanitized re-marshal; positioned overlay parent; halt + no-recenter + analyzed
  frame; meta written only when a bbox exists). 16:9-in-4:3 and identity cover
  cases check out. Nits folded in: stale "height" comment fixed; outline button
  glyph now tracks hide/show. Deferred (harmless): rune-safe trunc, orphan meta
  for nonexistent names, ∅ re-fetch.

## Post-execution report

**Implemented** all four pieces of user feedback: explicit halt the instant the
target is found; a toggleable ◻ outline on the found photo in the gallery
(bbox sidecars + cover-aware CSS overlay — the JPEG is untouched); a real
approach behavior (creep, floor-gated, until the target's larger bbox dimension
fills ≥25% of frame, with a 6-approach fallback); and misidentification
mitigation (prompt requires a description/color match, reports the seen color,
and the found photo is the exact analyzed frame — plus the closer look itself).
**Deviations:** "close" metric changed from height to larger-dimension and the
approach fallback added (both from plan review); overlay switched from naive %
to cover-aware mapping (codex blocker).
**Tests/CI:** 124 Python + Go tests, ci-local PASS. Live vision behavior to be
validated by the user's next real `$pen`/`$find a green pen` run.
**No motion was commanded during dev/test.**
