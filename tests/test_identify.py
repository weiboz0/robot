"""Plan 029: bbox→sphere conversion math + identify_objects on fakes.
The angular pins encode both plan-review catches: the exact pinhole atan
values (linear approximation erred ~3.8°) and the y-up/y-down sign fix
(upper-ring objects must land ABOVE the horizon)."""
import json
import math
import os
import tempfile
import unittest

import scene


def center_angles(pan, tilt):
    lon, lat, _, _ = scene.bbox_to_angles(pan, tilt, [0.4, 0.4, 0.6, 0.6], 9 / 16)
    return lon, lat


class BboxToAnglesTest(unittest.TestCase):
    def test_frame_center_is_the_pose(self):
        for pan, tilt in ((0, 0), (60, -5), (-120, 35), (180, 80)):
            lon, lat = center_angles(pan, tilt)
            self.assertAlmostEqual(lon, (pan + 180) % 360 - 180, places=5)
            self.assertAlmostEqual(lat, tilt, places=5)

    def test_upper_ring_lands_above_horizon(self):
        # the sign-convention catch: y-down matrix verbatim gave lat=-35
        _, lat = center_angles(0, 35)
        self.assertGreater(lat, 30)

    def test_horizontal_edge_is_half_fov(self):
        lon, _, _, _ = scene.bbox_to_angles(0, 0, [0.98, 0.45, 1.0, 0.55], 9 / 16)
        self.assertAlmostEqual(lon, scene.IDENTIFY_HFOV / 2, delta=1.0)

    def test_pinhole_not_linear_at_quarter_offset(self):
        # codex's quantified point: cx=0.75 → atan(0.5·tan44°) = 25.78°,
        # NOT the linear 22.0°
        lon, _, _, _ = scene.bbox_to_angles(0, 0, [0.73, 0.45, 0.77, 0.55], 9 / 16)
        self.assertAlmostEqual(lon, 25.78, delta=0.3)
        self.assertGreater(abs(lon - 22.0), 3.0)

    def test_wraps_at_180(self):
        lon, _, _, _ = scene.bbox_to_angles(180, 0, [0.73, 0.45, 0.77, 0.55], 9 / 16)
        self.assertAlmostEqual(lon, -154.2, delta=0.4)
        self.assertLessEqual(abs(lon), 180.0)

    def test_box_size_positive_and_sane(self):
        _, _, w, h = scene.bbox_to_angles(0, 0, [0.4, 0.4, 0.6, 0.6], 9 / 16)
        self.assertTrue(10 < w < 25, w)      # 0.2 frac ≈ 17.6°-ish region
        self.assertTrue(0 < h < w)           # aspect shrinks vertical


