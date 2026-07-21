"""Plan 031: chatbot auto-flashlight + the rover_identify_scan tool — fakes
only, no LLM, no hardware."""
import time
import unittest

import agent_chat as ac


class FlashRover:
    """Records every call; behavior knobs for each test."""
    def __init__(self, state=None, allowed=True, dark=True,
                 photo_raises=False):
        self.calls = []
        self._state = state
        self._allowed = allowed
        self._photo_raises = photo_raises
        self.dark = dark

    def light_state(self):
        self.calls.append("light_state")
        return dict(self._state) if self._state is not None else None

    def auto_flash_allowed(self):
        self.calls.append("allowed")
        return self._allowed

    def get_stream_frame(self):
        self.calls.append("grab")
        return b"\xff\xd8DARK" if self.dark else b"\xff\xd8BRIGHT"

    def lights(self, front, base):
        self.calls.append(("lights", front, base))

    def photo(self):
        self.calls.append("photo")
        if self._photo_raises:
            raise OSError("camera busy")
        return "photos/p.jpg"


def fake_mean(dark_value=20.0, bright_value=140.0):
    def f(jpeg):
        return dark_value if b"DARK" in jpeg else bright_value
    return f


class AutoFlashTest(unittest.TestCase):
    def setUp(self):
        self._orig = ac._frame_mean

    def tearDown(self):
        ac._frame_mean = self._orig

    def test_dark_flash_and_exact_restore(self):
        ac._frame_mean = fake_mean()
        r = FlashRover(state={"head": True, "base": False}, dark=True)
        p, flashed, rf = ac.photo_with_autoflash(r)
        self.assertEqual((p, flashed, rf), ("photos/p.jpg", True, False))
        self.assertIn(("lights", 255, 255), r.calls)          # flash on
        self.assertEqual(r.calls[-1], ("lights", 255, 0))     # EXACT prior state
        self.assertLess(r.calls.index("photo"), r.calls.index(("lights", 255, 0)))

    def test_kill_switch_off_never_touches_lights(self):
        ac._frame_mean = fake_mean()
        r = FlashRover(state={"head": False, "base": False},
                       allowed=False, dark=True)
        p, flashed, _ = ac.photo_with_autoflash(r)
        self.assertFalse(flashed)
        self.assertFalse(any(isinstance(c, tuple) for c in r.calls))

    def test_bright_scene_no_flash(self):
        ac._frame_mean = fake_mean()
        r = FlashRover(state={"head": False, "base": False}, dark=False)
        _, flashed, _ = ac.photo_with_autoflash(r)
        self.assertFalse(flashed)
        self.assertFalse(any(isinstance(c, tuple) for c in r.calls))

    def test_unknowable_light_state_skips(self):
        ac._frame_mean = fake_mean()
        r = FlashRover(state=None, dark=True)      # serial backend: no healthz
        _, flashed, _ = ac.photo_with_autoflash(r)
        self.assertFalse(flashed)
        self.assertNotIn("grab", r.calls)          # didn't even measure

    def test_luma_failure_means_no_light_command(self):
        ac._frame_mean = lambda jpeg: None         # cv2 missing / decode fail
        r = FlashRover(state={"head": False, "base": False}, dark=True)
        p, flashed, _ = ac.photo_with_autoflash(r)
        self.assertEqual(p, "photos/p.jpg")
        self.assertFalse(any(isinstance(c, tuple) for c in r.calls))

    def test_restore_is_finally_guaranteed_on_photo_failure(self):
        ac._frame_mean = fake_mean()
        r = FlashRover(state={"head": False, "base": True},
                       dark=True, photo_raises=True)
        with self.assertRaises(OSError):
            ac.photo_with_autoflash(r)
        self.assertEqual(r.calls[-1], ("lights", 0, 255))     # restored anyway


class RealBackendSurfaceTest(unittest.TestCase):
    def test_roverctl_has_every_method_the_flash_path_calls(self):
        # the fake rover masked a missing RoverCtl.get_stream_frame once
        # (code-review catch) — pin the real surface
        import rover_backend
        for m in ("light_state", "auto_flash_allowed", "get_stream_frame",
                  "lights", "photo", "list_scans", "scan_meta",
                  "identify_scan"):
            self.assertTrue(hasattr(rover_backend.RoverCtl, m), m)
        import rovercontrol_client
        for f in ("get_auto_flash", "get_stream_frame", "healthz",
                  "list_scans", "scan_meta", "identify_scan"):
            self.assertTrue(hasattr(rovercontrol_client, f), f)


class IdentifyToolTest(unittest.TestCase):
    class ScanRover:
        def __init__(self):
            self.identified = []
            self.metas = {"scan_b.jpg": {"made": "t0", "objects": []}}

        def list_scans(self):
            return ["scan_c.jpg", "scan_b.jpg", "scan_a.jpg"]   # newest first

        def scan_meta(self, name):
            return self.metas.get(name)

        def identify_scan(self, name, focus=None):
            self.identified.append((name, focus))
            self.metas[name] = {"made": "t1", "objects": [
                {"name": "books", "lon": 1, "lat": 2, "w": 3, "h": 4},
                {"name": "shelf", "lon": 5, "lat": 6, "w": 7, "h": 8}]}

    def test_second_last_resolution_and_focus(self):
        r = self.ScanRover()
        orig_sleep = time.sleep
        time.sleep = lambda s: None                # skip the 5s poll waits
        try:
            out = ac.run_tool(r, None, "rover_identify_scan",
                              {"which": 2, "focus": "stack of books"})
        finally:
            time.sleep = orig_sleep
        self.assertEqual(r.identified, [("scan_b.jpg", "stack of books")])
        self.assertIn("books", out)
        self.assertIn("shelf", out)

    def test_out_of_range(self):
        r = self.ScanRover()
        out = ac.run_tool(r, None, "rover_identify_scan", {"which": 9})
        self.assertIn("out of range", out)

    def test_no_scans(self):
        class Empty(self.ScanRover):
            def list_scans(self):
                return []
        out = ac.run_tool(Empty(), None, "rover_identify_scan", {"which": 1})
        self.assertIn("no saved 3D scans", out)


if __name__ == "__main__":
    unittest.main()
