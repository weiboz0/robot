# Plan 034 — Chatbot "find X": scan-for active search

## Goal

"Find my suitcase" today either recalls memory (`rover_where_is`, plan 033)
or physically drives (`rover_find_object`, env-gated). The middle tool is
missing: **look around from right here**. New chatbot tool `rover_scan_for`:
run a fresh 3D scan (gimbal sweep only — wheels never move), let
identification find the target, and answer with the direction to turn —
falling back to a *focused* re-identify when the general pass misses it.

## Existing machinery this rides on (verified)

- `POST /scan` → 200 or 409 with a reason (busy / wheels moving / e-stopped)
  (rovercontrold.py:2431).
- `GET /pano_status` → `{"state": "scanning|stitching|done|failed|…",
  "age_s"}` (2289); "done" flips at publish (plan 029's early flip), after
  which the archived scan + pose sidecar exist and the background
  auto-identify (no focus) is running.
- `POST /scan_identify/<name>?focus=` → 202, or 409 "an identify is already
  running", or 404 (plan 031); sidecar `made` advances when it lands.
- `/objects` + `_match_objects` + `relative_turn` (plan 033) for the answer.

## Design

### Backend surface (mirrored + pinned, RuntimeError sentinels)

- `rovercontrol_client.start_scan()` → POST `/scan`; on HTTP 409 **parse the
  JSON body's `error` field** and re-raise as `RuntimeError(reason)` —
  `str(HTTPError)` is only "HTTP Error 409: Conflict"; the "wheels are
  moving" reason lives in the body (Opus catch).
- `rovercontrol_client.get_pano_status()` → GET `/pano_status` parsed.
- `RoverCtl.start_scan()` / `RoverCtl.get_pano_status()` — rovercontrol
  backend only, `RuntimeError` otherwise; both pinned in
  `RealBackendSurfaceTest` alongside plan 033's additions.

### Tool: `rover_scan_for` (agent_chat.py)

Schema `{target: string}` — description says explicitly: the CAMERA/GIMBAL
sweeps the room (physical camera motion), the wheels NEVER move, takes
several minutes, and it must be used only when the user asks to look
around/search from here — never auto-invoked for a memory question
(contrast: `rover_where_is` = memory only, no motion at all;
`rover_find_object` = drives). The step-5/6 `get_objects` match runs
regardless of why the poll exited; `scan_meta(new) is None` counts as "not
yet" (the minimal-sidecar write is best-effort).

`run_tool` flow (every wait uses `time.sleep(SCANFOR_POLL_S)`; **one
deadline source**: `deadline = time.monotonic() + SCANFOR_BUDGET_S` (600 s)
checked in every loop — the chat turn holds, same accepted tradeoff as
`rover_identify_scan`'s 360 s. Tests patch BOTH `time.sleep` (no-op) and
`time.monotonic` (stepping fake) so the budget path is really exercised):

1. `before = list_scans()[0] if any` (baseline).
2. `start_scan()`; a `RuntimeError` → return its reason verbatim ("scan
   refused: wheels are moving — stop first").
3. Poll `get_pano_status()` until `state == "done"` (→ continue) or
   `"failed"` (→ "the scan failed or was cancelled — nothing to search").
   Phase cap **320 s** — deliberately above the controller's
   `SCAN_BUILD_TIMEOUT_S = 300` so a slow-but-successful stitch is never
   abandoned by the poll (Opus catch; the 600 s budget still governs).
4. Resolve `new = list_scans()[0]`; if it equals `before` (archive failed) →
   report honestly and stop.
5. **General pass**: capture `baseline = scan_meta(new)` (may legitimately
   be `None` — the minimal-sidecar write is best-effort). Poll
   `scan_meta(new)` until **meta content changes**: the `(made, objects)`
   pair differs from the baseline's (never `made` alone — both stamps are
   second-resolution and a fast identify can land in the same second;
   plan-review catch) — or ~150 s elapse (auto-identify may have been
   skipped as busy). Fuzzy-match `target` against the newest scan's
   sightings in `get_objects()` (reuse `_match_objects`, filtered to
   `scan == new`). Hit → answer (step 7).
6. **Focused pass**: re-capture `baseline = scan_meta(new)`, then
   `identify_scan(new, focus=target)`. **Busy classification** (both
   reviewers' catch — the client raises raw `urllib.error.HTTPError` whose
   `str()` is just "HTTP Error 409: Conflict"; the reason text is ONLY in
   the JSON body): retryable iff `getattr(e, "code", None) == 409` (covers
   the real HTTPError) or the message contains "running"/"busy" (covers
   wrapped forms). Retry every 10 s (≤ 6 tries); other errors → report
   verbatim. Then poll the same **content-change** condition (≤ 240 s
   within budget) and match again.
   Known asymmetry (Opus discovery, disclosed): `scene.py` writes identify
   meta ONLY when objects were found — a finished-but-empty identify leaves
   the sidecar untouched, indistinguishable from "still running", so the
   never-found path legitimately burns its remaining cap before the
   fallback reply. Bounded by the budget; acceptable.
6b. **Newer-scan interference watch** (plan-review catch): a scan started
   AFTER our "done" kills our identify (`start_scan` killpg's any running
   identify) and frees the slot — silent "not found" would be a lie. Every
   poll iteration in steps 5–6 also checks `list_scans()[0] != new`; if a
   newer scan appears, stop and answer "a newer scan interrupted the
   search — ask again when it finishes".
7. Answer: reuse plan 033 phrasing — object name, world bearing, and
   `relative_turn(bearing, current_heading)` from `get_pose()` when fresh
   ("found the suitcase in the new scan — turn ~40° left to face it");
   multiple hits summarized. Not found after both passes → "scanned from
   here and couldn't find X — it may be out of view; try driving elsewhere
   (rover_find_object can search while driving)".

### Non-goals

- No wheel motion, no gimbal commands outside the existing scan pipeline.
- No new controller endpoints — this is purely client-side orchestration of
  plans 025–033 machinery.
- No streaming progress into the chat (chat streaming is plan 038).

## Deliverables

- rovercontrol_client.py: `start_scan`, `get_pano_status`.
- rover_backend.py: `RoverCtl.start_scan`, `RoverCtl.get_pano_status`.
- agent_chat.py: `rover_scan_for` tool (schema + `run_tool` flow), constants
  `SCANFOR_BUDGET_S`/`SCANFOR_POLL_S`, HELP_TEXT line.
- docs/reference/controller-commands.md: tool mention on the `/scan` row.
- Tests (below).

## Testing (fakes only; `time.sleep` patched to no-op; no hardware)

`tests/test_autoflash.py` (or a new `test_scanfor.py`): a scripted fake
rover whose `get_pano_status()` returns a queue of states and whose scans
list/sidecar/objects evolve per step:

- happy path, found by the GENERAL pass: status scanning→stitching→done,
  new scan appears, objects gain the target → answer has bearing + turn
  phrase, `identify_scan` was NEVER called.
- found only by the FOCUSED pass: general objects lack the target;
  `identify_scan(new, focus)` called once; the match lands after a CONTENT
  change with the SAME second-resolution `made` stamp (pins the
  same-second fix) → answer includes it.
- identify-busy then free, BOTH error shapes (pins the 409-classification
  fix): (a) a REAL `urllib.error.HTTPError` with code 409 (str() carries no
  reason text — exactly the production shape; Opus demand), (b)
  `RuntimeError("an identify is already running")`; retry succeeds for
  both; a non-busy error is reported without retry.
- newer-scan interference: `list_scans()[0]` changes mid-poll → the
  "interrupted by a newer scan" answer, no false "not found".
- `scan_meta(new)` returns None throughout the general pass (minimal
  sidecar write failed) → tool proceeds to the focused pass, no crash.
- scan refused: `start_scan` raises → reason echoed, nothing else called.
- scan failed/cancelled: status → failed → honest report.
- archive failed: `done` but scan list unchanged → honest report.
- never found: both passes exhaust → the "try driving elsewhere" reply.
- budget: a status queue that never terminates → the tool returns (not
  hangs) once the patched clock passes the budget (inject a fake clock via
  `time.monotonic` patch or a step counter).
- `RealBackendSurfaceTest`: `start_scan` + `get_pano_status` pinned on both
  surfaces.

## Risks

- **Long turn holds the chat**: bounded at 600 s; precedent accepted (the
  identify tool holds 360 s). Streaming/stop lands in plan 038.
- **Racing another scan**: `/scan` 409s cleanly; the tool reports it.
- **Auto-identify contention on `_ident_busy`**: handled by the bounded 409
  retry loop; worst case the focused pass reports "still busy — try again".
- No motion: only the existing gimbal-sweep pipeline is triggered; the tool
  itself sends no motion commands. (Standing rule: I never trigger a live
  scan in dev/test either — fakes only.)

## Stages

1. Client + backend passthroughs + surface pins.
2. Tool flow + constants + tests.
3. Docs, CI, review gate, PR.

## Reviews

### Plan review

- **codex** — round 1: BLOCKED. (B1) 409 retry specified against
  `RuntimeError` while the client raises raw `HTTPError` — fakes would pass
  where production fails; (B2) `made`-only completion detection can miss a
  same-second identify; (B3) a newer scan after "done" kills our identify →
  silent false "not found". Non-blocking: `scan_meta None` handling,
  single monotonic deadline + fake-clock test, scan-cap alignment.
  **Resolution**: `.code == 409` classification + content-change `(made,
  objects)` detection + the newer-scan interference watch (6b) + all
  non-blocking items adopted.
- **Opus** — round 1: BLOCKED solely on the 409 contract, sharper still:
  `str(HTTPError)` carries NO reason text (it lives in the JSON body), so
  substring matching can't work and the busy test must raise a REAL
  `HTTPError(409)`. Also verified: "done" reliably follows archive+sidecar;
  gimbal-only safety airtight; single-flight identify; and discovered that
  a no-object identify never touches the sidecar (finished-empty ≡
  still-running → never-found path runs to budget; disclosed). Non-blocking:
  parse the 409 body for `start_scan` reasons, 320 s scan cap ≥ build
  timeout, monotonic clock consistency, gimbal-motion wording in the tool
  description. **Resolution**: all adopted verbatim.
- **Re-verification** — codex: PASS (all three blockers + non-blocking asks
  confirmed; no new issues). Opus: PASS (confirmed the two load-bearing
  controller facts — `SCAN_BUILD_TIMEOUT_S=300`, 409 body `error` field —
  and every resolution; poll caps and budget verified consistent).

### Code review

- **glm-5.1** — PASS. Verified deadline coverage in all three loops, every
  exit path honest (interrupted/not-found/timeout/refused/failed/archive-
  failed distinct), both busy shapes, fake fidelity, patch hygiene, safety.
- **codex** — PASS. One non-blocking ask: client-level pin on the
  start_scan 409 body parse — adopted (`StartScanClientTest`, both the
  body-reason and fp=None-fallback cases).
- **Opus** — PASS. Ran the full suite (417 green); traced the state machine
  against every test; confirmed the same-second test genuinely requires
  content-pair detection; flagged that the new client tests were added
  after staging (re-staged before commit). Non-blocking notes: transient
  empty scan list reads as "interrupted" (benign, never a false found);
  unreachable controller during the sweep reports "taking too long"
  (bounded; cosmetic).

## Post-execution report

Implemented as revised: `rover_scan_for` tool (600 s monotonic budget,
320 s sweep cap, general pass → focused pass with 409-retry, content-change
completion detection, newer-scan interference watch), client/backend
passthroughs (`start_scan` with 409-body reason parse, `get_pano_status`),
HELP_TEXT + docs rows, 13-test `test_scanfor.py` on a scripted hold-last
fake with patched sleep/monotonic clocks, surface pins extended.

Deviations: none from the revised plan. Deferred: streaming progress into
the chat (plan 038); the finished-empty-identify budget burn is disclosed
in-plan (no external signal exists to distinguish it).

Outcomes: plan gate codex BLOCKED→PASS + Opus BLOCKED→PASS; code gate glm
PASS + codex PASS + Opus PASS. CI green (417 tests). No motion commands
sent during dev/test; the tool itself was NOT live-fired (it sweeps the
gimbal — left for the user to try).
