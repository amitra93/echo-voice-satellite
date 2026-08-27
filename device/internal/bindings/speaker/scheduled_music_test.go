package speaker

import (
	"encoding/binary"
	"math"
	"testing"
)

func pcmSamples(values ...int16) []byte {
	raw := make([]byte, len(values)*2)
	for i, value := range values {
		binary.LittleEndian.PutUint16(raw[i*2:], uint16(value))
	}
	return raw
}

func TestScheduledMusicWaitsForTargetAndRendersAtTarget(t *testing.T) {
	var stream scheduledMusic
	if !stream.start(1) || !stream.push(1, 0, 1_000_000, pcmSamples(10, 20, 30, 40)) {
		t.Fatal("failed to start or queue scheduled stream")
	}
	if got := stream.render(999_000, 1.0, 2); len(got) != 4 || got[0] != 0 || got[1] != 0 {
		t.Fatalf("future audio was not silent: %v", got)
	}
	got := stream.render(1_000_000, 1.0, 4)
	if string(got) != string(pcmSamples(10, 20, 30, 40)) {
		t.Fatalf("rendered PCM = %v", got)
	}
}

func TestScheduledMusicSkipsLatePrefix(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	stream.push(1, 0, 1_000_000, pcmSamples(1, 2, 3, 4, 5, 6, 7, 8))
	got := stream.render(1_000_050, 1.0, 2)
	// At 48kHz, 50us is approximately 2.4 samples. The first rendered sample
	// interpolates around the third source sample, not delayed audio; with a
	// small fraction it rounds back to that sample's value.
	if got[0] != byte(3) || got[1] != 0 {
		t.Fatalf("late prefix was not skipped: %v", got)
	}
	if stream.stats().LateSamples == 0 {
		t.Fatal("late-sample counter did not advance")
	}
}

func TestScheduledMusicInterpolatesBetweenSamples(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	// Two samples 0 and 1000; rendering exactly half a sample past the anchor
	// must land near the midpoint (500), which nearest-neighbour never could.
	stream.push(1, 0, 1_000_000, pcmSamples(0, 1000))
	// 0.5 samples at 48kHz ~= 10.4us. Render one sample at anchor+10us.
	got := stream.render(1_000_010, 1.0, 1)
	v := int16(uint16(got[0]) | uint16(got[1])<<8)
	if v < 400 || v > 600 {
		t.Fatalf("expected interpolated midpoint ~500, got %d", v)
	}
}

func TestScheduledMusicHeldEdgeIsNotAnUnderrun(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	stream.push(1, 0, 1_000_000, pcmSamples(1000))
	// A fractional position beyond the only sample holds it to avoid a click.
	// It is not a zero-filled starvation gap and must not inflate underruns.
	stream.render(1_000_010, 1.0, 1)
	if got := stream.stats().Underruns; got != 0 {
		t.Fatalf("held edge recorded %d underruns, want 0", got)
	}
}

func TestScheduledMusicRetainsHistoryAcrossClockOverlap(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	stream.push(1, 0, 1_000_000, pcmSamples(10, 20))
	stream.push(1, 1, 1_000_042, pcmSamples(30, 40))

	want := pcmSamples(10, 20, 30, 40)
	if got := stream.render(1_000_000, 1.0, 4); string(got) != string(want) {
		t.Fatalf("first render = %v, want %v", got, want)
	}
	if got := stream.render(1_000_000, 1.0, 4); string(got) != string(want) {
		t.Fatalf("overlapping render = %v, want %v", got, want)
	}
	if got := stream.stats().Underruns; got != 0 {
		t.Fatalf("overlapping clock interval recorded %d underruns, want 0", got)
	}
}

func TestScheduledMusicClearAndEndDoNotLeakOldAudio(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	stream.push(1, 0, 1_000, pcmSamples(10, 20))
	if !stream.clear(1) {
		t.Fatal("clear rejected active generation")
	}
	if got := stream.render(1_000, 1.0, 2); got[0] != 0 || got[1] != 0 {
		t.Fatal("clear left old audio queued")
	}
	stream.push(1, 1, 2_000, pcmSamples(30, 40))
	if !stream.end(1) {
		t.Fatal("end rejected active generation")
	}
	stream.render(2_000, 1.0, 2)
	if stream.hasStream() {
		t.Fatal("ended stream remained active after queue drained")
	}
}

