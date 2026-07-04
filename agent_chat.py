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
    "pen": "a pen",
}


def autonomous_find(rover, target):
    """$find <obj> / $screwdriver: drive the rover autonomously to find `target`
    and return a photo. Camera-only safety (no cliff sensor) — see docs/plans/017.
    Gated: vision must be configured AND ROVER_FIND_ENABLE=1, checked BEFORE any
    rover call, so an unset run is a pure no-op that never moves the rover."""
    if os.environ.get("ROVER_FIND_ENABLE", "").strip().lower() not in ("1", "true", "yes"):
        return ("autonomous find is DISABLED — it drives the rover on its own with "
                "camera-only safety (NO cliff sensor). To allow it: set ROVER_FIND_ENABLE=1 "
                "and run only on a flat, enclosed, ledge-free floor, ready to E-STOP.")
    if rover is None or rover.backend != "rovercontrol":
        return ("autonomous find needs the rovercontrol backend (run the Go controller "
                "and let the chatbot drive it over :8080 — not the serial/app.py path).")
    try:                                    # vision FIRST — no rover contact if unavailable
        import vision as _vision
        vm = _vision.VisionModel(timeout=45)   # zen gateway can be slow; fail one call, not the run
    except Exception as e:
        return f"vision not available: {e}"
    import autodrive
    import rovercontrol_client as client   # already pointed at the rover by RoverCtl
    def capture():
        n = client.snapshot()
        return n, client.get_photo(n)
    found_obs = {}
    def on_found(name, obs):                # store the bbox so the gallery can outline it
        found_obs.update(obs)
        if obs.get("bbox"):
            client.set_photo_meta(name, {
                "target": target, "color": str(obs.get("color") or ""),
                "bbox": obs["bbox"], "confidence": float(obs.get("confidence") or 0)})
    driver = autodrive.SafeDriver(client)
    try:
        shot = autodrive.find_object(driver, vm, target, capture=capture,
                                     log=lambda m: print("   " + m), on_found=on_found)
    except Exception as e:                  # rover is left stopped/safe (preflight refusal or cleanup)
        return f"find aborted — rover stopped/safe: {e}"
    if shot:
        color = found_obs.get("color") or "?"
        hint = ("outline: press ◻ on the photo in the gallery"
                if found_obs.get("bbox") else "no outline data (model gave no bbox)")
        return (f"found {target} (color: {color}) → {shot}   "
                f"({rover.where}/photos/{shot} — {hint})")
    return f"did not find {target} within the budget — wheels stopped."


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
            p = rover.photo(); return f"photo saved to {p}" if p else "photo failed (camera busy?)"
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
    "Use rover_get_status to check what is connected. "
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


def main():
    load_dotenv()
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
    print("Plain English to chat; $ for direct commands ($help). 'quit' to exit.\n")

    messages = [{"role": "system", "content": SYSTEM}]
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
            if user.startswith("$"):
                cmd = user[1:].strip()
                if cmd in ("help", ""):
                    print("rover camera: up/down/left/right [deg], cam P T, center, relax, lock\n"
                          "rover motors: drive L R [s], move L R, fwd/back/spinl/spinr [s], stop, estop\n"
                          "rover extras: light FRONT BASE (0-255), oled LINE TEXT, oledclear, demo, photo\n"
                          "rover meta:   speed [CAP 0..0.5], status, photos\n"
                          "website names also work: camera_up/..., camera_aim, camera_center, snapshot,\n"
                          "  gimbal_relax/lock, light_head|light_base [on|off], move_forward/back/left/right [MS]\n"
                          "  (note: drive/fwd/back keep CHATBOT units here — seconds, speeds -0.5..0.5)\n"
                          "autonomous:   find <object> / screwdriver / pen  (drives itself, stops +\n"
                          "  photographs when it sees the target; needs ROVER_FIND_ENABLE=1 + vision key)\n"
                          "dobot: $dobot <raw cmd>  e.g. $dobot GetPose() / $dobot EnableRobot()")
                elif cmd.lower().startswith("dobot"):
                    raw = cmd[5:].strip()
                    if arm is None:
                        print("  dobot not connected")
                    else:
                        try:                # a socket error must not kill the REPL
                            print(" ", arm.motion(raw) if raw.startswith("Mov")
                                  else arm.dashboard(raw))
                        except Exception as e:
                            print(f"  dobot error: {e}")
                elif cmd.lower() in FIND_SHORTCUTS or cmd.lower().startswith("find "):
                    target = FIND_SHORTCUTS.get(cmd.lower()) or cmd[5:].strip()
                    print(" ", autonomous_find(rover, target))
                elif rover is not None:
                    print(" ", rover_command(rover, cmd))
                else:
                    print("  rover not connected")
                continue
            if client is None:
                print("  chat off — use $ commands ($help)")
                continue
            trim_history(messages)              # P3: cap history at the user-turn boundary
            messages.append({"role": "user", "content": user})
            try:
                while True:
                    print("  (thinking…)", end="\r", flush=True)
                    resp = client.chat.completions.create(
                        model=MODEL, messages=messages, tools=tools, max_tokens=8192)
                    print(" " * 14, end="\r")
                    msg = resp.choices[0].message
                    content = strip_think(msg.content)
                    # P4: store None (not "") so providers that reject empty content don't choke
                    am = {"role": "assistant", "content": content or None}
                    if msg.tool_calls:
                        am["tool_calls"] = [{"id": tc.id, "type": "function",
                                             "function": {"name": tc.function.name,
                                                          "arguments": tc.function.arguments}}
                                            for tc in msg.tool_calls]
                    messages.append(am)
                    if content:
                        print(f"\nbot> {content}\n")
                    if not msg.tool_calls:
                        break
                    for tc in msg.tool_calls:
                        raw = tc.function.arguments or "{}"
                        try:
                            args = json.loads(raw)
                        except json.JSONDecodeError:
                            # P5: tell the model what it sent so it can self-correct
                            print(f"  [{tc.function.name}: invalid JSON args]")
                            messages.append({"role": "tool", "tool_call_id": tc.id,
                                             "content": f"error: arguments were not valid JSON: {raw}"})
                            continue
                        print(f"  [{tc.function.name}({args})]")
                        out = run_tool(rover, arm, tc.function.name, args)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(out)})
            except Exception as e:             # P1: an LLM/network error returns to the prompt
                print(f"\n  chat error: {e}")
    finally:
        if rover is not None:
            rover.close()
        if arm is not None:
            arm.close()


if __name__ == "__main__":
    main()
