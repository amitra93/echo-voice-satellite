package client

import (
	"context"
	"crypto/md5"
	"encoding/binary"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
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
