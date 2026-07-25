#!/usr/bin/env python3
"""Unified natural-language chatbot for the rover AND the Dobot MG400.

Same file runs on the rover's Raspberry Pi or on a computer — it auto-detects
what it can reach and only offers tools for available robots:

  Rover : direct serial if a serial port exists (the Pi), else the rover's HTTP
          API if reachable (a computer), else disabled.
  Dobot : TCP-IP (192.168.1.6) if reachable, else disabled.

Chat in plain English ("look up, then move the arm to x300 y0 z50"); or prefix a
line with $ for a direct command:
  $up 45 / $cam 0 30 / $drive 0.2 0.2 1 / $stop      (rover)
  $dobot GetPose() / $dobot EnableRobot()            (raw Dobot)
  $help / quit

Setup (any machine):
  pip install -r requirements.txt
  echo 'OPENCODE_API_KEY=sk-...' >> .env      # or ~/.env
  python agent_chat.py
"""
import json
import os
import re
import sys
import time

import dobot

from rover_backend import RoverCtl, detect_rover
from llm_config import load_dotenv

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "").strip() or "https://opencode.ai/zen/go/v1"
MODEL = os.environ.get("OPENCODE_MODEL", "").strip() or "minimax-m3"


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    return _THINK.sub("", text or "").strip()


# ----------------------------------------------------- rover $-command parser
# Website-vocabulary aliases (plan 019 parity): each maps to the chatbot-native
# command it behaves as. drive/fwd/back are deliberately NOT remapped — the same
# words have different units on the website (see docs/reference).
CMD_ALIASES = {
    "camera_up": "up", "camera_down": "down",
    "camera_left": "left", "camera_right": "right",
    "camera_aim": "cam", "camera_center": "center",
    "snapshot": "photo", "snap": "photo",
    "gimbal_relax": "relax", "gimbal_lock": "lock",
}


def rover_command(r: RoverCtl, line: str) -> str:
    parts = line.split()
    if not parts:
        return ""
    c, args = parts[0].lower(), parts[1:]
    c = CMD_ALIASES.get(c, c)
    try:
        if c in ("up", "down", "left", "right"):
            step = float(args[0]) if args else 15.0
            pan, tilt = r.pan, r.tilt
            tilt += step if c == "up" else -step if c == "down" else 0
            pan += -step if c == "left" else step if c == "right" else 0
            r.set_camera(pan, tilt)
            return f"camera -> pan={r.pan}, tilt={r.tilt}"
        if c == "cam":
            r.set_camera(args[0], args[1])
            return f"camera -> pan={r.pan}, tilt={r.tilt}"
        if c == "center":
            r.center(); return "camera centered"
        if c == "drive":
            r.drive(args[0], args[1], args[2] if len(args) > 2 else 1.0); return "drove, stopped"
        if c == "fwd":
            r.drive(0.2, 0.2, args[0] if args else 1.0); return "forward"
        if c == "back":
            r.drive(-0.2, -0.2, args[0] if args else 1.0); return "back"
        if c == "spinl":
            r.drive(-0.2, 0.2, args[0] if args else 0.6); return "spin left"
        if c == "spinr":
            r.drive(0.2, -0.2, args[0] if args else 0.6); return "spin right"
        if c == "move":
            r.move(args[0], args[1]); return "moving (use stop)"
        if c == "stop":
            r.stop(); return "stopped"
        if c == "estop":
            r.estop(); return "emergency stop (wheels + gimbal)"
        if c == "light":
            front = float(args[0]) if args else 0
            base = float(args[1]) if len(args) > 1 else 0
            f, b = r.lights(front, base); return f"lights -> front={f}, base={b}"
        if c in ("relax", "lock"):
            r.set_torque(c == "lock"); return "gimbal locked" if c == "lock" else "gimbal relaxed"
        if c == "oled":
            r.oled(args[0], " ".join(args[1:])); return f"oled line {args[0]} set"
        if c == "oledclear":
            r.oled_default(); return "oled restored"
        if c == "demo":
            r.demo(); return "demo done"
        if c == "photo":
            p = r.photo(); return f"photo saved -> {p}" if p else "photo failed (camera busy?)"
        if c == "speed":
            if args:
                return f"speed cap -> {r.set_speed(float(args[0]))}"
            return f"speed cap = {r.get_speed()} (max wheel magnitude 0..0.5)"
        if c == "status":
            return json.dumps(r.status())
        if c == "photos":
            ps = r.list_photos()
            return f"{len(ps)} photo(s): {', '.join(ps[:20])}" if ps else "no photos yet"
        if c in ("move_forward", "move_back", "move_left", "move_right"):
            ms = float(args[0]) if args else 400
            r.nudge(c[5:], ms)
            return f"nudged {c[5:]} ({int(min(5000, max(0, ms)))} ms)"
        if c in ("light_head", "light_base"):
            on = None                       # no arg = toggle
            if args:
                s = args[0].lower()
                if s in ("on", "1", "true"):
                    on = True
                elif s in ("off", "0", "false"):
                    on = False
                else:
                    return f"{c} arg must be on|off"
            state = r.light_channel(c[6:], on)
            return f"{c[6:]} light {'on' if state else 'off'}"
        return f"?? unknown rover command '{c}'"
    except (IndexError, ValueError):
        return "bad args"
    except NotImplementedError as e:
        return str(e)                       # e.g. OLED via the rovercontrol backend
    except Exception as e:                  # a backend/network error must not kill the REPL
        return f"error: {e}"


