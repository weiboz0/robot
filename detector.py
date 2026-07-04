"""Local color-object detection for the rover's autonomous find (plan 021).

Detects a color-named object (e.g. "a green pen") in a camera frame with plain
OpenCV — no LLM, no network, milliseconds. Used as the TARGET detector by the
find loop; the floor-safety judgment stays with the vision LLM (a color blob
can't recognize stairs or people).

Parameters were tuned against real rover frames of the actual target scene and
verified by eye (docs/plans/021):
- dark objects allowed (V floor 18): the user's pen is deep green;
- NO morphological open — the pen's black clip splits its blob into fragments
  that open() would erode away; close(13x13) bridges the clip instead;
- scoring = size * elongation * fill * MEAN SATURATION (shadow strips on the
  floor read as weak green; real colored objects are strongly saturated);
- filters: floor region only (bbox bottom >= 0.35), not the rover's own hull /
  immediate foreground (bbox top <= 0.78), not edge-clipped (ambiguous).

cv2/numpy are imported lazily; if missing, available() is False and callers
fall back to the LLM detector.
"""
from __future__ import annotations

# HSV ranges (OpenCV: H 0..180). A color may have several ranges (red wraps).
COLOR_RANGES = {
    "green":  [((40, 60, 18), (95, 255, 255))],
    "blue":   [((96, 60, 30), (130, 255, 255))],
    "red":    [((0, 90, 50), (10, 255, 255)), ((170, 90, 50), (180, 255, 255))],
    "yellow": [((20, 90, 70), (35, 255, 255))],
}

MIN_FRAC, MAX_FRAC = 0.00015, 0.15   # blob area as a fraction of the frame
MIN_MEAN_SAT = 115                   # HARD floor: shadow strips read ~112, real
                                     # colored objects 140+ (measured live)
MIN_SCORE = 0.15                     # below this the best blob is junk → None
FLOOR_MIN_Y2 = 0.35                  # bbox bottom must reach the floor region
HULL_MAX_Y1 = 0.78                   # bbox starting lower = the rover's own hull
EDGE_MARGIN = 0.02                   # edge-clipped blobs are ambiguous
CLOSE_KERNEL = 13                    # bridges dark clips/prints inside the object
SAT_NORM = 120.0                     # mean-saturation normalizer for scoring


def available() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def color_for_target(target: str) -> "str | None":
    """The first known color word in a target description, else None."""
    words = (target or "").lower().replace(",", " ").split()
    for w in words:
        if w in COLOR_RANGES:
            return w
    return None


def detect_color_object(jpeg_bytes: bytes, color: str) -> "dict | None":
    """Best <color> object candidate in the frame.

    Returns {"bbox": [x1,y1,x2,y2] fractions, "score": 0..1-ish, "elong": float,
    "mean_sat": int} or None. Raises ValueError for an unknown color and
    ImportError if cv2/numpy are unavailable (callers check available()).
    """
    import cv2
    import numpy as np
    if color not in COLOR_RANGES:
        raise ValueError(f"unknown color {color!r} (know: {sorted(COLOR_RANGES)})")
    img = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = None
    for lo, hi in COLOR_RANGES[color]:
        m = cv2.inRange(hsv, np.array(lo), np.array(hi))
        mask = m if mask is None else cv2.bitwise_or(mask, m)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            np.ones((CLOSE_KERNEL, CLOSE_KERNEL), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    sat = hsv[..., 1]
    best = None
    for c in cnts:
        area = cv2.contourArea(c)
        frac = area / (w * h)
        if frac < MIN_FRAC or frac > MAX_FRAC:
            continue
        rw, rh = cv2.minAreaRect(c)[1]
        if min(rw, rh) < 3:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        b = [x / w, y / h, (x + bw) / w, (y + bh) / h]
        if b[3] < FLOOR_MIN_Y2 or b[1] > HULL_MAX_Y1:
            continue
        if b[0] < EDGE_MARGIN or b[2] > 1.0 - EDGE_MARGIN:
            continue
        blob = np.zeros(mask.shape, np.uint8)
        cv2.drawContours(blob, [c], -1, 255, -1)
        mean_sat = float(cv2.mean(sat, mask=blob)[0])
        if mean_sat < MIN_MEAN_SAT:      # weakly-colored = shadow/reflection, not an object
            continue
        elong = max(rw, rh) / max(1.0, min(rw, rh))
        fill = area / max(1.0, rw * rh)
        score = (min(1.0, frac * 1500) * min(elong / 3.0, 1.0)
                 * max(fill, 0.3) * min(mean_sat / SAT_NORM, 1.0))
        cand = {"bbox": [round(v, 4) for v in b], "score": round(score, 3),
                "elong": round(elong, 2), "mean_sat": int(mean_sat)}
        if best is None or cand["score"] > best["score"]:
            best = cand
    if best is not None and best["score"] < MIN_SCORE:
        return None                      # best blob is junk — report "not seen"
    return best
