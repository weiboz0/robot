"""Autonomous rover driving with a camera-only safety envelope (plan 017).

SAFETY MODEL — read before changing anything here:
- The rover has NO distance/cliff/proximity sensors. The only sensor is the
  pan/tilt camera, so obstacle/drop-off avoidance is vision-only and best-effort,
  NOT a guarantee. First runs must be on a flat, enclosed, ledge-free floor.
- The one guarantee that survives a Python crash/hang/network drop: the
  controller's /move_*?ms nudges auto-stop server-side after `ms`
  (rovercontrol.go time.AfterFunc). So we NEVER use continuous /drive — every
  move is a tiny bounded nudge. If this loop dies, the rover stops on its own.
- The Go controller process is the ultimate stop authority (the ESP32 holds the
  last speed; there is no firmware heartbeat). If the controller ITSELF is killed
  mid-nudge, the wheels run until power-off. Out of scope for this Python change;
  the real fix is a firmware heartbeat (offered with a ToF sensor).

Defense in depth: crawl cap; bounded nudges only; look-where-you-drive (forward()
aims the gimbal forward+down and requires a FRESH near-floor clearance verdict
before moving); short HTTP timeouts; an independent watchdog Timer that estops on
overrun even if the loop wedges; hard step/time caps; camera-up precondition;
e-stop-latch fail-closed; cleanup in finally.
"""
from __future__ import annotations

import threading
import time

# Conservative defaults (all overridable for tests).
CRAWL_CAP = 0.12
FORWARD_MS = 250          # tiny forward pulse — server auto-stops after this
TURN_MS = 220
FLOOR_TILT = -25.0        # aim down at the near floor for the clearance check
SETTLE_S = 0.5            # let the gimbal settle before capturing
FORWARD_COOLDOWN_S = 0.8  # min gap between forward nudges (no effectively-continuous motion)
HTTP_TIMEOUT = 3.0        # short, so a hung controller call can't block forever
MAX_STEPS = 40
MAX_SECONDS = 180.0
WATCHDOG_MARGIN_S = 15.0
FOUND_MIN_CONF = 0.5      # don't declare "found" on a low-confidence guess
MAX_VISION_ERRORS = 4     # give up if the vision API keeps failing (don't spin)


class SafetyLimit(RuntimeError):
    """A safety budget/precondition stopped the run."""