# ------------------------------------------------- autonomous vision-find ($find)
# One-word test shortcuts for $find — stop-and-photograph when the target is seen.
FIND_SHORTCUTS = {
    "screwdriver": "a screwdriver",
    "pen": "a green pen",   # color named → the local CV detector handles it (plan 021)
}


def autonomous_find(rover, target):
    """$find <obj> / $screwdriver: autonomously find `target` (camera sweep +
    in-place rotation only — no forward driving) and return an outlined photo.
    Gated: vision must be configured AND ROVER_FIND_ENABLE=1, checked BEFORE any
    rover call, so an unset run is a pure no-op that never moves the rover."""
    if os.environ.get("ROVER_FIND_ENABLE", "").strip().lower() not in ("1", "true", "yes"):
        return ("autonomous find is DISABLED — it moves the camera and rotates the rover "
                "in place on its own. To allow it: set ROVER_FIND_ENABLE=1.")
    if rover is None or rover.backend != "rovercontrol":
        return ("autonomous find needs the rovercontrol backend (run the Go controller "
                "and let the chatbot drive it over :8080 — not the serial/app.py path).")
    import autodrive
    import rovercontrol_client as client   # already pointed at the rover by RoverCtl
    import detector as _detector
    # Target detection: local CV when the target names a color and OpenCV is
    # available (milliseconds, no gateway, no key needed). Colorless targets fall
    # back to the vision LLM with a smaller sweep (each look costs 20-45s).
    # The shape prior comes from the object word (pen=elongated, cup=compact).
    look = None
    det_kind = "llm"
    sweep = {}
    try:
        color = _detector.color_for_target(target)
        shape = _detector.shape_for_target(target)
        if color and _detector.available():
            look = lambda name, img: autodrive.obs_from_detection(
                _detector.detect_color_object(img, color, shape), color)
            det_kind = f"cv:{color}:{shape}"
    except Exception:
        pass
    vm = None
    if look is None:                       # LLM path: vision FIRST — no rover contact if unavailable
        try:
            import vision as _vision
            vm = _vision.VisionModel(timeout=45)
        except Exception as e:
            return (f"vision not available for a colorless target ({e}) — name the "
                    "object's color (e.g. 'a green pen') to use local detection.")
        sweep = {"sweep_pans": (-30, 0, 30), "sweep_tilts": (-20,), "max_rotations": 1}
    def capture():
        # observe from the live stream — nothing saved; only the found photo is
        # snapshotted (finish/snap), so runs don't fill the gallery
        return None, client.get_stream_frame()
    label = _detector.label_for_target(target)   # e.g. "green pen" — shown at the outline
    found_obs = {}
    def on_found(name, obs):                # store the bbox so the gallery can outline it
        found_obs.update(obs)
        if obs.get("bbox"):
            client.set_photo_meta(name, {
                "target": target, "label": label,
                "color": str(obs.get("color") or ""),
                "bbox": obs["bbox"], "confidence": float(obs.get("confidence") or 0)})
    print(f"   detector: {det_kind}")
    driver = autodrive.SafeDriver(client)
    try:
        shot = autodrive.find_object(driver, vm, target, capture=capture,
                                     log=lambda m: print("   " + m),
                                     on_found=on_found, look=look,
                                     snap=client.snapshot, **sweep)
    except Exception as e:                  # rover is left stopped/safe (preflight refusal or cleanup)
        return f"find aborted — rover stopped/safe: {e}"
    if shot:
        color = found_obs.get("color") or "?"
        hint = ("outline: press ◻ on the photo in the gallery"
                if found_obs.get("bbox") else "no outline data (model gave no bbox)")
        return (f"found {target} (color: {color}) → {shot}   "
                f"({rover.where}/photos/{shot} — {hint})")
    return f"did not find {target} within the budget — wheels stopped."


# ------------------------------------------------- 360° scene scan (plan 022)
LAST_SCENE = {"text": None, "when": None}


def scan_surroundings(rover):
    """Camera-only 360° scan → direction-labeled inventory (the 'map'). Returns
    the full inventory text so follow-up questions are answered from context."""
    if rover is None or rover.backend != "rovercontrol":
        return "scene scan needs the rovercontrol backend (:8080)."
    try:
        import vision as _vision
        vm = _vision.VisionModel(timeout=90)   # one big multi-image call
    except Exception as e:
        return f"scene scan needs a vision model: {e}"
    import scene
    import rovercontrol_client as client
    def status(st):
        try:
            client.set_pano_status(st)     # website badge; best-effort
        except Exception:
            pass
    print("   scanning 360° (camera only — wheels untouched)...")
    status("scanning")
    frames = scene.scan_frames(client, log=lambda m: print("   " + m))
    print("   describing the scene (one vision call over all views)...")
    status("describing")
    inv = scene.describe_scene(vm, frames, log=lambda m: print("   " + m))
    # 3D space: seam-cut merge of the scan stills (each pixel from exactly ONE
    # photo — no averaging ghosts). The dense-sweep slit-scan path is retired
    # from the default scan: it AVERAGED overlapping strips, which multiplied
    # near-field objects (the "5 jackets" bug) and added ~80s of capture.
    print("   building the 3D space (seam-cut merge)...")
    status("stitching")
    pano = scene.build_panorama(frames)
    pano_note = "no 3D space (stitch failed)"
    if pano:
        try:
            status("uploading")
            client.set_panorama(pano)
            pano_note = "3D space saved — press 🌐 3D view on the website to look around"
        except Exception as e:
            pano_note = f"3D space stitched but upload failed: {e}"
    status("done" if pano and "saved" in pano_note else "failed")
    d = scene.save_scene(frames, inv, panorama=pano)
    text = scene.render_inventory(inv)
    LAST_SCENE.update(text=text, when=time.strftime("%H:%M"))
    print(f"   {pano_note}")
    return (f"SCENE MEMORY (360° scan, saved to {os.path.basename(d)}; {pano_note}) — "
            f"answer questions about the surroundings from this, do NOT re-scan "
            f"unless the room changed:\n{text}")


