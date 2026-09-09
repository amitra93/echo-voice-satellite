package led

import (
	"bytes"
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

func TestMergeLEDs_UpdatesMatchingID(t *testing.T) {
	stored := []led.Led{
		{ID: 0, R: 1, G: 1, B: 1},
		{ID: 1, R: 2, G: 2, B: 2},
		{ID: 2, R: 3, G: 3, B: 3},
	}
	mergeLEDs(stored, led.Led{ID: 1, R: 9, G: 9, B: 9})

	if stored[1] != (led.Led{ID: 1, R: 9, G: 9, B: 9}) {
		t.Fatalf("expected LED 1 updated, got %+v", stored[1])
	}
	// Untouched entries must survive exactly as they were.
	if stored[0] != (led.Led{ID: 0, R: 1, G: 1, B: 1}) {
		t.Fatalf("LED 0 must be untouched, got %+v", stored[0])
	}
	if stored[2] != (led.Led{ID: 2, R: 3, G: 3, B: 3}) {
		t.Fatalf("LED 2 must be untouched, got %+v", stored[2])
	}
}

func TestMergeLEDs_UnknownIDIsIgnored(t *testing.T) {
	stored := []led.Led{
		{ID: 0, R: 1, G: 1, B: 1},
	}
	// Incoming ID 99 doesn't exist in stored — must be a no-op, not appended.
	mergeLEDs(stored, led.Led{ID: 99, R: 9, G: 9, B: 9})

	if len(stored) != 1 {
		t.Fatalf("expected stored length unchanged, got %d", len(stored))
	}
	if stored[0] != (led.Led{ID: 0, R: 1, G: 1, B: 1}) {
		t.Fatalf("expected LED 0 untouched, got %+v", stored[0])
	}
}

func TestMergeLEDs_MultipleIncoming(t *testing.T) {
	stored := []led.Led{
		{ID: 0, R: 1, G: 1, B: 1},
		{ID: 1, R: 2, G: 2, B: 2},
	}
	mergeLEDs(stored,
		led.Led{ID: 1, R: 5, G: 5, B: 5},
		led.Led{ID: 0, R: 6, G: 6, B: 6},
	)

	if stored[0] != (led.Led{ID: 0, R: 6, G: 6, B: 6}) {
		t.Fatalf("expected LED 0 updated, got %+v", stored[0])
	}
	if stored[1] != (led.Led{ID: 1, R: 5, G: 5, B: 5}) {
		t.Fatalf("expected LED 1 updated, got %+v", stored[1])
	}
}

func TestMergeLEDs_NoIncomingIsNoOp(t *testing.T) {
	stored := []led.Led{{ID: 0, R: 1, G: 1, B: 1}}
	mergeLEDs(stored)
	if stored[0] != (led.Led{ID: 0, R: 1, G: 1, B: 1}) {
		t.Fatalf("expected LED untouched with no incoming, got %+v", stored[0])
	}
}

func TestBuildFrame(t *testing.T) {
	leds := []led.Led{
		{ID: 0, R: 0, G: 0, B: 255},
		{ID: 1, R: 255, G: 0, B: 0},
	}
	got := buildFrame(leds)
	// Concatenation of each LED's own BuildArgument, in order — build the
	// expected value the same way rather than hand-writing hex, since
	// duplicating the format here would just be a second copy to drift.
	var expected bytes.Buffer
	for _, l := range leds {
		expected.Write(l.BuildArgument())
	}
	if !bytes.Equal(got, expected.Bytes()) {
		t.Fatalf("buildFrame mismatch: got %q want %q", got, expected.Bytes())
	}
}

func TestBuildFrame_Empty(t *testing.T) {
	got := buildFrame(nil)
	if len(got) != 0 {
		t.Fatalf("expected empty frame for no LEDs, got %q", got)
	}
}

func TestGetNumLEDs(t *testing.T) {
	c := &I2CController{}
	n, err := c.GetNumLEDs()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if n != len(led.Leds) {
		t.Fatalf("expected %d, got %d", len(led.Leds), n)
	}
}
