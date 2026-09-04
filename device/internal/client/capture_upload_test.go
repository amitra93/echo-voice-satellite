package client

import (
	"context"
	"crypto/md5"
	"encoding/binary"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/wakeword/capture"
	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
)

func TestCaptureFramesMatchControllerContract(t *testing.T) {
	metadata := capture.Metadata{
		CaptureID: "boot:1", Kind: "act", Model: "wake",
		ClassifierMD5: "0123456789abcdef0123456789abcdef",
		Score:         0.8, Threshold: 0.5, NearMissFloor: 0.1,
		ActivationSeq: 20, RequestedPrerollMs: 80, ActualPrerollMs: 80,
		Complete: true, SampleRate: 16000, SampleWidth: 2, Channels: 1,
		FrameBytes: captureFrameBytes,
	}
	begin, err := encodeCaptureBegin(metadata)
	if err != nil || begin[0] != frameTypeCaptureBegin || begin[1] != captureProtocolVersion {
		t.Fatalf("begin = %x, %v", begin, err)
	}
	length := int(binary.BigEndian.Uint16(begin[2:4]))
	var decoded capture.Metadata
	if length != len(begin)-4 || json.Unmarshal(begin[4:], &decoded) != nil || decoded.CaptureID != "boot:1" {
		t.Fatalf("decoded begin = %#v", decoded)
	}
	pcm := make([]byte, captureFrameBytes)
	chunk := encodeCapturePCM(7, pcm)
	if chunk[0] != frameTypeCapturePCM || binary.BigEndian.Uint16(chunk[1:3]) != 7 || len(chunk) != 3+captureFrameBytes {
		t.Fatalf("PCM frame malformed")
	}
	end := encodeCaptureEnd(1, pcm)
	sum := md5.Sum(pcm)
	if end[0] != frameTypeCaptureEnd || binary.BigEndian.Uint16(end[1:3]) != 1 ||
		binary.BigEndian.Uint32(end[3:7]) != uint32(len(pcm)) || string(end[7:]) != string(sum[:]) {
		t.Fatalf("end frame = %x", end)
	}
}

func TestCaptureUploaderStopsWithItsConnectionAndRetriesUnackedCapture(t *testing.T) {
	receivedEnd := make(chan struct{}, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			_, frame, err := conn.ReadMessage()
			if err != nil {
				return
			}
			if len(frame) > 0 && frame[0] == frameTypeCaptureEnd {
				receivedEnd <- struct{}{}
			}
		}
	}))
	defer server.Close()
	conn, _, err := websocket.DefaultDialer.Dial(
		"ws://"+strings.TrimPrefix(server.URL, "http://"), nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	ring := capture.New(capture.DefaultFrames)
	manager := capture.NewManager(ring)
	manager.Configure(capture.Settings{
		Enabled: true, Frames: 1, NearMissFloor: 0.1, Model: "wake",
		ClassifierMD5: "0123456789abcdef0123456789abcdef",
	})
	ring.Push(1, make([]byte, captureFrameBytes))
	manager.Observe(shadow.ScoreEvent{
		Score: 0.8, Threshold: 0.5, Sequence: 1, Crossed: true,
	})
	manager.BindRequest(1, "wake:1")
	manager.Deny("wake:1")
	d := &DataClient{conn: conn, captureManager: manager}
	done := make(chan struct{})
	uploaderDone := make(chan struct{})
	go func() {
		d.runCaptureUploader(context.Background(), done, conn)
		close(uploaderDone)
	}()
	select {
	case <-receivedEnd:
	case <-time.After(time.Second):
		t.Fatal("capture upload did not complete")
	}
	close(done)
	select {
	case <-uploaderDone:
	case <-time.After(time.Second):
		t.Fatal("capture uploader outlived its data connection")
	}
	if item := manager.NextReady(); item == nil {
		t.Fatal("unacknowledged capture was not retried after reconnect")
	}
}

func TestConfigureStopCapturesUsesStopKindsAndUploadsWithoutGrant(t *testing.T) {
	d := NewDataClient("stop-capture-test", nil, nil)
	// Configure first: an identity change (the manager's zero-value settings
	// to this model/checksum) clears the ring, same as capture.Manager's own
	// tests do.
	d.ConfigureStopCaptures(true, 0.08, 0.1, "stop", "0123456789abcdef0123456789abcdef")
	d.stopRing.Push(1, make([]byte, captureFrameBytes))
	d.ObserveStopScore(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 1, Crossed: true})
	item := d.stopCaptureManager.NextReady()
	if item == nil || item.Metadata.Kind != "stop_act" {
		t.Fatalf("stop capture = %#v", item)
	}
	// The wake manager must be entirely untouched by a stop-only config push.
	if d.captureManager.Enabled() {
		t.Fatal("stop capture config enabled the wake capture manager")
	}
}

