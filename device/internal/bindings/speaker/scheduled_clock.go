package speaker

import (
	"sync"

	"github.com/wilbowes/EchoMuse/internal/sendspin"
)

// scheduledClock is the presentation clock the scheduled-music renderer reads.
//
// It advances ONE RENDERED PERIOD PER RENDERED PERIOD, at the DAC's measured
// rate. It does not sample a clock at all, and that is the point: the renderer
// maps presentation time onto a read position, so ANY jitter or step in this
// value is samples skipped or replayed — an audible click. Both of the obvious
// implementations have one.
//
//   - Reading the device monotonic clock inherits however late silenceLoop was
//     scheduled. Pump returns when the hardware has room, so its return jitters
//     by milliseconds under load (39-52ms measured for a 42.7ms period), and a
//     read position taken from a clock read jumps by that jitter — hundreds of
//     samples, every period.
//   - Projecting hw_ptr and dividing the accumulated frame count by a live rate
//     estimate rescales the WHOLE history whenever the estimate moves. Measured
//     against this hardware's 47973fps DAC with 1s status samples, that stepped
//     the clock ~0.5ms one second into a track and ~3.2ms five minutes in — it
//     grows with elapsed time, because it multiplies a growing frame count by a
//     changing number. The renderer turned each step into ~150 samples skipped,
//     or, when the step went backwards far enough for the monotonic clamp to
//     hold the clock still, a whole period replayed. That was the once-a-second
//     click, and it survived the renderer's underrun fix precisely because
//     nothing was starving: the buffer held 9-14s throughout.
//
// Counting periods makes both impossible by construction. The hardware paces
// the loop, so one period rendered IS one period of playback time, whenever the
// goroutine happened to wake; and time accumulates in microseconds, so a rate
// refinement changes the NEXT increment and can never move a sample already
// played. hw_ptr observations now serve only to measure the rate.
//
// The DAC's real rate (47973, not the nominal 48000) still matters: it is what
// makes the renderer read 2049.17 source samples per 2048 emitted, which is the
// resampling that keeps the buffer level stable against a source producing
// 48000 samples per second of device time. Being slightly wrong about it is a
// slow drift the renderer's interpolation absorbs inaudibly. Being
// discontinuous about it is a click immediately — hence rate corrections apply
// forward only.
type scheduledClock struct {
	mu    sync.Mutex
	nowUs func() int64
	clock *sendspin.PlaybackClock

	started  bool
	us       float64 // presentation time of the next period to render
	offsetUs float64 // queue latency folded into the anchor, see Advance
	delay    int64   // ALSA frames between the write point and the speaker
	resyncs  uint64
}

// scheduledClockMaxDelayUs bounds the queue latency that will be trusted. The
// buffer is ~5.5s deep but only ever holds a fraction of that, so a reading
// beyond this is a misparse or a stalled substream, and believing one would
// delay the start of playback by that much.
const scheduledClockMaxDelayUs = 400_000.0

// scheduledClockResyncUs bounds how far the counted timeline may drift from the
// device clock before it is re-anchored. Ordinary scheduling jitter is tens of
// milliseconds and rate error is parts per million, so only a genuine break in
// playback — an XRUN, a suspended goroutine, a stopped substream — can reach
// this. When one happens a step IS the right answer: the alternative is a
// timeline that stays wrong for the rest of the track.
const scheduledClockResyncUs = 250_000.0

func newScheduledClock(nowUs func() int64) *scheduledClock {
	return &scheduledClock{
		nowUs: nowUs,
		clock: sendspin.NewPlaybackClock(scheduledMusicRate),
	}
}

// Reset drops the anchor so the next Advance re-takes it from the device clock.
// A stream's timestamps are relative to that clock, so nothing from a previous
// stream may carry over into it.
func (c *scheduledClock) Reset() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.started = false
}

// Advance returns the presentation time of the period about to be rendered and
// moves the clock on by frames of playback. It must be called exactly once per
// rendered period: it is the period count, not the elapsed wall time, that
// defines this timeline.
//
// The anchor includes the ALSA QUEUE LATENCY, because the samples handed over
// now are not audible now — they are audible once the frames already queued
// ahead of them have played, measured at ~7800 frames (163ms) here. Without
// that term the device plays every stream that much ahead of the presentation
// time Music Assistant asked for, which is invisible on one device and wrong
// the moment a second player is meant to be in sync with it. It is safe to add
// only because this timeline is counted: it is a constant taken once at the
// anchor, so it cannot jump the way it would if it were re-derived per period
// from a live queue reading that moves with every write.
func (c *scheduledClock) Advance(frames int64) int64 {
	now := c.nowUs()
	c.mu.Lock()
	defer c.mu.Unlock()

	rate := c.clock.Rate()
	if rate <= 0 {
		rate = scheduledMusicRate
	}
	switch {
	case !c.started:
		c.started = true
		c.offsetUs = float64(c.delay) * 1_000_000.0 / rate
		if c.offsetUs < 0 || c.offsetUs > scheduledClockMaxDelayUs {
			c.offsetUs = 0
		}
		c.us = float64(now) + c.offsetUs
	case c.us-float64(now)-c.offsetUs > scheduledClockResyncUs,
		float64(now)+c.offsetUs-c.us > scheduledClockResyncUs:
		c.resyncs++
		c.us = float64(now) + c.offsetUs
	}

	out := int64(c.us)
	c.us += float64(frames) * 1_000_000.0 / rate
	return out
}

// Stride is how many SOURCE samples pass per output frame.
//
// The source runs at 48000 samples per second of device time while the DAC
// consumes `rate` frames in that same second, so anything other than exactly
// 1.0 here IS the resampling that keeps the two in step. The renderer needs it
// per sample rather than per period: applying the correction only at period
// boundaries leaves the read phase sawtoothing across each period, which is a
// 23Hz modulation of everything playing rather than a slow drift.
func (c *scheduledClock) Stride() float64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	rate := c.clock.Rate()
	if rate <= 0 {
		return 1.0
	}
	return scheduledMusicRate / rate
}

// Observe feeds an ALSA status sample. It refines the measured rate and nothing
// else — the timeline is never re-derived from hw_ptr, because that is what
// used to step it.
func (c *scheduledClock) Observe(status sendspin.PcmStatus) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.clock.Observe(status)
	if status.Running() && status.Delay > 0 {
		c.delay = status.Delay
	}
}

// Resyncs counts re-anchors, i.e. the discontinuities this clock could not
// avoid. It should stay at zero across a track.
func (c *scheduledClock) Resyncs() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.resyncs
}

// Rate is the measured DAC rate backing the timeline, for telemetry.
func (c *scheduledClock) Rate() float64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.clock.Rate()
}
