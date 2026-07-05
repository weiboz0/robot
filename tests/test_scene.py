"""Tests for the 360° scene scan memory (plan 022). Fakes — no camera/network."""
import json
import os
import tempfile
import unittest

import scene


class DirectionTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(scene.direction_label(0), "front")
        self.assertEqual(scene.direction_label(60), "front-right")
        self.assertEqual(scene.direction_label(90), "right")
        self.assertEqual(scene.direction_label(120), "back-right")
        self.assertEqual(scene.direction_label(180), "behind")
        self.assertEqual(scene.direction_label(-180), "behind")
        self.assertEqual(scene.direction_label(-120), "back-left")
        self.assertEqual(scene.direction_label(-60), "front-left")


class FakeClient:
    def __init__(self):
        self.aims = []
        self.grabs = 0

    def set_camera(self, p, t):
        self.aims.append((p, t))

    def get_stream_frame(self):
        self.grabs += 1
        return b"JPEG" + str(self.grabs).encode()


class ScanTest(unittest.TestCase):
    def test_scan_aims_grabs_and_recenters(self):
        c = FakeClient()
        frames = scene.scan_frames(c, pans=(0, 90, 180), tilt=-5, sleep=lambda s: None)
        self.assertEqual([p for p, _ in frames], [0, 90, 180])
        self.assertEqual(len({img for _, img in frames}), 3)   # distinct grabs
        self.assertEqual(c.aims[-1], (0, 0))                   # recentered at the end

    def test_recenters_even_on_failure(self):
        class Boom(FakeClient):
            def get_stream_frame(self):
                raise OSError("camera gone")
        c = Boom()
        with self.assertRaises(OSError):
            scene.scan_frames(c, pans=(0,), sleep=lambda s: None)
        self.assertEqual(c.aims[-1], (0, 0))


class FakeVision:
    def __init__(self, multi_fails=False):
        self.multi_fails = multi_fails
        self.many_calls = 0
        self.single_calls = 0

    def describe_many(self, labeled, prompt, json_out=False, max_tokens=None):
        self.many_calls += 1
        if self.multi_fails:
            raise RuntimeError("multi-image unsupported")
        self.labeled = labeled
        return {"views": [{"direction": "front", "objects":
                           [{"name": "bin", "color": "grey", "details": "black lid"}],
                           "summary": "a bin"}], "overall": "a room"}

    def describe(self, img, prompt, json_out=False, max_tokens=None):
        self.single_calls += 1
        return {"direction": "front", "objects": [], "summary": "view"}


class DescribeTest(unittest.TestCase):
    def test_single_multiimage_call(self):
        v = FakeVision()
        inv = scene.describe_scene(v, [(0, b"A"), (180, b"B")])
        self.assertEqual(v.many_calls, 1)
        self.assertEqual(v.single_calls, 0)
        self.assertIn("behind", v.labeled[1][0])               # direction in the label
        self.assertEqual(inv["views"][0]["objects"][0]["details"], "black lid")

    def test_fallback_to_per_frame(self):
        v = FakeVision(multi_fails=True)
        inv = scene.describe_scene(v, [(0, b"A"), (180, b"B")])
        self.assertEqual(v.single_calls, 2)
        self.assertEqual(len(inv["views"]), 2)


class RenderAndPersistTest(unittest.TestCase):
    def test_render_inventory(self):
        inv = {"views": [{"direction": "behind",
                          "objects": [{"name": "bin", "color": "grey",
                                       "details": "black lid"}],
                          "summary": ""}], "overall": "a tidy room"}
        text = scene.render_inventory(inv)
        self.assertIn("[behind]", text)
        self.assertIn("grey bin (black lid)", text)
        self.assertIn("overall: a tidy room", text)
        self.assertIn("OVERLAP", text)                          # direction-overlap legend

    def test_save_and_load_roundtrip(self):
        inv = {"views": [{"direction": "front", "objects": [], "summary": "s"}],
               "overall": "o"}
        with tempfile.TemporaryDirectory() as d:
            saved = scene.save_scene([(0, b"JPEG0"), (-120, b"JPEG1")], inv, scenes_dir=d)
            self.assertTrue(os.path.exists(os.path.join(saved, "pan+000.jpg")))
            self.assertTrue(os.path.exists(os.path.join(saved, "pan-120.jpg")))
            d2, inv2 = scene.load_latest_scene(scenes_dir=d)
            self.assertEqual(d2, saved)
            self.assertEqual(inv2, inv)

    def test_load_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(scene.load_latest_scene(scenes_dir=d), (None, None))


if __name__ == "__main__":
    unittest.main()
