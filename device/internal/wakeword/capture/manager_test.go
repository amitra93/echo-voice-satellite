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

func TestCaptureSettingsClampFrameWindow(t *testing.T) {
	m := NewManager(New(DefaultFrames))
	m.Configure(Settings{Enabled: true, Frames: 0})
	if got := m.settings.Frames; got != 1 {
		t.Fatalf("minimum capture window = %d, want 1", got)
	}
	m.Configure(Settings{Enabled: true, Frames: 999})
	if got := m.settings.Frames; got != 62 {
		t.Fatalf("maximum capture window = %d, want 62", got)
	}
}

func TestActivationSnapshotRecordsSafeIncompleteSuffix(t *testing.T) {
	ring := New(8)
	m := NewManager(ring)
	m.Configure(Settings{
		Enabled: true, Frames: 3, NearMissFloor: 0.1,
		Model: "wake", ClassifierMD5: "0123456789abcdef0123456789abcdef",
	})
	for _, sequence := range []uint16{10, 12} {
		ring.Push(sequence, []byte{byte(sequence)})
	}
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 12, Crossed: true})
	m.BindRequest(12, "wake:incomplete")
	m.Deny("wake:incomplete")
	item := m.NextReady()
	if item == nil {
		t.Fatal("incomplete activation was not retained")
	}
	if item.Metadata.ActualPrerollMs != FrameMs || item.Metadata.Complete {
		t.Fatalf("incomplete metadata = %+v", item.Metadata)
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

func TestSuppressedActivationIsRemoved(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 20, Crossed: true})
	m.DropActivation(20)
	if m.Count() != 0 {
		t.Fatal("suppressed activation remained in the bounded queue")
	}
}

func TestNonTriggeringModeActivationUploadsWithoutAdmission(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 20, Crossed: true})
	m.ReleaseActivation(20)
	if item := m.NextReady(); item == nil || item.Metadata.ActivationSeq != 20 {
		t.Fatalf("non-triggering activation was not retained: %#v", item)
	}
}

func TestRetryInFlightMakesCaptureAvailableAgain(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 20, Crossed: true})
	m.BindRequest(20, "wake:retry")
	m.Deny("wake:retry")
	first := m.NextReady()
	if first == nil {
		t.Fatal("capture was not claimed")
	}
	m.RetryInFlight()
	second := m.NextReady()
	if second == nil || second.Metadata.CaptureID != first.Metadata.CaptureID {
		t.Fatalf("retried capture = %#v", second)
	}
}

func TestStaleDebounceCallbackCannotClearNewGeneration(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.3, Threshold: 0.5, Sequence: 20})
	staleEpoch := m.timerEpoch
	m.Clear()
	m.Configure(Settings{
		Enabled: true, Frames: 5, NearMissFloor: 0.1,
		Model: "wake", ClassifierMD5: "0123456789abcdef0123456789abcdef",
	})
	for i := 0; i < 40; i++ {
		m.ring.Push(uint16(i), []byte{byte(i)})
	}
	m.Observe(shadow.ScoreEvent{Score: 0.4, Threshold: 0.5, Sequence: 21})
	m.endUtterance(staleEpoch)
	if m.candidate == nil || m.candidate.event.Sequence != 21 {
		t.Fatal("stale debounce callback cleared the new generation")
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

// stopManager mirrors testManager but configured the way
// DataClient.ConfigureStopCaptures configures the stop word's manager
// instance: distinct kinds, and crossings ready to upload immediately (no
// BindRequest/Grant/Deny handshake — see Settings.CrossReady).
func stopManagerForTest(t *testing.T) *Manager {
	t.Helper()
	ring := New(DefaultFrames)
	for i := 0; i < 40; i++ {
		ring.Push(uint16(i), []byte{byte(i)})
	}
	m := NewManager(ring)
	m.debounce = 5 * time.Millisecond
	m.Configure(Settings{
		Enabled: true, Frames: 5, NearMissFloor: 0.1,
		Model: "stop", ClassifierMD5: "0123456789abcdef0123456789abcdef",
		CrossKind: "stop_act", MissKind: "stop_miss", CrossReady: true,
	})
	for i := 0; i < 40; i++ {
		ring.Push(uint16(i), []byte{byte(i)})
	}
	return m
}

func TestStopCrossingIsReadyImmediatelyWithoutGrant(t *testing.T) {
	m := stopManagerForTest(t)
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 20, Crossed: true})
	item := m.NextReady()
	if item == nil || item.Metadata.Kind != "stop_act" || item.Metadata.Score != 0.8 {
		t.Fatalf("stop crossing capture = %#v", item)
	}
}

func TestStopNearMissUsesStopMissKind(t *testing.T) {
	m := stopManagerForTest(t)
	m.Observe(shadow.ScoreEvent{Score: 0.2, Threshold: 0.5, Sequence: 20})
	time.Sleep(15 * time.Millisecond)
	item := m.NextReady()
	if item == nil || item.Metadata.Kind != "stop_miss" || item.Metadata.Score != 0.2 {
		t.Fatalf("stop near-miss capture = %#v", item)
	}
}

func TestStopQueueEvictsStopMissButNeverStopAct(t *testing.T) {
	m := stopManagerForTest(t)
	for i := 0; i < QueueCapacity; i++ {
		m.Observe(shadow.ScoreEvent{Score: 0.2, Threshold: 0.5, Sequence: uint16(10 + i)})
		time.Sleep(10 * time.Millisecond)
	}
	if m.Count() != QueueCapacity {
		t.Fatalf("queue = %d misses, want %d", m.Count(), QueueCapacity)
	}
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 30, Crossed: true})
	if m.Count() != QueueCapacity || m.queue[0].Metadata.Kind != "stop_act" {
		t.Fatalf("queue policy = %#v", m.queue)
	}
}

// Wake's own manager must be unaffected by the stop kinds existing at
// all — Configure defaults an empty CrossKind/MissKind to "act"/"miss" so
// the existing ConfigureWakeCaptures call site needed no change.
func TestUnconfiguredKindsDefaultToWakeNames(t *testing.T) {
	m := testManager(t)
	m.Observe(shadow.ScoreEvent{Score: 0.8, Threshold: 0.5, Sequence: 20, Crossed: true})
	m.BindRequest(20, "wake:default")
	m.Deny("wake:default")
	item := m.NextReady()
	if item == nil || item.Metadata.Kind != "act" {
		t.Fatalf("default cross kind = %#v", item)
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
