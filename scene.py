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
SCAN_TILT = -5.0          # mid view: floor through furniture height
SETTLE_S = 1.4
SCENES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenes")

SCENE_PROMPT = (
    "You are building a spatial memory for a small rover from {n} photos taken "
    "by rotating its camera in place — each photo is labeled with the direction "
    "it faces. List EVERYTHING recognizable in each view. Reply ONLY with JSON: "
    "{{\"views\": [{{\"direction\": str, \"objects\": [{{\"name\": str, "
    "\"color\": str, \"details\": str}}], \"summary\": str}}], "
    "\"overall\": str}}. Be specific about colors (incl. parts, e.g. a bin and "
    "its lid) and positions (left/right, floor/shelf). One views[] entry per "
    "photo, in the given order, using the given direction labels.")


def direction_label(pan: float) -> str:
    """Human direction for a gimbal pan (0=front, +right, ±180=behind)."""
    p = (float(pan) + 180.0) % 360.0 - 180.0
    names = [(-180, "behind"), (-135, "back-left"), (-90, "left"),
             (-45, "front-left"), (0, "front"), (45, "front-right"),
             (90, "right"), (135, "back-right"), (180, "behind")]
    best = min(names, key=lambda nv: abs(p - nv[0]))
    return best[1]


def scan_frames(client, *, pans=SCAN_PANS, tilt=SCAN_TILT, settle_s=SETTLE_S,
                sleep=time.sleep, log=lambda m: None):
    """Aim → settle → non-saving stream grab for each pan. Camera recentered
    after. Gimbal only — the wheels are never commanded."""
    frames = []
    try:
        for pan in pans:
            client.set_camera(pan, tilt)
            sleep(settle_s)
            img = client.get_stream_frame()
            frames.append((pan, img))
            log(f"  captured {direction_label(pan)} (pan {pan})")
    finally:
        try:
            client.set_camera(0, 0)        # leave the camera centered
        except Exception:
            pass
    return frames


def describe_scene(vision, frames, log=lambda m: None):
    """One multi-image vision call over all frames → inventory dict. Falls back
    to per-frame calls if the gateway rejects the multi-image payload."""
    labels = [f"View {i + 1} — facing {direction_label(p)} (pan {p}°)"
              for i, (p, _) in enumerate(frames)]
    prompt = SCENE_PROMPT.format(n=len(frames))
    try:
        return vision.describe_many(
            [(lab, img) for lab, (_, img) in zip(labels, frames)],
            prompt, json_out=True, max_tokens=1800)
    except Exception as e:
        log(f"  multi-image call failed ({e}); describing per view")
    views = []
    for lab, (pan, img) in zip(labels, frames):
        one = ("Describe this photo for a rover's spatial memory: it faces "
               f"{direction_label(pan)}. Reply ONLY with JSON: {{\"direction\": "
               f"\"{direction_label(pan)}\", \"objects\": [{{\"name\": str, "
               "\"color\": str, \"details\": str}], \"summary\": str}. Be "
               "specific about colors, including parts like lids.")
        try:
            views.append(vision.describe(img, one, json_out=True, max_tokens=500))
        except Exception as e:
            views.append({"direction": direction_label(pan), "objects": [],
                          "summary": f"(view failed: {e})"})
    return {"views": views, "overall": ""}


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


def save_scene(frames, inv, scenes_dir=SCENES_DIR):
    """Persist frames + inventory under scenes/<local timestamp>/; returns the dir."""
    d = os.path.join(scenes_dir, time.strftime("%Y%m%d_%H%M%S"))
    n = 1
    while os.path.exists(d):               # same-second scans don't overwrite
        d = os.path.join(scenes_dir, time.strftime("%Y%m%d_%H%M%S") + f"-{n}")
        n += 1
    os.makedirs(d, exist_ok=True)
    for pan, img in frames:
        with open(os.path.join(d, f"pan{int(pan):+04d}.jpg"), "wb") as f:
            f.write(img)
    with open(os.path.join(d, "inventory.json"), "w") as f:
        json.dump(inv, f, indent=1)
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
