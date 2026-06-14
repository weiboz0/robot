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
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
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

	mu      sync.Mutex
	up      bool
	lastErr string

	// attemptFn overrides runOnce in tests (nil = use runOnce).
	attemptFn func(ctx context.Context, hub *Hub, w, h int) error
}

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
	case "v4l2", "rpicam":
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

// buildCameraCmd returns the capture command that writes concatenated MJPG to
// stdout. For v4l2, width/height are only set when both > 0 (so width=0 lets the
// camera pick a supported resolution — the unsized-retry escape hatch).
func buildCameraCmd(ctx context.Context, mode, device string, w, h, fps int) *exec.Cmd {
	if mode == "v4l2" {
		fmtArg := "--set-fmt-video=pixelformat=MJPG"
		if w > 0 && h > 0 {
			fmtArg = fmt.Sprintf("--set-fmt-video=width=%d,height=%d,pixelformat=MJPG", w, h)
		}
		return exec.CommandContext(ctx, "v4l2-ctl", "-d", device,
			fmtArg, "--stream-mmap", "--stream-count=0", "--stream-to=-")
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
		if err != nil && c.mode == "v4l2" && c.width > 0 && c.height > 0 && !unsizedExhausted {
			err2 := attempt(ctx, hub, 0, 0)
			if ctx.Err() != nil {
				return
			}
			if err2 == nil {
				err = nil
			} else {
				unsizedExhausted = true
				err = fmt.Errorf("sized: %v; unsized: %v", err, err2)
			}
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
	cmd := buildCameraCmd(ctx, c.mode, c.device, w, h, c.fps)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	var stderr tailBuffer // keep the last bytes of stderr for diagnosis
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return err
	}
	c.setStatus(true, "")
	if c.mode == "v4l2" {
		log.Printf("camera: v4l2 started (%dx%d, %s)", w, h, c.device)
	} else {
		log.Printf("camera: rpicam started (%dx%d@%dfps)", w, h, c.fps)
	}
	splitErr := splitFrames(stdout, hub.publish)
	waitErr := cmd.Wait()
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
	p, t, _ := a.r.aimCamera(pan, tilt)
	a.mu.Lock()
	a.pan, a.tilt = p, t
	a.mu.Unlock()
	return p, t
}

func (a *CameraAim) nudge(dPan, dTilt float64) (float64, float64) {
	a.mu.Lock()
	p, t := a.pan+dPan, a.tilt+dTilt
	a.mu.Unlock()
	return a.set(p, t)
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

func (app *App) setLights(head, base bool) error {
	app.lightMu.Lock()
	app.headOn, app.baseOn = head, base
	app.lightMu.Unlock()
	hv, bv := 0, 0
	if head {
		hv = 255
	}
	if base {
		bv = 255
	}
	return app.rover.lights(hv, bv)
}

func (app *App) toggleHead() (bool, error) {
	app.lightMu.Lock()
	h, b := !app.headOn, app.baseOn
	app.lightMu.Unlock()
	return h, app.setLights(h, b)
}

func (app *App) toggleBase() (bool, error) {
	app.lightMu.Lock()
	h, b := app.headOn, !app.baseOn
	app.lightMu.Unlock()
	return b, app.setLights(h, b)
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

// GamepadMapping carries only what identifies a pad's controls (indices + axis
// signs). Tuning (deadzone/rates/speedSteps) stays in code constants.
type GamepadMapping struct {
	Throttle  AxisMap `json:"throttle"` // left stick Y (up = forward)
	Steer     AxisMap `json:"steer"`    // left stick X (right = right)
	Pan       AxisMap `json:"pan"`      // right stick X (right = pan right)
	Tilt      AxisMap `json:"tilt"`     // right stick Y (up = tilt up)
	Turbo     int     `json:"turbo"`    // hold for higher top speed (RB)
	Stop      int     `json:"stop"`
	Estop     int     `json:"estop"`
	HeadLight int     `json:"head_light"`
	BaseLight int     `json:"base_light"`
	Center    int     `json:"center"`
	Snapshot  int     `json:"snapshot"`
	Relax     int     `json:"relax"`
	Lock      int     `json:"lock"`
	Hat       HatMap  `json:"hat"` // D-pad → speed cap
}

// defaultMapping reproduces EXACTLY the historical hard-coded constants (and the
// signs the old loop applied), so with no config file behavior is unchanged.
func defaultMapping() GamepadMapping {
	return GamepadMapping{
		Throttle:  AxisMap{1, true},  // throttle = -axis(LY): up = forward
		Steer:     AxisMap{0, false}, // axis(LX)
		Pan:       AxisMap{3, false}, // axis(RX)
		Tilt:      AxisMap{4, true},  // dTilt = -axis(RY): up = tilt up
		Turbo:     5,                 // RB
		Stop:      0,                 // A
		Snapshot:  1,                 // B
		HeadLight: 2,                 // X
		Center:    3,                 // Y
		BaseLight: 4,                 // LB
		Estop:     6,                 // Back
		Relax:     9,                 // L3
		Lock:      10,                // R3
		// D-pad vertical on axis 7; up (raw negative) => +1 via Invert.
		Hat: HatMap{Kind: "axis", Axis: AxisMap{7, true}},
	}
}

func (m GamepadMapping) validate() error {
	idx := []int{m.Throttle.Index, m.Steer.Index, m.Pan.Index, m.Tilt.Index,
		m.Turbo, m.Stop, m.Estop, m.HeadLight, m.BaseLight, m.Center,
		m.Snapshot, m.Relax, m.Lock}
	for _, i := range idx {
		if i < 0 {
			return fmt.Errorf("negative control index %d", i)
		}
	}
	switch m.Hat.Kind {
	case "axis":
		if m.Hat.Axis.Index < 0 {
			return fmt.Errorf("negative hat axis %d", m.Hat.Axis.Index)
		}
	case "buttons":
		if m.Hat.Up < 0 || m.Hat.Down < 0 {
			return fmt.Errorf("negative hat button index")
		}
	case "none":
	default:
		return fmt.Errorf("invalid hat kind %q", m.Hat.Kind)
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

// gpPrev carries edge-detection state across ticks.
type gpPrev struct {
	btn map[int]bool
	hat int
}

// gpActions is the decision for one tick: which buttons fired (rising edge),
// the speed-cap step, the deadzoned signed stick values, and turbo.
type gpActions struct {
	stop, estop, head, base, snap, center, relax, lock bool
	hatDelta                                           int
	throttle, steer, pan, tilt                         float64
	turbo                                              bool
}

// computeJoystick reads the mapping against a state snapshot and updates prev.
func computeJoystick(m *GamepadMapping, st gpState, prev *gpPrev) gpActions {
	edge := func(idx int) bool {
		now := st.button(idx)
		fired := now && !prev.btn[idx]
		prev.btn[idx] = now
		return fired
	}
	var a gpActions
	a.stop = edge(m.Stop)
	a.estop = edge(m.Estop)
	a.head = edge(m.HeadLight)
	a.base = edge(m.BaseLight)
	a.snap = edge(m.Snapshot)
	a.center = edge(m.Center)
	a.relax = edge(m.Relax)
	a.lock = edge(m.Lock)
	a.turbo = st.button(m.Turbo)
	hd := hatDirection(m.Hat, st) // speed cap on rising edge only
	if hd != 0 && prev.hat == 0 {
		a.hatDelta = hd
	}
	prev.hat = hd
	a.throttle = dz(axisSigned(st, m.Throttle))
	a.steer = dz(axisSigned(st, m.Steer))
	a.pan = dz(axisSigned(st, m.Pan))
	a.tilt = dz(axisSigned(st, m.Tilt))
	return a
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

// joystickLoop applies the deadzone + slew ramp and commands motion every tick
// (a held-steady stick emits no events, so motion must be re-commanded on a
// fixed dt — and that refresh also keeps the watchdog from auto-stopping a
// gamepad driver). debugFn, if set, is called with the live state instead.
func (app *App) joystickLoop(ctx context.Context, g *gamepad) {
	dt := 1.0 / jsRateHz
	ticker := time.NewTicker(time.Duration(dt * float64(time.Second)))
	defer ticker.Stop()

	var left, right float64
	speedIdx := 2
	prev := &gpPrev{btn: map[int]bool{}}
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

		top := speedSteps[speedIdx]
		if a.turbo {
			top = turbo
		}
		tgtL, tgtR := driveMix(a.throttle, a.steer, top)
		step := ramp * dt
		left = rampToward(left, tgtL, step)
		right = rampToward(right, tgtR, step)
		app.move.setDrive(left, right)

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
				app.lightMu.Lock()
				h, b := app.headOn, app.baseOn
				app.lightMu.Unlock()
				if which == "head" {
					h = state
				} else {
					b = state
				}
				e = app.setLights(h, b)
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
		writeJSON(w, 200, map[string]any{"ok": true})
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

func (app *App) videoFeed(w http.ResponseWriter, r *http.Request) {
	const boundary = "rovercamframe"
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	frames, cancel := app.hub.subscribe()
	defer cancel()
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+boundary)
	w.Header().Set("Cache-Control", "no-store")

	// Keep the <img> alive even with no camera: resend latest/placeholder on a
	// timer so the connection never just hangs.
	keep := time.NewTicker(time.Second)
	defer keep.Stop()
	send := func(frame []byte) bool {
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
		flusher.Flush()
		return true
	}
	for {
		select {
		case frame := <-frames:
			if !send(frame) {
				return
			}
		case <-keep.C:
			f := app.hub.latestFrame()
			if up, _ := app.cam.status(); !up || f == nil {
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
 figure img{width:100%;display:block;aspect-ratio:4/3;object-fit:cover}
 figcaption{font-size:10px;padding:5px;display:flex;justify-content:space-between;gap:5px;word-break:break-all}
 small{color:#999}
</style></head><body>
<header><h1>🤖 Rover controller</h1>
 <button class="warn" onclick="cmd('estop')">⛔ E-STOP</button>
 <button onclick="snap()">📸 Snapshot</button>
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
 <label>speed <input id="cap" type="range" min="0" max="0.5" step="0.05" value="0.25"
   onchange="fetch('/speed?cap='+this.value,{method:'POST'})"></label>
</div>
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
  '<figure><a href="/photos/'+n+'" target="_blank"><img loading="lazy" src="/photos/'+n+'"></a>'+
  '<figcaption><span>'+n+'</span><button class="warn" onclick="del(\''+n+'\')">del</button></figcaption></figure>').join('');
}
async function del(n){await fetch('/delete_photo/'+n,{method:'POST'});seen='';load();}
async function health(){try{const h=await(await fetch('/healthz')).json();
 document.getElementById('health').innerHTML='<small>serial '+(h.serial.up?'✓':'✗')+
 ' · cam '+(h.camera.up?'✓':'✗')+' · pad '+(h.gamepad?'✓':'–')+'</small>';}catch(e){}}
setInterval(()=>{load();health();},2000);load();health();
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
	width := flag.Int("width", 1280, "camera width (0 = let the camera choose)")
	height := flag.Int("height", 720, "camera height (0 = let the camera choose)")
	fps := flag.Int("fps", 30, "camera fps (rpicam only)")
	camMode := flag.String("camera-mode", "auto", "camera backend: auto|v4l2|rpicam")
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

	ctx := context.Background()

	// serial: try now, retry in the background so a busy port doesn't kill us.
	go openSerialWithRetry(ctx, rover, *serialPath)

	// camera
	go app.cam.run(ctx, app.hub)

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

	srv := &http.Server{Addr: ":" + *port, Handler: app.routes()}
	log.Printf("rovercontrol: http://0.0.0.0:%s  (photos: %s)", *port, *photos)
	log.Fatal(srv.ListenAndServe())
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
	// buttons
	for _, step := range []struct {
		prompt string
		dst    *int
	}{
		{"Hold/press TURBO (higher top speed)", &m.Turbo},
		{"Press STOP wheels", &m.Stop},
		{"Press EMERGENCY STOP", &m.Estop},
		{"Press HEAD light toggle", &m.HeadLight},
		{"Press BASE light toggle", &m.BaseLight},
		{"Press CENTER camera", &m.Center},
		{"Press SNAPSHOT", &m.Snapshot},
		{"Press RELAX gimbal", &m.Relax},
		{"Press LOCK gimbal", &m.Lock},
	} {
		if b, ok := captureButton(events, step.prompt); ok {
			*step.dst = b
		}
	}
	m.Hat = captureHat(events)

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
// Kind "buttons" (then asks for DOWN); a timeout → Kind "none".
func captureHat(events <-chan jsEvent) HatMap {
	waitNeutral(events) // drain BEFORE prompting so a fast press isn't swallowed
	fmt.Print("  Press D-pad UP (or wait to skip the speed-cap D-pad) ... ")
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
				up := int(e.number)
				fmt.Printf("button %d\n", up)
				// captureButton drains the UP release first, then prompts DOWN.
				if dn, ok := captureButton(events, "Now press D-pad DOWN"); ok {
					return HatMap{Kind: "buttons", Up: up, Down: dn}
				}
				return HatMap{Kind: "none"}
			}
		case <-deadline:
			fmt.Println("(skipped)")
			return HatMap{Kind: "none"}
		}
	}
}
