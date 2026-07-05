# 022 - 360° scene scan → queryable spatial memory ($scan)

## Goal
"3D map, start simple": WITHOUT moving the wheels, photograph every direction
(gimbal pans ±180°), build a direction-labeled inventory of the surroundings
with the vision LLM, store it, and answer later questions ("what color is the
bin's lid behind you?") from the stored memory — no physical re-look.

## Honest scope
This is a panoramic SEMANTIC map (what is where, by compass direction), not 3D
geometry. It exactly serves the stated use-case; true 3D (depth/pointclouds)
would need different sensors/pipelines.

## Design
- `scene.py`:
  - `scan_frames(client, pans=(0,60,120,180,-120,-60), tilt=-5)` — for each pan:
    `/camera_aim` + settle + non-saving stream grab. Wide lens (~130° FOV) makes
    6 views a full circle with overlap. Camera recentered after. NO wheels.
  - `describe_scene(vision, frames)` — ONE multi-image LLM call (each view
    prefixed "View N — facing <front/right/behind/left/...> (pan X°)"): returns
    strict JSON {views:[{direction, objects:[{name,color,details}], summary}],
    overall}. Fallback: per-frame calls if the gateway rejects multi-image.
  - `save_scene`/`load_latest_scene` — frames + inventory persisted under
    `scenes/<UTC timestamp>/` next to the chatbot (not the rover gallery — no
    photo spam; frames retrievable for future re-inspection).
  - `render_inventory(inv)` — compact text for the chat context.
- `agent_chat.py`:
  - `$scan` + LLM tool `rover_scan_surroundings` → runs the scan and RETURNS THE
    FULL INVENTORY TEXT as the tool result — so follow-up questions are answered
    by the chat model from conversation context, zero re-looks by construction.
  - Tool `rover_scene_recall` → reloads the latest saved scene (post-restart or
    post-trim), same no-relook property.
  - SYSTEM prompt: after a scan, answer surroundings questions from the scan
    result; only re-scan if the user says the room changed.
- Gating: camera-only (like $cam/center) — not behind ROVER_FIND_ENABLE; no
  wheel motion whatsoever.

## Testing
Unit (fakes): direction labeling from pan; scan_frames aims+grabs per pan and
recenters; describe_scene builds one multi-image payload (mock client) + falls
back per-frame on error; save/load roundtrip; render_inventory. Live (camera
only): full scan of the actual room; verify the inventory is direction-labeled
and mentions known objects (blue suitcase right, bin behind, ...). ci-local.

## Risks
- Vision quality/gateway latency: one multi-image call ≈ one look's latency;
  fallback path is ~6 sequential calls (slow but works).
- Inventory can miss/err on objects — the user can re-scan; frames are kept so
  a future "re-inspect stored frame" tool can answer harder questions.

## Reviews
### Review (glm-5.1; codex out of credits) — APPROVE, no blockers
Verified: camera-only (set_camera+stream grab, no drive path), finally-recenter,
direction math at ±180, multi-image payload + graceful per-frame fallback,
save/load path safety, and that the tool-result prompt-injection surface is the
same one already accepted for find. Nits taken: timestamp-collision guard,
docstring TZ. Deferred: data-framing prefix for the inventory text, max_tokens
headroom.

## Post-execution report
Built scene.py (sweep/describe/render/persist), vision.describe_many
(multi-image), $scan + rover_scan_surroundings + rover_scene_recall, SYSTEM
guidance. LIVE-VALIDATED end-to-end: 6-view 360° scan of the real room; the
inventory recorded the user's exact test object — "clear plastic storage bin
with a black lid" behind the rover — so "what color is the bin's lid behind
you?" answers from memory with zero re-looks. Live scan also caught a real bug
(300° gimbal swing blurred one view) → monotonic pan order + sharpness-verified
(all 6 views sharp). 167 tests, ci-local PASS. Wheels never commanded.
