package speaker

import (
	"encoding/binary"
	"math"
	"sync"
)

const scheduledMusicRate = 48000

type scheduledMusicChunk struct {
	startSample int64 // absolute stream sample index of pcm[0]
	targetUs    int64 // intended presentation time of pcm[0] (device clock)
	sequence    uint32
	pcm         []byte
}

// scheduledMusic renders contiguous mono PCM against the device monotonic
// clock. It is deliberately separate from audioStream: that stream is
// arrival-paced and primes before starting, while Sendspin audio must honour
// presentation times.
//
// Chunks carry a per-frame presentation timestamp, but after the first sample
// anchors the stream those timestamps are used only for drift telemetry, not
// for per-sample positioning. The previous renderer re-derived the read index
// from each chunk's own timestamp and picked the NEAREST sample; the slow
// device-vs-source clock drift then made it skip or repeat a whole sample
// roughly twice a second, and each skip/repeat is a step discontinuity —
// audible as soft clicking on sustained tones. Instead a single anchor maps
// device time to a continuous fractional read position and output samples are
// LINEARLY INTERPOLATED, so a drift correction moves the read point by a
// fraction of a sample rather than dropping or duplicating one.
type scheduledMusic struct {
	mu           sync.Mutex
	generation   uint32
	active       bool
	ended        bool
	hasSequence  bool
	lastSequence uint32
	lastTargetUs int64

	chunks     []scheduledMusicChunk
	nextSample int64 // absolute index assigned to the next pushed chunk

	// Scratch owned by the render path (the ALSA writer), so a period costs
	// no allocation: scratch is the flattened source window, out the emitted
	// period.
	scratch []int16
	out     []byte

	started        bool
	anchorUs       int64 // device time at which stream sample 0 is presented
	lastReadSample int64 // floor of the most recent read position (telemetry)

	// telemetry — all reset per stream
	underruns uint64
	// holds counts samples emitted by repeating the previous one because the
	// NEXT source sample had not arrived. It replaced a count of interpolated
	// samples, which incremented ~48000 times a second — once per sample,
	// near enough — and so could never indicate anything. A hold is the
	// leading-edge starvation that deliberately does not count as an underrun,
	// and it is the number that says the buffer is running close to empty
	// before it actually empties.
	holds        uint64
	lateSamples  uint64
	startErrorUs int64
	lastErrorUs  int64
	maxDriftUs   int64
}

type scheduledMusicStats struct {
	Underruns    uint64
	Holds        uint64
	LateSamples  uint64
	StartErrorUs int64
	LastErrorUs  int64
	MaxDriftUs   int64
	BufferedMs   int64
}

func (s *scheduledMusic) resetStream() {
	s.chunks = nil
	s.nextSample = 0
	s.started = false
	s.hasSequence = false
	s.anchorUs = 0
	s.lastReadSample = 0
}

func (s *scheduledMusic) start(generation uint32) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if generation == 0 || (s.generation != 0 && generation <= s.generation) {
		return false
	}
	s.generation = generation
	s.active = true
	s.ended = false
	s.resetStream()
	s.underruns, s.holds, s.lateSamples = 0, 0, 0
	s.startErrorUs, s.lastErrorUs, s.maxDriftUs = 0, 0, 0
	s.lastTargetUs = 0
	return true
}

func (s *scheduledMusic) push(generation, sequence uint32, targetUs int64, pcm []byte) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.active || generation != s.generation || (sequence == 0 && s.hasSequence) ||
		(s.hasSequence && sequence <= s.lastSequence) || targetUs < 0 ||
		len(pcm) == 0 || len(pcm)%2 != 0 {
		return false
	}
	if s.hasSequence && targetUs < s.lastTargetUs {
		return false
	}
	s.chunks = append(s.chunks, scheduledMusicChunk{
		startSample: s.nextSample,
		targetUs:    targetUs,
		sequence:    sequence,
		pcm:         append([]byte(nil), pcm...),
	})
	s.nextSample += int64(len(pcm) / 2)
	s.lastSequence = sequence
	s.hasSequence = true
	s.lastTargetUs = targetUs
	return true
}

func (s *scheduledMusic) clear(generation uint32) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.active || generation != s.generation {
		return false
	}
	s.resetStream()
	s.ended = false
	return true
}

func (s *scheduledMusic) end(generation uint32) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.active || generation != s.generation {
		return false
	}
	s.ended = true
	return true
}

func (s *scheduledMusic) hasStream() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.active
}