@unittest.skipUnless(__import__("importlib").util.find_spec("cv2"), "opencv not installed")
class WarperConsistencyTest(unittest.TestCase):
    def test_marker_through_real_warper_matches_conversion(self):
        # the convention pin: a marker at a known pixel, warped by the SAME
        # spherical warper/rmat the stitcher uses, must land within 1° of
        # where bbox_to_angles says
        import cv2
        import numpy as np
        w, h = 640, 360
        f_pin = (w / 2) / math.tan(math.radians(scene.IDENTIFY_HFOV / 2))
        K = np.array([[f_pin, 0, w / 2], [0, f_pin, h / 2], [0, 0, 1]], np.float32)

        def rmat(pan, tilt):
            p, t = math.radians(pan), math.radians(tilt)
            ry = np.array([[math.cos(p), 0, math.sin(p)], [0, 1, 0],
                           [-math.sin(p), 0, math.cos(p)]])
            rx = np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)],
                           [0, math.sin(t), math.cos(t)]])
            return (ry @ rx).astype(np.float32)

        warper = cv2.PyRotationWarper("spherical", f_pin)
        for pan, tilt, fx, fy in ((0, 0, 0.5, 0.5), (60, -5, 0.7, 0.4),
                                  (-120, 35, 0.3, 0.6), (0, 35, 0.5, 0.3)):
            img = np.zeros((h, w, 3), np.uint8)
            px, py = int(fx * w), int(fy * h)
            img[py - 2:py + 3, px - 2:px + 3] = (255, 255, 255)   # 5×5 marker
            # (a single pixel can vanish under INTER_NEAREST warping)
            corner, warped = warper.warp(img, K, rmat(pan, tilt),
                                         cv2.INTER_NEAREST, cv2.BORDER_CONSTANT)
            ys, xs = np.nonzero(warped[:, :, 0])
            self.assertTrue(len(xs), f"marker lost at pose {pan},{tilt}")
            mx, my = corner[0] + xs.mean(), corner[1] + ys.mean()
            # spherical warper coords: x = f·lon (0=front), y = f·(π/2 − lat)
            lon_w = math.degrees(mx / f_pin)
            lat_w = 90.0 - math.degrees(my / f_pin)
            eps = 0.004
            lon_c, lat_c, _, _ = scene.bbox_to_angles(
                pan, tilt, [fx - eps, fy - eps, fx + eps, fy + eps], h / w)
            dlon = abs((lon_w - lon_c + 180) % 360 - 180)
            self.assertLess(dlon, 1.0, f"lon {lon_w} vs {lon_c} at {pan},{tilt}")
            self.assertLess(abs(lat_w - lat_c), 1.0,
                            f"lat {lat_w} vs {lat_c} at {pan},{tilt}")


class FakeVis:
    """Per-frame fake: payloads consumed one per describe() call."""
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def describe(self, img, prompt, json_out=False, max_tokens=None):
        self.calls += 1
        return self.payloads.pop(0) if self.payloads else {"objects": []}