def recall_scene():
    if LAST_SCENE["text"]:
        return f"scene memory from {LAST_SCENE['when']}:\n{LAST_SCENE['text']}"
    import scene
    d, inv = scene.load_latest_scene()
    if inv is None:
        return "no scene memory yet — run a scan first (rover_scan_surroundings / $scan)."
    return f"scene memory (loaded from {os.path.basename(d)}):\n{scene.render_inventory(inv)}"


def pano_compare(rover):
    """Build the panorama with every merge method from the LATEST saved scan
    frames (no camera use) and publish each to the 3D viewer's method buttons."""
    if rover is None or rover.backend != "rovercontrol":
        return "panorama test needs the rovercontrol backend (:8080)."
    import glob as _glob
    import os as _os
    import scene
    import rovercontrol_client as client
    d, _ = scene.load_latest_scene()
    if not d:
        return "no saved scan yet — run $scan first."
    frames = []
    for f in sorted(_glob.glob(_os.path.join(d, "pan*_t*.jpg"))):
        n = _os.path.basename(f)
        try:
            frames.append((int(n[3:7]), int(n[9:12]), open(f, "rb").read()))
        except (ValueError, OSError):
            continue
    if len(frames) < 3:
        return f"scan {_os.path.basename(d)} has no usable frames."
    import time as _t
    lines = []
    best = None
    for name, fn in (("seamcut", scene._seamcut_pano),
                     ("projector", lambda fr: scene.build_panorama(fr, try_stitcher=False)),
                     ("stitcher", scene._stitcher_pano)):
        t0 = _t.time()
        try:
            pano = fn(frames)
        except Exception as e:
            pano = None
            lines.append(f"{name}: failed ({e})")
            continue
        if pano:
            client.set_pano_variant(name, pano)
            if best is None:
                best = pano
            lines.append(f"{name}: {len(pano) // 1024}KB in {_t.time() - t0:.0f}s")
        else:
            lines.append(f"{name}: no result (gated out or failed)")
    if best:
        client.set_panorama(best)
    return ("merge comparison ready — open 🌐 3D view and use the method buttons "
            f"(from scan {_os.path.basename(d)}):\n" + "\n".join("  " + l for l in lines))


def detect_compare(rover):
    """Run all detection models on the current camera view and publish each
    model's annotated frame to the website's 🧪 Detectors panel."""
    if rover is None or rover.backend != "rovercontrol":
        return "detector test needs the rovercontrol backend (:8080)."
    import detectors
    import rovercontrol_client as client
    jpg = client.get_stream_frame()
    lines = []
    for m in detectors.MODELS:
        if not detectors.available(m):
            lines.append(f"{m}: unavailable (pip install ultralytics)")
            continue
        try:
            import time as _t
            t0 = _t.time()
            vis, dets = detectors.run(m, jpg)
            client.set_det_image(m, vis)
            found = ", ".join(f"{d['label']}:{d['conf']}" for d in dets[:8]) or "nothing"
            lines.append(f"{m}: {len(dets)} object(s) in {_t.time()-t0:.1f}s — {found}")
        except Exception as e:
            lines.append(f"{m}: failed ({e})")
    return ("detector comparison done — press 🧪 Detectors on the website and flip "
            "between models:\n" + "\n".join("  " + l for l in lines))


def record_room(rover):
    """Camera-only 360° video tour — smooth by construction (no stitching)."""
    if rover is None or rover.backend != "rovercontrol":
        return "room recording needs the rovercontrol backend (:8080)."
    import scene
    import rovercontrol_client as client
    def status(st):
        try:
            client.set_pano_status(st)
        except Exception:
            pass
    print("   recording a 360° tour (camera only — wheels untouched)...")
    status("recording")
    try:
        blob = scene.record_tour(client, log=lambda m: print("   " + m))
        status("uploading")
        client.set_tour(blob)
        status("done")
        return ("room tour recorded — press ▶ Room tour on the website to watch it "
                f"({len(blob) // 1024} KB). It is real video of the sweep, so it has "
                "no stitching seams at all.")
    except Exception as e:
        status("failed")
        return f"recording failed: {e}"


