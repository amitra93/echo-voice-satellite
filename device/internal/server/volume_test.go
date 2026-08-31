package server

import (
	"github.com/wilbowes/EchoMuse/pkg/led"
	"sync"
	"testing"
	"time"
)

// fakeLEDController records what it was asked to paint — enough to observe
// showLEDs actually running, unlike passing a nil led.Controller (showLEDs
// returns before touching displayActive when its getter yields nil).
//
// Guarded by a mutex because showLEDs' 2s expiry paints from its own
// AfterFunc goroutine — any test that forces that timer to fire (rather
// than only ever calling showLEDs synchronously, as the older tests below
// do) reads setCalls/leds concurrently with that goroutine's write.
type fakeLEDController struct {
	mu       sync.Mutex
	setCalls int
	leds     []led.Led
}

func TestRemoteVolumeLevelSnapsNonzeroValuesToPhysicalSteps(t *testing.T) {
	for _, tt := range []struct{ in, want int }{
		{0, 0}, {1, 73}, {72, 73}, {73, 73}, {76, 79}, {97, 97}, {127, 127}, {200, 127},
	} {
		if got := remoteVolumeLevel(tt.in); got != tt.want {
			t.Errorf("remoteVolumeLevel(%d) = %d, want %d", tt.in, got, tt.want)
		}
	}
}

func (f *fakeLEDController) Init() error              { return nil }
func (f *fakeLEDController) GetNumLEDs() (int, error) { return numLEDs, nil }
func (f *fakeLEDController) SetLEDs(leds ...led.Led) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.setCalls++
	f.leds = append([]led.Led(nil), leds...)
	return nil
}

// snapshotLeds is the safe read side of the mutex above — for tests that
// read after forcing the expiry timer, where a direct f.leds access would
// race the timer goroutine's write.
func (f *fakeLEDController) snapshotLeds() []led.Led {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]led.Led(nil), f.leds...)
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
	if presses != 9 {
		t.Fatalf("%d presses to cross the band (step %d over %d..%d); want 9 for ten levels",
			presses, volumeStep, volumeButtonFloor, volumeMax)
	}
}

func TestVolumeLEDArcChangesAtEveryButtonLevel(t *testing.T) {
	fake := &fakeLEDController{}
	vc := newVolumeController(func() led.Controller { return fake })
	seen := make(map[int]bool)
	for level := volumeButtonFloor; level <= volumeMax; level += volumeStep {
		vc.showLEDs(level)
		lit := 0
		for _, pixel := range fake.leds {
			if pixel.G != 0 {
				lit++
			}
		}
		if seen[lit] {
			t.Fatalf("level %d repeats a %d-LED arc", level, lit)
		}
		seen[lit] = true
	}
	if len(seen) != 10 {
		t.Fatalf("distinct volume arcs = %d, want 10", len(seen))
	}
	for _, lit := range []int{1, 2, 3, 5, 6, 7, 8, 10, 11, 12} {
		if !seen[lit] {
			t.Fatalf("missing %d-LED arc", lit)
		}
	}
}