class IdentifyObjectsTest(unittest.TestCase):
    FRAMES = [(p, -5, b"\xff\xd8f" + bytes([i])) for i, p in
              enumerate((-120, -60, 0, 60, 120, 180))] + \
             [(p, 35, b"\xff\xd8u" + bytes([i])) for i, p in
              enumerate((180, 120, 60, 0, -60, -120))] + [(-120, 80, b"\xff\xd8c")]

    def test_per_frame_calls_and_conversion(self):
        payloads = [{"objects": []}] * 13
        payloads[2] = {"objects": [{"name": "Suitcase", "color": "black",
                                    "bbox": [0.4, 0.4, 0.6, 0.6]}]}   # frame 2: pan 0, eye
        payloads[9] = {"objects": [{"name": "printer", "color": "white",
                                    "bbox": [0.45, 0.4, 0.55, 0.6]}]}  # frame 9: pan 0, upper
        v = FakeVis(payloads)
        meta = scene.identify_objects(v, self.FRAMES)
        self.assertEqual(v.calls, 13)                      # one call per frame
        names = {o["name"]: o for o in meta["objects"]}
        self.assertIn("suitcase", names)                   # lowercased
        self.assertAlmostEqual(names["suitcase"]["lon"], 0.0, delta=0.1)
        self.assertAlmostEqual(names["suitcase"]["lat"], -5.0, delta=0.5)
        self.assertIn("printer", names)
        self.assertAlmostEqual(names["printer"]["lat"], 35.0, delta=0.7)   # upper ring UP

    def test_overlap_dedup_keeps_largest(self):
        payloads = [{"objects": []}] * 13
        # the same physical suitcase at lon≈30: right of frame 2 (pan 0),
        # left of frame 3 (pan 60) — real cross-frame dups land within a few
        # degrees of each other
        payloads[2] = {"objects": [{"name": "suitcase", "color": "black",
                                    "bbox": [0.7, 0.3, 0.9, 0.7]}]}
        payloads[3] = {"objects": [{"name": "suitcase", "color": "black",
                                    "bbox": [0.1, 0.35, 0.3, 0.6]}]}
        v = FakeVis(payloads)
        meta = scene.identify_objects(v, self.FRAMES)
        cases = [o for o in meta["objects"] if o["name"] == "suitcase"]
        self.assertEqual(len(cases), 1)                    # deduped
        self.assertGreater(cases[0]["h"], 15)              # the LARGE view won

    def test_twin_objects_different_color_survive(self):
        payloads = [{"objects": []}] * 13
        payloads[2] = {"objects": [
            {"name": "printer", "color": "white", "bbox": [0.40, 0.4, 0.50, 0.6]},
            {"name": "printer", "color": "black", "bbox": [0.52, 0.4, 0.62, 0.6]}]}
        meta = scene.identify_objects(FakeVis(payloads), self.FRAMES)
        self.assertEqual(len([o for o in meta["objects"]
                              if o["name"] == "printer"]), 2)

    def test_distinct_same_name_objects_kept(self):
        payloads = [{"objects": []}] * 13
        payloads[2] = {"objects": [{"name": "printer", "bbox": [0.4, 0.4, 0.6, 0.6]}]}
        payloads[5] = {"objects": [{"name": "printer", "bbox": [0.4, 0.4, 0.6, 0.6]}]}
        v = FakeVis(payloads)                              # pan 0 vs pan 180
        meta = scene.identify_objects(v, self.FRAMES)
        self.assertEqual(len([o for o in meta["objects"]
                              if o["name"] == "printer"]), 2)   # both printers kept

    def test_bad_entries_skipped(self):
        payloads = [{"objects": [
            {"name": "ok", "bbox": [0.1, 0.1, 0.3, 0.3]},
            {"name": "inverted", "bbox": [0.5, 0.5, 0.2, 0.2]},
            {"name": "out-of-range", "bbox": [0.1, 0.1, 1.5, 0.5]},
            {"name": "short", "bbox": [0.1, 0.2]},
            {"name": "no-bbox"}]}] + [{"objects": []}] * 12
        meta = scene.identify_objects(FakeVis(payloads), self.FRAMES)
        self.assertEqual([o["name"] for o in meta["objects"]], ["ok"])

    def test_per_frame_failures_tolerated(self):
        class Flaky:
            calls = 0
            def describe(self, *a, **k):
                Flaky.calls += 1
                if Flaky.calls != 3:
                    raise RuntimeError("gateway hiccup")
                return {"objects": [{"name": "bin", "bbox": [0.4, 0.4, 0.6, 0.6]}]}
        meta = scene.identify_objects(Flaky(), self.FRAMES)
        self.assertEqual(len(meta["objects"]), 1)          # partial results kept

    def test_all_failures_return_none(self):
        class Boom:
            def describe(self, *a, **k):
                raise RuntimeError("no provider")
        self.assertIsNone(scene.identify_objects(Boom(), self.FRAMES))


class FrameOrderTest(unittest.TestCase):
    def test_eye_ring_first_then_upper_then_ceiling(self):
        frames = [(0, 80, b"c"), (60, 35, b"u"), (-60, -5, b"e1"), (0, -5, b"e2")]
        ordered = scene.order_frames_for_identify(frames)
        self.assertEqual([f[1] for f in ordered], [-5, -5, 35, 80])
        self.assertEqual(ordered[0][0], -60)               # pan-sorted within ring


class StripMathTest(unittest.TestCase):
    def test_center_of_first_strip(self):
        # strip 0 starts at lon -180; a centered bbox → lon -135 (strip mid)
        lon, lat, w, h = scene.strip_bbox_to_angles(
            -180.0, [0.4, 0.4, 0.6, 0.6], 180.0)
        self.assertAlmostEqual(lon, -135.0)
        self.assertAlmostEqual(lat, 0.0)
        self.assertAlmostEqual(w, 18.0)     # 0.2 × 90°
        self.assertAlmostEqual(h, 36.0)     # 0.2 × 180°

    def test_wraps_across_the_seam(self):
        # last strip starts at 120° and spans to 210° → wraps to -150
        lon, _, _, _ = scene.strip_bbox_to_angles(
            120.0, [0.85, 0.4, 0.95, 0.6], 180.0)
        self.assertAlmostEqual(lon, -159.0)  # 120 + 0.9·90 = 201 → -159
        self.assertLessEqual(abs(lon), 180.0)

    def test_non_2_to_1_pano_uses_viewer_vspan(self):
        # a stitcher crop 4:1 → vspan 90°, NOT 180 (round-2 reviewer catch)
        self.assertAlmostEqual(scene.pano_vspan_deg(500, 2000), 90.0)
        _, lat, _, h = scene.strip_bbox_to_angles(
            0.0, [0.4, 0.0, 0.6, 0.5], scene.pano_vspan_deg(500, 2000))
        self.assertAlmostEqual(lat, 22.5)    # (0.5−0.25)·90
        self.assertAlmostEqual(h, 45.0)      # 0.5·90

    def test_full_sphere_vspan(self):
        self.assertAlmostEqual(scene.pano_vspan_deg(1000, 2000), 180.0)