func TestRunCaptureUploaderServesBothManagersWithoutInterleaving(t *testing.T) {
	type received struct {
		kind   string
		frames int
		ends   int
	}
	results := make(chan received, 2)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		var current received
		inCapture := false
		for {
			_, frame, err := conn.ReadMessage()
			if err != nil {
				return
			}
			if len(frame) == 0 {
				continue
			}
			switch frame[0] {
			case frameTypeCaptureBegin:
				if inCapture {
					t.Errorf("BEGIN received while a capture was already open")
				}
				inCapture = true
				var meta capture.Metadata
				length := int(binary.BigEndian.Uint16(frame[2:4]))
				if json.Unmarshal(frame[4:4+length], &meta) == nil {
					current = received{kind: meta.Kind}
				}
			case frameTypeCapturePCM:
				current.frames++
			case frameTypeCaptureEnd:
				current.ends++
				inCapture = false
				results <- current
			}
		}
	}))
	defer server.Close()
	conn, _, err := websocket.DefaultDialer.Dial(
		"ws://"+strings.TrimPrefix(server.URL, "http://"), nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	d := NewDataClient("dual-capture-test", nil, nil)
	d.conn = conn
	d.ConfigureWakeCaptures(true, 0.08, 0.1, "wake", "0123456789abcdef0123456789abcdef")
	d.ConfigureStopCaptures(true, 0.08, 0.1, "stop", "0123456789abcdef0123456789abcdef")
	d.localRing.Push(1, make([]byte, captureFrameBytes))
	d.stopRing.Push(1, make([]byte, captureFrameBytes))
	d.ObserveWakeScore(shadow.ScoreEvent{Score: 0.2, Threshold: 0.5, Sequence: 1})
	time.Sleep(15 * time.Millisecond) // clear the wake near-miss debounce window
	d.ObserveWakeScore(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 1, Crossed: true})
	d.captureManager.BindRequest(1, "wake:dual")
	d.captureManager.Deny("wake:dual")
	d.ObserveStopScore(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 1, Crossed: true})

	done := make(chan struct{})
	uploaderDone := make(chan struct{})
	go func() {
		d.runCaptureUploader(context.Background(), done, conn)
		close(uploaderDone)
	}()
	seen := map[string]received{}
	for i := 0; i < 2; i++ {
		select {
		case r := <-results:
			seen[r.kind] = r
		case <-time.After(time.Second):
			t.Fatalf("only received %d of 2 captures: %#v", i, seen)
		}
	}
	close(done)
	select {
	case <-uploaderDone:
	case <-time.After(time.Second):
		t.Fatal("capture uploader outlived its data connection")
	}
	for _, kind := range []string{"act", "stop_act"} {
		r, ok := seen[kind]
		if !ok || r.frames != 1 || r.ends != 1 {
			t.Fatalf("capture %q = %#v (all seen: %#v)", kind, r, seen)
		}
	}
}

func TestLiveSTTPreemptsCaptureAfterCurrentChunk(t *testing.T) {
	var pcmFrames atomic.Int32
	var endFrames atomic.Int32
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			_, frame, err := conn.ReadMessage()
			if err != nil {
				return
			}
			if len(frame) == 0 {
				continue
			}
			switch frame[0] {
			case frameTypeCapturePCM:
				pcmFrames.Add(1)
			case frameTypeCaptureEnd:
				endFrames.Add(1)
			}
		}
	}))
	defer server.Close()
	conn, _, err := websocket.DefaultDialer.Dial(
		"ws://"+strings.TrimPrefix(server.URL, "http://"), nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	ring := capture.New(capture.DefaultFrames)
	manager := capture.NewManager(ring)
	manager.Configure(capture.Settings{
		Enabled: true, Frames: 3, NearMissFloor: 0.1, Model: "wake",
		ClassifierMD5: "0123456789abcdef0123456789abcdef",
	})
	for sequence := uint16(1); sequence <= 3; sequence++ {
		ring.Push(sequence, make([]byte, captureFrameBytes))
	}
	manager.Observe(shadow.ScoreEvent{
		Score: 0.8, Threshold: 0.5, Sequence: 3, Crossed: true,
	})
	manager.BindRequest(3, "wake:stt")
	manager.Deny("wake:stt")
	d := &DataClient{conn: conn, captureManager: manager}
	preempted := make(chan struct{})
	var yielded atomic.Bool
	originalYield := captureYield
	captureYield = func() {
		if yielded.CompareAndSwap(false, true) {
			d.wakeMu.Lock()
			d.wakeGranted = true
			d.wakeMu.Unlock()
			close(preempted)
		}
	}
	t.Cleanup(func() { captureYield = originalYield })
	done := make(chan struct{})
	uploaderDone := make(chan struct{})
	go func() {
		d.runCaptureUploader(context.Background(), done, conn)
		close(uploaderDone)
	}()
	select {
	case <-preempted:
	case <-time.After(time.Second):
		t.Fatal("STT did not preempt active capture upload")
	}
	time.Sleep(50 * time.Millisecond)
	if got := pcmFrames.Load(); got != 1 || endFrames.Load() != 0 {
		t.Fatalf("capture advanced during STT: pcm=%d end=%d", got, endFrames.Load())
	}
	d.wakeMu.Lock()
	d.wakeGranted = false
	d.wakeMu.Unlock()
	select {
	case <-time.After(time.Second):
		t.Fatal("capture did not resume after STT")
	case <-func() <-chan struct{} {
		finished := make(chan struct{})
		go func() {
			for endFrames.Load() == 0 {
				time.Sleep(time.Millisecond)
			}
			close(finished)
		}()
		return finished
	}():
	}
	close(done)
	<-uploaderDone
}
