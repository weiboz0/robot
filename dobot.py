"""Dobot MG400 control over the TCP-IP protocol.

Dashboard commands (enable, query, clear error) go to port 29999; motion commands
(MovJ/MovL) go to port 30003. Replies look like `ErrorID,{value},Command();` — an
ErrorID of 0 is success; -1 usually means the arm is in Local mode (not Remote/TCP),
so unlock TCP control in DobotStudio Pro first.
"""
import socket

DOBOT_HOST = "192.168.1.6"
DASH_PORT = 29999
MOVE_PORT = 30003


def reachable(host: str = DOBOT_HOST, port: int = DASH_PORT, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Dobot:
    def __init__(self, host: str = DOBOT_HOST, timeout: float = 5.0):
        self.host = host
        self.dash = socket.create_connection((host, DASH_PORT), timeout=timeout)
        try:
            self.move = socket.create_connection((host, MOVE_PORT), timeout=timeout)
        except OSError:                 # don't leak the dashboard socket if motion fails
            self.dash.close()
            raise
        self.dash.settimeout(timeout)
        self.move.settimeout(timeout)

    def _send(self, sock: socket.socket, cmd: str) -> str:
        sock.sendall(cmd.encode())
        try:
            return sock.recv(2048).decode(errors="replace").strip()
        except socket.timeout:
            return "(no reply)"

    # -- raw channels -------------------------------------------------------
    def dashboard(self, cmd: str) -> str:
        return self._send(self.dash, cmd)

    def motion(self, cmd: str) -> str:
        return self._send(self.move, cmd)

    # -- queries (read-only) ------------------------------------------------
    def mode(self) -> str:
        return self.dashboard("RobotMode()")

    def pose(self) -> str:
        return self.dashboard("GetPose()")

    # -- state --------------------------------------------------------------
    def enable(self) -> str:
        return self.dashboard("EnableRobot()")

    def disable(self) -> str:
        return self.dashboard("DisableRobot()")

    def clear_error(self) -> str:
        return self.dashboard("ClearError()")

    def speed_factor(self, percent: int) -> str:
        return self.dashboard(f"SpeedFactor({int(percent)})")

    # -- motion -------------------------------------------------------------
    def move_j(self, x: float, y: float, z: float, r: float) -> str:
        """Joint move to Cartesian pose (mm, deg)."""
        return self.motion(f"MovJ({x},{y},{z},{r})")

    def move_l(self, x: float, y: float, z: float, r: float) -> str:
        """Linear move to Cartesian pose (mm, deg)."""
        return self.motion(f"MovL({x},{y},{z},{r})")

    def close(self) -> None:
        for s in (self.move, self.dash):
            try:
                s.close()
            except OSError:
                pass
