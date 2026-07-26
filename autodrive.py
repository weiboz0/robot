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

import math
import threading
import time

# Conservative defaults (all overridable for tests).
CRAWL_CAP = 0.12
FORWARD_MS = 250          # tiny forward pulse — server auto-stops after this
TURN_MS = 220
FLOOR_TILT = -20.0        # forward+down view — used for BOTH observing the target
                          # and the near-floor clearance check (one frame per cycle)
SETTLE_S = 1.2            # gimbal settle before capturing — CV detection made the
                          # loop fast enough that 0.5s captured mid-swing (blurry
                          # frames = boxes in the wrong place)
FORWARD_COOLDOWN_S = 0.8  # min gap between forward nudges (no effectively-continuous motion)
HTTP_TIMEOUT = 3.0        # short, so a hung controller call can't block forever
MAX_STEPS = 40
MAX_SECONDS = 240.0   # wall-clock incl. vision latency (a slow gateway night can
                      # cost 20-45s per look; motion is separately step-capped)
WATCHDOG_MARGIN_S = 15.0
FOUND_MIN_CONF = 0.5      # don't declare "found" on a low-confidence guess
MAX_VISION_ERRORS = 4     # give up if the vision API keeps failing (don't spin)

# plan 036 — go-to / come-back navigation
TURN_VIEW_PAN = 40.0      # side view aim (deg) for turn clearance checks
TURN_SURVEY_TTL_S = 10.0  # a 3-view turn-zone survey stays valid this long
BODY_ALIGN_TOL = 10.0     # forward only when the sighting pan is within this
ALIGN_MS_PER_DEG = 8.0    # turn pulse ms per degree of pan to zero out
LOST_MAX = 3              # consecutive lost-sight looks → honest stop
MIN_CAL_DELTA_DEG = 3.0   # smallest Δheading that proves a turn registered
WP_SPACING_M = 0.4        # backtrack waypoint spacing along the trail
ARRIVE_M = 0.3            # "I'm there" radius

# plan 037 — search-first + obstacle detours
SEARCH_PANS = (-50, 0, 50)   # coarse grid: 3 looks/viewpoint (budget-real)
SEARCH_TILTS = (-18,)
SEARCH_PHASE_S = 240.0    # search sub-budget inside the go-to wall budget
DETOUR_MAX = 3            # detour attempts per approach run
DETOUR_TURN_MS = 350      # nominal ~45°; OPEN-LOOP — the true angle is a
                          # guess on this uncalibrated rover; forward()
                          # re-gates whatever heading results
