package server

import (
	"fmt"
	"github.com/wilbowes/EchoMuse/pkg/led"
	"log"
	"os/exec"
	"sync"
	"time"
)

const (
	volumeMin = 0

	// volumeMax is the codec's UNITY gain, not the top of the mixer control's
	// range. tinymix ctl 61 is the tlv320aic32x4 DAC digital volume: 176
	// steps of 0.5dB spanning -63.5dB..+24dB, with 0dB at index 127. The 48
	// steps above 127 apply POSITIVE digital gain to already near-full-scale
	// PCM, which saturates inside the DAC.
	//
	// Measured on hardware 2026-08-13 (1kHz at -6dBFS, recorded through the
	// mic array): THD 1.5% at 127, 2.3% at 136, 65% at 153, 89% at 170 — with
	// the output level FLAT from 153 upward, because it had already stopped
	// being able to get louder. Third harmonic rose to -1.1dB relative to the
	// fundamental, i.e. very nearly a square wave. The control run that
	// isolates it: index 170 with the source scaled down to land at the same
	// acoustic level reads 1.1%, clean — so the codec's gain stage is fine
	// and it is purely source x gain exceeding full scale.
	//
	// Stock FireOS never writes this control at all: it appears in no
	// /system binary and not once in /system/etc/audio_device.xml, which
	// leaves the DAC at its 0dB reset default and takes user volume from
	// AudioFlinger's software attenuation instead. That is why native Alexa
	// has no such distortion, and why matching it means capping here.
	//
	// Do not raise this. The lost headroom cannot be bought back from
	// Ext_Amp_Gain either — that control is inert on this board (measured
	// 0.0dB of effect across its whole 6/12/18/24dB range, while still
	// reading its new value back).
	volumeMax = 127

	// volumeButtonFloor is the bottom of the band the PHYSICAL buttons
	// traverse. The scale is dB-linear, so index 0 is -63.5dB. Start at -27dB
	// rather than -40dB: the lower band was effectively inaudible after the
	// output chain was tuned for loud speech. This leaves ten useful levels
	// from -27dB through 0dB. Silencing the device is the mute button's
	// job, not the volume button's.
	//
	// Explicit Set() calls are deliberately NOT floored — HA's volume 0.0
	// has to still mean silent. A press from below the floor lands ON the
	// floor rather than adding a step, so one press always reaches audible.
	volumeButtonFloor = 73 // -27dB

	// volumeStep is 3dB per press: nine intervals make ten useful levels
	// from -27dB through 0dB. The old level one (79, -24dB) is now level two.
	volumeStep       = 6
	volumeDisplayMax = 12 // level 10 fills all twelve physical LEDs
	volumeLEDStart   = 11 // volume arc begins at the physical ring position 11
	volumeLEDSecs    = 2  // how long to show volume ring
	numLEDs          = 12
)

type volumeController struct {
	mu             sync.Mutex
	level          int
	ledCtrl        func() led.Controller // getter so we handle nil during boot
	timer          *time.Timer
	displayActive  bool        // volume arc currently on the ring — see DisplayActive
	isMuted        func() bool // set after construction to avoid circular dependency
	onVolumeChange func(int)   // set after construction; called after every Set()
	codecVolume    bool        // false when Android owns output attenuation
	// onDisplayExpire, when set, replaces the default clear-to-black at the
	// end of the display window: the server wires it to repaint the ring
	// from its stored controller state, so a volume press mid-turn hands
	// back to the listening/thinking/playing animation instead of going
	// dark. The muted → red-ring case stays here either way.
	onDisplayExpire func()
}

// DisplayActive reports whether the volume arc is currently on the ring.
// The server checks this to suppress controller LED paints (and the
// direction overlay) for the display window — without it, the turn
// animations repaint within one frame (~100ms) and the arc appears as a
// glitch rather than a reading.
func (vc *volumeController) DisplayActive() bool {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	return vc.displayActive
}

// SetOnVolumeChange wires a callback invoked after every Set() call.
// B7 fix (2026-07-05 review): previously Server.SetVolumeChangeCallback
// reached directly into vc.mu/vc.onVolumeChange from outside this struct.
// Encapsulating the lock here keeps volumeController responsible for its
// own synchronisation, matching every other volumeController method.
func (vc *volumeController) SetOnVolumeChange(cb func(int)) {
	vc.mu.Lock()
	vc.onVolumeChange = cb
	vc.mu.Unlock()
}

