"""Shared, auto-detecting rover-control backend.

One interface over three transports so the chatbot (agent_chat.py) can "just call
the functions" wherever it runs:
  - **serial**       — direct UART to the ESP32 via rover_direct, ON the rover.
  - **rovercontrol** — the Go controller's :8080 API (it owns the camera + the
    Pi-attached joystick); preferred when running remotely.
  - **http**         — the legacy ugv_rpi app.py :5000 API (fallback).

`connect()` picks serial if the board's serial device opens, else rovercontrol if
its :8080 /healthz reports serial up, else app.py if :5000 is reachable. Import-safe
anywhere (rover_direct is imported lazily on the serial path only).
"""
import os
import socket

import rover_client          # stdlib-only (urllib); safe to import anywhere
import rovercontrol_client   # stdlib-only; the :8080 client
import rover_camera          # stdlib-only; safe to import anywhere

ROVER_HTTP_HOST, ROVER_HTTP_PORT = "192.168.1.131", 5000
ROVERCONTROL_PORT = 8080


def _reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class RoverCtl:
    """Common rover interface over the serial / rovercontrol / http backends.

    The two HTTP backends (rovercontrol, http) share rover_client's function
    surface, so for everything except OLED/photo the non-serial path just calls
    `self._http.<fn>` against whichever client module is selected.
    """

    def __init__(self, backend: str, port: str = None, http_host: str = None):
        self.backend = backend          # "serial" | "rovercontrol" | "http"
        self.pan = 0.0
        self.tilt = 0.0
        if backend == "serial":
            import rover_direct
            self._rd = rover_direct
            if rover_direct.stop_http_service():   # frees the port from legacy app.py only
                print("stopped ugv_rpi/app.py to free the serial port.")
            self._r = rover_direct.Rover(port=port)
            self.where = self._r.port
            self._http = None
        else:
            self._http_host = http_host or ROVER_HTTP_HOST
            if backend == "rovercontrol":
                self._http = rovercontrol_client
                self._http_port = ROVERCONTROL_PORT
            else:                                   # "http" (app.py)
                self._http = rover_client
                self._http_port = ROVER_HTTP_PORT
            self._http.set_host(self._http_host)
            self.where = f"http://{self._http_host}:{self._http_port}"

    def set_camera(self, pan, tilt):
        self.pan = _clamp(float(pan), -180.0, 180.0)
        self.tilt = _clamp(float(tilt), -45.0, 90.0)
        if self.backend == "serial":
            self._r.set_camera(self.pan, self.tilt)
        else:
            self._http.set_camera(self.pan, self.tilt)

    def drive(self, left, right, seconds):
        left = _clamp(float(left), -0.5, 0.5)
        right = _clamp(float(right), -0.5, 0.5)
        seconds = _clamp(float(seconds), 0.0, 5.0)
        if self.backend == "serial":
            self._r.drive_for(left, right, seconds)
        else:
            self._http.drive(left, right, seconds)

    def move(self, left, right):
        left = _clamp(float(left), -0.5, 0.5)
        right = _clamp(float(right), -0.5, 0.5)
        if self.backend == "serial":
            self._r.drive(left, right)
        else:
            self._http.move(left, right)

    def stop(self):
        if self.backend == "serial":
            self._r.stop()
        else:
            self._http.stop()

    def estop(self):
        if self.backend == "serial":
            self._r.estop()
        else:
            self._http.estop()

    def lights(self, front=0, base=0):
        front = int(_clamp(float(front), 0, 255))
        base = int(_clamp(float(base), 0, 255))
        if self.backend == "serial":
            self._r.lights(front, base)
        else:
            self._http.lights(front, base)   # rovercontrol degrades PWM → on/off
        return front, base

    def set_torque(self, lock):
        if self.backend == "serial":
            self._r.servo_torque(lock)
        else:
            self._http.servo_torque(lock)

    # alias so callers using either name work (agent_chat: set_torque; gamepad: servo_torque)
    servo_torque = set_torque

    def oled(self, line, text):
        if self.backend == "serial":
            self._r.oled(int(line), text)
        elif self.backend == "http":
            rover_client.oled(int(line), text)
        else:
            raise NotImplementedError("OLED is not available via rovercontrol (:8080)")

    def oled_default(self):
        if self.backend == "serial":
            self._r.oled_default()
        elif self.backend == "http":
            rover_client.oled_default()
        else:
            raise NotImplementedError("OLED is not available via rovercontrol (:8080)")

    def center(self):
        self.set_camera(0, 0)

    def photo(self):
        # serial (Pi, app down) → rpicam-still fallback; HTTP backends → grab one
        # frame from that backend's MJPEG stream (app.py :5000 or rovercontrol :8080).
        if self.backend == "serial":
            return rover_camera.take_photo(wait=True, host="127.0.0.1")
        return rover_camera.take_photo(wait=True, host=self._http_host, port=self._http_port)

    def demo(self):
        # motor + camera self-test (backend-agnostic, mirrors rover_direct.demo)
        import time
        self.set_camera(0, 45); time.sleep(2)
        self.set_camera(0, -30); time.sleep(2)
        self.set_camera(-45, 0); time.sleep(2)
        self.center(); time.sleep(1)
        self.drive(0.15, 0.15, 0.6)   # nudge forward
        self.drive(0.2, -0.2, 0.5)    # spin right

    def close(self):
        if self.backend == "serial":
            self._r.close()


def _rovercontrol_ready(host: str) -> bool:
    """True if rovercontrol's :8080 is up AND its serial link is up (control
    endpoints 503 when serial is down, so reachable-but-serial-down doesn't count)."""
    try:
        rovercontrol_client.set_host(host)
        h = rovercontrol_client.healthz(timeout=2.0)
        return bool(h.get("serial", {}).get("up"))
    except Exception:
        return False


def detect_rover(host: str = None, timeout: float = None):
    """Return a RoverCtl: serial if the board's serial device opens, else
    rovercontrol if its :8080 serial is up, else app.py if :5000 is reachable,
    else None. host/timeout override the HTTP target (only affect the HTTP paths)."""
    # Serial first (on the Pi). A failed open (e.g. rovercontrol owns the port)
    # falls through — never fatal. stop_http_service() only stops legacy app.py.
    try:
        import rover_direct
        port = rover_direct.detect_port()
    except Exception:
        port = None
    if port and os.path.exists(port):
        try:
            return RoverCtl("serial", port=port)
        except Exception as e:
            print(f"rover serial detected ({port}) but failed to open: {e}")

    http_host = host or ROVER_HTTP_HOST
    if timeout is not None:
        rover_client.set_timeout(timeout)
        rovercontrol_client.set_timeout(timeout)
    if _rovercontrol_ready(http_host):
        return RoverCtl("rovercontrol", http_host=http_host)
    if _reachable(http_host, ROVER_HTTP_PORT):
        return RoverCtl("http", http_host=http_host)
    return None


# friendlier name for new callers; detect_rover kept for agent_chat
connect = detect_rover
