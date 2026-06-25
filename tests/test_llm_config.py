"""No-network tests for llm_config: provider selection, base-url resolution, and
the unified load_dotenv reading both ~/.env and the repo .env."""
import os
import tempfile
import unittest
from unittest import mock

import llm_config


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_pick_provider_forced(self):
        os.environ["PROVIDER"] = "ark"
        self.assertEqual(llm_config.pick_provider(), "ark")

    def test_pick_provider_by_key(self):
        for k in ("PROVIDER", "ARK_API_KEY", "DASHSCOPE_CODING_KEY", "OPENCODE_API_KEY"):
            os.environ.pop(k, None)
        os.environ["DASHSCOPE_CODING_KEY"] = "x"
        self.assertEqual(llm_config.pick_provider(), "dashscope")

    def test_resolve_base_url_default_and_override(self):
        os.environ.pop("ARK_BASE_URL", None)
        self.assertIn("volces.com", llm_config.resolve_base_url("ark"))
        os.environ["ARK_BASE_URL"] = "https://example.test/v1"
        self.assertEqual(llm_config.resolve_base_url("ark"), "https://example.test/v1")


class LoadDotenvTest(unittest.TestCase):
    def test_loads_both_home_and_repo(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo:
            with open(os.path.join(home, ".env"), "w") as f:
                f.write("FOO_HOME=1\n")
            with open(os.path.join(repo, ".env"), "w") as f:
                f.write('FOO_REPO="2"\n')
            for k in ("FOO_HOME", "FOO_REPO"):
                os.environ.pop(k, None)
            with mock.patch("llm_config.os.path.expanduser",
                            lambda p: os.path.join(home, ".env") if p == "~/.env" else p), \
                 mock.patch("llm_config.os.path.dirname", return_value=repo):
                llm_config.load_dotenv()
            try:
                self.assertEqual(os.environ.get("FOO_HOME"), "1")
                self.assertEqual(os.environ.get("FOO_REPO"), "2")
            finally:
                for k in ("FOO_HOME", "FOO_REPO"):
                    os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()
