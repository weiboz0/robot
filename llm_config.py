"""Shared LLM provider config + .env loading.

Used by the chatbot (agent_chat.py) and the model-listing tools (list_models.py,
list_ark_endpoints.py). Dependency-free (no rover imports).
"""
import os
import sys


def load_dotenv() -> None:
    """Load ~/.env then the repo's .env (next to this file) into os.environ
    (setdefault, so real env vars win). Loads both — the superset of what the
    chatbot and the listers each used to load on their own."""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.expanduser("~/.env"), os.path.join(here, ".env")):
        if not os.path.exists(p):
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k.startswith("export "):     # tolerate `export VAR=value` lines
                        k = k[len("export "):].strip()
                    os.environ.setdefault(k, v.strip().strip('"').strip("'"))


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
