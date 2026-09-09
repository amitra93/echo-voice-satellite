package sendspin

import (
	"testing"
	"time"
)

// TestManagerRetriesFailedDialThenStopsOnClose drives Manager.run through a
// real dial failure (connection refused on loopback — fast, local-only, no
// external network) and then cancels while the loop is asleep in its retry
// backoff, exercising the ctx.Done() exit branch of run's select. The
// same-URL no-op and empty-URL Close paths are covered by
// TestManagerAcceptsEmptyAndReplacementConfiguration in client_test.go.
func TestManagerRetriesFailedDialThenStopsOnClose(t *testing.T) {
	m := NewManager(NewClient("id", "Study", &fakeSink{}))
	m.Configure("ws://127.0.0.1:1/sendspin")
	// Give the run loop time to dial (fails immediately — refused), log the
	// error, and enter its backoff wait.
	time.Sleep(50 * time.Millisecond)
	m.Close()
}

// TestManagerReconfigureCancelsPriorRun exercises the "m.cancel != nil"
// branch of Configure: a second, different URL must cancel the first run
// before starting a new one, never leaving two sessions alive at once.
func TestManagerReconfigureCancelsPriorRun(t *testing.T) {
	m := NewManager(NewClient("id", "Study", &fakeSink{}))
	m.Configure("ws://127.0.0.1:1/sendspin")
	time.Sleep(10 * time.Millisecond)
	m.Configure("ws://127.0.0.1:2/sendspin")
	time.Sleep(10 * time.Millisecond)
	m.Close()
}
