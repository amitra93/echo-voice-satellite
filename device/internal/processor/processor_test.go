package processor

import (
	"encoding/binary"
	"math"
	"testing"
)

// s16le encodes a slice of normalised (-1..1) float samples as S16_LE bytes,
// mirroring the wire format Process operates on.
func s16le(samples []float64) []byte {
	out := make([]byte, len(samples)*2)
	for i, s := range samples {
		v := int16(s * 32767)
		binary.LittleEndian.PutUint16(out[i*2:], uint16(v))
	}
	return out
}

// constBuf returns n samples all at the given amplitude — not real audio,
// but the AGC only cares about RMS, and a constant buffer has a
// closed-form RMS (== amplitude), which is what makes the exact gain
// deltas below predictable rather than approximate.
func constBuf(n int, amplitude float64) []byte {
	s := make([]float64, n)
	for i := range s {
		s[i] = amplitude
	}
	return s16le(s)
}

func TestNewInitialGainIsUnity(t *testing.T) {
	p := New()
	if p.agcGain != 1.0 {
		t.Fatalf("New() gain = %v, want 1.0", p.agcGain)
	}
}

func TestResetAGCReturnsToUnity(t *testing.T) {
	p := New()
	p.agcGain = 5.0
	p.ResetAGC()
	if p.agcGain != 1.0 {
		t.Fatalf("ResetAGC() gain = %v, want 1.0", p.agcGain)
	}
}

func TestDestroyIsNoop(t *testing.T) {
	p := New()
	// Must not panic — the lifecycle contract survived the RNNoise removal
	// as a no-op, per the doc comment.
	p.Destroy()
}

func TestProcessEmptyInputReturnsUnchanged(t *testing.T) {
	p := New()
	out := p.Process(nil, true, true)
	if len(out) != 0 {
		t.Fatalf("Process(nil) returned %d bytes, want 0", len(out))
	}
}

func TestProcessAGCDisabledIsPassthrough(t *testing.T) {
	p := New()
	in := constBuf(64, 0.5)
	before := p.agcGain
	out := p.Process(in, false, true)
	if len(out) != len(in) {
		t.Fatalf("passthrough changed length: got %d, want %d", len(out), len(in))
	}
	for i := range in {
		if out[i] != in[i] {
			t.Fatalf("passthrough changed byte %d: got %d, want %d", i, out[i], in[i])
		}
	}
	if p.agcGain != before {
		t.Fatalf("agcGain moved on a disabled call: got %v, want unchanged %v", p.agcGain, before)
	}
}

func TestAGCAttackReducesGainOnLoudInput(t *testing.T) {
	p := New() // agcGain starts at 1.0
	// amplitude 0.5 -> rms 0.5 -> target = agcTargetRMS(0.08)/0.5 = 0.16,
	// which is below the starting gain, so the attack branch fires
	// (attack is unconditional — it doesn't need `speech`, unlike release).
	in := constBuf(160, 0.5)
	p.Process(in, true, false)

	wantTarget := agcTargetRMS / 0.5
	wantGain := 1.0 + agcAttack*(wantTarget-1.0)
	if math.Abs(p.agcGain-wantGain) > 1e-3 {
		t.Fatalf("gain after one attack step = %v, want %v", p.agcGain, wantGain)
	}
	if p.agcGain >= 1.0 {
		t.Fatalf("attack should reduce gain on loud input, got %v (was 1.0)", p.agcGain)
	}
}

func TestAGCReleaseRaisesGainOnQuietSpeech(t *testing.T) {
	p := New()
	p.agcGain = 0.5
	// A quiet signal (rms well below target) makes target > gain, which
	// only moves the gain when speech is true (release is speech-gated).
	in := constBuf(160, 0.01)
	p.Process(in, true, true)

	rms := 0.01
	target := agcTargetRMS / rms
	wantGain := 0.5 + agcRelease*(target-0.5)
	if math.Abs(p.agcGain-wantGain) > 1e-3 {
		t.Fatalf("gain after one release step = %v, want %v", p.agcGain, wantGain)
	}
	if p.agcGain <= 0.5 {
		t.Fatalf("release should raise gain on quiet speech, got %v (was 0.5)", p.agcGain)
	}
}

// This is the property the whole `speech` parameter exists for: release
// must be frozen during silence, or the noise floor gets amplified. See
// the package doc comment.
func TestAGCReleaseFrozenDuringSilence(t *testing.T) {
	p := New() // gain 1.0
	in := constBuf(160, 0.01)
	p.Process(in, true, false) // speech=false, quiet input -> target > gain
	if p.agcGain != 1.0 {
		t.Fatalf("gain moved during silence: got %v, want unchanged 1.0", p.agcGain)
	}
}

func TestAGCGainClampsToMax(t *testing.T) {
	p := New()
	p.agcGain = agcMaxGain + 5 // out of range on entry
	in := constBuf(160, 0.5)   // target(0.16) < gain, attack fires but can't recover in one step
	p.Process(in, true, false)
	if p.agcGain != agcMaxGain {
		t.Fatalf("gain not clamped to max: got %v, want %v", p.agcGain, agcMaxGain)
	}
}

func TestAGCGainClampsToMin(t *testing.T) {
	p := New()
	p.agcGain = agcMinGain - 0.4 // out of range on entry
	in := constBuf(160, 0.01)    // quiet + speech -> release nudges up, but not enough to clear the floor in one step
	p.Process(in, true, true)
	if p.agcGain != agcMinGain {
		t.Fatalf("gain not clamped to min: got %v, want %v", p.agcGain, agcMinGain)
	}
}

func TestAGCSilentInputLeavesGainUnchanged(t *testing.T) {
	// All-zero samples have rms == 0, below the 1e-6 floor that guards the
	// target computation, so the gain update is skipped entirely (not
	// updated toward some huge target from a divide-by-near-zero).
	p := New()
	in := constBuf(160, 0.0)
	p.Process(in, true, true)
	if p.agcGain != 1.0 {
		t.Fatalf("gain moved on silent input: got %v, want unchanged 1.0", p.agcGain)
	}
}

func TestProcessOutputClipsToFullScale(t *testing.T) {
	p := New()
	p.agcGain = agcMaxGain // 20x — any input above ~0.05 amplitude will clip
	in := constBuf(4, 0.9)
	out := p.Process(in, true, false)

	for i := 0; i < len(out); i += 2 {
		v := int16(binary.LittleEndian.Uint16(out[i:]))
		if v != 32767 {
			t.Fatalf("sample %d = %d, want clipped to 32767", i/2, v)
		}
	}
}

func TestProcessOutputClipsToFullScaleNegative(t *testing.T) {
	p := New()
	p.agcGain = agcMaxGain
	in := constBuf(4, -0.9)
	out := p.Process(in, true, false)

	for i := 0; i < len(out); i += 2 {
		v := int16(binary.LittleEndian.Uint16(out[i:]))
		if v != -32767 {
			t.Fatalf("sample %d = %d, want clipped to -32767", i/2, v)
		}
	}
}

func TestProcessPreservesLength(t *testing.T) {
	p := New()
	in := constBuf(37, 0.3) // odd sample count, even byte count
	out := p.Process(in, true, true)
	if len(out) != len(in) {
		t.Fatalf("Process changed byte length: got %d, want %d", len(out), len(in))
	}
}
