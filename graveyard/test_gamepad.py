"""No-hardware tests for the laptop gamepad controller (rover_gamepad.py):
pure compute_step logic, the prioritized Sender, the Dispatcher gating, and
rover_client host/timeout overrides. No pygame, no real HTTP."""
import collections
import dataclasses
import unittest

import rover_client
import rover_gamepad as g


def pad(axes=(0.0,) * 6, buttons=(0,) * 12, hats=((0, 0),)):
    return g.PadState(axes=tuple(axes), buttons=tuple(buttons), hats=tuple(hats))


def btn(i, n=12):
    b = [0] * n
    b[i] = 1
    return tuple(b)


class ComputeStepTest(unittest.TestCase):
    def step(self, state, prev=None, ctrl=None, dt=1.0):
        return g.compute_step(state, prev or pad(), ctrl or g.ControlState(), dt)

    def test_deadzone(self):
        s, c = self.step(pad(axes=(0.1, 0.1, 0, 0, 0, 0)))  # below DEADZONE
        self.assertEqual((s.left, s.right), (0.0, 0.0))

    def test_forward_mix_equal_wheels(self):
        s, _ = self.step(pad(axes=(0.0, -1.0, 0, 0, 0, 0)))  # full throttle up
        self.assertGreater(s.left, 0)
        self.assertAlmostEqual(s.left, s.right)

    def test_steer_opposite_signs(self):
        s, _ = self.step(pad(axes=(1.0, 0.0, 0, 0, 0, 0)))   # pure steer
        self.assertGreater(s.left, 0)
        self.assertLess(s.right, 0)

    def test_ramp_limits_one_tick(self):
        # small dt → slew limit RAMP*dt caps the change well below the target
        s, _ = self.step(pad(axes=(0.0, -1.0, 0, 0, 0, 0)), dt=1.0 / 25)
        self.assertAlmostEqual(s.left, g.RAMP * (1.0 / 25), places=6)
        self.assertLess(s.left, g.SPEED_STEPS[g.SPEED_START])

    def test_dpad_changes_speed_cap(self):
        up = pad(hats=((0, 1),))
        _, c = self.step(up)                    # rising edge from neutral hat
        self.assertEqual(c.speed_idx, g.SPEED_START + 1)
        # no re-trigger while still held
        _, c2 = g.compute_step(up, up, c, 1.0)
        self.assertEqual(c2.speed_idx, c.speed_idx)

    def test_turbo_overrides_cap(self):
        s, _ = self.step(pad(axes=(0.0, -1.0, 0, 0, 0, 0), buttons=btn(g.BTN_RB)))
        self.assertAlmostEqual(s.left, g.TURBO_SPEED)

    def test_camera_integrates_and_clamps(self):
        s, c = self.step(pad(axes=(0, 0, 0, 1.0, -1.0, 0)))  # RX right, RY up
        self.assertAlmostEqual(s.pan, g.PAN_RATE)            # 90*dt(=1)
        self.assertAlmostEqual(s.tilt, g.TILT_RATE)
        self.assertEqual((c.pan, c.tilt), (s.pan, s.tilt))

    def test_center_button(self):
        ctrl = dataclasses.replace(g.ControlState(), pan=50.0, tilt=20.0)
        s, c = self.step(pad(buttons=btn(g.BTN_Y)), ctrl=ctrl)
        self.assertTrue(s.center)
        self.assertEqual((c.pan, c.tilt), (0.0, 0.0))

    def test_button_one_shots(self):
        self.assertTrue(self.step(pad(buttons=btn(g.BTN_A)))[0].stop)
        self.assertTrue(self.step(pad(buttons=btn(g.BTN_B)))[0].snapshot)
        self.assertTrue(self.step(pad(buttons=btn(g.BTN_L3)))[0].relax)
        self.assertTrue(self.step(pad(buttons=btn(g.BTN_R3)))[0].lock)
        self.assertTrue(self.step(pad(buttons=btn(g.BTN_START)))[0].quit)

    def test_light_toggle_is_edge_only(self):
        x = pad(buttons=btn(g.BTN_X))
        s, c = self.step(x)
        self.assertTrue(s.head_on and s.light_changed)
        s2, _ = g.compute_step(x, x, c, 1.0)     # still held → no re-toggle
        self.assertFalse(s2.light_changed)
        self.assertTrue(s2.head_on)

    def test_estop_latches_until_recenter(self):
        # press Back WHILE driving → estop fires and the latch engages (stick held)
        held = pad(axes=(0, -1.0, 0, 0, 0, 0))                      # throttle up
        back = pad(axes=(0, -1.0, 0, 0, 0, 0), buttons=btn(g.BTN_BACK))
        s, c = g.compute_step(back, held, g.ControlState(), 1.0)
        self.assertTrue(s.estop and c.estopped)
        self.assertEqual((s.left, s.right), (0.0, 0.0))
        # still held next tick → no drive, still latched
        s2, c2 = g.compute_step(held, back, c, 1.0)
        self.assertEqual((s2.left, s2.right), (0.0, 0.0))
        self.assertTrue(c2.estopped)
        # sticks centered → latch releases
        _, c3 = g.compute_step(pad(), held, c2, 1.0)
        self.assertFalse(c3.estopped)

    def test_purity_no_input_mutation(self):
        ctrl = g.ControlState(speed_idx=3, pan=10.0)
        before = dataclasses.astuple(ctrl)
        g.compute_step(pad(buttons=btn(g.BTN_X)), pad(), ctrl, 1.0)
        self.assertEqual(dataclasses.astuple(ctrl), before)


