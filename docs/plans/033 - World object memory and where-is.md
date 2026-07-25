# Plan 033 — World-coordinate object memory + "where is X"

## Goal

Plan 032 gave every scan a pose. Plan 029/031 gave scans identified objects
with pano angles. This plan joins them: a controller-wide **object memory**
aggregated from all pose-stamped scan sidecars, each object resolved to a
**world bearing** from the scan position; a **Map-tab overlay** showing them;
and a **chatbot tool** so "where is the suitcase?" gets a real answer:
which scan saw it, from where, and how far to turn from the rover's *current*
heading to face it.

**Distance is out of scope.** There is no reliable range source (no depth, no
calibrated camera height), so an object is a *ray* (scan position + world
bearing), never an (x, y) point. The Map overlay draws direction markers, not
fake positions; the chatbot phrases direction, not distance. `world_x/world_y`
from the original sketch are dropped.

## Angle conventions (verified in code)

- Gimbal pan **positive = camera right** (`/camera_left` nudges pan −1,
  rovercontrold.py:2304). `bbox_to_angles` maps a frame-center pixel at pan p
  to lon = p (Ry(pan) in the y-up frame), so **lon positive = right of the
  rover's forward** at scan time.
- `Pose.heading` is **CCW-positive** (left turn increases it; dth ∝ dr − dl).
- Therefore `world_bearing = norm(heading_at_scan − lon)` where
  `norm` wraps to [−180, 180). Bearing shares the heading frame: 0 = the +X
  axis of the pose frame, CCW positive.
- Relative turn for the chatbot: `delta = norm(bearing − current_heading)`;
  `delta > 0` → "turn ~|delta|° left", `< 0` → right, |delta| ≤ 10 → "roughly
  ahead".

## Design

### Controller: `App.list_objects()` + `GET /objects`

- Read-only aggregation, **no lock taken**: iterate `list_scans()` newest
  first; for each scan read its sidecar (`json.load`); skip files that are
  missing, unreadable, corrupt, or have no `pose` dict (per-file try/except).
  Concurrent-publish tolerance: sidecar-only updates are atomic
  `os.replace` (never torn), but the newest-scan publish path briefly
  `os.remove`s the sidecar before re-linking it — a read landing in that
  window sees a missing file and skips that scan for one call, which the
  per-file try/except already handles (self-heals next tick).
- For each object entry with numeric `lon`/`lat`: emit
  `{"id": "<scan>#<index>", "name", "color", "scan": <name>,
    "made": <sidecar made>, "lon", "lat", "pose": {x, y, heading},
    "bearing": <world bearing>}` (color optional, passed through when
  present; `id` is stable per sidecar content — scan name + object index).
- **No global dedup** (plan-review catch: the identification pipeline
  *deliberately* preserves distinct same-name objects — e.g. the user's two
  printers — and a name-keyed dedup would silently hide physical objects).
  ALL sightings are returned, newest scan first, original per-scan order
  within a scan. Consumers choose: the chat tool answers with the newest
  sighting and reports the other candidates; the map draws them all.
- `GET /objects` → `{"objects": [...]}` (200 always; empty list when no
  pose-stamped scans exist yet).
- Bearing helper is a small module-level function
  `world_bearing(heading, lon)` in rovercontrold.py so tests pin it directly.

### Map-tab overlay

- New small toggle button in the map panel: `👁 objects` (`id="mapobjbtn"`,
  default ON) flipping `mapShowObj`.
- `mapTick()` additionally fetches `/objects` (only while the tab is visible
  and the toggle is on — one request per 2 s tick, cheap file reads on the
  Pi).
- `drawMap()` draws each object as a small cyan dot on a fixed 0.55 m circle
  around its scan's pin, in the bearing direction, with a tiny name label
  (canvas `fillText`, 9 px). A fixed radius honestly communicates "direction
  seen from this pin", not a position claim. The dot position is computed in
  WORLD coordinates (`pin + 0.55·(cos b, sin b)`) and passed through the same
  `X()/Y()` transform as pins/trail — never hand-rolled in canvas space, so
  the y-flip can't mirror bearings (Opus note).

### Backend surface (mirrored + pinned)

- `rovercontrol_client.get_objects()` → GET `/objects` parsed, and
  `rovercontrol_client.get_pose()` → GET `/pose` parsed (plan-review catch:
  the chat tool needs the CURRENT heading and `RoverCtl` has no pose
  passthrough today — without it `relative_turn` is uncomputable through the
  backend abstraction).
- `RoverCtl.get_objects()` / `RoverCtl.get_pose()` — rovercontrol backend
  only, raising `RuntimeError` otherwise (matching `list_scans`/`scan_meta`/
  `identify_scan`, NOT `OSError` as originally sketched);
  `RealBackendSurfaceTest` pins all of them on both surfaces.

### Chatbot: `rover_where_is` tool