DETOUR_PROBE_PAN = 40.0   # side floor views probed before picking a side


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
        self._aim = None      # gimbal (pan, tilt) cache — look() skips redundant aims
        self._survey_at = None   # plan 036: last valid 3-view turn-zone survey

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
        self._survey_at = None   # a prior run's turn survey must never leak
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
        """Aim the camera and wait for the gimbal to settle before any capture.
        A no-op when already aimed there (skips the redundant move AND the
        settle sleep) — this is what keeps the loop to one aim per cycle. Base
        turns/nudges do NOT invalidate the cache: pan/tilt are rover-relative,
        so the gimbal is still physically at the cached aim after a base move
        (post-move scene settle is handled by _nudge_and_settle)."""
        if self._aim == (pan, tilt):
            return
        self.c.set_camera(pan, tilt)
        self._aim = (pan, tilt)
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
            # evidence of a hazard invalidates every cached clearance —
            # without this, a detour's turn could reuse a ≤TTL-old survey
            # that this verdict just contradicted (plan-037 review catch)
            self._survey_at = None
            return False
        gap = self._clock() - self._last_forward
        if gap < self.forward_cooldown_s:
            self._sleep(self.forward_cooldown_s - gap)
        self._nudge_and_settle("forward", self.forward_ms)
        self._last_forward = self._clock()
        self._survey_at = None   # motion changes the scene: turn survey is void
        return True

    def _nudge_and_settle(self, direction, ms):
        """Issue a bounded nudge, then wait out the motion + rock — the next
        capture must never happen while the BASE is still moving (blurry frames
        put detection boxes in the wrong place)."""
        self.c.nudge(direction, ms)
        self._sleep(ms / 1000.0 + 0.4)

    def turn_left(self, ms=None):
        """Bounded in-place turn; ms overrides the default (clamped ≤600)."""
        self._tick()
        self._nudge_and_settle("left", min(600, max(0, int(ms or self.turn_ms))))

    def turn_right(self, ms=None):
        self._tick()
        self._nudge_and_settle("right", min(600, max(0, int(ms or self.turn_ms))))

    # No back(): the camera can't see behind, so reversing can never be
    # look-where-you-drive safe. Turns are near-in-place on a differential rover
    # (minimal net translation) and kept tiny; forward is the gated path.

    # ---- gated turns (plan 036): look-where-you-turn ----
    # Geometry: an in-place turn sweeps ≈ the rover's own bounding circle.
    # The camera can survey the FRONT half (three floor-tilt views); the rear
    # half — including the rear corner swinging into the turn — is a
    # permanent blind spot (same physical limit that forbids back()).
    # Mitigations: tiny pulses at crawl cap, and the survey caught anything
    # approaching from the front seconds earlier.

    def _survey_ok(self):
        return (self._survey_at is not None
                and self._clock() - self._survey_at <= TURN_SURVEY_TTL_S)

    def turn_survey(self, clearance):
        """3-view turn-zone survey (pan −40/0/+40 at floor tilt) — ALL clear
        or no turning. Valid TURN_SURVEY_TTL_S; any forward pulse voids it."""
        for pan in (-TURN_VIEW_PAN, 0.0, TURN_VIEW_PAN):
            self.look(pan, self.floor_tilt)
            if not clearance():
                self._survey_at = None
                return False
        self._survey_at = self._clock()
        return True

    def turn_gated(self, direction, ms, clearance):
        """Bounded in-place turn behind a fresh floor check: a valid turn-zone
        survey (run one if needed) + ONE fresh clearance on the turn-direction
        view immediately before the nudge. Returns False (no motion) when any
        view is dirty."""
        if direction not in ("left", "right"):
            raise ValueError(f"bad turn direction {direction!r}")
        self._tick()
        if not self._survey_ok() and not self.turn_survey(clearance):
            return False
        self.look(TURN_VIEW_PAN if direction == "right" else -TURN_VIEW_PAN,
                  self.floor_tilt)
        if not clearance():
            self._survey_at = None
            return False
        self._nudge_and_settle(direction, min(600, max(0, int(ms))))
        return True

    def halt(self):
        self._safe_stop()


# ───────────────────────── vision prompts / judging ─────────────────────────

FIND_PROMPT = (
    "You are the eyes of a small floor rover looking for: {target}. "
    "Reply ONLY with JSON: {{\"seen\": bool, \"bbox\": [x1, y1, x2, y2] or null, "
    "\"bearing\": \"left\"|\"center\"|\"right\", \"close\": bool, \"color\": string, "
    "\"confidence\": 0..1, \"reason\": string}}. "
    "bbox = the target's bounding box as FRACTIONS of image width/height (0..1), "
    "null if not seen. bearing = horizontal position of the target. close = target "
    "is within about one rover-length. Report the object's actual color in "
    "\"color\". Only set seen=true if the object CLEARLY matches the description "
    "\"{target}\" — including its color, if the description names one. If unsure, "
    "set seen=false.")

# Approach tuning: a valid bbox overrides the model's coarse flags. The rover
# keeps creeping (floor-gated) until the target is "close", judged two ways:
# 1. SIZE — the LARGER bbox dimension fills CLOSE_BBOX_DIM of the frame (an
#    elongated pen lying sideways is wide but tiny in height, so height alone
#    would never trigger). Right for hand-sized-and-bigger objects.
# 2. PROXIMITY — the bbox BOTTOM is low in the frame. With the camera tilted
#    down, "near the bottom edge" = "at my feet". Small objects (a pen is ~6%
#    of the frame even up close) can NEVER pass the size test; without this
#    they get overrun into the near-field blind spot, sight is lost, and the
#    search spirals (the real-world $pen failure).
CLOSE_BBOX_DIM = 0.25     # larger bbox dimension fraction that counts as "close"
CLOSE_BBOX_BOTTOM = 0.70  # bbox bottom edge (y2) below this fraction = at my feet
BEAR_LEFT, BEAR_RIGHT = 0.40, 0.60   # bbox center-x thresholds (tightened: better-centered shots)
CENTER_TOL = 0.08         # final-photo camera centering tolerance (bbox cx from 0.5)
CAM_DEG_PER_FRAC = 70.0   # gimbal deg per frame-fraction of offset (wide lens ≈133;
                          # conservative gain converges in 2-3 refinement steps)


