package client

import (
	"encoding/binary"
	"testing"
)

func TestApplyS16GainSaturates(t *testing.T) {
	frame := make([]byte, 6)
	binary.LittleEndian.PutUint16(frame[0:], uint16(int16(1000)))
	binary.LittleEndian.PutUint16(frame[2:], uint16(int16(20000)))
	binary.LittleEndian.PutUint16(frame[4:], uint16(0xb1e0)) // -20000
	if got := applyS16Gain(frame, 2); got != 2 {
		t.Fatalf("clipped = %d, want 2", got)
	}
	got := []int16{
		int16(binary.LittleEndian.Uint16(frame[0:])),
		int16(binary.LittleEndian.Uint16(frame[2:])),
		int16(binary.LittleEndian.Uint16(frame[4:])),
	}
	want := []int16{2000, 32767, -32768}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("sample %d = %d, want %d", i, got[i], want[i])
		}
	}
}