class FakeClient:
    def __init__(self):
        self.calls = []

    def move(self, l, r): self.calls.append(("move", l, r))
    def set_camera(self, p, t): self.calls.append(("cam", p, t))
    def stop(self): self.calls.append(("stop",))
    def estop(self): self.calls.append(("estop",))
    def lights(self, f, b): self.calls.append(("lights", f, b))
    def servo_torque(self, lock): self.calls.append(("torque", lock))


class SenderTest(unittest.TestCase):
    def test_emergency_clears_pending_drive(self):
        s = g.Sender(FakeClient())            # not started: inspect slots directly
        s.set_drive(0.3, 0.3)
        self.assertEqual(s._drive, (0.3, 0.3))
        s.emergency("stop")
        self.assertIsNone(s._drive)           # queued motion discarded
        self.assertEqual(list(s._urgent), ["stop"])

    def test_urgent_dispatch_mapping(self):
        c = FakeClient()
        s = g.Sender(c)
        s._dispatch_urgent("estop"); s._dispatch_urgent("stop")
        s._dispatch_urgent(("lights", 255, 0))
        s._dispatch_urgent("relax"); s._dispatch_urgent("lock")
        self.assertEqual(c.calls, [("estop",), ("stop",), ("lights", 255, 0),
                                   ("torque", False), ("torque", True)])

    def test_shutdown_estop_is_last_command(self):
        import time
        c = FakeClient()
        s = g.Sender(c)
        s.start()
        s.set_drive(0.2, 0.2)
        s.emergency("estop")          # discard pending drive, queue estop
        s.shutdown()
        s.join(timeout=2.0)
        self.assertIn(("estop",), c.calls)
        self.assertEqual(c.calls[-1], ("estop",))   # never a move after the estop

    def test_nonblocking_and_eventually_sends(self):
        import threading, time
        gate = threading.Event()

        class SlowClient(FakeClient):
            def move(self, l, r):
                gate.wait(1.0)                # simulate a slow link
                super().move(l, r)

        c = SlowClient()
        s = g.Sender(c)
        s.start()
        t0 = time.monotonic()
        s.set_drive(0.1, 0.1)                 # must NOT block on the slow send
        self.assertLess(time.monotonic() - t0, 0.1)
        gate.set()
        time.sleep(0.2)
        self.assertIn(("move", 0.1, 0.1), c.calls)
        s.shutdown()