def _sane_bbox(b):
    """Validate a model bbox: 4 finite fractions 0..1 with x1<x2, y1<y2 — else None."""
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return None
    try:
        v = [float(x) for x in b]
    except (TypeError, ValueError):
        return None
    if any(x != x or x < 0.0 or x > 1.0 for x in v):     # NaN or out of range
        return None
    if not (v[0] < v[2] and v[1] < v[3]):
        return None
    return v

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
    return _apply_bbox(v)


def _apply_bbox(v):
    """A valid bbox overrides the coarse flags: bearing from its center-x, close
    from its LARGER dimension (height alone fails for elongated floor objects)
    or bottom-proximity. Garbage bbox → keep the flags (backward compatible).
    Shared by the LLM (look_for) and CV (obs_from_detection) paths."""
    bbox = _sane_bbox(v.get("bbox"))
    v["bbox"] = bbox
    if bbox is not None:
        cx = (bbox[0] + bbox[2]) / 2.0
        v["bearing"] = "left" if cx < BEAR_LEFT else "right" if cx > BEAR_RIGHT else "center"
        v["close"] = (max(bbox[2] - bbox[0], bbox[3] - bbox[1]) >= CLOSE_BBOX_DIM
                      or bbox[3] >= CLOSE_BBOX_BOTTOM)
    return v


def obs_from_detection(det, color):
    """Map a detector.detect_color_object result to the loop's obs shape, so the
    CV detector is a drop-in `look` (plan 021 — no LLM in the detect path)."""
    if not det:
        return {"seen": False, "bearing": "center", "close": False, "bbox": None,
                "confidence": 0.0, "color": color, "reason": "no colored object detected"}
    # Detections already passed the size/region/edge filters; score adds margin.
    conf = round(min(1.0, 0.5 + float(det.get("score") or 0)), 2)
    v = {"seen": True, "bbox": det.get("bbox"), "bearing": "center", "close": False,
         "confidence": conf, "color": color,
         "reason": f"cv blob score={det.get('score')} elong={det.get('elong')}"}
    return _apply_bbox(v)


def _refine_center(driver, looker, capture, obs, log, max_iters=3):
    """Camera-only final centering: pan the gimbal so the target's bbox sits
    near mid-frame before the found photo (wheels untouched). Keeps the last
    good observation if the target slips out of a refinement frame."""
    for _ in range(max_iters):
        b = obs.get("bbox")
        if not b:
            break
        cx = (b[0] + b[2]) / 2.0
        if abs(cx - 0.5) <= CENTER_TOL:
            break
        pan, tilt = getattr(driver, "_aim", None) or (0.0, driver.floor_tilt)
        pan = max(-90.0, min(90.0, pan + (cx - 0.5) * CAM_DEG_PER_FRAC))
        driver.look(pan, tilt)
        try:
            _, img = capture()
        except Exception as e:
            log(f"  centering capture failed ({e})")
            break
        o2 = looker(None, img)
        if not (isinstance(o2, dict) and o2.get("seen") and o2.get("bbox")):
            break                          # lost it — keep the previous good obs
        obs = o2
        log(f"  centering: cx={((o2['bbox'][0]+o2['bbox'][2])/2):.2f}")
    return obs


# Camera-first search plan (v3): sweep the GIMBAL across the room from where the
# rover stands — the wide lens covers most of a room from one spot, so the target
# is usually findable with ZERO wheel motion (the real-world failure mode was
# staring forward and body-turning blindly). Wheels only rotate in place between
# sweeps; forward driving is out of the default entirely (it caused the
# floor-gate-vs-target conflicts and the near-field blind-spot losses).
SWEEP_PANS = (-50, -25, 0, 25, 50)
SWEEP_TILTS = (-15, -28)
MAX_ROTATIONS = 5          # in-place ~60° rotations between sweeps (covers 360°)
ROTATE_MS = 550
EARLY_ACCEPT_CONF = 0.85   # stop sweeping immediately on a very strong sighting


