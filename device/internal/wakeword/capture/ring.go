// Package capture stores a short, sequence-addressed microphone history in RAM.
package capture

import "sync"

const DefaultFrames = 100 // 8 seconds at 80 ms per frame.

type Frame struct {
	Sequence uint16
	PCM      []byte
}

type Ring struct {
	mu       sync.Mutex
	frames   []Frame
	next     int
	count    int
	capacity int
}

func New(capacity int) *Ring {
	if capacity < 1 {
		capacity = DefaultFrames
	}
	return &Ring{frames: make([]Frame, capacity), capacity: capacity}
}

func (r *Ring) Clear() {
	r.mu.Lock()
	defer r.mu.Unlock()
	for i := range r.frames {
		r.frames[i] = Frame{}
	}
	r.next, r.count = 0, 0
}

func (r *Ring) Push(sequence uint16, pcm []byte) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.frames[r.next] = Frame{Sequence: sequence, PCM: append([]byte(nil), pcm...)}
	r.next = (r.next + 1) % r.capacity
	if r.count < r.capacity {
		r.count++
	}
}

// SnapshotFrom returns a contiguous window beginning up to preFrames before
// activation and ending at the newest buffered frame.
func (r *Ring) SnapshotFrom(activation uint16, preFrames int) ([]Frame, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	ordered := make([]Frame, 0, r.count)
	start := (r.next - r.count + r.capacity) % r.capacity
	for i := 0; i < r.count; i++ {
		ordered = append(ordered, r.frames[(start+i)%r.capacity])
	}
	anchor := -1
	for i := len(ordered) - 1; i >= 0; i-- {
		if ordered[i].Sequence == activation {
			anchor = i
			break
		}
	}
	if anchor < 0 {
		return nil, false
	}
	begin := anchor - preFrames
	if begin < 0 {
		return nil, false
	}
	// Never bridge a sequence gap.
	for i := anchor; i > begin; i-- {
		if ordered[i].Sequence != ordered[i-1].Sequence+1 {
			return nil, false
		}
	}
	end := len(ordered) - 1
	for i := anchor + 1; i <= end; i++ {
		if ordered[i].Sequence != ordered[i-1].Sequence+1 {
			end = i - 1
			break
		}
	}
	out := make([]Frame, end-begin+1)
	for i := range out {
		out[i] = Frame{Sequence: ordered[begin+i].Sequence, PCM: append([]byte(nil), ordered[begin+i].PCM...)}
	}
	return out, true
}

// SnapshotEndingAt returns up to frameCount contiguous frames ending exactly
// at sequence. Short history and gaps return the safe suffix with complete=false.
func (r *Ring) SnapshotEndingAt(sequence uint16, frameCount int) ([]Frame, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if frameCount < 1 {
		return nil, false
	}
	ordered := make([]Frame, 0, r.count)
	start := (r.next - r.count + r.capacity) % r.capacity
	for i := 0; i < r.count; i++ {
		ordered = append(ordered, r.frames[(start+i)%r.capacity])
	}
	anchor := -1
	for i := len(ordered) - 1; i >= 0; i-- {
		if ordered[i].Sequence == sequence {
			anchor = i
			break
		}
	}
	if anchor < 0 {
		return nil, false
	}
	begin := anchor
	for begin > 0 && anchor-begin+1 < frameCount {
		if ordered[begin].Sequence != ordered[begin-1].Sequence+1 {
			break
		}
		begin--
	}
	out := make([]Frame, anchor-begin+1)
	for i := range out {
		frame := ordered[begin+i]
		out[i] = Frame{Sequence: frame.Sequence, PCM: append([]byte(nil), frame.PCM...)}
	}
	return out, len(out) == frameCount
}