func TestScheduledMusicRejectsStaleSequenceTargetAndGeneration(t *testing.T) {
	var stream scheduledMusic
	stream.start(2)
	if stream.start(1) || stream.push(1, 0, 1, pcmSamples(1)) || stream.push(2, 0, 2_000, pcmSamples(1, 2)) == false {
		t.Fatal("unexpected generation or first-frame result")
	}
	if stream.push(2, 0, 2_001, pcmSamples(3, 4)) || stream.push(2, 1, 1_999, pcmSamples(5, 6)) {
		t.Fatal("stale sequence or target regression accepted")
	}
}

// sineSamples is one mono tone at 48kHz, for measuring what the interpolator
// does to the top octave.
func sineSamples(n int, freqHz, amp float64) []byte {
	v := make([]int16, n)
	for i := range v {
		v[i] = int16(amp * math.Sin(2*math.Pi*freqHz*float64(i)/scheduledMusicRate))
	}
	return pcmSamples(v...)
}

func rms(pcm []byte) float64 {
	var sum float64
	n := len(pcm) / 2
	for i := 0; i < n; i++ {
		s := float64(int16(binary.LittleEndian.Uint16(pcm[i*2:])))
		sum += s * s
	}
	return math.Sqrt(sum / float64(n))
}

// The renderer reads at a fractional position, and how it fills in between two
// samples decides what happens to the top octave.
//
// Linear interpolation costs |(1-f) + f·e^(-jω)|, which at half a sample is
// −5.1dB at 15kHz and 0dB at zero offset. The read phase sweeps a full cycle
// whenever the DAC and source rates differ by one part in 48000 — every 2-10
// seconds on this hardware — so linear makes the treble audibly BREATHE on
// that period. This pins the loss at a half-sample offset, which is where the
// worst of it is.
func TestScheduledMusicHoldsTrebleAtAFractionalOffset(t *testing.T) {
	const (
		n       = 4096
		freq    = 15000.0
		amp     = 12000.0
		maxLoss = 0.05 // dB; linear gives 5.1 here and Catmull-Rom 2.5
	)
	src := sineSamples(n, freq, amp)

	var stream scheduledMusic
	stream.start(1)
	stream.push(1, 0, 1_000_000, src)
	// 10us is 0.48 of a sample, near the worst case for interpolation error.
	got := stream.render(1_000_010, 1.0, n-4)

	// Compare against the same span of the source, skipping the ramp-in.
	want := src[8 : (n-4)*2]
	loss := 20 * math.Log10(rms(got[8:])/rms(want))
	if loss < -maxLoss {
		t.Fatalf("interpolation cost %.2fdB at %gHz, want no worse than -%.1fdB",
			loss, freq, maxLoss)
	}
	if loss > 1.0 {
		t.Fatalf("interpolation ADDED %.2fdB at %gHz — the kernel is overshooting",
			loss, freq)
	}
}

// stride is the DAC-versus-source rate ratio. It has to be applied per sample:
// correcting only at period boundaries leaves the read phase sawtoothing
// across every period, which modulates everything playing at the period rate.
func TestScheduledMusicStrideResamples(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	stream.push(1, 0, 1_000_000, pcmSamples(0, 100, 200, 300, 400, 500, 600, 700))

	// Two source samples per output frame reads every other sample.
	got := stream.render(1_000_000, 2.0, 3)
	for i, want := range []int16{0, 200, 400} {
		if v := int16(binary.LittleEndian.Uint16(got[i*2:])); v != want {
			t.Fatalf("sample %d = %d, want %d at stride 2", i, v, want)
		}
	}
}

// A hold is the leading edge of the buffer — a fractional read with nothing to
// interpolate towards. It is deliberately not an underrun, so it needs its own
// counter or it is invisible.
func TestScheduledMusicCountsHoldsNotUnderruns(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	stream.push(1, 0, 1_000_000, pcmSamples(1000))
	stream.render(1_000_010, 1.0, 1)

	st := stream.stats()
	if st.Holds != 1 {
		t.Fatalf("holds = %d, want 1", st.Holds)
	}
	if st.Underruns != 0 {
		t.Fatalf("a held edge recorded %d underruns, want 0", st.Underruns)
	}
}

// The emitted period is scratch owned by the stream. Pinning it here so that a
// future caller retaining one across periods is a test failure rather than an
// intermittent glitch on the device.
func TestScheduledMusicReusesItsOutputBuffer(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	stream.push(1, 0, 1_000_000, pcmSamples(10, 20, 30, 40))
	first := stream.render(1_000_000, 1.0, 2)
	second := stream.render(1_000_000, 1.0, 2)
	if &first[0] != &second[0] {
		t.Fatal("render allocated a fresh period; it must reuse its scratch")
	}
}
