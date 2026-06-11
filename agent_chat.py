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
import socket
import sys

import rover_client          # stdlib-only (urllib); safe to import anywhere
import dobot

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "").strip() or "https://opencode.ai/zen/go/v1"
MODEL = os.environ.get("OPENCODE_MODEL", "").strip() or "minimax-m3"

SERIAL_DEVICES = ("/dev/ttyAMA0", "/dev/serial0")
ROVER_HTTP_HOST, ROVER_HTTP_PORT = "192.168.1.131", 5000

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def load_dotenv() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.expanduser("~/.env"), os.path.join(here, ".env")):
        if not os.path.exists(p):
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# --------------------------------------------------------------- rover backend
class RoverCtl:
    """Common rover interface over either the serial or HTTP backend."""

    def __init__(self, backend: str):
        self.backend = backend          # "serial" | "http"
        self.pan = 0.0
        self.tilt = 0.0
        if backend == "serial":
            import rover_direct
            self._rd = rover_direct
            if rover_direct.stop_http_service():
                print("stopped ugv_rpi/app.py to free the serial port.")
            self._r = rover_direct.Rover()
            self.where = self._r.port
        else:
            self.where = f"http://{ROVER_HTTP_HOST}:{ROVER_HTTP_PORT}"

    def set_camera(self, pan, tilt):
        self.pan = _clamp(float(pan), -180.0, 180.0)
        self.tilt = _clamp(float(tilt), -45.0, 90.0)
        if self.backend == "serial":
            self._r.set_camera(self.pan, self.tilt)
        else:
            rover_client.set_camera(self.pan, self.tilt)

    def drive(self, left, right, seconds):
        if self.backend == "serial":
            self._r.drive_for(float(left), float(right), float(seconds))
        else:
            rover_client.drive(float(left), float(right), float(seconds))

    def stop(self):
        if self.backend == "serial":
            self._r.stop()
        else:
            rover_client.stop()

    def center(self):
        self.set_camera(0, 0)

    def close(self):
        if self.backend == "serial":
            self._r.close()


def detect_rover():
    if any(os.path.exists(p) for p in SERIAL_DEVICES):
        try:
            return RoverCtl("serial")
        except Exception as e:
            print(f"rover serial detected but failed to open: {e}")
    if _reachable(ROVER_HTTP_HOST, ROVER_HTTP_PORT):
        return RoverCtl("http")
    return None


# ----------------------------------------------------- rover $-command parser
def rover_command(r: RoverCtl, line: str) -> str:
    parts = line.split()
    if not parts:
        return ""
    c, args = parts[0].lower(), parts[1:]
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
        if c == "stop":
            r.stop(); return "stopped"
        return f"?? unknown rover command '{c}'"
    except (IndexError, ValueError):
        return "bad args"


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
    "speeds small (<=0.3). Dobot: coordinates are mm (x,y,z) and degrees (r); move "
    "conservatively and enable the arm before moving. After acting, briefly say what "
    "you did. Be concise."
)


def main():
    load_dotenv()
    print("detecting robots...")
    rover = detect_rover()
    arm = dobot.Dobot() if dobot.reachable() else None
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
                    print("rover: up/down/left/right [deg], cam P T, center, drive L R [s], "
                          "fwd/back/spinl/spinr [s], stop\n"
                          "dobot: $dobot <raw cmd>  e.g. $dobot GetPose() / $dobot EnableRobot()")
                elif cmd.lower().startswith("dobot"):
                    raw = cmd[5:].strip()
                    if arm is None:
                        print("  dobot not connected")
                    elif raw.startswith("Mov"):
                        print(" ", arm.motion(raw))
                    else:
                        print(" ", arm.dashboard(raw))
                elif rover is not None:
                    print(" ", rover_command(rover, cmd))
                else:
                    print("  rover not connected")
                continue
            if client is None:
                print("  chat off — use $ commands ($help)")
                continue
            messages.append({"role": "user", "content": user})
            while True:
                print("  (thinking…)", end="\r", flush=True)
                resp = client.chat.completions.create(
                    model=MODEL, messages=messages, tools=tools, max_tokens=8192)
                print(" " * 14, end="\r")
                msg = resp.choices[0].message
                content = strip_think(msg.content)
                am = {"role": "assistant", "content": content}
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
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    print(f"  [{tc.function.name}({args})]")
                    out = run_tool(rover, arm, tc.function.name, args)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(out)})
    finally:
        if rover is not None:
            rover.close()
        if arm is not None:
            arm.close()


if __name__ == "__main__":
    main()
