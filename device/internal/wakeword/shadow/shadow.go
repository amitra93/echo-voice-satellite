// Package shadow scores the wake stream on the device WITHOUT acting on it, so
// on-device detection can be compared against the controller's on the same
// audio before anything depends on it.
//
// The one hard rule: a shadow feature must never be able to damage the real
// audio path. Inference costs ~31ms per 80ms frame on an Echo Dot, and the mic
// goroutine reads 160ms ALSA batches into a ring only 160ms deep — running two
// frames of inference inline would spend 62ms of that budget and risk the
// capture stalls that lose whole batches. So Push only hands the samples to a
// buffered channel and returns; a separate goroutine does the work, and when it
// falls behind, frames are DROPPED AND COUNTED rather than blocking the caller.
// A shadow run that drops frames tells you something useful. One that stutters
// the microphone tells you nothing and costs a working device.
//
// Scoring is otherwise identical to the controller's: the same models, the same
// streaming buffers, the same threshold. Nothing HERE triggers a turn, touches
// the LEDs, or sends audio — it counts, and it reports crossings. Whether a
// crossing goes on to start a turn is owwOnDevice's business and is decided by
// the callback, which is why this package kept its name and its rule after
// on-device triggering shipped: the scorer is the same instrument either way,
// and it must stay unable to damage the audio path in both.
package shadow

import (
	"io"
	"log"
	"sync"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
)

// queueFrames is how much audio may be in flight to the scorer goroutine.
// 8 frames is 640ms: enough to ride out a scheduling hiccup or a slow frame,
// short enough that a persistently overloaded device drops promptly instead of
// scoring audio from a second ago and reporting a crossing that has no
// plausible relationship to what was said.
const queueFrames = 8

// DefaultRefractory suppresses repeat crossings after one fires. Wake scores
// stay above threshold for several consecutive frames, so without this a single
// utterance emits a handful of crossings — noise in the logs, and a correlation
// window that matches whichever one happens to be nearest.
const DefaultRefractory = 1500 * time.Millisecond

// Stats is a snapshot of what the scorer has seen. Counters are cumulative
// since the last Drain.
type Stats struct {
	// Frames scored, and Drops that never reached the scorer because it was
	// behind. Drops are the health metric: a nonzero rate means the device
	// cannot keep up and the comparison is running on a subset of the audio.
	Frames uint64
	Drops  uint64
	// NotReady counts frames pushed before the detector had 16 embeddings
	// (~1.28s after a reset). Expected to be small and nonzero.
	NotReady uint64
	// Crossings is how many times the score reached the threshold, after the
	// refractory period is applied.
	Crossings uint64
	// MaxScore is the highest score seen. This is the number that says whether
	// a device is nearly detecting or nowhere close.
	MaxScore float32
	// Threshold is the crossing threshold in force at the end of the window.
	// Reported because the controller cannot judge a non-crossing without it:
	// during playback the controller lowers its own bar to the barge-in
	// threshold, and a device scoring against the normal one was never asked
	// the same question. Without this the comparison records every barge-in as
	// an on-device miss.
	Threshold float32
	// Errors counts inference failures, with the most recent message. An
	// inference error is not fatal here: the scorer keeps going, because
	// losing shadow data is always preferable to losing the audio path.
	Errors  uint64
	LastErr string
	// MaxInferMs and MaxGapMs exist to explain DROPS, which is the one number
	// here nobody has been able to account for: 58 in 925k frames, in discrete
	// steps, on an idle device, with turn bursts / core hotplug / controller
	// redeploys all ruled out by measurement.
	//
	// A drop means the 8-frame (640ms) queue was full, which has exactly two
	// causes and they call for opposite fixes. MaxInferMs is the slowest single
	// score in the window — the CONSUMER stalling. MaxGapMs is the longest gap
	// between frames arriving — the PRODUCER bursting, i.e. the mic pipeline
	// handing over 160ms batches late and in clumps. Without separating them,
	// "the scorer fell behind" is a description rather than a diagnosis.
	//
	// Both are maxima, not averages: a stall is by definition the tail, and an
	// average over a 30s window buries a 700ms event under 375 normal frames.
	MaxInferMs uint32
	MaxGapMs   uint32
}