// gather flattens the absolute source range [from, to) into s.scratch.
//
// The queue is CONTIGUOUS by construction — push assigns each chunk the sample
// index the previous one ended on — so availability is a range test rather
// than a search, and the copy is one forward walk. The previous renderer
// resolved every sample independently, re-walking the chunk list twice per
// output sample; that is 4096 lookups per period to read 2052 consecutive
// samples, and it is why the interpolation could not afford more taps.
//
// Returns the range actually copied, clamped to what is buffered.
func (s *scheduledMusic) gather(from, to int64) (int64, int64) {
	if len(s.chunks) == 0 {
		return 0, 0
	}
	bufFrom, bufTo := s.chunks[0].startSample, s.nextSample
	if from < bufFrom {
		from = bufFrom
	}
	if to > bufTo {
		to = bufTo
	}
	if to <= from {
		return 0, 0
	}
	n := int(to - from)
	if cap(s.scratch) < n {
		s.scratch = make([]int16, n)
	}
	s.scratch = s.scratch[:n]

	w := 0
	for i := range s.chunks {
		c := &s.chunks[i]
		cn := int64(len(c.pcm) / 2)
		if c.startSample+cn <= from {
			continue
		}
		if c.startSample >= to {
			break
		}
		lo, hi := from, to
		if c.startSample > lo {
			lo = c.startSample
		}
		if c.startSample+cn < hi {
			hi = c.startSample + cn
		}
		for k := lo; k < hi; k++ {
			off := int(k-c.startSample) * 2
			s.scratch[w] = int16(binary.LittleEndian.Uint16(c.pcm[off:]))
			w++
		}
	}
	return from, from + int64(w)
}

// Fractional-delay interpolation, as a polyphase table of windowed-sinc taps.
//
// WHAT THIS REPLACED AND WHY. The renderer reads at a fractional position, so
// the interpolator's response IS the system's treble response. Linear
// interpolation costs |(1-f) + f·e^(-jω)|: 0dB at zero offset, but −2.0dB at
// 10kHz and −5.1dB at 15kHz at half a sample. The read phase sweeps a full
// cycle whenever the DAC and source rates differ by one part in 48000, which
// on this hardware is every 2-10 seconds — so linear does not merely dull the
// top octave, it makes it BREATHE on that period, which is far more noticeable
// than a fixed loss. Catmull-Rom cubic measures −2.5dB in the same test: an
// improvement, and still a swing.
//
// Sixteen taps at 512 phases measures below 0.01dB in the same test. Tap count
// was chosen by measuring rather than assumed: 8 taps gives 0.91dB and 12 gives
// 0.07dB, because a Blackman window this short trades passband flatness for a
// stopband that is nearly worthless HERE — the stride is within a few parts per
// million of 1.0, so this is a fractional DELAY, not a rate conversion, and
// there is essentially no aliasing for the stopband to reject. Passband
// flatness is the whole job.
//
// Sixteen taps is 32k multiply-adds per period, which against the biquad chain
// already running on this path is nothing. It is affordable only because the
// gather above made the source window contiguous; resolving each tap through
// the old per-sample chunk search would have been sixteen list walks per sample.
//
// Each phase is normalised to unity DC gain, so no level shift rides along as
// the phase sweeps — which would reintroduce exactly the slow modulation this
// exists to remove.
const (
	fdTaps   = 16
	fdPhases = 512
	fdCentre = fdTaps/2 - 1 // taps span [-7, +8] around the read position
)

var fdTable = buildFractionalDelayTable()

func buildFractionalDelayTable() *[fdPhases][fdTaps]float64 {
	t := new([fdPhases][fdTaps]float64)
	const halfWidth = float64(fdTaps) / 2
	for p := range t {
		f := float64(p) / fdPhases
		var sum float64
		for j := 0; j < fdTaps; j++ {
			x := float64(j-fdCentre) - f
			v := sinc(x) * blackman(x, halfWidth)
			t[p][j] = v
			sum += v
		}
		if sum != 0 {
			for j := range t[p] {
				t[p][j] /= sum
			}
		}
	}
	return t
}

func sinc(x float64) float64 {
	if x == 0 {
		return 1
	}
	px := math.Pi * x
	return math.Sin(px) / px
}

// blackman is the window over [-half, half], zero at and beyond the edges.
func blackman(x, half float64) float64 {
	if x <= -half || x >= half {
		return 0
	}
	t := math.Pi * x / half
	return 0.42 + 0.5*math.Cos(t) + 0.08*math.Cos(2*t)
}

