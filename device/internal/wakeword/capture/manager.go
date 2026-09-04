package capture

import (
	cryptorand "crypto/rand"
	"encoding/hex"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
)

const (
	QueueCapacity = 4
	Debounce      = 3 * time.Second
	FrameMs       = 80
)

type Metadata struct {
	CaptureID            string  `json:"captureId"`
	Kind                 string  `json:"kind"`
	Model                string  `json:"model"`
	ClassifierMD5        string  `json:"classifierMd5"`
	Score                float32 `json:"score"`
	Threshold            float32 `json:"threshold"`
	NearMissFloor        float32 `json:"nearMissFloor"`
	ActivationSeq        uint16  `json:"activationSeq"`
	RequestedPrerollMs   int     `json:"requestedPrerollMs"`
	ActualPrerollMs      int     `json:"actualPrerollMs"`
	Complete             bool    `json:"complete"`
	SampleRate           int     `json:"sampleRate"`
	SampleWidth          int     `json:"sampleWidth"`
	Channels             int     `json:"channels"`
	FrameBytes           int     `json:"frameBytes"`
	BargeThresholdActive bool    `json:"bargeThresholdActive"`
}

type Capture struct {
	Metadata Metadata
	PCM      []byte

	requestID  string
	ready      bool
	inFlight   bool
	generation uint64
}

type Settings struct {
	Enabled       bool
	Frames        int
	NearMissFloor float32
	Model         string
	ClassifierMD5 string
	// CrossKind and MissKind name the two capture kinds this manager instance
	// produces, matching em_training_captures.KINDS on the controller. Empty
	// defaults to "act"/"miss" (wake's kinds) in Configure, so the existing
	// wake call site needs no change; the stop manager sets these explicitly
	// to "stop_act"/"stop_miss".
	CrossKind string
	MissKind  string
	// CrossReady marks a crossed-kind capture ready to upload immediately,
	// skipping the BindRequest/Grant/Deny handshake a wake activation needs.
	// Multi-device wake arbitration can deny a wake request after the fact,
	// so a wake "act" clip waits for confirmation it was actually used before
	// it uploads. A stop crossing has no equivalent ambiguity —
	// stopword.Manager.Accept already consumed a live, ungranted arm before
	// HandleStopCrossing runs — so the stop manager sets this true and never
	// calls the grant/deny methods at all.
	CrossReady bool
}

type candidate struct {
	event shadow.ScoreEvent
}

type Manager struct {
	mu             sync.Mutex
	ring           *Ring
	settings       Settings
	queue          []*Capture
	decisions      map[string]bool
	candidate      *candidate
	timer          *time.Timer
	timerEpoch     uint64
	activationSeen bool
	notify         chan struct{}
	nonce          string
	counter        atomic.Uint64
	debounce       time.Duration
	generation     uint64
}

func NewManager(ring *Ring) *Manager {
	var nonce [8]byte
	if _, err := cryptorand.Read(nonce[:]); err != nil {
		copy(nonce[:], []byte("capture!"))
	}
	return &Manager{
		ring: ring, notify: make(chan struct{}, 1), nonce: hex.EncodeToString(nonce[:]),
		debounce: Debounce, decisions: make(map[string]bool),
	}
}

func (m *Manager) Notify() <-chan struct{} { return m.notify }

func (m *Manager) Enabled() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.settings.Enabled
}

func (m *Manager) Configure(settings Settings) {
	if settings.Frames < 1 {
		settings.Frames = 1
	}
	if settings.Frames > 62 {
		settings.Frames = 62
	}
	if settings.CrossKind == "" {
		settings.CrossKind = "act"
	}
	if settings.MissKind == "" {
		settings.MissKind = "miss"
	}
	m.mu.Lock()
	changedIdentity := settings.Model != m.settings.Model || settings.ClassifierMD5 != m.settings.ClassifierMD5
	m.settings = settings
	if !settings.Enabled || changedIdentity {
		m.generation++
		m.clearLocked()
	}
	m.mu.Unlock()
	if !settings.Enabled || changedIdentity {
		m.ring.Clear()
	}
}

func (m *Manager) Clear() {
	m.mu.Lock()
	m.generation++
	m.clearLocked()
	m.mu.Unlock()
	m.ring.Clear()
}

