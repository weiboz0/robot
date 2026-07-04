"""Tests for the local CV color-object detector (plan 021). Synthetic frames —
no camera, no network, no motion. Skipped when OpenCV isn't installed."""
import unittest

import autodrive
import detector


def _cv2():
    try:
        import cv2
        import numpy as np
        return cv2, np
    except ImportError:
        return None, None


cv2, np = _cv2()


def frame(draw):
    """A beige-floor 640x480 frame; draw(img) adds objects. Returns JPEG bytes."""
    img = np.full((480, 640, 3), (150, 180, 200), np.uint8)   # beige-ish BGR
    draw(img)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


@unittest.skipUnless(cv2 is not None, "opencv not installed")
class DetectorTest(unittest.TestCase):
    def test_green_pen_like_bar_detected(self):
        # elongated saturated-green bar on the floor region
        jpg = frame(lambda im: cv2.rectangle(im, (300, 300), (390, 318), (40, 160, 30), -1))
        d = detector.detect_color_object(jpg, "green")
        self.assertIsNotNone(d)
        b = d["bbox"]
        self.assertAlmostEqual((b[0] + b[2]) / 2, 345 / 640, delta=0.03)
        self.assertAlmostEqual((b[1] + b[3]) / 2, 309 / 480, delta=0.03)
        self.assertGreater(d["elong"], 2.0)

    def test_empty_floor_detects_nothing(self):
        self.assertIsNone(detector.detect_color_object(frame(lambda im: None), "green"))

    def test_wall_region_blob_filtered(self):
        # green blob high in the frame (wall art) — bottom above the floor line
        jpg = frame(lambda im: cv2.rectangle(im, (300, 20), (390, 60), (40, 160, 30), -1))
        self.assertIsNone(detector.detect_color_object(jpg, "green"))

    def test_edge_clipped_blob_filtered(self):
        jpg = frame(lambda im: cv2.rectangle(im, (0, 300), (60, 320), (40, 160, 30), -1))
        self.assertIsNone(detector.detect_color_object(jpg, "green"))

    def test_weak_saturation_scores_below_strong(self):
        def draw(im):
            cv2.rectangle(im, (100, 300), (190, 318), (90, 130, 100), -1)   # dull green strip
            cv2.rectangle(im, (400, 300), (490, 318), (40, 170, 25), -1)    # vivid green bar
        d = detector.detect_color_object(frame(draw), "green")
        self.assertGreater((d["bbox"][0] + d["bbox"][2]) / 2, 0.5)          # vivid one wins

    def test_corrupt_jpeg_returns_none(self):
        # bad frame = transient → "not seen", never a crash (codex plan review)
        self.assertIsNone(detector.detect_color_object(b"not a jpeg", "green"))

    def test_dull_only_frame_filtered_by_saturation(self):
        # a weakly-saturated strip alone (shadow-like) must NOT become a detection
        jpg = frame(lambda im: cv2.rectangle(im, (100, 300), (190, 318), (95, 128, 105), -1))
        self.assertIsNone(detector.detect_color_object(jpg, "green"))

    def test_red_wraparound_range(self):
        # red spans two HSV ranges (0-10 and 170-180) — draw a hue~175 object
        jpg = frame(lambda im: cv2.rectangle(im, (300, 300), (390, 318), (40, 30, 200), -1))
        d = detector.detect_color_object(jpg, "red")
        self.assertIsNotNone(d)

    def test_unknown_color_raises(self):
        with self.assertRaises(ValueError):
            detector.detect_color_object(b"xx", "chartreuse")


class TargetColorTest(unittest.TestCase):
    def test_color_for_target(self):
        self.assertEqual(detector.color_for_target("a green pen"), "green")
        self.assertEqual(detector.color_for_target("the RED cup"), "red")
        self.assertIsNone(detector.color_for_target("a screwdriver"))


class ObsFromDetectionTest(unittest.TestCase):
    def test_detection_maps_to_obs(self):
        det = {"bbox": [0.55, 0.42, 0.66, 0.50], "score": 0.39, "elong": 4.5, "mean_sat": 158}
        obs = autodrive.obs_from_detection(det, "green")
        self.assertTrue(obs["seen"])
        self.assertEqual(obs["color"], "green")
        self.assertEqual(obs["bearing"], "center")
        self.assertGreaterEqual(obs["confidence"], autodrive.FOUND_MIN_CONF)
        self.assertFalse(obs["close"])                     # small & mid-frame → approach

    def test_none_detection_is_not_seen(self):
        obs = autodrive.obs_from_detection(None, "green")
        self.assertFalse(obs["seen"])
        self.assertEqual(obs["confidence"], 0.0)

    def test_low_bbox_is_close(self):
        det = {"bbox": [0.45, 0.6, 0.55, 0.75], "score": 0.4, "elong": 3.0, "mean_sat": 150}
        self.assertTrue(autodrive.obs_from_detection(det, "green")["close"])


class LookInjectionTest(unittest.TestCase):
    def test_find_object_uses_injected_look(self):
        from tests.test_autodrive import FakeDriver
        calls = []
        def look(name, img):
            calls.append(name)
            return {"seen": True, "bearing": "center", "close": True,
                    "confidence": 0.9, "bbox": [0.4, 0.6, 0.6, 0.8], "color": "green"}
        class NoLLM:                                        # detect path must not touch the LLM
            def describe(self, *a, **k): raise AssertionError("LLM used for detection")
        d = FakeDriver()
        out = autodrive.find_object(d, NoLLM(), "a green pen",
                                    capture=lambda: ("rover_x.jpg", b"IMG"), look=look)
        self.assertEqual(out, "rover_x.jpg")
        self.assertEqual(calls, ["rover_x.jpg"])


class CVFloorGateTest(unittest.TestCase):
    def test_cv_mode_still_llm_gates_forward(self):
        # THE safety seam: in CV mode the LLM floor gate must still be consulted
        # before any forward advance (Opus review nit #2).
        from tests.test_autodrive import FakeDriver
        floor_calls = []

        class FloorLLM:
            def describe(self, img, prompt, json_out=False, max_tokens=None):
                floor_calls.append(prompt[:20])
                return {"clear": True, "confidence": 0.9, "hazard": ""}

        seq = [{"seen": True, "bearing": "center", "close": False, "confidence": 0.9,
                "bbox": [0.45, 0.4, 0.55, 0.45]},
               {"seen": True, "bearing": "center", "close": True, "confidence": 0.9,
                "bbox": [0.4, 0.6, 0.6, 0.8]}]
        look = lambda name, img: seq.pop(0)
        d = FakeDriver()
        out = autodrive.find_object(d, FloorLLM(), "a green pen",
                                    capture=lambda: ("rover_x.jpg", b"IMG"), look=look)
        self.assertEqual(out, "rover_x.jpg")
        self.assertIn(("forward", True), d.actions)   # it advanced...
        self.assertTrue(floor_calls)                  # ...and the LLM floor gate WAS consulted


if __name__ == "__main__":
    unittest.main()