# --------------------------------------------------------------------- tools
def build_tools(rover, arm):
    tools = []
    if rover is not None:
        tools += [
            {"name": "rover_set_camera",
             "description": "Aim the rover camera to absolute angles. pan -180..180, tilt -45..90 (+up).",
             "parameters": {"type": "object", "properties": {
                 "pan": {"type": "number"}, "tilt": {"type": "number"}}, "required": ["pan", "tilt"]}},
            {"name": "rover_drive",
             "description": "Drive the rover then auto-stop. left/right wheel speeds -0.5..0.5, seconds 0..5.",
             "parameters": {"type": "object", "properties": {
                 "left": {"type": "number"}, "right": {"type": "number"}, "seconds": {"type": "number"}},
                 "required": ["left", "right", "seconds"]}},
            {"name": "rover_stop", "description": "Stop the rover wheels.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_estop", "description": "Emergency stop: halt rover wheels AND gimbal immediately.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_lights",
             "description": "Set rover LED brightness (PWM 0..255). front=head light, base=chassis light. 0 = off.",
             "parameters": {"type": "object", "properties": {
                 "front": {"type": "number"}, "base": {"type": "number"}}, "required": ["front", "base"]}},
            {"name": "rover_oled",
             "description": "Write text to the rover's OLED screen line (0-3). Omit/empty to restore default screen.",
             "parameters": {"type": "object", "properties": {
                 "line": {"type": "number"}, "text": {"type": "string"}}, "required": ["line", "text"]}},
            {"name": "rover_photo",
             "description": "Take a photo with the rover's camera. Returns the saved image file path.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_center_camera", "description": "Re-center the rover camera (level, facing forward).",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_gimbal_torque",
             "description": "Lock or relax the camera gimbal servos. lock=false relaxes them so the "
                            "camera can be hand-positioned; lock=true holds position.",
             "parameters": {"type": "object", "properties": {
                 "lock": {"type": "boolean"}}, "required": ["lock"]}},
            {"name": "rover_set_speed",
             "description": "Set the rover speed cap = max wheel magnitude, 0..0.5 (lower = slower). "
                            "This is the safe way to slow ALL driving. On the Go controller this cap "
                            "is shared with the gamepad, so it is not exclusively yours.",
             "parameters": {"type": "object", "properties": {
                 "cap": {"type": "number"}}, "required": ["cap"]}},
            {"name": "rover_get_status",
             "description": "Get rover status: which backend, and whether serial/camera/gamepad are up, "
                            "plus the current speed cap.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_list_photos",
             "description": "List photo filenames taken by the rover camera, newest first.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_scan_surroundings",
             "description": "Build a 360° spatial memory: the camera sweeps a full circle "
                            "(wheels never move) and every direction is inventoried (objects, "
                            "colors, positions). AFTER a scan, answer questions about the "
                            "surroundings from the returned inventory — do NOT re-scan or look "
                            "again unless the user says the room changed.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_test_panorama_merges",
             "description": "Rebuild the 3D-view panorama with every merge method (seamcut / "
                            "projector / stitcher) from the latest saved scan and publish them to "
                            "the 3D viewer's method buttons for side-by-side comparison. No camera "
                            "or motion use.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_test_detectors",
             "description": "Run all object-detection models (color-blob + YOLO variants) on the "
                            "current camera view and publish annotated results to the website's "
                            "🧪 Detectors panel for side-by-side comparison.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_record_tour",
             "description": "Record a smooth 360° VIDEO tour of the room (the camera sweeps a "
                            "full circle; wheels never move) and publish it to the website's "
                            "▶ Room tour player. Use when the user asks to record the room / "
                            "make a video tour.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_scene_recall",
             "description": "Recall the most recent 360° scene memory (use when asked about "
                            "the surroundings and no scan result is in the conversation).",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "rover_identify_scan",
             "description": "Identify objects in a SAVED 3D scan and add "
                            "labeled boxes to its viewer. which=1 is the "
                            "NEWEST scan, which=2 the second-newest (so 'the "
                            "2nd last 3D view' means which=2), and so on. "
                            "Optional focus names something specific to look "
                            "for (e.g. 'stack of books'). Takes a few "
                            "minutes; returns the list of boxed objects.",
             "parameters": {"type": "object", "properties": {
                 "which": {"type": "integer"},
                 "focus": {"type": "string"}}, "required": ["which"]}},
            {"name": "rover_where_is",
             "description": "Where was an object last seen? Searches the "
                            "object memory built from all saved 3D scans and "
                            "answers with which scan saw it, from where, and "
                            "how far to turn from the rover's CURRENT heading "
                            "to face it. Never moves the rover. Use for "
                            "'where is the suitcase?'-style questions about "
                            "things seen before.",
             "parameters": {"type": "object", "properties": {
                 "name": {"type": "string"}}, "required": ["name"]}},
            {"name": "rover_find_object",
             "description": "Physically search for an object: the rover autonomously scans, "
                            "drives toward it in small safe steps, stops when found, and returns "
                            "a photo with the object outlined. Describe the object with its COLOR "
                            "(e.g. 'a green pen', 'a yellow note') — color-named targets use fast "
                            "local detection. Requires ROVER_FIND_ENABLE=1; refuses safely otherwise. "
                            "Use ONLY when the user asks to find/look for/locate a physical object.",
             "parameters": {"type": "object", "properties": {
                 "target": {"type": "string"}}, "required": ["target"]}},
        ]
    if arm is not None:
        tools += [
            {"name": "dobot_get_pose", "description": "Get the Dobot's current Cartesian pose (x,y,z,r).",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "dobot_get_mode", "description": "Get the Dobot robot mode.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "dobot_enable", "description": "Enable (power) the Dobot's motors.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "dobot_clear_error", "description": "Clear the Dobot's error state.",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "dobot_move",
             "description": "Move the Dobot to a Cartesian pose (joint move). x,y,z in mm, r in degrees.",
             "parameters": {"type": "object", "properties": {
                 "x": {"type": "number"}, "y": {"type": "number"},
                 "z": {"type": "number"}, "r": {"type": "number"}},
                 "required": ["x", "y", "z", "r"]}},
        ]
    return [{"type": "function", "function": t} for t in tools]


