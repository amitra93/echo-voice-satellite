package als

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// The threshold policy decides what reaches Home Assistant promptly and what
// waits up to 30s for the stats tick. Too sensitive and a flickering lamp
// floods the control plane; too coarse and "someone turned a light on" is the
// thing it misses.
func TestSignificant(t *testing.T) {
	cases := []struct {
		name          string
		baseline, now int
		want          bool
	}{
		// Measured noise on a still room: 309/311/313/308/312 on consecutive
		// reads, about ±1.5%. None of it may report.
		{"still room drift up", 309, 313, false},
		{"still room drift down", 313, 308, false},

		// The case this exists for.
		{"lamp switched on", 40, 300, true},
		{"lamp switched off", 300, 40, true},

		// Hand over the sensor, measured 309 -> 0.
		{"covered", 309, 0, true},
		{"uncovered", 0, 308, true},

		// Near darkness must not produce infinite ratios: a 2 lux wobble in a
		// dark room is not a room lighting up.
		{"tiny change near zero", 0, 2, false},
		{"tiny change near zero, down", 3, 0, false},

		// Big RELATIVE change but small absolute — still noise-ish.
		{"5 to 9 lux", 5, 9, false},

		// Daylight: 50 lux is invisible against 20000 and must not report,
		// which an absolute threshold would get wrong.
		{"daylight jitter", 20000, 20050, false},
		{"cloud clears", 8000, 20000, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := Significant(c.baseline, c.now); got != c.want {
				t.Fatalf("Significant(%d, %d) = %v, want %v",
					c.baseline, c.now, got, c.want)
			}
		})
	}
}

// Symmetry matters: a light going off should be as reportable as one coming
// on. Comparing against the baseline rather than the new value is what makes
// that true, and it is easy to get backwards.
func TestSignificantIsSymmetricEnough(t *testing.T) {
	if !Significant(300, 40) || !Significant(40, 300) {
		t.Fatal("a lamp must be reportable in both directions")
	}
}

// ── Status reporting (issue #90) ─────────────────────────────────────────────
//
// Two users' Dots reported no ambient light sensor and there was no way to
// tell whether the chip was absent or the driver had not bound, because the
// answer was only ever written to a log file on the device. These pin the
// three verdicts against a fake i2c bus.

// fakeBus builds an i2c tree and points the package at it. Returns the root.
// Also resets the package's cached state, which otherwise leaks between tests
// and makes the second one assert against the first one's scan.
func fakeBus(t *testing.T, devices map[string]bool) {
	t.Helper()
	root := t.TempDir()
	for name, withAttr := range devices {
		dir := filepath.Join(root, name)
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "name"), []byte(name+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		if withAttr {
			if err := os.WriteFile(filepath.Join(dir, "als_lux"), []byte("42\n"), 0o644); err != nil {
				t.Fatal(err)
			}
		}
	}

	mu.Lock()
	path, lastScan, reported = "", time.Time{}, false
	status = Status{Code: StatusUnknown}
	mu.Unlock()

	old := i2cGlob
	i2cGlob = filepath.Join(root, "*", "name")
	t.Cleanup(func() {
		i2cGlob = old
		mu.Lock()
		path, lastScan, reported = "", time.Time{}, false
		status = Status{Code: StatusUnknown}
		mu.Unlock()
	})
}

func TestStatusOK(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2540": true, "tsl2584tsv": false})
	got := Report()
	if got.Code != StatusOK {
		t.Fatalf("code = %q, want %q (detail %q)", got.Code, StatusOK, got.Detail)
	}
	if got.Path == "" {
		t.Fatal("a resolved sensor must report where it was found")
	}
	if !Present() {
		t.Fatal("Present() must be true when the sensor resolves")
	}
}

