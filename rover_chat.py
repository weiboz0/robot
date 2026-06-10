#!/usr/bin/env python3
"""Natural-language chatbot that drives the rover via DIRECT serial control.

Runs ON the rover. Combines rover_direct.py (UART motor/camera control) with an
LLM so fuzzy commands like "look up a bit", "spin around", or "drive forward a
little" work — the model turns them into tool calls that hit the serial port
directly (no HTTP service needed).

Default model: OpenCode / minimax-m3 (OpenAI-compatible). Configure via env/.env:
  OPENCODE_API_KEY   (required)
  OPENCODE_BASE_URL  (default https://opencode.ai/zen/go/v1)
  OPENCODE_MODEL     (default minimax-m3)

Setup on the rover (once):
  ~/ugv_rpi/ugv-env/bin/pip install openai
  printf 'OPENCODE_API_KEY=sk-...\\n' >> ~/.env
Run:
  ~/ugv_rpi/ugv-env/bin/python ~/rover_chat.py     (or: roverchat)
"""
import json
import os
import re
import sys

import rover_direct

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "").strip() or "https://opencode.ai/zen/go/v1"
MODEL = os.environ.get("OPENCODE_MODEL", "").strip() or "minimax-m3"

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from ~/.env (and ./ .env) into the environment."""
    for p in (os.path.expanduser("~/.env"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), path)):
        if not os.path.exists(p):
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


SYSTEM = (
    "You are the onboard assistant of a Waveshare UGV rover (tank-style wheels "
    "and a pan/tilt camera). Chat normally, and when the user wants to move, "
    "use the tools. You control the rover DIRECTLY over its serial bus. Keep "
    "actions small and safe unless told otherwise: modest speeds (|wheel| <= "
    "0.3) and short durations (<= 2s). Camera tilt is up/down (+ is up, range "
    "-45..90), pan is left/right (-180..180). After acting, say briefly what you "
    "did. Be concise."
)

TOOL_DEFS = [
    {
        "name": "set_camera",
        "description": "Aim the camera to absolute angles. pan=left/right, tilt=up/down (+ up).",
        "parameters": {
            "type": "object",
            "properties": {
                "pan": {"type": "number", "description": "-180..180, 0 = forward"},
                "tilt": {"type": "number", "description": "-45..90, + = up, 0 = level"},
            },
            "required": ["pan", "tilt"],
        },
    },
    {
        "name": "center_camera",
        "description": "Level the camera (pan 0, tilt 0).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "drive",
        "description": "Drive the rover for a moment then auto-stop. left/right are wheel speeds -0.5..0.5 (negative reverses). Opposite signs turn in place.",
        "parameters": {
            "type": "object",
            "properties": {
                "left": {"type": "number", "description": "left wheel -0.5..0.5"},
                "right": {"type": "number", "description": "right wheel -0.5..0.5"},
                "seconds": {"type": "number", "description": "duration 0..5"},
            },
            "required": ["left", "right", "seconds"],
        },
    },
    {
        "name": "stop",
        "description": "Stop the wheels immediately.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "lights",
        "description": "Set LED brightness, 0..255. front=head light, base=chassis light.",
        "parameters": {
            "type": "object",
            "properties": {
                "front": {"type": "integer", "description": "0..255"},
                "base": {"type": "integer", "description": "0..255"},
            },
            "required": ["front", "base"],
        },
    },
]


def run_tool(rover, name: str, args: dict) -> str:
    try:
        if name == "set_camera":
            rover.set_camera(args.get("pan", 0), args.get("tilt", 0))
            return f"camera -> pan={args.get('pan', 0)}, tilt={args.get('tilt', 0)}"
        if name == "center_camera":
            rover.center_camera()
            return "camera centered"
        if name == "drive":
            rover.drive_for(args["left"], args["right"], args.get("seconds", 1.0))
            return f"drove L={args['left']} R={args['right']} for {args.get('seconds', 1.0)}s, stopped"
        if name == "stop":
            rover.stop()
            return "stopped"
        if name == "lights":
            rover.lights(args.get("front", 0), args.get("base", 0))
            return f"lights front={args.get('front', 0)} base={args.get('base', 0)}"
        return f"unknown tool: {name}"
    except Exception as e:
        return f"tool error: {e}"


def main() -> None:
    load_dotenv()
    api_key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not api_key:
        sys.exit("OPENCODE_API_KEY not set. Add it to ~/.env on the rover.")

    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("openai SDK missing. Run: ~/ugv_rpi/ugv-env/bin/pip install openai")

    client = OpenAI(api_key=api_key, base_url=BASE_URL)
    tools = [{"type": "function", "function": t} for t in TOOL_DEFS]

    # Take the serial port (stops the web app) and connect.
    keep_app = "--keep-app" in sys.argv
    if not keep_app and rover_direct.stop_http_service():
        print("stopped ugv_rpi/app.py to free the serial port.")
    try:
        rover = rover_direct.Rover()
    except Exception as e:
        sys.exit(f"Could not open serial: {e}")
    print(f"[rover chat | {MODEL}] connected on {rover.port}.")
    print("Type normally to chat. Prefix a line with $ for a direct command "
          "(e.g. '$up 45', '$drive 0.2 0.2 1', '$help'). 'quit' to exit.\n")

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
            if user.startswith("$"):                 # direct command, no LLM
                out = rover_direct.exec_command(rover, user[1:].strip())
                if out == rover_direct.QUIT:
                    break
                if out:
                    print(out)
                continue
            messages.append({"role": "user", "content": user})
            while True:
                print("  (thinking…)", end="\r", flush=True)
                resp = client.chat.completions.create(
                    model=MODEL, messages=messages, tools=tools, max_tokens=8192,
                )
                print(" " * 14, end="\r")
                msg = resp.choices[0].message
                content = strip_think(msg.content)
                assistant = {"role": "assistant", "content": content}
                if msg.tool_calls:
                    assistant["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ]
                messages.append(assistant)
                if content:
                    print(f"\nrover> {content}\n")
                if not msg.tool_calls:
                    break
                for tc in msg.tool_calls:
                    try:
                        a = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        a = {}
                    print(f"  [{tc.function.name}({a})]")
                    out = run_tool(rover, tc.function.name, a)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    finally:
        rover.close()


if __name__ == "__main__":
    main()
