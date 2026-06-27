package main

import (
	"strings"
	"testing"
)

// Plan 016: the web UI exposes the new controls (clear-all photos, type-in speed,
// command box). Assert the served page contains them.
func TestWebUIHasNewControls(t *testing.T) {
	app, _ := testApp(t)
	w := do(t, app, "GET", "/")
	if w.Code != 200 {
		t.Fatalf("GET / = %d", w.Code)
	}
	body := w.Body.String()
	for _, want := range []string{
		`id="cmdin"`,    // command box
		`runCmd(`,       // command parser
		`id="capNum"`,   // type-in speed value
		`Clear all`,     // clear-all button
		`clearAll(`,     // clear-all handler
		`initCap(`,      // load-time speed read
		`toggleHelp(`,   // commands help button + panel
		`id="cmdhelp"`,  // the commands panel
	} {
		if !strings.Contains(body, want) {
			t.Errorf("page missing %q", want)
		}
	}
}

// Every endpoint the command box POSTs to must exist (guard against a typo in the
// JS mapping silently 404ing). We hit each with representative params through the
// same router and require a non-404 (200/400/503 are all "route exists").
func TestCommandBoxTargetsRouteExist(t *testing.T) {
	app, _ := testApp(t)
	app.rover.setLink(nil) // serial down: control routes 503 (still "exists"), not 404
	paths := []string{
		"/drive?l=0&r=0", "/move_forward?ms=200", "/move_back", "/move_left",
		"/move_right", "/stop", "/estop", "/camera_aim?pan=0&tilt=0",
		"/camera_up?deg=10", "/camera_down", "/camera_left", "/camera_right",
		"/camera_center", "/light_head?on=1", "/light_base", "/gimbal_relax",
		"/gimbal_lock", "/speed?cap=0.2", "/snapshot",
	}
	for _, p := range paths {
		if w := do(t, app, "POST", p); w.Code == 404 {
			t.Errorf("command-box target %q is 404 (route missing)", p)
		}
	}
	// the destructive photo route the clear-all loop uses
	if w := do(t, app, "POST", "/delete_photo/rover_x.jpg"); w.Code == 404 {
		t.Error("/delete_photo/{name} is 404")
	}
}
