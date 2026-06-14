package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// ── fake serial link: captures each written JSON line ───────────────────────

type recLink struct {
	mu    sync.Mutex
	lines []string
}

func (l *recLink) Write(p []byte) (int, error) {
	l.mu.Lock()
	l.lines = append(l.lines, strings.TrimRight(string(p), "\n"))
	l.mu.Unlock()
	return len(p), nil
}
func (l *recLink) Close() error { return nil }
func (l *recLink) last() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if len(l.lines) == 0 {
		return ""
	}
	return l.lines[len(l.lines)-1]
}
func (l *recLink) all() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.lines...)
}

func newTestRover() (*Rover, *recLink) {
	rl := &recLink{}
	return &Rover{link: rl}, rl
}

// ── serial command encoding / clamping ──────────────────────────────────────

func TestSerialEncoding(t *testing.T) {
	r, rl := newTestRover()
	r.drive(0.2, -0.3)
	if got := rl.last(); got != `{"L":0.2,"R":-0.3,"T":1}` {
		t.Fatalf("drive: %s", got)
	}
	r.drive(9, -9) // clamp to ±0.5
	if got := rl.last(); got != `{"L":0.5,"R":-0.5,"T":1}` {
		t.Fatalf("drive clamp: %s", got)
	}
	r.lights(999, -5) // clamp 0..255
	if got := rl.last(); got != `{"IO4":0,"IO5":255,"T":132}` {
		t.Fatalf("lights clamp: %s", got)
	}
	p, tl, _ := r.aimCamera(999, 999) // clamp pan 180, tilt 90
	if p != 180 || tl != 90 {
		t.Fatalf("aim clamp: %v %v", p, tl)
	}
	rl.mu.Lock()
	rl.lines = nil
	rl.mu.Unlock()
	r.estop()
	got := rl.all()
	if len(got) != 2 || got[0] != `{"L":0,"R":0,"T":1}` || got[1] != `{"T":0}` {
		t.Fatalf("estop sequence: %v", got)
	}
}

func TestInitLink(t *testing.T) {
	rl := &recLink{}
	if err := initLink(rl); err != nil {
		t.Fatal(err)
	}
	want := []string{`{"T":143,"cmd":0}`, `{"T":131,"cmd":0}`, `{"T":4,"cmd":2}`}
	if got := rl.all(); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("initLink: %v", got)
	}
}

func TestEstopLatchRefusesMotionUntilReleased(t *testing.T) {
	r, rl := newTestRover()
	m := newMovement(r)
	m.doEstop()
	if !m.isEstopped() {
		t.Fatal("not latched after estop")
	}
	// a held/new nonzero command is refused while latched
	m.setDrive(0.3, 0.3)
	if got := rl.last(); got != `{"T":0}` { // still the estop, no drive went out
		t.Fatalf("latched estop drove anyway: %s", got)
	}
	if !m.isEstopped() {
		t.Fatal("nonzero command cleared the latch")
	}
	// a zero command (recenter / explicit) releases it
	m.setDrive(0, 0)
	if m.isEstopped() {
		t.Fatal("zero command did not release latch")
	}
	// now motion is allowed again
	m.setDrive(0.2, 0.2)
	if got := rl.last(); got != `{"L":0.2,"R":0.2,"T":1}` {
		t.Fatalf("motion not resumed after release: %s", got)
	}
}

func TestLongNudgeNotTruncatedByWatchdog(t *testing.T) {
	r, _ := newTestRover()
	m := newMovement(r)
	m.nudge(1, 1, 700*time.Millisecond) // longer than the 500ms watchdog TTL
	// the watchdog must NOT stop a nudge (nudges aren't continuous leases)
	if m.watchdogTick(time.Now().Add(time.Second)) {
		t.Fatal("watchdog truncated a nudge")
	}
}

func TestSerialUnavailable(t *testing.T) {
	r := &Rover{} // no link
	if r.ok() {
		t.Fatal("ok() true with no link")
	}
	if err := r.drive(0.1, 0.1); err == nil {
		t.Fatal("expected error writing with no link")
	}
}

