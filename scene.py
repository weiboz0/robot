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


def _describe_chunk(vision, chunk, log):
    """One multi-image call for ≤DESCRIBE_CHUNK frames; per-frame fallback."""
    labels = [f"View {i + 1} — facing {view_label(p, t)}"
              for i, (p, t, _) in enumerate(chunk)]
    prompt = SCENE_PROMPT.format(n=len(chunk))
    try:
        out = vision.describe_many(
            [(lab, img) for lab, (_, _, img) in zip(labels, chunk)],
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
            views.append(vision.describe(img, one, json_out=True, max_tokens=900))
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


def build_panorama(frames, max_width=2400):
    """Stitch the ring frames (eye-level + upper; the ceiling frame's geometry
    doesn't overlap usefully) into one 360° panorama — the scan's "3D space".
    Returns JPEG bytes, or None if OpenCV is missing or stitching fails."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    imgs = [cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
            for pan, tilt, img in frames if tier_label(tilt) != "ceiling"]
    imgs = [i for i in imgs if i is not None]
    if len(imgs) < 3:
        return None
    try:
        st = cv2.Stitcher.create(cv2.Stitcher_PANORAMA)
        status, pano = st.stitch(imgs)
    except cv2.error:                      # stitcher can throw, not just non-OK
        return None
    if status != cv2.Stitcher_OK or pano is None:
        return None
    h, w = pano.shape[:2]
    if w > max_width:
        pano = cv2.resize(pano, (max_width, int(h * max_width / w)),
                          interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", pano, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return buf.tobytes() if ok else None


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