// Seen must list the WHOLE bus even when the sensor is found early.
//
// The first version returned as soon as it matched, so a working device
// reported only the names sorting before tsl2540 — on real hardware that
// dropped is31fl3236, tlv320aic32x4 and bq24297. Comparing a healthy bus
// against a broken one is what this field is for, so a truncated list from
// the healthy side defeats the purpose. The fixtures missed it because none
// had a device sorting after the match; `zz_after` is here to guarantee one
// always does.
func TestSeenListsWholeBusWhenSensorFound(t *testing.T) {
	fakeBus(t, map[string]bool{
		"aa_before": false,
		"tsl2540":   true,
		"zz_after":  false,
	})
	got := Report()
	if got.Code != StatusOK {
		t.Fatalf("code = %q, want %q", got.Code, StatusOK)
	}
	if len(got.Seen) != 3 {
		t.Fatalf("Seen = %v, want all three bus devices", got.Seen)
	}
	var sawAfter bool
	for _, s := range got.Seen {
		if s == "zz_after" {
			sawAfter = true
		}
	}
	if !sawAfter {
		t.Fatalf("Seen = %v, missing the device that sorts after the sensor", got.Seen)
	}
}

// The hypothesis for #90: these units carry the second ALS, which is present
// on working devices too but has no usable driver, and not the tsl2540.
func TestStatusNoChip(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2584tsv": false, "tlv320aic3101": false})
	got := Report()
	if got.Code != StatusNoChip {
		t.Fatalf("code = %q, want %q", got.Code, StatusNoChip)
	}
	// Seen is what makes the answer verifiable rather than merely asserted,
	// and identifies an unfamiliar revision the first time one appears.
	if len(got.Seen) != 2 {
		t.Fatalf("Seen = %v, want both bus devices", got.Seen)
	}
	if Present() {
		t.Fatal("Present() must be false with no tsl2540")
	}
}

// Distinct from no_chip because the fixes are opposite: this one is ours.
func TestStatusNoAttribute(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2540": false})
	got := Report()
	if got.Code != StatusNoAttribute {
		t.Fatalf("code = %q, want %q", got.Code, StatusNoAttribute)
	}
	if got.Detail == "" {
		t.Fatal("a failure must carry a detail a human can read in a bundle")
	}
}

// The absence LOG is deliberately once-only; the status must not be, or a
// device whose bus changed would keep reporting its first answer forever.
func TestStatusRefreshesAcrossScans(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2584tsv": false})
	if got := Report(); got.Code != StatusNoChip {
		t.Fatalf("first scan: code = %q, want %q", got.Code, StatusNoChip)
	}
	mu.Lock()
	lastScan = time.Time{} // allow an immediate re-scan
	mu.Unlock()
	if got := Report(); got.Code != StatusNoChip {
		t.Fatalf("second scan: code = %q, want %q", got.Code, StatusNoChip)
	}
}

func TestLuxReadsValidValueAndReturnsNilForBadReads(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2540": true})
	p := Report().Path
	if got := Lux(); got == nil || *got != 42 {
		t.Fatalf("valid Lux() = %v, want 42", got)
	}
	if err := os.WriteFile(p, []byte("not-a-number\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := Lux(); got != nil {
		t.Fatalf("malformed Lux() = %d, want nil", *got)
	}
	if err := os.Remove(p); err != nil {
		t.Fatal(err)
	}
	if got := Lux(); got != nil {
		t.Fatalf("missing Lux() = %d, want nil", *got)
	}
}

func TestLuxIsNilWithoutSensor(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2540": false})
	if got := Lux(); got != nil {
		t.Fatalf("Lux() without a readable sensor = %d, want nil", *got)
	}
}

func TestWatchReturnsWithoutSensor(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2540": false})
	Watch(context.Background(), func(int) { t.Fatal("callback fired without a sensor") })
}

// ─── Watch's polling loop ───────────────────────────────────────────────────
//
// These drive Watch through presentFunc/luxFunc rather than fakeBus: the loop
// itself (baseline seeding, the Significant/MinInterval gates, dispatch) has
// nothing to do with how the sensor is read, and PollInterval/MinInterval
// are vars specifically so this doesn't have to wait on the real ~1s/2s
// production cadence.

func intp(v int) *int { return &v }

// runWatch feeds readings to Watch one per poll (holding at the last
// reading once the list is exhausted, so a poll that outruns the fixture
// doesn't panic), lets it run for window, then stops it and returns every
// onChange call observed in order.
func runWatch(t *testing.T, readings []*int, pollInterval, minInterval, window time.Duration) []int {
	t.Helper()
	oldPresent, oldLux := presentFunc, luxFunc
	oldPoll, oldMinInterval := PollInterval, MinInterval
	PollInterval, MinInterval = pollInterval, minInterval
	t.Cleanup(func() {
		presentFunc, luxFunc = oldPresent, oldLux
		PollInterval, MinInterval = oldPoll, oldMinInterval
	})

	var mu sync.Mutex
	i := 0
	presentFunc = func() bool { return true }
	luxFunc = func() *int {
		mu.Lock()
		defer mu.Unlock()
		if i >= len(readings) {
			return readings[len(readings)-1]
		}
		v := readings[i]
		i++
		return v
	}

	var calls []int
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		Watch(ctx, func(v int) {
			mu.Lock()
			calls = append(calls, v)
			mu.Unlock()
		})
		close(done)
	}()
	time.Sleep(window)
	cancel()
	<-done

	mu.Lock()
	defer mu.Unlock()
	return append([]int(nil), calls...)
}

