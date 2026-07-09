"""Object-detection model registry for comparison testing (plan 024).

Four detectors, one interface: run(name, jpeg) -> (annotated_jpeg, [dets]).
YOLO models (ultralytics) recognize 80 object classes by WHAT they are; the
HSV blob detector (detector.py) finds a colored thing. Heavy models load
lazily and are cached; a missing dependency degrades to a clear message.
"""
from __future__ import annotations

MODELS = ("hsv", "yolo11n", "yolov8n", "yolo11s")
_cache = {}


def available(name: str) -> bool:
    if name == "hsv":
        import detector
        return detector.available()
    try:
        import ultralytics  # noqa: F401
        return True
    except ImportError:
        return False


def run(name: str, jpeg: bytes, color: str = "green"):
    """Returns (annotated_jpeg_bytes, [{'label','conf','bbox'}...])."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("bad frame")
    h, w = img.shape[:2]
    dets = []
    if name == "hsv":
        import detector
        d = detector.detect_color_object(jpeg, color)
        if d:
            dets.append({"label": f"{color} blob", "conf": round(min(1.0, 0.5 + d["score"]), 2),
                         "bbox": d["bbox"]})
    elif name in MODELS:
        if name not in _cache:
            from ultralytics import YOLO
            _cache[name] = YOLO(name + ".pt")
        r = _cache[name](img, verbose=False, conf=0.35)[0]
        for b in r.boxes:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
            dets.append({"label": r.names[int(b.cls[0])], "conf": round(float(b.conf[0]), 2),
                         "bbox": [round(x1 / w, 4), round(y1 / h, 4),
                                  round(x2 / w, 4), round(y2 / h, 4)]})
    else:
        raise ValueError(f"unknown model {name!r} (know: {MODELS})")
    vis = img.copy()
    for d in dets:
        b = d["bbox"]
        p1, p2 = (int(b[0] * w), int(b[1] * h)), (int(b[2] * w), int(b[3] * h))
        cv2.rectangle(vis, p1, p2, (0, 255, 90), 2)
        cv2.putText(vis, f"{d['label']} {d['conf']:.2f}", (p1[0], max(14, p1[1] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 90), 2)
    cv2.putText(vis, name, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (60, 200, 255), 2)
    ok, buf = cv2.imencode(".jpg", vis, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return buf.tobytes(), dets
