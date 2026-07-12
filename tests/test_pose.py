"""Pose dead reckoning + telemetry (plan 026) — pure math and parser tests,
plus the reader thread's shutdown/flood behavior on fakes. No hardware.
Heading is DIFFERENTIAL ODOMETRY (the firmware's streamed gyro is broken —
see the plan's discovery section); everything derives from wheel encoders."""
import json
import math
import os
import threading
import time
import unittest

import rovercontrold as rc


class PoseMathTest(unittest.TestCase):
    def test_straight_line_cm_to_m(self):
        p = rc.Pose()
        p.update(0, 0)
        for i in range(1, 11):                     # +2 cm per wheel per sample
            p.update(i * 2, i * 2)
        self.assertAlmostEqual(p.x, 0.20, places=6)
        self.assertAlmostEqual(p.y, 0.0)
        self.assertAlmostEqual(p.heading, 0.0)

    def test_reverse_integrates_negative_no_freeze(self):
        p = rc.Pose()
        p.update(0, 0)
        for i in range(1, 11):                     # backing up: signed negative
            p.update(-i * 2, -i * 2)
        self.assertAlmostEqual(p.x, -0.20, places=6)
        self.assertAlmostEqual(p.heading, 0.0)     # straight back: no turn

    def test_turn_in_place_heading_no_translation(self):
        p = rc.Pose()
        p.update(0, 0)
        # spin CCW: right wheel forward, left back, symmetric
        w_cm = rc.TRACK_WIDTH_M * rc.SKID_FACTOR * 100
        quarter_arc = (math.pi / 2) * w_cm / 2     # per wheel for a 90° spin
        steps = 10
        for i in range(1, steps + 1):
            d = quarter_arc * i / steps
            p.update(-d, d)
        self.assertAlmostEqual(p.heading, 90.0, delta=0.5)
        self.assertAlmostEqual(p.x, 0.0, places=3)
        self.assertAlmostEqual(p.y, 0.0, places=3)

    def test_heading_wraps_to_signed_180(self):
        p = rc.Pose()
        p.update(0, 0)
        w_cm = rc.TRACK_WIDTH_M * rc.SKID_FACTOR * 100
        full_arc = (2 * math.pi) * w_cm / 2        # 360° spin in 40 steps
        for i in range(1, 41):
            d = full_arc * i / 40
            p.update(-d, d)
        self.assertAlmostEqual(p.heading, 0.0, delta=1.0)   # wrapped around
        self.assertLessEqual(abs(p.heading), 180.0)

    def test_position_follows_heading(self):
        p = rc.Pose()
        p.update(0, 0)
        p.heading = 90.0                           # facing +Y
        for i in range(1, 11):
            p.update(i * 2, i * 2)
        self.assertAlmostEqual(p.x, 0.0, places=6)
        self.assertAlmostEqual(p.y, 0.20, places=6)

    def test_arc_curves(self):
        p = rc.Pose()
        p.update(0, 0)
        for i in range(1, 21):                     # right wheel faster → CCW arc
            p.update(i * 1, i * 2)
        self.assertGreater(p.heading, 5.0)
        self.assertGreater(p.x, 0.0)
        self.assertGreater(p.y, 0.0)               # curled toward +Y

    def test_counter_reset_rebaselines_no_jump(self):
        p = rc.Pose()
        p.update(5000, 5000)
        p.update(5010, 5010)                       # normal step
        x_before = p.x
        p.update(3, 2)                             # ESP32 rebooted: snap to ~0
        self.assertEqual(p.x, x_before)            # held, no phantom -49.9 m
        p.update(5, 4)                             # next delta integrates again
        self.assertAlmostEqual(p.x, x_before + 0.02, places=6)

    def test_reset_zeroes_pose_keeps_baselines(self):
        p = rc.Pose()
        p.update(100, 100)
        p.update(110, 110)
        p.reset()
        self.assertEqual((p.x, p.y, p.heading), (0.0, 0.0, 0.0))
        p.update(112, 112)                         # deltas keep working
        self.assertAlmostEqual(p.x, 0.02, places=6)

    def test_rebaseline_holds_pose(self):
        p = rc.Pose()
        p.update(0, 0)
        p.update(10, 10)
        x = p.x
        p.rebaseline()                             # link republished
        p.update(99999, 99999)                     # new counters: baseline only
        self.assertEqual(p.x, x)

    def test_snapshot_freshness(self):
        p = rc.Pose()
        self.assertFalse(p.snapshot(now=0.0)["fresh"])
        p.update(0, 0, now=100.0)
        self.assertTrue(p.snapshot(now=100.5)["fresh"])
        self.assertFalse(p.snapshot(now=100.0 + rc.POSE_FRESH_S + 0.1)["fresh"])


