// rovercontrol — the single-file controller for the Waveshare UGV rover.
//
// Runs ON the rover. Owns the hardware directly and is the ONLY thing needed to
// drive it: it holds the ESP32 serial link (motors / lights / gimbal) and the
// camera (rpicam-vid MJPEG live view + still snapshots), reads a gamepad plugged
// into the Pi, and exposes everything over HTTP where the URL path IS the
// command (POST /move_left, /drive?l=&r=, /camera_up, /light_head?on=1, ...).
// Camera capture auto-selects a USB UVC cam (v4l2-ctl, MJPG) or a CSI cam
// (rpicam-vid) — see -camera-mode.
// It serves a built-in web UI at "/" as the default client. No CLI, no chatbot.
//
// It fully replaces (for control + camera use) the stock ugv_rpi app.py, the
// Python gallery, rover_direct.py, rover_camera.py, and rover_joystick.py.
// Dropped stock features (WebRTC, audio, CV modes, video recording, OLED,
// ESP-NOW, telemetry) are intentional non-goals — see docs/plans/002.
//
// Build for the rover (from this dir):
//
//	GOOS=linux GOARCH=arm64 go build -o ../rovercontrol-arm64 .
//
// One source file by request, stdlib only (no module dependencies). The serial
// port is configured with stty; the web UI is the embedded htmlPage constant.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// ───────────────────────── limits (ported from rover_direct.py) ─────────────

const (
	speedLimit = 0.5    // max |wheel speed|
	panMin     = -180.0 // gimbal pan
	panMax     = 180.0
	tiltMin    = -45.0 // gimbal tilt (+ is up)
	tiltMax    = 90.0
)

func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

// ───────────────────────────── serial link ─────────────────────────────────

// serialLink is the write side of the ESP32 UART. Abstracted so tests can
// capture the exact bytes without a real port.
type serialLink interface {
	io.Writer
	Close() error
}

// ttyLink configures the ESP32 UART for raw write-only 8N1 and writes
// newline-delimited JSON. We open with O_NONBLOCK so open() can't block on
// carrier, then `stty ... clocal raw` sets the line discipline (incl. CLOCAL),
// then we restore blocking writes. The feedback stream is disabled at init so
// we never need to read — the link is write-only.
type ttyLink struct {
	f *os.File
}

func openTTY(path string, baud int) (*ttyLink, error) {
	f, err := os.OpenFile(path, os.O_RDWR|syscall.O_NOCTTY|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	cfg := exec.Command("stty", "-F", path, strconv.Itoa(baud),
		"cs8", "-cstopb", "-parenb", "-crtscts", "clocal", "-echo", "raw")
	if out, err := cfg.CombinedOutput(); err != nil {
		f.Close()
		return nil, fmt.Errorf("stty %s: %v: %s", path, err, bytes.TrimSpace(out))
	}
	if err := syscall.SetNonblock(int(f.Fd()), false); err != nil {
		f.Close()
		return nil, err
	}
	return &ttyLink{f: f}, nil
}

func (l *ttyLink) Write(p []byte) (int, error) { return l.f.Write(p) }
func (l *ttyLink) Close() error                { return l.f.Close() }

// ───────────────────────────── rover hardware ──────────────────────────────

// Rover serializes all serial writes (HTTP handlers + joystick share it) and
// encodes the same JSON commands base_ctrl.py/rover_direct.py use.
type Rover struct {
	mu      sync.Mutex
	link    serialLink
	lastErr string
}

func (r *Rover) send(cmd map[string]any) error {
	b, err := json.Marshal(cmd)
	if err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.link == nil {
		return errors.New("serial unavailable")
	}
	_, err = r.link.Write(append(b, '\n'))
	return err
}

func (r *Rover) ok() bool { r.mu.Lock(); defer r.mu.Unlock(); return r.link != nil }

func (r *Rover) status() (bool, string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.link != nil, r.lastErr
}

func (r *Rover) setStatus(l serialLink, errMsg string) {
	r.mu.Lock()
	r.link, r.lastErr = l, errMsg
	r.mu.Unlock()
}

func (r *Rover) setLink(l serialLink) { r.setStatus(l, "") }

// closeLink closes and clears the serial link (used on graceful shutdown).
func (r *Rover) closeLink() {
	r.mu.Lock()
	l := r.link
	r.link, r.lastErr = nil, "shut down"
	r.mu.Unlock()
	if l != nil {
		l.Close()
	}
}

// initLink runs the boot sequence (echo off, feedback off, Gimbal module —
// required for pan/tilt) directly on a link before it is published, so a
// half-initialised port is never exposed.
func initLink(l serialLink) error {
	for _, c := range []map[string]any{
		{"T": 143, "cmd": 0},
		{"T": 131, "cmd": 0},
		{"T": 4, "cmd": 2},
	} {
		b, err := json.Marshal(c)
		if err != nil {
			return err
		}
		if _, err := l.Write(append(b, '\n')); err != nil {
			return err
		}
	}
	return nil
}

func (r *Rover) drive(left, right float64) error {
	return r.send(map[string]any{"T": 1,
		"L": clamp(left, -speedLimit, speedLimit),
		"R": clamp(right, -speedLimit, speedLimit)})
}

func (r *Rover) stopWheels() error { return r.send(map[string]any{"T": 1, "L": 0, "R": 0}) }

// estop halts wheels AND gimbal immediately.
func (r *Rover) estop() error {
	e1 := r.send(map[string]any{"T": 1, "L": 0, "R": 0})
	e2 := r.send(map[string]any{"T": 0})
	return errors.Join(e1, e2)
}

func (r *Rover) aimCamera(pan, tilt float64) (float64, float64, error) {
	pan = clamp(pan, panMin, panMax)
	tilt = clamp(tilt, tiltMin, tiltMax)
	return pan, tilt, r.send(map[string]any{
		"T": 133, "X": pan, "Y": tilt, "SPD": 0, "ACC": 0})
}

// lights: PWM 0..255. front = IO5 (head), base = IO4 (chassis).
func (r *Rover) lights(front, base int) error {
	cl := func(v int) int {
		if v < 0 {
			return 0
		}
		if v > 255 {
			return 255
		}
		return v
	}
	return r.send(map[string]any{"T": 132, "IO4": cl(base), "IO5": cl(front)})
}

func (r *Rover) gimbalTorque(lock bool) error {
	cmd := 0
	if lock {
		cmd = 1
	}
	return r.send(map[string]any{"T": 210, "id": 255, "cmd": cmd})
}

// ─────────────────── movement arbitration + watchdog ───────────────────────

// Movement is the single source of truth for wheel motion. Generation tokens
// stop stale nudge timers from cancelling newer commands; a watchdog fed by the
// internal drive path stops continuous motion if its source goes quiet.
type Movement struct {
	r   *Rover
	mu  sync.Mutex
	gen uint64 // bumped by every motion-setting call

	cap float64 // speed cap scaler for analog drive (0..speedLimit)

	moving   bool      // under a continuous lease (drive/joystick)?
	deadline time.Time // watchdog: stop continuous motion if now > deadline
	estopped bool      // latched until a fresh motion command
}

const watchdogTTL = 500 * time.Millisecond

func newMovement(r *Rover) *Movement { return &Movement{r: r, cap: 0.25} }

// applyDrive is the single funnel for wheel motion. The state decision AND the
// serial write happen under m.mu (r.mu nests inside m.mu — consistent order),
// so a stale nudge/watchdog stop can never interleave ahead of a newer command
// on the wire. continuous=true arms the watchdog (HTTP /drive, joystick);
// nudges (continuous=false) rely on their own timer instead. While e-stop is
// latched, nonzero motion is REFUSED — a zero command (recentered sticks or an
// explicit stop) releases the latch. Returns the (new) generation.
func (m *Movement) applyDrive(left, right float64, continuous bool) uint64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	nonzero := left != 0 || right != 0
	if m.estopped {
		if nonzero {
			return m.gen // refuse: stay stopped until released by a zero command
		}
		m.estopped = false // recentered / explicit zero releases the latch
	}
	m.gen++
	m.moving = continuous && nonzero
	if m.moving {
		m.deadline = time.Now().Add(watchdogTTL)
	}
	_ = m.r.drive(left, right)
	return m.gen
}

// setDrive is the continuous path (HTTP /drive and the joystick tick); it arms
// the watchdog and refreshes its deadline every call.
func (m *Movement) setDrive(left, right float64) uint64 {
	return m.applyDrive(left, right, true)
}

// driveCap drives at analog l/r (−1..1) scaled by the current speed cap.
func (m *Movement) driveCap(l, r float64) {
	c := m.getCap()
	m.setDrive(clamp(l, -1, 1)*c, clamp(r, -1, 1)*c)
}

// nudge drives at the cap for d, then stops — but only if no newer command
// (generation change) superseded it. Not watchdog-managed (its own timer stops
// it), so long ?ms nudges run their full duration.
func (m *Movement) nudge(l, r float64, d time.Duration) {
	g := m.applyDrive(l*m.getCap(), r*m.getCap(), false)
	time.AfterFunc(d, func() {
		m.mu.Lock()
		defer m.mu.Unlock()
		if g == m.gen { // not superseded → stop, write under the lock
			m.moving = false
			_ = m.r.stopWheels()
		}
	})
}

func (m *Movement) stop() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.gen++
	m.moving = false
	m.estopped = false // explicit stop clears the latch
	_ = m.r.stopWheels()
}

func (m *Movement) doEstop() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.gen++
	m.moving = false
	m.estopped = true
	_ = m.r.estop()
}

func (m *Movement) isEstopped() bool { m.mu.Lock(); defer m.mu.Unlock(); return m.estopped }

func (m *Movement) setCap(c float64) { m.mu.Lock(); m.cap = clamp(c, 0, speedLimit); m.mu.Unlock() }
func (m *Movement) getCap() float64  { m.mu.Lock(); defer m.mu.Unlock(); return m.cap }

// watchdogTick stops the wheels if a continuous lease went stale (its source
// stopped refreshing). Decision + write under m.mu. Returns true when it stopped.
func (m *Movement) watchdogTick(now time.Time) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.moving && now.After(m.deadline) {
		m.moving = false
		m.gen++
		_ = m.r.stopWheels()
		return true
	}
	return false
}

func (m *Movement) runWatchdog(ctx context.Context) {
	t := time.NewTicker(50 * time.Millisecond)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-t.C:
			m.watchdogTick(now)
		}
	}
}

// ─────────────────────── camera: hub + MJPEG splitter ──────────────────────

// Hub fans camera frames out to any number of MJPEG clients, latest-frame-wins
// (a slow client skips frames instead of building a backlog).
type Hub struct {
	mu     sync.Mutex
	subs   map[chan []byte]struct{}
	latest []byte
}

func newHub() *Hub { return &Hub{subs: make(map[chan []byte]struct{})} }

func (h *Hub) publish(frame []byte) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.latest = frame
	for ch := range h.subs {
		select {
		case ch <- frame:
		default:
			select {
			case <-ch:
			default:
			}
			select {
			case ch <- frame:
			default:
			}
		}
	}
}

func (h *Hub) latestFrame() []byte {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.latest
}

func (h *Hub) subscribe() (<-chan []byte, func()) {
	ch := make(chan []byte, 1)
	h.mu.Lock()
	if h.latest != nil {
		ch <- h.latest
	}
	h.subs[ch] = struct{}{}
	h.mu.Unlock()
	return ch, func() { h.mu.Lock(); delete(h.subs, ch); h.mu.Unlock() }
}

var soi = []byte{0xff, 0xd8} // JPEG start-of-image

// splitFrames splits an MJPEG stream SOI-to-next-SOI (rpicam-vid emits clean
// concatenated JFIF). It deliberately does NOT search for EOI, which can occur
// inside entropy-coded data.
func splitFrames(r io.Reader, emit func([]byte)) error {
	var buf []byte
	chunk := make([]byte, 64*1024)
	start := -1
	for {
		n, err := r.Read(chunk)
		if n > 0 {
			from := len(buf) - 1
			if from < 0 {
				from = 0
			}
			buf = append(buf, chunk[:n]...)
			for {
				i := bytes.Index(buf[from:], soi)
				if i < 0 {
					break
				}
				i += from
				if start >= 0 && i > start {
					frame := make([]byte, i-start)
					copy(frame, buf[start:i])
					emit(frame)
				}
				start = i
				from = i + 2
			}
			if start > 0 {
				buf = append(buf[:0], buf[start:]...)
				start = 0
			} else if start < 0 && len(buf) > 1 {
				buf = append(buf[:0], buf[len(buf)-1])
			}
		}
		if err != nil {
			if start >= 0 && len(buf) > start+2 {
				emit(buf[start:])
			}
			if err == io.EOF {
				return nil
			}
			return err
		}
	}
}

// Camera manages the capture process and tracks a user-visible status so a busy
// or missing camera surfaces in /healthz and the UI instead of silently looping.
// Two capture backends: "v4l2" (USB UVC cam via v4l2-ctl, MJPG) and "rpicam"
// (CSI via rpicam-vid). "auto" picks v4l2 when the device node exists.
type Camera struct {
	mode, device       string // resolved mode ("v4l2"/"rpicam"); V4L2 device path
	width, height, fps int

	// lastFrame is the UnixNano of the most recent published frame, read by the
	// stall watchdog. atomic so the splitter and watchdog don't race (-race).
	lastFrame atomic.Int64

	mu      sync.Mutex
	up      bool
	lastErr string

	// Test injection (all nil/zero in production):
	//   attemptFn    overrides runOnce entirely.
	//   newCmd       overrides the capture command runOnce spawns.
	//   stallTimeout overrides camStallTimeout for the watchdog.
	attemptFn    func(ctx context.Context, hub *Hub, w, h int) error
	newCmd       func(ctx context.Context, w, h int) *exec.Cmd
	stallTimeout time.Duration
}

