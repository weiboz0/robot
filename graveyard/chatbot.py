#!/usr/bin/env python3
"""A simple terminal chatbot that can also drive the rover.

Works with OpenAI-compatible model providers:
  - ark        Volcano Engine ARK  (ARK_API_KEY) — also serves third-party
               models like MiniMax via an inference endpoint; set ARK_MODEL
               to that endpoint id (ep-...) or a foundation-model id.
  - dashscope  Alibaba DashScope / Qwen  (DASHSCOPE_CODING_KEY)

Provider is auto-selected from whichever key is present (override with
PROVIDER=... in .env). Pick the model with ARK_MODEL / DASHSCOPE_MODEL.

Chat normally; ask it to move the camera or drive and it calls the rover tools.
Run:  ./.venv/bin/python chatbot.py
"""
import json
import os
import re
import sys

import rover_client

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks some models emit inline."""
    return _THINK.sub("", text or "").strip()


# ---------------------------------------------------------------- env loading
def load_dotenv(path: str = ".env") -> None:
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(here):
        return
    with open(here) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# ------------------------------------------------------------------ providers
PROVIDERS = {
    "ark": {
        "key_env": "ARK_API_KEY",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model_env": "ARK_MODEL",
        "default_model": "doubao-1-5-lite-32k-250115",
    },
    "dashscope": {
        "key_env": "DASHSCOPE_CODING_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model_env": "DASHSCOPE_MODEL",
        "default_model": "qwen-plus",
    },
    "opencode": {
        "key_env": "OPENCODE_API_KEY",
        "base_url": None,  # set OPENCODE_BASE_URL in .env
        "model_env": "OPENCODE_MODEL",
        "default_model": "",  # set OPENCODE_MODEL in .env
    },
}


def pick_provider() -> str:
    forced = os.environ.get("PROVIDER", "").strip().lower()
    if forced:
        if forced not in PROVIDERS:
            sys.exit(f"PROVIDER={forced} is not one of {list(PROVIDERS)}")
        return forced
    for name in ("ark", "dashscope", "opencode"):  # priority order
        if os.environ.get(PROVIDERS[name]["key_env"], "").strip():
            return name
    sys.exit("No provider key found in .env (set ARK_API_KEY, DASHSCOPE_CODING_KEY, or OPENCODE_API_KEY).")


def resolve_base_url(provider: str) -> str:
    """Per-provider base URL, overridable via <PROVIDER>_BASE_URL in .env."""
    return os.environ.get(f"{provider.upper()}_BASE_URL", "").strip() or PROVIDERS[provider]["base_url"]


# ----------------------------------------------------------------- bot config
SYSTEM = (
    "You are a friendly assistant embedded with a Waveshare UGV rover named "
    "'rover' (a Raspberry Pi robot with a pan/tilt camera and tank-style "
    "wheels). Chat normally about anything. When the user wants to move the "
    "camera or drive the rover, use the provided tools. Keep physical actions "
    "small and safe unless the user insists: short drive durations, modest "
    "speeds. After acting, briefly say what you did. Camera tilt is up/down "
    "(+ is up), pan is left/right."
)

# Neutral tool definitions; reformatted per provider below.
TOOL_DEFS = [
    {
        "name": "set_camera",
        "description": "Aim the rover's camera gimbal to absolute angles.",
        "parameters": {
            "type": "object",
            "properties": {
                "pan": {"type": "number", "description": "Left/right angle, -180..180 (0 = forward)."},
                "tilt": {"type": "number", "description": "Up/down angle, -45..90 (+ is up, 0 = level)."},
            },
            "required": ["pan", "tilt"],
        },
    },
    {
        "name": "drive",
        "description": "Drive the rover. left/right are wheel speeds (-0.5..0.5); negative reverses. Drives for `seconds` then stops. Opposite signs turn in place.",
        "parameters": {
            "type": "object",
            "properties": {
                "left": {"type": "number", "description": "Left wheel speed -0.5..0.5"},
                "right": {"type": "number", "description": "Right wheel speed -0.5..0.5"},
                "seconds": {"type": "number", "description": "How long to drive (0..5)."},
            },
            "required": ["left", "right", "seconds"],
        },
    },
    {
        "name": "stop",
        "description": "Immediately stop the rover's wheels.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def run_tool(name: str, args: dict) -> str:
    try:
        if name == "set_camera":
            rover_client.set_camera(args.get("pan", 0), args.get("tilt", 0))
            return f"Camera set to pan={args.get('pan', 0)}, tilt={args.get('tilt', 0)}."
        if name == "drive":
            rover_client.drive(args["left"], args["right"], args.get("seconds", 1.0))
            return f"Drove left={args['left']}, right={args['right']} for {args.get('seconds', 1.0)}s, then stopped."
        if name == "stop":
            rover_client.stop()
            return "Stopped."
        return f"Unknown tool: {name}"
    except Exception as e:
        return f"Tool error: {e}"


# ------------------------------------------------------- OpenAI-compatible run
def chat_openai(base_url: str, api_key: str, model: str) -> None:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    tools = [{"type": "function", "function": t} for t in TOOL_DEFS]
    messages = [{"role": "system", "content": SYSTEM}]
    print("Rover chatbot. Type your message, 'quit' to exit.\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not user:
            continue
        if user.lower() in ("quit", "exit"):
            return
        messages.append({"role": "user", "content": user})
        while True:
            print("  (thinking…)", end="\r", flush=True)
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=8192,
            )
            print(" " * 14, end="\r")  # clear the "(thinking…)" line
            msg = resp.choices[0].message
            if resp.choices[0].finish_reason == "length":
                print("  [note: response hit the token limit and may be truncated]")
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
                print(f"\nrover-bot> {content}\n")
            if not msg.tool_calls:
                break
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                print(f"  [calling {tc.function.name}({args})]")
                out = run_tool(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})


def main() -> None:
    load_dotenv()
    provider = pick_provider()
    cfg = PROVIDERS[provider]
    api_key = os.environ.get(cfg["key_env"], "").strip()
    if not api_key:
        sys.exit(f"Provider '{provider}' selected but {cfg['key_env']} is empty in .env.")
    base_url = resolve_base_url(provider)
    if not base_url:
        sys.exit(f"Provider '{provider}' needs a base URL — set {provider.upper()}_BASE_URL in .env.")
    model = os.environ.get(cfg["model_env"], "").strip() or cfg["default_model"]
    if not model:
        sys.exit(f"Provider '{provider}' needs a model — set {cfg['model_env']} in .env.")
    print(f"[provider: {provider} | model: {model}]")
    chat_openai(base_url, api_key, model)


if __name__ == "__main__":
    main()
