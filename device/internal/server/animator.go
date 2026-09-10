package server

import (
	"log"
	"math"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// AnimSpec describes a device-rendered ring animation, received from the
// controller as a led_anim control message. The device renders every frame
// locally on its own ticker, so animation smoothness no longer depends on
// controller event-loop scheduling or WiFi jitter. The controller remains
// the source of truth for *which* animation should be showing: it re-sends
// the current spec on reconnect, and TTLSec is the dead-man switch for the
// opposite failure (controller gone mid-animation).
type AnimSpec struct {
	// Pattern: "off", "solid", "spin" (head+trail dot), "rotate" (palette
	// rotates around the ring — the pride spinner), "pulse" (sinusoidal
	// throb), or "meter" (brightness follows the live speaker RMS — the
	// ring throbs with the response being played).
	Pattern string `json:"pattern"`
	// Colors semantics per pattern:
	//   solid        — palette painted 1:1 (1 colour = whole ring, else per-LED)
	//   spin         — [head, trail]
	//   rotate       — palette rotated one LED per frame
	//   pulse/meter  — palette whose brightness is modulated
	Colors [][3]uint8 `json:"colors"`
	// PeriodMs: frame interval for spin/rotate (0 → 80ms); full throb
	// cycle for pulse (0 → 2000ms). Meter ignores it (fixed 40ms tick).
	PeriodMs int `json:"periodMs"`
	// Listening marks solid frames as the listening ring so the
	// beamformer direction overlay engages (same flag as set_leds).
	Listening bool `json:"listening"`
	// TTLSec auto-clears the ring if no newer spec arrives — protects
	// against a controller that died mid-turn. 0 → no TTL.
	TTLSec int `json:"ttlSec"`

	// ── countdown-only fields ────────────────────────────────────────────
	// FractionNow is the remaining fraction (0-1) of the timer's duration
	// at the instant this spec was sent — the device interpolates every
	// subsequent frame from here rather than being re-pushed.
	FractionNow float64 `json:"fractionNow"`
	// RunningMps is the fraction lost per second of wall time (1/total
	// seconds while the timer is running, exactly 0 while paused).
	RunningMps float64 `json:"runningMps"`

	// ── meter-only response curve ────────────────────────────────────────
	// Pointer-typed so 0 is expressible and "absent" is distinguishable
	// from "zero" (same reason micGainDb is a pointer in ConfigMessage).
	// Nil fields fall back to the meterDefaults below.
	//
	// Tunable from the dashboard because this is a *taste* parameter: the
	// original fixed curve measured only ~23% perceived brightness
	// variation on speech (see docs/led-ring-states.md), and finding a
	// value that reads well in a real room takes several passes. Shipping
	// them as config turns that loop into a page refresh instead of a
	// firmware OTA per iteration.
	Attack *float64 `json:"attack"` // envelope rise coefficient per 40ms tick
	Decay  *float64 `json:"decay"`  // envelope fall coefficient per 40ms tick
	Floor  *float64 `json:"floor"`  // perceptual brightness at silence
	Gamma  *float64 `json:"gamma"`  // output gamma; >1 expands the dark end
	Ref    *float64 `json:"ref"`    // RMS mapped to full scale
	Curve  *float64 `json:"curve"`  // input exponent; <1 lifts quiet detail
}

// meterDefaults are the shipped response curve. Derived in
// docs/led-ring-states.md §"Why it's barely visible":
//   - decay 0.30 → τ≈133ms, which tracks syllables (150-250ms). The old
//     0.12 gave τ≈333ms and smoothed everything below phrase rate away,
//     which is why the ring looked nearly static during speech.
//   - floor 0.06 + gamma 2.2: the paint is (floor+span*env)^gamma, so the
//     value is a *perceptual* target rather than a raw duty cycle. Without
//     the gamma the eye sees roughly the 1/2.2 power of the linear swing,
//     which is what compressed the old range into invisibility.
//   - ref 0.22 / curve 0.7 replace sqrt(rms/0.35): a gentler lift that
//     keeps quiet consonants visible without squashing normal speech into
//     the top of the range.
var meterDefaults = struct {
	attack, decay, floor, gamma, ref, curve float64
}{
	attack: 0.6,
	decay:  0.30,
	floor:  0.06,
	gamma:  2.2,
	ref:    0.22,
	curve:  0.7,
}

// resolveMeter returns spec's meter parameters with defaults filled in and
// each value clamped to a range that cannot produce a dead or seizure-grade
// ring — a bad config push must not be able to break the display.
func resolveMeter(spec AnimSpec) (attack, decay, floor, gamma, ref, curve float64) {
	pick := func(p *float64, def, lo, hi float64) float64 {
		if p == nil {
			return def
		}
		return math.Min(hi, math.Max(lo, *p))
	}
	attack = pick(spec.Attack, meterDefaults.attack, 0.05, 1.0)
	decay = pick(spec.Decay, meterDefaults.decay, 0.02, 1.0)
	floor = pick(spec.Floor, meterDefaults.floor, 0.0, 0.6)
	gamma = pick(spec.Gamma, meterDefaults.gamma, 1.0, 3.5)
	ref = pick(spec.Ref, meterDefaults.ref, 0.02, 1.0)
	curve = pick(spec.Curve, meterDefaults.curve, 0.3, 2.0)
	return
}

// animator owns the ring animation goroutine. One animation at a time; a
// new spec (or Stop) atomically replaces the current one via generation
// counting, so a stale goroutine can never paint over its successor.
type animator struct {
	mu  sync.Mutex
	gen int
}

const defaultAnimPeriod = 80 * time.Millisecond

// StartAnim replaces the current animation with spec.
func (s *Server) StartAnim(spec AnimSpec) {
	s.anim.mu.Lock()
	s.anim.gen++
	gen := s.anim.gen
	s.anim.mu.Unlock()

	switch spec.Pattern {
	case "off":
		s.paintIfCurrent(gen, blackFrame(), boolPtr(false))
	case "solid":
		s.paintIfCurrent(gen, paletteFrame(spec.Colors), boolPtr(spec.Listening))
		if spec.TTLSec > 0 {
			go s.animExpiry(gen, time.Duration(spec.TTLSec)*time.Second)
		}
	case "spin", "rotate":
		go s.runAnim(gen, spec)
	case "pulse":
		go s.runPulse(gen, spec)
	case "meter":
		go s.runMeter(gen, spec)
	case "countdown":
		go s.runCountdown(gen, spec)
	default:
		log.Printf("StartAnim: unknown pattern %q — clearing ring", spec.Pattern)
		s.paintIfCurrent(gen, blackFrame(), boolPtr(false))
	}
}

// StopAnim cancels any running animation without touching the ring — used
// when another subsystem (e.g. a controller set_leds frame) takes over.
func (s *Server) StopAnim() {
	s.anim.mu.Lock()
	s.anim.gen++
	s.anim.mu.Unlock()
}

// animCurrent reports whether gen is still the live animation.
func (s *Server) animCurrent(gen int) bool {
	s.anim.mu.Lock()
	defer s.anim.mu.Unlock()
	return gen == s.anim.gen
}

// paintIfCurrent atomically confirms that an animation generation is still
// live and records/paints its frame. Keeping the generation lock across
// SetLEDs closes the check-then-paint race where a one-second countdown tick
// could overwrite a newer listening or alarm animation.
func (s *Server) paintIfCurrent(gen int, frame []led.Led, listeningHint *bool) bool {
	s.anim.mu.Lock()
	defer s.anim.mu.Unlock()
	if gen != s.anim.gen {
		return false
	}
	s.SetLEDs(frame, listeningHint)
	return true
}

// animExpiry clears the ring when a TTL'd static frame outlives its
// dead-man window without being replaced.
func (s *Server) animExpiry(gen int, ttl time.Duration) {
	time.Sleep(ttl)
	if !s.paintIfCurrent(gen, blackFrame(), boolPtr(false)) {
		return
	}
	log.Printf("led_anim: TTL expired with no replacement — clearing ring")
}

// runAnim renders spin/rotate frames until replaced or TTL-expired. Frames
// go through SetLEDs so the mute-ring and volume-arc paint suppressions
// (and baseLEDs recording for the hand-back repaint) apply unchanged.
func (s *Server) runAnim(gen int, spec AnimSpec) {
	period := defaultAnimPeriod
	if spec.PeriodMs > 0 {
		period = time.Duration(spec.PeriodMs) * time.Millisecond
	}
	var deadline time.Time
	if spec.TTLSec > 0 {
		deadline = time.Now().Add(time.Duration(spec.TTLSec) * time.Second)
	}

	ticker := time.NewTicker(period)
	defer ticker.Stop()

	pos := 0
	for {
		if !s.animCurrent(gen) {
			return
		}
		if !deadline.IsZero() && time.Now().After(deadline) {
			if s.paintIfCurrent(gen, blackFrame(), boolPtr(false)) {
				log.Printf("led_anim: TTL expired with no replacement — clearing ring")
			}
			return
		}
		if !s.paintIfCurrent(gen, animFrame(spec, pos), boolPtr(false)) {
			return
		}
		pos = (pos + 1) % 12
		<-ticker.C
	}
}

// SetAudioLevel records the live speaker RMS (0..1) — fed by the speaker's
// levelTap on the ALSA pump goroutine, read by the meter animation.
func (s *Server) SetAudioLevel(rms float64) {
	s.audioLevel.Store(math.Float64bits(rms))
}

func (s *Server) getAudioLevel() float64 {
	return math.Float64frombits(s.audioLevel.Load())
}

// runPulse throbs the palette sinusoidally between 15% and 100% brightness
// over PeriodMs (default 2s) until replaced or TTL-expired.
func (s *Server) runPulse(gen int, spec AnimSpec) {
	cycle := 2 * time.Second
	if spec.PeriodMs > 0 {
		cycle = time.Duration(spec.PeriodMs) * time.Millisecond
	}
	var deadline time.Time
	if spec.TTLSec > 0 {
		deadline = time.Now().Add(time.Duration(spec.TTLSec) * time.Second)
	}
	base := paletteFrame(spec.Colors)
	start := time.Now()
	ticker := time.NewTicker(40 * time.Millisecond)
	defer ticker.Stop()
	for {
		if !s.animCurrent(gen) {
			return
		}
		if !deadline.IsZero() && time.Now().After(deadline) {
			if s.paintIfCurrent(gen, blackFrame(), boolPtr(false)) {
				log.Printf("led_anim: TTL expired with no replacement — clearing ring")
			}
			return
		}
		phase := float64(time.Since(start)) / float64(cycle)
		b := 0.15 + 0.85*(0.5-0.5*math.Cos(2*math.Pi*phase))
		if !s.paintIfCurrent(gen, scaleFrame(base, b), boolPtr(false)) {
			return
		}
		<-ticker.C
	}
}

// runMeter throbs the palette with the live speaker level: fast attack,
// decay envelope over the RMS the speaker reports at its ALSA write — so
// the ring follows what is audible *now*, not the controller's send pace
// (~5.5s of device buffer sits between the two). The floor keeps the ring
// visibly owned by the turn through inter-word silence. Response curve is
// config-tunable — see AnimSpec's meter fields and meterDefaults.
func (s *Server) runMeter(gen int, spec AnimSpec) {
	var deadline time.Time
	if spec.TTLSec > 0 {
		deadline = time.Now().Add(time.Duration(spec.TTLSec) * time.Second)
	}
	base := paletteFrame(spec.Colors)
	attack, decay, floor, gamma, ref, curve := resolveMeter(spec)
	span := 1.0 - floor
	env := 0.0
	ticker := time.NewTicker(40 * time.Millisecond)
	defer ticker.Stop()
	for {
		if !s.animCurrent(gen) {
			return
		}
		if !deadline.IsZero() && time.Now().After(deadline) {
			if s.paintIfCurrent(gen, blackFrame(), boolPtr(false)) {
				log.Printf("led_anim: TTL expired with no replacement — clearing ring")
			}
			return
		}
		// Input curve: normalise the speaker RMS against ref, then apply
		// curve (<1 lifts quiet detail so consonants register).
		level := math.Pow(math.Min(1, s.getAudioLevel()/ref), curve)
		if level > env {
			env += attack * (level - env) // fast attack
		} else {
			env += decay * (level - env) // decay tracks syllable rate
		}
		// floor+span*env is a PERCEPTUAL brightness target; raising it to
		// gamma converts to the duty cycle that the eye reads as that
		// target. Painting the target directly (the pre-2026-07-25
		// behaviour) is what made the throb near-invisible.
		b := math.Pow(floor+span*env, gamma)
		if !s.paintIfCurrent(gen, scaleFrame(base, b), boolPtr(false)) {
			return
		}
		<-ticker.C
	}
}

// runCountdown renders a running timer's remaining fraction as a partial
// ring — lit LEDs of spec.Colors[0] starting at LED 0, the rest black. The
// controller sends a starting fraction and a decay rate; the device
// interpolates every subsequent frame itself so controller/WiFi jitter
// can't judder it, the same reasoning spin/pulse/meter are already
// device-rendered for. Ticks once per second — deliberately slower than
// pulse/meter's 40ms tick, since a 12-LED lit-count only changes roughly
// every total_seconds/12 seconds and there is no audio or motion to track
// smoothly here.
func (s *Server) runCountdown(gen int, spec AnimSpec) {
	var deadline time.Time
	if spec.TTLSec > 0 {
		deadline = time.Now().Add(time.Duration(spec.TTLSec) * time.Second)
	}
	var color [3]uint8
	if len(spec.Colors) > 0 {
		color = spec.Colors[0]
	}
	start := time.Now()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		if !s.animCurrent(gen) {
			return
		}
		if !deadline.IsZero() && time.Now().After(deadline) {
			if s.paintIfCurrent(gen, blackFrame(), boolPtr(false)) {
				log.Printf("led_anim: TTL expired with no replacement — clearing ring")
			}
			return
		}
		fraction := spec.FractionNow - spec.RunningMps*time.Since(start).Seconds()
		fraction = math.Min(1, math.Max(0, fraction))
		lit := int(math.Round(fraction * 12))
		if lit < 0 {
			lit = 0
		} else if lit > 12 {
			lit = 12
		}
		if !s.paintIfCurrent(gen, countdownFrame(color, lit), boolPtr(false)) {
			return
		}
		<-ticker.C
	}
}

