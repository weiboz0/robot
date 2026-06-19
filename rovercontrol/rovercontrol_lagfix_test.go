package main

import (
	"bytes"
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeStreamW is a ResponseWriter+Flusher for driving videoFeed without a socket.
// It records everything written and can force Write to fail (slow/stuck client)
// or run a hook after each write (used to end the stream from the test).
type fakeStreamW struct {
	mu       sync.Mutex
	buf      bytes.Buffer
	hdr      http.Header
	writeErr error
	onWrite  func()
}

func (f *fakeStreamW) Header() http.Header {
	if f.hdr == nil {
		f.hdr = http.Header{}
	}
	return f.hdr
}
func (f *fakeStreamW) WriteHeader(int) {}
func (f *fakeStreamW) Flush()          {}
func (f *fakeStreamW) Write(p []byte) (int, error) {
	f.mu.Lock()
	f.buf.Write(p)
	f.mu.Unlock()
	if f.onWrite != nil {
		f.onWrite()
	}
	if f.writeErr != nil {
		return 0, f.writeErr
	}
	return len(p), nil
}
func (f *fakeStreamW) bytes() []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]byte(nil), f.buf.Bytes()...)
}

// A wedged/slow client whose Write fails must not pin the handler: videoFeed
// should return promptly rather than loop forever.
func TestVideoFeedClosesOnWriteError(t *testing.T) {
	app, _ := testApp(t) // camera up by default
	app.hub.publish(fakeJPEG([]byte{0x01}))
	w := &fakeStreamW{writeErr: errors.New("broken pipe")}
	req := httptest.NewRequest("GET", "/video_feed", nil)

	done := make(chan struct{})
	go func() { app.videoFeed(w, req); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("videoFeed did not return on write error (wedged client could pin it)")
	}
}

// After a stall flips the camera down, a fresh subscriber must get the
// placeholder, never the stale real frame the hub still holds (codex #3).
func TestVideoFeedPlaceholderWhenCameraDown(t *testing.T) {
	app, _ := testApp(t)
	app.cam.setStatus(false, "stalled")
	real := fakeJPEG([]byte{0xDE, 0xAD, 0xBE, 0xEF}) // distinctive stale frame
	app.hub.publish(real)                            // hub.latest is now set

	req := httptest.NewRequest("GET", "/video_feed", nil)
	ctx, cancel := context.WithCancel(req.Context())
	req = req.WithContext(ctx)
	w := &fakeStreamW{onWrite: cancel} // end the stream after the first frame

	done := make(chan struct{})
	go func() { app.videoFeed(w, req); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("videoFeed did not return")
	}

	got := w.bytes()
	if !bytes.Contains(got, placeholderFrame) {
		t.Fatal("camera down: expected the placeholder frame")
	}
	if bytes.Contains(got, real) {
		t.Fatal("camera down: leaked the stale real frame")
	}
}

// tuneStreamConn must be a safe no-op when the context carries no conn or a
// non-TCP conn, and must not panic on a real TCP conn.
func TestTuneStreamConnSafe(t *testing.T) {
	tuneStreamConn(context.Background()) // no conn stashed

	type nonTCP struct{ net.Conn }
	ctx := context.WithValue(context.Background(), connCtxKey{}, net.Conn(nonTCP{}))
	tuneStreamConn(ctx) // wrong conn type → no-op

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	tuneStreamConn(context.WithValue(context.Background(), connCtxKey{}, c)) // real *net.TCPConn
}

// A stall (errStalled) must respawn quickly with the backoff reset, not escalate
// the exponential failure backoff (codex #2). Reset → ~1s spacing → ≥3 attempts
// inside 3s; exponential growth (1s,2s,4s…) would yield only 2.
func TestCameraStallRespawnsFastWithoutBackoffGrowth(t *testing.T) {
	var mu sync.Mutex
	calls := 0
	c := &Camera{mode: "rpicam"}
	c.attemptFn = func(ctx context.Context, hub *Hub, w, h int) error {
		mu.Lock()
		calls++
		mu.Unlock()
		return errStalled
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { c.run(ctx, newHub()); close(done) }()

	deadline := time.Now().Add(3 * time.Second)
	for {
		mu.Lock()
		n := calls
		mu.Unlock()
		if n >= 3 || time.Now().After(deadline) {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done

	mu.Lock()
	n := calls
	mu.Unlock()
	if n < 3 {
		t.Fatalf("stall should respawn ~1s apart (backoff reset); got %d attempts in 3s", n)
	}
	if up, e := c.status(); up || !strings.Contains(e, "restart") {
		t.Fatalf("after a stall, status should be down/restarting; up=%v err=%q", up, e)
	}
}

// runOnce against a producer that emits two frames then goes silent: the
// watchdog must end the attempt with errStalled, and status must have flipped up
// on the first frame (not at process start). `exec sleep` so the kill closes the
// stdout pipe (no orphaned child holding it open).
func TestRunOnceWatchdogStallAndFirstFrameStatus(t *testing.T) {
	if _, err := exec.LookPath("sh"); err != nil {
		t.Skip("sh not available")
	}
	c := &Camera{mode: "rpicam", stallTimeout: 200 * time.Millisecond}
	c.newCmd = func(ctx context.Context, w, h int) *exec.Cmd {
		return exec.CommandContext(ctx, "sh", "-c",
			`printf '\377\330\252\377\331\377\330\273\377\331'; exec sleep 30`)
	}
	hub := newHub()
	start := time.Now()
	err := c.runOnce(context.Background(), hub, 640, 480)
	if !errors.Is(err, errStalled) {
		t.Fatalf("silent producer should yield errStalled, got %v", err)
	}
	if d := time.Since(start); d > 3*time.Second {
		t.Fatalf("watchdog took too long to fire: %s", d)
	}
	if up, _ := c.status(); !up {
		t.Fatal("status should be up after the first frame was published")
	}
	if hub.latestFrame() == nil {
		t.Fatal("expected at least one frame published before the stall")
	}
}
