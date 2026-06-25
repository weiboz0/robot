#!/usr/bin/env python3
"""Photo gallery + live camera view for the rover, in the browser.

Browse photos taken by the chatbot/joystick (saved in ./photos) without copying
them off the rover, and watch the live camera. Uses only the camera/HTTP — not
the serial port — so it runs happily alongside the chatbot.

The live view embeds the stock ugv_rpi web app's MJPEG stream (it owns the
camera and serves /video_feed on :5000), so keep that app running for live view.
The gallery and Snap work regardless.

Run on the rover:
  ~/ugv_rpi/ugv-env/bin/python ~/robot/rover_web.py     (or:  roverweb)
then open  http://<rover-ip>:8080   e.g.  http://192.168.1.131:8080
"""
import os

from flask import (Flask, send_from_directory, redirect, url_for, request,
                   abort)

import rover_camera

PHOTO_DIR = rover_camera.PHOTO_DIR
APP_PORT = 8080
STREAM_PORT = 5000          # stock ugv_rpi web app = camera owner / MJPEG source

app = Flask(__name__)


def list_photos():
    try:
        names = [f for f in os.listdir(PHOTO_DIR) if f.lower().endswith(".jpg")]
    except OSError:
        return []
    return sorted(names, reverse=True)          # newest first


def render(stream_host: str) -> str:
    photos = list_photos()
    cards = "\n".join(
        f'<figure><a href="/photos/{n}" target="_blank">'
        f'<img loading="lazy" src="/photos/{n}"></a>'
        f'<figcaption><span>{n}</span>'
        f'<form method="post" action="/delete/{n}" '
        f'onsubmit="return confirm(\'Delete {n}?\')">'
        f'<button class="del">delete</button></form></figcaption></figure>'
        for n in photos
    )
    if not cards:
        cards = ('<p class="empty">No photos yet — press “Snap”, or use '
                 '$photo / the joystick B button.</p>')
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rover camera</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}}
 header{{padding:12px 16px;background:#1c1c1c;position:sticky;top:0;z-index:1;
   display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
 h1{{font-size:18px;margin:0}}
 button{{background:#2d6cdf;color:#fff;border:0;padding:8px 14px;border-radius:6px;
   cursor:pointer;font-size:14px}}
 button.del{{background:#a33;padding:3px 8px;font-size:12px}}
 .live{{display:block;max-width:640px;width:100%;margin:12px auto;border-radius:8px;
   background:#000}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
   gap:12px;padding:12px}}
 figure{{margin:0;background:#1c1c1c;border-radius:8px;overflow:hidden}}
 figure img{{width:100%;display:block;aspect-ratio:4/3;object-fit:cover}}
 figcaption{{font-size:11px;padding:6px;display:flex;justify-content:space-between;
   align-items:center;gap:6px;word-break:break-all}}
 .empty{{padding:24px;text-align:center;color:#999}}
 form{{margin:0}}
</style></head>
<body>
<header>
  <h1>🤖 Rover camera</h1>
  <form method="post" action="/snap"><button>📸 Snap</button></form>
  <a href="/"><button style="background:#444">↻ Refresh</button></a>
  <span style="color:#999;font-size:12px">{len(photos)} photo(s)</span>
</header>
<img class="live" src="http://{stream_host}:{STREAM_PORT}/video_feed"
     alt="live view — needs the ugv_rpi web app running on :{STREAM_PORT}">
<div class="grid">
{cards}
</div>
</body></html>"""


@app.route("/")
def index():
    return render(request.host.split(":")[0])


@app.route("/photos/<path:name>")
def photo(name):
    return send_from_directory(PHOTO_DIR, name)   # safe against traversal


@app.route("/snap", methods=["POST"])
def snap():
    rover_camera.take_photo(wait=True, host="127.0.0.1")
    return redirect(url_for("index"))


@app.route("/delete/<path:name>", methods=["POST"])
def delete(name):
    if "/" in name or "\\" in name or ".." in name:
        abort(400)
    try:
        os.remove(os.path.join(PHOTO_DIR, name))
    except OSError:
        pass
    return redirect(url_for("index"))


if __name__ == "__main__":
    print(f"rover gallery + live view -> http://0.0.0.0:{APP_PORT}")
    app.run(host="0.0.0.0", port=APP_PORT, threaded=True)