// errStalled marks a capture attempt that was killed by the frame-staleness
// watchdog (producer alive but silent). Camera.run respawns it quickly instead
// of treating it as a hard failure (which would grow the backoff).
var errStalled = errors.New("camera produced no frames; restarting")

func (c *Camera) status() (bool, string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.up, c.lastErr
}

func (c *Camera) setStatus(up bool, err string) {
	c.mu.Lock()
	c.up, c.lastErr = up, err
	c.mu.Unlock()
}

// resolveCameraMode turns "auto" into a concrete backend: v4l2 if the device
// node exists (the USB cam on this rover), else rpicam (CSI). An explicit
// v4l2/rpicam is honored; anything else falls back to rpicam. Resolved once at
// startup (no hot-plug detection — pass -camera-mode explicitly if needed).
func resolveCameraMode(mode, device string) string {
	switch mode {
	case "v4l2", "rpicam", "off":
		return mode
	case "auto":
		if _, err := os.Stat(device); err == nil {
			return "v4l2"
		}
		return "rpicam"
	default:
		log.Printf("camera: unknown -camera-mode %q; using rpicam", mode)
		return "rpicam"
	}
}

// Default camera capture geometry — favours low-latency teleop over sharpness.
// Override per-launch with -width/-height/-fps. (USB/V4L2 honours -fps via
// --set-parm; see buildCameraCmd.)
const (
	defaultCamWidth  = 640
	defaultCamHeight = 480
	defaultCamFPS    = 15
)

// buildCameraCmd returns the capture command that writes concatenated MJPG to
// stdout. For v4l2, width/height are only set when both > 0 (so width=0 lets the
// camera pick a supported resolution — the unsized-retry escape hatch).
func buildCameraCmd(ctx context.Context, mode, device string, w, h, fps int) *exec.Cmd {
	if mode == "v4l2" {
		fmtArg := "--set-fmt-video=pixelformat=MJPG"
		if w > 0 && h > 0 {
			fmtArg = fmt.Sprintf("--set-fmt-video=width=%d,height=%d,pixelformat=MJPG", w, h)
		}
		args := []string{"-d", device, fmtArg}
		if fps > 0 {
			// Without --set-parm the USB cam ignores -fps and free-runs at its
			// default (~30fps), so the bandwidth/lag reduction never lands.
			args = append(args, fmt.Sprintf("--set-parm=%d", fps))
		}
		args = append(args, "--stream-mmap", "--stream-count=0", "--stream-to=-")
		return exec.CommandContext(ctx, "v4l2-ctl", args...)
	}
	// rpicam-vid: omit width/height/framerate when ≤0 so "0 = let the camera
	// choose" works for CSI too (not just v4l2).
	args := []string{"-n", "-t", "0", "--codec", "mjpeg"}
	if w > 0 && h > 0 {
		args = append(args, "--width", strconv.Itoa(w), "--height", strconv.Itoa(h))
	}
	if fps > 0 {
		args = append(args, "--framerate", strconv.Itoa(fps))
	}
	args = append(args, "-o", "-")
	return exec.CommandContext(ctx, "rpicam-vid", args...)
}

func (c *Camera) run(ctx context.Context, hub *Hub) {
	attempt := c.runOnce
	if c.attemptFn != nil {
		attempt = c.attemptFn
	}
	backoff := time.Second
	loggedFail := false
	unsizedExhausted := false // stop the unsized retry once it has also failed
	for ctx.Err() == nil {
		start := time.Now()
		err := attempt(ctx, hub, c.width, c.height)
		if ctx.Err() != nil {
			return
		}
		// codex: an unsupported resolution shouldn't be a permanent failure —
		// retry once unsized (v4l2 only). Once that also fails, stop retrying it
		// (glm: avoid double-spawning every backoff cycle). Surface BOTH errors.
		// A stall isn't a resolution problem, so don't probe unsized for one; but
		// if the unsized attempt itself streamed then stalled, treat that as a
		// stall too (fast respawn), not exhaustion.
		if err != nil && !errors.Is(err, errStalled) &&
			c.mode == "v4l2" && c.width > 0 && c.height > 0 && !unsizedExhausted {
			err2 := attempt(ctx, hub, 0, 0)
			if ctx.Err() != nil {
				return
			}
			if err2 == nil {
				err = nil
			} else if errors.Is(err2, errStalled) {
				err = errStalled
			} else {
				unsizedExhausted = true
				err = fmt.Errorf("sized: %v; unsized: %v", err, err2)
			}
		}
		// A stall (producer alive but silent, from either attempt) isn't a hard
		// failure: respawn quickly with backoff reset, and flip status down so the
		// UI shows the placeholder instead of the frozen last frame.
		if errors.Is(err, errStalled) {
			c.setStatus(false, "no frames; restarting")
			log.Printf("camera: %s stalled (no frames for %s); restarting", c.mode, camStallTimeout)
			backoff = time.Second
			loggedFail = false
			unsizedExhausted = false
			select {
			case <-time.After(backoff):
			case <-ctx.Done():
				return
			}
			continue
		}
		// A clean exit only counts as "healthy" if it actually streamed for a
		// while; a process that exits 0 immediately (e.g. a finite stream) is
		// treated as a failure so we never hot-loop spawning it.
		if err == nil && time.Since(start) > 3*time.Second {
			backoff = time.Second
			loggedFail = false
			unsizedExhausted = false // healthy stream — re-allow the probe later
		} else {
			if err == nil {
				err = errors.New("camera stream ended immediately")
			}
			c.setStatus(false, err.Error())
			if !loggedFail { // log a recurring failure once, not every retry
				log.Printf("camera: %s unavailable (%v); will keep retrying quietly", c.mode, err)
				loggedFail = true
			}
			if backoff < 15*time.Second {
				backoff *= 2
			}
		}
		// Always wait the (≥1s) backoff before re-spawning — no zero-delay path.
		select {
		case <-time.After(backoff):
		case <-ctx.Done():
			return
		}
	}
}

func (c *Camera) runOnce(ctx context.Context, hub *Hub, w, h int) error {
	// Per-attempt context so the stall watchdog can kill a silent-but-alive
	// producer (which otherwise wedges splitFrames' blocking Read forever).
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	build := c.newCmd
	if build == nil {
		build = func(ctx context.Context, w, h int) *exec.Cmd {
			return buildCameraCmd(ctx, c.mode, c.device, w, h, c.fps)
		}
	}
	cmd := build(ctx, w, h)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	var stderr tailBuffer // keep the last bytes of stderr for diagnosis
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return err
	}
	if c.mode == "v4l2" {
		log.Printf("camera: v4l2 started (%dx%d, %s)", w, h, c.device)
	} else {
		log.Printf("camera: rpicam started (%dx%d@%dfps)", w, h, c.fps)
	}

	// Frame-staleness watchdog. The clock starts now (an implicit first-frame
	// grace ≈ stall timeout); each published frame refreshes it. If no frame
	// arrives within the timeout, cancel the producer so this attempt ends and
	// run() respawns it.
	timeout := c.stallTimeout
	if timeout <= 0 {
		timeout = camStallTimeout
	}
	c.lastFrame.Store(time.Now().UnixNano())
	stalled := make(chan struct{})
	watchdogDone := make(chan struct{})
	go func() {
		defer close(watchdogDone)
		tick := timeout / 2
		if tick <= 0 || tick > time.Second {
			tick = time.Second
		}
		t := time.NewTicker(tick)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				if time.Since(time.Unix(0, c.lastFrame.Load())) > timeout {
					close(stalled)
					cancel()
					return
				}
			}
		}
	}()

	// Status flips up only on the FIRST published frame (not at process start),
	// so a respawn that produces nothing keeps the camera "down" and videoFeed
	// serves the placeholder rather than the stale pre-stall frame (codex #1).
	firstFrame := true
	splitErr := splitFrames(stdout, func(frame []byte) {
		c.lastFrame.Store(time.Now().UnixNano())
		if firstFrame {
			firstFrame = false
			c.setStatus(true, "")
		}
		hub.publish(frame)
	})
	waitErr := cmd.Wait()
	cancel()
	<-watchdogDone

	// If the watchdog fired, report it as a stall (fast respawn) rather than a
	// generic producer error.
	select {
	case <-stalled:
		return errStalled
	default:
	}
	if waitErr != nil {
		if s := strings.TrimSpace(stderr.String()); s != "" {
			return fmt.Errorf("%v: %s", waitErr, lastLine(s))
		}
		return waitErr
	}
	return splitErr
}

// tailBuffer keeps only the last ~2KB written — bounded stderr capture so a
// chatty producer can't grow memory, while still surfacing the error tail.
type tailBuffer struct {
	mu  sync.Mutex
	buf []byte
}

func (t *tailBuffer) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, p...)
	if len(t.buf) > 2048 {
		t.buf = t.buf[len(t.buf)-2048:]
	}
	return len(p), nil
}

func (t *tailBuffer) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}

func lastLine(s string) string {
	s = strings.TrimRight(s, "\r\n")
	if i := strings.LastIndexAny(s, "\r\n"); i >= 0 {
		return s[i+1:]
	}
	return s
}

// CameraAim tracks the current pan/tilt so HTTP nudges and the joystick share
// one absolute aim.
type CameraAim struct {
	r         *Rover
	mu        sync.Mutex
	pan, tilt float64
}

func (a *CameraAim) set(pan, tilt float64) (float64, float64) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.setLocked(pan, tilt)
}

// setLocked writes the absolute aim and records it; the caller holds a.mu so a
// concurrent nudge can't read a stale base and lose this update. The serial
// write (aimCamera → r.mu) happens under a.mu — ordering a.mu→r.mu is consistent
// (nothing takes a.mu while holding r.mu) and the write is camera-only, ms-bounded.
func (a *CameraAim) setLocked(pan, tilt float64) (float64, float64) {
	p, t, _ := a.r.aimCamera(pan, tilt)
	a.pan, a.tilt = p, t
	return p, t
}

func (a *CameraAim) nudge(dPan, dTilt float64) (float64, float64) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.setLocked(a.pan+dPan, a.tilt+dTilt)
}

func (a *CameraAim) center() (float64, float64) { return a.set(0, 0) }

func (a *CameraAim) get() (float64, float64) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.pan, a.tilt
}

// App ties the hardware, camera, and photo store together; both the HTTP layer
// and the joystick drive it through the same methods.
type App struct {
	rover *Rover
	move  *Movement
	aim   *CameraAim
	hub   *Hub
	cam   *Camera

	photoDir string
	snapMu   sync.Mutex
	snapSeq  int

	lightMu        sync.Mutex
	headOn, baseOn bool

	gpMu sync.Mutex
	gpUp bool

	mapping   *GamepadMapping // gamepad control mapping (default or from config)
	mapSource string          // "default" | "config" | "invalid"
}

// updateLights computes the new on/off state from the current state under a
// single held lock (so concurrent toggles/sets can't lose an update), commits
// it, then writes the PWM after unlocking. Returns the new (head, base) state.
func (app *App) updateLights(next func(h, b bool) (bool, bool)) (bool, bool, error) {
	app.lightMu.Lock()
	defer app.lightMu.Unlock()
	h, b := next(app.headOn, app.baseOn)
	app.headOn, app.baseOn = h, b
	hv, bv := 0, 0
	if h {
		hv = 255
	}
	if b {
		bv = 255
	}
	// Write under lightMu so the hardware write order matches the commit order —
	// two concurrent toggles can't leave the lights disagreeing with state.
	// lightMu→r.mu ordering is consistent (nothing takes lightMu while holding
	// r.mu); the write is a single LED command, ms-bounded.
	return h, b, app.rover.lights(hv, bv)
}

func (app *App) setLights(head, base bool) error {
	_, _, err := app.updateLights(func(_, _ bool) (bool, bool) { return head, base })
	return err
}

func (app *App) toggleHead() (bool, error) {
	h, _, err := app.updateLights(func(h, b bool) (bool, bool) { return !h, b })
	return h, err
}

func (app *App) toggleBase() (bool, error) {
	_, b, err := app.updateLights(func(h, b bool) (bool, bool) { return h, !b })
	return b, err
}