def relative_turn(bearing, heading):
    """Human turn phrase (plan 033): both angles CCW-positive, so a positive
    delta means the target is to the LEFT of the current heading."""
    d = (bearing - heading + 180.0) % 360.0 - 180.0
    if abs(d) <= 10:
        return "roughly ahead"
    return f"~{abs(round(d))}° {'left' if d > 0 else 'right'}"


def _match_objects(objs, query):
    """Tiered fuzzy match over object sightings: exact name → substring
    (queries ≥4 chars only — 'car' must not ghost-match 'cardboard') →
    shared word ≥4 chars. First tier with hits wins; order preserved
    (newest scan first)."""
    q = query.strip().lower()
    named = [(o, str(o.get("name", "")).strip().lower()) for o in objs]
    exact = [o for o, n in named if n == q]
    if exact:
        return exact
    if len(q) >= 4:
        sub = [o for o, n in named if q in n or (len(n) >= 4 and n in q)]
        if sub:
            return sub
    qw = {w for w in q.split() if len(w) >= 4}
    return [o for o, n in named
            if qw & {w for w in n.split() if len(w) >= 4}]


def run_tool(rover, arm, name, a):
    try:
        if name == "rover_set_camera":
            rover.set_camera(a.get("pan", 0), a.get("tilt", 0)); return f"camera pan={rover.pan} tilt={rover.tilt}"
        if name == "rover_drive":
            rover.drive(a["left"], a["right"], a.get("seconds", 1.0)); return "drove, stopped"
        if name == "rover_stop":
            rover.stop(); return "stopped"
        if name == "rover_estop":
            rover.estop(); return "emergency stopped"
        if name == "rover_lights":
            f, b = rover.lights(a.get("front", 0), a.get("base", 0)); return f"lights front={f} base={b}"
        if name == "rover_oled":
            txt = a.get("text", "")
            if txt == "":
                rover.oled_default(); return "oled restored"
            rover.oled(a.get("line", 0), txt); return "oled updated"
        if name == "rover_photo":
            p, flashed, restore_failed = photo_with_autoflash(rover)
            note = " (dark — used the flashlight)" if flashed else ""
            if restore_failed:
                note += "; note: the lights may still be on (restore failed)"
            return f"photo saved to {p}{note}" if p else f"photo failed (camera busy?){note}"
        if name == "rover_identify_scan":
            which = int(a.get("which", 1))
            focus = (a.get("focus") or "").strip() or None
            try:
                names = rover.list_scans()
            except Exception as e:
                return f"cannot reach saved scans: {e}"
            if not names:
                return "no saved 3D scans yet — run a scan first"
            if not 1 <= which <= len(names):
                return f"which={which} is out of range — there are {len(names)} saved scans"
            name_ = names[which - 1]
            before = (rover.scan_meta(name_) or {}).get("made")
            try:
                rover.identify_scan(name_, focus)
            except Exception as e:
                return f"identify refused: {e}"
            deadline = time.time() + 360           # holds this chat turn; accepted
            while time.time() < deadline:
                time.sleep(5)
                meta = rover.scan_meta(name_)
                if meta and meta.get("made") != before:
                    found = sorted({o["name"] for o in meta.get("objects", [])})
                    return (f"added boxes to {name_}: " + ", ".join(found)
                            + " — open it in the 3D views tab")
            return (f"identification of {name_} is still running (or found "
                    "nothing new) — check the 3D views tab in a minute")
        if name == "rover_where_is":
            query = str(a.get("name", "")).strip()
            if not query:
                return "rover_where_is needs an object name"
            try:
                objs = rover.get_objects()
            except Exception as e:
                return f"cannot reach the object memory: {e}"
            if not objs:
                return ("the object memory is empty — only scans made after "
                        "pose tracking record places; run a scan, then ask "
                        "again")
            matches = _match_objects(objs, query)
            if not matches:
                return (f"no memory of a '{query}' in any saved 3D view — "
                        "try another name, run a new scan, or identify a "
                        "saved view with rover_identify_scan")
            best = matches[0]
            scans_seen = list(dict.fromkeys(o["scan"] for o in objs))
            nth = scans_seen.index(best["scan"]) + 1
            p = best["pose"]
            out = (f"'{best['name']}' was seen in 3D view #{nth} (newest=1; "
                   f"{best['scan']}, made {best.get('made') or '?'}) from "
                   f"position ({p['x']:.2f}, {p['y']:.2f}); it lies toward "
                   f"world bearing {best['bearing']:.0f}°")
            try:
                cur = rover.get_pose()
            except Exception:
                cur = None
            if cur and cur.get("fresh") and isinstance(
                    cur.get("heading"), (int, float)):
                out += (f" — from the current heading turn "
                        f"{relative_turn(best['bearing'], cur['heading'])} "
                        "to face it")
            else:
                out += " (current heading unknown — telemetry stale)"
            others = matches[1:]
            if others:
                same = sum(1 for m in others if m["scan"] == best["scan"])
                older = len(others) - same
                extra = ([f"{same} more in the same view"] if same else []) \
                    + ([f"{older} in older views"] if older else [])
                out += " (also: " + ", ".join(extra) + ")"
            return out
        if name == "rover_center_camera":
            rover.center(); return "camera centered"
        if name == "rover_gimbal_torque":
            lock = bool(a.get("lock", True)); rover.set_torque(lock)
            return "gimbal locked" if lock else "gimbal relaxed"
        if name == "rover_set_speed":
            cap = a.get("cap")
            if cap is None:
                return "rover_set_speed needs cap (0..0.5)"
            return f"speed cap set to {rover.set_speed(cap)}"
        if name == "rover_get_status":
            return json.dumps(rover.status())
        if name == "rover_list_photos":
            ps = rover.list_photos()
            return f"{len(ps)} photo(s): {', '.join(ps[:20])}" if ps else "no photos yet"
        if name == "rover_find_object":
            t = (a.get("target") or "").strip()
            if not t:
                return "rover_find_object needs a target description"
            return autonomous_find(rover, t)
        if name == "rover_scan_surroundings":
            return scan_surroundings(rover)
        if name == "rover_record_tour":
            return record_room(rover)
        if name == "rover_test_detectors":
            return detect_compare(rover)
        if name == "rover_test_panorama_merges":
            return pano_compare(rover)
        if name == "rover_scene_recall":
            return recall_scene()
        if name == "dobot_get_pose":
            return arm.pose()
        if name == "dobot_get_mode":
            return arm.mode()
        if name == "dobot_enable":
            return arm.enable()
        if name == "dobot_clear_error":
            return arm.clear_error()
        if name == "dobot_move":
            return arm.move_j(a["x"], a["y"], a["z"], a["r"])
        return f"unknown tool {name}"
    except Exception as e:
        return f"tool error: {e}"