class FakeSender:
    def __init__(self):
        self.calls = []

    def emergency(self, item): self.calls.append(("emergency", item))
    def urgent(self, item): self.calls.append(("urgent", item))
    def set_drive(self, l, r): self.calls.append(("drive", l, r))
    def set_camera(self, p, t): self.calls.append(("cam", round(p, 1), round(t, 1)))


class DispatcherTest(unittest.TestCase):
    def test_estop_preempts_and_marks_drive(self):
        fs = FakeSender()
        d = g.Dispatcher(fs)
        d.dispatch(g.Step(estop=True), now=0.0)
        self.assertEqual(fs.calls, [("emergency", "estop")])

    def test_drive_change_gating_and_heartbeat(self):
        fs = FakeSender()
        d = g.Dispatcher(fs)
        d.dispatch(g.Step(left=0.2, right=0.2), now=0.0)     # change → send
        d.dispatch(g.Step(left=0.2, right=0.2), now=0.01)    # same, < period → no
        self.assertEqual(fs.calls, [("drive", 0.2, 0.2)])
        d.dispatch(g.Step(left=0.2, right=0.2), now=1.0)     # heartbeat (period passed)
        self.assertEqual(fs.calls[-1], ("drive", 0.2, 0.2))
        self.assertEqual(len(fs.calls), 2)

    def test_neutral_sends_stop_value_once(self):
        fs = FakeSender()
        d = g.Dispatcher(fs)
        d.dispatch(g.Step(left=0.2, right=0.2), now=0.0)
        d.dispatch(g.Step(left=0.0, right=0.0), now=0.1)     # change to neutral → send
        d.dispatch(g.Step(left=0.0, right=0.0), now=0.2)     # still neutral → no send
        self.assertEqual(fs.calls, [("drive", 0.2, 0.2), ("drive", 0.0, 0.0)])

    def test_nonzero_drive_is_rate_limited(self):
        fs = FakeSender()
        d = g.Dispatcher(fs)
        d.dispatch(g.Step(left=0.1, right=0.1), now=0.0)    # first → send
        d.dispatch(g.Step(left=0.2, right=0.2), now=0.02)   # < period → suppress
        d.dispatch(g.Step(left=0.3, right=0.3), now=0.04)   # < period → suppress
        self.assertEqual(fs.calls, [("drive", 0.1, 0.1)])
        d.dispatch(g.Step(left=0.3, right=0.3), now=1.0)    # due → latest value
        self.assertEqual(fs.calls[-1], ("drive", 0.3, 0.3))

    def test_lights_urgent(self):
        fs = FakeSender()
        g.Dispatcher(fs).dispatch(g.Step(light_changed=True, head_on=True), now=0.0)
        self.assertIn(("urgent", ("lights", 255, 0)), fs.calls)


class RoverClientOverrideTest(unittest.TestCase):
    def setUp(self):
        self._host, self._to = rover_client.ROVER_HOST, rover_client._TIMEOUT

    def tearDown(self):
        rover_client.set_host(self._host)
        rover_client.set_timeout(self._to)

    def test_set_host_changes_post_url(self):
        captured = {}

        class FakeResp:
            def read(self): return b""

        def fake_urlopen(url, data=None, timeout=None):
            captured["url"], captured["timeout"] = url, timeout
            return FakeResp()

        orig = rover_client.urllib.request.urlopen
        rover_client.urllib.request.urlopen = fake_urlopen
        try:
            rover_client.set_host("10.0.0.9")
            rover_client.set_timeout(0.5)
            rover_client.stop()
            self.assertIn("10.0.0.9", captured["url"])
            self.assertEqual(captured["timeout"], 0.5)
        finally:
            rover_client.urllib.request.urlopen = orig


if __name__ == "__main__":
    unittest.main()
