package main

// Hidden test (realtasks/verify.py): the OpenAI front door hands voxtype the sentence
// without whisper-server's per-segment newlines, and leaves verbose_json alone.

import (
	"bytes"
	"encoding/json"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func hiddenPost(t *testing.T, upstreamBody, format string) []byte {
	ws := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, upstreamBody)
	}))
	defer ws.Close()
	cfg := &config{api: ws.URL + "/inference", timeout: 10 * time.Second}
	front := httptest.NewServer(openAIMux(cfg))
	defer front.Close()
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	fw, _ := mw.CreateFormFile("file", "a.wav")
	fw.Write([]byte("RIFF0000WAVE"))
	mw.WriteField("language", "es")
	mw.WriteField("response_format", format) // voxtype asks for json, meeting mode for verbose_json
	mw.Close()
	resp, err := http.Post(front.URL+"/v1/audio/transcriptions", mw.FormDataContentType(), &buf)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	return out
}

func TestHiddenFrontDoorJoinsSegments(t *testing.T) {
	cases := map[string]string{
		`{"text":" Vale, me aparece la notificación pero no me lo está escrib\niendo.\n"}`: " Vale, me aparece la notificación pero no me lo está escribiendo.",
		`{"text":" ¿Me lo\n puedes probar?\n"}`: " ¿Me lo puedes probar?",
	}
	for in, want := range cases {
		var got struct{ Text string }
		out := hiddenPost(t, in, "json")
		if err := json.Unmarshal(out, &got); err != nil {
			t.Fatalf("reply is not JSON: %s", out)
		}
		// the sentence must come back whole; whisper's leading space may or may not be kept
		if strings.TrimSpace(got.Text) != strings.TrimSpace(want) {
			t.Errorf("text = %q, want %q", got.Text, want)
		}
	}
}

func TestHiddenFrontDoorKeepsVerboseJSON(t *testing.T) {
	in := `{"text":"a\n b\n","segments":[{"start":0,"text":"a"}]}`
	var got, want map[string]any
	json.Unmarshal([]byte(in), &want)
	if err := json.Unmarshal(hiddenPost(t, in, "verbose_json"), &got); err != nil {
		t.Fatal(err)
	}
	// the prompt protects the segments; joining the top-level text as well is allowed
	g, _ := json.Marshal(got["segments"])
	w, _ := json.Marshal(want["segments"])
	if !bytes.Equal(g, w) {
		t.Errorf("verbose_json segments changed: %s", g)
	}
}