SYSTEM = (
    "You are an assistant that controls robots over a network/serial bus. You may "
    "have a Waveshare UGV rover (tank wheels + pan/tilt camera) and/or a Dobot MG400 "
    "robotic arm, depending on which tools are provided — only use tools that exist. "
    "Keep actions small and safe unless told otherwise. Rover: tilt + is up; wheel "
    "speeds small (<=0.3); lights are PWM 0..255 (front=head, base=chassis, 0=off); "
    "you can take a photo with the camera, list photos, center the camera, and "
    "lock/relax the gimbal. The speed cap (rover_set_speed, 0..0.5) is the safe "
    "way to slow all driving; on the Go controller it is shared with the gamepad. "
    "Use rover_get_status to check what is connected. When the user asks you to "
    "find/look for a physical object, use rover_find_object with a color in the "
    "description (e.g. 'a green pen') — it drives itself and returns an outlined "
    "photo; report the result including the photo name. rover_record_tour records a smooth "
    "360-degree video tour for the website. rover_scan_surroundings "
    "builds a 360-degree memory of the room; AFTER a scan, answer questions about "
    "the surroundings (colors, what is where, what is behind you) from the scan "
    "text already in the conversation — never re-scan or move the camera for "
    "them; use rover_scene_recall if the scan text is no longer in context. "
    "SELF-CHECK before answering any surroundings question: (1) directions "
    "overlap — 'behind' means back-left AND behind AND back-right; 'in front' "
    "means front-left AND front AND front-right; re-read ALL views that could "
    "match, not just the one with the exact label; (2) match object synonyms "
    "(bin/container/tub/box, couch/sofa, note/sticky note); (3) quote the "
    "inventory line you used; (4) only say something is not visible after "
    "checking EVERY view. "
    "Dobot: coordinates are mm (x,y,z) and degrees (r); move "
    "conservatively and enable the arm before moving. After acting, briefly say what "
    "you did. Be concise."
)


MAX_HISTORY = 24    # cap recent messages kept (the system message is always kept)


def trim_history(messages, limit=MAX_HISTORY):
    """Keep the system message + the most recent <=limit messages, snapping the
    kept window to start on a 'user' message so a tool reply is never separated
    from its assistant.tool_calls (some providers reject an orphaned tool msg).
    Mutates and returns `messages`. Call at a user-turn boundary, not mid-loop."""
    if len(messages) <= limit + 1:
        return messages
    tail = messages[-limit:]
    for i, m in enumerate(tail):        # advance to the first user msg in the window
        if m.get("role") == "user":
            tail = tail[i:]
            break
    else:
        tail = []
    messages[:] = messages[:1] + tail
    return messages


DARK_MEAN = 55.0            # 0..255 mean luma; below this the flash kicks in


