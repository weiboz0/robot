package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func axisEv(num uint8, val int16) jsEvent {
	return jsEvent{value: val, etype: jsEventAxis, number: num}
}
func btnEv(num uint8) jsEvent {
	return jsEvent{value: 1, etype: jsEventButton, number: num}
}

func TestCaptureAxis(t *testing.T) {
	ch := make(chan jsEvent, 8)
	go func() { time.Sleep(400 * time.Millisecond); ch <- axisEv(3, -30000) }()
	am, ok := captureAxis(ch, "push")
	if !ok || am.Index != 3 || !am.Invert { // negative move → Invert true
		t.Fatalf("captureAxis = %+v %v", am, ok)
	}
}

func TestCaptureButton(t *testing.T) {
	ch := make(chan jsEvent, 8)
	go func() { time.Sleep(400 * time.Millisecond); ch <- btnEv(5) }()
	b, ok := captureButton(ch, "press")
	if !ok || b != 5 {
		t.Fatalf("captureButton = %d %v", b, ok)
	}
}

func TestCaptureHatAxis(t *testing.T) {
	ch := make(chan jsEvent, 8)
	go func() { time.Sleep(400 * time.Millisecond); ch <- axisEv(7, -30000) }()
	h := captureHat(ch, "Press D-pad UP", "test")
	if h.Kind != "axis" || h.Axis.Index != 7 || !h.Axis.Invert { // raw-neg up → Invert
		t.Fatalf("captureHat axis = %+v", h)
	}
}

func TestComputeJoystickCameraSigns(t *testing.T) {
	m := defaultMapping()
	prev := &gpPrev{btn: map[int]bool{}}
	// default: pan axis 3 (not inverted) → right stick right = +pan;
	// tilt axis 4 inverted → right stick up (raw negative) = +tilt.
	a := computeJoystick(&m, fakeState(map[int]float64{3: 1.0, 4: -1.0}, nil), prev)
	if a.pan <= 0.9 {
		t.Fatalf("pan-right should be +: %v", a.pan)
	}
	if a.tilt <= 0.9 {
		t.Fatalf("tilt-up should be + (inverted axis): %v", a.tilt)
	}
}

func TestCaptureHatButtons(t *testing.T) {
	ch := make(chan jsEvent, 8)
	go func() {
		time.Sleep(400 * time.Millisecond)
		ch <- btnEv(11)
		time.Sleep(400 * time.Millisecond)
		ch <- btnEv(12)
	}()
	if h := captureHat(ch, "Press D-pad UP", "test"); h.Kind != "buttons" || h.Up != 11 || h.Down != 12 {
		t.Fatalf("captureHat buttons = %+v", h)
	}
}

func TestCaptureControl(t *testing.T) {
	// button press → Kind button
	ch := make(chan jsEvent, 8)
	go func() { time.Sleep(400 * time.Millisecond); ch <- btnEv(7) }()
	if c, ok := captureControl(ch, "press"); !ok || c.Kind != "button" || c.Index != 7 {
		t.Fatalf("button control: %+v %v", c, ok)
	}
	// trigger held (axis) → Kind axis
	ch2 := make(chan jsEvent, 8)
	go func() { time.Sleep(400 * time.Millisecond); ch2 <- axisEv(5, 30000) }()
	if c, ok := captureControl(ch2, "hold"); !ok || c.Kind != "axis" || c.Axis.Index != 5 {
		t.Fatalf("axis control: %+v %v", c, ok)
	}
	// closed device → skip
	ch3 := make(chan jsEvent)
	close(ch3)
	if _, ok := captureControl(ch3, "x"); ok {
		t.Fatal("closed device returned ok")
	}
}

func TestCaptureClosedDevice(t *testing.T) {
	ch := make(chan jsEvent)
	close(ch)
	if _, ok := captureAxis(ch, "x"); ok {
		t.Fatal("captureAxis on closed device returned ok")
	}
	ch2 := make(chan jsEvent)
	close(ch2)
	if h := captureHat(ch2, "Press D-pad UP", "test"); h.Kind != "none" {
		t.Fatalf("captureHat on closed device = %+v", h)
	}
}

// fakeState builds a gpState from axis/button maps for computeJoystick tests.
func fakeState(axes map[int]float64, btns map[int]bool) gpState {
	return gpState{
		axis:   func(i int) float64 { return axes[i] },
		button: func(i int) bool { return btns[i] },
	}
}