func newVolumeController(ledGetter func() led.Controller) *volumeController {
	vc := &volumeController{
		ledCtrl:     ledGetter,
		codecVolume: true,
	}
	// Read initial volume from tinymix
	vc.level = vc.readFromDevice()
	log.Printf("Volume controller initialised at %d/%d", vc.level, volumeMax)
	return vc
}

// UseAndroidVolume leaves the codec at unity and hands user attenuation to
// AudioFlinger. It is used only by the Amazon AFE path; direct ALSA continues
// to own ctl 61 itself.
func (vc *volumeController) UseAndroidVolume() {
	if err := exec.Command("tinymix", "-D", "0", "61",
		fmt.Sprintf("%d", volumeMax), fmt.Sprintf("%d", volumeMax)).Run(); err != nil {
		log.Printf("tinymix unity set failed: %v", err)
	}
	vc.mu.Lock()
	vc.codecVolume = false
	vc.mu.Unlock()
}

// tinymixGet is a seam over the raw tinymix read, the same reasoning as
// wifi.go's wpaCli: production shells out for real, host tests supply
// deterministic output so the Sscanf/clamp logic below — not just the
// "tinymix is missing" fallback — is exercised.
var tinymixGet = func() ([]byte, error) {
	return exec.Command("tinymix", "-D", "0", "61").Output()
}

// readFromDevice reads current tinymix level. Returns the midpoint of the
// button band on failure — volumeMax/2 is -32dB on this dB-linear scale,
// which is quiet enough to read as broken.
func (vc *volumeController) readFromDevice() int {
	fallback := (volumeButtonFloor + volumeMax) / 2
	out, err := tinymixGet()
	if err != nil {
		log.Printf("Volume read failed: %v", err)
		return fallback
	}
	var l, r int
	// Output: "PCM Playback Volume: 100 100 (range 0->175)". The control's
	// own range is 0->175; volumeMax caps us at 127 (unity) — see the
	// constant. A device that was left above the cap reads back high here
	// and the next Set() clamps it.
	if _, err := fmt.Sscanf(string(out), "PCM Playback Volume: %d %d", &l, &r); err != nil {
		log.Printf("Volume parse failed: %v (output: %s)", err, out)
		return fallback
	}
	if l > volumeMax {
		l = volumeMax
	}
	return l
}

// Set applies a new volume level (0–volumeMax) and updates tinymix. showRing
// paints the cyan volume arc for the 2s display window — physical button
// presses and remote sets (controller command / HA, Server.SetVolume) both
// pass true, since both are a deliberate action by someone. Only the
// boot-time SeedVolume passes false, since nobody asked for that one.
func (vc *volumeController) Set(level int, showRing bool) {
	if level < volumeMin {
		level = volumeMin
	}
	if level > volumeMax {
		level = volumeMax
	}

	vc.mu.Lock()
	vc.level = level
	// Copy under the lock — SetOnVolumeChange writes this field under mu
	// from the main goroutine, and button events can fire before that
	// wiring completes (SubscribeToButton starts the evdev goroutines
	// first).
	cb := vc.onVolumeChange
	codecVolume := vc.codecVolume
	vc.mu.Unlock()

	if codecVolume {
		if err := exec.Command("tinymix", "-D", "0", "61",
			fmt.Sprintf("%d", level), fmt.Sprintf("%d", level)).Run(); err != nil {
			log.Printf("tinymix set failed: %v", err)
		}
	}

	log.Printf("Volume set to %d/%d", level, volumeMax)
	if showRing {
		vc.showLEDs(level)
	}
	if cb != nil {
		cb(level)
	}
}

// remoteVolumeLevel keeps Home Assistant/controller volume commands aligned
// with the ten physical button levels. Zero remains a real mute request; every
// nonzero value selects one of the audible STREAM_MUSIC 21..30 steps.
func remoteVolumeLevel(level int) int {
	if level <= volumeMin {
		return volumeMin
	}
	if level >= volumeMax {
		return volumeMax
	}
	step := (level-volumeButtonFloor+volumeStep/2)/volumeStep + 1
	if step < 1 {
		step = 1
	}
	if step > 10 {
		step = 10
	}
	return volumeButtonFloor + (step-1)*volumeStep
}

