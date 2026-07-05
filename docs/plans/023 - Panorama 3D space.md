# 023 - "3D space": stitched 360° panorama + website look-around viewer

## Goal (user /goal)
Stitch the scan images into a stored "3D space" and show it on the website as
an interactive view. No wheel motion.

## What was built
- scene.build_panorama: cv2.Stitcher over the eye+upper ring frames (ceiling
  excluded), downscaled ≤2400px, JPEG out; any stitcher exception/non-OK → None
  (the scan continues with a note). Saved to the scene dir + uploaded.
- Go: POST /panorama (JPEG-SOI + 8MB cap) stores photoDir/panorama.jpg;
  GET /panorama serves it.
- Website: 🌐 3D view button → embedded WebGL equirect look-around viewer
  (drag yaw/pitch, wheel zoom, vspan from image aspect, shader-link + WebGL
  fallbacks to a flat scrollable image, Esc/背景 close).
- $scan pipeline stitches + uploads automatically after the inventory.

## Validation
- Real-frame stitch quality eye-verified (both rings, full room, clean seams).
- Live end-to-end: build from a real scan, upload, GET roundtrip, page markers.
- Tests: synthetic-ring stitch, failure→None paths, endpoint validation, page
  markers, upload client. ci-local PASS.

## Review (glm; codex at usage limit) — APPROVE after 1 blocker fixed
Blocker: stitcher exceptions weren't guarded (would abort the scan) → wrapped,
returns None. Nit taken: shader link-status check → flat-image fallback.
Shader math, endpoint validation, memory caps, JS-in-raw-string all verified.
Deferred nits: EOI validation, atomic pano write, per-scan pano history.

## Post-execution report
Delivered per goal: images stitched into a stored 3D space (scene dir + rover),
website shows it interactively. Wheels never commanded. The pano is a real
spherical panorama (not volumetric 3D) — honest scope, matches the ask.
