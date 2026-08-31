package beamformer

import (
	"encoding/binary"
	"math"
	"testing"
)

// warmBeamformer returns a Beamformer with baseline warmed up and a uniform
// noise floor, as if it had been running in a quiet room.
func warmBeamformer(baseline float64) *Beamformer {
	b := New()
	b.baselineReady = 100
	for di := 0; di < nDirections; di++ {
		b.energyBaseline[di] = baseline
	}
	return b
}

// TestLockBackPicksPastBurst is the scenario that motivated lock-back:
// the wake word was spoken ~1s ago from direction 2, the fast smoother has
// since decayed and (thanks to a TV) now points at direction 5. Live onset
// selection picks the TV; lock-back must pick the speaker.
func TestLockBackPicksPastBurst(t *testing.T) {
	b := warmBeamformer(1e-6)

	// Fill the ring with baseline-level noise…
	for i := 0; i < historyPeriods; i++ {
		for di := 0; di < nDirections; di++ {
			b.energyHistory[i][di] = 1e-6
		}
	}
	b.historyCount = historyPeriods

	// …with a wake-word burst on direction 2, ~10 periods long, in the
	// middle of the window (well before "now").
	for i := 20; i < 30; i++ {
		b.energyHistory[i][2] = 5e-4
	}

	// TV on direction 5: elevated steady energy in both the ring and the
	// live smoother — loud in absolute terms, but not a burst relative to
	// its own baseline.
	b.energyBaseline[5] = 4e-4
	for i := 0; i < historyPeriods; i++ {
		b.energyHistory[i][5] = 5e-4
	}
	b.energySmooth[5] = 5e-4 // live smoother points at the TV
	b.energySmooth[2] = 2e-6 // speaker's onset has decayed

	b.Lock(true)

	if b.lockedChannel != directionToChannel[2] {
		t.Fatalf("lock-back picked ch%d, want ch%d (direction 2 burst)",
			b.lockedChannel, directionToChannel[2])
	}
}

// TestLockFallsBackToOnsetRatioWithoutHistory — fresh start: baseline warm
// (carried into the ready state quickly) but ring not yet populated. Must
// use the live onset ratio, not a zero-filled ring.
func TestLockFallsBackToOnsetRatioWithoutHistory(t *testing.T) {
	b := warmBeamformer(1e-6)
	b.historyCount = 0
	b.energySmooth[4] = 3e-4 // live onset on direction 4

	b.Lock(true)

	if b.lockedChannel != directionToChannel[4] {
		t.Fatalf("fallback picked ch%d, want ch%d (live onset direction 4)",
			b.lockedChannel, directionToChannel[4])
	}
}

// TestLockDisabledIsNoOp — beamforming off must leave the channel unlocked
// (ch6 omni output path).
func TestLockDisabledIsNoOp(t *testing.T) {
	b := warmBeamformer(1e-6)
	b.historyCount = historyPeriods
	b.energyHistory[0][3] = 1.0

	b.Lock(false)

	if b.lockedChannel != -1 {
		t.Fatalf("Lock(false) locked to ch%d, want unlocked (-1)", b.lockedChannel)
	}
}

// TestBurstRatioTopNMean checks the allocation-free partial selection:
// history 1..64 on direction 0 → top 8 are 57..64, mean 60.5.
func TestBurstRatioTopNMean(t *testing.T) {
	b := warmBeamformer(1.0)
	for i := 0; i < historyPeriods; i++ {
		b.energyHistory[i][0] = float64(i + 1)
	}
	b.historyCount = historyPeriods

	got := b.burstRatio(0)
	want := 60.5 // mean of 57..64, baseline 1.0
	if got != want {
		t.Fatalf("burstRatio = %v, want %v", got, want)
	}
}

// TestBurstRatioPartialHistory — fewer samples than burstTopN averages what
// exists instead of diluting with zeros.
func TestBurstRatioPartialHistory(t *testing.T) {
	b := warmBeamformer(1.0)
	b.energyHistory[0][0] = 4.0
	b.energyHistory[1][0] = 2.0
	b.historyCount = 2

	got := b.burstRatio(0)
	want := 3.0
	if got != want {
		t.Fatalf("burstRatio = %v, want %v", got, want)
	}
}

func TestOnsetRatioUsesBaselineAndFallsBackWhenUninitialised(t *testing.T) {
	b := New()
	b.energySmooth[0] = 4
	if got := b.onsetRatio(0); got != 4 {
		t.Fatalf("zero-baseline onsetRatio = %v, want 4", got)
	}
	b.energyBaseline[0] = 2
	if got := b.onsetRatio(0); got != 2 {
		t.Fatalf("onsetRatio = %v, want 2", got)
	}
}

func TestBurstRatioFallsBackToBurstWhenBaselineIsZero(t *testing.T) {
	b := New()
	b.energyHistory[0][0] = 4
	b.historyCount = 1
	if got := b.burstRatio(0); got != 4 {
		t.Fatalf("zero-baseline burstRatio = %v, want 4", got)
	}
}