// snapshot writes the latest camera frame to the photo dir, collision-safe
// across processes (os.Link fails if the name exists → bump the counter), so a
// concurrent Python-side capture in the same second is never overwritten.
func (app *App) snapshot(now time.Time) (string, error) {
	frame := app.hub.latestFrame()
	if frame == nil {
		return "", errors.New("no camera frame yet")
	}
	if err := os.MkdirAll(app.photoDir, 0o755); err != nil {
		return "", err
	}
	tmp, err := os.CreateTemp(app.photoDir, ".snap-*.tmp")
	if err != nil {
		return "", err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(frame); err != nil {
		tmp.Close()
		return "", err
	}
	if err := tmp.Close(); err != nil {
		return "", err
	}
	stamp := now.Format("rover_20060102_150405_")
	app.snapMu.Lock()
	defer app.snapMu.Unlock()
	for i := 0; i < 1000; i++ {
		app.snapSeq++
		name := fmt.Sprintf("%s%03d.jpg", stamp, app.snapSeq)
		if err := os.Link(tmp.Name(), filepath.Join(app.photoDir, name)); err == nil {
			return name, nil
		} else if !os.IsExist(err) {
			return "", err
		}
	}
	return "", fmt.Errorf("no free filename for %s*", stamp)
}

func (app *App) listPhotos() []string {
	entries, err := os.ReadDir(app.photoDir)
	if err != nil {
		return nil
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() && safePhotoName(e.Name()) {
			names = append(names, e.Name())
		}
	}
	sort.Sort(sort.Reverse(sort.StringSlice(names)))
	return names
}

// placeholderFrame is a tiny valid JPEG served as the live frame when the camera
// is unavailable, so the UI <img> shows "no signal" instead of hanging.
var placeholderFrame = buildPlaceholder()

func buildPlaceholder() []byte {
	// minimal 1x1 grey baseline JPEG
	return []byte{
		0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0x00, 0x01,
		0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xff, 0xdb, 0x00, 0x43,
		0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
		0x09, 0x08, 0x0a, 0x0c, 0x14, 0x0d, 0x0c, 0x0b, 0x0b, 0x0c, 0x19, 0x12,
		0x13, 0x0f, 0x14, 0x1d, 0x1a, 0x1f, 0x1e, 0x1d, 0x1a, 0x1c, 0x1c, 0x20,
		0x24, 0x2e, 0x27, 0x20, 0x22, 0x2c, 0x23, 0x1c, 0x1c, 0x28, 0x37, 0x29,
		0x2c, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1f, 0x27, 0x39, 0x3d, 0x38, 0x32,
		0x3c, 0x2e, 0x33, 0x34, 0x32, 0xff, 0xc0, 0x00, 0x0b, 0x08, 0x00, 0x01,
		0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xff, 0xc4, 0x00, 0x1f, 0x00, 0x00,
		0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
		0x09, 0x0a, 0x0b, 0xff, 0xc4, 0x00, 0xb5, 0x10, 0x00, 0x02, 0x01, 0x03,
		0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7d,
		0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
		0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x08,
		0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52, 0xd1, 0xf0, 0x24, 0x33, 0x62, 0x72,
		0x82, 0x09, 0x0a, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x25, 0x26, 0x27, 0x28,
		0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44, 0x45,
		0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
		0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74, 0x75,
		0x76, 0x77, 0x78, 0x79, 0x7a, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
		0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3,
		0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6,
		0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9,
		0xca, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2,
		0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea, 0xf1, 0xf2, 0xf3, 0xf4,
		0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa, 0xff, 0xda, 0x00, 0x08, 0x01, 0x01,
		0x00, 0x00, 0x3f, 0x00, 0xf7, 0xfa, 0x28, 0xa2, 0x80, 0x3f, 0xff, 0xd9,
	}
}

// ───────────────────────── joystick (raw /dev/input/js0) ────────────────────

// Linux joystick API event: time u32[0:4], value i16[4:6], type u8[6],
// number u8[7] (little-endian). type carries JS_EVENT_INIT (0x80) on the
// synthetic burst sent at open.
const (
	jsEventButton = 0x01
	jsEventAxis   = 0x02
	jsEventInit   = 0x80
)

type jsEvent struct {
	value  int16
	etype  uint8 // with the 0x80 init bit already masked off
	isInit bool
	number uint8
}

func parseJSEvent(b []byte) (jsEvent, bool) {
	if len(b) < 8 {
		return jsEvent{}, false
	}
	val := int16(uint16(b[4]) | uint16(b[5])<<8) // signed little-endian
	et := b[6]
	return jsEvent{
		value:  val,
		etype:  et &^ jsEventInit,
		isInit: et&jsEventInit != 0,
		number: b[7],
	}, true
}

// Behavioral tuning (NOT part of the per-pad mapping — see GamepadMapping).
const (
	deadzone = 0.15
	turbo    = 0.40
	ramp     = 1.2  // max wheel-speed change per second (slew-rate limit)
	panRate  = 90.0 // deg/sec at full stick
	tiltRate = 70.0
	jsRateHz = 25.0
)

var speedSteps = []float64{0.15, 0.20, 0.25, 0.30, 0.40}

// ───────────────────── gamepad mapping (per-pad, configurable) ──────────────

// AxisMap is a stick/trigger axis index plus a sign: Invert=true means
// stick-up / stick-right reads as +1 regardless of the pad's raw polarity.
type AxisMap struct {
	Index  int  `json:"index"`
	Invert bool `json:"invert"`
}

// HatMap models the D-pad's three real shapes across pads.
type HatMap struct {
	Kind string  `json:"kind"`           // "axis" | "buttons" | "none"
	Axis AxisMap `json:"axis,omitempty"` // Kind=="axis": vertical axis (+ = up)
	Up   int     `json:"up,omitempty"`   // Kind=="buttons": up button index
	Down int     `json:"down,omitempty"` // Kind=="buttons": down button index
}

// ControlMap is an optional control bound to a button, a held trigger-axis, or
// disabled. Unlike a bare int it distinguishes "none" from "button 0", and it
// handles triggers that report as axes. Default zero value is Kind=="" → treated
// as disabled.
type ControlMap struct {
	Kind  string  `json:"kind"`            // "button" | "axis" | "none" | ""
	Index int     `json:"index,omitempty"` // Kind=="button"
	Axis  AxisMap `json:"axis,omitempty"`  // Kind=="axis": held when axisSigned > 0.5
}

func (c ControlMap) held(st gpState) bool {
	switch c.Kind {
	case "button":
		return st.button(c.Index)
	case "axis":
		return axisSigned(st, c.Axis) > 0.5
	}
	return false
}

// UnmarshalJSON accepts three forms so old configs keep working (plan 007):
//   - null            → leave the (pre-seeded default) value unchanged
//   - a number N      → {Kind:"button", Index:N}  (legacy bare-int controls)
//   - an object {...} → the current ControlMap shape
func (c *ControlMap) UnmarshalJSON(b []byte) error {
	if string(b) == "null" { // Go calls this for explicit null; keep the default
		return nil
	}
	if n := bytes.TrimSpace(b); len(n) > 0 && (n[0] == '-' || (n[0] >= '0' && n[0] <= '9')) {
		var idx int
		if err := json.Unmarshal(b, &idx); err != nil {
			return err
		}
		*c = ControlMap{Kind: "button", Index: idx}
		return nil
	}
	type alias ControlMap // avoid recursing into this method
	var a alias
	if err := json.Unmarshal(b, &a); err != nil {
		return err
	}
	*c = ControlMap(a)
	return nil
}

// GamepadMapping carries only what identifies a pad's controls (indices + axis
// signs). Tuning (deadzone/rates/speedSteps) stays in code constants.
type GamepadMapping struct {
	Throttle  AxisMap    `json:"throttle"` // left stick Y (up = forward)
	Steer     AxisMap    `json:"steer"`    // left stick X (right = right)
	Pan       AxisMap    `json:"pan"`      // right stick X (right = pan right)
	Tilt      AxisMap    `json:"tilt"`     // right stick Y (up = tilt up)
	Turbo     ControlMap `json:"turbo"`    // hold for higher top speed
	Stop      ControlMap `json:"stop"`
	Estop     ControlMap `json:"estop"`
	HeadLight ControlMap `json:"head_light"`
	BaseLight ControlMap `json:"base_light"`
	Center    ControlMap `json:"center"`
	Snapshot  ControlMap `json:"snapshot"`
	Relax     ControlMap `json:"relax"`
	Lock      ControlMap `json:"lock"`
	Hat       HatMap     `json:"hat"` // D-pad vertical → speed cap

	// Optional, default-disabled (enabled by -calibrate). Plan 006.
	Precision ControlMap `json:"precision"`  // hold → slow mode
	Boost     ControlMap `json:"boost"`      // hold → max speed
	PanicStop ControlMap `json:"panic_stop"` // press → instant e-stop (2nd panic)
	HatX      HatMap     `json:"hat_x"`      // D-pad horizontal → fine camera pan
}

// defaultMapping reproduces EXACTLY the historical hard-coded constants (and the
// signs the old loop applied), so with no config file behavior is unchanged.
func defaultMapping() GamepadMapping {
	return GamepadMapping{
		Throttle:  AxisMap{1, true},                      // throttle = -axis(LY): up = forward
		Steer:     AxisMap{0, false},                     // axis(LX)
		Pan:       AxisMap{3, false},                     // axis(RX)
		Tilt:      AxisMap{4, true},                      // dTilt = -axis(RY): up = tilt up
		Turbo:     ControlMap{Kind: "button", Index: 5},  // RB
		Stop:      ControlMap{Kind: "button", Index: 0},  // A
		Snapshot:  ControlMap{Kind: "button", Index: 1},  // B
		HeadLight: ControlMap{Kind: "button", Index: 2},  // X
		Center:    ControlMap{Kind: "button", Index: 3},  // Y
		BaseLight: ControlMap{Kind: "button", Index: 4},  // LB
		Estop:     ControlMap{Kind: "button", Index: 6},  // Back
		Relax:     ControlMap{Kind: "button", Index: 9},  // L3
		Lock:      ControlMap{Kind: "button", Index: 10}, // R3
		// D-pad vertical on axis 7; up (raw negative) => +1 via Invert.
		Hat: HatMap{Kind: "axis", Axis: AxisMap{7, true}},
		// New optional controls default DISABLED — enabled via -calibrate so we
		// never guess colliding indices (plan 006).
		Precision: ControlMap{Kind: "none"},
		Boost:     ControlMap{Kind: "none"},
		PanicStop: ControlMap{Kind: "none"},
		HatX:      HatMap{Kind: "none"},
	}
}

func (c ControlMap) validate() error {
	switch c.Kind {
	case "button":
		if c.Index < 0 {
			return fmt.Errorf("negative button index %d", c.Index)
		}
	case "axis":
		if c.Axis.Index < 0 {
			return fmt.Errorf("negative axis index %d", c.Axis.Index)
		}
	case "none", "":
	default:
		return fmt.Errorf("invalid control kind %q", c.Kind)
	}
	return nil
}

func validateHat(h HatMap) error {
	switch h.Kind {
	case "axis":
		if h.Axis.Index < 0 {
			return fmt.Errorf("negative hat axis %d", h.Axis.Index)
		}
	case "buttons":
		if h.Up < 0 || h.Down < 0 {
			return fmt.Errorf("negative hat button index")
		}
	case "none", "":
	default:
		return fmt.Errorf("invalid hat kind %q", h.Kind)
	}
	return nil
}

func (m GamepadMapping) validate() error {
	for _, i := range []int{m.Throttle.Index, m.Steer.Index, m.Pan.Index, m.Tilt.Index} {
		if i < 0 {
			return fmt.Errorf("negative stick axis %d", i)
		}
	}
	if err := validateHat(m.Hat); err != nil {
		return err
	}
	if err := validateHat(m.HatX); err != nil {
		return err
	}
	ctrls := []ControlMap{m.Turbo, m.Stop, m.Estop, m.HeadLight, m.BaseLight,
		m.Center, m.Snapshot, m.Relax, m.Lock, m.Precision, m.Boost, m.PanicStop}
	for _, c := range ctrls {
		if err := c.validate(); err != nil {
			return err
		}
	}
	// Safety net: warn loudly if e-stop is left with no binding at all.
	if m.Estop.Kind == "none" && m.PanicStop.Kind == "none" {
		log.Printf("gamepad: WARNING — no e-stop button is bound (both estop and panic_stop are disabled)")
	}
	// Non-fatal: warn if two button-controls share an index (likely a calibration
	// slip — e.g. a control landing on an already-used button).
	seen := map[int]bool{}
	for _, c := range ctrls {
		if c.Kind != "button" { // never treat axis/none as button 0
			continue
		}
		if seen[c.Index] {
			log.Printf("gamepad: warning — button %d is bound to more than one action", c.Index)
		}
		seen[c.Index] = true
	}
	return nil
}

// loadMapping returns (mapping, source). Missing file → default. Present →
// unmarshalled OVER the default (so partial JSON keeps defaults), then
// validated. A malformed/invalid file returns an error WITHOUT falling back —
// the caller disables the gamepad so the rover never drives with wrong controls.
func loadMapping(path string) (GamepadMapping, string, error) {
	m := defaultMapping()
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return m, "default", nil
		}
		return GamepadMapping{}, "invalid", err
	}
	if err := json.Unmarshal(b, &m); err != nil {
		return GamepadMapping{}, "invalid", fmt.Errorf("parse %s: %w", path, err)
	}
	if err := m.validate(); err != nil {
		return GamepadMapping{}, "invalid", fmt.Errorf("validate %s: %w", path, err)
	}
	return m, "config", nil
}

// ── per-tick decision (pure, unit-testable without hardware) ────────────────

// gpState is a snapshot accessor over the live gamepad (or a fake in tests).
type gpState struct {
	axis   func(idx int) float64
	button func(idx int) bool
}

func axisSigned(st gpState, a AxisMap) float64 {
	v := st.axis(a.Index)
	if a.Invert {
		v = -v
	}
	return v
}

func hatDirection(h HatMap, st gpState) int {
	switch h.Kind {
	case "axis":
		v := axisSigned(st, h.Axis)
		if v > 0.5 {
			return 1
		} else if v < -0.5 {
			return -1
		}
	case "buttons":
		if st.button(h.Up) {
			return 1
		}
		if st.button(h.Down) {
			return -1
		}
	}
	return 0
}

// gpPrev carries edge-detection state across ticks. Edge controls are keyed by
// NAME (not button index) so two controls sharing a button — or a trigger-axis
// control — each get their own rising-edge slot.
type gpPrev struct {
	ctrl  map[string]bool
	hat   int  // vertical D-pad (speed cap)
	hatX  int  // horizontal D-pad (camera pan)
	panic bool // PanicStop held-state for its own rising edge
}

// gpActions is the decision for one tick: which buttons fired (rising edge),
// the speed-cap step, the deadzoned signed stick values, and the held modifiers.
type gpActions struct {
	stop, estop, head, base, snap, center, relax, lock bool
	hatDelta, panNudge                                 int
	throttle, steer, pan, tilt                         float64
	turbo, boost, precision                            bool
}