// ── movement arbitration + watchdog ─────────────────────────────────────────

func TestStaleNudgeDoesNotStopNewerCommand(t *testing.T) {
	r, rl := newTestRover()
	m := newMovement(r)
	m.nudge(1, 1, 50*time.Millisecond) // arms stop timer at this gen
	m.setDrive(0.3, 0.3)               // newer command bumps gen
	time.Sleep(120 * time.Millisecond) // let the stale timer fire (and no-op)
	if got := rl.last(); got != `{"L":0.3,"R":0.3,"T":1}` {
		t.Fatalf("stale nudge cancelled newer command; last=%s", got)
	}
}

func TestNudgeStopsWhenNotSuperseded(t *testing.T) {
	r, rl := newTestRover()
	m := newMovement(r)
	m.nudge(1, 1, 40*time.Millisecond)
	time.Sleep(90 * time.Millisecond)
	if got := rl.last(); got != `{"L":0,"R":0,"T":1}` {
		t.Fatalf("nudge did not auto-stop; last=%s", got)
	}
}

func TestWatchdog(t *testing.T) {
	r, rl := newTestRover()
	m := newMovement(r)
	m.setDrive(0.2, 0.2)
	// not yet past the deadline → no stop
	if m.watchdogTick(time.Now()) {
		t.Fatal("watchdog fired immediately")
	}
	// past the deadline → stop
	if !m.watchdogTick(time.Now().Add(time.Second)) {
		t.Fatal("watchdog did not fire after TTL")
	}
	if got := rl.last(); got != `{"L":0,"R":0,"T":1}` {
		t.Fatalf("watchdog stop cmd: %s", got)
	}
	// after a stop, not moving → never fires
	if m.watchdogTick(time.Now().Add(time.Hour)) {
		t.Fatal("watchdog fired while stopped")
	}
}

func TestWatchdogNotTrippedBySteadyRefresh(t *testing.T) {
	r, _ := newTestRover()
	m := newMovement(r)
	// simulate a 25Hz driver refreshing within TTL every tick
	for i := 0; i < 30; i++ {
		m.setDrive(0.2, 0.2)
		if m.watchdogTick(time.Now()) {
			t.Fatalf("watchdog tripped under steady driving at tick %d", i)
		}
	}
}

func TestStopAndEstopInvalidatePendingNudge(t *testing.T) {
	r, rl := newTestRover()
	m := newMovement(r)
	m.nudge(1, 1, 50*time.Millisecond)
	m.doEstop() // bumps gen → the pending nudge timer must no-op
	time.Sleep(90 * time.Millisecond)
	got := rl.all()
	// last two writes are the estop pair; no late stray stop afterward
	if len(got) < 2 || got[len(got)-1] != `{"T":0}` || got[len(got)-2] != `{"L":0,"R":0,"T":1}` {
		t.Fatalf("estop not the final action (stale nudge leaked?): %v", got)
	}
}

// ── joystick parsing + mapping ──────────────────────────────────────────────

func jsEventBytes(value int16, etype, number uint8) []byte {
	return []byte{0, 0, 0, 0, byte(uint16(value)), byte(uint16(value) >> 8), etype, number}
}

func TestParseJSEvent(t *testing.T) {
	// button 3 pressed
	e, ok := parseJSEvent(jsEventBytes(1, jsEventButton, 3))
	if !ok || e.etype != jsEventButton || e.number != 3 || e.value != 1 || e.isInit {
		t.Fatalf("button: %+v", e)
	}
	// axis 4 fully negative (signed LE)
	e, _ = parseJSEvent(jsEventBytes(-32767, jsEventAxis, 4))
	if e.value != -32767 || e.etype != jsEventAxis {
		t.Fatalf("neg axis: %+v", e)
	}
	// init bit masked off but flagged
	e, _ = parseJSEvent(jsEventBytes(0, jsEventAxis|jsEventInit, 1))
	if e.etype != jsEventAxis || !e.isInit {
		t.Fatalf("init: %+v", e)
	}
	if _, ok := parseJSEvent([]byte{1, 2, 3}); ok {
		t.Fatal("short event accepted")
	}
}

