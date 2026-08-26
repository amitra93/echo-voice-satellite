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
			for len(c.sets) == 0 && time.Now().Before(deadline) {
				time.Sleep(time.Millisecond)
			}
			s.StopAnim()
			if len(c.sets) == 0 {
				t.Fatal("animation did not render a frame")
			}
			count := len(c.sets)
			time.Sleep(10 * time.Millisecond)
			if len(c.sets) > count+1 {
				t.Fatalf("animation continued after StopAnim: %d -> %d frames", count, len(c.sets))
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