// computeJoystick reads the mapping against a state snapshot and updates prev.
func computeJoystick(m *GamepadMapping, st gpState, prev *gpPrev) gpActions {
	// ctrlEdge: rising edge of a ControlMap (button or trigger-axis), keyed by
	// control name so distinct controls never share an edge slot.
	ctrlEdge := func(name string, c ControlMap) bool {
		now := c.held(st)
		fired := now && !prev.ctrl[name]
		prev.ctrl[name] = now
		return fired
	}
	var a gpActions
	a.stop = ctrlEdge("stop", m.Stop)
	a.head = ctrlEdge("head", m.HeadLight)
	a.base = ctrlEdge("base", m.BaseLight)
	a.snap = ctrlEdge("snap", m.Snapshot)
	a.center = ctrlEdge("center", m.Center)
	a.relax = ctrlEdge("relax", m.Relax)
	a.lock = ctrlEdge("lock", m.Lock)
	a.turbo = m.Turbo.held(st)
	a.boost = m.Boost.held(st)
	a.precision = m.Precision.held(st)
	// e-stop: the Estop control OR the optional PanicStop, each on a rising edge.
	panicNow := m.PanicStop.held(st)
	panicEdge := panicNow && !prev.panic
	prev.panic = panicNow
	a.estop = ctrlEdge("estop", m.Estop) || panicEdge
	hd := hatDirection(m.Hat, st) // speed cap on rising edge only
	if hd != 0 && prev.hat == 0 {
		a.hatDelta = hd
	}
	prev.hat = hd
	hx := hatDirection(m.HatX, st) // camera pan nudge on rising edge (+ = right)
	if hx != 0 && prev.hatX == 0 {
		a.panNudge = hx
	}
	prev.hatX = hx
	a.throttle = dz(axisSigned(st, m.Throttle))
	a.steer = dz(axisSigned(st, m.Steer))
	a.pan = dz(axisSigned(st, m.Pan))
	a.tilt = dz(axisSigned(st, m.Tilt))
	return a
}

const precisionCap = 0.15 // top speed while the precision modifier is held
const fineNudgeDeg = 10.0 // camera pan per D-pad ←/→ press

// topSpeed resolves the wheel speed cap for one tick. Precedence: start at the
// selected step, apply turbo, then boost (max), then precision LAST so it always
// wins (slow mode is the safety, so it overrides turbo/boost).
func topSpeed(idx int, turboHeld, boostHeld, precisionHeld bool) float64 {
	top := speedSteps[idx]
	if turboHeld {
		top = turbo
	}
	if boostHeld {
		top = speedLimit
	}
	if precisionHeld && top > precisionCap {
		top = precisionCap
	}
	return top
}

func dz(v float64) float64 {
	if v < deadzone && v > -deadzone {
		return 0
	}
	return v
}

func rampToward(cur, tgt, step float64) float64 {
	return cur + clamp(tgt-cur, -step, step)
}

// driveMix turns throttle/steer into clamped differential wheel targets.
func driveMix(throttle, steer, top float64) (float64, float64) {
	l := clamp(throttle+steer, -1, 1) * top
	r := clamp(throttle-steer, -1, 1) * top
	return l, r
}

// gamepad holds the latest raw axis/button state from the reader goroutine.
type gamepad struct {
	mu      sync.Mutex
	axes    map[uint8]float64
	buttons map[uint8]bool
}

// state exposes the gamepad as a gpState (int indices) for computeJoystick.
func (g *gamepad) state() gpState {
	return gpState{
		axis:   func(i int) float64 { return g.axis(uint8(i)) },
		button: func(i int) bool { return g.button(uint8(i)) },
	}
}

func newGamepad() *gamepad {
	return &gamepad{axes: map[uint8]float64{}, buttons: map[uint8]bool{}}
}

func (g *gamepad) apply(e jsEvent) {
	g.mu.Lock()
	defer g.mu.Unlock()
	switch e.etype {
	case jsEventAxis:
		g.axes[e.number] = float64(e.value) / 32767.0
	case jsEventButton:
		g.buttons[e.number] = e.value != 0
	}
}

func (g *gamepad) axis(n uint8) float64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.axes[n]
}

func (g *gamepad) button(n uint8) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.buttons[n]
}

// reader blocks on the device, parsing events into the shared gamepad state.
func (g *gamepad) reader(ctx context.Context, dev io.Reader) {
	buf := make([]byte, 8)
	for ctx.Err() == nil {
		if _, err := io.ReadFull(dev, buf); err != nil {
			return
		}
		if e, ok := parseJSEvent(buf); ok {
			g.apply(e) // init events just seed state; edges handled in the ticker
		}
	}
}

// driveGate decides whether joystickLoop should command drive this tick. While the
// stick is active (or ramping to a stop) it commands; once fully idle it goes silent
// so an idle gamepad doesn't override HTTP /drive. wasActive carries the previous
// tick's state so exactly one final stop is emitted on the active→idle transition.
func driveGate(tgtL, tgtR, curL, curR float64, wasActive bool) (emit, active bool) {
	active = tgtL != 0 || tgtR != 0 || curL != 0 || curR != 0
	return active || wasActive, active
}

// joystickLoop applies the deadzone + slew ramp and commands motion every tick
// (a held-steady stick emits no events, so motion must be re-commanded on a
// fixed dt — and that refresh also keeps the watchdog from auto-stopping a
// gamepad driver). debugFn, if set, is called with the live state instead.
func (app *App) joystickLoop(ctx context.Context, g *gamepad) {
	dt := 1.0 / jsRateHz
	ticker := time.NewTicker(time.Duration(dt * float64(time.Second)))
	defer ticker.Stop()

	var left, right float64
	var wasActive bool
	speedIdx := 2
	prev := &gpPrev{ctrl: map[string]bool{}}
	st := g.state()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}

		a := computeJoystick(app.mapping, st, prev)
		if a.center {
			app.aim.center()
		}
		if a.stop {
			app.move.stop()
		}
		if a.head {
			app.toggleHead()
		}
		if a.base {
			app.toggleBase()
		}
		if a.snap {
			go app.snapshot(time.Now())
		}
		if a.estop {
			left, right = 0, 0
			app.move.doEstop() // latches in Movement; held sticks stay refused
		}
		if a.relax {
			app.rover.gimbalTorque(false)
		}
		if a.lock {
			app.rover.gimbalTorque(true)
		}
		if a.hatDelta != 0 {
			speedIdx = int(clamp(float64(speedIdx+a.hatDelta), 0, float64(len(speedSteps)-1)))
			app.move.setCap(speedSteps[speedIdx])
		}
		if a.panNudge != 0 { // D-pad ←/→ fine camera pan
			app.aim.nudge(float64(a.panNudge)*fineNudgeDeg, 0)
		}

		top := topSpeed(speedIdx, a.turbo, a.boost, a.precision)
		tgtL, tgtR := driveMix(a.throttle, a.steer, top)
		step := ramp * dt
		left = rampToward(left, tgtL, step)
		right = rampToward(right, tgtR, step)
		// Only command drive while the stick is active (or ramping to a stop);
		// an idle gamepad stays silent so HTTP /drive (chatbot) gets through.
		emit, active := driveGate(tgtL, tgtR, left, right, wasActive)
		if emit {
			app.move.setDrive(left, right)
		}
		wasActive = active

		if a.pan != 0 || a.tilt != 0 {
			app.aim.nudge(a.pan*panRate*dt, a.tilt*tiltRate*dt)
		}
	}
}

// ───────────────────────────── HTTP layer ──────────────────────────────────

func (app *App) gamepadPresent() bool { app.gpMu.Lock(); defer app.gpMu.Unlock(); return app.gpUp }
func (app *App) setGamepad(up bool)   { app.gpMu.Lock(); app.gpUp = up; app.gpMu.Unlock() }

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func errJSON(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]any{"ok": false, "error": msg})
}

// floatParam returns def if the query param is absent, an error if it's present
// but malformed (we reject rather than silently coerce).
func floatParam(r *http.Request, name string, def float64) (float64, error) {
	s := r.URL.Query().Get(name)
	if s == "" {
		return def, nil
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0, fmt.Errorf("bad %s=%q", name, s)
	}
	if math.IsNaN(v) || math.IsInf(v, 0) {
		// NaN/Inf survive clamp() and would poison shared state (m.cap) or make
		// writeJSON fail after the 200 header — reject them up front.
		return 0, fmt.Errorf("bad %s=%q", name, s)
	}
	return v, nil
}

// requireSerial reports 503 when the ESP32 link is down; control endpoints use
// it so a busy/missing port surfaces clearly instead of silently no-op'ing.
func (app *App) requireSerial(w http.ResponseWriter) bool {
	if !app.rover.ok() {
		errJSON(w, http.StatusServiceUnavailable, "serial unavailable")
		return false
	}
	return true
}

const nudgeDefaultMS = 400

