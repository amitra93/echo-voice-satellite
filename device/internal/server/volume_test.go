package server

import (
	"github.com/wilbowes/EchoMuse/pkg/led"
	"testing"
	"time"
)

// fakeLEDController records what it was asked to paint — enough to observe
// showLEDs actually running, unlike passing a nil led.Controller (showLEDs
// returns before touching displayActive when its getter yields nil).
type fakeLEDController struct{ setCalls int }

func (f *fakeLEDController) Init() error              { return nil }
func (f *fakeLEDController) GetNumLEDs() (int, error) { return numLEDs, nil }
func (f *fakeLEDController) SetLEDs(leds ...led.Led) error {
	f.setCalls++
	return nil
}

// SetVolume (the controller/HA remote-command path) used to pass showRing
// false — "nobody is at the device" — which was true for an automation but
// not for someone dragging the HA slider. It now shows the ring for every
// remote set, the same as a physical button press.
func TestSetVolumeShowsTheRing(t *testing.T) {
	fake := &fakeLEDController{}
	vc := newVolumeController(func() led.Controller { return fake })
	s := &Server{volume: vc}

	s.SetVolume(80)

	if !vc.DisplayActive() {
		t.Fatal("SetVolume must show the volume ring — it is always a " +
			"deliberate remote action (someone moved the slider), not an " +
			"unattended background change")
	}
	if fake.setCalls == 0 {
		t.Fatal("SetVolume must actually paint the ring, not just flag it active")
	}
}

// SeedVolume (the boot-time restore) is the one call site that must stay
// silent — nobody asked for it, it just runs on every connect/config push.
func TestSeedVolumeStaysSilent(t *testing.T) {
	fake := &fakeLEDController{}
	vc := newVolumeController(func() led.Controller { return fake })
	s := &Server{volume: vc}

	s.SeedVolume(80)

	if vc.DisplayActive() {
		t.Fatal("SeedVolume must not show the ring — it fires on every " +
			"boot/reconnect regardless of whether anyone touched the volume")
	}
	if fake.setCalls != 0 {
		t.Fatal("SeedVolume must not paint the ring")
	}
}

// A deliberate button press must outrank the volume arc's 2s hold. Before
// this, adjusting volume then immediately pressing the action button left
// the arc owning the ring for the remainder of its window, so the device
// gave no sign it had started listening.
func TestCancelDisplayReleasesTheRing(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })

	vc.mu.Lock()
	vc.displayActive = true
	vc.timer = time.AfterFunc(volumeLEDSecs*time.Second, func() {})
	vc.mu.Unlock()

	if !vc.DisplayActive() {
		t.Fatal("precondition: arc should own the ring")
	}

	vc.CancelDisplay()

	if vc.DisplayActive() {
		t.Fatal("arc still owns the ring after CancelDisplay — a listening " +
			"frame would be recorded but not painted")
	}
	// Idempotent: a second press must not panic on the already-stopped timer.
	vc.CancelDisplay()
}

// tinymix ctl 61 spans 0..175, but 127 is the codec's 0dB. Above it the DAC
// applies positive digital gain to near-full-scale PCM and saturates —
// measured on hardware at 65% THD by index 153, 89% by 170, with the output
// level flat from 153 up because it had stopped getting louder. Stock FireOS
// never writes this control at all. If this constant creeps back toward 175,
// the garbling above ~73% volume returns.
func TestVolumeMaxIsCodecUnityNotTheControlMaximum(t *testing.T) {
	if volumeMax != 127 {
		t.Fatalf("volumeMax = %d, want 127 (0dB). Anything higher clips the DAC.",
			volumeMax)
	}
	if volumeButtonFloor >= volumeMax {
		t.Fatalf("button floor %d must sit below the ceiling %d",
			volumeButtonFloor, volumeMax)
	}
}

