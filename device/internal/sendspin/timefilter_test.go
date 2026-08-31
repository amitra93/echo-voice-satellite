package sendspin

import (
	"encoding/json"
	"math"
	"os"
	"testing"
)

type tfStep struct {
	Measurement int64   `json:"measurement"`
	MaxError    int64   `json:"max_error"`
	TimeAdded   int64   `json:"time_added"`
	Offset      float64 `json:"offset"`
	Count       int     `json:"count"`
	Synced      bool    `json:"synced"`
	Error       int64   `json:"error"`
	CS          int64   `json:"cs"`
	CC          int64   `json:"cc"`
}

// TestTimeFilterMatchesPythonReference replays the exact input sequence the
// Python aiosendspin.SendspinTimeFilter was fed (testdata generated from it)
// and asserts the Go port agrees on every observable after every update:
// the rounded conversions must be exact, the float offset within a hair.
func TestTimeFilterMatchesPythonReference(t *testing.T) {
	raw, err := os.ReadFile("testdata/timefilter_fixture.json")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var fixture struct {
		Steps []tfStep `json:"steps"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatalf("parse fixture: %v", err)
	}
	if len(fixture.Steps) < 100 {
		t.Fatalf("fixture too short (%d steps) — must cross the adaptive-forgetting threshold", len(fixture.Steps))
	}

	f := NewTimeFilter()
	for i, s := range fixture.Steps {
		f.Update(s.Measurement, s.MaxError, s.TimeAdded)

		if f.Count() != s.Count {
			t.Fatalf("step %d: count=%d want %d", i, f.Count(), s.Count)
		}
		if f.IsSynchronized() != s.Synced {
			t.Fatalf("step %d: synced=%v want %v", i, f.IsSynchronized(), s.Synced)
		}
		if f.Error() != s.Error {
			t.Fatalf("step %d: error=%d want %d", i, f.Error(), s.Error)
		}
		// Offset is float; the ordered port should be bit-identical, but allow
		// a sub-microsecond epsilon relative to scale.
		if d := math.Abs(f.Offset() - s.Offset); d > 1e-3 {
			t.Fatalf("step %d: offset=%.6f want %.6f (|d|=%.6g)", i, f.Offset(), s.Offset, d)
		}
		if got := f.ComputeServerTime(s.TimeAdded); got != s.CS {
			t.Fatalf("step %d: compute_server_time=%d want %d", i, got, s.CS)
		}
		if got := f.ComputeClientTime(s.Measurement + s.TimeAdded); got != s.CC {
			t.Fatalf("step %d: compute_client_time=%d want %d", i, got, s.CC)
		}
	}
}

func TestTimeFilterResetAndNonMonotonic(t *testing.T) {
	f := NewTimeFilter()
	f.Update(100, 3000, 1_000_000)
	f.Update(200, 3000, 2_000_000)
	if !f.IsSynchronized() {
		t.Fatal("expected synchronized after two updates")
	}
	// Non-monotonic timeAdded must be ignored (no state change, no panic).
	before := f.Offset()
	f.Update(999, 3000, 1_500_000)
	if f.Offset() != before {
		t.Fatalf("non-monotonic update changed offset: %v -> %v", before, f.Offset())
	}
	f.Reset()
	if f.Count() != 0 || f.IsSynchronized() {
		t.Fatalf("reset did not clear state: count=%d synced=%v", f.Count(), f.IsSynchronized())
	}
	if !math.IsInf(f.offsetCovariance, 1) {
		t.Fatal("reset did not restore infinite offset covariance")
	}
}

func TestTimeFilterFirstMeasurementBaseline(t *testing.T) {
	f := NewTimeFilter()
	f.Update(492_000_000, 3000, 5_000_000)
	if f.Count() != 1 || f.IsSynchronized() {
		t.Fatalf("after one update: count=%d synced=%v (want 1, false)", f.Count(), f.IsSynchronized())
	}
	// With one measurement, compute_server_time applies the baseline offset.
	if got := f.ComputeServerTime(5_000_000); got != 5_000_000+492_000_000 {
		t.Fatalf("baseline compute_server_time=%d want %d", got, 5_000_000+492_000_000)
	}
}
