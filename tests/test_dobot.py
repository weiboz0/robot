"""No-hardware tests for the Dobot client (mocked sockets)."""
import socket
import unittest

import dobot


class FakeSock:
    def __init__(self):
        self.sent = []

    def sendall(self, b):
        self.sent.append(b.decode())

    def recv(self, n):
        return b"0,{},Reply();"

    def settimeout(self, t):
        pass

    def close(self):
        pass


class DobotTest(unittest.TestCase):
    def setUp(self):
        self.socks = []
        self._orig = socket.create_connection

        def fake_conn(addr, timeout=None):
            s = FakeSock()
            self.socks.append((addr, s))
            return s

        socket.create_connection = fake_conn
        self.d = dobot.Dobot()

    def tearDown(self):
        socket.create_connection = self._orig

    def test_connects_to_dashboard_then_motion_ports(self):
        self.assertEqual([a for a, _ in self.socks],
                         [(dobot.DOBOT_HOST, 29999), (dobot.DOBOT_HOST, 30003)])

    def test_queries_go_to_dashboard(self):
        self.d.mode()
        self.d.pose()
        self.assertIn("RobotMode()", self.socks[0][1].sent)
        self.assertIn("GetPose()", self.socks[0][1].sent)

    def test_motion_goes_to_move_port_and_formats(self):
        self.d.move_j(300, 0, 50, 0)
        self.assertIn("MovJ(300,0,50,0)", self.socks[1][1].sent)
        self.assertEqual(self.socks[0][1].sent, [])  # nothing on dashboard

    def test_enable_clear(self):
        self.d.enable()
        self.d.clear_error()
        self.assertIn("EnableRobot()", self.socks[0][1].sent)
        self.assertIn("ClearError()", self.socks[0][1].sent)


if __name__ == "__main__":
    unittest.main()