func (app *App) routes() http.Handler {
	mux := http.NewServeMux()

	// movement nudges: l/r direction, scaled by the speed cap, auto-stop after ?ms
	nudge := func(l, r float64) http.HandlerFunc {
		return func(w http.ResponseWriter, req *http.Request) {
			ms, err := floatParam(req, "ms", nudgeDefaultMS)
			if err != nil {
				errJSON(w, http.StatusBadRequest, err.Error())
				return
			}
			if !app.requireSerial(w) {
				return
			}
			app.move.nudge(l, r, time.Duration(clamp(ms, 0, 5000))*time.Millisecond)
			writeJSON(w, 200, map[string]any{"ok": true})
		}
	}
	mux.Handle("POST /move_forward", nudge(1, 1))
	mux.Handle("POST /move_back", nudge(-1, -1))
	mux.Handle("POST /move_left", nudge(-1, 1))
	mux.Handle("POST /move_right", nudge(1, -1))

	mux.HandleFunc("POST /stop", func(w http.ResponseWriter, r *http.Request) {
		if !app.requireSerial(w) {
			return
		}
		app.move.stop()
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	mux.HandleFunc("POST /estop", func(w http.ResponseWriter, r *http.Request) {
		if !app.requireSerial(w) {
			return
		}
		app.move.doEstop()
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	mux.HandleFunc("POST /drive", func(w http.ResponseWriter, r *http.Request) {
		l, err := floatParam(r, "l", 0)
		if err != nil {
			errJSON(w, http.StatusBadRequest, err.Error())
			return
		}
		rr, err := floatParam(r, "r", 0)
		if err != nil {
			errJSON(w, http.StatusBadRequest, err.Error())
			return
		}
		if !app.requireSerial(w) {
			return
		}
		app.move.driveCap(l, rr)
		writeJSON(w, 200, map[string]any{"ok": true})
	})

	// camera gimbal
	camNudge := func(dp, dt float64) http.HandlerFunc {
		return func(w http.ResponseWriter, req *http.Request) {
			deg, err := floatParam(req, "deg", 15)
			if err != nil {
				errJSON(w, http.StatusBadRequest, err.Error())
				return
			}
			if !app.requireSerial(w) {
				return
			}
			p, t := app.aim.nudge(dp*deg, dt*deg)
			writeJSON(w, 200, map[string]any{"ok": true, "pan": p, "tilt": t})
		}
	}
	mux.Handle("POST /camera_up", camNudge(0, 1))
	mux.Handle("POST /camera_down", camNudge(0, -1))
	mux.Handle("POST /camera_left", camNudge(-1, 0))
	mux.Handle("POST /camera_right", camNudge(1, 0))
	mux.HandleFunc("POST /camera_center", func(w http.ResponseWriter, r *http.Request) {
		if !app.requireSerial(w) {
			return
		}
		p, t := app.aim.center()
		writeJSON(w, 200, map[string]any{"ok": true, "pan": p, "tilt": t})
	})
	mux.HandleFunc("POST /camera_aim", func(w http.ResponseWriter, r *http.Request) {
		pan, err := floatParam(r, "pan", 0)
		if err != nil {
			errJSON(w, http.StatusBadRequest, err.Error())
			return
		}
		tilt, err := floatParam(r, "tilt", 0)
		if err != nil {
			errJSON(w, http.StatusBadRequest, err.Error())
			return
		}
		if !app.requireSerial(w) {
			return
		}
		p, t := app.aim.set(pan, tilt)
		writeJSON(w, 200, map[string]any{"ok": true, "pan": p, "tilt": t})
	})

	// lights
	lightToggle := func(which string) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			on, err := floatParam(r, "on", -1)
			if err != nil {
				errJSON(w, http.StatusBadRequest, err.Error())
				return
			}
			if !app.requireSerial(w) {
				return
			}
			var state bool
			var e error
			switch {
			case on < 0: // no explicit value → toggle
				if which == "head" {
					state, e = app.toggleHead()
				} else {
					state, e = app.toggleBase()
				}
			default:
				state = on != 0
				if which == "head" {
					_, _, e = app.updateLights(func(_, b bool) (bool, bool) { return state, b })
				} else {
					_, _, e = app.updateLights(func(h, _ bool) (bool, bool) { return h, state })
				}
			}
			if e != nil {
				errJSON(w, http.StatusInternalServerError, e.Error())
				return
			}
			writeJSON(w, 200, map[string]any{"ok": true, "on": state})
		}
	}
	mux.Handle("POST /light_head", lightToggle("head"))
	mux.Handle("POST /light_base", lightToggle("base"))

	mux.HandleFunc("POST /gimbal_relax", func(w http.ResponseWriter, r *http.Request) {
		if !app.requireSerial(w) {
			return
		}
		app.rover.gimbalTorque(false)
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	mux.HandleFunc("POST /gimbal_lock", func(w http.ResponseWriter, r *http.Request) {
		if !app.requireSerial(w) {
			return
		}
		app.rover.gimbalTorque(true)
		writeJSON(w, 200, map[string]any{"ok": true})
	})

	// speed cap
	mux.HandleFunc("POST /speed", func(w http.ResponseWriter, r *http.Request) {
		cap, err := floatParam(r, "cap", 0.25)
		if err != nil {
			errJSON(w, http.StatusBadRequest, err.Error())
			return
		}
		app.move.setCap(cap)
		writeJSON(w, 200, map[string]any{"ok": true, "cap": app.move.getCap()})
	})
	mux.HandleFunc("GET /speed", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"ok": true, "cap": app.move.getCap()})
	})

	// camera media
	mux.HandleFunc("GET /video_feed", app.videoFeed)
	mux.HandleFunc("POST /snapshot", func(w http.ResponseWriter, r *http.Request) {
		name, err := app.snapshot(time.Now())
		if err != nil {
			errJSON(w, http.StatusServiceUnavailable, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true, "name": name})
	})
	mux.HandleFunc("GET /latest", func(w http.ResponseWriter, r *http.Request) {
		photos := app.listPhotos()
		var latest *string
		if len(photos) > 0 {
			latest = &photos[0]
		}
		writeJSON(w, 200, map[string]any{"count": len(photos), "latest": latest})
	})
	mux.HandleFunc("GET /photos", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"photos": app.listPhotos()})
	})
	mux.HandleFunc("GET /photos/", func(w http.ResponseWriter, r *http.Request) {
		name, ok := photoName(r.URL.Path, "/photos/")
		if !ok {
			errJSON(w, http.StatusBadRequest, "bad photo name")
			return
		}
		http.ServeFile(w, r, filepath.Join(app.photoDir, name))
	})
	mux.HandleFunc("POST /delete_photo/", func(w http.ResponseWriter, r *http.Request) {
		name, ok := photoName(r.URL.Path, "/delete_photo/")
		if !ok {
			errJSON(w, http.StatusBadRequest, "bad photo name")
			return
		}
		os.Remove(filepath.Join(app.photoDir, name))
		os.Remove(filepath.Join(app.photoDir, name+".meta.json")) // outline sidecar, if any
		writeJSON(w, 200, map[string]any{"ok": true})
	})

	// photo metadata sidecars (plan 020): the autonomous find loop stores the
	// found object's bounding box so the gallery can draw a toggleable outline.
	// The name is safePhotoName-validated; the body is re-marshalled from
	// sanitized fields only, so arbitrary client JSON is never stored verbatim.
	mux.HandleFunc("POST /photo_meta/", func(w http.ResponseWriter, r *http.Request) {
		name, ok := photoName(r.URL.Path, "/photo_meta/")
		if !ok {
			errJSON(w, http.StatusBadRequest, "bad photo name")
			return
		}
		var in struct {
			Target     string    `json:"target"`
			Label      string    `json:"label"`
			Color      string    `json:"color"`
			BBox       []float64 `json:"bbox"`
			Confidence float64   `json:"confidence"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&in); err != nil {
			errJSON(w, http.StatusBadRequest, "bad meta body")
			return
		}
		if len(in.BBox) != 4 || in.BBox[0] >= in.BBox[2] || in.BBox[1] >= in.BBox[3] {
			errJSON(w, http.StatusBadRequest, "bbox must be [x1,y1,x2,y2] fractions")
			return
		}
		for _, v := range in.BBox {
			if math.IsNaN(v) || v < 0 || v > 1 {
				errJSON(w, http.StatusBadRequest, "bbox values must be 0..1")
				return
			}
		}
		trunc := func(s string, n int) string {
			if len(s) > n {
				return s[:n]
			}
			return s
		}
		out, _ := json.Marshal(map[string]any{
			"target": trunc(in.Target, 100), "label": trunc(in.Label, 32),
			"color": trunc(in.Color, 40),
			"bbox": in.BBox, "confidence": clamp(in.Confidence, 0, 1),
		})
		if err := os.WriteFile(filepath.Join(app.photoDir, name+".meta.json"), out, 0o644); err != nil {
			errJSON(w, http.StatusInternalServerError, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	mux.HandleFunc("GET /photo_meta/", func(w http.ResponseWriter, r *http.Request) {
		name, ok := photoName(r.URL.Path, "/photo_meta/")
		if !ok {
			errJSON(w, http.StatusBadRequest, "bad photo name")
			return
		}
		b, err := os.ReadFile(filepath.Join(app.photoDir, name+".meta.json"))
		if err != nil {
			errJSON(w, http.StatusNotFound, "no meta")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write(b)
	})

	// meta
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		camUp, camErr := app.cam.status()
		serUp, serErr := app.rover.status()
		writeJSON(w, 200, map[string]any{
			"ok":      true,
			"serial":  map[string]any{"up": serUp, "err": serErr},
			"camera":  map[string]any{"up": camUp, "err": camErr},
			"gamepad": map[string]any{"up": app.gamepadPresent(), "mapping": app.mapSource},
		})
	})
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		io.WriteString(w, htmlPage)
	})

	return withCORS(mux)
}

// withCORS adds permissive CORS (LAN control API) and answers preflights.
func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "*")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// safePhotoName accepts only a bare .jpg filename with a strict [A-Za-z0-9._-]
// charset: no separators, no "..", no dotfiles, and nothing that could break out
// of the gallery's HTML/JS interpolation (XSS) or escape the photo dir.
func safePhotoName(name string) bool {
	if name == "" || name != filepath.Base(name) || strings.HasPrefix(name, ".") ||
		!strings.HasSuffix(strings.ToLower(name), ".jpg") {
		return false
	}
	for _, c := range name {
		if !(c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' ||
			c == '.' || c == '_' || c == '-') {
			return false
		}
	}
	return true
}

func photoName(path, prefix string) (string, bool) {
	name := strings.TrimPrefix(path, prefix)
	return name, safePhotoName(name)
}

// Stream tuning.
//   streamWriteTimeout — max time a single MJPEG frame write may block before we
//     drop the slow/stuck client (it reconnects); also prevents a wedged client
//     from pinning the handler/keepalive forever.
//   streamSendBuffer — per-connection TCP send-buffer cap so a slow link can't
//     build a multi-frame backlog (latency); the kernel may round/double it, so
//     this only approximately bounds in-flight frames.
//   camStallTimeout — respawn a producer that has gone silent (see runOnce).
const (
	streamWriteTimeout = 5 * time.Second
	streamSendBuffer   = 256 << 10
	camStallTimeout    = 5 * time.Second
)

// connCtxKey stashes the raw net.Conn in each request's context (via
// http.Server.ConnContext) so videoFeed can tune the socket's send buffer.
type connCtxKey struct{}

// tuneStreamConn shrinks the MJPEG client's TCP send buffer so a slow link can't
// accumulate a multi-frame backlog. No-op if the conn wasn't stashed or isn't TCP.
func tuneStreamConn(ctx context.Context) {
	if c, ok := ctx.Value(connCtxKey{}).(net.Conn); ok {
		if tcp, ok := c.(*net.TCPConn); ok {
			_ = tcp.SetWriteBuffer(streamSendBuffer)
		}
	}
}

func (app *App) videoFeed(w http.ResponseWriter, r *http.Request) {
	const boundary = "rovercamframe"
	if _, ok := w.(http.Flusher); !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	rc := http.NewResponseController(w)
	tuneStreamConn(r.Context())
	frames, cancel := app.hub.subscribe()
	defer cancel()
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-store")

	// Keep the <img> alive even with no camera: resend latest/placeholder on a
	// timer so the connection never just hangs.
	keep := time.NewTicker(time.Second)
	defer keep.Stop()
	send := func(frame []byte) bool {
		// Camera down → placeholder, never the stale real frame the hub may still
		// hold from before a stall. Applies to every send path (incl. the frame
		// preloaded into a fresh subscription).
		if up, _ := app.cam.status(); !up {
			frame = placeholderFrame
		}
		// Bound how long one frame may block a slow/stuck client; on timeout (or
		// any write/flush error) return false → close → the <img> reconnects.
		// SetWriteDeadline is ErrNotSupported under httptest recorders; ignore it.
		_ = rc.SetWriteDeadline(time.Now().Add(streamWriteTimeout))
		if _, err := fmt.Fprintf(w, "--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n",
			boundary, len(frame)); err != nil {
			return false
		}
		if _, err := w.Write(frame); err != nil {
			return false
		}
		if _, err := fmt.Fprint(w, "\r\n"); err != nil {
			return false
		}
		return rc.Flush() == nil
	}
	for {
		select {
		case frame := <-frames:
			if !send(frame) {
				return
			}
		case <-keep.C:
			f := app.hub.latestFrame()
			if f == nil {
				f = placeholderFrame
			}
			if !send(f) {
				return
			}
		case <-r.Context().Done():
			return
		}
	}
}

// ───────────────────────────── web UI client ───────────────────────────────

const htmlPage = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rover controller</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}
 header{padding:10px 14px;background:#1c1c1c;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 h1{font-size:17px;margin:0}
 .live{display:block;max-width:640px;width:100%;margin:10px auto;border-radius:8px;background:#000}
 .pads{display:flex;gap:24px;justify-content:center;flex-wrap:wrap;padding:8px}
 .pad{display:grid;grid-template-columns:repeat(3,64px);grid-auto-rows:48px;gap:6px}
 button{background:#2d6cdf;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:14px;padding:8px}
 button:active{background:#1b50ad}
 button.warn{background:#a33}
 .pad .sp{visibility:hidden}
 .bar{display:flex;gap:10px;justify-content:center;align-items:center;flex-wrap:wrap;padding:8px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;padding:12px}
 figure{margin:0;background:#1c1c1c;border-radius:8px;overflow:hidden}
 figure a{position:relative;display:block}
 figure img{width:100%;display:block;aspect-ratio:4/3;object-fit:cover}
 .obox{position:absolute;border:1px solid rgba(90,255,90,.95);pointer-events:none}
 .obox span{position:absolute;right:-1px;top:100%;background:rgba(0,0,0,.6);color:#9f9;
   font-size:10px;line-height:1.4;padding:0 3px;white-space:nowrap;border-radius:0 0 3px 3px}
 .lb{position:fixed;inset:0;background:rgba(0,0,0,.86);display:flex;align-items:center;justify-content:center;z-index:10}
 .lbwrap{position:relative;display:inline-block}
 .lbwrap img{max-width:92vw;max-height:88vh;display:block}
 .lbbar{position:fixed;top:10px;right:14px;display:flex;gap:8px;z-index:11}
 figcaption{font-size:10px;padding:5px;display:flex;justify-content:space-between;gap:5px;word-break:break-all}
 small{color:#999}
 .help{max-width:640px;margin:6px auto;background:#1c1c1c;border-radius:8px;padding:10px 12px;font-size:13px}
 .help td{padding:2px 10px 2px 0;vertical-align:top}
 .help td:first-child{font-family:monospace;color:#9cf;white-space:nowrap;cursor:pointer;text-decoration:underline}
 .help td:first-child:hover{color:#fff}
 .prog{max-width:640px;margin:6px auto;padding:0 0 0 30px;color:#eee}
 .prog li{background:#1c1c1c;margin:3px 0;padding:5px 8px;border-radius:6px;display:flex;gap:6px;
   align-items:center;font-family:monospace;font-size:13px}
 .prog li.run{outline:2px solid #2d6cdf}
 .prog li span{flex:1;word-break:break-all}
 .prog li button{padding:2px 7px;font-size:12px}
</style></head><body>
<header><h1>🤖 Rover controller</h1>
 <button class="warn" onclick="cmd('estop')">⛔ E-STOP</button>
 <button onclick="snap()">📸 Snapshot</button>
 <span id="gp" style="font-size:12px;color:#999">🎮 none (press a button)</span>
 <span id="health"><small>…</small></span>
</header>
<img class="live" src="/video_feed" alt="live view">
<div class="pads">
 <div class="pad" aria-label="drive">
  <span class="sp"></span>
  <button onmousedown="hold(1,1)" onmouseup="release()" ontouchstart="hold(1,1)" ontouchend="release()">▲</button>
  <span class="sp"></span>
  <button onmousedown="hold(-1,1)" onmouseup="release()" ontouchstart="hold(-1,1)" ontouchend="release()">◀</button>
  <button onclick="cmd('stop')">■</button>
  <button onmousedown="hold(1,-1)" onmouseup="release()" ontouchstart="hold(1,-1)" ontouchend="release()">▶</button>
  <span class="sp"></span>
  <button onmousedown="hold(-1,-1)" onmouseup="release()" ontouchstart="hold(-1,-1)" ontouchend="release()">▼</button>
  <span class="sp"></span>
 </div>
 <div class="pad" aria-label="camera">
  <span class="sp"></span>
  <button onclick="cmd('camera_up')">cam ▲</button>
  <span class="sp"></span>
  <button onclick="cmd('camera_left')">cam ◀</button>
  <button onclick="cmd('camera_center')">⊙</button>
  <button onclick="cmd('camera_right')">cam ▶</button>
  <span class="sp"></span>
  <button onclick="cmd('camera_down')">cam ▼</button>
  <span class="sp"></span>
 </div>
</div>
<div class="bar">
 <button onclick="cmd('light_head')">head light</button>
 <button onclick="cmd('light_base')">base light</button>
 <button onclick="cmd('gimbal_relax')">relax gimbal</button>
 <button onclick="cmd('gimbal_lock')">lock gimbal</button>
 <label>speed cap <input id="cap" type="range" min="0" max="0.5" step="0.01" value="0.25"
   oninput="document.getElementById('capNum').value=this.value" onchange="setCap(this.value)">
  <input id="capNum" type="number" min="0" max="0.5" step="0.01" value="0.25"
   style="width:5em;padding:6px;border-radius:6px;border:0" onchange="setCap(this.value)"></label>
 <span id="capShow"><small>cap 0.25</small></span> <small>(0..0.5, not m/s)</small>
</div>
<form class="bar" style="margin:0" onsubmit="runCmd();return false">
 <input id="cmdin" type="text" autocomplete="off" spellcheck="false"
  placeholder="command — e.g. drive 0.2 0.2 · camera_aim 30 0 · light_head on · speed 0.15 · relax · stop"
  style="flex:1;min-width:200px;padding:8px;border-radius:6px;border:0">
 <button type="submit">Send</button>
 <button type="button" onclick="addStep()">＋ Add to program</button>
 <button type="button" onclick="toggleHelp()">❔ Commands</button>
 <small id="cmdout">Enter sends · ＋ adds it to the program below</small>
</form>
<div id="cmdhelp" class="help" style="display:none">
 <small>click a command to load it into the box, then edit the numbers:</small>
 <table>
  <tr><td onclick="pick('drive 0.2 0.2')">drive L R</td><td>drive, −1..1 (scaled by speed cap; ~0.5s pulse, then auto-stops)</td></tr>
  <tr><td onclick="pick('move_forward 400')">move_forward|back|left|right [MS]</td><td>nudge for MS ms (default 400)</td></tr>
  <tr><td onclick="pick('stop')">stop</td><td>stop the wheels</td></tr>
  <tr><td onclick="pick('estop')">estop</td><td>emergency stop (wheels + gimbal)</td></tr>
  <tr><td onclick="pick('camera_aim 0 0')">camera_aim PAN TILT</td><td>aim camera (pan −180..180, tilt −45..90, + is up)</td></tr>
  <tr><td onclick="pick('camera_up 15')">camera_up|down|left|right [DEG]</td><td>nudge camera (default 15°)</td></tr>
  <tr><td onclick="pick('camera_center')">camera_center</td><td>re-center the camera</td></tr>
  <tr><td onclick="pick('light_head on')">light_head|light_base [on|off]</td><td>no arg = toggle; or set on / off</td></tr>
  <tr><td onclick="pick('relax')">relax / lock</td><td>relax / lock the gimbal servos (hand-position the camera)</td></tr>
  <tr><td onclick="pick('speed 0.15')">speed CAP</td><td>set the speed cap, 0..0.5 (max wheel magnitude)</td></tr>
  <tr><td onclick="pick('snapshot')">snapshot</td><td>take a photo</td></tr>
 </table>
 <small>aliases: relax=gimbal_relax · lock=gimbal_lock · snap=snapshot · fwd=move_forward · back=move_back</small><br>
 <small>chatbot names also work: up/down/left/right = CAMERA nudge (wheels = spinl/spinr [S] or move_*) ·
 cam P T · center · photo · light F B (&gt;0 = on) · move L R = ONE ~0.5s pulse, not continuous ·
 note: spinl/spinr and move_* run at the speed cap here (the chatbot's spins are gentler)</small>
</div>
<div class="bar">
 <b>Program</b>
 <button onclick="runProgram()">▶ Run</button>
 <button class="warn" onclick="stopProgram()">■ Stop</button>
 <label>repeat <input id="reps" type="number" min="1" max="1000" value="1" style="width:4em;padding:6px;border-radius:6px;border:0"></label>
 <label>gap <input id="gap" type="number" min="0" max="10" step="0.1" value="0.6" style="width:4em;padding:6px;border-radius:6px;border:0">s</label>
 <button onclick="clearProg()">clear</button>
 <button onclick="saveProg()">💾 Save</button>
 <select id="saved" onchange="loadProg(this.value)" style="padding:6px;border-radius:6px;border:0"><option value="">load…</option></select>
 <span id="progstat"><small>empty — build a sequence with ＋ Add</small></span>
</div>
<ol id="program" class="prog"></ol>
<div style="max-width:640px;margin:0 auto;padding:0 12px"><small>press ■ Stop to end a running program — E-STOP halts motion but the loop keeps going</small></div>
<div class="bar"><button class="warn" onclick="clearAll()">🗑 Clear all photos</button></div>
<div class="grid" id="gallery"></div>
<script>
const cmd = (c,q='')=>fetch('/'+c+(q?'?'+q:''),{method:'POST'});
let driving=false;
function hold(l,r){driving=true;send(l,r);}
function send(l,r){if(driving)fetch('/drive?l='+l+'&r='+r,{method:'POST'}).then(()=>{if(driving)setTimeout(()=>send(l,r),200);});}
function release(){driving=false;cmd('stop');}
async function snap(){await cmd('snapshot');load();}
let seen='';
async function load(){
 const j=await(await fetch('/latest')).json();
 const key=j.count+':'+(j.latest||'');
 if(key===seen)return; seen=key;
 const p=await(await fetch('/photos')).json();
 document.getElementById('gallery').innerHTML=(p.photos||[]).map(n=>
  '<figure><a href="/photos/'+n+'" onclick="lightbox(\''+n+'\');return false"><img loading="lazy" src="/photos/'+n+'"></a>'+
  '<figcaption><span>'+n+'</span><button onclick="outline(this,\''+n+'\')" title="toggle found-object outline">◻</button>'+
  '<button class="warn" onclick="del(\''+n+'\')">del</button></figcaption></figure>').join('');
}
async function fetchMeta(n){
 try{const r=await fetch('/photo_meta/'+encodeURIComponent(n));if(r.ok)return await r.json();}catch(e){}
 return null;
}
// small label at the outline's bottom-right (e.g. "green pen"); textContent: no XSS
function boxLabel(d,m){
 const txt=(m&&(m.label||m.target))||'';
 if(!txt)return;
 const s=document.createElement('span');s.textContent=txt;d.appendChild(s);
}
// click a photo → zoomed lightbox with the outline + its toggle (plan 021 UX).
// The wrapper hugs the displayed image exactly, so bbox fractions map straight
// to % — no cover-crop math needed here. Esc / background click closes.
async function lightbox(n){
 const m=await fetchMeta(n);
 const lb=document.createElement('div');lb.className='lb';
 lb.onclick=e=>{if(e.target===lb)lb.remove();};
 const wrap=document.createElement('div');wrap.className='lbwrap';
 const img=document.createElement('img');img.src='/photos/'+encodeURIComponent(n);
 wrap.appendChild(img);
 const bar=document.createElement('div');bar.className='lbbar';
 const b=m&&m.bbox;
 if(b&&b.length===4){
  const d=document.createElement('div');d.className='obox';
  d.style.left=(b[0]*100)+'%';d.style.top=(b[1]*100)+'%';
  d.style.width=((b[2]-b[0])*100)+'%';d.style.height=((b[3]-b[1])*100)+'%';
  if(m.target||m.color)d.title=(m.target||'')+(m.color?' ('+m.color+')':'');
  boxLabel(d,m);
  wrap.appendChild(d);
  const t=document.createElement('button');t.textContent='◼ outline';
  t.onclick=()=>{const on=d.style.display!=='none';d.style.display=on?'none':'block';
   t.textContent=(on?'◻':'◼')+' outline';};
  bar.appendChild(t);
 }
 const x=document.createElement('button');x.textContent='✕ close';x.onclick=()=>lb.remove();
 bar.appendChild(x);
 lb.appendChild(wrap);lb.appendChild(bar);
 document.body.appendChild(lb);
 document.addEventListener('keydown',function esc(e){
  if(e.key==='Escape'){lb.remove();document.removeEventListener('keydown',esc);}});
}
// toggleable found-object outline (plan 020): lazy-fetch /photo_meta/{name} once,
// overlay a CSS box from the bbox fractions. The JPEG itself is untouched.
// coverPct maps image-fraction bbox -> container fractions accounting for the
// object-fit:cover crop, so the overlay aligns for ANY capture aspect (not just
// the default 4:3). Resize-safe: the container keeps its aspect, so % holds.
function coverPct(img,b){
 const cw=img.clientWidth,ch=img.clientHeight,iw=img.naturalWidth,ih=img.naturalHeight;
 if(!cw||!ch||!iw||!ih)return null;
 const s=Math.max(cw/iw,ch/ih),dw=iw*s,dh=ih*s,ox=(cw-dw)/2,oy=(ch-dh)/2;
 return [(ox+b[0]*dw)/cw,(oy+b[1]*dh)/ch,(b[2]-b[0])*dw/cw,(b[3]-b[1])*dh/ch];
}
async function outline(btn,n){
 const a=btn.closest('figure').querySelector('a');
 const old=a.querySelector('.obox');
 if(old){const on=(old.style.display==='none');old.style.display=on?'block':'none';
  btn.textContent=on?'◼':'◻';return;}
 let m=null;
 try{const r=await fetch('/photo_meta/'+encodeURIComponent(n));if(r.ok)m=await r.json();}catch(e){}
 const b=m&&m.bbox;
 if(!b||b.length!==4){btn.textContent='∅';btn.title='no outline data for this photo';return;}
 const img=a.querySelector('img');
 if(img&&!img.complete){try{await img.decode();}catch(e){}}
 const p=(img&&coverPct(img,b))||[b[0],b[1],b[2]-b[0],b[3]-b[1]];  // fallback: naive %
 const d=document.createElement('div');d.className='obox';
 d.style.left=(p[0]*100)+'%';d.style.top=(p[1]*100)+'%';
 d.style.width=(p[2]*100)+'%';d.style.height=(p[3]*100)+'%';
 if(m.target||m.color)d.title=(m.target||'')+(m.color?' ('+m.color+')':'');  // title property: no HTML
 boxLabel(d,m);
 a.appendChild(d);btn.textContent='◼';
}
async function del(n){await fetch('/delete_photo/'+encodeURIComponent(n),{method:'POST'});seen='';load();}
async function clearAll(){const p=await(await fetch('/photos')).json();const ns=p.photos||[];
 if(!ns.length||!confirm('Delete all '+ns.length+' photos?'))return;
 for(const n of ns){await fetch('/delete_photo/'+encodeURIComponent(n),{method:'POST'});}
 seen='';load();}

// ── speed cap: slider + number input + live value, synced from the server ────
function syncCap(v){v=Number(v);const c=document.getElementById('cap'),n=document.getElementById('capNum'),
 s=document.getElementById('capShow');if(c)c.value=v;if(n)n.value=v;if(s)s.innerHTML='<small>cap '+v.toFixed(2)+'</small>';}
function setCap(v){v=Number(v);if(!Number.isFinite(v))v=0;v=Math.max(0,Math.min(0.5,v));
 fetch('/speed?'+new URLSearchParams({cap:v}),{method:'POST'}).then(r=>r.json()).then(j=>syncCap(j.cap!==undefined?j.cap:v)).catch(()=>syncCap(v));}
async function initCap(){try{const j=await(await fetch('/speed')).json();if(j.cap!==undefined)syncCap(j.cap);}catch(e){}}

// ── direct command box: controller commands → existing HTTP endpoints ────────
// Pure client mapping; the server keeps all clamps/watchdog/estop. No raw serial.
const CMD_ALIAS={relax:'gimbal_relax',lock:'gimbal_lock',snap:'snapshot',fwd:'move_forward',back:'move_back',
 // chatbot-vocabulary parity (plan 019): bare up/down/left/right = CAMERA (as in the chatbot);
 // wheel turns are spinl/spinr or move_left/move_right. move = ONE ~0.5s pulse (not continuous).
 up:'camera_up',down:'camera_down',left:'camera_left',right:'camera_right',
 cam:'camera_aim',center:'camera_center',photo:'snapshot',move:'drive'};
const CMD_REQ={drive:['l','r'],camera_aim:['pan','tilt'],speed:['cap']};       // required numeric args
const CMD_OPT={move_forward:'ms',move_back:'ms',move_left:'ms',move_right:'ms', // one optional numeric arg
 camera_up:'deg',camera_down:'deg',camera_left:'deg',camera_right:'deg'};
const CMD_LIGHT=['light_head','light_base'];                                    // optional on|off
const CMD_NOARG=['stop','estop','camera_center','gimbal_relax','gimbal_lock','snapshot'];
function cout(m){document.getElementById('cmdout').textContent=m;}             // textContent: no XSS
function cnum(s){const v=Number(s);return Number.isFinite(v)?v:null;}          // rejects '10abc'/NaN/Inf (empty tokens are gated by the arity checks)
function toggleHelp(){const h=document.getElementById('cmdhelp');h.style.display=(h.style.display==='none')?'block':'none';}
// parseCmd: raw text → {cmd,path} or {error}. Shared by the box and the program.
function parseCmd(raw){
 const t=raw.trim().split(/\s+/);let c=(t[0]||'').toLowerCase();const a=t.slice(1);
 // chatbot-vocabulary special cases (units converted; plan 019)
 if(c==='spinl'||c==='spinr'){                       // chatbot: seconds → controller: ms
  if(a.length>1)return {error:c+' takes at most one number (seconds)'};
  let s=0.6;if(a.length===1){const v=cnum(a[0]);if(v===null)return {error:'not a number: '+a[0]};s=v;}
  const ms=Math.max(0,Math.min(5000,Math.round(s*1000)));
  return {cmd:c==='spinl'?'move_left':'move_right',
          path:'/'+(c==='spinl'?'move_left':'move_right')+'?'+new URLSearchParams({ms:ms})};
 }
 if(c==='light'){                                    // chatbot: light F B (PWM) → on/off pair (>0 = on)
  if(a.length!==2)return {error:'light needs 2 numbers: FRONT BASE (>0 = on)'};
  const f=cnum(a[0]),b=cnum(a[1]);if(f===null||b===null)return {error:'light args must be numbers'};
  return {cmd:'light',multi:['/light_head?on='+(f>0?1:0),'/light_base?on='+(b>0?1:0)]};
 }
 c=CMD_ALIAS[c]||c;
 let qs=null;
 if(CMD_REQ[c]){const k=CMD_REQ[c];
  if(a.length!==k.length)return {error:c+' needs '+k.length+' number(s): '+k.join(' ')};
  qs=new URLSearchParams();for(let i=0;i<k.length;i++){const v=cnum(a[i]);if(v===null)return {error:'not a number: '+a[i]};qs.set(k[i],v);}
 }else if(CMD_OPT[c]){
  if(a.length>1)return {error:c+' takes at most one number'};
  if(a.length===1){const v=cnum(a[0]);if(v===null)return {error:'not a number: '+a[0]};qs=new URLSearchParams();qs.set(CMD_OPT[c],v);}
 }else if(CMD_LIGHT.includes(c)){
  if(a.length>1)return {error:c+' takes on|off or nothing'};
  if(a.length===1){const s=a[0].toLowerCase();
   if(s==='on'||s==='1'||s==='true'){qs=new URLSearchParams();qs.set('on',1);}
   else if(s==='off'||s==='0'||s==='false'){qs=new URLSearchParams();qs.set('on',0);}
   else return {error:c+' arg must be on|off'};}
 }else if(CMD_NOARG.includes(c)){
  if(a.length)return {error:c+' takes no args'};
 }else return {error:'unknown command: '+t[0]};
 return {cmd:c,path:'/'+c+(qs?'?'+qs.toString():'')};
}
// sendCommand: run one command; returns a Promise<bool ok> (awaited by the program).
function sendCommand(raw,signal){
 const p=parseCmd(raw);
 if(p.error){cout('✗ '+p.error);return Promise.resolve(false);}
 cout('… '+raw);
 if(p.multi){                                        // e.g. 'light F B' = two channel sets
  return Promise.all(p.multi.map(u=>fetch(u,{method:'POST',signal}).then(r=>r.ok).catch(()=>false)))
   .then(oks=>{const ok=oks.every(Boolean);cout((ok?'✓ ':'✗ ')+raw);return ok;});
 }
 return fetch(p.path,{method:'POST',signal}).then(r=>r.json().then(j=>({ok:r.ok,j})).catch(()=>({ok:r.ok,j:{}}))).then(function(res){
  if(res.ok){let extra='';const j=res.j||{};
   if(j.cap!==undefined){extra=' (cap '+j.cap+')';syncCap(j.cap);}
   else if(j.pan!==undefined){extra=' (pan '+j.pan+' tilt '+j.tilt+')';}
   else if(j.on!==undefined){extra=' ('+(j.on?'on':'off')+')';}
   cout('✓ '+raw+extra);
   if(p.cmd==='snapshot'){seen='';load();}
   return true;
  }
  cout('✗ '+raw+' → '+((res.j&&res.j.error)||'HTTP error'));return false;
 }).catch(e=>{if(!e||e.name!=='AbortError')cout('✗ '+e);return false;});
}
function runCmd(){const el=document.getElementById('cmdin');const raw=el.value.trim();if(!raw)return;sendCommand(raw);el.value='';}
function pick(tpl){const el=document.getElementById('cmdin');el.value=tpl;el.focus();}

// ── program: a saved 'scratch' stack of commands (build, reorder, run, repeat) ─
let prog=[], running=false, runGen=0, runAbort=null;
const MIN_STEP_MS=60;   // floor so repeat×0-gap of instant steps can't hammer the server
function renderProg(){
 const ol=document.getElementById('program');
 ol.innerHTML=prog.map((s,i)=>'<li><span></span>'+
  '<button onclick="mv('+i+',-1)">↑</button><button onclick="mv('+i+',1)">↓</button>'+
  '<button class="warn" onclick="rm('+i+')">×</button></li>').join('');
 [...ol.children].forEach((li,i)=>{li.querySelector('span').textContent=(i+1)+'. '+prog[i];});   // textContent: no XSS
 if(!running)document.getElementById('progstat').innerHTML='<small>'+(prog.length?prog.length+' step(s)':'empty — build a sequence with ＋ Add')+'</small>';
}
function addStep(){const el=document.getElementById('cmdin');const raw=el.value.trim();if(!raw)return;
 const p=parseCmd(raw);if(p.error){cout('✗ '+p.error);return;}
 prog.push(raw);renderProg();el.value='';cout('added: '+raw);}
function rm(i){prog.splice(i,1);renderProg();}
function mv(i,d){const j=i+d;if(j<0||j>=prog.length)return;[prog[i],prog[j]]=[prog[j],prog[i]];renderProg();}
function clearProg(){if(prog.length&&confirm('Clear the program?')){prog=[];renderProg();}}
function sleepMs(ms){return new Promise(r=>setTimeout(r,ms));}
// motionMs: how long a step keeps the wheels moving, so we wait it out before the
// next step (non-overlap). Must mirror the server: /drive auto-stops after
// watchdogTTL (500ms in rovercontrol.go — keep in sync); /move_* self-stops after
// its ms arg (nudge default 400, clamped 0..5000). Camera/light/etc = 0.
function motionMs(raw){const p=parseCmd(raw);if(p.error||!p.cmd||!p.path)return 0;const c=p.cmd;
 if(c==='drive')return 500;
 if(c.indexOf('move_')===0){
  // read ms from the PARSED path (spinl converts seconds→ms there; re-reading the
  // raw token would treat 'spinl 2' as 2ms and under-wait the sequencer).
  // Missing ms must fall back to the server default 400 — note Number(null)===0,
  // so test for null explicitly or a bare move_forward would under-wait.
  const q=new URLSearchParams(p.path.split('?')[1]||'').get('ms');
  const m=(q===null)?NaN:Number(q);
  return Number.isFinite(m)?Math.max(0,Math.min(5000,m)):400;}
 return 0;}
async function runProgram(){
 if(running||!prog.length)return;                          // ignore while running: Stop, then Run to restart
 const my=++runGen; running=true;
 const ac=(typeof AbortController!=='undefined')?new AbortController():null; runAbort=ac;
 const steps=prog.slice();                                 // snapshot: mid-run edits can't corrupt the loop
 const reps=Math.max(1,Math.min(1000,parseInt(document.getElementById('reps').value)||1));
 const gap=Math.max(0,Math.min(10,parseFloat(document.getElementById('gap').value)||0))*1000;
 const ol=document.getElementById('program');
 const ps=document.getElementById('progstat');
 const clearHi=()=>[...ol.children].forEach(li=>li.classList.remove('run'));
 try{
  for(let r=0;r<reps;r++){
   for(let i=0;i<steps.length;i++){
    if(my!==runGen)return;                                 // superseded / stopped
    clearHi();if(ol.children[i])ol.children[i].classList.add('run');
    ps.innerHTML='<small>rep '+(r+1)+'/'+reps+' · step '+(i+1)+'/'+steps.length+'</small>';
    const ok=await sendCommand(steps[i],ac&&ac.signal);
    if(my!==runGen)return;
    if(!ok){ps.innerHTML='<small>stopped: step '+(i+1)+' failed</small>';return;}   // a failed step aborts
    await sleepMs(Math.max(gap,motionMs(steps[i]),MIN_STEP_MS));
   }
  }
  ps.innerHTML='<small>done</small>';
 }finally{
  // ownership-guarded: only THIS run (if still current) clears state + stops the
  // wheels — a superseded/stopped old run must not stomp a newer run or re-/stop.
  if(my===runGen){running=false;clearHi();fetch('/stop',{method:'POST'});}
 }
}
function stopProgram(){runGen++;running=false;
 if(runAbort)try{runAbort.abort();}catch(e){}                 // cancel any in-flight step request
 [...document.getElementById('program').children].forEach(li=>li.classList.remove('run'));
 fetch('/stop',{method:'POST'});cout('program stopped');      // + server drive-watchdog stops any leaked pulse (≤500ms)
 document.getElementById('progstat').innerHTML='<small>stopped</small>';}
// named programs saved in the browser (localStorage)
function refreshSaved(){const sel=document.getElementById('saved');
 const names=Object.keys(localStorage).filter(k=>k.indexOf('roverprog:')===0).map(k=>k.slice(10)).sort();
 sel.innerHTML='<option value="">load…</option>';
 names.forEach(n=>{const o=document.createElement('option');o.textContent=n;sel.appendChild(o);});}
function saveProg(){if(!prog.length)return;const n=(prompt('Save program as:')||'').trim();if(!n)return;
 localStorage.setItem('roverprog:'+n,JSON.stringify(prog));refreshSaved();cout('saved "'+n+'"');}
function loadProg(n){if(!n)return;try{const p=JSON.parse(localStorage.getItem('roverprog:'+n));
 if(Array.isArray(p)){prog=p.filter(x=>typeof x==='string');renderProg();cout('loaded "'+n+'"');}}catch(e){}}
async function health(){try{const h=await(await fetch('/healthz')).json();
 document.getElementById('health').innerHTML='<small>serial '+(h.serial.up?'✓':'✗')+
 ' · cam '+(h.camera.up?'✓':'✗')+' · pad '+(h.gamepad.up?'✓':'–')+'</small>';}catch(e){}}
setInterval(()=>{load();health();},2000);load();health();initCap();renderProg();refreshSaved();

// ── Mac-side gamepad: Gamepad API → existing HTTP endpoints (no server change).
// Drive is in-flight-guarded and refreshed continuously while deflected (feeds
// the 500ms server watchdog); centered → /stop once. Camera integrates the
// right stick into an absolute angle for /camera_aim. getGamepads() is re-read
// every tick (never cached).
let padIndex=null,padPrev=[],driveBusy=false,aimBusy=false,wasMoving=false;
let panAngle=0,tiltAngle=0,lastDrive=0,lastAim=0;
const DZ=0.15,PANR=90,TILTR=70,SENDMS=120;
const gpEl=document.getElementById('gp');
function gpStop(){fetch('/stop',{method:'POST'});}
addEventListener('gamepadconnected',e=>{padIndex=e.gamepad.index;padPrev=[];
 if(gpEl)gpEl.textContent='🎮 '+e.gamepad.id.slice(0,20);});
addEventListener('gamepaddisconnected',e=>{if(e.gamepad.index===padIndex){
 padIndex=null;gpStop();if(gpEl)gpEl.textContent='🎮 none';}});
document.addEventListener('visibilitychange',()=>{if(document.hidden){wasMoving=false;gpStop();}});
addEventListener('pagehide',()=>{try{fetch('/stop',{method:'POST',keepalive:true});}catch(e){}});
function dzf(v){return Math.abs(v)<DZ?0:v;}
function gpEdge(b,i){const n=!!(b&&b.pressed);const f=n&&!padPrev[i];padPrev[i]=n;return f;}
function gpPoll(){
 if(padIndex===null||document.hidden)return; // never drive while backgrounded
 const gp=navigator.getGamepads&&navigator.getGamepads()[padIndex];
 if(!gp)return;
 const ax=gp.axes,bt=gp.buttons,now=Date.now();
 if(gpEdge(bt[0],0))cmd('stop');
 if(gpEdge(bt[1],1))snap();
 if(gpEdge(bt[2],2))cmd('light_head');
 if(gpEdge(bt[3],3)){panAngle=0;tiltAngle=0;cmd('camera_center');}
 if(gpEdge(bt[4],4))cmd('light_base');
 if(gpEdge(bt[8],8))cmd('estop');
 const thr=-dzf(ax[1]||0),str=dzf(ax[0]||0);
 const l=Math.max(-1,Math.min(1,thr+str)),r=Math.max(-1,Math.min(1,thr-str));
 if(l!==0||r!==0){wasMoving=true;
  if(!driveBusy&&now-lastDrive>=SENDMS){driveBusy=true;lastDrive=now;
   fetch('/drive?l='+l.toFixed(3)+'&r='+r.toFixed(3),{method:'POST'}).finally(()=>{driveBusy=false;});}
 }else if(wasMoving){wasMoving=false;gpStop();}
 const dp=dzf(ax[2]||0)*PANR/20,dt=-dzf(ax[3]||0)*TILTR/20;
 if(dp!==0||dt!==0){
  panAngle=Math.max(-180,Math.min(180,panAngle+dp));
  tiltAngle=Math.max(-45,Math.min(90,tiltAngle+dt));
  if(!aimBusy&&now-lastAim>=SENDMS){aimBusy=true;lastAim=now;
   fetch('/camera_aim?pan='+panAngle.toFixed(1)+'&tilt='+tiltAngle.toFixed(1),{method:'POST'}).finally(()=>{aimBusy=false;});}
 }
}
setInterval(gpPoll,50);
</script>
</body></html>`

// ─────────────────────────────── main ──────────────────────────────────────

func defaultPhotoDir() string {
	exe, err := os.Executable()
	if err != nil {
		return "photos"
	}
	if r, err := filepath.EvalSymlinks(exe); err == nil {
		exe = r
	}
	return filepath.Join(filepath.Dir(exe), "photos")
}

func main() {
	port := flag.String("port", "8080", "HTTP listen port")
	photos := flag.String("photos", defaultPhotoDir(), "photo directory")
	serialPath := flag.String("serial", "/dev/ttyAMA0", "ESP32 serial device")
	jsPath := flag.String("gamepad", "/dev/input/js0", "joystick device ('' to disable)")
	width := flag.Int("width", defaultCamWidth, "camera width (0 = let the camera choose)")
	height := flag.Int("height", defaultCamHeight, "camera height (0 = let the camera choose)")
	fps := flag.Int("fps", defaultCamFPS, "camera fps (v4l2 via --set-parm, rpicam --framerate; 0 = camera default)")
	camMode := flag.String("camera-mode", "auto", "camera backend: auto|v4l2|rpicam|off")
	camDevice := flag.String("camera-device", "/dev/video0", "V4L2 device (v4l2 mode)")
	gpDebug := flag.Bool("gamepad-debug", false, "print live gamepad indices and exit")
	gpMap := flag.String("gamepad-map", defaultMapPath(), "gamepad mapping JSON (default if absent)")
	calibrate := flag.Bool("calibrate", false, "guided gamepad calibration, then exit")
	flag.Parse()

	if *gpDebug {
		runGamepadDebug(*jsPath)
		return
	}
	if *calibrate {
		if err := runCalibrate(*jsPath, *gpMap); err != nil {
			log.Fatalf("calibrate: %v", err)
		}
		return
	}

	mode := resolveCameraMode(*camMode, *camDevice)
	log.Printf("camera: backend %s (device %s)", mode, *camDevice)
	rover := &Rover{}
	app := &App{
		rover: rover,
		hub:   newHub(),
		cam: &Camera{mode: mode, device: *camDevice,
			width: *width, height: *height, fps: *fps},
		photoDir: *photos,
	}
	app.move = newMovement(rover)
	app.aim = &CameraAim{r: rover}

	// gamepad mapping: missing → default; malformed/invalid → disable the pad
	// (don't drive with wrong controls), surfaced in /healthz.
	mp, src, mErr := loadMapping(*gpMap)
	app.mapSource = src
	if mErr != nil {
		log.Printf("gamepad: mapping %s invalid (%v) — joystick DISABLED; fix the file", *gpMap, mErr)
	} else {
		app.mapping = &mp
		log.Printf("gamepad: mapping %s", src)
	}

	// Cancel on SIGINT/SIGTERM: this stops the camera child, watchdog, gamepad,
	// and serial-retry goroutines, and triggers the graceful shutdown below.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// serial: try now, retry in the background so a busy port doesn't kill us.
	// -serial '' skips it entirely (control endpoints 503 via requireSerial).
	if *serialPath != "" {
		go openSerialWithRetry(ctx, rover, *serialPath)
	} else {
		rover.setStatus(nil, "disabled")
		log.Printf("serial: disabled (-serial '')")
	}

	// camera (-camera-mode off skips it; videoFeed then serves the placeholder)
	if mode != "off" {
		go app.cam.run(ctx, app.hub)
	} else {
		app.cam.setStatus(false, "disabled")
		log.Printf("camera: disabled (-camera-mode off)")
	}

	// joystick (optional; only if the mapping loaded)
	if *jsPath != "" && app.mapping != nil {
		if f, err := os.Open(*jsPath); err == nil {
			log.Printf("gamepad: reading %s", *jsPath)
			go app.runGamepad(ctx, f)
		} else {
			log.Printf("gamepad: %s unavailable (%v); HTTP-only control", *jsPath, err)
		}
	}

	go app.move.runWatchdog(ctx)

	srv := &http.Server{
		Addr:    ":" + *port,
		Handler: app.routes(),
		// Stash the raw conn so videoFeed can shrink the stream socket's send
		// buffer (latency bound).
		ConnContext: func(ctx context.Context, c net.Conn) context.Context {
			return context.WithValue(ctx, connCtxKey{}, c)
		},
	}
	// Graceful shutdown: on signal, stop the wheels (a stop, not a move — safe)
	// while the link is still open, close the serial port, then drain the server.
	go func() {
		<-ctx.Done()
		log.Printf("rovercontrol: signal received, shutting down")
		// doEstop (not stop): latch e-stop so a /drive racing in the window before
		// closeLink is refused — the watchdog goroutine is already cancelled here.
		app.move.doEstop()
		rover.closeLink()
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
	}()

	log.Printf("rovercontrol: http://0.0.0.0:%s  (photos: %s)", *port, *photos)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
	log.Printf("rovercontrol: stopped")
}

func openSerialWithRetry(ctx context.Context, rover *Rover, path string) {
	for ctx.Err() == nil {
		link, err := openTTY(path, 115200)
		if err != nil {
			rover.setStatus(nil, err.Error())
			log.Printf("serial: %s unavailable (%v); retrying", path, err)
		} else if err := initLink(link); err != nil {
			// init failed — close so we don't leak the port, and never expose
			// a half-initialised link (gimbal module may not be selected).
			link.Close()
			rover.setStatus(nil, "init failed: "+err.Error())
			log.Printf("serial: init failed on %s: %v; retrying", path, err)
		} else if ctx.Err() != nil {
			// shutting down — don't publish a link closeLink() will never see
			link.Close()
			return
		} else {
			rover.setStatus(link, "")
			log.Printf("serial: connected on %s @115200", path)
			return
		}
		select {
		case <-time.After(3 * time.Second):
		case <-ctx.Done():
			return
		}
	}
}

// runGamepad reads the gamepad and drives the rover, and—critically—stops the
// wheels and marks the pad down if the device read fails (unplug / wireless
// dropout). Without this, a disconnect with a deflected stick would keep
// re-commanding the last motion every tick and the watchdog (which that motion
// refreshes) could never stop it.
func (app *App) runGamepad(parent context.Context, dev *os.File) {
	defer dev.Close()
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	g := newGamepad()
	app.setGamepad(true)
	go func() { g.reader(ctx, dev); cancel() }() // reader exit (EOF/unplug) cancels
	app.joystickLoop(ctx, g)                     // returns when ctx is cancelled
	app.setGamepad(false)
	app.move.stop() // gamepad gone → ensure the rover is stopped
}

func runGamepadDebug(path string) {
	f, err := os.Open(path)
	if err != nil {
		log.Fatalf("gamepad-debug: %v", err)
	}
	defer f.Close()
	log.Printf("gamepad-debug: move sticks / press buttons (Ctrl-C to quit)")
	buf := make([]byte, 8)
	for {
		if _, err := io.ReadFull(f, buf); err != nil {
			return
		}
		if e, ok := parseJSEvent(buf); ok && !e.isInit {
			kind := "axis"
			if e.etype == jsEventButton {
				kind = "button"
			}
			fmt.Printf("%s #%d = %d\n", kind, e.number, e.value)
		}
	}
}

func defaultMapPath() string {
	exe, err := os.Executable()
	if err != nil {
		return "gamepad.json"
	}
	if r, err := filepath.EvalSymlinks(exe); err == nil {
		exe = r
	}
	return filepath.Join(filepath.Dir(exe), "gamepad.json")
}

const calibThreshold = 0.7 // axis must cross this (of full scale) to count

// runCalibrate is a guided terminal wizard: for each control it waits for the
// pad to return to neutral, then captures the moved axis (index + sign) or the
// pressed button. JS_EVENT_INIT events are ignored; a step times out to "skip"
// (keeps the default). The result is written atomically to mapPath.
func runCalibrate(jsPath, mapPath string) error {
	f, err := os.Open(jsPath)
	if err != nil {
		return err
	}
	defer f.Close()
	events := make(chan jsEvent, 128)
	go func() {
		buf := make([]byte, 8)
		for {
			if _, err := io.ReadFull(f, buf); err != nil {
				close(events)
				return
			}
			if e, ok := parseJSEvent(buf); ok && !e.isInit { // mask init burst
				events <- e
			}
		}
	}()

	fmt.Println("Gamepad calibration — follow each prompt. A step auto-skips")
	fmt.Print("(keeps the default) after ~12s if you don't move that control.\n\n")
	m := defaultMapping()

	// axes: prompt asks for a direction; Invert makes that direction read +1.
	if a, ok := captureAxis(events, "Push LEFT stick UP (forward)"); ok {
		m.Throttle = a
	}
	if a, ok := captureAxis(events, "Push LEFT stick RIGHT"); ok {
		m.Steer = a
	}
	if a, ok := captureAxis(events, "Push RIGHT stick RIGHT (camera pan right)"); ok {
		m.Pan = a
	}
	if a, ok := captureAxis(events, "Push RIGHT stick UP (camera tilt up)"); ok {
		m.Tilt = a
	}
	// per-button controls — a button OR a held trigger; SKIP disables it (plan
	// 007), so a pad missing a button (e.g. no L3/R3) just leaves it off.
	for _, step := range []struct {
		prompt string
		dst    *ControlMap
	}{
		{"Press/hold TURBO (higher top speed)", &m.Turbo},
		{"Press STOP wheels", &m.Stop},
		{"Press EMERGENCY STOP", &m.Estop},
		{"Press HEAD light toggle", &m.HeadLight},
		{"Press BASE light toggle", &m.BaseLight},
		{"Press CENTER camera", &m.Center},
		{"Press SNAPSHOT", &m.Snapshot},
		{"Press RELAX gimbal (e.g. L2)", &m.Relax},
		{"Press LOCK gimbal (e.g. R2)", &m.Lock},
	} {
		if c, ok := captureControl(events, step.prompt); ok {
			*step.dst = c
		} else {
			*step.dst = ControlMap{Kind: "none"} // skip → disabled
		}
	}
	m.Hat = captureHat(events, "Press D-pad UP", "speed cap")
	// Optional extras (plan 006): a button OR a held trigger; skip → disabled.
	for _, step := range []struct {
		prompt string
		dst    *ControlMap
	}{
		{"Hold the PRECISION (slow) trigger/button", &m.Precision},
		{"Hold the BOOST (fast) trigger/button", &m.Boost},
		{"Press your INSTANT-STOP (panic) button", &m.PanicStop},
	} {
		if c, ok := captureControl(events, step.prompt); ok {
			*step.dst = c
		}
	}
	m.HatX = captureHat(events, "Press D-pad RIGHT", "camera pan")

	if m.Estop.Kind == "none" && m.PanicStop.Kind == "none" {
		fmt.Println("  ⚠️  WARNING: you bound NO e-stop button — there will be no panic stop!")
	}
	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	tmp := mapPath + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp, mapPath); err != nil {
		os.Remove(tmp)
		return err
	}
	fmt.Printf("\nWrote mapping to %s — restart rovercontrol to use it.\n", mapPath)
	return nil
}

// waitNeutral drains events until the pad is quiet (no event for 250ms), i.e.
// sticks centered and buttons released, so the next capture sees a fresh move.
func waitNeutral(events <-chan jsEvent) {
	for {
		select {
		case _, ok := <-events:
			if !ok {
				return
			}
		case <-time.After(250 * time.Millisecond):
			return
		}
	}
}

func captureAxis(events <-chan jsEvent, prompt string) (AxisMap, bool) {
	waitNeutral(events) // drain BEFORE prompting so a fast move isn't swallowed
	fmt.Printf("  %s ... ", prompt)
	deadline := time.After(12 * time.Second)
	for {
		select {
		case e, ok := <-events:
			if !ok {
				fmt.Println("(device closed)")
				return AxisMap{}, false
			}
			if e.etype == jsEventAxis {
				v := float64(e.value) / 32767.0
				if v > calibThreshold || v < -calibThreshold {
					// Invert when the pushed direction reads negative, so the
					// requested direction maps to +1.
					inv := v < 0
					fmt.Printf("axis %d%s\n", e.number, map[bool]string{true: " (inverted)"}[inv])
					return AxisMap{Index: int(e.number), Invert: inv}, true
				}
			}
		case <-deadline:
			fmt.Println("(skipped, kept default)")
			return AxisMap{}, false
		}
	}
}

func captureButton(events <-chan jsEvent, prompt string) (int, bool) {
	waitNeutral(events) // drain BEFORE prompting so a fast press isn't swallowed
	if prompt != "" {
		fmt.Printf("  %s ... ", prompt)
	}
	deadline := time.After(12 * time.Second)
	for {
		select {
		case e, ok := <-events:
			if !ok {
				fmt.Println("(device closed)")
				return 0, false
			}
			if e.etype == jsEventButton && e.value != 0 {
				fmt.Printf("button %d\n", e.number)
				return int(e.number), true
			}
		case <-deadline:
			fmt.Println("(skipped, kept default)")
			return 0, false
		}
	}
}

// captureHat detects the D-pad shape: an axis move → Kind "axis"; a button →
// Kind "buttons" (then asks for the opposite direction); a timeout → "none".
// posPrompt is the positive-direction prompt (e.g. "Press D-pad UP"); label
// names what it controls (for the skip message).
func captureHat(events <-chan jsEvent, posPrompt, label string) HatMap {
	waitNeutral(events) // drain BEFORE prompting so a fast press isn't swallowed
	fmt.Printf("  %s (or wait to skip the %s D-pad) ... ", posPrompt, label)
	deadline := time.After(10 * time.Second)
	for {
		select {
		case e, ok := <-events:
			if !ok {
				return HatMap{Kind: "none"}
			}
			if e.etype == jsEventAxis {
				v := float64(e.value) / 32767.0
				if v > calibThreshold || v < -calibThreshold {
					fmt.Printf("axis %d\n", e.number)
					return HatMap{Kind: "axis", Axis: AxisMap{int(e.number), v < 0}}
				}
			}
			if e.etype == jsEventButton && e.value != 0 {
				pos := int(e.number)
				fmt.Printf("button %d\n", pos)
				if neg, ok := captureButton(events, "Now press the OPPOSITE D-pad direction"); ok {
					return HatMap{Kind: "buttons", Up: pos, Down: neg}
				}
				return HatMap{Kind: "none"}
			}
		case <-deadline:
			fmt.Println("(skipped)")
			return HatMap{Kind: "none"}
		}
	}
}

// captureControl captures an optional control that may be a button OR a held
// trigger (axis): whichever the user activates past threshold. Skip → false.
func captureControl(events <-chan jsEvent, prompt string) (ControlMap, bool) {
	waitNeutral(events)
	fmt.Printf("  %s (or wait to skip) ... ", prompt)
	deadline := time.After(12 * time.Second)
	for {
		select {
		case e, ok := <-events:
			if !ok {
				fmt.Println("(device closed)")
				return ControlMap{}, false
			}
			if e.etype == jsEventAxis {
				v := float64(e.value) / 32767.0
				if v > calibThreshold || v < -calibThreshold {
					fmt.Printf("axis %d%s\n", e.number, map[bool]string{true: " (inverted)"}[v < 0])
					return ControlMap{Kind: "axis", Axis: AxisMap{int(e.number), v < 0}}, true
				}
			}
			if e.etype == jsEventButton && e.value != 0 {
				fmt.Printf("button %d\n", e.number)
				return ControlMap{Kind: "button", Index: int(e.number)}, true
			}
		case <-deadline:
			fmt.Println("(skipped, stays disabled)")
			return ControlMap{}, false
		}
	}
}