def _sweep_for(driver, looker, capture, *, err, log=lambda m: None,
               sweep_pans=SWEEP_PANS, sweep_tilts=SWEEP_TILTS,
               deadline=None):
    """One gimbal sweep from the CURRENT spot → ("found", (conf, obs, pan,
    tilt)) / ("none", None) / ("abort_time"|"abort_capture"|"abort_vision",
    None). Contract (plan 036): does NO context management (the driver must
    already be entered) and NO base rotation — the between-sweep rotation
    stays in find_object; a rotating sweep would smuggle ungated turns past
    the gated-only guarantee. `err` is a shared {"n": int} vision-error
    counter (find_object's original consecutive-failure semantics).
    `deadline` (driver-clock absolute, plan 037) is checked BEFORE every
    look so a slow sweep can never overrun its phase cap nor accept a
    post-cap sighting — the cap is authoritative (code-review catch)."""
    best = None
    for tilt in sweep_tilts:
        for pan in sweep_pans:
            if driver.elapsed() >= driver.max_seconds or (
                    deadline is not None and driver._clock() >= deadline):
                log("time cap reached — stopping the search")
                return "abort_time", None
            driver.look(pan, tilt)
            try:
                _, img = capture()
            except Exception as e:
                log(f"capture failed ({e}); stopping")
                return "abort_capture", None
            obs = looker(None, img)
            # the phase cap is authoritative even over a sighting whose look
            # STARTED in-budget — a post-cap result is discarded (plan-037
            # code-review demand; wall-clock semantics for find_object are
            # deliberately unchanged: only the deadline param is strict)
            if deadline is not None and driver._clock() >= deadline:
                log("time cap reached — stopping the search")
                return "abort_time", None
            if obs.get("error"):
                err["n"] += 1
                if err["n"] >= MAX_VISION_ERRORS:
                    log(f"vision failing ({err['n']}x) — giving up")
                    return "abort_vision", None
                continue
            err["n"] = 0
            conf = _num(obs.get("confidence"))
            if obs.get("seen") and obs.get("bbox") and conf >= FOUND_MIN_CONF:
                log(f"  spotted at pan={pan} tilt={tilt} conf={conf:.2f} "
                    f"({str(obs.get('reason', ''))[:40]})")
                cand = (conf, obs, pan, tilt)
                if best is None or conf > best[0]:
                    best = cand
                if conf >= EARLY_ACCEPT_CONF:
                    return "found", best
    return ("found", best) if best else ("none", None)


def find_object(driver, vision, target, *, capture, log=lambda m: None,
                on_found=None, look=None, snap=None,
                sweep_pans=SWEEP_PANS, sweep_tilts=SWEEP_TILTS,
                max_rotations=MAX_ROTATIONS):
    """Camera-first autonomous find. From the current spot, sweep the gimbal
    over `sweep_pans` x `sweep_tilts` looking for the target; if a full sweep
    sees nothing, rotate the base in place (~60°, bounded) and sweep again — up
    to `max_rotations` times (≈ full circle). On the best sighting: halt, center
    the camera on it (gimbal only), `snap()` the ONE photo, store outline meta
    via `on_found(photo_name, obs)`. NO forward driving — finding does not
    require approaching, and staying put removes the cliff/obstacle risk of the
    old approach phase. Returns the saved photo name, else None."""
    looker = look if look is not None else (lambda nm, im: look_for(vision, im, target))

    def finish(obs):
        driver.halt()                      # wheels stop (belt: they weren't moving)
        obs = _refine_center(driver, looker, capture, obs, log)
        shot = None
        if snap is not None:
            try:
                shot = snap()              # the one saved photo of the run
            except Exception as e:
                log(f"  (snapshot failed: {e})")
        log(f"FOUND {target} -> {shot} (color: {obs.get('color', '?')})")
        if shot and on_found is not None:
            try:
                on_found(shot, obs)        # bbox matches: scene static since capture
            except Exception as e:
                log(f"  (meta save failed: {e})")
        return shot

    err = {"n": 0}
    with driver:
        for rot in range(max_rotations + 1):
            state, best = _sweep_for(driver, looker, capture, err=err, log=log,
                                     sweep_pans=sweep_pans,
                                     sweep_tilts=sweep_tilts)
            if state.startswith("abort"):
                return None

            if best:
                _, obs, pan, tilt = best
                driver.look(pan, tilt)      # re-aim at the winning view (no-op if there)
                try:                        # fresh confirm at that aim (scene static)
                    _, img = capture()
                    o2 = looker(None, img)
                    if o2.get("seen") and o2.get("bbox"):
                        obs = o2
                except Exception:
                    pass
                return finish(obs)

            if rot < max_rotations:
                log(f"not visible from this viewpoint (sweep {rot + 1}) — rotating to look around")
                try:
                    driver.turn_left(ROTATE_MS)   # in-place, bounded, step-capped
                except SafetyLimit as e:
                    log(f"budget/precondition hit: {e}")
                    return None

        log("swept a full circle at two camera heights without seeing it — it may "
            "be occluded or out of view; move it into the open or say roughly "
            "where it is and I'll look again")
        return None