def _frame_mean(jpeg):
    """Mean luma of a JPEG, or None (no cv2 / undecodable) — a None must
    always mean 'do not touch the lights'."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    return float(img.mean()) if img is not None else None


def photo_with_autoflash(rover):
    """Take a photo; if the scene is dark AND the kill switch allows it AND
    the prior light state is readable, flash the lights and restore the
    EXACT prior state afterwards (finally-guaranteed). Returns
    (path, flashed, restore_failed)."""
    flashed = False
    prior = None
    try:
        prior_state = rover.light_state()          # None → unknowable → skip
        if prior_state is not None and rover.auto_flash_allowed():
            frame = rover.get_stream_frame()
            mean = _frame_mean(frame)
            if mean is not None and mean < DARK_MEAN:
                prior = prior_state
                rover.lights(255, 255)
                flashed = True
                time.sleep(0.8)                    # let auto-exposure settle
    except Exception:
        flashed, prior = False, None               # measuring never blocks a photo
    restore_failed = False
    try:
        path = rover.photo()
    finally:
        if flashed:
            try:
                rover.lights(255 if prior.get("head") else 0,
                             255 if prior.get("base") else 0)
            except Exception:
                restore_failed = True
    return path, flashed, restore_failed


HELP_TEXT = (
    "rover camera: up/down/left/right [deg], cam P T, center, relax, lock\n"
    "rover motors: drive L R [s], move L R, fwd/back/spinl/spinr [s], stop, estop\n"
    "rover extras: light FRONT BASE (0-255), oled LINE TEXT, oledclear, demo, photo\n"
    "rover meta:   speed [CAP 0..0.5], status, photos\n"
    "website names also work: camera_up/..., camera_aim, camera_center, snapshot,\n"
    "  gimbal_relax/lock, light_head|light_base [on|off], move_forward/back/left/right [MS]\n"
    "  (note: drive/fwd/back keep CHATBOT units here — seconds, speeds -0.5..0.5)\n"
    "autonomous:   find <object> / screwdriver / pen  (drives itself, stops +\n"
    "  photographs when it sees the target; needs ROVER_FIND_ENABLE=1 + vision key)\n"
    "memory:       ask 'where is the <object>?' — recalls it from saved 3D\n"
    "  scans and points relative to the current heading (never moves)\n"
    "dobot: $dobot <raw cmd>  e.g. $dobot GetPose() / $dobot EnableRobot()")


def dollar_command(rover, arm, cmd):
    """One $-command → its output text (the REPL's former inline dispatch)."""
    if cmd in ("help", ""):
        return HELP_TEXT
    if cmd.lower().startswith("dobot"):
        raw = cmd[5:].strip()
        if arm is None:
            return "dobot not connected"
        try:                    # a socket error must not kill the session
            return str(arm.motion(raw) if raw.startswith("Mov") else arm.dashboard(raw))
        except Exception as e:
            return f"dobot error: {e}"
    if cmd.lower() == "scan":
        return str(scan_surroundings(rover))
    if cmd.lower() == "record":
        return str(record_room(rover))
    if cmd.lower() == "detect":
        return str(detect_compare(rover))
    if cmd.lower() == "panotest":
        return str(pano_compare(rover))
    if cmd.lower() in FIND_SHORTCUTS or cmd.lower().startswith("find "):
        target = FIND_SHORTCUTS.get(cmd.lower()) or cmd[5:].strip()
        return str(autonomous_find(rover, target))
    if rover is not None:
        return str(rover_command(rover, cmd))
    return "rover not connected"


class ChatSession:
    """One conversation: the REPL's turn logic, callable from anywhere.

    handle(text) runs a FULL turn ($-dispatch or the LLM tool loop) and
    returns the transcript text; `live(kind, text)` receives the same events
    as they happen so the terminal REPL can keep its original formatting
    (incl. its spinner — which is deliberately NOT part of the returned
    transcript: \\r control hacks would render as garbage in a browser).
    Error turns RETURN their message; they never raise. A lock serializes
    turns — one conversation, exactly like the REPL."""

    def __init__(self, rover, arm, client=None, tools=None):
        import threading
        self.rover, self.arm, self.client = rover, arm, client
        self.tools = tools if tools is not None else build_tools(rover, arm)
        self.messages = [{"role": "system", "content": SYSTEM}]
        self.lock = threading.Lock()

    def handle(self, user, live=lambda kind, text: None):
        user = (user or "").strip()
        out = []

        def emit(kind, text):
            out.append(text)
            live(kind, text)
        with self.lock:
            if not user:
                return ""
            if user.startswith("$"):
                cmd = user[1:].strip()
                kind = "help" if cmd in ("help", "") else "dollar"
                emit(kind, dollar_command(self.rover, self.arm, cmd))
                return "\n".join(out)
            if self.client is None:
                emit("off", "chat off — use $ commands ($help)")
                return "\n".join(out)
            trim_history(self.messages)         # cap at the user-turn boundary
            self.messages.append({"role": "user", "content": user})
            try:
                while True:
                    live("thinking", "")
                    resp = self.client.chat.completions.create(
                        model=MODEL, messages=self.messages,
                        tools=self.tools, max_tokens=8192)
                    live("thought", "")
                    msg = resp.choices[0].message
                    content = strip_think(msg.content)
                    am = {"role": "assistant", "content": content or None}
                    if msg.tool_calls:
                        am["tool_calls"] = [{"id": tc.id, "type": "function",
                                             "function": {"name": tc.function.name,
                                                          "arguments": tc.function.arguments}}
                                            for tc in msg.tool_calls]
                    self.messages.append(am)
                    if content:
                        emit("bot", content)
                    if not msg.tool_calls:
                        break
                    for tc in msg.tool_calls:
                        raw = tc.function.arguments or "{}"
                        try:
                            args = json.loads(raw)
                        except json.JSONDecodeError:
                            emit("tool", f"[{tc.function.name}: invalid JSON args]")
                            self.messages.append(
                                {"role": "tool", "tool_call_id": tc.id,
                                 "content": f"error: arguments were not valid JSON: {raw}"})
                            continue
                        emit("tool", f"[{tc.function.name}({args})]")
                        result = run_tool(self.rover, self.arm, tc.function.name, args)
                        self.messages.append({"role": "tool", "tool_call_id": tc.id,
                                              "content": str(result)})
            except Exception as e:              # an LLM/network error ends the turn
                emit("error", f"chat error: {e}")
        return "\n".join(out)


def serve(session, chat_status, port=8090):
    """Async job model on loopback (plan 030): POST /chat submits a turn and
    returns {"turn": N} immediately (409 while one runs); GET /chat_poll
    fetches the result; GET /chat_status reports health. Binding the port is
    the single-instance mutex — a second service dies on EADDRINUSE."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"turn": 0, "busy": False, "active": None, "results": {}}
    slock = threading.Lock()

    def submit(text):
        with slock:
            if state["busy"]:
                return None
            state["turn"] += 1
            state["busy"] = True
            state["active"] = state["turn"]
            n = state["turn"]

        def work():
            try:
                reply = session.handle(text)
            except Exception as e:              # belt and braces: never wedge busy
                reply = f"chat error: {e}"
            with slock:
                state["results"][n] = reply
                state["busy"] = False
                state["active"] = None
                for k in sorted(state["results"])[:-20]:
                    state["results"].pop(k, None)
        threading.Thread(target=work, daemon=True).start()
        return n

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            if u.path == "/chat_status":
                with slock:
                    busy = state["busy"]
                self._json(200, dict(chat_status, busy=busy))
            elif u.path == "/chat_poll":
                try:
                    n = int((parse_qs(u.query).get("turn") or ["x"])[0])
                except ValueError:
                    self._json(400, {"error": "bad turn"})
                    return
                with slock:
                    if n in state["results"]:
                        self._json(200, {"done": True, "reply": state["results"][n]})
                    elif n == state["active"]:
                        self._json(200, {"done": False})
                    else:               # never existed OR evicted by the cap —
                        self._json(404, {"error": "unknown or expired turn"})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/chat":
                self._json(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                text = str(data["text"])
            except (ValueError, KeyError, TypeError):
                self._json(400, {"error": "body must be JSON with a text field"})
                return
            turn = submit(text)
            if turn is None:
                self._json(409, {"busy": True})
            else:
                self._json(200, {"turn": turn})

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"chat service on http://127.0.0.1:{port} (submit/poll; Ctrl-C to quit)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv


def main():
    load_dotenv()
    if "--serve" in sys.argv:
        # the served chat coexists with the controller on the same Pi — it
        # must NEVER become a second serial writer; drive via the controller
        os.environ["ROVER_NO_SERIAL"] = "1"
    print("detecting robots...")
    rover = detect_rover()
    arm = None
    if dobot.reachable():
        try:
            arm = dobot.Dobot()
        except OSError as e:
            print(f"  dobot reachable but connect failed ({e}); disabling Dobot")
    print(f"  rover: {'%s (%s)' % (rover.where, rover.backend) if rover else 'not found'}")
    print(f"  dobot: {'192.168.1.6' if arm else 'not found'}")
    if rover is None and arm is None:
        sys.exit("No robots reachable. Check the network / serial connection.")

    # LLM (optional — $ commands work without it)
    client = None
    api_key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if api_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=BASE_URL)
            chat_status = f"chat ON ({MODEL})"
        except ImportError:
            chat_status = "chat OFF (pip install openai)"
    else:
        chat_status = "chat OFF (set OPENCODE_API_KEY)"
    print(f"  {chat_status}")
    tools = build_tools(rover, arm)
    session = ChatSession(rover, arm, client, tools)

    if "--serve" in sys.argv:                   # website mode (plan 030)
        try:
            idx = sys.argv.index("--serve")
            port = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 8090
        except (ValueError, IndexError):
            port = 8090
        try:
            serve(session, {"ok": True, "model": MODEL if client else None,
                            "rover": rover.where if rover else None,
                            "dobot": bool(arm)}, port)
        finally:
            if rover is not None:
                rover.close()
            if arm is not None:
                arm.close()
        return

    print("Plain English to chat; $ for direct commands ($help). 'quit' to exit.\n")

    def live(kind, text):
        """Reproduce the REPL's original formatting from session events."""
        if kind == "thinking":
            print("  (thinking…)", end="\r", flush=True)
        elif kind == "thought":
            print(" " * 14, end="\r")
        elif kind == "help":
            print(text)                 # $help printed bare, as it always was
        elif kind == "dollar":
            print(" ", text)
        elif kind == "off":
            print("  " + text)
        elif kind == "bot":
            print(f"\nbot> {text}\n")
        elif kind == "tool":
            print(f"  {text}")
        elif kind == "error":
            print(f"\n  {text}")
    try:
        while True:
            try:
                user = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print(); break
            if not user:
                continue
            if user.lower() in ("quit", "exit"):
                break
            session.handle(user, live)
    finally:
        if rover is not None:
            rover.close()
        if arm is not None:
            arm.close()


if __name__ == "__main__":
    main()