// Scorer runs a Detector over pushed audio on its own goroutine.
type Scorer struct {
	det       *wakeword.Detector
	threshold float32
	// bargeThreshold is used instead of threshold while the speaker is
	// streaming, mirroring the controller's own effective threshold. Zero
	// disables the behaviour, so a controller that never sends one leaves the
	// scorer exactly as it was.
	bargeThreshold float32
	speakerActive  func() bool
	refract        time.Duration
	onCross        func(score, threshold float32, at time.Time, sequence uint16)
	onScore        func(ScoreEvent)
	head           []Head

	ch   chan scoredFrame
	done chan struct{}
	stop sync.Once
	// quit ends the scorer goroutine. Close must NOT close `ch`: the mic
	// goroutine captures the scorer pointer once per stream (see
	// DataClient.startMicStream) and keeps pushing to it, so closing the
	// channel it sends on panics the process on the next 80ms frame.
	quit chan struct{}
	// closed makes enqueue a no-op after Close. Not required for safety once
	// `ch` is never closed — a send into a buffered channel nobody reads is
	// harmless — but it stops a dead scorer silently accruing drop counts.
	closed atomic.Bool

	// closer releases the inference engine, when one owns resources (the ORT
	// sessions). Nil for an injected Inferer, e.g. in tests.
	closers []io.Closer
	// info describes the loaded engine for logging: runtime version, model,
	// whether XNNPACK attached. Set once at construction, never mutated.
	info string

	// Maxima are atomics rather than mutex-guarded state because they are
	// written on the two hot paths: the mic goroutine (gap) and the scorer
	// goroutine (inference). Push must stay lock-free — it runs on the mic
	// path, which has a hard 160ms deadline and a ring only 160ms deep.
	maxInferMs atomic.Int64
	maxGapMs   atomic.Int64
	// lastPush is touched ONLY by the producer. Push is called from the mic
	// goroutine alone, so this needs no synchronisation of its own; putting it
	// under the mutex would add contention to the path this design exists to
	// keep clear.
	lastPush time.Time

	mu        sync.Mutex
	stats     Stats
	ready     bool
	lastCross time.Time
	// resetReq is consumed by the scorer goroutine rather than acted on by
	// the caller, because Detector is not safe for concurrent use and the
	// caller is a different goroutine.
	resetReq bool
}

type ScoreEvent struct {
	Score          float32
	Threshold      float32
	At             time.Time
	Sequence       uint16
	Crossed        bool
	BargeThreshold bool
}

// Head is an optional classifier fed from Scorer's Detector. Its classifier
// sees the exact same 16x96 embedding window as the primary wake head; Enabled
// is checked on the worker, so an unarmed stop head incurs no classifier cost.
type Head struct {
	Classifier wakeword.Classifier
	Threshold  float32
	Enabled    func() bool
	OnCross    func(score, threshold float32, at time.Time, sequence uint16)

	lastCross time.Time
}

// scoredFrame keeps the data-plane position associated with the audio handed
// to inference. The scorer runs asynchronously, so time.Now at a crossing is
// not a reliable substitute for this sequence number.
type scoredFrame struct {
	pcm      []int16
	sequence uint16
}

// NewScorer starts a scorer. onCross is called from the scorer goroutine when
// the score reaches threshold, so it must not block — the controller-bound
// send it wraps is buffered for that reason.
//
// It receives the threshold actually crossed, which during playback is the
// lower barge-in bar. Reported from here rather than re-derived by the caller
// because this is the only place that knows: the bar depends on whether the
// speaker was streaming at the instant the frame was SCORED, and by the time a
// callback asks, playback may have ended.
func NewScorer(inf wakeword.Inferer, threshold float32, onCross func(score, threshold float32, at time.Time, sequence uint16)) *Scorer {
	return NewScorerWithHeads(inf, threshold, onCross)
}

// NewScorerWithHeads creates one non-blocking worker and one shared feature
// pipeline. Every optional head is classified against the same embeddings.
func NewScorerWithHeads(inf wakeword.Inferer, threshold float32, onCross func(score, threshold float32, at time.Time, sequence uint16), heads ...Head) *Scorer {
	s := &Scorer{
		det:       wakeword.New(inf),
		threshold: threshold,
		refract:   DefaultRefractory,
		onCross:   onCross,
		head:      heads,
		ch:        make(chan scoredFrame, queueFrames),
		done:      make(chan struct{}),
		quit:      make(chan struct{}),
	}
	go s.run()
	return s
}

// SetThreshold updates the crossing threshold, which the controller can change
// at any time via a config push.
func (s *Scorer) SetThreshold(t float32) {
	s.mu.Lock()
	s.threshold = t
	s.mu.Unlock()
}