func TestGamepadReader(t *testing.T) {
	g := newGamepad()
	stream := append(jsEventBytes(16383, jsEventAxis, axLX), jsEventBytes(1, jsEventButton, btnA)...)
	g.reader(context.Background(), bytes.NewReader(stream)) // returns at EOF
	if v := g.axis(axLX); v < 0.49 || v > 0.51 {
		t.Fatalf("axis normalize: %v", v)
	}
	if !g.button(btnA) {
		t.Fatal("button not set")
	}
}

func TestGamepadDisconnectStops(t *testing.T) {
	app, rl := testApp(t)
	pr, pw, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	pw.Write(jsEventBytes(32767, jsEventAxis, axLY)) // stick deflected when it drops
	done := make(chan struct{})
	go func() { app.runGamepad(context.Background(), pr); close(done) }()
	time.Sleep(80 * time.Millisecond) // let it connect + drive
	pw.Close()                        // EOF → reader exits → loop cancelled
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("runGamepad did not return after disconnect")
	}
	if app.gamepadPresent() {
		t.Fatal("gamepad still present after disconnect")
	}
	if got := rl.last(); got != `{"L":0,"R":0,"T":1}` {
		t.Fatalf("rover not stopped after disconnect: %s", got)
	}
}

func TestDriveMixAndRamp(t *testing.T) {
	l, r := driveMix(1, 0, 0.4)
	if l != 0.4 || r != 0.4 {
		t.Fatalf("forward mix: %v %v", l, r)
	}
	l, r = driveMix(0, 1, 0.4) // pure steer
	if l != 0.4 || r != -0.4 {
		t.Fatalf("steer mix: %v %v", l, r)
	}
	if dz(0.1) != 0 || dz(0.2) != 0.2 || dz(-0.2) != -0.2 {
		t.Fatal("deadzone")
	}
	if v := rampToward(0, 1, 0.05); v != 0.05 {
		t.Fatalf("ramp step: %v", v)
	}
}

// ── HTTP layer ──────────────────────────────────────────────────────────────

func testApp(t *testing.T) (*App, *recLink) {
	t.Helper()
	r, rl := newTestRover()
	app := &App{rover: r, hub: newHub(), cam: &Camera{}, photoDir: t.TempDir()}
	app.move = newMovement(r)
	app.aim = &CameraAim{r: r}
	app.cam.setStatus(true, "")
	return app, rl
}

func do(t *testing.T, app *App, method, path string) *httptest.ResponseRecorder {
	t.Helper()
	w := httptest.NewRecorder()
	app.routes().ServeHTTP(w, httptest.NewRequest(method, path, nil))
	return w
}

func TestHTTPMovementRouting(t *testing.T) {
	app, rl := testApp(t)
	if w := do(t, app, "POST", "/move_forward"); w.Code != 200 {
		t.Fatalf("move_forward: %d", w.Code)
	}
	// nudge scales by the default cap 0.25
	if got := rl.last(); got != `{"L":0.25,"R":0.25,"T":1}` {
		t.Fatalf("move_forward cmd: %s", got)
	}
	if w := do(t, app, "POST", "/estop"); w.Code != 200 {
		t.Fatalf("estop: %d", w.Code)
	}
	if got := rl.last(); got != `{"T":0}` {
		t.Fatalf("estop cmd: %s", got)
	}
}

func TestHTTPDriveClampAndValidation(t *testing.T) {
	app, rl := testApp(t)
	if w := do(t, app, "POST", "/drive?l=1&r=-1"); w.Code != 200 {
		t.Fatalf("drive: %d", w.Code)
	}
	if got := rl.last(); got != `{"L":0.25,"R":-0.25,"T":1}` { // clamped ±1 * cap
		t.Fatalf("drive scaled: %s", got)
	}
	// malformed number → 400 even though serial is up
	if w := do(t, app, "POST", "/drive?l=abc&r=0"); w.Code != http.StatusBadRequest {
		t.Fatalf("bad number: %d", w.Code)
	}
}