// CancelDisplay ends the volume arc's hold early, releasing the ring back to
// whatever wants to paint next.
//
// The hold exists to stop turn animations — which repaint every ~80ms — from
// stomping the arc within a frame of it appearing. It was never meant to
// outrank a deliberate press: adjusting the volume and immediately pressing
// the action button left the arc sitting there for the rest of its 2s with no
// sign the device had started listening.
//
// Deliberately does NOT repaint. The caller is about to start a turn, so its
// listening frame lands within a round trip; clearing to black here would put
// a visible dark gap between the two. The arc simply stops being sovereign.
func (vc *volumeController) CancelDisplay() {
	vc.mu.Lock()
	if vc.timer != nil {
		vc.timer.Stop()
		vc.timer = nil
	}
	vc.displayActive = false
	vc.mu.Unlock()
}

// Get returns current volume level.
func (vc *volumeController) Get() int {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	return vc.level
}

// StepUp increases volume by one step, within the button band.
func (vc *volumeController) StepUp() {
	vc.mu.Lock()
	level := vc.level
	vc.mu.Unlock()
	vc.Set(nextButtonLevel(level), true)
}

// StepDown decreases volume by one step, within the button band.
func (vc *volumeController) StepDown() {
	vc.mu.Lock()
	level := vc.level
	vc.mu.Unlock()
	vc.Set(previousButtonLevel(level), true)
}

// clampToButtonBand holds a stepped level inside [volumeButtonFloor,
// volumeMax]. A device sitting below the floor — HA can put it there, and so
// can a stored level from before the cap — lands ON the floor from one press
// instead of creeping up 4dB at a time through inaudible territory.
func clampToButtonBand(level int) int {
	if level < volumeButtonFloor {
		return volumeButtonFloor
	}
	if level > volumeMax {
		return volumeMax
	}
	return level
}

// nextButtonLevel and previousButtonLevel keep physical presses on the ten
// canonical values even when a legacy controller state or remote raw set left
// the codec between them.
func nextButtonLevel(level int) int {
	if level < volumeButtonFloor {
		return volumeButtonFloor
	}
	return clampToButtonBand(volumeButtonFloor +
		((level-volumeButtonFloor)/volumeStep+1)*volumeStep)
}

func previousButtonLevel(level int) int {
	if level <= volumeButtonFloor {
		return volumeButtonFloor
	}
	return volumeButtonFloor + ((level-volumeButtonFloor-1)/volumeStep)*volumeStep
}

// showLEDs lights N of 12 LEDs in cyan proportional to volume, then clears after 2s.
func (vc *volumeController) showLEDs(level int) {
	lc := vc.ledCtrl()
	if lc == nil {
		return
	}

	// Spread the ten volume positions across all twelve physical LEDs: level 1
	// lights one LED and level 10 fills the ring. Round to the nearest LED so
	// the unused two positions are distributed across the range.
	volumeLevel := (level-volumeButtonFloor)/volumeStep + 1
	lit := 1 + ((volumeLevel-1)*(numLEDs-1)+4)/9
	if lit < 1 && level > volumeMin {
		lit = 1
	}
	if lit > volumeDisplayMax {
		lit = volumeDisplayMax
	}
	leds := make([]led.Led, numLEDs)
	for i := 0; i < numLEDs; i++ {
		id := (volumeLEDStart + i) % numLEDs
		if i < lit {
			leds[id] = led.Led{ID: id, R: 0, G: 200, B: 200} // cyan
		} else {
			leds[id] = led.Led{ID: id, R: 0, G: 0, B: 0}
		}
	}
	if err := lc.SetLEDs(leds...); err != nil {
		log.Printf("Volume LED set failed: %v", err)
		return
	}

	// Cancel any existing clear timer and start a new one
	vc.mu.Lock()
	vc.displayActive = true
	if vc.timer != nil {
		vc.timer.Stop()
	}
	vc.timer = time.AfterFunc(volumeLEDSecs*time.Second, func() {
		vc.mu.Lock()
		vc.displayActive = false
		expire := vc.onDisplayExpire
		vc.mu.Unlock()
		if vc.isMuted != nil && vc.isMuted() {
			// Restore mute indicator — red ring
			leds := make([]led.Led, numLEDs)
			for i := 0; i < numLEDs; i++ {
				leds[i] = led.Led{ID: i, R: 180, G: 0, B: 0}
			}
			lc.SetLEDs(leds...)
		} else if expire != nil {
			// Hand back to whatever the controller last painted —
			// listening/thinking/playing ring mid-turn, all-off when idle.
			expire()
		} else {
			clearLeds(lc)
		}
	})
	vc.mu.Unlock()
}
