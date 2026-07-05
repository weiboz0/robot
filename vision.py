"""Provider-agnostic vision-model client for the rover's autonomous mode.

Sends a camera frame (JPEG bytes) + a prompt to any OpenAI-compatible vision
endpoint and returns text, or a validated JSON object. Configure via env:

  VISION_PROVIDER   openrouter | gemini | ark | dashscope | custom  (auto if unset)
  VISION_API_KEY    the key (else falls back to the provider's usual key env)
  VISION_BASE_URL   override base url (custom / self-host)
  VISION_MODEL      model id (else the provider's default below)

With nothing configured, constructing a VisionModel raises VisionUnavailable
with a helpful message — the autonomous command surfaces that instead of running.

Recommended free option: OpenRouter — a free key at openrouter.ai gives access
to free vision models (e.g. a Gemini/Qwen-VL/Llama-vision `:free` model). Set
OPENROUTER_API_KEY (and optionally VISION_MODEL).
"""
from __future__ import annotations

import base64
import json
import os
import re

PRESETS = {
    "openrouter": {"base": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
                   "model": "google/gemini-2.0-flash-exp:free"},
    "gemini": {"base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_env": "GEMINI_API_KEY", "model": "gemini-2.0-flash"},
    # The chatbot's own opencode zen key serves vision too — qwen3.6-plus passed
    # the live structured-JSON find/floor tests on real rover frames (plan 017).
    "opencode": {"base": "https://opencode.ai/zen/go/v1", "key_env": "OPENCODE_API_KEY",
                 "model": "qwen3.6-plus"},
    "dashscope": {"base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "key_env": "DASHSCOPE_CODING_KEY", "model": "qwen-vl-max"},
    "ark": {"base": "https://ark.cn-beijing.volces.com/api/v3", "key_env": "ARK_API_KEY",
            "model": "doubao-1-5-vision-pro-32k-250115"},
}
# Auto-detect order: explicit vision keys first, then the chatbot's opencode key
# (verified working), then dashscope/ark (ark needs per-model console activation,
# so it's the most likely to be blocked).
_AUTO_ORDER = ("openrouter", "gemini", "opencode", "dashscope", "ark")


class VisionUnavailable(RuntimeError):
    """No vision model is configured/reachable."""


def _autodetect() -> str:
    for p in _AUTO_ORDER:
        if os.environ.get(PRESETS[p]["key_env"], "").strip():
            return p
    return ""


class VisionModel:
    """One OpenAI-compatible vision endpoint. `client` is injectable for tests."""

    def __init__(self, *, provider: str = None, model: str = None, api_key: str = None,
                 base_url: str = None, timeout: float = 30.0, client=None):
        provider = (provider or os.environ.get("VISION_PROVIDER", "")).strip().lower() or _autodetect()
        if not provider:
            raise VisionUnavailable(
                "no vision model configured — the chatbot's OPENCODE_API_KEY works "
                "(auto-detected), or set OPENROUTER_API_KEY / VISION_PROVIDER + "
                "VISION_API_KEY. See docs/plans/017.")
        preset = PRESETS.get(provider, {})
        self.provider = provider
        self.base_url = base_url or os.environ.get("VISION_BASE_URL", "").strip() or preset.get("base")
        if provider == "opencode":          # honor a custom zen gateway if configured
            self.base_url = (base_url or os.environ.get("VISION_BASE_URL", "").strip()
                             or os.environ.get("OPENCODE_BASE_URL", "").strip() or preset.get("base"))
        self.api_key = (api_key or os.environ.get("VISION_API_KEY", "").strip()
                        or os.environ.get(preset.get("key_env", ""), "").strip())
        self.model = model or os.environ.get("VISION_MODEL", "").strip() or preset.get("model")
        self.timeout = timeout
        if not (self.base_url and self.api_key and self.model):
            raise VisionUnavailable(
                f"vision provider '{provider}' is missing base_url/api_key/model "
                f"(need {preset.get('key_env', 'VISION_API_KEY')} + VISION_MODEL).")
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI
            # max_retries=1 (not the SDK's default 2): a slow/timing-out gateway
            # otherwise burns ~3x the timeout per call, silently eating the
            # autonomous run's wall-clock budget.
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                                  timeout=timeout, max_retries=1)

    def describe_many(self, labeled_images, prompt: str, *, json_out: bool = False,
                      max_tokens: int = 1800) -> "str | dict":
        """Multi-image call: labeled_images = [(label, jpeg_bytes), ...]. Each
        image is preceded by its text label so the model can reference views."""
        content = [{"type": "text", "text": prompt}]
        for label, img in labeled_images:
            b64 = base64.b64encode(img).decode()
            content.append({"type": "text", "text": label})
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + b64}})
        kw = {"model": self.model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": content}]}
        if json_out:
            kw["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kw)
        text = resp.choices[0].message.content or ""
        return _extract_json(text) if json_out else text.strip()

    def describe(self, image_bytes: bytes, prompt: str, *, json_out: bool = False,
                 max_tokens: int = 400) -> "str | dict":
        """Send the frame + prompt; return text, or a parsed dict if json_out."""
        b64 = base64.b64encode(image_bytes).decode()
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]
        kw = {"model": self.model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": content}]}
        if json_out:
            kw["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kw)
        text = resp.choices[0].message.content or ""
        return _extract_json(text) if json_out else text.strip()


def _extract_json(text: str) -> dict:
    """Parse a JSON object from a model reply (tolerates prose/code fences)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"vision reply was not valid JSON: {text[:200]}")
