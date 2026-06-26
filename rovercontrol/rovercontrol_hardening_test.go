package main

import (
	"net/http/httptest"
	"testing"
)

// Plan 015 G3: floatParam must reject NaN/Inf so they can't poison shared state
// (m.cap) or make writeJSON fail after the 200 header is already sent.
func TestFloatParamRejectsNonFinite(t *testing.T) {
	for _, s := range []string{"NaN", "Inf", "-Inf", "+Inf"} {
		req := httptest.NewRequest("POST", "/x?cap="+s, nil)
		if _, err := floatParam(req, "cap", 0.25); err == nil {
			t.Fatalf("floatParam accepted %q (want error)", s)
		}
	}
	// a normal value still parses
	req := httptest.NewRequest("POST", "/x?cap=0.3", nil)
	if v, err := floatParam(req, "cap", 0.25); err != nil || v != 0.3 {
		t.Fatalf("floatParam(0.3) = %v, %v", v, err)
	}
}

func TestSpeedRejectsNaN(t *testing.T) {
	app, _ := testApp(t)
	if w := do(t, app, "POST", "/speed?cap=NaN"); w.Code != 400 {
		t.Fatalf("/speed?cap=NaN code = %d (want 400)", w.Code)
	}
	if cap := app.move.getCap(); cap != 0.25 { // unchanged default, not poisoned
		t.Fatalf("cap poisoned to %v", cap)
	}
}

// Plan 015 G4: light toggles must not lose updates — two toggles return to off.
func TestLightToggleRoundTrips(t *testing.T) {
	app, _ := testApp(t)
	on, err := app.toggleHead()
	if err != nil || !on {
		t.Fatalf("first toggle: on=%v err=%v", on, err)
	}
	on, err = app.toggleHead()
	if err != nil || on {
		t.Fatalf("second toggle should be off: on=%v err=%v", on, err)
	}
	// base is independent and unaffected by head toggles
	if app.baseOn {
		t.Fatal("base flipped by head toggle")
	}
}

// Plan 015 G4: explicit set of one channel preserves the other.
func TestLightSetPreservesOtherChannel(t *testing.T) {
	app, _ := testApp(t)
	if _, err := app.toggleBase(); err != nil { // base on
		t.Fatal(err)
	}
	if w := do(t, app, "POST", "/light_head?on=1"); w.Code != 200 {
		t.Fatalf("light_head: %d", w.Code)
	}
	if !app.headOn || !app.baseOn {
		t.Fatalf("set head clobbered base: head=%v base=%v", app.headOn, app.baseOn)
	}
}

// Plan 015 G4: camera nudges accumulate (no lost update from the old read-release-write).
func TestCameraNudgeAccumulates(t *testing.T) {
	app, _ := testApp(t)
	app.aim.nudge(10, 0)
	app.aim.nudge(10, 0)
	if p, _ := app.aim.get(); p != 20 {
		t.Fatalf("two +10 pan nudges = %v (want 20)", p)
	}
}

// Plan 015 G2: closeLink stops reporting the link and is safe to call twice / with no link.
func TestCloseLink(t *testing.T) {
	r, _ := newTestRover()
	if up, _ := r.status(); !up {
		t.Fatal("link should be up before close")
	}
	r.closeLink()
	if up, _ := r.status(); up {
		t.Fatal("link still up after closeLink")
	}
	r.closeLink() // idempotent / nil-safe
	empty := &Rover{}
	empty.closeLink()
}