# ───────────────────── go-to / come-back navigation (plan 036) ────────────────
# Every wheel nudge below — forward AND turn — sits behind a fresh floor check
# (forward() and turn_gated()). Pose reads used for motion decisions require
# fresh:true (fail-closed). The chat tools gate all of this behind
# ROVER_GO_ENABLE=1, a separate consent from ROVER_FIND_ENABLE's
# rotation-only authorization.


def _norm180(deg):
    return (deg + 180.0) % 360.0 - 180.0


def _fresh_pose(get_pose):
    """Pose dict for a motion decision — stale/malformed → SafetyLimit."""
    try:
        p = get_pose() or {}
    except Exception as e:
        raise SafetyLimit(f"pose read failed ({e}) — refusing to navigate blind")
    if not p.get("fresh"):
        raise SafetyLimit("pose is stale — refusing to navigate blind")
    if not all(isinstance(p.get(k), (int, float)) for k in ("x", "y", "heading")):
        raise SafetyLimit("pose is malformed — refusing to navigate blind")
    return p


def make_llm_clearance(vision, capture):
    """The standard floor gate: fresh frame at the current aim → fail-closed
    floor_is_clear verdict."""
    def clearance():
        try:
            _, img = capture()
        except Exception:
            return False
        return floor_is_clear(vision, img)
    return clearance


def turn_to_heading(driver, get_pose, target_deg, *, clearance,
                    tol_deg=12.0, max_pulses=10, sign_state=None):
    """Closed-loop gated in-place turn to a pose-frame heading (CCW+).
    HEADING_SIGN is uncalibrated on this rover, so the wheel→heading mapping
    is detected at runtime and FAIL-CLOSED (plan-036 review spec):
    calibration pulse at full turn_ms must move the heading ≥
    MIN_CAL_DELTA_DEG (one 1.5× retry allowed), else SafetyLimit; the next
    measured pulse must agree with the detected mapping, else SafetyLimit.
    `sign_state` ({"left_is_plus": bool}) lets a caller reuse a mapping
    across calls — the agreement check still runs. Returns True when within
    tol_deg."""
    state = sign_state if sign_state is not None else {}
    h = _fresh_pose(get_pose)["heading"]
    err = _norm180(target_deg - h)
    if abs(err) <= tol_deg:
        return True
    left_is_plus = state.get("left_is_plus")
    confirmed = False
    pulses = 0
    while pulses < max_pulses:
        if abs(err) <= tol_deg:
            return True
        if left_is_plus is None:            # nominal: CCW+ means left = +
            direction = "left" if err > 0 else "right"
        else:
            direction = "left" if ((err > 0) == left_is_plus) else "right"
        ms = driver.turn_ms if abs(err) > 25 else int(driver.turn_ms * 0.6)
        if not driver.turn_gated(direction, ms, clearance):
            raise SafetyLimit("path blocked while turning")
        pulses += 1
        h2 = _fresh_pose(get_pose)["heading"]
        delta = _norm180(h2 - h)
        if left_is_plus is None:
            if abs(delta) < MIN_CAL_DELTA_DEG:
                # ONE bigger retry, then fail closed — a tiny/noisy delta
                # must never lock in a wrong mapping. The retry consumes a
                # pulse from the SAME budget: max_pulses is a hard cap on
                # nudges, never exceeded (code-review catch).
                if pulses >= max_pulses:
                    raise SafetyLimit("pose isn't registering turns — "
                                      "refusing to navigate blind")
                if not driver.turn_gated(direction, int(driver.turn_ms * 1.5),
                                         clearance):
                    raise SafetyLimit("path blocked while turning")
                pulses += 1
                h2 = _fresh_pose(get_pose)["heading"]
                delta = _norm180(h2 - h)
                if abs(delta) < MIN_CAL_DELTA_DEG:
                    raise SafetyLimit("pose isn't registering turns — "
                                      "refusing to navigate blind")
            left_is_plus = (delta > 0) if direction == "left" else (delta < 0)
            state["left_is_plus"] = left_is_plus
        elif not confirmed and abs(delta) >= MIN_CAL_DELTA_DEG:
            moved_plus = delta > 0
            expect_plus = (direction == "left") == left_is_plus
            if moved_plus != expect_plus:
                raise SafetyLimit("turn direction is inconsistent — stopping")
            confirmed = True
        h = h2
        err = _norm180(target_deg - h)
    return abs(err) <= tol_deg