- Tool schema: `{name: string}` — "find where a previously seen object is".
- Match strategy over the full all-sightings `/objects` list, lowercased:
  1. exact name equality;
  2. substring either direction (`"suitcase"` ↔ `"black suitcase"`), only
     for queries ≥ 4 chars (short queries like "car" would ghost-match
     "cardboard" — Opus catch);
  3. word overlap (any shared word ≥ 4 chars) as last resort.
  First strategy with hits wins; multiple hits (same-name sightings across
  scans AND distinct same-name objects within one scan — both preserved by
  the no-dedup aggregation) → answer the newest sighting and summarize the
  rest in one trailing sentence ("also: 1 more in the same view, 2 in older
  views").
- Answer assembly (in `run_tool`): object's scan + `made` stamp + scan pose +
  bearing + current `/pose` heading → e.g. *"the suitcase was seen in the 3rd
  newest 3D view (scan_20260715_004255, 2026-07-15T00:42), looking 40° right
  of where the rover stood at (1.2, 0.5); from the current heading turn ~25°
  left to face it."* Degrees rounded to integers; "roughly ahead" within 10°.
- No match → say so and suggest `rover_identify_scan` / a new scan.
- No motion is ever commanded by this tool.

## Deliverables

- rovercontrold.py: `world_bearing()`, `App.list_objects()`, `GET /objects`.
- rovercontrold_page.py: objects overlay + toggle in the Map tab.
- rovercontrol_client.py / rover_backend.py: `get_objects` passthroughs.
- agent_chat.py: `rover_where_is` tool (schema, dispatch, phrasing helper
  `relative_turn(bearing, heading)`), HELP_TEXT line.
- docs/reference/controller-commands.md: `/objects` row + tool mention.
- Tests (below).

## Testing (fakes only; no hardware, no motion)

- `test_pose.py` or `test_controller_scan.py`: `world_bearing` pins —
  lon 0 → bearing == heading; heading 0, lon +30 → −30 (object right);
  heading 90, lon −40 → 130; wrap cases at ±180.
- `test_controller_scan.py`: `list_objects` aggregation — synthetic sidecars
  (two scans with overlapping names incl. two same-name objects in ONE scan,
  one poseless, one corrupt) → ALL sightings kept with stable ids, newest
  scan first, poseless/corrupt skipped, an object entry with non-numeric
  lon/lat skipped, shape correct.
- `test_controller_http.py`: `/objects` endpoint (200 + shape + empty case);
  page markers (`mapobjbtn`, `mapShowObj`, `/objects` fetch, label draw).
- `test_autoflash.py` (`RealBackendSurfaceTest`): `get_objects` pinned on
  `RoverCtl` and `rovercontrol_client`.
- `test_chat_session.py` or `test_autoflash.py`: `rover_where_is` with a fake
  rover — exact/substring/word match tiers (incl. a word-overlap false-
  positive guard and a color-word query), multiple candidates sentence (two
  printers in one scan + an older sighting), no-match path, and **wraparound
  relative-turn pins**: heading 170 → bearing −170 = "~20° left"; heading
  −170 → bearing 170 = "~20° right"; heading 0 → bearing −30 = right;
  |delta| ≤ 10 → "roughly ahead".

## Risks

- **Sign errors** are the real risk; the conventions section above is
  verified against code and the tests pin concrete numbers both for
  `world_bearing` and `relative_turn`.
- Pose odometry drift/calibration (user hasn't done the sign drive yet) makes
  bearings only as good as the pose — accepted, same caveat as the Map tab.
- Read-only aggregation: no sidecar writes, no locks, no drive-path contact.

## Stages

1. `world_bearing` + `list_objects` + `/objects` + tests.
2. Map overlay + markers.
3. Client/backend passthroughs + surface pins.
4. Chatbot tool + phrasing tests.
5. Docs, CI, review gate, PR.

## Reviews

### Plan review

- **codex** — round 1: BLOCKED. (B1) no pose passthrough on the backend
  surface — the tool's `relative_turn` was uncomputable; (B2) global
  name-dedup collapses distinct physical objects (the two printers).
  Non-blocking: `RuntimeError` not `OSError` for the sentinel, the
  remove→link publish window wording, fuzzy false-positive + wraparound
  phrasing test asks. It independently verified all four angle-convention
  claims correct. **Resolution**: `get_pose()` added to both surfaces
  (+ pins); dedup dropped entirely — all sightings with stable ids, chat
  summarizes candidates; all wording/test asks adopted.
- **Opus** — round 1: BLOCKED on the same missing current-heading source;
  independently re-derived pan/lon/heading/bearing/relative-turn math and
  confirmed every sign. Non-blocking: sentinel alignment (adopted),
  within-scan same-name collision under dedup (moot — dedup removed),
  tier-2 substring min-length guard (adopted, ≥4 chars), world-frame map
  overlay sentence (adopted), non-numeric lon/lat test (adopted). Also
  validated the lock-free read tolerance claims against every sidecar
  writer.
- **Re-verification** — codex: PASS (both blockers + all four asks confirmed;
  one stale "deduped" word, fixed). Opus: PASS (each resolution line-checked
  against code, agrees dedup removal moots the collision concern; same
  wording nit, fixed).

### Code review

- **glm-5.1** — PASS. Numerically verified every bearing/turn pin, all
  aggregation skip paths, the tier guards, the world-frame overlay, and the
  passthrough conventions. Non-blocking nits: weak ±180 boundary assertion
  (fixed — both ±180 inputs now pinned to −180), `bool` passing the numeric
  lon check (accepted, unreal in practice), match tiers only exercised
  behaviorally (accepted), hardcoded bearings in the tool test (accepted —
  the aggregation path pins the same math).
- **codex** — PASS. Independently re-derived the sign chain (third
  independent derivation this plan); confirmed every gate demand present.
  Could not run tests in its read-only sandbox (run green locally).
- **Opus** — PASS (see re-run note in post-exec report): first review run
  was stopped mid-flight by the user; re-launched and passed.

## Post-execution report