class ParseFeedbackTest(unittest.TestCase):
    def test_good_line(self):
        fb = rc.parse_feedback(
            b'{"T":1001,"L":0,"R":0,"gx":1,"gy":2,"gz":-89,"odl":56,"odr":20,'
            b'"v":1216,"pan":-179.9,"tilt":0.08}')
        self.assertEqual((fb["odl"], fb["odr"]), (56.0, 20.0))
        self.assertAlmostEqual(fb["v"], 12.16)
        self.assertAlmostEqual(fb["pan"], -179.9)

    def test_missing_optional_fields(self):
        fb = rc.parse_feedback(b'{"T":1001,"odl":1,"odr":2}')
        self.assertIsNone(fb["v"])
        self.assertIsNone(fb["pan"])

    def test_rejects(self):
        for bad in (b"junk", b"{}", b'{"T":1005,"id":2}', b'[1,2]',
                    b'{"T":1001,"odl":"x","odr":1}', b'{"T":1001}', b""):
            self.assertIsNone(rc.parse_feedback(bad), bad)


class FakeReadLink:
    """Feeds canned bytes through a real pipe so select() works on it."""
    def __init__(self):
        self._r, self._w = os.pipe()
        self.fd = self._r

    def feed(self, data):
        os.write(self._w, data)

    def read(self, n=4096):
        return os.read(self._r, n)

    def write(self, data):
        pass

    def close(self):
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass


def fb_line(odl, odr, v=1200):
    return (json.dumps({"T": 1001, "odl": odl, "odr": odr, "gz": 0,
                        "v": v, "pan": 0, "tilt": 0}) + "\n").encode()


class TelemetryReaderTest(unittest.TestCase):
    def _run(self, link, pose, feed_fn, timeout=5.0):
        rover = rc.Rover()
        rover.set_status(link, "")
        stop = threading.Event()
        th = threading.Thread(target=rc.run_telemetry,
                              args=(rover, pose, stop), daemon=True)
        th.start()
        try:
            feed_fn()
        finally:
            stop.set()
            th.join(timeout)
            self.assertFalse(th.is_alive(), "reader did not exit promptly")
            link.close()

    def test_reads_lines_and_updates_pose(self):
        link, pose = FakeReadLink(), rc.Pose()

        def feed():
            for i in range(3):
                link.feed(fb_line(i, i))
            deadline = time.time() + 3
            while pose.snapshot()["battery_v"] is None and time.time() < deadline:
                time.sleep(0.01)
        self._run(link, pose, feed)
        self.assertAlmostEqual(pose.snapshot()["battery_v"], 12.0)

    def test_partial_line_across_reads(self):
        link, pose = FakeReadLink(), rc.Pose()
        whole = fb_line(1, 1, v=999)

        def feed():
            link.feed(whole[:10])
            time.sleep(0.1)
            link.feed(whole[10:])
            deadline = time.time() + 3
            while pose.snapshot()["battery_v"] is None and time.time() < deadline:
                time.sleep(0.01)
        self._run(link, pose, feed)
        self.assertAlmostEqual(pose.snapshot()["battery_v"], 9.99)

    def test_junk_flood_does_not_break_reader(self):
        link, pose = FakeReadLink(), rc.Pose()

        def feed():
            link.feed(b"garbage with no newline " * 100)
            link.feed(b"\x00\xff" * 500 + b"\n")
            link.feed(fb_line(1, 1, v=777))
            deadline = time.time() + 3
            while pose.snapshot()["battery_v"] is None and time.time() < deadline:
                time.sleep(0.01)
        self._run(link, pose, feed)
        self.assertAlmostEqual(pose.snapshot()["battery_v"], 7.77)

    def test_flood_does_not_delay_serial_writes(self):
        link, pose = FakeReadLink(), rc.Pose()
        rover = rc.Rover()
        rover.set_status(link, "")
        stop = threading.Event()
        th = threading.Thread(target=rc.run_telemetry,
                              args=(rover, pose, stop), daemon=True)
        th.start()
        try:
            worst = 0.0
            for i in range(50):                    # flood + write interleaved
                link.feed(fb_line(i, i) * 20)
                t0 = time.monotonic()
                rover.send({"T": 1})               # the drive path's send
                worst = max(worst, time.monotonic() - t0)
            self.assertLess(worst, 0.1, "write path stalled by telemetry")
        finally:
            stop.set()
            th.join(5)
            link.close()

    def test_ttylink_sets_vmin_vtime(self):
        import pty
        import termios
        master, slave = pty.openpty()
        try:
            link = rc.TTYLink(os.ttyname(slave))
            attrs = termios.tcgetattr(link.fd)
            self.assertEqual(attrs[6][termios.VMIN], 0)
            self.assertEqual(attrs[6][termios.VTIME], 1)
            link.close()
        finally:
            for fd in (master, slave):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_exits_when_no_link(self):
        rover = rc.Rover()                          # link None
        stop = threading.Event()
        th = threading.Thread(target=rc.run_telemetry,
                              args=(rover, rc.Pose(), stop), daemon=True)
        th.start()
        time.sleep(0.1)
        stop.set()
        th.join(3)
        self.assertFalse(th.is_alive())


if __name__ == "__main__":
    unittest.main()