def search_around(driver, looker, capture, clearance, *,
                  max_rotations=5, phase_cap_s=SEARCH_PHASE_S,
                  log=lambda m: None):
    """Look approximately all the way around (plan 037): coarse gimbal
    sweeps with GATED in-place rotations between them (unlike
    find_object's legacy ungated rotation), until the target is sighted or
    the phase cap / rotation budget runs out. Coverage is BEST-EFFORT
    within the cap — rotations are open-loop and nominal (~60°), never a
    guaranteed 360°. Returns (best | None, why). The driver must already
    be entered (no context management here)."""
    end = driver._clock() + phase_cap_s
    timeout_why = ("couldn't spot it in the time I had — I looked around "
                   "as far as time allowed")
    err = {"n": 0}
    for rot in range(max_rotations + 1):
        if driver._clock() >= end:
            return None, timeout_why
        state, best = _sweep_for(driver, looker, capture, err=err, log=log,
                                 sweep_pans=SEARCH_PANS,
                                 sweep_tilts=SEARCH_TILTS,
                                 deadline=end)
        if state == "abort_time":
            return None, timeout_why
        if state.startswith("abort"):
            return None, "search aborted (camera or vision failing)"
        if best:
            return best, "sighted"
        if rot < max_rotations:
            if driver._clock() >= end:
                return None, timeout_why
            log(f"not visible from viewpoint {rot + 1} — turning to look "
                "around")
            if not driver.turn_gated("left", ROTATE_MS, clearance):
                return None, ("floor not clear for turning — couldn't look "
                              "further around")
    return None, ("looked around (approximately a full circle) without "
                  "seeing it")


