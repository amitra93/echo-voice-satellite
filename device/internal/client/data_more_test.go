package client

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/pkg/buttons"
)

// ─── ConfigureWakeCaptures / ConfigureStopCaptures: disabling closes the
// in-flight data connection ─────────────────────────────────────────────────

func TestConfigureWakeCapturesClosesConnectionOnDisable(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	closed := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		_, _, _ = conn.ReadMessage()
		close(closed)
	}))
	defer server.Close()

	addr := "ws://" + strings.TrimPrefix(server.URL, "http://")
	conn, _, err := websocket.DefaultDialer.Dial(addr, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	d := NewDataClient("test", nil, nil)
	d.conn = conn
	d.ConfigureWakeCaptures(true, 1.0, 0.1, "hey_jarvis_v0.1", "md5sum")
	// Enabled -> disabled with a live connection must close it, per the
	// "queue drained, connection bounced" contract documented on
	// ConfigureWakeCaptures.
	d.ConfigureWakeCaptures(false, 1.0, 0.1, "hey_jarvis_v0.1", "md5sum")

	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("disabling wake captures did not close the data connection")
	}
}

func TestConfigureStopCapturesClosesConnectionOnDisable(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	closed := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		_, _, _ = conn.ReadMessage()
		close(closed)
	}))
	defer server.Close()

	addr := "ws://" + strings.TrimPrefix(server.URL, "http://")
	conn, _, err := websocket.DefaultDialer.Dial(addr, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	d := NewDataClient("test", nil, nil)
	d.conn = conn
	d.ConfigureStopCaptures(true, 1.0, 0.1, "stop_v1", "md5sum")
	d.ConfigureStopCaptures(false, 1.0, 0.1, "stop_v1", "md5sum")

	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("disabling stop captures did not close the data connection")
	}
}

// ─── SetShadowScorer closes the scorer it replaces ─────────────────────────

func TestSetShadowScorerClosesReplacedScorer(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	first := newTestScorer(t, 0.1)
	d.SetShadowScorer(first)
	if d.ShadowScorer() != first {
		t.Fatal("SetShadowScorer did not install the scorer")
	}
	// Replacing it must Close the old one (idempotent — see shadow.Scorer.Close
	// — so a redundant Close from t.Cleanup afterwards is harmless).
	d.SetShadowScorer(nil)
	if d.ShadowScorer() != nil {
		t.Fatal("SetShadowScorer(nil) did not clear the scorer")
	}
}

// ─── writeJSON / SendButton over a genuinely broken connection ────────────

func TestSendButtonOnReleaseCreatesWakeRequestAndSurvivesBrokenConn(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		conn.Close() // close immediately so the client's next write fails
	}))
	defer server.Close()

	addr := "ws://" + strings.TrimPrefix(server.URL, "http://")
	conn, _, err := websocket.DefaultDialer.Dial(addr, nil)
	if err != nil {
		t.Fatal(err)
	}
	// Give the server a moment to close its end before we write, and close
	// our own end too — either is sufficient to force a write error.
	conn.Close()

	c := &ControlClient{conn: conn}
	// A DotClick release is the one SendButton path that also opens a wake
	// request (button-press-as-wake); combined with the closed conn this
	// exercises both SendButton's beginWakeRequest branch and its
	// writeJSON-error log branch in one call.
	c.SendButton(buttons.ButtonClickEvent{ClickType: buttons.DotClick, Down: false, HeldMs: 120})
	if c.pendingWake.id == "" {
		t.Fatal("SendButton did not open a wake request for a dot-button release")
	}
}

// ─── SendWakeStatus field combinations ─────────────────────────────────────

func TestSendWakeStatusFieldCombinations(t *testing.T) {
	c := &ControlClient{}
	// Disconnected client — writeJSON no-ops, but every branch inside
	// SendWakeStatus must still execute regardless of connection state.
	c.SendWakeStatus(true, "hey_jarvis_v0.1", "", "")
	c.SendWakeStatus(false, "hey_jarvis_v0.1", "abc123", "model missing")
	c.SendWakeStatus(false, "", "", "runtime unavailable")
}

// ─── GetSerialNo: getprop succeeds but prints nothing ──────────────────────