class SafeDriver:
    """Safety envelope over a controller client (rovercontrol_client-shaped:
    healthz/stop/estop/get_speed/set_speed/nudge/set_camera/set_timeout)."""

    def __init__(self, client, *, crawl_cap=CRAWL_CAP, forward_ms=FORWARD_MS,
                 turn_ms=TURN_MS, floor_tilt=FLOOR_TILT, settle_s=SETTLE_S,
                 forward_cooldown_s=FORWARD_COOLDOWN_S, http_timeout=HTTP_TIMEOUT,
                 max_steps=MAX_STEPS, max_seconds=MAX_SECONDS,
                 clock=time.monotonic, sleep=time.sleep, timer=threading.Timer):
        self.c = client
        self.crawl_cap = max(0.0, min(0.5, crawl_cap))
        self.forward_ms = max(0, min(400, int(forward_ms)))   # hard client-side clamp: forward stays tiny
        self.turn_ms = max(0, min(600, int(turn_ms)))
        self.floor_tilt = floor_tilt
        self.settle_s = settle_s
        self.forward_cooldown_s = forward_cooldown_s
        self.http_timeout = http_timeout
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self._clock = clock
        self._sleep = sleep
        self._timer_cls = timer
        self.steps = 0
        self._start = 0.0
        self._prior_cap = None
        self._last_forward = -1e9
        self._wd = None
        self._entered = False

    # ---- lifecycle (context manager) ----
    def __enter__(self):
        self._prior_timeout = getattr(self.c, "_TIMEOUT", 4.0)   # restore on exit
        try:
            self.c.set_timeout(self.http_timeout)   # cap every control call
        except Exception:
            pass
        h = self.c.healthz(timeout=self.http_timeout)
        # serial.up is load-bearing for TWO reasons: (1) motion works, and (2) the
        # controller's initLink selects the Gimbal module ({"T":4,"cmd":2}, see
        # rovercontrol.go initLink / TestInitLink) on every connect — so whenever
        # serial is up, the camera's floor-TILT is honored. This is what makes
        # forward()'s look-where-you-drive real (the stock app.py's module_type=0
        # gimbal-ignored problem does NOT apply to this Go controller).
        if not (h.get("serial") or {}).get("up"):
            raise SafetyLimit("serial link is down — refusing to drive")
        if not (h.get("camera") or {}).get("up"):
            raise SafetyLimit("camera is down — refusing to drive blind")
        # Start from a known-clear state: a zero /stop also clears any e-stop
        # latch. Best-effort — healthz already confirmed the link is reachable.
        try:
            self.c.stop()
        except Exception:
            pass
        self._prior_cap = self.c.get_speed()
        self.c.set_speed(self.crawl_cap)
        self._start = self._clock()
        self.steps = 0
        self._last_forward = -1e9
        # Independent watchdog: estop if the whole run overruns, even if the main
        # loop is wedged (hard caps only fire while code executes).
        self._wd = self._timer_cls(self.max_seconds + WATCHDOG_MARGIN_S, self._watchdog_fire)
        self._wd.daemon = True
        self._wd.start()
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        # ALWAYS stop FIRST (watchdog stays armed during the stop attempt so a
        # hung stop still escalates). stop() (zero drive) also clears the e-stop
        # latch so manual driving isn't left silently refused. Only restore the
        # prior cap if we confirmed a stop; else escalate to estop, leave cap low.
        stopped = self._safe_stop()
        if self._wd:
            self._wd.cancel()
        if stopped and self._prior_cap is not None:
            try:
                self.c.set_speed(self._prior_cap)
            except Exception:
                pass
        try:
            self.c.set_timeout(getattr(self, "_prior_timeout", 4.0))   # restore HTTP timeout
        except Exception:
            pass
        self._entered = False
        return False   # never suppress the exception

    def _watchdog_fire(self):
        try:
            self.c.estop()
        except Exception:
            pass

    def _safe_stop(self):
        for _ in range(3):
            try:
                self.c.stop()
                return True
            except Exception:
                pass
        try:
            self.c.estop()   # last resort (latches; a genuine emergency)
        except Exception:
            pass
        return False

    # ---- budget ----
    def _tick(self):
        if not self._entered:
            raise SafetyLimit("SafeDriver used outside its context")
        if self.steps >= self.max_steps:
            raise SafetyLimit(f"step cap {self.max_steps} reached")
        if self._clock() - self._start >= self.max_seconds:
            raise SafetyLimit("time cap reached")
        self.steps += 1

    def elapsed(self):
        return self._clock() - self._start

    # ---- motion (all bounded) ----
    def look(self, pan, tilt):
        """Aim the camera and wait for the gimbal to settle before any capture."""
        self.c.set_camera(pan, tilt)
        self._sleep(self.settle_s)

    def center_camera(self):
        self.look(0.0, 0.0)

    def forward(self, clearance):
        """Look WHERE WE DRIVE: aim the gimbal forward + down at the floor path,
        then require a FRESH clearance verdict on that view (clearance() must
        capture a new forward-pointing frame itself), respect a cooldown, then a
        tiny nudge. Returns False (no motion) if blocked."""
        self._tick()
        self.look(0.0, self.floor_tilt)     # camera now reflects travel direction
        if not clearance():                 # fresh near-floor safety check
            return False
        gap = self._clock() - self._last_forward
        if gap < self.forward_cooldown_s:
            self._sleep(self.forward_cooldown_s - gap)
        self.c.nudge("forward", self.forward_ms)
        self._last_forward = self._clock()
        return True

    def turn_left(self):
        self._tick()
        self.c.nudge("left", self.turn_ms)

    def turn_right(self):
        self._tick()
        self.c.nudge("right", self.turn_ms)

    # No back(): the camera can't see behind, so reversing can never be
    # look-where-you-drive safe. Turns are near-in-place on a differential rover
    # (minimal net translation) and kept tiny; forward is the gated path.

    def halt(self):
        self._safe_stop()


# ───────────────────────── vision prompts / judging ─────────────────────────

