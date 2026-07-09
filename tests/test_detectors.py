"""Tests for the detector-comparison registry (plan 024). YOLO runs are heavy —
only the hsv path executes; YOLO coverage is registry/interface-level."""
import unittest

import detectors


def _cv2():
    try:
        import cv2
        import numpy as np
        return cv2, np
    except ImportError:
        return None, None


cv2, np = _cv2()


class RegistryTest(unittest.TestCase):
    def test_models_listed(self):
        self.assertIn("hsv", detectors.MODELS)
        self.assertIn("yolo11s", detectors.MODELS)

    def test_unknown_model_raises(self):
        with self.assertRaises(ValueError):
            detectors.run("nope", b"\xff\xd8x")


@unittest.skipUnless(cv2 is not None, "opencv not installed")
class HsvPathTest(unittest.TestCase):
    def test_hsv_run_annotates_and_reports(self):
        img = np.full((480, 640, 3), (150, 180, 200), np.uint8)
        cv2.rectangle(img, (300, 300), (390, 318), (40, 160, 30), -1)
        jpg = cv2.imencode(".jpg", img)[1].tobytes()
        vis, dets = detectors.run("hsv", jpg, "green")
        self.assertEqual(vis[:2], b"\xff\xd8")
        self.assertEqual(len(dets), 1)
        self.assertIn("green", dets[0]["label"])
        self.assertEqual(len(dets[0]["bbox"]), 4)

    def test_bad_frame_raises(self):
        with self.assertRaises(ValueError):
            detectors.run("hsv", b"not a jpeg")


if __name__ == "__main__":
    unittest.main()
