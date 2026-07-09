"""360° scene scan → queryable spatial memory (plan 022).

Without moving the wheels, sweep the gimbal a full circle, photograph each
direction, and have the vision LLM build a direction-labeled inventory of the
surroundings. The inventory is returned into the chat context and persisted, so
later questions ("what color is the bin's lid behind you?") are answered from
memory — no physical re-look. This is a panoramic SEMANTIC map, not 3D geometry.
"""
from __future__ import annotations

import json
import os
import time

# 6 views cover the full circle with heavy overlap (the lens is ~130° wide).
# MONOTONIC order: the gimbal can't wrap past ±180, so jumping 180 → -120 swings
# 300° the long way and the next frame catches it mid-move (blurred view in the
# first live scan). Stepping -120..180 keeps every move to 60°.
SCAN_PANS = (-120, -60, 0, 60, 120, 180)
SCAN_TILT = -5.0          # eye-level ring: floor through furniture height
UPPER_TILT = 35.0         # upper ring: shelves, desks, walls, windows
CEILING_TILT = 80.0       # one straight-up shot (ceiling is rotation-invariant)
SETTLE_S = 1.4
DESCRIBE_CHUNK = 7        # max images per vision call (keeps per-view fidelity)
SCENES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenes")

SCENE_PROMPT = (
    "You are building a spatial memory for a small rover from {n} photos taken "
    "by rotating its camera in place — each photo is labeled with the direction "
    "it faces. List EVERYTHING recognizable in each view. Reply ONLY with JSON: "
    "{{\"views\": [{{\"direction\": str, \"objects\": [{{\"name\": str, "
    "\"color\": str, \"details\": str}}], \"summary\": str}}], "
    "\"overall\": str}}. Be specific about colors (incl. parts, e.g. a bin and "
    "its lid) and positions (left/right, floor/shelf). Keep each details field "
    "under 12 words. One views[] entry per photo, in the given order, using the "
    "given direction labels.")


def direction_label(pan: float) -> str:
    """Human direction for a gimbal pan (0=front, +right, ±180=behind)."""
    p = (float(pan) + 180.0) % 360.0 - 180.0
    names = [(-180, "behind"), (-135, "back-left"), (-90, "left"),
             (-45, "front-left"), (0, "front"), (45, "front-right"),
             (90, "right"), (135, "back-right"), (180, "behind")]
    best = min(names, key=lambda nv: abs(p - nv[0]))
    return best[1]


def tier_label(tilt: float) -> str:
    """Height tier for a tilt: '' (eye level), 'upper', or 'ceiling'."""
    if tilt >= 60:
        return "ceiling"
    if tilt >= 20:
        return "upper"
    return ""


def view_label(pan: float, tilt: float) -> str:
    t = tier_label(tilt)
    if t == "ceiling":
        return "ceiling (straight up)"
    return direction_label(pan) + (f" ({t})" if t else "")


def scan_frames(client, *, pans=SCAN_PANS, tilts=(SCAN_TILT, UPPER_TILT),
                ceiling=True, settle_s=SETTLE_S, sleep=time.sleep,
                log=lambda m: None):
    """Aim → settle → non-saving stream grab for each (pan, tilt) view: an
    eye-level ring, an upper ring (shelves/walls), and one ceiling shot. The
    path is SERPENTINE (ring out, ring back) so every gimbal move stays small —
    big swings blurred frames in live testing. Camera recentered after. Gimbal
    only — the wheels are never commanded."""
    plan = []
    for i, tilt in enumerate(tilts):
        ring = list(pans) if i % 2 == 0 else list(reversed(pans))
        plan += [(p, tilt) for p in ring]
    if ceiling:
        # ceiling is rotation-invariant — shoot it from wherever the last ring
        # ended (avoids a long pan swing that could blur the frame)
        plan.append((plan[-1][0] if plan else 0, CEILING_TILT))
    frames = []
    try:
        for pan, tilt in plan:
            client.set_camera(pan, tilt)
            sleep(settle_s)
            img = client.get_stream_frame()
            frames.append((pan, tilt, img))
            log(f"  captured {view_label(pan, tilt)}")
    finally:
        try:
            client.set_camera(0, 0)        # leave the camera centered
        except Exception:
            pass
    return frames