// SetBargeThreshold sets the threshold used while the speaker is streaming, and
// the predicate for "is it streaming". Pass 0 (or a nil predicate) to score
// against the normal threshold at all times.
//
// This mirrors the controller: echo at the mic is ~25dB louder than the person,
// so speech-over-TTS scores are depressed and the controller drops its bar to
// bargeInThreshold during playback. A device that did not do the same would
// disagree with the controller on every barge-in — and did, which is how this
// was found.
func (s *Scorer) SetBargeThreshold(t float32, active func() bool) {
	s.mu.Lock()
	s.bargeThreshold = t
	s.speakerActive = active
	s.mu.Unlock()
}

// SetScoreCallback observes every trusted primary score on the scorer worker.
// The callback must not block; capture selection only copies bounded RAM.
func (s *Scorer) SetScoreCallback(callback func(ScoreEvent)) {
	s.mu.Lock()
	s.onScore = callback
	s.mu.Unlock()
}

// effectiveThreshold picks the bar currently in force. Caller holds mu.
func (s *Scorer) effectiveThresholdLocked() float32 {
	if s.bargeThreshold > 0 && s.speakerActive != nil && s.speakerActive() &&
		s.bargeThreshold < s.threshold {
		return s.bargeThreshold
	}
	return s.threshold
}

// Push queues one chunk of 16kHz mono PCM. It never blocks and never fails:
// if the scorer is behind, the frame is dropped and counted.
//
// The samples are COPIED, because the mic pipeline reuses its buffers between
// periods. One 2.5KB allocation per 80ms is immaterial next to the inference
// it feeds, and the alternative — a buffer pool shared across goroutines — is
// a lifetime bug waiting to happen on the audio path.
func (s *Scorer) Push(samples []int16) {
	cp := make([]int16, len(samples))
	copy(cp, samples)
	s.enqueue(scoredFrame{pcm: cp})
}

// PushBytes is Push for little-endian S16 bytes, which is what the mic
// pipeline carries. An odd trailing byte cannot happen (frames are whole
// samples) and is dropped rather than silently shifting every sample after it.
func (s *Scorer) PushBytes(b []byte) {
	s.PushBytesSequence(b, 0)
}

// PushBytesSequence is PushBytes with the original data-plane PCM sequence.
// It is the sequence emitted with a crossing so the controller can rewind its
// own short PCM ring to the exact frame where device inference crossed.
func (s *Scorer) PushBytesSequence(b []byte, sequence uint16) {
	n := len(b) / 2
	if n == 0 {
		return
	}
	cp := make([]int16, n)
	for i := 0; i < n; i++ {
		cp[i] = int16(uint16(b[2*i]) | uint16(b[2*i+1])<<8)
	}
	s.enqueue(scoredFrame{pcm: cp, sequence: sequence})
}

// enqueue is the only path into the scorer, and the only place a drop can
// happen — deliberately one place, so "never block the audio path" is a
// property of one function rather than a convention.
func (s *Scorer) enqueue(frame scoredFrame) {
	// A closed scorer still receives frames: the mic goroutine holds the
	// pointer it captured when the stream started, and a config push can
	// replace and close it mid-stream.
	if s.closed.Load() {
		return
	}
	// Producer cadence, measured here rather than in Push/PushBytes so both
	// entry points are covered by one site — the same reason drops are counted
	// in exactly one place.
	if now := time.Now(); !s.lastPush.IsZero() {
		if ms := now.Sub(s.lastPush).Milliseconds(); ms > s.maxGapMs.Load() {
			s.maxGapMs.Store(ms)
		}
		s.lastPush = now
	} else {
		s.lastPush = now
	}

	select {
	case s.ch <- frame:
	default:
		s.mu.Lock()
		s.stats.Drops++
		s.mu.Unlock()
	}
}

// Ready reports whether the detector has accumulated enough embeddings to
// score (~1.28s of audio). Exposed through the mutex rather than by reading the
// Detector, which belongs to the scorer goroutine and is not safe to touch from
// anywhere else — worth having so a log line can distinguish "no crossings
// because the room was quiet" from "no crossings because it never warmed up".
func (s *Scorer) Ready() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ready
}

// Reset asks the scorer to clear its streaming state, for when the mic stream
// restarts — audio from before a gap must not contribute to a score after it.
// Applied by the scorer goroutine, since Detector is single-goroutine.
func (s *Scorer) Reset() {
	s.mu.Lock()
	s.resetReq = true
	s.mu.Unlock()
}

