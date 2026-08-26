package speaker

import (
	"encoding/binary"
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
	if got := stream.render(999_000, 2); len(got) != 4 || got[0] != 0 || got[1] != 0 {
		t.Fatalf("future audio was not silent: %v", got)
	}
	got := stream.render(1_000_000, 4)
	if string(got) != string(pcmSamples(10, 20, 30, 40)) {
		t.Fatalf("rendered PCM = %v", got)
	}
}

func TestScheduledMusicSkipsLatePrefix(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	stream.push(1, 0, 1_000_000, pcmSamples(1, 2, 3, 4, 5, 6, 7, 8))
	got := stream.render(1_000_050, 2)
	// At 48kHz, 50us is approximately 2.4 samples. The first rendered sample
	// interpolates around the third source sample, not delayed audio; with a
	// small fraction it rounds back to that sample's value.
	if got[0] != byte(3) || got[1] != 0 {
		t.Fatalf("late prefix was not skipped: %v", got)
	}
	stats := stream.stats()
	if stats.LateSamples == 0 || stats.Interpolations == 0 {
		t.Fatal("late-sample / interpolation counters did not advance")
	}
}

func TestScheduledMusicInterpolatesBetweenSamples(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	// Two samples 0 and 1000; rendering exactly half a sample past the anchor
	// must land near the midpoint (500), which nearest-neighbour never could.
	stream.push(1, 0, 1_000_000, pcmSamples(0, 1000))
	// 0.5 samples at 48kHz ~= 10.4us. Render one sample at anchor+10us.
	got := stream.render(1_000_010, 1)
	v := int16(uint16(got[0]) | uint16(got[1])<<8)
	if v < 400 || v > 600 {
		t.Fatalf("expected interpolated midpoint ~500, got %d", v)
	}
}

func TestScheduledMusicClearAndEndDoNotLeakOldAudio(t *testing.T) {
	var stream scheduledMusic
	stream.start(1)
	stream.push(1, 0, 1_000, pcmSamples(10, 20))
	if !stream.clear(1) {
		t.Fatal("clear rejected active generation")
	}
	if got := stream.render(1_000, 2); got[0] != 0 || got[1] != 0 {
		t.Fatal("clear left old audio queued")
	}
	stream.push(1, 1, 2_000, pcmSamples(30, 40))
	if !stream.end(1) {
		t.Fatal("end rejected active generation")
	}
	stream.render(2_000, 2)
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