// scaleFrame returns frame with every channel scaled by b (0..1).
func scaleFrame(frame []led.Led, b float64) []led.Led {
	out := make([]led.Led, len(frame))
	for i, l := range frame {
		out[i] = led.Led{
			ID: l.ID,
			R:  uint8(float64(l.R)*b + 0.5),
			G:  uint8(float64(l.G)*b + 0.5),
			B:  uint8(float64(l.B)*b + 0.5),
		}
	}
	return out
}

// animFrame renders one frame of a spin/rotate animation.
func animFrame(spec AnimSpec, pos int) []led.Led {
	frame := make([]led.Led, 12)
	for i := range frame {
		frame[i].ID = i
	}
	switch spec.Pattern {
	case "spin":
		var head, trail [3]uint8
		if len(spec.Colors) > 0 {
			head = spec.Colors[0]
		}
		if len(spec.Colors) > 1 {
			trail = spec.Colors[1]
		}
		frame[pos%12] = led.Led{ID: pos % 12, R: head[0], G: head[1], B: head[2]}
		p := (pos + 11) % 12
		frame[p] = led.Led{ID: p, R: trail[0], G: trail[1], B: trail[2]}
	case "rotate":
		n := len(spec.Colors)
		if n == 0 {
			return frame
		}
		for i := range frame {
			c := spec.Colors[((i-pos)%n+n)%n]
			frame[i].R, frame[i].G, frame[i].B = c[0], c[1], c[2]
		}
	}
	return frame
}