func (m *Manager) clearLocked() {
	m.timerEpoch++
	if m.timer != nil {
		m.timer.Stop()
	}
	m.timer = nil
	m.candidate = nil
	m.activationSeen = false
	for _, capture := range m.queue {
		for i := range capture.PCM {
			capture.PCM[i] = 0
		}
		capture.PCM = nil
	}
	m.queue = nil
	clear(m.decisions)
}

func (m *Manager) Observe(event shadow.ScoreEvent) {
	m.mu.Lock()
	defer m.mu.Unlock()
	settings := m.settings
	if !settings.Enabled {
		return
	}
	if event.Crossed {
		m.activationSeen = true
		m.candidate = nil
		if m.timer != nil {
			m.timer.Stop()
		}
		m.timerEpoch++
		epoch := m.timerEpoch
		m.timer = time.AfterFunc(m.debounce, func() { m.endUtterance(epoch) })
		m.enqueueLocked(m.snapshotLocked(settings.CrossKind, event, settings.CrossReady))
		return
	}
	if event.Score <= settings.NearMissFloor {
		return
	}
	if m.activationSeen || event.Score >= event.Threshold {
		return
	}
	if m.candidate == nil || event.Score > m.candidate.event.Score {
		m.candidate = &candidate{event: event}
	}
	if m.timer == nil {
		m.timerEpoch++
		epoch := m.timerEpoch
		m.timer = time.AfterFunc(m.debounce, func() { m.endUtterance(epoch) })
	}
}

func (m *Manager) endUtterance(epoch uint64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if epoch != m.timerEpoch {
		return
	}
	if !m.activationSeen && m.candidate != nil && m.settings.Enabled {
		m.enqueueLocked(m.snapshotLocked(m.settings.MissKind, m.candidate.event, true))
	}
	m.timer = nil
	m.candidate = nil
	m.activationSeen = false
}

func (m *Manager) snapshotLocked(kind string, event shadow.ScoreEvent, ready bool) *Capture {
	frames, complete := m.ring.SnapshotEndingAt(event.Sequence, m.settings.Frames)
	if len(frames) == 0 {
		return nil
	}
	pcm := make([]byte, 0, len(frames)*len(frames[0].PCM))
	for _, frame := range frames {
		pcm = append(pcm, frame.PCM...)
	}
	id := fmt.Sprintf("%s:%d", m.nonce, m.counter.Add(1))
	return &Capture{
		Metadata: Metadata{
			CaptureID: id, Kind: kind, Model: m.settings.Model,
			ClassifierMD5: m.settings.ClassifierMD5, Score: event.Score,
			Threshold: event.Threshold, NearMissFloor: m.settings.NearMissFloor,
			ActivationSeq:      event.Sequence,
			RequestedPrerollMs: m.settings.Frames * FrameMs,
			ActualPrerollMs:    len(frames) * FrameMs, Complete: complete,
			SampleRate: 16000, SampleWidth: 2, Channels: 1,
			FrameBytes: 2560, BargeThresholdActive: event.BargeThreshold,
		},
		PCM: pcm, ready: ready, generation: m.generation,
	}
}

func (m *Manager) enqueueLocked(capture *Capture) {
	if capture == nil {
		return
	}
	crossKind := m.settings.CrossKind
	missKind := m.settings.MissKind
	if len(m.queue) >= QueueCapacity {
		if capture.Metadata.Kind != crossKind {
			return
		}
		evict := -1
		for i, queued := range m.queue {
			if queued.Metadata.Kind == missKind && !queued.inFlight {
				evict = i
				break
			}
		}
		if evict < 0 {
			return
		}
		m.queue = append(m.queue[:evict], m.queue[evict+1:]...)
	}
	if capture.Metadata.Kind == crossKind {
		index := 0
		for index < len(m.queue) && m.queue[index].Metadata.Kind == crossKind {
			index++
		}
		m.queue = append(m.queue, nil)
		copy(m.queue[index+1:], m.queue[index:])
		m.queue[index] = capture
	} else {
		m.queue = append(m.queue, capture)
	}
	if capture.ready {
		m.signalLocked()
	}
}

func (m *Manager) BindRequest(sequence uint16, requestID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	crossKind := m.settings.CrossKind
	for i := len(m.queue) - 1; i >= 0; i-- {
		capture := m.queue[i]
		if capture.Metadata.Kind == crossKind && capture.Metadata.ActivationSeq == sequence && capture.requestID == "" {
			capture.requestID = requestID
			if ready, ok := m.decisions[requestID]; ok {
				capture.ready = ready
				if ready {
					m.signalLocked()
				}
			}
			return
		}
	}
}

