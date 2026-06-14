"""Tests for the rover_web gallery (skipped where Flask isn't installed)."""
import os
import shutil
import tempfile
import unittest

try:
    import flask  # noqa: F401
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False


@unittest.skipUnless(HAS_FLASK, "flask not installed")
class WebTest(unittest.TestCase):
    def setUp(self):
        import rover_web
        self.rw = rover_web
        self.tmp = tempfile.mkdtemp()
        self._orig = rover_web.PHOTO_DIR
        rover_web.PHOTO_DIR = self.tmp
        with open(os.path.join(self.tmp, "rover_x.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xd9")          # minimal JPEG marker
        self.client = rover_web.app.test_client()

    def tearDown(self):
        self.rw.PHOTO_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_lists_photo_and_live_view(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"rover_x.jpg", r.data)
        self.assertIn(b"/video_feed", r.data)     # live view embedded

    def test_serves_photo_file(self):
        r = self.client.get("/photos/rover_x.jpg")
        self.assertEqual(r.status_code, 200)

    def test_delete_removes_file(self):
        r = self.client.post("/delete/rover_x.jpg")
        self.assertEqual(r.status_code, 302)       # redirect back to index
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "rover_x.jpg")))

    def test_delete_rejects_path_traversal(self):
        r = self.client.post("/delete/..%2f..%2fsecret")
        self.assertIn(r.status_code, (400, 404))


if __name__ == "__main__":
    unittest.main()
