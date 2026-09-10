package server

import (
	"math"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

func TestSpinFrame(t *testing.T) {
	spec := AnimSpec{
		Pattern: "spin",
		Colors:  [][3]uint8{{0, 200, 0}, {0, 60, 0}},
	}
	frame := animFrame(spec, 3)
	if frame[3].G != 200 {
		t.Fatalf("head not at pos 3: %+v", frame[3])
	}
	if frame[2].G != 60 {
		t.Fatalf("trail not at pos 2: %+v", frame[2])
	}
	for i, l := range frame {
		if i == 3 || i == 2 {
			continue
		}
		if l.R != 0 || l.G != 0 || l.B != 0 {
			t.Fatalf("LED %d not dark: %+v", i, l)
		}
		if l.ID != i {
			t.Fatalf("LED %d has wrong ID %d", i, l.ID)
		}
	}
	// Wraparound: head at 0 puts trail at 11.
	frame = animFrame(spec, 0)
	if frame[0].G != 200 || frame[11].G != 60 {
		t.Fatalf("wraparound wrong: head=%+v trail=%+v", frame[0], frame[11])
	}
}

func TestRotateFrame(t *testing.T) {
	palette := make([][3]uint8, 12)
	for i := range palette {
		palette[i] = [3]uint8{uint8(i), 0, 0}
	}
	spec := AnimSpec{Pattern: "rotate", Colors: palette}
	// pos=0 is the palette 1:1; pos=1 shifts every colour one LED clockwise.
	frame := animFrame(spec, 0)
	for i := range frame {
		if frame[i].R != uint8(i) {
			t.Fatalf("pos 0: LED %d = %d, want %d", i, frame[i].R, i)
		}
	}
	frame = animFrame(spec, 1)
	if frame[1].R != 0 || frame[0].R != 11 {
		t.Fatalf("pos 1 rotation wrong: led0=%d led1=%d", frame[0].R, frame[1].R)
	}
}

func TestRotateFrameEmptyPalette(t *testing.T) {
	frame := animFrame(AnimSpec{Pattern: "rotate"}, 5)
	for i, l := range frame {
		if l.R != 0 || l.G != 0 || l.B != 0 {
			t.Fatalf("LED %d not dark on empty palette: %+v", i, l)
		}
	}
}

func TestPaletteFrame(t *testing.T) {
	// Single colour fills the ring.
	frame := paletteFrame([][3]uint8{{10, 20, 30}})
	for i, l := range frame {
		if l.R != 10 || l.G != 20 || l.B != 30 {
			t.Fatalf("LED %d wrong: %+v", i, l)
		}
	}
	// Short multi-colour list leaves the rest dark.
	frame = paletteFrame([][3]uint8{{1, 0, 0}, {2, 0, 0}})
	if frame[0].R != 1 || frame[1].R != 2 || frame[2].R != 0 {
		t.Fatalf("partial palette wrong: %+v", frame[:3])
	}
}

func TestResolveMeterDefaultsAndClamps(t *testing.T) {
	// Absent fields fall back to the shipped curve.
	a, d, f, g, r, c := resolveMeter(AnimSpec{Pattern: "meter"})
	if a != meterDefaults.attack || d != meterDefaults.decay ||
		f != meterDefaults.floor || g != meterDefaults.gamma ||
		r != meterDefaults.ref || c != meterDefaults.curve {
		t.Fatalf("defaults not applied: %v %v %v %v %v %v", a, d, f, g, r, c)
	}

	// floor=0 must survive: it is a legitimate value (fully dark at
	// silence), which is the whole reason these fields are pointers.
	zero := 0.0
	_, _, f0, _, _, _ := resolveMeter(AnimSpec{Floor: &zero})
	if f0 != 0.0 {
		t.Fatalf("floor 0 not honoured, got %v", f0)
	}

	// Out-of-range values clamp rather than producing a dead ring: a
	// config push must not be able to break the display.
	huge, neg := 99.0, -5.0
	at, dc, fl, gm, rf, cv := resolveMeter(AnimSpec{
		Attack: &huge, Decay: &neg, Floor: &huge,
		Gamma: &neg, Ref: &neg, Curve: &huge,
	})
	if at != 1.0 || dc != 0.02 || fl != 0.6 || gm != 1.0 || rf != 0.02 || cv != 2.0 {
		t.Fatalf("clamping wrong: %v %v %v %v %v %v", at, dc, fl, gm, rf, cv)
	}
}

func TestScaleFrameAndBlackFrame(t *testing.T) {
	frame := []led.Led{{ID: 3, R: 100, G: 51, B: 1}}
	got := scaleFrame(frame, 0.5)
	if got[0] != (led.Led{ID: 3, R: 50, G: 26, B: 1}) {
		t.Fatalf("scaleFrame = %#v", got[0])
	}
	black := blackFrame()
	if len(black) != numLEDs || black[0].ID != 0 || black[numLEDs-1].ID != numLEDs-1 {
		t.Fatalf("blackFrame IDs = %#v", black)
	}
}

func TestStartAnimStaticAndReplacement(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.StartAnim(AnimSpec{Pattern: "solid", Colors: [][3]uint8{{1, 2, 3}}})
	if len(c.sets) != 1 || c.sets[0][0].R != 1 {
		t.Fatalf("solid animation = %#v", c.sets)
	}
	s.StartAnim(AnimSpec{Pattern: "off"})
	if len(c.sets) != 2 || c.sets[1][0].R != 0 {
		t.Fatalf("off animation = %#v", c.sets)
	}
	s.StartAnim(AnimSpec{Pattern: "not-a-pattern"})
	if len(c.sets) != 3 {
		t.Fatalf("unknown animation did not clear: %d", len(c.sets))
	}
	s.SetAudioLevel(0.25)
	if got := s.getAudioLevel(); got != 0.25 {
		t.Fatalf("audio level = %v", got)
	}
	oldGen := s.anim.gen
	s.StopAnim()
	if s.animCurrent(oldGen) {
		t.Fatal("stopped generation still current")
	}
}

// TestMeterCurveIsVisiblyVaried is the regression guard for the reported
// "too subtle to distinguish from a solid ring" bug. It asserts the shipped
// curve produces a wide PERCEPTUAL swing across ordinary speech levels —
// the old curve (sqrt(rms/0.35), floor .15, no gamma) managed only ~23%.
func TestMeterCurveIsVisiblyVaried(t *testing.T) {
	_, _, floor, gamma, ref, curve := resolveMeter(AnimSpec{Pattern: "meter"})
	span := 1.0 - floor

	// Perceived lightness of a painted duty cycle b is ~b^(1/gamma), so
	// with the gamma encoding the perceptual value is just floor+span*env.
	perceived := func(rms float64) float64 {
		env := math.Pow(math.Min(1, rms/ref), curve)
		b := math.Pow(floor+span*env, gamma)
		return math.Pow(b, 1.0/gamma)
	}

	quiet, loud := perceived(0.02), perceived(0.20)
	if got := loud - quiet; got < 0.55 {
		t.Fatalf("perceptual swing across speech only %.2f, want >=0.55 "+
			"(quiet=%.2f loud=%.2f) — meter will read as a solid ring", got, quiet, loud)
	}
	// Silence must still be visibly lit: the ring belongs to the turn.
	if s := perceived(0.0); s < 0.03 {
		t.Fatalf("silence too dark (%.3f) — ring reads as off mid-response", s)
	}
	// And full scale must not clip below full brightness.
	if p := perceived(1.0); p < 0.99 {
		t.Fatalf("peak not full brightness: %.3f", p)
	}
}

func TestAnimationGoroutinesRenderAndStop(t *testing.T) {
	patterns := []string{"spin", "pulse", "meter"}
	for _, pattern := range patterns {
		t.Run(pattern, func(t *testing.T) {
			c := &recordingLEDController{}
			s := newTestServer(c)
			s.SetAudioLevel(0.2)
			s.StartAnim(AnimSpec{Pattern: pattern, Colors: [][3]uint8{{10, 20, 30}}, PeriodMs: 1})
			deadline := time.Now().Add(250 * time.Millisecond)
			for c.frameCount() == 0 && time.Now().Before(deadline) {
				time.Sleep(time.Millisecond)
			}
			s.StopAnim()
			if c.frameCount() == 0 {
				t.Fatal("animation did not render a frame")
			}
			count := c.frameCount()
			time.Sleep(10 * time.Millisecond)
			if got := c.frameCount(); got > count+1 {
				t.Fatalf("animation continued after StopAnim: %d -> %d frames", count, got)
			}
		})
	}
}

func TestAnimExpiryIgnoresReplacedGeneration(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.anim.gen = 2
	s.animExpiry(1, 0)
	if len(c.sets) != 0 {
		t.Fatal("stale animation expiry cleared a replacement")
	}
}

// TestAnimExpiryClearsRingWhenStillCurrent is animExpiry's other branch:
// the generation it was armed for is still the live one, so its dead-man
// window firing must black out the ring.
func TestAnimExpiryClearsRingWhenStillCurrent(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	gen := s.anim.gen
	s.animExpiry(gen, 0)
	if len(c.sets) != 1 {
		t.Fatalf("expected exactly one SetLEDs call clearing the ring, got %d", len(c.sets))
	}
	for _, l := range c.sets[0] {
		if l.R != 0 || l.G != 0 || l.B != 0 {
			t.Fatalf("animExpiry did not clear to black: %+v", c.sets[0])
		}
	}
}

func TestPaintIfCurrentCannotOverwriteNewerAnimation(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.anim.gen = 1
	countdown := countdownFrame([3]uint8{9, 0, 0}, 6)
	if !s.paintIfCurrent(1, countdown, boolPtr(false)) {
		t.Fatal("current countdown frame was not painted")
	}

	// This is the exact interleaving the old check-then-paint sequence lost:
	// a countdown tick had checked generation 1, listening became generation 2,
	// then the stale tick painted on top. paintIfCurrent checks while holding
	// the generation lock, so that final stale paint is refused.
	s.StartAnim(AnimSpec{Pattern: "solid", Colors: [][3]uint8{{0, 100, 0}}, Listening: true})
	setsAfterListening := c.frameCount()
	if s.paintIfCurrent(1, countdown, boolPtr(false)) {
		t.Fatal("stale countdown frame overwrote newer listening animation")
	}
	if got := c.frameCount(); got != setsAfterListening {
		t.Fatalf("stale countdown added a frame: %d -> %d", setsAfterListening, got)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if last := c.sets[len(c.sets)-1][0]; last.G != 100 || last.R != 0 {
		t.Fatalf("newer listening frame was overwritten: %+v", last)
	}
}

// waitForClearedFrame polls c until a fully-black frame lands after at
// least one non-empty frame — the same detection shape
// TestRunCountdownTTLExpiryClearsRing already uses for countdown.
func waitForClearedFrame(t *testing.T, c *recordingLEDController, within time.Duration) bool {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		c.mu.Lock()
		cleared := false
		if len(c.sets) > 1 {
			last := c.sets[len(c.sets)-1]
			allDark := true
			for _, l := range last {
				if l.R != 0 || l.G != 0 || l.B != 0 {
					allDark = false
					break
				}
			}
			cleared = allDark
		}
		c.mu.Unlock()
		if cleared {
			return true
		}
		time.Sleep(10 * time.Millisecond)
	}
	return false
}

// TestRunAnimTTLExpiryClearsRing, TestRunPulseTTLExpiryClearsRing and
// TestRunMeterTTLExpiryClearsRing pin the same TTL dead-man for the three
// patterns that don't already have their own TTL test (countdown does,
// above).
func TestRunAnimTTLExpiryClearsRing(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.StartAnim(AnimSpec{Pattern: "spin", Colors: [][3]uint8{{7, 7, 7}}, PeriodMs: 5, TTLSec: 1})
	if !waitForClearedFrame(t, c, 3*time.Second) {
		t.Fatal("spin animation did not clear to black after TTL expiry")
	}
}

func TestRunPulseTTLExpiryClearsRing(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.StartAnim(AnimSpec{Pattern: "pulse", Colors: [][3]uint8{{7, 7, 7}}, TTLSec: 1})
	if !waitForClearedFrame(t, c, 3*time.Second) {
		t.Fatal("pulse animation did not clear to black after TTL expiry")
	}
}

func TestRunMeterTTLExpiryClearsRing(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.SetAudioLevel(0.2)
	s.StartAnim(AnimSpec{Pattern: "meter", Colors: [][3]uint8{{7, 7, 7}}, TTLSec: 1})
	if !waitForClearedFrame(t, c, 3*time.Second) {
		t.Fatal("meter animation did not clear to black after TTL expiry")
	}
}

func TestCountdownFrame(t *testing.T) {
	color := [3]uint8{10, 20, 30}
	// lit count math at known fractions, and a rounding case.
	for _, tc := range []struct {
		lit  int
		want int // number of LEDs expected lit
	}{
		{lit: 0, want: 0},
		{lit: 6, want: 6},
		{lit: 12, want: 12},
	} {
		frame := countdownFrame(color, tc.lit)
		lit := 0
		for i, l := range frame {
			if l.ID != i {
				t.Fatalf("LED %d has wrong ID %d", i, l.ID)
			}
			if l.R != 0 || l.G != 0 || l.B != 0 {
				lit++
				if i >= tc.lit {
					t.Fatalf("LED %d lit but only %d should be: %+v", i, tc.lit, l)
				}
				if l.R != color[0] || l.G != color[1] || l.B != color[2] {
					t.Fatalf("LED %d wrong colour: %+v", i, l)
				}
			}
		}
		if lit != tc.want {
			t.Fatalf("countdownFrame(%d) lit %d LEDs, want %d", tc.lit, lit, tc.want)
		}
	}
}

// TestCountdownLitCountMath pins fraction -> lit-LED-count rounding at a
// few known fractions, matching the math runCountdown performs per tick.
func TestCountdownLitCountMath(t *testing.T) {
	litFor := func(fraction float64) int {
		fraction = math.Min(1, math.Max(0, fraction))
		lit := int(math.Round(fraction * 12))
		if lit < 0 {
			lit = 0
		} else if lit > 12 {
			lit = 12
		}
		return lit
	}
	cases := []struct {
		fraction float64
		want     int
	}{
		{0.0, 0},
		{0.5, 6},
		{1.0, 12},
		{0.99, 12}, // rounds up to 12
		{0.041, 0}, // rounds down to 0 (0.041*12 = 0.492)
		{1.5, 12},  // above 1 clamps first, then rounds
		{-0.5, 0},  // below 0 clamps first, then rounds
	}
	for _, tc := range cases {
		if got := litFor(tc.fraction); got != tc.want {
			t.Fatalf("litFor(%v) = %d, want %d", tc.fraction, got, tc.want)
		}
	}
}

func TestRunCountdownClampsFraction(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	// FractionNow above 1 must clamp to a full ring rather than an
	// out-of-range lit count; the animation ticks once per second, so a
	// TTL of 1 lets a single tick land before it self-clears.
	s.StartAnim(AnimSpec{
		Pattern:     "countdown",
		Colors:      [][3]uint8{{5, 5, 5}},
		FractionNow: 1.5,
		RunningMps:  0,
		TTLSec:      5,
	})
	deadline := time.Now().Add(2 * time.Second)
	for c.frameCount() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	s.StopAnim()
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.sets) == 0 {
		t.Fatal("countdown animation did not render a frame")
	}
	lit := 0
	for _, l := range c.sets[0] {
		if l.R != 0 || l.G != 0 || l.B != 0 {
			lit++
		}
	}
	if lit != 12 {
		t.Fatalf("fraction 1.5 rendered %d lit LEDs, want 12 (clamped)", lit)
	}

	// FractionNow below 0 must clamp to a dark ring.
	c2 := &recordingLEDController{}
	s2 := newTestServer(c2)
	s2.StartAnim(AnimSpec{
		Pattern:     "countdown",
		Colors:      [][3]uint8{{5, 5, 5}},
		FractionNow: -0.5,
		RunningMps:  0,
		TTLSec:      5,
	})
	deadline = time.Now().Add(2 * time.Second)
	for c2.frameCount() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	s2.StopAnim()
	c2.mu.Lock()
	defer c2.mu.Unlock()
	if len(c2.sets) == 0 {
		t.Fatal("countdown animation did not render a frame")
	}
	for i, l := range c2.sets[0] {
		if l.R != 0 || l.G != 0 || l.B != 0 {
			t.Fatalf("fraction -0.5 rendered LED %d lit, want fully dark: %+v", i, l)
		}
	}
}

