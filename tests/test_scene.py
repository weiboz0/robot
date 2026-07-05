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
    def test_scan_covers_rings_ceiling_and_recenters(self):
        c = FakeClient()
        frames = scene.scan_frames(c, pans=(0, 90), tilts=(-5, 35),
                                   ceiling=True, sleep=lambda s: None)
        views = [(p, t) for p, t, _ in frames]
        # serpentine: eye-level out, upper ring BACK (small gimbal moves), ceiling last
        self.assertEqual(views, [(0, -5), (90, -5), (90, 35), (0, 35), (0, scene.CEILING_TILT)])   # ceiling at last ring pan
        self.assertEqual(len({img for _, _, img in frames}), 5)   # distinct grabs
        self.assertEqual(c.aims[-1], (0, 0))                      # recentered at the end

    def test_recenters_even_on_failure(self):
        class Boom(FakeClient):
            def get_stream_frame(self):
                raise OSError("camera gone")
        c = Boom()
        with self.assertRaises(OSError):
            scene.scan_frames(c, pans=(0,), tilts=(-5,), ceiling=False, sleep=lambda s: None)
        self.assertEqual(c.aims[-1], (0, 0))

    def test_tier_and_view_labels(self):
        self.assertEqual(scene.tier_label(-5), "")
        self.assertEqual(scene.tier_label(35), "upper")
        self.assertEqual(scene.tier_label(80), "ceiling")
        self.assertEqual(scene.view_label(180, 35), "behind (upper)")
        self.assertEqual(scene.view_label(0, 80), "ceiling (straight up)")


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
        inv = scene.describe_scene(v, [(0, -5, b"A"), (180, 35, b"B")])
        self.assertEqual(v.many_calls, 1)
        self.assertEqual(v.single_calls, 0)
        self.assertIn("behind (upper)", v.labeled[1][0])       # direction+tier in the label
        self.assertEqual(inv["views"][0]["objects"][0]["details"], "black lid")

    def test_fallback_to_per_frame(self):
        v = FakeVision(multi_fails=True)
        inv = scene.describe_scene(v, [(0, -5, b"A"), (180, -5, b"B")])
        self.assertEqual(v.single_calls, 2)
        self.assertEqual(len(inv["views"]), 2)

    def test_chunked_describe_over_limit(self):
        v = FakeVision()
        frames = [(p, -5, bytes([65 + i])) for i, p in enumerate(range(0, 13))]
        scene.describe_scene(v, frames)                        # 13 views → 2 calls
        self.assertEqual(v.many_calls, 2)


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
            saved = scene.save_scene([(0, -5, b"JPEG0"), (-120, 35, b"JPEG1")], inv, scenes_dir=d)
            self.assertTrue(os.path.exists(os.path.join(saved, "pan+000_t-05.jpg")))
            self.assertTrue(os.path.exists(os.path.join(saved, "pan-120_t+35.jpg")))
            d2, inv2 = scene.load_latest_scene(scenes_dir=d)
            self.assertEqual(d2, saved)
            self.assertEqual(inv2, inv)

    def test_load_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(scene.load_latest_scene(scenes_dir=d), (None, None))


@unittest.skipUnless(scene and __import__("importlib").util.find_spec("cv2"), "opencv not installed")
class PanoramaTest(unittest.TestCase):
    def _ring(self):
        # synthetic overlapping ring: crops sliding across one textured image
        import cv2
        import numpy as np
        rng = np.random.RandomState(7)
        base = np.full((500, 2200, 3), 200, np.uint8)
        for _ in range(160):                                 # distinct matchable features
            x, y = int(rng.randint(0, 2200)), int(rng.randint(0, 500))
            c = tuple(int(v) for v in rng.randint(0, 255, 3))
            if rng.rand() < 0.5:
                cv2.circle(base, (x, y), int(rng.randint(6, 28)), c, -1)
            else:
                cv2.rectangle(base, (x, y), (x + int(rng.randint(10, 60)),
                                             y + int(rng.randint(10, 60))), c, -1)
        frames = []
        for i, x in enumerate(range(0, 1500, 300)):
            crop = base[:, x:x + 700]
            ok, buf = cv2.imencode(".jpg", crop)
            frames.append((i * 60 - 120, -5, buf.tobytes()))
        return frames

    def test_stitch_overlapping_ring(self):
        pano = scene.build_panorama(self._ring())
        self.assertIsNotNone(pano)
        self.assertEqual(pano[:2], b"\xff\xd8")            # JPEG out

    def test_stitch_failure_returns_none(self):
        import cv2
        import numpy as np
        blank = cv2.imencode(".jpg", np.zeros((200, 300, 3), np.uint8))[1].tobytes()
        self.assertIsNone(scene.build_panorama([(0, -5, blank)] * 4))

    def test_ceiling_frame_excluded_and_few_frames_none(self):
        self.assertIsNone(scene.build_panorama([(0, 80, b"x"), (0, 80, b"y")]))

    def test_save_scene_writes_panorama(self):
        inv = {"views": [], "overall": ""}
        with tempfile.TemporaryDirectory() as d:
            saved = scene.save_scene([(0, -5, b"JPEG0")], inv, scenes_dir=d,
                                     panorama=b"\xff\xd8PANO")
            self.assertTrue(os.path.exists(os.path.join(saved, "panorama.jpg")))


if __name__ == "__main__":
    unittest.main()
