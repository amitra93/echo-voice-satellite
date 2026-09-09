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

// failAfterNWriter succeeds its first n Write calls (returning the full
// byte count each time, as a well-behaved writer would) and fails or
// misbehaves from then on — used to reach WriteFrame's separate header vs
// payload write-error branches, and writeAll's defence against a writer
// that reports writing 0 bytes with no error.
type failAfterNWriter struct {
	n       int
	shortOK bool // if true, "fail" means return (0, nil) instead of an error
}

func (w *failAfterNWriter) Write(p []byte) (int, error) {
	if w.n > 0 {
		w.n--
		return len(p), nil
	}
	if w.shortOK {
		return 0, nil
	}
	return 0, errWriteFailed
}

var errWriteFailed = &writeFailedError{}

type writeFailedError struct{}

func (*writeFailedError) Error() string { return "synthetic write failure" }

func TestWriteFrameSurfacesAPayloadWriteError(t *testing.T) {
	// The header write (1 call) succeeds; the payload write (2nd call) fails.
	w := &failAfterNWriter{n: 1}
	f := Frame{Type: WritePlayer, RequestID: 1, Payload: []byte{1, 2, 3}}
	if err := f.WriteFrame(w); err == nil || !strings.Contains(err.Error(), "write payload") {
		t.Fatalf("WriteFrame() = %v, want a payload write error", err)
	}
}

func TestWriteAllRejectsAMisbehavingZeroLengthWrite(t *testing.T) {
	w := &failAfterNWriter{n: 0, shortOK: true}
	if err := writeAll(w, []byte{1, 2, 3}); err == nil {
		t.Fatal("writeAll accepted a writer reporting 0 bytes written with no error")
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