@unittest.skipUnless(__import__("importlib").util.find_spec("cv2"), "opencv not installed")
class IdentifyEquirectTest(unittest.TestCase):
    def _pano(self, w=1440, h=720):
        import cv2
        import numpy as np
        img = np.full((h, w, 3), 120, np.uint8)
        return cv2.imencode(".jpg", img)[1].tobytes()

    class VisCapture:
        def __init__(self, per_strip=None):
            self.calls = 0
            self.prompts = []
            self.per_strip = per_strip or {}

        def describe(self, img, prompt, json_out=False, max_tokens=None):
            self.prompts.append(prompt)
            i = self.calls
            self.calls += 1
            return self.per_strip.get(i, {"objects": []})

    def test_six_strips_and_focus_in_prompt(self):
        v = self.VisCapture()
        meta = scene.identify_equirect(v, self._pano(), focus="stack of books")
        self.assertEqual(v.calls, 6)
        self.assertIn("stack of books", v.prompts[0])
        self.assertIsNone(meta)               # nothing found → None

    def test_strip_zero_object_converts(self):
        v = self.VisCapture({0: {"objects": [
            {"name": "Books", "color": "multi", "bbox": [0.4, 0.4, 0.6, 0.6]}]}})
        meta = scene.identify_equirect(v, self._pano())
        o = meta["objects"][0]
        self.assertEqual(o["name"], "books")
        self.assertAlmostEqual(o["lon"], -135.0, delta=0.5)
        self.assertAlmostEqual(o["lat"], 0.0, delta=0.5)

    def test_seam_strip_dedup_across_overlap(self):
        # same object seen in strips 4 (lon0=60) and 5 (lon0=120): a box at
        # 130° appears at x≈0.78 in strip 4 and x≈0.11 in strip 5
        v = self.VisCapture({
            4: {"objects": [{"name": "bin", "color": "clear",
                             "bbox": [0.72, 0.4, 0.84, 0.6]}]},
            5: {"objects": [{"name": "bin", "color": "clear",
                             "bbox": [0.05, 0.4, 0.17, 0.6]}]}})
        meta = scene.identify_equirect(v, self._pano())
        bins = [o for o in meta["objects"] if o["name"] == "bin"]
        self.assertEqual(len(bins), 1)        # deduped across the overlap

    def test_undecodable_pano_none(self):
        self.assertIsNone(scene.identify_equirect(self.VisCapture(), b"junk"))


class CliIdentifyTest(unittest.TestCase):
    def test_no_frames_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(scene.cli_identify(d, os.path.join(d, "m.json")), 1)

    def test_vision_unavailable_exits_1_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pan+000_t-05.jpg"), "wb") as f:
                f.write(b"\xff\xd8x")
            os.environ["VISION_PROVIDER"] = "nonexistent-provider"
            try:
                rc = scene.cli_identify(d, os.path.join(d, "m.json"))
            finally:
                del os.environ["VISION_PROVIDER"]
            self.assertEqual(rc, 1)
            self.assertFalse(os.path.exists(os.path.join(d, "m.json")))


if __name__ == "__main__":
    unittest.main()