// Drain returns the accumulated stats and zeroes the counters, so the caller
// can attach them to a periodic report. MaxScore resets too: it is the maximum
// within a reporting window, not since boot, which is what makes a series of
// them readable.
func (s *Scorer) Drain() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := s.stats
	s.stats = Stats{}
	out.MaxInferMs = uint32(s.maxInferMs.Swap(0))
	out.MaxGapMs = uint32(s.maxGapMs.Swap(0))
	return out
}

// Info describes the loaded inference engine, for a log line at startup.
func (s *Scorer) Info() string { return s.info }

// Close stops the scorer goroutine and releases the inference engine. Safe to
// call more than once: it runs both on a config change and at shutdown, and
// those can overlap.
func (s *Scorer) Close() {
	s.stop.Do(func() {
		s.closed.Store(true)
		close(s.quit)
		<-s.done
		// After the goroutine has exited, so no inference is in flight.
		for _, closer := range s.closers {
			_ = closer.Close()
		}
	})
}

func (s *Scorer) run() {
	defer close(s.done)
	for {
		var frame scoredFrame
		select {
		case <-s.quit:
			return
		case frame = <-s.ch:
		}
		s.mu.Lock()
		reset := s.resetReq
		s.resetReq = false
		threshold := s.effectiveThresholdLocked()
		s.stats.Threshold = threshold
		s.mu.Unlock()

		if reset {
			s.det.Reset()
			s.mu.Lock()
			s.ready = false
			s.mu.Unlock()
		}

		t0 := time.Now()
		if _, err := s.det.Push(frame.pcm); err != nil {
			s.recordErr(err)
			continue
		}
		if !s.det.Ready() {
			s.mu.Lock()
			s.stats.Frames++
			s.stats.NotReady++
			s.mu.Unlock()
			continue
		}
		s.mu.Lock()
		s.ready = true
		s.mu.Unlock()
		score, err := s.det.Score()
		if err != nil {
			s.recordErr(err)
			continue
		}

		now := time.Now()
		// Feature extraction plus classification: the whole cost of one frame,
		// which is what has to fit inside the queue's 640ms of slack.
		if ms := now.Sub(t0).Milliseconds(); ms > s.maxInferMs.Load() {
			s.maxInferMs.Store(ms)
		}
		s.mu.Lock()
		s.stats.Frames++
		if score > s.stats.MaxScore {
			s.stats.MaxScore = score
		}
		crossed := score >= threshold && now.Sub(s.lastCross) >= s.refract
		if crossed {
			s.stats.Crossings++
			s.lastCross = now
		}
		onScore := s.onScore
		bargeThreshold := s.bargeThreshold > 0 && threshold == s.bargeThreshold
		s.mu.Unlock()

		if score > 0.01 {
			log.Printf("[wake] score=%.4f threshold=%.2f crossed=%v seq=%d barge=%v", score, threshold, crossed, frame.sequence, bargeThreshold)
		}
		if onScore != nil {
			onScore(ScoreEvent{
				Score: score, Threshold: threshold, At: now,
				Sequence: frame.sequence, Crossed: crossed,
				BargeThreshold: bargeThreshold,
			})
		}
		if crossed && s.onCross != nil {
			s.onCross(score, threshold, now, frame.sequence)
		}
		// Optional heads share the already-computed feature ring. They remain
		// completely off while disabled, which is how stop detection avoids
		// permanent classifier work between armed turns.
		for i := range s.head {
			h := &s.head[i]
			if h.Enabled != nil && !h.Enabled() {
				continue
			}
			headScore, err := s.det.ScoreWith(h.Classifier)
			if err != nil {
				s.recordErr(err)
				continue
			}
			if headScore > 0.01 {
				log.Printf("[stopword] score=%.4f threshold=%.2f seq=%d crossed=%v", headScore, h.Threshold, frame.sequence, headScore >= h.Threshold && now.Sub(h.lastCross) >= s.refract)
			}
			if headScore < h.Threshold || now.Sub(h.lastCross) < s.refract {
				continue
			}
			h.lastCross = now
			if h.OnCross != nil {
				h.OnCross(headScore, h.Threshold, now, frame.sequence)
			}
		}
	}
}

func (s *Scorer) recordErr(err error) {
	s.mu.Lock()
	s.stats.Frames++
	s.stats.Errors++
	s.stats.LastErr = err.Error()
	s.mu.Unlock()
}