def approach_object(driver, vision, target, *, capture, log=lambda m: None,
                    look=None, clearance=None, search=False, detours=0):
    """Drive TO a visible object (plan 036; search/detours plan 037).
    Sight the target — a stationary sweep by default, `search=True` adds
    gated look-around rotations first — turn the BODY until the sighting
    is dead ahead (|pan| ≤ BODY_ALIGN_TOL — a pan=50° sighting must never
    trigger a body-forward pulse, that vector is wrong), then creep
    forward, floor-gated per pulse, until the close criteria fire.
    `detours > 0` allows bounded go-around attempts at a blocked path
    (side probes are ADVISORY; the turn/forward gates stay authoritative,
    and the blocked forward has already voided the turn survey so a detour
    turn always re-surveys). HONEST LIMIT (glm review): the fresh survey's
    center view is the same physical view that just blocked, so a STATIC
    dead-ahead obstacle re-fails it and the detour stops with "path
    blocked while turning" — detours genuinely help only when the hazard
    is TRANSIENT (a cat that wandered off, a person passing) and the
    re-check legitimately clears. Callers using search=True should size
    max_seconds ≥ 2× SEARCH_PHASE_S or the search can eat the wall budget.
    Defaults keep plan-036 behavior byte-stable.
    Returns (ok, obs, why). The driver context is entered HERE."""
    looker = look if look is not None else (lambda nm, im: look_for(vision, im, target))
    clearance = clearance or make_llm_clearance(vision, capture)
    obs = None
    with driver:
        try:
            if search:
                best, why = search_around(driver, looker, capture,
                                          clearance, log=log)
                if best is None:
                    return False, None, why
            else:
                err = {"n": 0}
                state, best = _sweep_for(driver, looker, capture, err=err,
                                         log=log)
                if state.startswith("abort") or best is None:
                    return False, None, "target not visible from here"
            _, obs, pan, tilt = best
            lost = 0
            detours_done = 0
            while True:
                if driver.elapsed() >= driver.max_seconds:
                    return False, obs, "time budget reached"
                if abs(pan) > BODY_ALIGN_TOL:
                    # body alignment: rotate toward the sighting (gated), then
                    # re-sight straight ahead
                    direction = "right" if pan > 0 else "left"
                    ms = min(driver.turn_ms,
                             max(120, int(abs(pan) * ALIGN_MS_PER_DEG)))
                    if not driver.turn_gated(direction, ms, clearance):
                        return False, obs, "path blocked while turning"
                    pan = 0.0
                driver.look(pan, tilt)
                try:
                    _, img = capture()
                except Exception as e:
                    return False, obs, f"camera failed: {e}"
                o2 = looker(None, img)
                if not (o2.get("seen") and o2.get("bbox")):
                    lost += 1
                    if lost >= LOST_MAX:
                        return False, obs, ("lost sight of it — stopped "
                                            "where I am")
                    continue
                lost = 0
                obs = o2
                b = obs["bbox"]
                cx = (b[0] + b[2]) / 2.0
                if obs.get("close"):
                    driver.halt()
                    obs = _refine_center(driver, looker, capture, obs, log)
                    return True, obs, "arrived"
                if cx < BEAR_LEFT or cx > BEAR_RIGHT:
                    direction = "right" if cx > 0.5 else "left"
                    ms = min(driver.turn_ms,
                             max(120, int(abs(cx - 0.5) * CAM_DEG_PER_FRAC
                                          * ALIGN_MS_PER_DEG)))
                    if not driver.turn_gated(direction, ms, clearance):
                        return False, obs, "path blocked while turning"
                    continue
                # dead ahead + centered → ONE floor-gated forward pulse
                if not driver.forward(clearance):
                    # blocked — forward() has voided the turn survey (the
                    # hazard verdict invalidates cached clearances)
                    if detours_done >= detours:
                        return False, obs, (
                            "path blocked ahead" if detours == 0
                            else "path blocked — out of detour attempts")
                    # side probes: camera-only, ADVISORY (the motion gates
                    # below remain authoritative)
                    clear_sides = []
                    for probe_pan, side in ((-DETOUR_PROBE_PAN, "left"),
                                            (DETOUR_PROBE_PAN, "right")):
                        driver.look(probe_pan, driver.floor_tilt)
                        if clearance():
                            clear_sides.append(side)
                    if not clear_sides:
                        return False, obs, "boxed in — stopped"
                    if len(clear_sides) == 2:   # tie-break toward the target
                        side = "left" if cx < 0.5 else "right"
                    else:
                        side = clear_sides[0]
                    # gated turn — fresh full survey guaranteed by the void
                    if not driver.turn_gated(side, DETOUR_TURN_MS, clearance):
                        return False, obs, "path blocked while turning"
                    if not driver.forward(clearance):
                        return False, obs, "path blocked on the detour"
                    detours_done += 1
                    # reacquire: camera sweep biased OPPOSITE the detour turn
                    first = -1.0 if side == "right" else 1.0
                    found = None
                    for p in (first * 55.0, first * 25.0, 0.0, -first * 25.0):
                        driver.look(p, tilt)
                        try:
                            _, img2 = capture()
                        except Exception as e:
                            return False, obs, f"camera failed: {e}"
                        o3 = looker(None, img2)
                        if o3.get("seen") and o3.get("bbox"):
                            found = (o3, p)
                            break
                    if found:
                        obs, pan = found
                        lost = 0       # reacquired: losses don't compound
                        continue
                    # note: lost is always 0 here (the forward branch only
                    # runs after a SEEN look, which resets it) — a distinct
                    # "lost after detour" terminal was dead code (glm review
                    # catch); the main loop's lost path decides from here
                    lost += 1
                    pan = 0.0
                    continue
                pan = 0.0     # forward() snapped the gimbal to (0, floor_tilt)
        except SafetyLimit as e:
            return False, obs, f"stopped by the safety envelope: {e}"


