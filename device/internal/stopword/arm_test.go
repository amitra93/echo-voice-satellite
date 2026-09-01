package stopword

import (
	"errors"
	"testing"
	"time"
)

func TestArmGenerationExpiryAndSingleAcceptance(t *testing.T) {
	var m Manager
	if err := m.Arm("turn-1", 1, "playback", time.Second); err != nil {
		t.Fatal(err)
	}
	if err := m.Arm("turn-2", 1, "thinking", time.Second); !errors.Is(err, ErrInvalidArm) {
		t.Fatalf("same generation = %v, want invalid", err)
	}
	if !m.Disarm(1) {
		t.Fatal("current generation did not disarm")
	}
	if err := m.Arm("turn-2", 2, "thinking", time.Second); err != nil {
		t.Fatal(err)
	}
	if arm, ok := m.Accept(); !ok || arm.TurnID != "turn-2" || arm.Generation != 2 {
		t.Fatalf("Accept() = %#v, %v", arm, ok)
	}
	if _, ok := m.Accept(); ok {
		t.Fatal("duplicate detection was accepted")
	}
	if err := m.Arm("turn-3", 3, "timer", time.Nanosecond); err != nil {
		t.Fatal(err)
	}
	time.Sleep(time.Millisecond)
	if m.Active() {
		t.Fatal("expired arm remained active")
	}
}

func TestArmRejectsInvalidInput(t *testing.T) {
	for _, tc := range []struct {
		turn  string
		gen   uint64
		phase string
		ttl   time.Duration
	}{
		{"", 1, "thinking", time.Second},
		{"turn", 0, "thinking", time.Second},
		{"turn", 1, "listening", time.Second},
		{"turn", 1, "thinking", 0},
	} {
		var m Manager
		if err := m.Arm(tc.turn, tc.gen, tc.phase, tc.ttl); !errors.Is(err, ErrInvalidArm) {
			t.Fatalf("Arm(%#v) = %v, want invalid", tc, err)
		}
	}
}
