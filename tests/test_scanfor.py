"""Plan 034: rover_scan_for — the polling state machine on a scripted fake.
No hardware, no LLM; time.sleep no-op'd and time.monotonic replaced with a
stepping clock so every cap/budget path really runs."""
import json
import time
import unittest
import urllib.error

import agent_chat as ac


TARGET_OBJ = {"id": "scan_new.jpg#0", "name": "suitcase",
              "scan": "scan_new.jpg", "made": "T1", "lon": 20, "lat": 0,
              "bearing": 40.0, "pose": {"x": 0, "y": 0, "heading": 60.0}}


class SeqRover:
    """Sequences with hold-last semantics: pop until one element remains."""

    def __init__(self):
        self.status_seq = ["done"]
        self.scans_seq = [["scan_old.jpg"]]
        self.meta_seq = [None]
        self.objects_seq = [[]]
        self.identify_errors = []          # raised in order, then success
        self.identify_calls = []
        self.start_error = None
        self.started = 0
        self.pose = {"heading": 0.0, "fresh": True}
        self.on_identify = None            # callable run on successful call

    def _pop(self, seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def list_scans(self):
        return list(self._pop(self.scans_seq))

    def start_scan(self):
        self.started += 1
        if self.start_error:
            raise self.start_error

    def get_pano_status(self):
        return {"state": self._pop(self.status_seq), "age_s": 0}

    def scan_meta(self, name):
        m = self._pop(self.meta_seq)
        return dict(m) if m else None

    def get_objects(self):
        return list(self._pop(self.objects_seq))

    def get_pose(self):
        return dict(self.pose)

    def identify_scan(self, name, focus=None):
        self.identify_calls.append((name, focus))
        if self.identify_errors:
            raise self.identify_errors.pop(0)
        if self.on_identify:
            self.on_identify()


def http_409():
    # the REAL production shape: str() carries no reason text
    return urllib.error.HTTPError("http://x/scan_identify/s", 409,
                                  "Conflict", None, None)


class ScanForTest(unittest.TestCase):
    def setUp(self):
        self._sleep, self._mono = time.sleep, time.monotonic
        time.sleep = lambda s: None
        state = {"t": 1000.0}

        def mono():
            state["t"] += 5.0
            return state["t"]
        time.monotonic = mono

    def tearDown(self):
        time.sleep, time.monotonic = self._sleep, self._mono

    def _run(self, r, target="suitcase"):
        return ac.run_tool(r, None, "rover_scan_for", {"target": target})

    def _happy_base(self):
        r = SeqRover()
        r.status_seq = ["scanning", "stitching", "done"]
        r.scans_seq = [["scan_old.jpg"], ["scan_new.jpg", "scan_old.jpg"]]
        return r

    def test_found_by_general_pass_no_focused_identify(self):
        r = self._happy_base()
        r.objects_seq = [[TARGET_OBJ]]                 # auto-identify landed
        out = self._run(r)
        self.assertIn("found 'suitcase'", out)
        self.assertIn("bearing 40", out)
        self.assertIn("~40° left", out)                # 40 − 0, CCW = left
        self.assertEqual(r.identify_calls, [])         # never went focused

    def test_found_by_focused_pass_same_second_made(self):
        r = self._happy_base()
        minimal = {"made": "T1", "objects": []}
        boxed = {"made": "T1",                          # SAME second stamp —
                 "objects": [{"name": "suitcase",       # content must win
                              "lon": 20, "lat": 0}]}
        r.meta_seq = [minimal]
        r.objects_seq = [[]]

        def landed():
            r.meta_seq = [boxed]
            r.objects_seq = [[TARGET_OBJ]]
        r.on_identify = landed
        out = self._run(r)
        self.assertEqual(len(r.identify_calls), 1)
        self.assertEqual(r.identify_calls[0], ("scan_new.jpg", "suitcase"))
        self.assertIn("found 'suitcase'", out)

    def test_identify_busy_both_shapes_then_retry(self):
        for err in (http_409(),
                    RuntimeError("an identify is already running")):
            r = self._happy_base()
            r.meta_seq = [{"made": "T1", "objects": []}]
            r.identify_errors = [err]

            def landed(r=r):
                r.meta_seq = [{"made": "T2", "objects": [
                    {"name": "suitcase", "lon": 20, "lat": 0}]}]
                r.objects_seq = [[TARGET_OBJ]]
            r.on_identify = landed
            out = self._run(r)
            self.assertEqual(len(r.identify_calls), 2, err)   # retried once
            self.assertIn("found 'suitcase'", out)

    def test_non_busy_identify_error_no_retry(self):
        r = self._happy_base()
        r.meta_seq = [{"made": "T1", "objects": []}]
        r.identify_errors = [RuntimeError("boom")]
        out = self._run(r)
        self.assertIn("focused identify refused", out)
        self.assertEqual(len(r.identify_calls), 1)

    def test_scan_refused_reason_echoed(self):
        r = SeqRover()
        r.start_error = RuntimeError("wheels are moving")
        out = self._run(r)
        self.assertIn("scan refused: wheels are moving", out)
        self.assertEqual(r.identify_calls, [])

    def test_scan_failed(self):
        r = SeqRover()
        r.status_seq = ["scanning", "failed"]
        out = self._run(r)
        self.assertIn("failed or was cancelled", out)

    def test_archive_failed(self):
        r = SeqRover()
        r.status_seq = ["scanning", "done"]
        r.scans_seq = [["scan_old.jpg"]]               # never gains the new one
        out = self._run(r)
        self.assertIn("no new 3D view was saved", out)

    def test_newer_scan_interrupts(self):
        r = self._happy_base()
        r.meta_seq = [None]
        # during the general wait a NEWER scan appears → our identify is dead
        r.scans_seq = [["scan_old.jpg"],
                       ["scan_new.jpg", "scan_old.jpg"],
                       ["scan_newer.jpg", "scan_new.jpg", "scan_old.jpg"]]
        out = self._run(r)
        self.assertIn("newer scan interrupted", out)

    def test_meta_none_throughout_general_pass_proceeds(self):
        r = self._happy_base()
        r.meta_seq = [None]                            # sidecar write failed

        def landed():
            r.meta_seq = [{"made": "T2", "objects": [
                {"name": "suitcase", "lon": 20, "lat": 0}]}]
            r.objects_seq = [[TARGET_OBJ]]
        r.on_identify = landed
        out = self._run(r)
        self.assertIn("found 'suitcase'", out)         # no crash on None

    def test_never_found_bounded_by_budget(self):
        r = self._happy_base()
        r.meta_seq = [None]                            # nothing ever lands
        out = self._run(r)
        self.assertIn("couldn't find 'suitcase'", out)
        self.assertIn("drive the rover somewhere else", out)

    def test_stuck_scan_bounded(self):
        r = SeqRover()
        r.status_seq = ["scanning"]                    # never terminates
        out = self._run(r)
        self.assertIn("taking too long", out)


class StartScanClientTest(unittest.TestCase):
    """Client-level pin (codex review ask): the 409 body's error field is
    the ONLY place the refusal reason lives — start_scan must surface it."""

    def _with_post_raising(self, err):
        import rovercontrol_client as rcc
        orig = rcc._post
        rcc._post = lambda *a, **k: (_ for _ in ()).throw(err)
        try:
            with self.assertRaises(RuntimeError) as cm:
                rcc.start_scan()
        finally:
            rcc._post = orig
        return str(cm.exception)

    def test_409_body_reason_parsed(self):
        import io
        err = urllib.error.HTTPError(
            "http://x/scan", 409, "Conflict", None,
            io.BytesIO(b'{"ok": false, "error": "wheels are moving"}'))
        self.assertEqual(self._with_post_raising(err), "wheels are moving")

    def test_409_unreadable_body_falls_back_to_busy(self):
        err = urllib.error.HTTPError("http://x/scan", 409, "Conflict",
                                     None, None)
        self.assertEqual(self._with_post_raising(err), "busy")


if __name__ == "__main__":
    unittest.main()
