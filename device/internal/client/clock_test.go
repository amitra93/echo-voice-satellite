package client

import (
	"encoding/json"
	"testing"

	deviceclock "github.com/wilbowes/EchoMuse/internal/clock"
)

func TestClockProbeReplyPreservesIDAndTimestamps(t *testing.T) {
	reply, ok := clockProbeReply([]byte(`{"type":"clock_probe","id":17,"controller_sent_us":99}`), 1234, 1240)
	if !ok {
		t.Fatal("clock probe was rejected")
	}
	if reply["type"] != "clock_probe" {
		t.Fatalf("type = %v", reply["type"])
	}
	if string(reply["id"].(json.RawMessage)) != "17" {
		t.Fatalf("id = %s", reply["id"])
	}
	if reply["device_received_us"] != int64(1234) {
		t.Fatalf("received = %v", reply["device_received_us"])
	}
	if reply["device_sent_us"] != int64(1240) {
		t.Fatalf("sent = %v", reply["device_sent_us"])
	}
}

func TestClockProbeReplyRejectsMissingOrMalformedID(t *testing.T) {
	for _, raw := range [][]byte{
		[]byte(`{"type":"clock_probe"}`),
		[]byte(`{"type":"clock_probe","id":}`),
		[]byte(`{"type":"clock_probe","id":null}`),
		[]byte(`{"type":"clock_probe","id":""}`),
	} {
		if _, ok := clockProbeReply(raw, 1, 2); ok {
			t.Fatalf("accepted invalid probe %s", raw)
		}
	}
}

func TestDeviceMonotonicClockDoesNotMoveBackwards(t *testing.T) {
	first := deviceclock.NowUs()
	second := deviceclock.NowUs()
	if second < first {
		t.Fatalf("device monotonic clock moved backwards: %d -> %d", first, second)
	}
}

func TestClockProbeReplyPreservesStringID(t *testing.T) {
	reply, ok := clockProbeReply([]byte(`{"type":"clock_probe","id":"probe-7"}`), 10, 11)
	if !ok {
		t.Fatal("clock probe was rejected")
	}
	if string(reply["id"].(json.RawMessage)) != `"probe-7"` {
		t.Fatalf("id = %s", reply["id"])
	}
}

func TestClockProbeReplyRejectsInvalidDeviceTimestamps(t *testing.T) {
	raw := []byte(`{"type":"clock_probe","id":1}`)
	for _, tc := range []struct {
		received int64
		sent     int64
	}{
		{received: -1, sent: 0},
		{received: 2, sent: 1},
	} {
		if _, ok := clockProbeReply(raw, tc.received, tc.sent); ok {
			t.Fatalf("accepted timestamps received=%d sent=%d", tc.received, tc.sent)
		}
	}
}
