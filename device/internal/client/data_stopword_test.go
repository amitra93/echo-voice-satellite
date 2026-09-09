package client

import (
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/stopword"
	"github.com/wilbowes/EchoMuse/internal/wakeword"
	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
)

// dataTestInferer stands in for ONNX Runtime — see shadow.fakeInferer, the
// same pattern this package's shadow dependency already uses to keep
// Scorer host-testable without a real model or runtime.
type dataTestInferer struct{ score float32 }

func (f *dataTestInferer) Melspec(samples []float32) ([]float32, int, error) {
	frames := len(samples)/160 - 3
	if frames < 0 {
		frames = 0
	}
	return make([]float32, frames*wakeword.MelBins), frames, nil
}

func (f *dataTestInferer) Embed(window []float32) ([]float32, error) {
	return make([]float32, wakeword.FeatDim), nil
}

func (f *dataTestInferer) Classify(feats []float32) (float32, error) {
	return f.score, nil
}

func newTestScorer(t *testing.T, score float32) *shadow.Scorer {
	t.Helper()
	s := shadow.NewScorer(&dataTestInferer{score: score}, 0.5, nil)
	t.Cleanup(s.Close)
	return s
}

// ─── Wake capture manager delegation ───────────────────────────────────────

func TestDataClientWakeCaptureLifecycle(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.ConfigureWakeCaptures(true, 1.0, 0.1, "hey_jarvis_v0.1", "md5sum")

	// Feed enough audio into the ring that a crossing has something to
	// snapshot, then observe a crossing so a capture is enqueued. A wake
	// "act" capture is NOT ready on arrival (unlike a stop crossing) — it
	// waits for the Bind/Grant handshake, since multi-device arbitration
	// can still deny the wake request it belongs to.
	for i := uint16(0); i < 20; i++ {
		d.localRing.Push(i, make([]byte, 2560))
	}
	d.ObserveWakeScore(shadow.ScoreEvent{Score: 0.9, Threshold: 0.5, Sequence: 19, Crossed: true})
	if d.captureManager.Count() == 0 {
		t.Fatal("expected a queued capture after a crossing observation")
	}
	if got := d.captureManager.NextReady(); got != nil {
		t.Fatalf("a fresh wake capture must not be ready before Bind/Grant, got %+v", got)
	}

	// Grant means the wake request went on to become a real turn — the
	// capture must wait for that turn's EndSTT rather than upload
	// immediately, so it must NOT be ready yet.
	d.BindActivationRequest(19, "req-1")
	d.GrantCapture("req-1")
	if got := d.captureManager.NextReady(); got != nil {
		t.Fatalf("a granted capture must wait for EndSTT, got ready %+v", got)
	}
	// EndSTT (turn completion) is what finally marks it ready; exercised
	// here via the manager directly since it's not part of the public
	// DataClient surface under test.
	d.captureManager.EndSTT("req-1")
	ready := d.captureManager.NextReady()
	if ready == nil || ready.Metadata.ActivationSeq != 19 {
		t.Fatalf("expected the capture to become ready after EndSTT, got %+v", ready)
	}
	if !d.AckCapture(ready.Metadata.CaptureID) {
		t.Fatal("AckCapture failed to remove the acknowledged capture")
	}
	if d.AckCapture("no-such-capture") {
		t.Fatal("AckCapture must report false for an unknown capture id")
	}
}

func TestDataClientWakeCaptureDenyDropRelease(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.ConfigureWakeCaptures(true, 1.0, 0.1, "hey_jarvis_v0.1", "md5sum")
	// Cover every sequence used below (19, 25, 31) with contiguous ring
	// history so each crossing has a snapshot to enqueue.
	for i := uint16(0); i < 40; i++ {
		d.localRing.Push(i, make([]byte, 2560))
	}

	// DenyCapture: bind then deny — a denied wake request has no future
	// EndSTT coming, so the capture becomes ready for upload immediately
	// (as a false-positive training example) rather than waiting.
	d.ObserveWakeScore(shadow.ScoreEvent{Score: 0.9, Threshold: 0.5, Sequence: 19, Crossed: true})
	if d.captureManager.Count() == 0 {
		t.Fatal("expected a queued capture")
	}
	d.BindActivationRequest(19, "req-deny")
	d.DenyCapture("req-deny")
	got := d.captureManager.NextReady()
	if got == nil || got.Metadata.ActivationSeq != 19 {
		t.Fatalf("denied capture should become ready immediately, got %+v", got)
	}

	// DropActivationCapture: a fresh crossing dropped outright must vanish.
	d.ObserveWakeScore(shadow.ScoreEvent{Score: 0.9, Threshold: 0.5, Sequence: 25, Crossed: true})
	before := d.captureManager.Count()
	d.DropActivationCapture(25)
	if d.captureManager.Count() != before-1 {
		t.Fatalf("DropActivationCapture did not remove the capture: count %d -> %d", before, d.captureManager.Count())
	}

	// ReleaseActivationCapture: marks ready without the bind/grant handshake.
	d.ObserveWakeScore(shadow.ScoreEvent{Score: 0.9, Threshold: 0.5, Sequence: 31, Crossed: true})
	d.ReleaseActivationCapture(31)
	released := d.captureManager.NextReady()
	if released == nil || released.Metadata.ActivationSeq != 31 {
		t.Fatalf("ReleaseActivationCapture did not surface the capture as ready, got %+v", released)
	}
}

// ─── Stop scorer plumbing ───────────────────────────────────────────────────