func TestControlMapHeld(t *testing.T) {
	none := ControlMap{Kind: "none"}
	if none.held(fakeState(map[int]float64{0: 1}, map[int]bool{0: true})) {
		t.Fatal("none should never be held")
	}
	btn := ControlMap{Kind: "button", Index: 7}
	if !btn.held(fakeState(nil, map[int]bool{7: true})) || btn.held(fakeState(nil, nil)) {
		t.Fatal("button held wrong")
	}
	trig := ControlMap{Kind: "axis", Axis: AxisMap{5, false}}
	if !trig.held(fakeState(map[int]float64{5: 1.0}, nil)) || trig.held(fakeState(map[int]float64{5: 0.2}, nil)) {
		t.Fatal("axis trigger held wrong")
	}
	// zero-value ControlMap (old config without the field) is disabled, not button 0
	var zero ControlMap
	if zero.held(fakeState(nil, map[int]bool{0: true})) {
		t.Fatal("zero-value ControlMap must be disabled, not button 0")
	}
}

func TestTopSpeedPrecedence(t *testing.T) {
	base := speedSteps[2] // 0.25
	if topSpeed(2, false, false, false) != base {
		t.Fatal("plain cap")
	}
	if topSpeed(2, true, false, false) != turbo {
		t.Fatal("turbo")
	}
	if topSpeed(2, false, true, false) != speedLimit {
		t.Fatal("boost = max")
	}
	// precision wins over turbo AND boost
	if topSpeed(2, true, true, true) != precisionCap {
		t.Fatalf("precision should win: %v", topSpeed(2, true, true, true))
	}
	if topSpeed(2, false, false, true) != precisionCap {
		t.Fatal("precision alone")
	}
}

func TestComputeJoystickNewControls(t *testing.T) {
	m := defaultMapping()
	m.Precision = ControlMap{Kind: "axis", Axis: AxisMap{2, false}} // LT trigger
	m.Boost = ControlMap{Kind: "button", Index: 7}
	m.PanicStop = ControlMap{Kind: "button", Index: 9}
	m.HatX = HatMap{Kind: "buttons", Up: 14, Down: 15} // right=Up=+1
	// precision held (trigger), boost held (button)
	a := computeJoystick(&m, fakeState(map[int]float64{2: 1.0}, map[int]bool{7: true}), &gpPrev{btn: map[int]bool{}})
	if !a.precision || !a.boost {
		t.Fatalf("precision/boost not held: %+v", a)
	}
	// PanicStop rising edge → estop, via its own slot (doesn't depend on m.Estop)
	prev := &gpPrev{btn: map[int]bool{}}
	if a := computeJoystick(&m, fakeState(nil, map[int]bool{9: true}), prev); !a.estop {
		t.Fatal("panic stop did not fold into estop")
	}
	if a := computeJoystick(&m, fakeState(nil, map[int]bool{9: true}), prev); a.estop {
		t.Fatal("panic stop fired twice while held")
	}
	// D-pad right → pan nudge +1 (rising edge), separate from speed hat
	if a := computeJoystick(&m, fakeState(nil, map[int]bool{14: true}), &gpPrev{btn: map[int]bool{}}); a.panNudge != 1 {
		t.Fatalf("pan nudge right: %d", a.panNudge)
	}
}

func TestLoadMappingBackwardCompat(t *testing.T) {
	// an old plan-004 config without the plan-006 fields must load them DISABLED
	dir := t.TempDir()
	p := filepath.Join(dir, "old.json")
	os.WriteFile(p, []byte(`{"stop":0,"estop":6,"hat":{"kind":"axis","axis":{"index":7,"invert":true}}}`), 0o644)
	m, src, err := loadMapping(p)
	if err != nil || src != "config" {
		t.Fatalf("old config → %s %v", src, err)
	}
	for _, c := range []ControlMap{m.Precision, m.Boost, m.PanicStop} {
		if c.Kind != "none" {
			t.Fatalf("old config gained an enabled control: %+v", c)
		}
	}
	if m.HatX.Kind != "none" {
		t.Fatalf("old config gained HatX: %+v", m.HatX)
	}
}

// TestDefaultMappingPinned locks every field to the historical constants — the
// no-config no-regression guarantee (reviewers N6/B1).
func TestDefaultMappingPinned(t *testing.T) {
	m := defaultMapping()
	want := GamepadMapping{
		Throttle: AxisMap{1, true}, Steer: AxisMap{0, false},
		Pan: AxisMap{3, false}, Tilt: AxisMap{4, true},
		Turbo: 5, Stop: 0, Snapshot: 1, HeadLight: 2, Center: 3,
		BaseLight: 4, Estop: 6, Relax: 9, Lock: 10,
		Hat: HatMap{Kind: "axis", Axis: AxisMap{7, true}},
		// plan 006: new controls default disabled
		Precision: ControlMap{Kind: "none"}, Boost: ControlMap{Kind: "none"},
		PanicStop: ControlMap{Kind: "none"}, HatX: HatMap{Kind: "none"},
	}
	if m != want {
		t.Fatalf("defaultMapping drifted:\n got %+v\nwant %+v", m, want)
	}
	if err := m.validate(); err != nil {
		t.Fatalf("default invalid: %v", err)
	}
}

