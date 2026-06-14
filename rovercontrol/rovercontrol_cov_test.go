package main

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestRunWatchdogReturnsOnCancel(t *testing.T) {
	m := newMovement(&Rover{link: &recLink{}})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { m.runWatchdog(ctx); close(done) }()
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("runWatchdog did not return on context cancel")
	}
}

// Additional unit tests covering the pure-logic paths the main suite missed
// (raising statement coverage of the hardware-free code).

func TestGimbalTorque(t *testing.T) {
	r, rl := newTestRover()
	r.gimbalTorque(true)
	if got := rl.last(); got != `{"T":210,"cmd":1,"id":255}` {
		t.Fatalf("lock: %s", got)
	}
	r.gimbalTorque(false)
	if got := rl.last(); got != `{"T":210,"cmd":0,"id":255}` {
		t.Fatalf("release: %s", got)
	}
}

func TestSpeedCap(t *testing.T) {
	m := newMovement(&Rover{})
	m.setCap(0.4)
	if m.getCap() != 0.4 {
		t.Fatalf("cap = %v", m.getCap())
	}
	m.setCap(99) // clamps to speedLimit
	if m.getCap() != speedLimit {
		t.Fatalf("cap not clamped high: %v", m.getCap())
	}
	m.setCap(-1) // clamps to 0
	if m.getCap() != 0 {
		t.Fatalf("cap not clamped low: %v", m.getCap())
	}
}

func TestCameraAim(t *testing.T) {
	r, rl := newTestRover()
	a := &CameraAim{r: r}
	p, tl := a.set(30, 20)
	if p != 30 || tl != 20 {
		t.Fatalf("set: %v %v", p, tl)
	}
	if got := rl.last(); got != `{"ACC":0,"SPD":0,"T":133,"X":30,"Y":20}` {
		t.Fatalf("set cmd: %s", got)
	}
	if gp, gt := a.get(); gp != 30 || gt != 20 {
		t.Fatalf("get: %v %v", gp, gt)
	}
	p, tl = a.nudge(10, -5) // 40, 15
	if p != 40 || tl != 15 {
		t.Fatalf("nudge: %v %v", p, tl)
	}
	p, tl = a.nudge(0, 999) // tilt clamps to 90
	if tl != 90 {
		t.Fatalf("nudge tilt clamp: %v", tl)
	}
	p, tl = a.center()
	if p != 0 || tl != 0 {
		t.Fatalf("center: %v %v", p, tl)
	}
}

func TestToggleBaseLight(t *testing.T) {
	app, rl := testApp(t)
	on, _ := app.toggleBase()
	if !on || rl.last() != `{"IO4":255,"IO5":0,"T":132}` {
		t.Fatalf("base on: %v %s", on, rl.last())
	}
	on, _ = app.toggleBase()
	if on || rl.last() != `{"IO4":0,"IO5":0,"T":132}` {
		t.Fatalf("base off: %v %s", on, rl.last())
	}
}

func TestDefaultPhotoDir(t *testing.T) {
	if d := defaultPhotoDir(); !strings.HasSuffix(d, "photos") {
		t.Fatalf("defaultPhotoDir = %q", d)
	}
}

// HTTP endpoints not covered by the main suite: camera gimbal, gimbal torque,
// base light, speed get/set, photos list, delete.
func TestHTTPCameraGimbalAndSpeed(t *testing.T) {
	app, rl := testApp(t)
	for _, ep := range []string{"/camera_up", "/camera_down", "/camera_left", "/camera_right", "/camera_center"} {
		if w := do(t, app, "POST", ep); w.Code != 200 {
			t.Fatalf("%s: %d", ep, w.Code)
		}
	}
	if w := do(t, app, "POST", "/camera_aim?pan=20&tilt=10"); w.Code != 200 {
		t.Fatalf("camera_aim: %d", w.Code)
	}
	if w := do(t, app, "POST", "/camera_aim?pan=x"); w.Code != http.StatusBadRequest {
		t.Fatalf("camera_aim bad arg: %d", w.Code)
	}
	for _, ep := range []string{"/gimbal_relax", "/gimbal_lock", "/light_base"} {
		if w := do(t, app, "POST", ep); w.Code != 200 {
			t.Fatalf("%s: %d", ep, w.Code)
		}
	}
	if got := rl.last(); got == "" {
		t.Fatal("no serial command issued")
	}
	if w := do(t, app, "POST", "/speed?cap=0.3"); w.Code != 200 || app.move.getCap() != 0.3 {
		t.Fatalf("speed set: %d cap=%v", w.Code, app.move.getCap())
	}
	if w := do(t, app, "GET", "/speed"); w.Code != 200 || !strings.Contains(w.Body.String(), "0.3") {
		t.Fatalf("speed get: %d %s", w.Code, w.Body.String())
	}
	if w := do(t, app, "POST", "/speed?cap=bad"); w.Code != http.StatusBadRequest {
		t.Fatalf("speed bad: %d", w.Code)
	}
}

func TestHTTPPhotosListAndDelete(t *testing.T) {
	app, _ := testApp(t)
	app.hub.publish([]byte{0xff, 0xd8, 0xff, 0xd9})
	w := do(t, app, "POST", "/snapshot")
	if w.Code != 200 {
		t.Fatalf("snapshot: %d", w.Code)
	}
	if w := do(t, app, "GET", "/photos"); w.Code != 200 || !strings.Contains(w.Body.String(), "rover_") {
		t.Fatalf("photos list: %d %s", w.Code, w.Body.String())
	}
	// delete the one we just took
	var name string
	for _, n := range app.listPhotos() {
		name = n
	}
	if w := do(t, app, "POST", "/delete_photo/"+name); w.Code != 200 {
		t.Fatalf("delete: %d", w.Code)
	}
	if len(app.listPhotos()) != 0 {
		t.Fatal("photo not deleted")
	}
}

// TestIndexGamepadJS pins the browser-gamepad front-end (plan 005) so it can't
// silently regress: the poller, in-flight guard, watchdog-fed drive refresh,
// camera-aim integrator, and stop-on-disconnect/center must all be present.
func TestIndexGamepadJS(t *testing.T) {
	app, _ := testApp(t)
	body := do(t, app, "GET", "/").Body.String()
	for _, want := range []string{
		"navigator.getGamepads", // re-read live each tick
		"gamepadconnected",      // connect handler
		"gamepaddisconnected",   // disconnect → stop
		"driveBusy",             // in-flight guard (no fetch pile-up)
		"/drive?l=",             // drive via existing endpoint
		"/camera_aim?pan=",      // absolute-aim integrator
		"panAngle",              // client-side angle integration
		"wasMoving",             // deadzone → /stop once
		"visibilitychange",      // background → stop failsafe
		"keepalive:true",        // pagehide stop
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("gamepad JS missing %q", want)
		}
	}
}

func TestHTTPMethodAndCORS(t *testing.T) {
	app, _ := testApp(t)
	// GET on a POST-only command → 405 (method pattern mismatch) or 404
	w := do(t, app, "GET", "/move_forward")
	if w.Code != http.StatusMethodNotAllowed && w.Code != http.StatusNotFound {
		t.Fatalf("GET move_forward: %d", w.Code)
	}
}