def plan_return_waypoints(trail, home, *, wp_spacing=WP_SPACING_M):
    """Waypoints for the return trip (pure function, heavily tested).
    Slices the trail by the INDEX recorded at go-to start — never
    nearest-point matching (a path crossing near home would pick the wrong
    segment) — reverses it, subsamples to wp_spacing, and always ends at
    home. Returns (waypoints, evicted_note)."""
    hp = home["pose"]
    start = max(0, int(home.get("trail_len") or 0) - 1)
    seg = list(trail[start:])
    note = None
    if seg and math.hypot(seg[0][0] - hp["x"], seg[0][1] - hp["y"]) > 0.5:
        note = "trail partially evicted — following the surviving suffix"
    wps = []
    last = None
    for x, y in reversed(seg):
        if last is None or math.hypot(x - last[0], y - last[1]) >= wp_spacing:
            wps.append((float(x), float(y)))
            last = (x, y)
    home_pt = (float(hp["x"]), float(hp["y"]))
    if not wps or math.hypot(wps[-1][0] - home_pt[0],
                             wps[-1][1] - home_pt[1]) > 1e-9:
        wps.append(home_pt)
    return wps, note


def backtrack(driver, get_pose, trail, home, *, clearance,
              wp_spacing=WP_SPACING_M, arrive_m=ARRIVE_M,
              log=lambda m: None):
    """Drive back along the recorded trail to home (plan 036): the path it
    actually drove — known passable minutes ago — beats a straight line
    through unknown floor. Turn-then-forward per waypoint, every nudge
    floor-gated, every motion-decision pose read fresh-checked. Returns
    (ok, remaining_m, why). The driver context is entered HERE."""
    hp = home["pose"]

    def remaining():
        try:
            p = get_pose() or {}
            return math.hypot(hp["x"] - float(p.get("x") or 0.0),
                              hp["y"] - float(p.get("y") or 0.0))
        except Exception:
            return float("nan")

    wps, note = plan_return_waypoints(trail, home, wp_spacing=wp_spacing)
    if note:
        log(note)
    sign_state = {}
    with driver:
        try:
            for i, (wx, wy) in enumerate(wps):
                final = i == len(wps) - 1
                tol = arrive_m if final else max(arrive_m, wp_spacing / 2)
                stalls = 0
                while True:
                    p = _fresh_pose(get_pose)
                    dx, dy = wx - p["x"], wy - p["y"]
                    dist = math.hypot(dx, dy)
                    if dist <= tol:
                        break
                    bearing = math.degrees(math.atan2(dy, dx))
                    if abs(_norm180(bearing - p["heading"])) > 20.0:
                        # a non-converged turn must NOT be followed by a
                        # forward pulse — misaligned forward motion is the
                        # exact wrong-vector risk (code-review catch)
                        if not turn_to_heading(driver, get_pose, bearing,
                                               clearance=clearance,
                                               sign_state=sign_state):
                            return False, remaining(), \
                                "couldn't align toward the path"
                    if not driver.forward(clearance):
                        return False, remaining(), "path blocked on the way back"
                    p2 = _fresh_pose(get_pose)
                    d2 = math.hypot(wx - p2["x"], wy - p2["y"])
                    if d2 >= dist - 0.01:
                        stalls += 1
                        if stalls >= 3:   # driving but not getting closer
                            if not turn_to_heading(
                                    driver, get_pose,
                                    math.degrees(math.atan2(wy - p2["y"],
                                                            wx - p2["x"])),
                                    clearance=clearance,
                                    sign_state=sign_state):
                                return False, remaining(), \
                                    "couldn't align toward the path"
                            stalls = 0
                    else:
                        stalls = 0
            rem = remaining()
            ok = rem == rem and rem <= arrive_m + 0.05   # NaN-safe
            return ok, rem, "arrived" if ok else "ended near home"
        except SafetyLimit as e:
            return False, remaining(), f"stopped by the safety envelope: {e}"