// The first reading has nothing to compare against — it can only seed the
// baseline. Reporting it would tell the controller a light "changed" the
// instant the device booted.
func TestWatchSeedsBaselineWithoutReporting(t *testing.T) {
	calls := runWatch(t, []*int{intp(300)}, time.Millisecond, time.Second, 30*time.Millisecond)
	if len(calls) != 0 {
		t.Fatalf("Watch reported on a steady baseline: %v", calls)
	}
}

func TestWatchIgnoresAnInsignificantChange(t *testing.T) {
	// 310 vs 300 is a 3.3% change — well under MinRatio.
	calls := runWatch(t, []*int{intp(300), intp(310)}, time.Millisecond, time.Second, 30*time.Millisecond)
	if len(calls) != 0 {
		t.Fatalf("Watch reported an insignificant change: %v", calls)
	}
}

func TestWatchReportsASignificantChange(t *testing.T) {
	// 500 vs 300 is a 66% change — comfortably over MinRatio.
	calls := runWatch(t, []*int{intp(300), intp(500)}, time.Millisecond, time.Second, 30*time.Millisecond)
	if len(calls) != 1 || calls[0] != 500 {
		t.Fatalf("calls = %v, want [500]", calls)
	}
}

// A nil reading (sensor read failed mid-poll) must be skipped, not treated
// as a reading of zero lux — that would read as "went completely dark" and
// could fire on every transient I2C hiccup.
func TestWatchSkipsANilReading(t *testing.T) {
	calls := runWatch(t, []*int{intp(300), nil, nil, intp(500)}, time.Millisecond, time.Second, 30*time.Millisecond)
	if len(calls) != 1 || calls[0] != 500 {
		t.Fatalf("calls = %v, want [500] (nil reads skipped, not reported as 0)", calls)
	}
}

// A flickering light must not flood the control plane: two significant
// changes closer together than MinInterval collapse to one report.
func TestWatchSuppressesARepeatWithinMinInterval(t *testing.T) {
	calls := runWatch(t, []*int{intp(300), intp(500), intp(800)},
		time.Millisecond, 500*time.Millisecond, 30*time.Millisecond)
	if len(calls) != 1 || calls[0] != 500 {
		t.Fatalf("calls = %v, want only [500] — 800 arrives well inside MinInterval", calls)
	}
}

// The same second change, given enough real time to clear MinInterval, must
// still be reported — suppression is a delay, not a permanent silence.
func TestWatchReportsAfterMinIntervalClears(t *testing.T) {
	calls := runWatch(t, []*int{intp(300), intp(500), intp(800)},
		time.Millisecond, 20*time.Millisecond, 100*time.Millisecond)
	if len(calls) != 2 || calls[0] != 500 || calls[1] != 800 {
		t.Fatalf("calls = %v, want [500 800]", calls)
	}
}