// The button band must be crossable in a sane number of presses: too few and
// each press is a huge jump, too many and reaching the top is a chore.
func TestButtonBandTakesAReasonableNumberOfPresses(t *testing.T) {
	presses := (volumeMax - volumeButtonFloor) / volumeStep
	if presses != 8 {
		t.Fatalf("%d presses to cross the band (step %d over %d..%d); want 8 for nine levels",
			presses, volumeStep, volumeButtonFloor, volumeMax)
	}
}

func TestStepsStayInsideTheButtonBand(t *testing.T) {
	cases := []struct {
		name string
		in   int
		want int
	}{
		// A level below the floor — HA can set one, and so could a stored
		// level from before the cap — must reach audible in ONE press, not
		// creep up 4dB at a time through inaudible territory.
		{"far below the floor lands on it", volumeButtonFloor - 40, volumeButtonFloor},
		{"just below the floor lands on it", volumeButtonFloor - 1, volumeButtonFloor},
		{"inside the band is untouched", volumeButtonFloor + volumeStep, volumeButtonFloor + volumeStep},
		{"above the ceiling clamps down", volumeMax + 30, volumeMax},
	}
	for _, tc := range cases {
		if got := clampToButtonBand(tc.in); got != tc.want {
			t.Errorf("%s: clampToButtonBand(%d) = %d, want %d",
				tc.name, tc.in, got, tc.want)
		}
	}
}

// Stepping up from the top and down from the bottom must settle, not
// oscillate or run away past the band.
func TestSteppingSaturatesAtBothEnds(t *testing.T) {
	level := volumeMax
	for i := 0; i < 5; i++ {
		level = clampToButtonBand(level + volumeStep)
	}
	if level != volumeMax {
		t.Errorf("stepping up from the ceiling reached %d, want %d", level, volumeMax)
	}

	level = volumeButtonFloor
	for i := 0; i < 5; i++ {
		level = clampToButtonBand(level - volumeStep)
	}
	if level != volumeButtonFloor {
		t.Errorf("stepping down from the floor reached %d, want %d",
			level, volumeButtonFloor)
	}
}

func TestVolumeControllerGetSetClampsAndNotifies(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })
	var notified []int
	vc.SetOnVolumeChange(func(level int) { notified = append(notified, level) })

	vc.Set(-10, false)
	vc.Set(volumeMax+10, false)
	if got := vc.Get(); got != volumeMax {
		t.Fatalf("clamped volume = %d, want %d", got, volumeMax)
	}
	if len(notified) != 2 || notified[0] != volumeMin || notified[1] != volumeMax {
		t.Fatalf("volume callbacks = %v", notified)
	}
}

func TestVolumeStepDownUsesButtonBand(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })
	vc.Set(volumeButtonFloor+volumeStep, false)
	vc.StepDown()
	if got := vc.Get(); got != volumeButtonFloor {
		t.Fatalf("StepDown() = %d, want floor %d", got, volumeButtonFloor)
	}
}

func TestSeedVolumeOnlyAppliesOnceAndMarksSeeded(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })
	s := &Server{volume: vc}
	if s.VolumeSeeded() {
		t.Fatal("volume unexpectedly seeded")
	}
	s.SeedVolume(40)
	s.SeedVolume(90)
	if got := s.VolumeLevel(); got != 40 {
		t.Fatalf("second SeedVolume overwrote level: %d", got)
	}
	if !s.VolumeSeeded() {
		t.Fatal("SeedVolume did not mark volume authoritative")
	}
}

func TestVolumeStepUpUnmutesWhenMuted(t *testing.T) {
	fake := &fakeLEDController{}
	vc := newVolumeController(func() led.Controller { return fake })
	mute := newMuteController(func() led.Controller { return fake }, nil)
	mute.muted = true
	s := &Server{volume: vc, mute: mute}

	s.VolumeStepUp()

	if s.mute.IsMuted() {
		t.Fatal("VolumeStepUp must unmute the device when muted")
	}
}
