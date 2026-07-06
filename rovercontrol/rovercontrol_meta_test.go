package main

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func doJSON(t *testing.T, app *App, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	w := httptest.NewRecorder()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	app.routes().ServeHTTP(w, req)
	return w
}

// Plan 020: photo_meta sidecars power the gallery's toggleable outline.
func TestPhotoMetaRoundtrip(t *testing.T) {
	app, _ := testApp(t)
	body := `{"target":"a green pen","label":"green pen","color":"green","bbox":[0.1,0.2,0.5,0.6],"confidence":0.9}`
	if w := doJSON(t, app, "POST", "/photo_meta/rover_x.jpg", body); w.Code != 200 {
		t.Fatalf("POST meta: %d %s", w.Code, w.Body.String())
	}
	w := do(t, app, "GET", "/photo_meta/rover_x.jpg")
	if w.Code != 200 {
		t.Fatalf("GET meta: %d", w.Code)
	}
	var m struct {
		Target string    `json:"target"`
		Label  string    `json:"label"`
		Color  string    `json:"color"`
		BBox   []float64 `json:"bbox"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &m); err != nil {
		t.Fatal(err)
	}
	if m.Target != "a green pen" || m.Label != "green pen" || m.Color != "green" || len(m.BBox) != 4 || m.BBox[2] != 0.5 {
		t.Fatalf("meta content: %+v", m)
	}
	// sidecar lives next to the photo and is removed with it
	side := filepath.Join(app.photoDir, "rover_x.jpg.meta.json")
	if _, err := os.Stat(side); err != nil {
		t.Fatalf("sidecar missing: %v", err)
	}
	// /photos reports which names have outlines (the gallery gates its toggle on it)
	lw := do(t, app, "GET", "/photos")
	var lst struct {
		Outlined []string `json:"outlined"`
	}
	if err := json.Unmarshal(lw.Body.Bytes(), &lst); err != nil {
		t.Fatal(err)
	}
	if len(lst.Outlined) != 1 || lst.Outlined[0] != "rover_x.jpg" {
		t.Fatalf("outlined list: %v", lst.Outlined)
	}
	do(t, app, "POST", "/delete_photo/rover_x.jpg")
	if _, err := os.Stat(side); !os.IsNotExist(err) {
		t.Fatal("sidecar not removed with the photo")
	}
}

func TestPhotoMetaValidation(t *testing.T) {
	app, _ := testApp(t)
	cases := []struct {
		path, body string
	}{
		{"/photo_meta/sub/evil.jpg", `{"bbox":[0.1,0.2,0.5,0.6]}`},       // subpath name
		{"/photo_meta/rover_x.jpg", `{"bbox":[0.5,0.2,0.1,0.6]}`},        // reversed
		{"/photo_meta/rover_x.jpg", `{"bbox":[0.1,0.2,0.5,1.5]}`},        // out of range
		{"/photo_meta/rover_x.jpg", `{"bbox":[0.1,0.2,0.5]}`},            // wrong arity
		{"/photo_meta/rover_x.jpg", `not json`},                          // bad body
	}
	for _, c := range cases {
		if w := doJSON(t, app, "POST", c.path, c.body); w.Code != 400 {
			t.Errorf("POST %s %q = %d (want 400)", c.path, c.body, w.Code)
		}
	}
	// raw ../ traversal is stopped before the handler (mux canonicalization);
	// anything but a 2xx write is acceptable
	if w := doJSON(t, app, "POST", "/photo_meta/../evil.jpg", `{"bbox":[0.1,0.2,0.5,0.6]}`); w.Code == 200 {
		t.Error("../ traversal reached the handler and succeeded")
	}
	if w := do(t, app, "GET", "/photo_meta/rover_absent.jpg"); w.Code != 404 {
		t.Errorf("GET absent meta = %d (want 404)", w.Code)
	}
}

func TestPanoramaEndpoints(t *testing.T) {
	app, _ := testApp(t)
	if w := do(t, app, "GET", "/panorama"); w.Code != 404 {
		t.Fatalf("GET before upload = %d (want 404)", w.Code)
	}
	jpeg := "\xff\xd8fakejpegdata"
	if w := doJSON(t, app, "POST", "/panorama", jpeg); w.Code != 200 {
		t.Fatalf("POST pano = %d %s", w.Code, w.Body.String())
	}
	if w := doJSON(t, app, "POST", "/panorama", "not a jpeg"); w.Code != 400 {
		t.Fatalf("POST non-jpeg = %d (want 400)", w.Code)
	}
	if w := do(t, app, "GET", "/panorama"); w.Code != 200 || w.Body.String() != jpeg {
		t.Fatalf("GET pano = %d", w.Code)
	}
}

func TestPanoStatusEndpoints(t *testing.T) {
	app, _ := testApp(t)
	if w := doJSON(t, app, "POST", "/pano_status?state=stitching", ""); w.Code != 200 {
		t.Fatalf("POST status = %d", w.Code)
	}
	if w := doJSON(t, app, "POST", "/pano_status?state=hacked", ""); w.Code != 400 {
		t.Fatalf("bad state = %d (want 400)", w.Code)
	}
	w := do(t, app, "GET", "/pano_status")
	var st struct {
		State string  `json:"state"`
		Age   float64 `json:"age_s"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &st); err != nil {
		t.Fatal(err)
	}
	if st.State != "stitching" || st.Age < 0 {
		t.Fatalf("status: %+v", st)
	}
}

func TestTourEndpoints(t *testing.T) {
	app, _ := testApp(t)
	if w := do(t, app, "GET", "/tour_feed"); w.Code != 404 {
		t.Fatalf("feed before upload = %d", w.Code)
	}
	two := "\xff\xd8frameone\xff\xd8frametwo"
	if w := doJSON(t, app, "POST", "/tour", two); w.Code != 200 {
		t.Fatalf("POST tour = %d %s", w.Code, w.Body.String())
	}
	if w := doJSON(t, app, "POST", "/tour", "junk"); w.Code != 400 {
		t.Fatalf("POST junk = %d", w.Code)
	}
	w := do(t, app, "GET", "/tour_feed?loops=1")
	if w.Code != 200 {
		t.Fatalf("feed = %d", w.Code)
	}
	if got := strings.Count(w.Body.String(), "--tourframe"); got != 2 {
		t.Fatalf("frames streamed = %d (want 2)", got)
	}
}

func TestWebUIHasOutlineToggle(t *testing.T) {
	app, _ := testApp(t)
	body := do(t, app, "GET", "/").Body.String()
	for _, want := range []string{"outline(", "coverPct(", "photo_meta", "className='obox'", "lightbox(", "lbwrap", "fetchMeta(", "boxLabel(", "pano3d(", "3D view", "/panorama", "pano_status", "downBg", "panostat", "tour_feed", "Room tour"} {
		if !strings.Contains(body, want) {
			t.Errorf("page missing %q", want)
		}
	}
}