func TestHTTPSerialDown503(t *testing.T) {
	app, _ := testApp(t)
	app.rover.setLink(nil) // serial drops
	if w := do(t, app, "POST", "/move_left"); w.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d", w.Code)
	}
}

func TestHTTPLightToggle(t *testing.T) {
	app, rl := testApp(t)
	do(t, app, "POST", "/light_head") // toggle on
	if got := rl.last(); got != `{"IO4":0,"IO5":255,"T":132}` {
		t.Fatalf("head on: %s", got)
	}
	do(t, app, "POST", "/light_head") // toggle off
	if got := rl.last(); got != `{"IO4":0,"IO5":0,"T":132}` {
		t.Fatalf("head off: %s", got)
	}
}

func TestHTTPSnapshotAndGallery(t *testing.T) {
	app, _ := testApp(t)
	// no frame yet → 503
	if w := do(t, app, "POST", "/snapshot"); w.Code != http.StatusServiceUnavailable {
		t.Fatalf("snapshot no frame: %d", w.Code)
	}
	app.hub.publish([]byte{0xff, 0xd8, 0x01, 0xff, 0xd9})
	w := do(t, app, "POST", "/snapshot")
	if w.Code != 200 {
		t.Fatalf("snapshot: %d", w.Code)
	}
	var resp struct {
		OK   bool   `json:"ok"`
		Name string `json:"name"`
	}
	json.Unmarshal(w.Body.Bytes(), &resp)
	if !resp.OK || !strings.HasSuffix(resp.Name, ".jpg") {
		t.Fatalf("snapshot resp: %+v", resp)
	}
	if _, err := os.Stat(filepath.Join(app.photoDir, resp.Name)); err != nil {
		t.Fatalf("snapshot file missing: %v", err)
	}
	if w := do(t, app, "GET", "/latest"); !strings.Contains(w.Body.String(), resp.Name) {
		t.Fatalf("/latest missing snapshot: %s", w.Body.String())
	}
}

func TestSnapshotNeverOverwrites(t *testing.T) {
	app, _ := testApp(t)
	app.hub.publish([]byte{0xff, 0xd8, 0xff, 0xd9})
	now := time.Date(2026, 6, 13, 12, 0, 0, 0, time.UTC)
	taken := filepath.Join(app.photoDir, "rover_20260613_120000_001.jpg")
	os.WriteFile(taken, []byte("python"), 0o644)
	name, err := app.snapshot(now)
	if err != nil || name == "rover_20260613_120000_001.jpg" {
		t.Fatalf("collided: %v %s", err, name)
	}
	if b, _ := os.ReadFile(taken); string(b) != "python" {
		t.Fatal("overwrote existing file")
	}
}

func TestHTTPTraversalRejected(t *testing.T) {
	app, _ := testApp(t)
	for _, p := range []string{
		"/photos/..%2f..%2fsecret.jpg", "/photos/.hidden.jpg", "/photos/x.png",
		"/delete_photo/..%2fsecret.jpg",
	} {
		method := "GET"
		if strings.HasPrefix(p, "/delete_photo") {
			method = "POST"
		}
		if w := do(t, app, method, p); w.Code != http.StatusBadRequest && w.Code != http.StatusNotFound {
			t.Fatalf("%s: expected 4xx, got %d", p, w.Code)
		}
	}
}

func TestHTTPHealthz(t *testing.T) {
	app, _ := testApp(t)
	app.cam.setStatus(false, "busy")
	w := do(t, app, "GET", "/healthz")
	var h struct {
		Serial  map[string]any `json:"serial"`
		Camera  map[string]any `json:"camera"`
		Gamepad bool           `json:"gamepad"`
	}
	json.Unmarshal(w.Body.Bytes(), &h)
	if h.Serial["up"] != true || h.Camera["up"] != false || h.Camera["err"] != "busy" {
		t.Fatalf("healthz: %s", w.Body.String())
	}
}