// paletteFrame maps a colour list onto the ring: one colour fills the
// whole ring, otherwise colours map per-LED (short lists leave the rest
// dark, matching set_leds partial-frame behaviour).
func paletteFrame(colors [][3]uint8) []led.Led {
	frame := make([]led.Led, 12)
	for i := range frame {
		frame[i].ID = i
		var c [3]uint8
		switch {
		case len(colors) == 1:
			c = colors[0]
		case i < len(colors):
			c = colors[i]
		}
		frame[i].R, frame[i].G, frame[i].B = c[0], c[1], c[2]
	}
	return frame
}

// countdownFrame renders lit LEDs of color starting at LED 0 and proceeding
// forward, the rest black — a fixed reference position distinct from the
// volume arc's volumeLEDStart (11), so the two partial-ring idioms are
// never visually confused.
func countdownFrame(color [3]uint8, lit int) []led.Led {
	frame := make([]led.Led, 12)
	for i := range frame {
		frame[i].ID = i
		if i < lit {
			frame[i].R, frame[i].G, frame[i].B = color[0], color[1], color[2]
		}
	}
	return frame
}

func blackFrame() []led.Led {
	frame := make([]led.Led, 12)
	for i := range frame {
		frame[i].ID = i
	}
	return frame
}

func boolPtr(b bool) *bool { return &b }