// render returns one mono period. Silence is returned while waiting for the
// first target timestamp, before the anchor, or across a starvation gap. A
// late start skips the elapsed prefix rather than playing it late, preserving
// synchronisation.
//
// stride is how many source samples pass per output frame — 1.0 when the DAC
// runs at exactly the source rate, and the resampling ratio otherwise.
//
// THE RETURNED SLICE IS SCRATCH owned by the stream and is valid only until
// the next call. It is reused because this runs on the ALSA writer, which has
// a hard 42.7ms deadline: allocating a period here (plus a second one in
// toStereo) put ~300KB/s of garbage on exactly the goroutine that must not be
// interrupted by a collection.
func (s *scheduledMusic) render(nowUs int64, stride float64, samples int) []byte {
	if samples <= 0 {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.active {
		return nil
	}
	if cap(s.out) < samples*2 {
		s.out = make([]byte, samples*2)
	}
	result := s.out[:samples*2]
	for i := range result {
		result[i] = 0
	}
	if stride <= 0 {
		stride = 1.0
	}

	if !s.started {
		if len(s.chunks) == 0 || nowUs < s.chunks[0].targetUs {
			return result // waiting for the anchor — silence
		}
		s.started = true
		s.anchorUs = s.chunks[0].targetUs
		s.startErrorUs = nowUs - s.anchorUs
		if s.startErrorUs > 0 {
			// Started late: the prefix between the anchor and now is skipped
			// rather than played late.
			s.lateSamples = uint64(s.startErrorUs * scheduledMusicRate / 1_000_000)
		}
	}

	const rate = float64(scheduledMusicRate)
	// Playback-clock rate corrections can leave two consecutive ALSA periods
	// with overlapping read positions. Keep a bounded history so that overlap
	// replays PCM instead of turning already-consumed samples into silence.
	pos0 := float64(nowUs-s.anchorUs) * rate / 1_000_000.0
	historyStart := int64(math.Floor(pos0)) - int64(samples*2)
	for len(s.chunks) > 1 {
		c := s.chunks[0]
		if c.startSample+int64(len(c.pcm)/2) <= historyStart {
			s.chunks = s.chunks[1:]
		} else {
			break
		}
	}

	// One flatten for the whole period, sized for the cubic's window: one
	// sample of history behind the first read and two of lookahead past the
	// last. The inner loop is then array indexing and arithmetic only.
	posEnd := pos0 + stride*float64(samples-1)
	have, haveEnd := s.gather(
		int64(math.Floor(pos0))-fdCentre,
		int64(math.Floor(posEnd))+fdTaps-fdCentre)

	starved := false
	var lastI0 int64
	for i := 0; i < samples; i++ {
		pos := pos0 + stride*float64(i)
		if pos < 0 {
			continue // before the anchor — silence
		}
		i0 := int64(math.Floor(pos))
		frac := pos - float64(i0)
		lastI0 = i0

		if i0 < have || i0 >= haveEnd {
			starved = true
			continue // buffer starved — silence
		}
		k := int(i0 - have)
		var out float64
		switch {
		case frac <= 0:
			out = float64(s.scratch[k])
		case i0+1 >= haveEnd:
			// The next sample has not arrived, so there is nothing to
			// interpolate towards. Hold rather than emitting a zero, which
			// would click; the period still contains audible stream data, so
			// this is not an underrun. It IS the leading edge of the buffer
			// though, which is why it is counted.
			out = float64(s.scratch[k])
			s.holds++
		default:
			taps := &fdTable[int(frac*fdPhases)]
			lo, hi := k-fdCentre, k-fdCentre+fdTaps
			if lo >= 0 && hi <= len(s.scratch) {
				w := s.scratch[lo:hi:hi]
				for j, c := range taps {
					out += c * float64(w[j])
				}
			} else {
				// Within a window of the buffer's edge, so the missing taps
				// repeat the edge sample. Replicating rather than zeroing:
				// a zero tap is a step towards silence, which is the click
				// this interpolator exists to avoid.
				for j, c := range taps {
					idx := lo + j
					if idx < 0 {
						idx = 0
					} else if idx >= len(s.scratch) {
						idx = len(s.scratch) - 1
					}
					out += c * float64(s.scratch[idx])
				}
			}
		}
		// A cubic through the samples can overshoot where a line cannot, so
		// the clamp is load-bearing here rather than defensive: a wrap would
		// turn a loud peak into a full-scale opposite-polarity one.
		v := math.Round(out)
		if v > 32767 {
			v = 32767
		} else if v < -32768 {
			v = -32768
		}
		binary.LittleEndian.PutUint16(result[i*2:], uint16(int16(v)))
	}

	// Drift telemetry: how far the front chunk's own timestamp sits from the
	// contiguous anchor timeline we actually play on. This is the per-chunk
	// jitter the interpolation smooths over; if it grew large it would mean
	// the controller's timestamps and the device clock disagree on rate.
	if len(s.chunks) > 0 {
		c := &s.chunks[0]
		drift := s.anchorUs + c.startSample*1_000_000/scheduledMusicRate - c.targetUs
		s.lastErrorUs = drift
		if drift < 0 {
			drift = -drift
		}
		if drift > s.maxDriftUs {
			s.maxDriftUs = drift
		}
	}

	s.lastReadSample = lastI0
	if starved && !s.ended {
		s.underruns++
	}
	if s.ended && (!s.started || lastI0 >= s.nextSample-1) {
		s.active = false
	}
	return result
}

func (s *scheduledMusic) stats() scheduledMusicStats {
	s.mu.Lock()
	defer s.mu.Unlock()
	buffered := s.nextSample - s.lastReadSample
	if buffered < 0 {
		buffered = 0
	}
	return scheduledMusicStats{
		Underruns:    s.underruns,
		Holds:        s.holds,
		LateSamples:  s.lateSamples,
		StartErrorUs: s.startErrorUs,
		LastErrorUs:  s.lastErrorUs,
		MaxDriftUs:   s.maxDriftUs,
		BufferedMs:   buffered * 1000 / scheduledMusicRate,
	}
}
