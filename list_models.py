#!/usr/bin/env python3
"""List the models available for a provider.

Usage:
  ./.venv/bin/python list_models.py            # auto-detected provider
  ./.venv/bin/python list_models.py ark
  ./.venv/bin/python list_models.py dashscope

Note: this lists each provider's foundation-model catalog. To see your ARK
inference endpoints (ep-..., e.g. MiniMax), use list_ark_endpoints.py instead.

Reads keys/config from .env via chatbot.PROVIDERS.
"""
import sys

from chatbot import PROVIDERS, load_dotenv, pick_provider, resolve_base_url
import os


def list_models(provider: str) -> list[str]:
    cfg = PROVIDERS[provider]
    api_key = os.environ.get(cfg["key_env"], "").strip()
    if not api_key:
        sys.exit(f"No key for '{provider}' — set {cfg['key_env']} in .env")

    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=resolve_base_url(provider))
    return [m.id for m in client.models.list().data]


def main() -> None:
    load_dotenv()
    provider = (sys.argv[1].strip().lower() if len(sys.argv) > 1 else pick_provider())
    if provider not in PROVIDERS:
        sys.exit(f"Unknown provider '{provider}'. Choose from: {', '.join(PROVIDERS)}")

    try:
        models = list_models(provider)
    except Exception as e:
        sys.exit(f"Failed to list models for '{provider}': {e}")

    print(f"[{provider}] {len(models)} models:")
    for mid in sorted(models):
        print(f"  {mid}")


if __name__ == "__main__":
    main()