func TestHTTPIndexAndCORS(t *testing.T) {
	app, _ := testApp(t)
	if w := do(t, app, "GET", "/"); w.Code != 200 || !strings.Contains(w.Body.String(), "/video_feed") {
		t.Fatalf("index: %d", w.Code)
	}
	if w := do(t, app, "OPTIONS", "/move_left"); w.Code != http.StatusNoContent ||
		w.Header().Get("Access-Control-Allow-Origin") != "*" {
		t.Fatalf("CORS preflight: %d %q", w.Code, w.Header().Get("Access-Control-Allow-Origin"))
	}
}

func TestVideoFeedMultipart(t *testing.T) {
	app, _ := testApp(t)
	app.hub.publish([]byte{0xff, 0xd8, 0xaa, 0xff, 0xd9})
	srv := httptest.NewServer(app.routes())
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/video_feed")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "multipart/x-mixed-replace; boundary=") {
		t.Fatalf("content-type %q", ct)
	}
	buf := make([]byte, 200)
	n, _ := io.ReadAtLeast(resp.Body, buf, 40)
	if !strings.Contains(string(buf[:n]), "Content-Type: image/jpeg") {
		t.Fatalf("stream part: %q", buf[:n])
	}
}

// ── MJPEG splitter + hub (folded from rovercam) ─────────────────────────────

func fakeJPEG(payload []byte) []byte {
	return append(append([]byte{0xff, 0xd8}, payload...), 0xff, 0xd9)
}

func TestSplitFrames(t *testing.T) {
	frames := [][]byte{
		fakeJPEG([]byte("hello")),
		fakeJPEG([]byte{0x01, 0xff, 0xd9, 0x02}), // decoy EOI inside payload
		fakeJPEG(bytes.Repeat([]byte{0xab}, 2000)),
	}
	stream := bytes.Join(frames, nil)
	for chunk := 1; chunk <= 5; chunk++ {
		var got [][]byte
		splitFrames(&sizedReader{stream, chunk}, func(f []byte) { got = append(got, f) })
		if len(got) != len(frames) {
			t.Fatalf("chunk=%d got %d frames", chunk, len(got))
		}
		for i := range frames {
			if !bytes.Equal(got[i], frames[i]) {
				t.Fatalf("chunk=%d frame %d mismatch", chunk, i)
			}
		}
	}
}

type sizedReader struct {
	data []byte
	size int
}

func (s *sizedReader) Read(p []byte) (int, error) {
	if len(s.data) == 0 {
		return 0, io.EOF
	}
	n := s.size
	if n > len(s.data) {
		n = len(s.data)
	}
	if n > len(p) {
		n = len(p)
	}
	copy(p, s.data[:n])
	s.data = s.data[n:]
	return n, nil
}

func TestBuildCameraCmd(t *testing.T) {
	ctx := context.Background()
	got := buildCameraCmd(ctx, "v4l2", "/dev/video0", 1280, 720, 30).Args
	want := []string{"v4l2-ctl", "-d", "/dev/video0",
		"--set-fmt-video=width=1280,height=720,pixelformat=MJPG",
		"--stream-mmap", "--stream-count=0", "--stream-to=-"}
	if strings.Join(got, " ") != strings.Join(want, " ") {
		t.Fatalf("v4l2 sized args:\n got %v\nwant %v", got, want)
	}
	got = buildCameraCmd(ctx, "v4l2", "/dev/video1", 0, 0, 30).Args
	if strings.Join(got, " ") != "v4l2-ctl -d /dev/video1 --set-fmt-video=pixelformat=MJPG --stream-mmap --stream-count=0 --stream-to=-" {
		t.Fatalf("v4l2 unsized args: %v", got)
	}
	got = buildCameraCmd(ctx, "rpicam", "", 1280, 720, 30).Args
	want = []string{"rpicam-vid", "-n", "-t", "0", "--codec", "mjpeg",
		"--width", "1280", "--height", "720", "--framerate", "30", "-o", "-"}
	if strings.Join(got, " ") != strings.Join(want, " ") {
		t.Fatalf("rpicam args:\n got %v\nwant %v", got, want)
	}
	// rpicam with 0 dimensions/fps omits them too ("let the camera choose")
	got = buildCameraCmd(ctx, "rpicam", "", 0, 0, 0).Args
	if strings.Join(got, " ") != "rpicam-vid -n -t 0 --codec mjpeg -o -" {
		t.Fatalf("rpicam unsized args: %v", got)
	}
}

