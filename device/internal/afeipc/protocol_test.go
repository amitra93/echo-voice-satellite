package afeipc

import (
	"bytes"
	"encoding/binary"
	"strings"
	"testing"
)

func TestFrameRoundTrip(t *testing.T) {
	want := Frame{Type: WritePlayer, RequestID: 42, Payload: []byte{0, 1, 2, 255}}
	var wire bytes.Buffer
	if err := want.WriteFrame(&wire); err != nil {
		t.Fatal(err)
	}
	got, err := ReadFrame(&wire)
	if err != nil {
		t.Fatal(err)
	}
	if got.Type != want.Type || got.RequestID != want.RequestID || !bytes.Equal(got.Payload, want.Payload) {
		t.Fatalf("got %#v, want %#v", got, want)
	}
}

func TestReadFrameRejectsOversizeBeforeAllocation(t *testing.T) {
	wire := make([]byte, headerSize)
	copy(wire, magic[:])
	wire[4] = Version
	binary.BigEndian.PutUint32(wire[12:], MaxPayload+1)
	if _, err := ReadFrame(bytes.NewReader(wire)); err == nil || !strings.Contains(err.Error(), "maximum") {
		t.Fatalf("expected bounded-size error, got %v", err)
	}
}

func TestWriteFrameRejectsOversize(t *testing.T) {
	if err := (Frame{Payload: make([]byte, MaxPayload+1)}).WriteFrame(&bytes.Buffer{}); err == nil {
		t.Fatal("expected oversize payload error")
	}
}

func TestReadFrameRejectsBadHeader(t *testing.T) {
	for _, name := range []string{"magic", "version"} {
		wire := make([]byte, headerSize)
		copy(wire, magic[:])
		wire[4] = Version
		if name == "magic" {
			wire[0] = 'x'
		} else {
			wire[4]++
		}
		if _, err := ReadFrame(bytes.NewReader(wire)); err == nil {
			t.Errorf("%s: expected error", name)
		}
	}
}

func TestIsHelperMode(t *testing.T) {
	if !IsHelperMode([]string{"--afe-helper"}) {
		t.Fatal("expected helper mode")
	}
	if IsHelperMode([]string{"--other"}) {
		t.Fatal("did not expect helper mode")
	}
}
