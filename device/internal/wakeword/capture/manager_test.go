package capture

import (
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
)

func testManager(t *testing.T) *Manager {
	t.Helper()
	ring := New(DefaultFrames)
	for i := 0; i < 40; i++ {
		ring.Push(uint16(i), []byte{byte(i)})
	}
	m := NewManager(ring)
	m.debounce = 5 * time.Millisecond
	m.Configure(Settings{
		Enabled: true, Frames: 5, NearMissFloor: 0.1,
		Model: "wake", ClassifierMD5: "0123456789abcdef0123456789abcdef",
	})
	// Configure clears identity-changing ring contents; refill after it.
	for i := 0; i < 40; i++ {
		ring.Push(uint16(i), []byte{byte(i)})
	}
	return m
}

func TestPeakNearMissWinsDebounceWindow(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.2, Threshold: 0.5, Sequence: 10})
	m.Observe(shadow.ScoreEvent{Score: 0.4, Threshold: 0.5, Sequence: 20})
	m.Observe(shadow.ScoreEvent{Score: 0.3, Threshold: 0.5, Sequence: 30})
	time.Sleep(15 * time.Millisecond)
	item := m.NextReady()
	if item == nil || item.Metadata.Kind != "miss" || item.Metadata.Score != 0.4 || item.Metadata.ActivationSeq != 20 {
		t.Fatalf("selected capture = %#v", item)
	}
}

func TestActivationSuppressesPendingNearMissAndWaitsForDecision(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.3, Threshold: 0.5, Sequence: 15})
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 20, Crossed: true})
	time.Sleep(15 * time.Millisecond)
	if item := m.NextReady(); item != nil {
		t.Fatalf("undecided activation became ready: %#v", item)
	}
	m.BindRequest(20, "wake:1")
	m.Grant("wake:1")
	if item := m.NextReady(); item != nil {
		t.Fatalf("granted activation uploaded before STT end: %#v", item)
	}
	m.EndSTT("wake:1")
	item := m.NextReady()
	if item == nil || item.Metadata.Kind != "act" || item.Metadata.Score != 0.8 {
		t.Fatalf("activation capture = %#v", item)
	}
}

func TestDeniedActivationIsReadyAndAckDeletesExactlyIt(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 20, Crossed: true})
	m.BindRequest(20, "wake:2")
	m.Deny("wake:2")
	item := m.NextReady()
	if item == nil {
		t.Fatal("denied activation was not ready")
	}
	if !m.Ack(item.Metadata.CaptureID) || m.Ack(item.Metadata.CaptureID) || m.Count() != 0 {
		t.Fatal("capture ACK was not idempotent")
	}
}

func TestDecisionArrivingBeforeRequestBindingIsApplied(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 20, Crossed: true})
	m.Deny("wake:fast")
	m.BindRequest(20, "wake:fast")
	if item := m.NextReady(); item == nil || item.Metadata.ActivationSeq != 20 {
		t.Fatalf("early denial was lost: %#v", item)
	}
}

func TestOneDecisionReleasesEveryCoalescedActivation(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 20, Crossed: true})
	m.Observe(shadow.ScoreEvent{Score: 0.9, Threshold: 0.5, Sequence: 21, Crossed: true})
	m.BindRequest(20, "wake:shared")
	m.BindRequest(21, "wake:shared")
	m.Deny("wake:shared")
	first := m.NextReady()
	if first == nil || !m.Ack(first.Metadata.CaptureID) {
		t.Fatal("first coalesced activation was not released")
	}
	second := m.NextReady()
	if second == nil || second.Metadata.CaptureID == first.Metadata.CaptureID {
		t.Fatal("second coalesced activation was not released")
	}
}

func TestActivationBelowNearMissFloorIsStillRetained(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.08, Threshold: 0.05, Sequence: 20, Crossed: true})
	m.BindRequest(20, "wake:barge")
	m.Deny("wake:barge")
	if item := m.NextReady(); item == nil || item.Metadata.Kind != "act" {
		t.Fatalf("low-threshold activation was dropped: %#v", item)
	}
}

func TestActivationEvictsNearMissButNeverAnotherActivation(t *testing.T) {
	m := testManager(t)
	for i := 0; i < QueueCapacity; i++ {
		m.enqueueLocked(m.snapshotLocked("miss", shadow.ScoreEvent{
			Score: 0.2, Threshold: 0.5, Sequence: uint16(10 + i),
		}, true))
	}
	m.enqueueLocked(m.snapshotLocked("act", shadow.ScoreEvent{
		Score: 0.8, Threshold: 0.5, Sequence: 30, Crossed: true,
	}, false))
	if len(m.queue) != QueueCapacity || m.queue[0].Metadata.Kind != "act" {
		t.Fatalf("queue policy = %#v", m.queue)
	}
	for _, queued := range m.queue {
		queued.Metadata.Kind = "act"
	}
	m.enqueueLocked(m.snapshotLocked("act", shadow.ScoreEvent{
		Score: 0.9, Threshold: 0.5, Sequence: 31, Crossed: true,
	}, false))
	if len(m.queue) != QueueCapacity {
		t.Fatalf("activation-only queue grew to %d", len(m.queue))
	}
}

func TestDisableClearsAllRetainedPCMAndRetryState(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 20, Crossed: true})
	m.BindRequest(20, "wake:disable")
	m.Deny("wake:disable")
	item := m.NextReady()
	m.Configure(Settings{Enabled: false})
	if m.Count() != 0 || m.candidate != nil || m.timer != nil {
		t.Fatalf("disabled manager retained state: %#v", m)
	}
	if frames, _ := m.ring.SnapshotEndingAt(20, 1); frames != nil {
		t.Fatal("disabled manager retained PCM ring")
	}
	if m.Current(item) {
		t.Fatal("disabled manager left an upload token current")
	}
	if pcm, ok := m.PCM(item); ok || pcm != nil || len(item.PCM) != 0 {
		t.Fatal("disabled manager exposed an uploader-local PCM copy")
	}
}