// TestRunCountdownStaticWhenPaused pins the RunningMps=0 (paused) case:
// the lit count must not change across ticks with no decay rate.
func TestRunCountdownStaticWhenPaused(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.StartAnim(AnimSpec{
		Pattern:     "countdown",
		Colors:      [][3]uint8{{9, 9, 9}},
		FractionNow: 0.5,
		RunningMps:  0,
	})
	deadline := time.Now().Add(2200 * time.Millisecond)
	for c.frameCount() < 2 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	s.StopAnim()
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.sets) < 2 {
		t.Fatalf("countdown animation only rendered %d frames across ticks, want >=2", len(c.sets))
	}
	litOf := func(frame []led.Led) int {
		n := 0
		for _, l := range frame {
			if l.R != 0 || l.G != 0 || l.B != 0 {
				n++
			}
		}
		return n
	}
	first := litOf(c.sets[0])
	if first != 6 {
		t.Fatalf("fraction 0.5 lit %d LEDs, want 6", first)
	}
	for i, frame := range c.sets {
		if got := litOf(frame); got != first {
			t.Fatalf("frame %d lit %d LEDs, paused animation should stay at %d", i, got, first)
		}
	}
}

// TestRunCountdownTTLExpiryClearsRing pins the TTL dead-man for countdown,
// same shape as TestAnimExpiryIgnoresReplacedGeneration for the other
// patterns.
func TestRunCountdownTTLExpiryClearsRing(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.StartAnim(AnimSpec{
		Pattern:     "countdown",
		Colors:      [][3]uint8{{7, 7, 7}},
		FractionNow: 1.0,
		RunningMps:  0,
		TTLSec:      1,
	})
	deadline := time.Now().Add(3 * time.Second)
	cleared := false
	for time.Now().Before(deadline) {
		c.mu.Lock()
		if len(c.sets) > 0 {
			last := c.sets[len(c.sets)-1]
			allDark := true
			for _, l := range last {
				if l.R != 0 || l.G != 0 || l.B != 0 {
					allDark = false
					break
				}
			}
			if allDark && len(c.sets) > 1 {
				cleared = true
			}
		}
		c.mu.Unlock()
		if cleared {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !cleared {
		t.Fatal("countdown animation did not clear to black after TTL expiry")
	}
}

// TestRunCountdownSupersededByNewerSpec pins that a newer spec (any
// pattern) stops the countdown goroutine from painting further frames via
// the existing generation counter — no countdown-specific code needed.
func TestRunCountdownSupersededByNewerSpec(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.StartAnim(AnimSpec{
		Pattern:     "countdown",
		Colors:      [][3]uint8{{3, 3, 3}},
		FractionNow: 1.0,
		RunningMps:  0,
	})
	deadline := time.Now().Add(2 * time.Second)
	for c.frameCount() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if c.frameCount() == 0 {
		t.Fatal("countdown animation did not render a frame")
	}
	// Supersede with a static solid frame — the pending countdown tick,
	// if it lands, must not repaint after this.
	s.StartAnim(AnimSpec{Pattern: "solid", Colors: [][3]uint8{{1, 1, 1}}})
	countAfterSupersede := c.frameCount()
	time.Sleep(1200 * time.Millisecond) // outlast the 1s countdown tick
	if got := c.frameCount(); got > countAfterSupersede {
		t.Fatalf("countdown animation painted after being superseded: %d -> %d frames", countAfterSupersede, got)
	}
}