func (m *Manager) DropActivation(sequence uint16) {
	m.mu.Lock()
	defer m.mu.Unlock()
	crossKind := m.settings.CrossKind
	for i := len(m.queue) - 1; i >= 0; i-- {
		capture := m.queue[i]
		if capture.Metadata.Kind == crossKind && capture.Metadata.ActivationSeq == sequence && capture.requestID == "" {
			for j := range capture.PCM {
				capture.PCM[j] = 0
			}
			m.queue = append(m.queue[:i], m.queue[i+1:]...)
			return
		}
	}
}

func (m *Manager) ReleaseActivation(sequence uint16) {
	m.mu.Lock()
	defer m.mu.Unlock()
	crossKind := m.settings.CrossKind
	for i := len(m.queue) - 1; i >= 0; i-- {
		capture := m.queue[i]
		if capture.Metadata.Kind == crossKind && capture.Metadata.ActivationSeq == sequence && capture.requestID == "" {
			capture.ready = true
			m.signalLocked()
			return
		}
	}
}

func (m *Manager) Grant(requestID string)  { m.setDecision(requestID, false) }
func (m *Manager) Deny(requestID string)   { m.setDecision(requestID, true) }
func (m *Manager) EndSTT(requestID string) { m.setDecision(requestID, true) }

func (m *Manager) setDecision(requestID string, ready bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, capture := range m.queue {
		if capture.requestID == requestID {
			capture.ready = ready
			if ready {
				m.signalLocked()
			}
		}
	}
	if len(m.decisions) >= QueueCapacity*2 {
		clear(m.decisions)
	}
	m.decisions[requestID] = ready
}

func (m *Manager) NextReady() *Capture {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, capture := range m.queue {
		if capture.ready && !capture.inFlight {
			capture.inFlight = true
			return &Capture{Metadata: capture.Metadata, generation: capture.generation}
		}
	}
	return nil
}

func (m *Manager) Current(capture *Capture) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.findCurrentLocked(capture) != nil
}

func (m *Manager) ChunkCount(capture *Capture) (int, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	queued := m.findCurrentLocked(capture)
	if queued == nil {
		return 0, false
	}
	return len(queued.PCM) / queued.Metadata.FrameBytes, true
}

func (m *Manager) Chunk(capture *Capture, index int) ([]byte, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	queued := m.findCurrentLocked(capture)
	if queued == nil || index < 0 {
		return nil, false
	}
	start := index * queued.Metadata.FrameBytes
	end := start + queued.Metadata.FrameBytes
	if end > len(queued.PCM) {
		return nil, false
	}
	return append([]byte(nil), queued.PCM[start:end]...), true
}

func (m *Manager) PCM(capture *Capture) ([]byte, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	queued := m.findCurrentLocked(capture)
	if queued == nil {
		return nil, false
	}
	return append([]byte(nil), queued.PCM...), true
}

func (m *Manager) findCurrentLocked(capture *Capture) *Capture {
	if capture == nil || !m.settings.Enabled || capture.generation != m.generation {
		return nil
	}
	for _, queued := range m.queue {
		if queued.Metadata.CaptureID == capture.Metadata.CaptureID {
			return queued
		}
	}
	return nil
}

func (m *Manager) Retry(captureID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, capture := range m.queue {
		if capture.Metadata.CaptureID == captureID {
			capture.inFlight = false
			if capture.ready {
				m.signalLocked()
			}
			return
		}
	}
}

func (m *Manager) RetryInFlight() {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, capture := range m.queue {
		if capture.inFlight {
			capture.inFlight = false
			if capture.ready {
				m.signalLocked()
			}
		}
	}
}

func (m *Manager) HasInFlight() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, capture := range m.queue {
		if capture.inFlight {
			return true
		}
	}
	return false
}

func (m *Manager) Ack(captureID string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	for i, capture := range m.queue {
		if capture.Metadata.CaptureID == captureID {
			m.queue = append(m.queue[:i], m.queue[i+1:]...)
			return true
		}
	}
	return false
}

func (m *Manager) signalLocked() {
	select {
	case m.notify <- struct{}{}:
	default:
	}
}

func (m *Manager) Count() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.queue)
}