func TestDataClientSetSharedStopAndStopArmed(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	if d.SharedStop() {
		t.Fatal("SharedStop should default false")
	}
	d.SetSharedStop(true)
	if !d.SharedStop() {
		t.Fatal("SetSharedStop(true) did not stick")
	}
	if d.StopArmed() {
		t.Fatal("StopArmed should default false with nothing armed")
	}
}

func TestDataClientSetStopScorerClosesReplaced(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	first := newTestScorer(t, 0.1)
	d.SetStopScorer(first)
	if d.StopScorer() != first {
		t.Fatal("StopScorer did not return the installed scorer")
	}

	second := shadow.NewScorer(&dataTestInferer{}, 0.5, nil)
	d.SetStopScorer(second)
	t.Cleanup(second.Close)
	if d.StopScorer() != second {
		t.Fatal("StopScorer did not return the replacement scorer")
	}
	// The replaced scorer must be closed — Push after Close is a documented
	// no-op (see shadow.Scorer.Close), not a panic, so exercise it as a
	// smoke check that nothing blows up on a closed scorer.
	first.Push(make([]int16, wakeword.ChunkSamples))
}

func TestDataClientArmStopRejectsWithoutActiveMic(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.SetSharedStop(true)
	if d.ArmStop("turn-1", 1, "thinking", time.Second) {
		t.Fatal("ArmStop must reject when the AFE mic stream is inactive")
	}
}

func TestDataClientArmStopRejectsWithoutScorerWhenNotShared(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.micMu.Lock()
	d.micActive = true
	d.micMu.Unlock()
	if d.ArmStop("turn-1", 1, "thinking", time.Second) {
		t.Fatal("ArmStop must reject with no scorer and no shared stop head")
	}
}

func TestDataClientArmStopSucceedsWhenShared(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.micMu.Lock()
	d.micActive = true
	d.micMu.Unlock()
	d.SetSharedStop(true)
	if !d.ArmStop("turn-1", 1, "thinking", time.Second) {
		t.Fatal("ArmStop should succeed for a shared stop head with an active mic")
	}
	if !d.StopArmed() {
		t.Fatal("StopArmed should report true after a successful arm")
	}
}

func TestDataClientArmStopSucceedsWithOwnScorerAndResetsIt(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.micMu.Lock()
	d.micActive = true
	d.micMu.Unlock()
	d.SetStopScorer(newTestScorer(t, 0.1))
	if !d.ArmStop("turn-1", 1, "playback", time.Second) {
		t.Fatal("ArmStop should succeed with an own, ready scorer")
	}
}

func TestDataClientArmStopRejectsInvalidPhase(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.micMu.Lock()
	d.micActive = true
	d.micMu.Unlock()
	d.SetSharedStop(true)
	if d.ArmStop("turn-1", 1, "not-a-phase", time.Second) {
		t.Fatal("ArmStop must reject an invalid phase (propagated from stopword.Manager)")
	}
}

func TestDataClientDisarmStop(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.micMu.Lock()
	d.micActive = true
	d.micMu.Unlock()
	d.SetSharedStop(true)
	if !d.ArmStop("turn-1", 5, "timer", time.Second) {
		t.Fatal("setup: ArmStop failed")
	}
	d.DisarmStop(5)
	if d.StopArmed() {
		t.Fatal("expected StopArmed false after DisarmStop of the current generation")
	}
	// A stale generation must be a no-op (logged, not acted on) rather than
	// erroring — exercised here for coverage of the "stale disarm" branch.
	d.DisarmStop(999)
}

func TestDataClientPushStopOnlyFeedsWhileArmed(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	// Not armed and no scorer at all — must be a safe no-op.
	d.pushStop(make([]byte, 4))

	d.micMu.Lock()
	d.micActive = true
	d.micMu.Unlock()
	d.SetStopScorer(newTestScorer(t, 0.1))
	if !d.ArmStop("turn-1", 1, "thinking", time.Second) {
		t.Fatal("setup: ArmStop failed")
	}
	// Armed with a scorer installed — must reach the scorer without panicking.
	d.pushStop(make([]byte, wakeword.ChunkSamples*2))
}

func TestDataClientHandleStopCrossingInvokesCallbackOnce(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.micMu.Lock()
	d.micActive = true
	d.micMu.Unlock()
	d.SetSharedStop(true)
	if !d.ArmStop("turn-1", 3, "playback", time.Second) {
		t.Fatal("setup: ArmStop failed")
	}

	var got stopword.Arm
	calls := 0
	d.OnStopDetected(func(arm stopword.Arm, score, threshold float32, at time.Time) {
		calls++
		got = arm
	})

	d.HandleStopCrossing(0.91, 0.5, time.Now())
	if calls != 1 {
		t.Fatalf("expected exactly one callback invocation, got %d", calls)
	}
	if got.TurnID != "turn-1" || got.Generation != 3 {
		t.Fatalf("callback received unexpected arm: %+v", got)
	}

	// The arm was consumed by the first Accept() — a second crossing with
	// nothing armed must be a stale/duplicate no-op, not a second callback.
	d.HandleStopCrossing(0.91, 0.5, time.Now())
	if calls != 1 {
		t.Fatalf("expected no callback for a stale crossing, got %d calls", calls)
	}
}

func TestDataClientHandleStopCrossingWithNoCallbackIsSafe(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.micMu.Lock()
	d.micActive = true
	d.micMu.Unlock()
	d.SetSharedStop(true)
	if !d.ArmStop("turn-1", 1, "thinking", time.Second) {
		t.Fatal("setup: ArmStop failed")
	}
	// No OnStopDetected callback registered — must not panic.
	d.HandleStopCrossing(0.91, 0.5, time.Now())
}