FIND_PROMPT = (
    "You are the eyes of a small floor rover looking for: {target}. "
    "Reply ONLY with JSON: {{\"seen\": bool, \"bearing\": \"left\"|\"center\"|\"right\", "
    "\"close\": bool, \"confidence\": 0..1, \"reason\": string}}. "
    "bearing = horizontal position of the target in the frame. close = target is "
    "within about one rover-length. If you are not sure the {target} is visible, "
    "set seen=false.")

FLOOR_PROMPT = (
    "This camera is aimed forward and downward at the floor directly ahead of a "
    "small rover that is about to creep forward ~10 cm. Reply ONLY with JSON: "
    "{\"clear\": bool, \"confidence\": 0..1, \"hazard\": string}. clear=true ONLY "
    "if the near floor straight ahead is flat, empty and safe — NO object, person, "
    "foot, wall, table/desk/chair edge, stair, step, dark gap or drop-off within "
    "~30 cm. If there is ANY doubt, or you cannot clearly see the floor, set "
    "clear=false.")


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def floor_is_clear(vision, img, *, min_conf=0.6):
    """Fail-closed near-floor safety verdict. Any error/ambiguity -> False."""
    try:
        v = vision.describe(img, FLOOR_PROMPT, json_out=True, max_tokens=200)
    except Exception:
        return False
    if not isinstance(v, dict):
        return False
    return v.get("clear") is True and _num(v.get("confidence")) >= min_conf


def look_for(vision, img, target):
    """Structured target observation; fail-closed to 'not seen' on any error."""
    prompt = FIND_PROMPT.format(target=target)
    try:
        v = vision.describe(img, prompt, json_out=True, max_tokens=250)
    except Exception as e:
        return {"seen": False, "bearing": "center", "close": False,
                "confidence": 0.0, "reason": f"vision error: {e}", "error": True}
    if not isinstance(v, dict):
        return {"seen": False, "bearing": "center", "close": False,
                "confidence": 0.0, "reason": "bad vision output", "error": True}
    v.setdefault("bearing", "center")
    if v.get("bearing") not in ("left", "center", "right"):
        v["bearing"] = "center"
    return v


def find_object(driver, vision, target, *, capture, log=lambda m: None):
    """Autonomous find loop. `capture()` -> (name, jpeg_bytes) of a fresh frame
    (a saved snapshot). Returns the saved photo name on success, else None.
    Motion happens ONLY through `driver` (bounded, safety-gated). Never raises for
    normal give-up; the driver context guarantees the wheels stop on exit."""
    def clearance():                       # fresh, forward-pointing near-floor check
        try:
            _, img = capture()
        except Exception as e:
            log(f"clearance capture failed ({e}) — treating as unsafe")
            return False
        ok = floor_is_clear(vision, img)
        log(f"  floor clear: {ok}")
        return ok

    vision_errors = 0
    with driver:
        while True:
            try:
                driver.look(0.0, -10.0)    # neutral forward-ish view to observe
                name, img = capture()
            except SafetyLimit:
                raise
            except Exception as e:
                log(f"capture failed ({e}); stopping")
                return None
            obs = look_for(vision, img, target)
            log(f"observe: seen={obs.get('seen')} bearing={obs.get('bearing')} "
                f"close={obs.get('close')} conf={obs.get('confidence')} "
                f"({obs.get('reason','')[:50]})")

            if obs.get("error"):           # don't spin forever on a dead vision API
                vision_errors += 1
                if vision_errors >= MAX_VISION_ERRORS:
                    log(f"vision failing ({vision_errors}x) — giving up")
                    return None
            else:
                vision_errors = 0

            if (obs.get("seen") and obs.get("close") and obs.get("bearing") == "center"
                    and _num(obs.get("confidence")) >= FOUND_MIN_CONF):
                driver.center_camera()
                shot = capture()[0]        # clean centered photo of the target
                log(f"FOUND {target} -> {shot}")
                return shot

            try:
                if obs.get("seen"):
                    b = obs.get("bearing")
                    if b == "left":
                        driver.turn_left()
                    elif b == "right":
                        driver.turn_right()
                    elif not driver.forward(clearance):   # centered but far → advance if floor clear
                        log("  path blocked ahead — turning to look for a way")
                        driver.turn_left()
                else:
                    driver.turn_left()     # not seen → rotate to scan (bounded by caps)
            except SafetyLimit as e:
                log(f"budget/precondition hit: {e}")
                return None