func TestUnlockAndRepeatedLock(t *testing.T) {
	b := warmBeamformer(1)
	b.energySmooth[2] = 5
	b.Lock(true)
	selected := b.lockedChannel
	b.Lock(true)
	if b.lockedChannel != selected {
		t.Fatalf("repeated Lock changed channel from %d to %d", selected, b.lockedChannel)
	}
	b.Unlock()
	if b.lockedChannel != -1 {
		t.Fatalf("Unlock left channel %d locked", b.lockedChannel)
	}
	b.Unlock() // idempotent while already unlocked
}

func TestDecodeS24SampleHandlesPositiveAndNegativeValues(t *testing.T) {
	if got := decodeS24Sample(0x00, 0x00, 0x00); got != 0 {
		t.Fatalf("zero sample = %v, want 0", got)
	}
	if got := decodeS24Sample(0xff, 0xff, 0x7f); math.Abs(float64(got-0.9999999)) > 1e-5 {
		t.Fatalf("positive full-scale sample = %v", got)
	}
	if got := decodeS24Sample(0x00, 0x00, 0x80); math.Abs(float64(got+1)) > 1e-6 {
		t.Fatalf("negative full-scale sample = %v, want -1", got)
	}
}

func TestBandDiffAndHFEnergy(t *testing.T) {
	b := New()
	b.chanBuf[0][0] = 1
	b.chanBuf[0][1] = 2
	b.chanBuf[0][2] = 5
	b.bandDiff()
	if b.hfBuf[0][0] != 0 || b.hfBuf[0][1] != 0 || b.hfBuf[0][2] != 2 {
		t.Fatalf("bandDiff prefix/stride output = %v, %v, %v", b.hfBuf[0][0], b.hfBuf[0][1], b.hfBuf[0][2])
	}
	var channels [6][]float32
	for i := range channels {
		channels[i] = []float32{0, 1, 2}
	}
	if got := hfEnergy(channels, 0); got != 5.0/3.0 {
		t.Fatalf("hfEnergy = %v, want %v", got, 5.0/3.0)
	}
}

func TestNearestDirectionWrapsAtZero(t *testing.T) {
	if got := nearestDirection(359); got != 0 {
		t.Fatalf("nearestDirection(359) = %d, want 0", got)
	}
	if got := nearestDirection(45); got != 1 {
		t.Fatalf("nearestDirection(45) = %d, want 1", got)
	}
	if got := angleDiff(10, 350); got != 20 {
		t.Fatalf("angleDiff(10, 350) = %v, want 20", got)
	}
	if got := angleDiff(350, 10); got != -20 {
		t.Fatalf("angleDiff(350, 10) = %v, want -20", got)
	}
	if got := CandidateAngles(); got != candidateAngles {
		t.Fatalf("CandidateAngles() = %v, want %v", got, candidateAngles)
	}
}

func putS24(raw []byte, frame, channel int, value int32) {
	offset := frame*frameSize + channel*byteSample
	if value < 0 {
		value += 1 << 24
	}
	raw[offset] = byte(value)
	raw[offset+1] = byte(value >> 8)
	raw[offset+2] = byte(value >> 16)
}

func TestExtractChannelAppliesGainAndCountsClipping(t *testing.T) {
	b := New()
	raw := make([]byte, frameSize*2)
	putS24(raw, 0, 0, 1<<20)
	putS24(raw, 1, 0, -(1 << 23))

	out := b.extractChannel(raw, 0, 2)
	if got := int16(binary.LittleEndian.Uint16(out[0:2])); got != 8192 {
		t.Fatalf("positive extracted sample = %d, want 8192", got)
	}
	if got := int16(binary.LittleEndian.Uint16(out[2:4])); got != -32768 {
		t.Fatalf("negative extracted sample = %d, want -32768", got)
	}
	if b.ClippedSamples() != 1 {
		t.Fatalf("ClippedSamples() = %d, want 1", b.ClippedSamples())
	}
}

func TestProcessShortInputUsesCentreChannel(t *testing.T) {
	b := New()
	raw := make([]byte, frameSize*2)
	putS24(raw, 0, centreCh, 1<<20)
	putS24(raw, 1, centreCh, -(1 << 20))

	mono, angle := b.Process(raw, -1, 1)
	if angle != -1 || len(mono) != 4 {
		t.Fatalf("short Process() returned len=%d angle=%v", len(mono), angle)
	}
	if got := int16(binary.LittleEndian.Uint16(mono[0:2])); got != 4096 {
		t.Fatalf("centre sample = %d, want 4096", got)
	}
}

func TestProcessFullPeriodUnlockedAndLockedModes(t *testing.T) {
	raw := make([]byte, periodFrames*frameSize)
	putS24(raw, 0, 1, 1<<20)

	b := New()
	mono, angle := b.Process(raw, -1, 1)
	if angle != -1 || len(mono) != periodFrames*2 || b.historyCount != 1 {
		t.Fatalf("unlocked Process() len=%d angle=%v history=%d", len(mono), angle, b.historyCount)
	}

	b.lockedChannel = 2
	_, angle = b.Process(raw, -1, 1)
	if angle != candidateAngles[1] {
		t.Fatalf("auto locked angle = %v, want %v", angle, candidateAngles[1])
	}
	_, angle = b.Process(raw, 31, 1)
	if angle != candidateAngles[1] {
		t.Fatalf("fixed locked angle = %v, want %v", angle, candidateAngles[1])
	}
}