func TestCameraRunUnsizedRetryAndExhaustion(t *testing.T) {
	// All attempts fail instantly → run() should try sized then unsized once,
	// then stop double-spawning (exhaustion), and surface the unsized error.
	var mu sync.Mutex
	var calls [][2]int
	c := &Camera{mode: "v4l2", width: 1280, height: 720, fps: 30}
	c.attemptFn = func(ctx context.Context, hub *Hub, w, h int) error {
		mu.Lock()
		calls = append(calls, [2]int{w, h})
		mu.Unlock()
		return fmt.Errorf("VIDIOC_STREAMON: failed")
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { c.run(ctx, newHub()); close(done) }()

	// wait until both the sized and the unsized attempt have happened
	deadline := time.Now().Add(2 * time.Second)
	for {
		mu.Lock()
		n := len(calls)
		mu.Unlock()
		if n >= 2 || time.Now().After(deadline) {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	<-done

	mu.Lock()
	defer mu.Unlock()
	if len(calls) < 2 {
		t.Fatalf("expected sized+unsized attempts, got %v", calls)
	}
	if calls[0] != [2]int{1280, 720} || calls[1] != [2]int{0, 0} {
		t.Fatalf("first two attempts should be sized then unsized: %v", calls[:2])
	}
	// exhaustion: at most one unsized (0,0) attempt despite repeated failures
	unsized := 0
	for _, call := range calls {
		if call == [2]int{0, 0} {
			unsized++
		}
	}
	if unsized != 1 {
		t.Fatalf("unsized retry should fire once (exhaustion), fired %d times: %v", unsized, calls)
	}
	// the surfaced status carries the (clearer) unsized error
	if _, e := c.status(); !strings.Contains(e, "unsized:") {
		t.Fatalf("status should surface unsized error, got %q", e)
	}
}

func TestResolveCameraMode(t *testing.T) {
	existing := t.TempDir() + "/video0"
	os.WriteFile(existing, nil, 0o644)
	missing := t.TempDir() + "/nope"
	cases := []struct{ mode, device, want string }{
		{"auto", existing, "v4l2"},
		{"auto", missing, "rpicam"},
		{"v4l2", missing, "v4l2"},
		{"rpicam", existing, "rpicam"},
		{"bogus", existing, "rpicam"},
	}
	for _, c := range cases {
		if got := resolveCameraMode(c.mode, c.device); got != c.want {
			t.Fatalf("resolveCameraMode(%q)=%q want %q", c.mode, got, c.want)
		}
	}
}

func TestTailBuffer(t *testing.T) {
	var tb tailBuffer
	tb.Write([]byte("line1\nline2\n"))
	tb.Write(bytes.Repeat([]byte("x"), 4000))
	if len(tb.String()) > 2048 {
		t.Fatalf("tailBuffer not bounded: %d", len(tb.String()))
	}
	var tb2 tailBuffer
	tb2.Write([]byte("first\nVIDIOC_STREAMON: failed"))
	if lastLine(tb2.String()) != "VIDIOC_STREAMON: failed" {
		t.Fatalf("lastLine: %q", lastLine(tb2.String()))
	}
}

func TestHubLatestWins(t *testing.T) {
	h := newHub()
	ch, cancel := h.subscribe()
	defer cancel()
	for i := 0; i < 5; i++ {
		h.publish([]byte{byte(i)})
	}
	if got := <-ch; got[0] != 4 {
		t.Fatalf("expected latest 4, got %d", got[0])
	}
	select {
	case <-ch:
		t.Fatal("backlog leaked")
	default:
	}
}
