//go:build integration

// Integration tests that hit a LIVE, deployed rovercontrol over HTTP — they
// exercise the hardware paths (serial, camera capture, gamepad) that can't run
// in unit tests. Run against the rover:
//
//	go test -tags integration ./...           # uses http://192.168.1.131:8080
//	ROVER_URL=http://host:8080 go test -tags integration ./...
//
// Skipped automatically if the rover isn't reachable, so they never break CI
// when offline. `ci-local.sh --all` runs them.
package main

import (
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

func roverURL() string {
	if v := os.Getenv("ROVER_URL"); v != "" {
		return strings.TrimRight(v, "/")
	}
	return "http://192.168.1.131:8080"
}

func reachable(t *testing.T) string {
	t.Helper()
	base := roverURL()
	c := &http.Client{Timeout: 4 * time.Second}
	resp, err := c.Get(base + "/healthz")
	if err != nil {
		t.Skipf("rover not reachable at %s (%v) — integration skipped", base, err)
	}
	resp.Body.Close()
	return base
}

func post(t *testing.T, base, path string) int {
	t.Helper()
	c := &http.Client{Timeout: 6 * time.Second}
	req, _ := http.NewRequest("POST", base+path, nil)
	resp, err := c.Do(req)
	if err != nil {
		t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	return resp.StatusCode
}

func TestIntegrationHealthz(t *testing.T) {
	base := reachable(t)
	resp, err := http.Get(base + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var h struct {
		OK     bool `json:"ok"`
		Serial struct {
			Up bool `json:"up"`
		} `json:"serial"`
		Camera struct {
			Up bool `json:"up"`
		} `json:"camera"`
	}
	json.NewDecoder(resp.Body).Decode(&h)
	if !h.OK {
		t.Fatal("healthz not ok")
	}
	t.Logf("serial.up=%v camera.up=%v", h.Serial.Up, h.Camera.Up)
	if !h.Serial.Up {
		t.Error("serial down — controller can't drive")
	}
	if !h.Camera.Up {
		t.Error("camera down — no video/snapshots")
	}
}

func TestIntegrationControl(t *testing.T) {
	base := reachable(t)
	// nudge then stop; toggle each light twice (back to start); center camera
	for _, p := range []string{"/move_forward?ms=200", "/stop",
		"/light_head", "/light_head", "/light_base", "/light_base",
		"/camera_center", "/gimbal_lock"} {
		if code := post(t, base, p); code != 200 {
			t.Errorf("POST %s = %d, want 200", p, code)
		}
	}
}

func TestIntegrationVideoAndSnapshot(t *testing.T) {
	base := reachable(t)
	// live MJPEG: first part should contain a JPEG SOI
	resp, err := (&http.Client{Timeout: 6 * time.Second}).Get(base + "/video_feed")
	if err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 1024)
	n, _ := io.ReadAtLeast(resp.Body, buf, 64)
	resp.Body.Close()
	if !strings.Contains(string(buf[:n]), "image/jpeg") || !strings.Contains(string(buf[:n]), "\xff\xd8") {
		t.Fatalf("video_feed: no JPEG frame in first %d bytes", n)
	}
	// snapshot returns a name
	r2, err := (&http.Client{Timeout: 8 * time.Second}).Post(base+"/snapshot", "", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer r2.Body.Close()
	var s struct {
		OK   bool   `json:"ok"`
		Name string `json:"name"`
	}
	json.NewDecoder(r2.Body).Decode(&s)
	if !s.OK || !strings.HasSuffix(s.Name, ".jpg") {
		t.Fatalf("snapshot: %+v", s)
	}
}