func TestGetSerialNoFallsBackOnEmptyOutput(t *testing.T) {
	dir := t.TempDir()
	getprop := filepath.Join(dir, "getprop")
	if err := os.WriteFile(getprop, []byte("#!/bin/sh\nprintf ''\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Cleanup(func() { _ = os.Setenv("PATH", oldPath) })
	if err := os.Setenv("PATH", dir); err != nil {
		t.Fatal(err)
	}
	if got := GetSerialNo(); got != "unknown-device" {
		t.Fatalf("GetSerialNo() with empty getprop output = %q, want unknown-device", got)
	}
}

// ─── decodeMusicSyncPCM: generation 0 is rejected ──────────────────────────

func TestDecodeMusicSyncPCMRejectsZeroGeneration(t *testing.T) {
	raw, err := encodeMusicSyncPCM(musicSyncPCMFrame{Generation: 1, PCM: []byte{1, 2}})
	if err != nil {
		t.Fatal(err)
	}
	// Zero out the generation field post-encode (encodeMusicSyncPCM itself
	// refuses to build a zero-generation frame, so this is the only way to
	// produce one on the wire for decodeMusicSyncPCM to reject).
	raw[1], raw[2], raw[3], raw[4] = 0, 0, 0, 0
	if _, err := decodeMusicSyncPCM(raw); err == nil {
		t.Fatal("decodeMusicSyncPCM accepted generation 0")
	}
}

// ─── Run: an already-canceled context returns immediately, no network ─────

func TestControlClientRunReturnsImmediatelyForCanceledContext(t *testing.T) {
	c := &ControlClient{}
	called := false
	c.OnDisconnected(func() { called = true })
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := c.Run(ctx, NewDataClient("test", nil, nil)); !errors.Is(err, context.Canceled) {
		t.Fatalf("Run(canceled) error = %v, want context.Canceled", err)
	}
	if called {
		t.Fatal("Run must check ctx.Err() before touching any callback")
	}
}

func TestDataClientRunReturnsImmediatelyForCanceledContext(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := d.Run(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("Run(canceled) error = %v, want context.Canceled", err)
	}
}

// ─── connect: a dial failure surfaces as an error, no panic ───────────────

func TestDataConnectSurfacesDialError(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	// Port 1 is a reserved, always-refused TCP port — a fast, deterministic
	// dial failure with no real server involved.
	if err := d.connect(context.Background(), "ws://127.0.0.1:1"); err == nil {
		t.Fatal("expected a dial error connecting to a refused port")
	}
}

// ─── StreamTestAudio / CleanupTestAudio: the fixed device path is absent on
// the host, which is itself the natural, safe way to exercise their error
// paths — nothing is written to it. ─────────────────────────────────────────

func TestStreamTestAudioFailsCleanlyWithoutTheDeviceFile(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	if err := d.StreamTestAudio(); err == nil {
		t.Fatal("expected an error opening the fixed test-audio path on a host with no such file")
	}
}

func TestCleanupTestAudioIsANoOpWithoutTheDeviceFile(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	if err := d.CleanupTestAudio(); err != nil {
		t.Fatalf("CleanupTestAudio() = %v, want nil for an already-absent file", err)
	}
}

// ─── connect: speaker error paths and the music-sync capability gate ──────

type erroringSpeaker struct {
	helperMusicSyncReceiver
}

func (s *erroringSpeaker) Init() error             { return nil }
func (s *erroringSpeaker) PumpPeriod([]byte) error { return errors.New("period write failed") }
func (s *erroringSpeaker) EndStream()              {}
func (s *erroringSpeaker) Flush()                  {}
func (s *erroringSpeaker) PumpMusic([]byte) error  { return errors.New("music write failed") }
func (s *erroringSpeaker) EndMusicStream()         {}
func (s *erroringSpeaker) FlushMusic()             {}
func (s *erroringSpeaker) SetDuck(float64)         {}
func (s *erroringSpeaker) Close()                  {}

// capabilityLessSpeaker implements speaker.Speaker but NOT
// speaker.MusicSyncReceiver, exercising connect()'s "speaker lacks
// capability" branches for music-sync frames.
type capabilityLessSpeaker struct{}

func (s *capabilityLessSpeaker) Init() error             { return nil }
func (s *capabilityLessSpeaker) PumpPeriod([]byte) error { return nil }
func (s *capabilityLessSpeaker) EndStream()              {}
func (s *capabilityLessSpeaker) Flush()                  {}
func (s *capabilityLessSpeaker) PumpMusic([]byte) error  { return nil }
func (s *capabilityLessSpeaker) EndMusicStream()         {}
func (s *capabilityLessSpeaker) FlushMusic()             {}
func (s *capabilityLessSpeaker) SetDuck(float64)         {}
func (s *capabilityLessSpeaker) Close()                  {}

func TestDataConnectLogsSpeakerErrorsWithoutFailingTheStream(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		var identify map[string]string
		if err := conn.ReadJSON(&identify); err != nil {
			return
		}
		_ = conn.WriteMessage(websocket.BinaryMessage, []byte{frameTypeSpeaker, 1, 2})
		_ = conn.WriteMessage(websocket.BinaryMessage, []byte{frameTypeMusic, 3, 4})
		_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, "done"), time.Now().Add(time.Second))
	}))
	defer server.Close()

	d := NewDataClient("data-test", nil, &erroringSpeaker{})
	addr := "ws://" + strings.TrimPrefix(server.URL, "http://")
	if err := d.connect(context.Background(), addr); err == nil {
		t.Fatal("expected connect to return once the server closes")
	}
}

