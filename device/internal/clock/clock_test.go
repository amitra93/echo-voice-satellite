package clock

import "testing"

// NowUs's origin is process start, not wall time (package comment), so the
// only properties worth pinning are: it's monotonic and it's in
// microseconds, not some other unit.
func TestNowUsMonotonic(t *testing.T) {
	a := NowUs()
	b := NowUs()
	if b < a {
		t.Fatalf("NowUs went backwards: %d then %d", a, b)
	}
}

func TestNowUsNonNegative(t *testing.T) {
	// processStart is set at package init, before this test can possibly
	// run, so NowUs() must never be negative.
	if NowUs() < 0 {
		t.Fatalf("NowUs returned a negative value")
	}
}
