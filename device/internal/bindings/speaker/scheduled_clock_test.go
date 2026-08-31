package speaker

import (
	"testing"

	"github.com/wilbowes/EchoMuse/internal/sendspin"
)

// The first rendered period is presented at the device clock: a stream's
// timestamps are relative to it, so the timeline has to start there.
func TestScheduledClockAnchorsOnTheDeviceClock(t *testing.T) {
	now := int64(1_000_000)
	c := newScheduledClock(func() int64 { return now })
	if got := c.Advance(2048); got != now {
		t.Fatalf("first Advance = %d, want the device clock %d", got, now)
	}
}

// The clock must not inherit the scheduler's jitter. Pump returns when the
// hardware has room, which under load has been measured varying from 39 to
// 52ms for a 42.7ms period; a timeline that read a clock would move the read
// position by that difference — hundreds of samples — every period.
func TestScheduledClockIgnoresWhenTheRenderLoopHappensToRun(t *testing.T) {
	now := int64(1_000_000)
	c := newScheduledClock(func() int64 { return now })

	var prev int64
	for i, wobble := range []int64{42_666, 52_000, 33_000, 47_000, 39_000, 42_666} {
		got := c.Advance(2048)
		if i > 0 {
			// 2048 frames at the nominal rate is 42666.67us, so consecutive
			// integer readings alternate by a microsecond — the accumulator
			// keeps the fraction rather than losing it every period.
			if delta := got - prev; delta < 42_666 || delta > 42_667 {
				t.Fatalf("period %d advanced %dus, want ~42666us regardless of "+
					"a %dus wake-up", i, delta, wobble)
			}
		}
		prev = got
		now += wobble
	}
}

// A rate refinement may change how fast the clock runs from here on. It must
// never move the timeline itself: the samples behind it have already been
// played, so a step is audible immediately, while a slope error is a drift the
// renderer's interpolation absorbs.
//
// The old projection did exactly this — it divided the whole accumulated frame
// count by the live estimate — and stepped ~0.5ms one second into a track,
// growing to ~3.2ms five minutes in.
func TestScheduledClockDoesNotStepWhenTheRateEstimateIsRefined(t *testing.T) {
	const (
		realRate    = 47973.0       // measured DAC rate on this hardware
		periodUs    = int64(42_666) // one ALSA period at 2048 frames
		observeUs   = int64(1_024_000)
		trackUs     = int64(300_000_000) // a five-minute track
		toleranceUs = int64(60)          // ~3 samples
	)

	now := int64(5_000_000)
	c := newScheduledClock(func() int64 { return now })

	start, hw := now, int64(1_000_000)
	tstampNs := int64(9_000_000_000)
	// A couple of frames of hw_ptr/tstamp read skew, which is what makes the
	// rate estimate wander in the first place.
	jitter := []int64{0, 2, -1, 1, -2, 3, -3, 0}
	step := 0
	observe := func() {
		elapsed := now - start
		c.Observe(sendspin.PcmStatus{
			State:    "RUNNING",
			HwPtr:    hw + int64(realRate*float64(elapsed)/1e6) + jitter[step%len(jitter)],
			Delay:    6_000,
			TstampNs: tstampNs + elapsed*1_000,
		})
		step++
	}
	observe()

	prev, sinceObserve, worst := c.Advance(2048), int64(0), int64(0)
	for elapsed := int64(0); elapsed < trackUs; elapsed += periodUs {
		now += periodUs
		if sinceObserve += periodUs; sinceObserve >= observeUs {
			sinceObserve = 0
			observe()
		}
		got := c.Advance(2048)
		off := (got - prev) - periodUs
		if off < 0 {
			off = -off
		}
		if off > worst {
			worst = off
		}
		prev = got
	}
	if worst > toleranceUs {
		t.Fatalf("timeline stepped %dus (%.0f samples) across a rate update; want <= %dus",
			worst, float64(worst)*48000/1e6, toleranceUs)
	}
	if got := c.Resyncs(); got != 0 {
		t.Fatalf("re-anchored %d times across an uninterrupted track, want 0", got)
	}
}

// A counted timeline cannot notice that playback actually stopped, so it needs
// one escape hatch: a break far larger than any jitter or rate error re-anchors
// rather than leaving the timeline permanently wrong.
func TestScheduledClockReanchorsAfterAPlaybackBreak(t *testing.T) {
	now := int64(1_000_000)
	c := newScheduledClock(func() int64 { return now })
	c.Advance(2048)

	now += int64(scheduledClockResyncUs) * 4 // the loop was stalled
	got := c.Advance(2048)
	if got != now {
		t.Fatalf("after a stall Advance = %d, want a re-anchor to %d", got, now)
	}
	if c.Resyncs() != 1 {
		t.Fatalf("resyncs = %d, want 1", c.Resyncs())
	}
}

// Starting a stream drops the previous stream's timeline.
func TestScheduledClockResetRetakesTheAnchor(t *testing.T) {
	now := int64(1_000_000)
	c := newScheduledClock(func() int64 { return now })
	c.Advance(2048)

	now += 5_000_000
	c.Reset()
	if got := c.Advance(2048); got != now {
		t.Fatalf("after Reset Advance = %d, want %d", got, now)
	}
}
