"""No-network tests for the provider-agnostic vision client (plan 017)."""
import os
import unittest
from unittest import mock

import vision


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.captured = None

    def create(self, **kw):
        self.captured = kw
        msg = type("M", (), {"content": self.content})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice]})()


class FakeClient:
    def __init__(self, content):
        self.chat = type("Chat", (), {"completions": FakeCompletions(content)})()


class VisionConfigTest(unittest.TestCase):
    def test_unavailable_when_nothing_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(vision.VisionUnavailable):
                vision.VisionModel()

    def test_explicit_config_ok(self):
        vm = vision.VisionModel(provider="openrouter", api_key="k", model="m",
                                client=FakeClient("{}"))
        self.assertEqual(vm.model, "m")
        self.assertIn("openrouter.ai", vm.base_url)

    def test_autodetect_prefers_openrouter_over_ark(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "x", "ARK_API_KEY": "y"}, clear=True):
            vm = vision.VisionModel(client=FakeClient("{}"))
            self.assertEqual(vm.provider, "openrouter")


class VisionCallTest(unittest.TestCase):
    def test_describe_sends_image_and_returns_text(self):
        fc = FakeClient("a screwdriver on a table")
        vm = vision.VisionModel(provider="openrouter", api_key="k", model="m", client=fc)
        out = vm.describe(b"JPEGDATA", "what is this?")
        self.assertEqual(out, "a screwdriver on a table")
        content = fc.chat.completions.captured["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_describe_json_out_parses_and_sets_response_format(self):
        fc = FakeClient('{"clear": true, "confidence": 0.9}')
        vm = vision.VisionModel(provider="openrouter", api_key="k", model="m", client=fc)
        out = vm.describe(b"IMG", "floor?", json_out=True)
        self.assertEqual(out, {"clear": True, "confidence": 0.9})
        self.assertEqual(fc.chat.completions.captured["response_format"], {"type": "json_object"})

    def test_extract_json_tolerates_prose_and_fences(self):
        self.assertEqual(vision._extract_json('sure: {"a": 1} ok'), {"a": 1})
        self.assertEqual(vision._extract_json('```json\n{"b": 2}\n```'), {"b": 2})
        with self.assertRaises(ValueError):
            vision._extract_json("no json here")


if __name__ == "__main__":
    unittest.main()