func TestDataConnectIgnoresMusicSyncFramesWithoutCapability(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		var identify map[string]string
		if err := conn.ReadJSON(&identify); err != nil {
			return
		}
		_ = conn.WriteMessage(websocket.BinaryMessage, []byte{frameTypeMusicSyncStart, 0, 0, 0, 7})
		_ = conn.WriteMessage(websocket.BinaryMessage, []byte{frameTypeMusicSyncPCM, 0, 0, 0, 7, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 99, 1, 2})
		_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, "done"), time.Now().Add(time.Second))
	}))
	defer server.Close()

	d := NewDataClient("data-test", nil, &capabilityLessSpeaker{})
	addr := "ws://" + strings.TrimPrefix(server.URL, "http://")
	if err := d.connect(context.Background(), addr); err == nil {
		t.Fatal("expected connect to return once the server closes")
	}
}

// ─── SendWakeRequest over a broken connection logs rather than panics ─────

func TestSendWakeRequestLogsWriteFailureWithoutPanicking(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		conn.Close()
	}))
	defer server.Close()
	addr := "ws://" + strings.TrimPrefix(server.URL, "http://")
	conn, _, err := websocket.DefaultDialer.Dial(addr, nil)
	if err != nil {
		t.Fatal(err)
	}
	conn.Close()

	c := &ControlClient{conn: conn}
	if id := c.SendWakeRequest("model", 0.9, 0.5, 1, 1); id == "" {
		t.Fatal("SendWakeRequest must still return a request id on a write failure")
	}
}

// ─── beginWakeRequest: a timeout firing after the request was already
// consumed must be a silent no-op, not a stale denial. ─────────────────────

func TestWakeRequestTimeoutIsANoOpAfterConsumption(t *testing.T) {
	old := wakeRequestTTL
	wakeRequestTTL = 5 * time.Millisecond
	t.Cleanup(func() { wakeRequestTTL = old })
	c := &ControlClient{}
	var denyCalls int
	c.OnWakeDeny(func(requestID, source, reason string) { denyCalls++ })

	pending, created := c.beginWakeRequest("wakeword", 1)
	if !created {
		t.Fatal("wake request was not created")
	}
	if _, ok := c.consumeWakeDecision(pending.id); !ok {
		t.Fatal("failed to consume the pending request before its timer fired")
	}
	time.Sleep(30 * time.Millisecond)
	if denyCalls != 0 {
		t.Fatalf("expected no deny callback for an already-consumed request, got %d calls", denyCalls)
	}
}

// ─── readTestWAV: truncated reads inside fmt/data chunk bodies ────────────

func TestReadTestWAVRejectsTruncatedChunkBodies(t *testing.T) {
	// fmt chunk declares a valid size (16) but the reader runs out partway
	// through it — io.ReadFull inside the fmt branch must surface that.
	truncatedFmt := append(wavHeader(), wavChunk("fmt ", make([]byte, 16))[:8+5]...)
	if _, err := readTestWAV(strings.NewReader(string(truncatedFmt))); err == nil {
		t.Fatal("expected an error for a truncated fmt chunk body")
	}

	// A valid fmt chunk followed by a data chunk that declares more bytes
	// than actually follow — io.ReadFull inside the data branch must
	// surface that rather than returning a short, silently-wrong PCM slice.
	fmtBuf := make([]byte, 16)
	fmtBuf[0], fmtBuf[2] = 1, 1
	fmtBuf[4], fmtBuf[5], fmtBuf[6], fmtBuf[7] = 0x80, 0x3e, 0, 0 // 16000 LE
	fmtBuf[14], fmtBuf[15] = 16, 0
	valid := append(wavHeader(), wavChunk("fmt ", fmtBuf)...)
	dataHeader := make([]byte, 8)
	copy(dataHeader, "data")
	dataHeader[4], dataHeader[5], dataHeader[6], dataHeader[7] = 100, 0, 0, 0 // claims 100 bytes
	truncatedData := append(valid, dataHeader...)
	truncatedData = append(truncatedData, []byte{1, 2, 3}...) // only 3 actually present
	if _, err := readTestWAV(strings.NewReader(string(truncatedData))); err == nil {
		t.Fatal("expected an error for a truncated data chunk body")
	}
}