func TestLoadMapping(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "none.json")
	if _, src, err := loadMapping(missing); src != "default" || err != nil {
		t.Fatalf("missing → %s %v (want default,nil)", src, err)
	}
	// malformed → invalid, NOT default (must not silently mis-drive)
	bad := filepath.Join(dir, "bad.json")
	os.WriteFile(bad, []byte("{not json"), 0o644)
	if m, src, err := loadMapping(bad); err == nil || src != "invalid" {
		t.Fatalf("malformed → %+v %s %v (want invalid,err)", m, src, err)
	}
	// partial valid → defaults fill omitted fields, then used
	part := filepath.Join(dir, "part.json")
	os.WriteFile(part, []byte(`{"throttle":{"index":9,"invert":true}}`), 0o644)
	m, src, err := loadMapping(part)
	if err != nil || src != "config" {
		t.Fatalf("partial → %s %v", src, err)
	}
	if m.Throttle != (AxisMap{9, true}) {
		t.Fatalf("partial throttle not applied: %+v", m.Throttle)
	}
	if m.Steer != (AxisMap{0, false}) { // default preserved
		t.Fatalf("partial dropped default steer: %+v", m.Steer)
	}
	// invalid index → rejected
	neg := filepath.Join(dir, "neg.json")
	os.WriteFile(neg, []byte(`{"stop":-1}`), 0o644)
	if _, src, err := loadMapping(neg); err == nil || src != "invalid" {
		t.Fatalf("negative index → %s %v (want invalid)", src, err)
	}
}

func TestComputeJoystickHonorsMapping(t *testing.T) {
	m := defaultMapping()
	m.Throttle = AxisMap{9, false} // remap throttle to axis 9, not inverted
	prev := &gpPrev{btn: map[int]bool{}}
	// axis 9 fully forward → throttle +1; default (axis 1) ignored
	a := computeJoystick(&m, fakeState(map[int]float64{9: 1.0, 1: -1.0}, nil), prev)
	if a.throttle <= 0.9 {
		t.Fatalf("remapped throttle not honored: %v", a.throttle)
	}
	// inverted axis reverses: push axis 9 to -1 with Invert → +1
	m.Throttle = AxisMap{9, true}
	a = computeJoystick(&m, fakeState(map[int]float64{9: -1.0}, nil), &gpPrev{btn: map[int]bool{}})
	if a.throttle <= 0.9 {
		t.Fatalf("inverted throttle wrong sign: %v", a.throttle)
	}
}

func TestComputeJoystickButtonEdges(t *testing.T) {
	m := defaultMapping()
	prev := &gpPrev{btn: map[int]bool{}}
	st := fakeState(nil, map[int]bool{m.Stop: true})
	if a := computeJoystick(&m, st, prev); !a.stop {
		t.Fatal("stop edge not detected")
	}
	// held → no second edge
	if a := computeJoystick(&m, st, prev); a.stop {
		t.Fatal("stop fired twice while held")
	}
}

func TestComputeJoystickHat(t *testing.T) {
	// axis hat: default axis 7 inverted, up (raw -1) → +1 speed step
	m := defaultMapping()
	if a := computeJoystick(&m, fakeState(map[int]float64{7: -1.0}, nil), &gpPrev{btn: map[int]bool{}}); a.hatDelta != 1 {
		t.Fatalf("axis hat up: %d", a.hatDelta)
	}
	// buttons hat
	m.Hat = HatMap{Kind: "buttons", Up: 11, Down: 12}
	if a := computeJoystick(&m, fakeState(nil, map[int]bool{11: true}), &gpPrev{btn: map[int]bool{}}); a.hatDelta != 1 {
		t.Fatalf("button hat up: %d", a.hatDelta)
	}
	if a := computeJoystick(&m, fakeState(nil, map[int]bool{12: true}), &gpPrev{btn: map[int]bool{}}); a.hatDelta != -1 {
		t.Fatalf("button hat down: %d", a.hatDelta)
	}
	// none hat: never changes speed
	m.Hat = HatMap{Kind: "none"}
	if a := computeJoystick(&m, fakeState(map[int]float64{7: -1.0}, nil), &gpPrev{btn: map[int]bool{}}); a.hatDelta != 0 {
		t.Fatalf("none hat moved: %d", a.hatDelta)
	}
}