func TestVolumeLEDArcStartsAtLED11AndWraps(t *testing.T) {
	fake := &fakeLEDController{}
	vc := newVolumeController(func() led.Controller { return fake })
	vc.showLEDs(volumeButtonFloor + 2*volumeStep)

	for i, id := range []int{11, 0, 1} {
		pixel := fake.leds[id]
		if pixel.ID != id || pixel.G == 0 {
			t.Fatalf("arc position %d: LED %d = %+v, want cyan", i+1, id, pixel)
		}
	}
	if fake.leds[2].G != 0 {
		t.Fatalf("LED 2 should be off at volume level 3")
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

func TestPhysicalButtonsSnapLegacyRawLevelsToTheTenStepScale(t *testing.T) {
	if got := nextButtonLevel(74); got != 79 {
		t.Errorf("nextButtonLevel(74) = %d, want 79 (level 2)", got)
	}
	if got := previousButtonLevel(74); got != 73 {
		t.Errorf("previousButtonLevel(74) = %d, want 73 (level 1)", got)
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

// nextButtonLevel/previousButtonLevel each have a branch StepUp/StepDown
// never reach in practice (Set() already clamps vc.level into the button
// band before these run) but must still answer correctly: a stored level
// from before the cap, or one HA set directly below the floor, has to land
// ON the floor from a single press rather than being treated as in-band.
func TestNextButtonLevelBelowFloorLandsOnFloor(t *testing.T) {
	if got := nextButtonLevel(volumeButtonFloor - 20); got != volumeButtonFloor {
		t.Fatalf("nextButtonLevel(below floor) = %d, want %d", got, volumeButtonFloor)
	}
}

func TestPreviousButtonLevelAtOrBelowFloorStaysOnFloor(t *testing.T) {
	if got := previousButtonLevel(volumeButtonFloor); got != volumeButtonFloor {
		t.Fatalf("previousButtonLevel(floor) = %d, want %d", got, volumeButtonFloor)
	}
	if got := previousButtonLevel(volumeButtonFloor - 20); got != volumeButtonFloor {
		t.Fatalf("previousButtonLevel(below floor) = %d, want %d", got, volumeButtonFloor)
	}
}

// showLEDs is called directly (not through Set) by a remote HA volume set
// below the button floor — Set() does not floor explicit values — so the
// arc math must not go negative and paint garbage.
func TestShowLEDsHandlesALevelBelowTheButtonFloor(t *testing.T) {
	fake := &fakeLEDController{}
	vc := newVolumeController(func() led.Controller { return fake })
	vc.showLEDs(1)
	lit := 0
	for _, pixel := range fake.leds {
		if pixel.G != 0 {
			lit++
		}
	}
	if lit != 1 {
		t.Fatalf("lit = %d, want the floor of 1 LED, not a negative/zero arc", lit)
	}
}

// The volume arc's 2s hold ends by handing the ring to whichever of three
// things owns it next: the mute indicator, the caller's own repaint hook,
// or (absent both) a plain clear. All three are wired through the same
// AfterFunc closure, which no other test waits for — Reset to a near-zero
// duration exercises the real closure instead of waiting out the real 2s.
func TestVolumeArcExpiryRestoresMuteRing(t *testing.T) {
	fake := &fakeLEDController{}
	vc := newVolumeController(func() led.Controller { return fake })
	vc.isMuted = func() bool { return true }
	vc.showLEDs(volumeButtonFloor)

	vc.mu.Lock()
	vc.timer.Reset(time.Millisecond)
	vc.mu.Unlock()
	time.Sleep(30 * time.Millisecond)

	if vc.DisplayActive() {
		t.Fatal("arc still marked active after its timer fired")
	}
	leds := fake.snapshotLeds()
	for _, pixel := range leds {
		if pixel.R != 180 || pixel.G != 0 || pixel.B != 0 {
			t.Fatalf("expiry while muted did not restore the red ring: %+v", leds)
		}
	}
}

func TestVolumeArcExpiryCallsOnDisplayExpireWhenSet(t *testing.T) {
	fake := &fakeLEDController{}
	vc := newVolumeController(func() led.Controller { return fake })
	done := make(chan struct{})
	vc.onDisplayExpire = func() { close(done) }
	vc.showLEDs(volumeButtonFloor)

	vc.mu.Lock()
	vc.timer.Reset(time.Millisecond)
	vc.mu.Unlock()
	select {
	case <-done:
	case <-time.After(100 * time.Millisecond):
		t.Fatal("expiry did not hand the ring back via onDisplayExpire")
	}
}

// ─── readFromDevice's tinymix output parsing ────────────────────────────────
//
// tinymixGet is a seam specifically so these branches — not just "tinymix
// binary is missing", which every other test above exercises for free by
// running on a host without it — get covered: the actual dB-linear cap this
// package exists to enforce lives in the clamp below.

func withFakeTinymixGet(t *testing.T, out string, err error) {
	t.Helper()
	old := tinymixGet
	tinymixGet = func() ([]byte, error) { return []byte(out), err }
	t.Cleanup(func() { tinymixGet = old })
}

func TestReadFromDeviceParsesTinymixOutput(t *testing.T) {
	withFakeTinymixGet(t, "PCM Playback Volume: 100 100 (range 0->175)", nil)
	vc := newVolumeController(func() led.Controller { return nil })
	if got := vc.readFromDevice(); got != 100 {
		t.Fatalf("readFromDevice() = %d, want 100", got)
	}
}

// A device left above the codec's unity-gain cap (index 127) — an older
// config, or manual tinkering — must not report back into the distorting
// range; the whole point of volumeMax is that nothing ever sets it there
// deliberately, including a read of the control's own state.
func TestReadFromDeviceClampsAboveVolumeMax(t *testing.T) {
	withFakeTinymixGet(t, "PCM Playback Volume: 175 175 (range 0->175)", nil)
	vc := newVolumeController(func() led.Controller { return nil })
	if got := vc.readFromDevice(); got != volumeMax {
		t.Fatalf("readFromDevice() = %d, want clamped to volumeMax %d", got, volumeMax)
	}
}

func TestReadFromDeviceFallsBackOnUnparsableOutput(t *testing.T) {
	withFakeTinymixGet(t, "not tinymix output at all", nil)
	vc := newVolumeController(func() led.Controller { return nil })
	want := (volumeButtonFloor + volumeMax) / 2
	if got := vc.readFromDevice(); got != want {
		t.Fatalf("readFromDevice() = %d, want fallback %d", got, want)
	}
}

func TestVolumeArcExpiryClearsWhenNeitherMutedNorExpireHookIsSet(t *testing.T) {
	fake := &fakeLEDController{}
	vc := newVolumeController(func() led.Controller { return fake })
	vc.showLEDs(volumeButtonFloor)

	vc.mu.Lock()
	vc.timer.Reset(time.Millisecond)
	vc.mu.Unlock()
	time.Sleep(30 * time.Millisecond)

	leds := fake.snapshotLeds()
	for _, pixel := range leds {
		if pixel.R != 0 || pixel.G != 0 || pixel.B != 0 {
			t.Fatalf("expiry with no mute/expire hook did not clear the ring: %+v", leds)
		}
	}
}