def _shrink(jpeg, max_w=800):
    """Downscale a frame for VISION calls (payload sanity at high capture res);
    stitching keeps the full-res originals. Passthrough without OpenCV."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return jpeg
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None or img.shape[1] <= max_w:
        return jpeg
    h = int(img.shape[0] * max_w / img.shape[1])
    ok, buf = cv2.imencode(".jpg", cv2.resize(img, (max_w, h), interpolation=cv2.INTER_AREA),
                           [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return buf.tobytes() if ok else jpeg


def _describe_chunk(vision, chunk, log):
    """One multi-image call for ≤DESCRIBE_CHUNK frames; per-frame fallback."""
    labels = [f"View {i + 1} — facing {view_label(p, t)}"
              for i, (p, t, _) in enumerate(chunk)]
    prompt = SCENE_PROMPT.format(n=len(chunk))
    try:
        out = vision.describe_many(
            [(lab, _shrink(img)) for lab, (_, _, img) in zip(labels, chunk)],
            prompt, json_out=True, max_tokens=3200)
        if isinstance(out, dict) and out.get("views"):
            return out
    except Exception as e:
        log(f"  multi-image call failed ({e}); describing per view")
    views = []
    for pan, tilt, img in chunk:
        lab = view_label(pan, tilt)
        one = ("Describe this photo for a rover's spatial memory: it faces "
               f"{lab}. Reply ONLY with JSON: {{\"direction\": \"{lab}\", "
               "\"objects\": [{\"name\": str, \"color\": str, \"details\": str}], "
               "\"summary\": str}. Be specific about colors, including parts like lids.")
        try:
            views.append(vision.describe(_shrink(img), one, json_out=True, max_tokens=900))
        except Exception as e:
            views.append({"direction": lab, "objects": [],
                          "summary": f"(view failed: {e})"})
    return {"views": views, "overall": ""}


def describe_scene(vision, frames, log=lambda m: None):
    """Batched multi-image vision calls (≤DESCRIBE_CHUNK images each — 13 views
    in one call dilutes per-view fidelity) merged into one inventory dict."""
    inv = {"views": [], "overall": ""}
    for i in range(0, len(frames), DESCRIBE_CHUNK):
        part = _describe_chunk(vision, frames[i:i + DESCRIBE_CHUNK], log)
        inv["views"] += part.get("views") or []
        if part.get("overall"):
            inv["overall"] = (inv["overall"] + " " + part["overall"]).strip()
    return inv


SWEEP_RING_STEP = 10      # sweep density: 10° + a real settle = sharp frames
                          # (2.5°/0.1s live-tested: gimbal never stops → motion blur)
SWEEP_FRAME_W = 960       # downscale in flight (a strip only needs ~25px anyway)


def sweep_frames(client, *, tilts=(SCAN_TILT, UPPER_TILT), step_deg=SWEEP_RING_STEP,
                 settle_s=0.9, ceiling=True, sleep=time.sleep, log=lambda m: None):
    """Dense video-style sweep for the panorama build: both rings in a
    serpentine at `step_deg`, plus one ceiling still. Frames are downscaled in
    flight (memory). Nothing is saved anywhere — the frames are raw material
    for build_panorama and are discarded after. Camera only."""
    def shrink(jpeg):
        return _shrink(jpeg, max_w=SWEEP_FRAME_W)
    frames = []
    try:
        for i, tilt in enumerate(tilts):
            pans = [(-180.0 + k * step_deg) for k in range(int(360.0 / step_deg) + 1)]
            if i % 2 == 1:
                pans.reverse()
            for pan in pans:
                client.set_camera(pan, tilt)
                sleep(settle_s)
                frames.append((pan, tilt, shrink(client.get_stream_frame())))
                if len(frames) % 40 == 0:
                    log(f"  swept {len(frames)} frames (pan {pan:.0f}°, tilt {tilt:.0f}°)")
        if ceiling:
            client.set_camera(frames[-1][0] if frames else 0, CEILING_TILT)
            sleep(1.0)
            frames.append((frames[-1][0] if frames else 0, CEILING_TILT,
                           shrink(client.get_stream_frame())))
    finally:
        try:
            client.set_camera(0, 0)
        except Exception:
            pass
    return frames


TOUR_STEP_DEG = 2.5
TOUR_TILT = -5.0
TOUR_SETTLE_S = 0.10


def record_tour(client, *, step_deg=TOUR_STEP_DEG, tilt=TOUR_TILT,
                settle_s=TOUR_SETTLE_S, sleep=time.sleep, log=lambda m: None):
    """Record a smooth 360° video tour: sweep the gimbal in small steps and grab
    a live-stream frame at each — no stitching at all, so it can't show seams.
    Returns the frames as one concatenated MJPEG bytes blob (the controller's
    /tour_feed plays it in a loop). Camera only; wheels never commanded."""
    frames = []
    try:
        pan = -180.0
        while pan <= 180.0:
            client.set_camera(pan, tilt)
            sleep(settle_s)
            frames.append(client.get_stream_frame())
            if len(frames) % 20 == 0:
                log(f"  recorded {len(frames)} frames (pan {pan:.0f}°)")
            pan += step_deg
    finally:
        try:
            client.set_camera(0, 0)
        except Exception:
            pass
    return b"".join(frames)


def render_inventory(inv) -> str:
    """Compact, chat-friendly text of the scene inventory."""
    if not isinstance(inv, dict):
        return str(inv)
    out = []
    for v in inv.get("views") or []:
        objs = "; ".join(
            " ".join(x for x in (str(o.get("color") or "").strip(),
                                 str(o.get("name") or "").strip()) if x)
            + (f" ({o.get('details')})" if o.get("details") else "")
            for o in (v.get("objects") or []) if isinstance(o, dict))
        out.append(f"[{v.get('direction', '?')}] {objs or v.get('summary', '')}")
    if inv.get("overall"):
        out.append(f"overall: {inv['overall']}")
    if out:
        out.append(
            "note: directions are approximate and OVERLAP — \"behind you\" includes "
            "back-left, behind AND back-right; \"in front\" includes front-left, front "
            "AND front-right; the wide lens means one object often appears in several "
            "adjacent views. Check every relevant view (and object synonyms: bin = "
            "container = tub = box) before concluding something is not in the scene.")
    return "\n".join(out) or "(empty scene)"


def _fisheye_focal(frames):
    """Fisheye focal (px/radian) measured from the pixel shift between adjacent
    eye-ring frames (template matching). None if unmeasurable."""
    import cv2
    import numpy as np
    ring = sorted((p, im) for p, t, im in frames if abs(t - SCAN_TILT) < 1)
    shifts, step = [], None
    for (p1, a), (p2, b) in zip(ring, ring[1:]):
        step = abs(p2 - p1)
        h, w = a.shape[:2]
        strip = slice(int(h * 0.30), int(h * 0.70))
        ag = cv2.cvtColor(a[strip], cv2.COLOR_BGR2GRAY)
        bg = cv2.cvtColor(b[strip], cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(ag, bg[:, :w // 4], cv2.TM_CCOEFF_NORMED)
        _, conf, _, loc = cv2.minMaxLoc(res)
        if conf > 0.5 and 4 <= loc[0] < 0.95 * w:   # dense sweeps shift only ~20px
            shifts.append(loc[0])
    if not shifts or not step:
        return None
    import math
    shifts.sort()
    return shifts[len(shifts) // 2] / math.radians(step)


def _correct_sweep_pans(metas, frames):
    """Dense sweeps tag frames with the COMMANDED pan, but the gimbal trails by
    a varying lag — strips land at wrong longitudes and smear the panorama.
    Re-estimate each frame's TRUE pan by visual odometry: template-match the
    shift between consecutive frames of a ring, integrate, and rescale so the
    ring still spans exactly its commanded range."""
    import cv2
    import numpy as np
    out = list(metas)
    # group ring indices by tilt, in capture order
    by_tilt = {}
    for i, (pan, tilt) in enumerate(metas):
        if tier_label(tilt) == "ceiling":
            continue
        by_tilt.setdefault(round(tilt, 1), []).append(i)
    for tilt, idxs in by_tilt.items():
        if len(idxs) < 3:
            continue
        grays = []
        for i in idxs:
            im = cv2.imdecode(np.frombuffer(frames[i][2], np.uint8),
                              cv2.IMREAD_REDUCED_GRAYSCALE_2)
            grays.append(im)
        shifts = []
        for a, b in zip(grays, grays[1:]):
            if a is None or b is None:
                shifts.append(None)
                continue
            h, w = a.shape
            strip = slice(int(h * 0.30), int(h * 0.70))
            res = cv2.matchTemplate(a[strip], b[strip, : w // 3], cv2.TM_CCOEFF_NORMED)
            _, conf, _, loc = cv2.minMaxLoc(res)
            shifts.append(float(loc[0]) if conf > 0.4 else None)
        good = [x for x in shifts if x is not None]
        if len(good) < len(shifts) // 2 or not good:
            continue                        # too little signal — keep commanded pans
        med = sorted(good)[len(good) // 2]
        shifts = [x if x is not None and 0 <= x <= 3 * med + 5 else med for x in shifts]
        pos = [0.0]
        for x in shifts:
            pos.append(pos[-1] + x)
        span = metas[idxs[-1]][0] - metas[idxs[0]][0]   # commanded ring span (signed)
        total = pos[-1] or 1.0
        for k, i in enumerate(idxs):
            out[i] = (metas[idxs[0]][0] + span * pos[k] / total, metas[i][1])
    return out


def _undistort_equidistant(im, f_fish, out_fov_deg=95):
    """Fisheye (r = f·θ) → pinhole (r = f·tanθ), cropped to out_fov — feature
    stitchers assume pinhole projection and misalign raw fisheye frames."""
    import cv2
    import numpy as np
    import math
    h, w = im.shape[:2]
    f_pin = (w / 2) / math.tan(math.radians(out_fov_deg / 2))
    X, Y = np.meshgrid(np.arange(w) - w / 2, np.arange(h) - h / 2)
    r_pin = np.sqrt(X ** 2 + Y ** 2)
    theta = np.arctan(r_pin / f_pin)
    scale = np.where(r_pin > 0.5, f_fish * theta / np.maximum(r_pin, 0.5), 1.0)
    mx = (X * scale + w / 2).astype(np.float32)
    my = (Y * scale + h / 2).astype(np.float32)
    return cv2.remap(im, mx, my, cv2.INTER_LINEAR)


def _seamcut_pano(frames, max_width=4096):
    """Best-quality merge (plan 025): undistort each fisheye frame to pinhole,
    warp onto the sphere at its KNOWN pan/tilt, even out exposure, cut each
    overlap along an optimal GRAPH-CUT seam (each pixel comes from exactly ONE
    photo — no averaging, no ghosting), and multiband-blend across the cuts.
    Output is padded onto the full 2:1 equirect canvas so the web viewer's
    mapping stays exact. Returns JPEG bytes or None on any failure."""
    import cv2
    import numpy as np
    import math
    decoded = []
    for pan, tilt, img in frames:
        im = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
        if im is not None:
            decoded.append((float(pan), float(tilt), im))
    if len(decoded) < 3:
        return None
    try:
        f_fish = _fisheye_focal(decoded) or decoded[0][2].shape[1] / 2.27
        FOV = 88.0   # crop balance (live-tested on the real scan): 92° keeps the
                     # most coverage but duplicates near-field objects at seams; 82°
                     # kills duplicates but loses top coverage + smudges thin overlaps
        h, w = decoded[0][2].shape[:2]
        f_pin = (w / 2) / math.tan(math.radians(FOV / 2))
        K = np.array([[f_pin, 0, w / 2], [0, f_pin, h / 2], [0, 0, 1]], np.float32)

        def rmat(pan, tilt):
            p, t = math.radians(pan), math.radians(tilt)
            ry = np.array([[math.cos(p), 0, math.sin(p)], [0, 1, 0],
                           [-math.sin(p), 0, math.cos(p)]])
            rx = np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)],
                           [0, math.sin(t), math.cos(t)]])
            return (ry @ rx).astype(np.float32)

        warper = cv2.PyRotationWarper("spherical", f_pin)
        imgs_w, masks_w, corners, sizes = [], [], [], []
        for pan, tilt, im in decoded:
            u = _undistort_equidistant(im, f_fish, out_fov_deg=FOV)
            R = rmat(pan, tilt)
            corner, img_w = warper.warp(u, K, R, cv2.INTER_LINEAR, cv2.BORDER_CONSTANT)
            _, mask_w = warper.warp(255 * np.ones((h, w), np.uint8), K, R,
                                    cv2.INTER_NEAREST, cv2.BORDER_CONSTANT)
            imgs_w.append(img_w)
            masks_w.append(mask_w)
            corners.append(corner)
            sizes.append((img_w.shape[1], img_w.shape[0]))
        comp = cv2.detail.ExposureCompensator_createDefault(
            cv2.detail.ExposureCompensator_GAIN_BLOCKS)
        comp.feed(corners=corners, images=imgs_w, masks=masks_w)
        for i, (im_, m_) in enumerate(zip(imgs_w, masks_w)):
            comp.apply(i, corners[i], im_, m_)
        # graph-cut at ~0.25 scale (standard practice): seam QUALITY is about
        # path topology, not resolution — and full-res cuts took ~5 minutes.
        SEAM_SCALE = 0.25
        small = [cv2.resize(im, None, fx=SEAM_SCALE, fy=SEAM_SCALE,
                            interpolation=cv2.INTER_AREA).astype(np.float32)
                 for im in imgs_w]
        small_masks = [cv2.resize(m, (im.shape[1], im.shape[0]),
                                  interpolation=cv2.INTER_NEAREST)
                       for m, im in zip(masks_w, small)]
        small_corners = [(int(c[0] * SEAM_SCALE), int(c[1] * SEAM_SCALE)) for c in corners]
        finder = cv2.detail_GraphCutSeamFinder("COST_COLOR")
        seams = finder.find(small, small_corners, small_masks)
        seams = [m.get() if hasattr(m, "get") else m for m in seams]
        # dilate the upscaled cut masks a little so neighbors overlap — the
        # nearest-upscale otherwise leaves 1px cracks; multiband blends overlaps
        kern = np.ones((7, 7), np.uint8)
        seams = [cv2.bitwise_and(
                    cv2.dilate(cv2.resize(sm, (mw.shape[1], mw.shape[0]),
                                          interpolation=cv2.INTER_NEAREST), kern), mw)
                 for sm, mw in zip(seams, masks_w)]
        blender = cv2.detail_MultiBandBlender(0, 5)
        roi = cv2.detail.resultRoi(corners=corners, sizes=sizes)
        blender.prepare(roi)
        for im_, m_, c in zip(imgs_w, seams, corners):
            blender.feed(im_.astype(np.int16), m_, c)
        pano, _ = blender.blend(None, None)
        pano = np.clip(pano, 0, 255).astype(np.uint8)
        # pad onto the full sphere canvas: spherical warper coords are radians
        # × f_pin, x∈[-π f, π f], y∈[-π/2 f, π/2 f]
        full_w = int(round(2 * math.pi * f_pin))
        full_h = int(round(math.pi * f_pin))
        canvas = np.zeros((full_h, full_w, 3), np.uint8)
        # warper coords: x∈[-π f, π f] (0 = front), y∈[0, π f] (equator at π/2 f)
        x0 = int(roi[0] + full_w // 2)
        y0 = int(roi[1])
        for row in range(pano.shape[0]):
            ry_ = y0 + row
            if 0 <= ry_ < full_h:
                xs = (np.arange(pano.shape[1]) + x0) % full_w
                canvas[ry_, xs] = pano[row]
        if full_w > max_width:
            canvas = cv2.resize(canvas, (max_width, max_width // 2),
                                interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        return buf.tobytes() if ok else None
    except (cv2.error, ValueError):
        return None


def _stitcher_pano(frames, max_width=4096):
    """Quality path: undistort to pinhole, then OpenCV's full stitching pipeline
    (bundle adjustment, gain compensation, graph-cut seams, multiband blending).
    Near-invisible seams when the scene has features; returns None when the
    result is partial (>25% black) or stitching fails — the known-pose
    projector then guarantees completeness."""
    import cv2
    import numpy as np
    decoded = []
    for pan, tilt, img in frames:
        im = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
        if im is not None:
            decoded.append((float(pan), float(tilt), im))
    if len(decoded) < 3:
        return None
    f = _fisheye_focal(decoded) or decoded[0][2].shape[1] / 2.27
    und = [_undistort_equidistant(im, f) for _, _, im in decoded]
    try:
        st = cv2.Stitcher.create(cv2.Stitcher_PANORAMA)
        status, pano = st.stitch(und)
    except cv2.error:
        return None
    if status != cv2.Stitcher_OK or pano is None:
        return None
    g = cv2.cvtColor(pano, cv2.COLOR_BGR2GRAY)
    # Good stitches carry ~10-15% black border from the projection bounds; the
    # degenerate under-furniture scene measured 22-39%. Gate between them.
    if float((g < 8).mean()) > 0.18:
        return None
    h, w = pano.shape[:2]
    if w > max_width:
        pano = cv2.resize(pano, (max_width, int(h * max_width / w)),
                          interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", pano, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return buf.tobytes() if ok else None


def build_panorama(frames, width=3600, strip_sigma_deg=None, try_stitcher=True):
    """Project frames onto a true 2:1 EQUIRECTANGULAR panorama.

    Two modes, auto-selected from the ring spacing:
    - SLIT-SCAN (dense video sweep, spacing < 15°): each frame contributes only
      a thin vertical strip around its optical axis — the lens's sharpest,
      least-distorted region, with negligible parallax between neighboring
      strips. This is how phone panoramas avoid the "multiple images" look.
    - WIDE (sparse 13-still scan): frames blend by full angular falloff.

    Fisheye model r = f·θ with f measured from adjacent-frame overlap; frames
    are gain-compensated (per-direction auto-exposure) and streamed one at a
    time (a dense sweep is ~290 frames). Returns JPEG bytes or None."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    import math
    metas = [(float(p), float(t)) for p, t, _ in frames]
    if len(metas) < 3:
        return None
    ring = sorted(p for (p, t), (_, _, _) in zip(metas, frames) if abs(t - SCAN_TILT) < 1)
    spacing = 60.0
    if len(ring) > 1:
        diffs = sorted(b - a for a, b in zip(ring, ring[1:]) if b > a)
        if diffs:
            spacing = diffs[len(diffs) // 2]
    if strip_sigma_deg is None and spacing < 15.0:
        strip_sigma_deg = max(1.0, spacing * 0.9)

    if try_stitcher and strip_sigma_deg is None:
        best = _seamcut_pano(frames)       # best merge: known-pose seam-cut
        if best is None:
            best = _stitcher_pano(frames)  # feature-based fallback
        if best is not None:
            return best

    if strip_sigma_deg is not None:
        metas = _correct_sweep_pans(metas, frames)

    W = int(width)
    H = W // 2
    lon = (np.arange(W) + 0.5) / W * 2 * math.pi - math.pi
    lat = math.pi / 2 - (np.arange(H) + 0.5) / H * math.pi
    LON, LAT = np.meshgrid(lon, lat)
    LON_DEG = np.degrees(LON)
    wx = np.sin(LON) * np.cos(LAT)
    wy = np.sin(LAT)
    wz = np.cos(LON) * np.cos(LAT)
    acc = np.zeros((H, W, 3), np.float64)
    wgt = np.zeros((H, W), np.float64) + 1e-9

    # pass 1 (cheap): per-frame exposure means from thumbnails; focal from a
    # decoded eye-ring subset
    means = []
    eye_imgs = []
    for (pan, tilt), (_, _, jpeg) in zip(metas, frames):
        im = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_COLOR_4)
        if im is None:
            means.append(None)
            continue
        h, w = im.shape[:2]
        means.append(im[h // 4:3 * h // 4, w // 4:3 * w // 4].reshape(-1, 3).mean(axis=0))
        if abs(tilt - SCAN_TILT) < 1 and len(eye_imgs) < 8:
            eye_imgs.append((pan, jpeg))
    valid_means = [m for m in means if m is not None]
    if not valid_means:
        return None
    global_mean = np.mean(valid_means, axis=0)

    f = None
    if len(eye_imgs) > 1:
        dec = [(p, cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR))
               for p, j in eye_imgs]
        f = _fisheye_focal([(p, SCAN_TILT, im) for p, im in dec if im is not None])
    # pass 2: stream-project
    first = cv2.imdecode(np.frombuffer(frames[0][2], np.uint8), cv2.IMREAD_COLOR)
    if first is None:
        return None
    if f is None:
        f = first.shape[1] / math.radians(130)
    rim = f * math.radians(62)
    for (pan, tilt), (_, _, jpeg), mean in zip(metas, frames, means):
        if mean is None:
            continue
        im = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            continue
        gain = np.clip(global_mean / np.maximum(mean, 1.0), 0.55, 1.8)
        h, w = im.shape[:2]
        cx, cy = w / 2.0, h / 2.0
        p, t = math.radians(pan), math.radians(tilt)
        cz = np.array([math.sin(p) * math.cos(t), math.sin(t), math.cos(p) * math.cos(t)])
        cxv = np.array([math.cos(p), 0.0, -math.sin(p)])
        cyv = -np.cross(cz, cxv)
        dz = wx * cz[0] + wy * cz[1] + wz * cz[2]
        # cheap prefilter: skip output columns far outside this frame's reach
        theta = np.arccos(np.clip(dz, -1, 1))
        r = f * theta
        dx = wx * cxv[0] + wy * cxv[1] + wz * cxv[2]
        dy = wx * cyv[0] + wy * cyv[1] + wz * cyv[2]
        alpha = np.arctan2(dy, dx)
        mx = (cx + r * np.cos(alpha)).astype(np.float32)
        my = (cy + r * np.sin(alpha)).astype(np.float32)
        valid = (dz > 0.05) & (mx >= 0) & (mx < w - 1) & (my >= 0) & (my < h - 1)
        wf = np.where(valid, np.clip(1.0 - r / rim, 0, 1) ** 1.5, 0)
        if strip_sigma_deg is not None and tier_label(tilt) != "ceiling":
            dlon = (LON_DEG - pan + 180.0) % 360.0 - 180.0
            wf = wf * np.exp(-(dlon / strip_sigma_deg) ** 2)
        if not wf.any():
            continue
        samp = cv2.remap(im, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        samp = np.clip(samp.astype(np.float64) * gain[None, None, :], 0, 255)
        acc += samp * wf[..., None]
        wgt += wf
    pano = (acc / wgt[..., None]).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", pano, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return buf.tobytes() if ok else None


def record_tour(client, *, step_deg=TOUR_STEP_DEG, tilt=TOUR_TILT,
                settle_s=TOUR_SETTLE_S, sleep=time.sleep, log=lambda m: None):
    """Record a smooth 360° video tour: sweep the gimbal in small steps and grab
    a live-stream frame at each — no stitching at all, so it can't show seams.
    Returns the frames as one concatenated MJPEG bytes blob (the controller's
    /tour_feed plays it in a loop). Camera only; wheels never commanded."""
    frames = []
    try:
        pan = -180.0
        while pan <= 180.0:
            client.set_camera(pan, tilt)
            sleep(settle_s)
            frames.append(client.get_stream_frame())
            if len(frames) % 20 == 0:
                log(f"  recorded {len(frames)} frames (pan {pan:.0f}°)")
            pan += step_deg
    finally:
        try:
            client.set_camera(0, 0)
        except Exception:
            pass
    return b"".join(frames)


def render_inventory(inv) -> str:
    """Compact, chat-friendly text of the scene inventory."""
    if not isinstance(inv, dict):
        return str(inv)
    out = []
    for v in inv.get("views") or []:
        objs = "; ".join(
            " ".join(x for x in (str(o.get("color") or "").strip(),
                                 str(o.get("name") or "").strip()) if x)
            + (f" ({o.get('details')})" if o.get("details") else "")
            for o in (v.get("objects") or []) if isinstance(o, dict))
        out.append(f"[{v.get('direction', '?')}] {objs or v.get('summary', '')}")
    if inv.get("overall"):
        out.append(f"overall: {inv['overall']}")
    if out:
        out.append(
            "note: directions are approximate and OVERLAP — \"behind you\" includes "
            "back-left, behind AND back-right; \"in front\" includes front-left, front "
            "AND front-right; the wide lens means one object often appears in several "
            "adjacent views. Check every relevant view (and object synonyms: bin = "
            "container = tub = box) before concluding something is not in the scene.")
    return "\n".join(out) or "(empty scene)"


def _fisheye_focal(frames):
    """Fisheye focal (px/radian) measured from the pixel shift between adjacent
    eye-ring frames (template matching). None if unmeasurable."""
    import cv2
    import numpy as np
    ring = sorted((p, im) for p, t, im in frames if abs(t - SCAN_TILT) < 1)
    shifts, step = [], None
    for (p1, a), (p2, b) in zip(ring, ring[1:]):
        step = abs(p2 - p1)
        h, w = a.shape[:2]
        strip = slice(int(h * 0.30), int(h * 0.70))
        ag = cv2.cvtColor(a[strip], cv2.COLOR_BGR2GRAY)
        bg = cv2.cvtColor(b[strip], cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(ag, bg[:, :w // 4], cv2.TM_CCOEFF_NORMED)
        _, conf, _, loc = cv2.minMaxLoc(res)
        if conf > 0.5 and 4 <= loc[0] < 0.95 * w:   # dense sweeps shift only ~20px
            shifts.append(loc[0])
    if not shifts or not step:
        return None
    import math
    shifts.sort()
    return shifts[len(shifts) // 2] / math.radians(step)


def sweep_frames(client, *, tilts=(SCAN_TILT, UPPER_TILT), step_deg=SWEEP_RING_STEP,
                 settle_s=0.9, ceiling=True, sleep=time.sleep, log=lambda m: None):
    """Dense video-style sweep for the panorama build: both rings in a
    serpentine at `step_deg`, plus one ceiling still. Frames are downscaled in
    flight (memory). Nothing is saved anywhere — the frames are raw material
    for build_panorama and are discarded after. Camera only."""
    def shrink(jpeg):
        return _shrink(jpeg, max_w=SWEEP_FRAME_W)
    frames = []
    try:
        for i, tilt in enumerate(tilts):
            pans = [(-180.0 + k * step_deg) for k in range(int(360.0 / step_deg) + 1)]
            if i % 2 == 1:
                pans.reverse()
            for pan in pans:
                client.set_camera(pan, tilt)
                sleep(settle_s)
                frames.append((pan, tilt, shrink(client.get_stream_frame())))
                if len(frames) % 40 == 0:
                    log(f"  swept {len(frames)} frames (pan {pan:.0f}°, tilt {tilt:.0f}°)")
        if ceiling:
            client.set_camera(frames[-1][0] if frames else 0, CEILING_TILT)
            sleep(1.0)
            frames.append((frames[-1][0] if frames else 0, CEILING_TILT,
                           shrink(client.get_stream_frame())))
    finally:
        try:
            client.set_camera(0, 0)
        except Exception:
            pass
    return frames


TOUR_STEP_DEG = 2.5
TOUR_TILT = -5.0
TOUR_SETTLE_S = 0.10


def record_tour(client, *, step_deg=TOUR_STEP_DEG, tilt=TOUR_TILT,
                settle_s=TOUR_SETTLE_S, sleep=time.sleep, log=lambda m: None):
    """Record a smooth 360° video tour: sweep the gimbal in small steps and grab
    a live-stream frame at each — no stitching at all, so it can't show seams.
    Returns the frames as one concatenated MJPEG bytes blob (the controller's
    /tour_feed plays it in a loop). Camera only; wheels never commanded."""
    frames = []
    try:
        pan = -180.0
        while pan <= 180.0:
            client.set_camera(pan, tilt)
            sleep(settle_s)
            frames.append(client.get_stream_frame())
            if len(frames) % 20 == 0:
                log(f"  recorded {len(frames)} frames (pan {pan:.0f}°)")
            pan += step_deg
    finally:
        try:
            client.set_camera(0, 0)
        except Exception:
            pass
    return b"".join(frames)


def render_inventory(inv) -> str:
    """Compact, chat-friendly text of the scene inventory."""
    if not isinstance(inv, dict):
        return str(inv)
    out = []
    for v in inv.get("views") or []:
        objs = "; ".join(
            " ".join(x for x in (str(o.get("color") or "").strip(),
                                 str(o.get("name") or "").strip()) if x)
            + (f" ({o.get('details')})" if o.get("details") else "")
            for o in (v.get("objects") or []) if isinstance(o, dict))
        out.append(f"[{v.get('direction', '?')}] {objs or v.get('summary', '')}")
    if inv.get("overall"):
        out.append(f"overall: {inv['overall']}")
    if out:
        out.append(
            "note: directions are approximate and OVERLAP — \"behind you\" includes "
            "back-left, behind AND back-right; \"in front\" includes front-left, front "
            "AND front-right; the wide lens means one object often appears in several "
            "adjacent views. Check every relevant view (and object synonyms: bin = "
            "container = tub = box) before concluding something is not in the scene.")
    return "\n".join(out) or "(empty scene)"


def _ring_shift_px(imgs, step_deg=60):
    """Measured horizontal pixel shift for one pan step: the left edge of the
    NEXT frame appears somewhere in the PREVIOUS frame — template-match it.
    Returns the median across pairs (px per step), or None if unmeasurable."""
    import cv2
    import numpy as np
    shifts = []
    for a, b in zip(imgs, imgs[1:]):
        h, w = a.shape[:2]
        strip = slice(int(h * 0.30), int(h * 0.70))
        tmpl = b[strip, 0:w // 4]
        res = cv2.matchTemplate(a[strip, :], tmpl, cv2.TM_CCOEFF_NORMED)
        _, conf, _, loc = cv2.minMaxLoc(res)
        if conf > 0.5 and 4 <= loc[0] < 0.95 * w:   # dense sweeps shift only ~20px
            shifts.append(loc[0])
    if not shifts:
        return None
    shifts.sort()
    return float(shifts[len(shifts) // 2])


SWEEP_RING_STEP = 10      # sweep density: 10° + a real settle = sharp frames
                          # (2.5°/0.1s live-tested: gimbal never stops → motion blur)
SWEEP_FRAME_W = 960       # downscale in flight (a strip only needs ~25px anyway)


def sweep_frames(client, *, tilts=(SCAN_TILT, UPPER_TILT), step_deg=SWEEP_RING_STEP,
                 settle_s=0.9, ceiling=True, sleep=time.sleep, log=lambda m: None):
    """Dense video-style sweep for the panorama build: both rings in a
    serpentine at `step_deg`, plus one ceiling still. Frames are downscaled in
    flight (memory). Nothing is saved anywhere — the frames are raw material
    for build_panorama and are discarded after. Camera only."""
    def shrink(jpeg):
        return _shrink(jpeg, max_w=SWEEP_FRAME_W)
    frames = []
    try:
        for i, tilt in enumerate(tilts):
            pans = [(-180.0 + k * step_deg) for k in range(int(360.0 / step_deg) + 1)]
            if i % 2 == 1:
                pans.reverse()
            for pan in pans:
                client.set_camera(pan, tilt)
                sleep(settle_s)
                frames.append((pan, tilt, shrink(client.get_stream_frame())))
                if len(frames) % 40 == 0:
                    log(f"  swept {len(frames)} frames (pan {pan:.0f}°, tilt {tilt:.0f}°)")
        if ceiling:
            client.set_camera(frames[-1][0] if frames else 0, CEILING_TILT)
            sleep(1.0)
            frames.append((frames[-1][0] if frames else 0, CEILING_TILT,
                           shrink(client.get_stream_frame())))
    finally:
        try:
            client.set_camera(0, 0)
        except Exception:
            pass
    return frames


TOUR_STEP_DEG = 2.5
TOUR_TILT = -5.0
TOUR_SETTLE_S = 0.10


def record_tour(client, *, step_deg=TOUR_STEP_DEG, tilt=TOUR_TILT,
                settle_s=TOUR_SETTLE_S, sleep=time.sleep, log=lambda m: None):
    """Record a smooth 360° video tour: sweep the gimbal in small steps and grab
    a live-stream frame at each — no stitching at all, so it can't show seams.
    Returns the frames as one concatenated MJPEG bytes blob (the controller's
    /tour_feed plays it in a loop). Camera only; wheels never commanded."""
    frames = []
    try:
        pan = -180.0
        while pan <= 180.0:
            client.set_camera(pan, tilt)
            sleep(settle_s)
            frames.append(client.get_stream_frame())
            if len(frames) % 20 == 0:
                log(f"  recorded {len(frames)} frames (pan {pan:.0f}°)")
            pan += step_deg
    finally:
        try:
            client.set_camera(0, 0)
        except Exception:
            pass
    return b"".join(frames)


def render_inventory(inv) -> str:
    """Compact, chat-friendly text of the scene inventory."""
    if not isinstance(inv, dict):
        return str(inv)
    out = []
    for v in inv.get("views") or []:
        objs = "; ".join(
            " ".join(x for x in (str(o.get("color") or "").strip(),
                                 str(o.get("name") or "").strip()) if x)
            + (f" ({o.get('details')})" if o.get("details") else "")
            for o in (v.get("objects") or []) if isinstance(o, dict))
        out.append(f"[{v.get('direction', '?')}] {objs or v.get('summary', '')}")
    if inv.get("overall"):
        out.append(f"overall: {inv['overall']}")
    if out:
        out.append(
            "note: directions are approximate and OVERLAP — \"behind you\" includes "
            "back-left, behind AND back-right; \"in front\" includes front-left, front "
            "AND front-right; the wide lens means one object often appears in several "
            "adjacent views. Check every relevant view (and object synonyms: bin = "
            "container = tub = box) before concluding something is not in the scene.")
    return "\n".join(out) or "(empty scene)"


SWEEP_RING_STEP = 10      # sweep density: 10° + a real settle = sharp frames
                          # (2.5°/0.1s live-tested: gimbal never stops → motion blur)
SWEEP_FRAME_W = 960       # downscale in flight (a strip only needs ~25px anyway)


def sweep_frames(client, *, tilts=(SCAN_TILT, UPPER_TILT), step_deg=SWEEP_RING_STEP,
                 settle_s=0.9, ceiling=True, sleep=time.sleep, log=lambda m: None):
    """Dense video-style sweep for the panorama build: both rings in a
    serpentine at `step_deg`, plus one ceiling still. Frames are downscaled in
    flight (memory). Nothing is saved anywhere — the frames are raw material
    for build_panorama and are discarded after. Camera only."""
    def shrink(jpeg):
        return _shrink(jpeg, max_w=SWEEP_FRAME_W)
    frames = []
    try:
        for i, tilt in enumerate(tilts):
            pans = [(-180.0 + k * step_deg) for k in range(int(360.0 / step_deg) + 1)]
            if i % 2 == 1:
                pans.reverse()
            for pan in pans:
                client.set_camera(pan, tilt)
                sleep(settle_s)
                frames.append((pan, tilt, shrink(client.get_stream_frame())))
                if len(frames) % 40 == 0:
                    log(f"  swept {len(frames)} frames (pan {pan:.0f}°, tilt {tilt:.0f}°)")
        if ceiling:
            client.set_camera(frames[-1][0] if frames else 0, CEILING_TILT)
            sleep(1.0)
            frames.append((frames[-1][0] if frames else 0, CEILING_TILT,
                           shrink(client.get_stream_frame())))
    finally:
        try:
            client.set_camera(0, 0)
        except Exception:
            pass
    return frames


TOUR_STEP_DEG = 2.5
TOUR_TILT = -5.0
TOUR_SETTLE_S = 0.10


def record_tour(client, *, step_deg=TOUR_STEP_DEG, tilt=TOUR_TILT,
                settle_s=TOUR_SETTLE_S, sleep=time.sleep, log=lambda m: None):
    """Record a smooth 360° video tour: sweep the gimbal in small steps and grab
    a live-stream frame at each — no stitching at all, so it can't show seams.
    Returns the frames as one concatenated MJPEG bytes blob (the controller's
    /tour_feed plays it in a loop). Camera only; wheels never commanded."""
    frames = []
    try:
        pan = -180.0
        while pan <= 180.0:
            client.set_camera(pan, tilt)
            sleep(settle_s)
            frames.append(client.get_stream_frame())
            if len(frames) % 20 == 0:
                log(f"  recorded {len(frames)} frames (pan {pan:.0f}°)")
            pan += step_deg
    finally:
        try:
            client.set_camera(0, 0)
        except Exception:
            pass
    return b"".join(frames)


def render_inventory(inv) -> str:
    """Compact, chat-friendly text of the scene inventory."""
    if not isinstance(inv, dict):
        return str(inv)
    out = []
    for v in inv.get("views") or []:
        objs = "; ".join(
            " ".join(x for x in (str(o.get("color") or "").strip(),
                                 str(o.get("name") or "").strip()) if x)
            + (f" ({o.get('details')})" if o.get("details") else "")
            for o in (v.get("objects") or []) if isinstance(o, dict))
        out.append(f"[{v.get('direction', '?')}] {objs or v.get('summary', '')}")
    if inv.get("overall"):
        out.append(f"overall: {inv['overall']}")
    if out:
        out.append(
            "note: directions are approximate and OVERLAP — \"behind you\" includes "
            "back-left, behind AND back-right; \"in front\" includes front-left, front "
            "AND front-right; the wide lens means one object often appears in several "
            "adjacent views. Check every relevant view (and object synonyms: bin = "
            "container = tub = box) before concluding something is not in the scene.")
    return "\n".join(out) or "(empty scene)"


def _ring_shift_px(imgs, step_deg=60):
    """Measured horizontal pixel shift for one pan step: the left edge of the
    NEXT frame appears somewhere in the PREVIOUS frame — template-match it.
    Returns the median across pairs (px per step), or None if unmeasurable."""
    import cv2
    import numpy as np
    shifts = []
    for a, b in zip(imgs, imgs[1:]):
        h, w = a.shape[:2]
        strip = slice(int(h * 0.30), int(h * 0.70))
        tmpl = b[strip, 0:w // 4]
        res = cv2.matchTemplate(a[strip, :], tmpl, cv2.TM_CCOEFF_NORMED)
        _, conf, _, loc = cv2.minMaxLoc(res)
        if conf > 0.5 and 4 <= loc[0] < 0.95 * w:   # dense sweeps shift only ~20px
            shifts.append(loc[0])
    if not shifts:
        return None
    shifts.sort()
    return float(shifts[len(shifts) // 2])


def _compose_known_pose(frames, out_w=4096):
    """Deterministic fallback panorama (used when feature-stitching fails or
    gates out): frames are placed at their KNOWN pan angles, scaled by the
    MEASURED degrees-per-pixel (template-matched overlap between adjacent
    frames), and feather-BLENDED — no duplicated content at seams. The two
    rings are blended vertically by their tilt offset."""
    import cv2
    import numpy as np
    rings = {}
    for pan, tilt, img in frames:
        if tier_label(tilt) == "ceiling":
            continue
        im = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
        if im is not None:
            rings.setdefault(round(float(tilt), 1), []).append((pan, im))
    if not rings:
        return None

    def build_band(ring, step_deg):
        ring = sorted(ring)
        imgs = [im for _, im in ring]
        h, w = imgs[0].shape[:2]
        shift = _ring_shift_px(imgs, step_deg) or w * step_deg / 130.0
        scale = (out_w * step_deg / 360.0) / shift    # measured px/deg → output px/deg
        sw, sh = int(w * scale), int(h * scale)
        # Keep only each frame's central share (its 60° + a thin blend margin):
        # blending across the lens's huge distorted overlaps causes ghosting —
        # thin, near-aligned slivers blend cleanly.
        step_px = int(out_w * step_deg / 360.0)
        keep_w = min(sw, int(step_px * 1.30))
        m = max(1, (keep_w - step_px) // 2 + 2)            # feather = the extra margin
        acc = np.zeros((sh, out_w, 3), np.float64)
        wgt = np.zeros((1, out_w, 1), np.float64) + 1e-6
        ramp = np.ones(keep_w, np.float64)
        ramp[:m] = np.linspace(0.02, 1, m)
        ramp[-m:] = np.linspace(1, 0.02, m)
        for pan, im in ring:
            seg = cv2.resize(im, (sw, sh), interpolation=cv2.INTER_AREA)
            c0 = (sw - keep_w) // 2
            seg = seg[:, c0:c0 + keep_w]
            xc = ((pan + 180.0) % 360.0) / 360.0 * out_w   # lon-aligned for the viewer
            x0 = int(xc - keep_w / 2)
            idx = (np.arange(keep_w) + x0) % out_w
            acc[:, idx] += seg * ramp[None, :, None]
            wgt[0, idx, 0] += ramp
        return (acc / wgt).astype(np.uint8), scale

    step = 60
    bands = []
    for tilt in sorted(rings, reverse=True):              # upper band first (top)
        band, scale = build_band(rings[tilt], step)
        bands.append((tilt, band, scale))
    if len(bands) == 1:
        pano = bands[0][1]
    else:
        # vertical placement from tilt difference at the measured px/deg
        px_per_deg = out_w / 360.0
        (t_hi, b_hi, _), (t_lo, b_lo, _) = bands[0], bands[1]
        vshift = int(abs(t_hi - t_lo) * px_per_deg)
        H = vshift + b_lo.shape[0]
        acc = np.zeros((H, out_w, 3), np.float64)
        wgt = np.zeros((H, 1, 1), np.float64) + 1e-6
        for y0, band in ((0, b_hi), (vshift, b_lo)):
            h = band.shape[0]
            r = np.ones(h, np.float64)
            fm = max(1, int(h * 0.15))
            r[:fm] = np.linspace(0.02, 1, fm)
            r[-fm:] = np.linspace(1, 0.02, fm)
            acc[y0:y0 + h] += band * r[:, None, None]
            wgt[y0:y0 + h, 0, 0] += r
        pano = (acc / wgt).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", pano, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return buf.tobytes() if ok else None


def _black_fraction(pano_bgr):
    import cv2
    g = cv2.cvtColor(pano_bgr, cv2.COLOR_BGR2GRAY)
    return float((g < 8).mean())


def save_scene(frames, inv, scenes_dir=SCENES_DIR, panorama=None):
    """Persist frames + inventory under scenes/<local timestamp>/; returns the dir."""
    d = os.path.join(scenes_dir, time.strftime("%Y%m%d_%H%M%S"))
    n = 1
    while os.path.exists(d):               # same-second scans don't overwrite
        d = os.path.join(scenes_dir, time.strftime("%Y%m%d_%H%M%S") + f"-{n}")
        n += 1
    os.makedirs(d, exist_ok=True)
    for pan, tilt, img in frames:
        with open(os.path.join(d, f"pan{int(pan):+04d}_t{int(tilt):+03d}.jpg"), "wb") as f:
            f.write(img)
    with open(os.path.join(d, "inventory.json"), "w") as f:
        json.dump(inv, f, indent=1)
    if panorama:
        with open(os.path.join(d, "panorama.jpg"), "wb") as f:
            f.write(panorama)
    return d


def load_latest_scene(scenes_dir=SCENES_DIR):
    """(dir, inventory) of the most recent saved scan, or (None, None)."""
    try:
        subs = sorted(os.listdir(scenes_dir), reverse=True)
    except OSError:
        return None, None
    for s in subs:
        p = os.path.join(scenes_dir, s, "inventory.json")
        if os.path.exists(p):
            try:
                with open(p) as f:
                    return os.path.join(scenes_dir, s), json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
    return None, None
