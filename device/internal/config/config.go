// Package config provides a shared, concurrency-safe device configuration
// that can be updated at runtime when the controller pushes a config message.
//
// Both the control client (OWW threshold) and the data client (VAD params)
// read from this struct so changes take effect immediately without a restart.
package config

import (
	"encoding/json"
	"os"
	"strconv"
	"sync"
)

// Device holds all runtime-tunable parameters for this device.
// Zero values are replaced by defaults on first access via Get().
type Device struct {
	mu sync.RWMutex

	// Microphone / VAD
	VadThreshold float64
	VadSpeechMs  int
	VadSilenceMs int

	// Speaker
	StartupVolume int
	// Output chain settings mirror the controller's playback config. They are
	// retained here so partial config pushes update the live chain safely.
	EqBands          []float64
	EqLoudness       bool
	BassShelfHz      float64
	SubsonicHz       float64
	BassGuardEnabled bool
	BassGuardDb      float64
	LimiterEnabled   bool
	LimiterThreshold float64
	LimiterRelease   float64
	// SendspinServer is the controller-selected Music Assistant Sendspin URL.
	// It is deliberately outside the fleet/device settings sections: it is
	// controller connection metadata, not an audio preference.
	SendspinServer string

	// Wake word
	OwwThreshold float64
	OwwModel     string
	// BargeInEnabled / BargeInThreshold mirror the controller's barge-in
	// settings. The device needs them for on-device scoring: while the speaker
	// is streaming, the controller lowers its wake bar to BargeInThreshold
	// (echo at the mic is ~25dB louder than the person, so speech-over-TTS
	// scores are depressed). A device scoring against the normal threshold
	// during playback is not answering the same question, which made every
	// barge-in look like an on-device miss.
	BargeInEnabled   bool
	BargeInThreshold float64
	// DuckDb is how far MUSIC is attenuated while a voice turn plays over
	// it, in dB (negative = quieter). Config rather than a constant because
	// it is a taste parameter that needs iterating in a real room, the same
	// reasoning as the LED meter response curve — not something to discover
	// via a firmware OTA per attempt.
	DuckDb float64
	// StopModel and StopThreshold configure the locally-scored mandatory stop
	// classifier. The controller arms it separately per response.
	StopModel         string
	StopThreshold     float64
	SaveWakeCaptures  bool
	WakeCaptureSec    float64
	WakeNearMissFloor float64
	SaveStopCaptures  bool
	StopCaptureSec    float64

	// AfeMicGainDb is a fixed digital gain applied to Amazon AFE's already
	// processed S16 capture before it is sent to the controller. 0 = unity.
	AfeMicGainDb int

	// BLE proxy (passive scan over /dev/stpbt, internal/bluetooth) —
	// pointer typed so false is expressible over the wire. Default off.
	BleProxyEnabled *bool

	// ListeningAnim carries the controller's current listening-ring
	// animation spec, raw JSON in the led_anim shape, so the device can
	// light it locally at its OWN wake crossing (#263) instead of waiting
	// a controller round trip for the authoritative frame. Nil until the
	// controller sends one; a device that has never received it simply
	// keeps the old behaviour.
	ListeningAnim json.RawMessage

	initialised bool
}

var global = &Device{}

// Get returns the global device config, initialised from environment
// variables on first call.
func Get() *Device {
	global.mu.Lock()
	defer global.mu.Unlock()
	if !global.initialised {
		global.loadDefaults()
		global.initialised = true
	}
	return global
}

// loadDefaults populates from environment variables, falling back to
// hard-coded defaults. Must be called with mu held.
func (d *Device) loadDefaults() {
	d.VadThreshold = envFloat("VAD_THRESHOLD", 0.004)
	d.VadSpeechMs = envInt("VAD_SPEECH_MS", 80)
	d.VadSilenceMs = envInt("VAD_SILENCE_MS", 600)
	d.StartupVolume = envInt("STARTUP_VOLUME", 85)
	d.EqBands = []float64{4.5, 3.0, -0.5, 0.0, 1.5, 1.0, 0.0, 1.5}
	d.EqLoudness = true
	d.BassShelfHz = 125
	d.SubsonicHz = 85
	d.BassGuardEnabled = true
	d.BassGuardDb = -30
	d.LimiterEnabled = true
	d.LimiterThreshold = -1
	d.LimiterRelease = 150
	d.OwwThreshold = envFloat("OWW_THRESHOLD", 0.5)
	d.OwwModel = envStr("OWW_MODEL", "hey_jarvis_v0.1")
	d.StopModel = envStr("STOP_MODEL", "")
	d.StopThreshold = envFloat("STOP_THRESHOLD", 0.5)
	d.SaveWakeCaptures = envBool("SAVE_WAKE_CAPTURES", false)
	d.WakeCaptureSec = clampFloat(envFloat("WAKE_CAPTURE_SEC", 2.0), 0.08, 5.0)
	d.WakeNearMissFloor = clampFloat(envFloat("WAKE_NEAR_MISS_FLOOR", 0.05), 0, 1)
	d.SaveStopCaptures = envBool("SAVE_STOP_CAPTURES", false)
	d.StopCaptureSec = clampFloat(envFloat("STOP_CAPTURE_SEC", 2.0), 0.08, 5.0)
	d.BargeInThreshold = envFloat("BARGE_IN_THRESHOLD", 0.05)
	d.DuckDb = envFloat("DUCK_DB", -18)
	d.AfeMicGainDb = clampAfeMicGainDb(envInt("AFE_MIC_GAIN_DB", 0))
	bleProxyEnabled := envBool("BLE_PROXY_ENABLED", false)
	d.BleProxyEnabled = &bleProxyEnabled
}

// Apply updates the config from a controller-pushed config message.
// Only non-zero / non-empty values from the message are applied so that
// a partial config push doesn't zero out unmentioned fields.
func (d *Device) Apply(msg ConfigMessage) {
	d.mu.Lock()
	defer d.mu.Unlock()

	if !d.initialised {
		d.loadDefaults()
		d.initialised = true
	}

	if msg.VadThreshold > 0 {
		d.VadThreshold = msg.VadThreshold
	}
	if msg.VadSpeechMs > 0 {
		d.VadSpeechMs = msg.VadSpeechMs
	}
	if msg.VadSilenceMs > 0 {
		d.VadSilenceMs = msg.VadSilenceMs
	}
	if msg.OwwThreshold > 0 {
		d.OwwThreshold = msg.OwwThreshold
	}
	if msg.OwwModel != "" {
		d.OwwModel = msg.OwwModel
	}
	if msg.StopModel != "" {
		d.StopModel = msg.StopModel
	}
	if msg.StopThreshold > 0 {
		d.StopThreshold = msg.StopThreshold
	}
	if msg.SaveWakeCaptures != nil {
		d.SaveWakeCaptures = *msg.SaveWakeCaptures
	}
	if msg.WakeCaptureSec > 0 {
		d.WakeCaptureSec = clampFloat(msg.WakeCaptureSec, 0.08, 5.0)
	}
	if msg.WakeNearMissFloor != nil {
		d.WakeNearMissFloor = clampFloat(*msg.WakeNearMissFloor, 0, 1)
	}
	if msg.SaveStopCaptures != nil {
		d.SaveStopCaptures = *msg.SaveStopCaptures
	}
	if msg.StopCaptureSec > 0 {
		d.StopCaptureSec = clampFloat(msg.StopCaptureSec, 0.08, 5.0)
	}
	if msg.BargeInEnabled != nil {
		d.BargeInEnabled = *msg.BargeInEnabled
	}
	if msg.BargeInThreshold > 0 {
		d.BargeInThreshold = msg.BargeInThreshold
	}
	// Negative-going, so the usual "non-zero means set" rule is inverted:
	// a duck of 0dB is a legitimate setting ("do not duck at all") and must
	// be distinguishable from an absent field, hence the pointer.
	if msg.DuckDb != nil {
		d.DuckDb = *msg.DuckDb
	}
	if msg.StartupVolume > 0 {
		d.StartupVolume = msg.StartupVolume
	}
	if msg.EqBands != nil {
		d.EqBands = append(d.EqBands[:0], msg.EqBands...)
	}
	if msg.EqLoudness != nil {
		d.EqLoudness = *msg.EqLoudness
	}
	if msg.BassShelfHz != nil {
		d.BassShelfHz = *msg.BassShelfHz
	}
	if msg.SubsonicHz != nil {
		d.SubsonicHz = *msg.SubsonicHz
	}
	if msg.BassGuardEnabled != nil {
		d.BassGuardEnabled = *msg.BassGuardEnabled
	}
	if msg.BassGuardDb != nil {
		d.BassGuardDb = *msg.BassGuardDb
	}
	if msg.LimiterEnabled != nil {
		d.LimiterEnabled = *msg.LimiterEnabled
	}
	if msg.LimiterThreshold != nil {
		d.LimiterThreshold = *msg.LimiterThreshold
	}
	if msg.LimiterRelease != nil {
		d.LimiterRelease = *msg.LimiterRelease
	}
	if msg.SendspinServer != "" {
		d.SendspinServer = msg.SendspinServer
	}
	if msg.AfeMicGainDb != nil {
		d.AfeMicGainDb = clampAfeMicGainDb(*msg.AfeMicGainDb)
	}
	if msg.BleProxyEnabled != nil {
		d.BleProxyEnabled = msg.BleProxyEnabled
	}
	if msg.ListeningAnim != nil {
		d.ListeningAnim = msg.ListeningAnim
	}
}

// Snapshot returns a consistent copy of all config values.
func (d *Device) Snapshot() ConfigMessage {
	d.mu.RLock()
	defer d.mu.RUnlock()
	eqBands := append([]float64(nil), d.EqBands...)
	eqLoudness := d.EqLoudness
	bassShelfHz := d.BassShelfHz
	subsonicHz := d.SubsonicHz
	bassGuardEnabled := d.BassGuardEnabled
	bassGuardDb := d.BassGuardDb
	limiterEnabled := d.LimiterEnabled
	limiterThreshold := d.LimiterThreshold
	limiterRelease := d.LimiterRelease
	bargeInEnabled := d.BargeInEnabled
	saveWakeCaptures := d.SaveWakeCaptures
	wakeNearMissFloor := d.WakeNearMissFloor
	saveStopCaptures := d.SaveStopCaptures
	afeMicGainDb := d.AfeMicGainDb
	bleProxyEnabled := false
	if d.BleProxyEnabled != nil {
		bleProxyEnabled = *d.BleProxyEnabled
	}
	return ConfigMessage{
		VadThreshold:      d.VadThreshold,
		VadSpeechMs:       d.VadSpeechMs,
		VadSilenceMs:      d.VadSilenceMs,
		OwwThreshold:      d.OwwThreshold,
		OwwModel:          d.OwwModel,
		StopModel:         d.StopModel,
		StopThreshold:     d.StopThreshold,
		SaveWakeCaptures:  &saveWakeCaptures,
		WakeCaptureSec:    d.WakeCaptureSec,
		WakeNearMissFloor: &wakeNearMissFloor,
		SaveStopCaptures:  &saveStopCaptures,
		StopCaptureSec:    d.StopCaptureSec,
		BargeInEnabled:    &bargeInEnabled,
		BargeInThreshold:  d.BargeInThreshold,
		StartupVolume:     d.StartupVolume,
		EqBands:           eqBands,
		EqLoudness:        &eqLoudness,
		BassShelfHz:       &bassShelfHz,
		SubsonicHz:        &subsonicHz,
		BassGuardEnabled:  &bassGuardEnabled,
		BassGuardDb:       &bassGuardDb,
		LimiterEnabled:    &limiterEnabled,
		LimiterThreshold:  &limiterThreshold,
		LimiterRelease:    &limiterRelease,
		SendspinServer:    d.SendspinServer,
		AfeMicGainDb:      &afeMicGainDb,
		BleProxyEnabled:   &bleProxyEnabled,
		ListeningAnim:     d.ListeningAnim,
	}
}

// ConfigMessage mirrors the JSON shape of the config control message
// sent by the controller. JSON tags must match em_controller.py exactly.
type ConfigMessage struct {
	Type              string    `json:"type,omitempty"`
	AfeMicGainDb      *int      `json:"afeMicGainDb,omitempty"`
	StartupVolume     int       `json:"startupVolume,omitempty"`
	EqBands           []float64 `json:"eqBands,omitempty"`
	EqLoudness        *bool     `json:"eqLoudness,omitempty"`
	BassShelfHz       *float64  `json:"bassShelfHz,omitempty"`
	SubsonicHz        *float64  `json:"subsonicHz,omitempty"`
	BassGuardEnabled  *bool     `json:"bassGuardEnabled,omitempty"`
	BassGuardDb       *float64  `json:"bassGuardDb,omitempty"`
	LimiterEnabled    *bool     `json:"limiterEnabled,omitempty"`
	LimiterThreshold  *float64  `json:"limiterThreshold,omitempty"`
	LimiterRelease    *float64  `json:"limiterRelease,omitempty"`
	SendspinServer    string    `json:"sendspinServer,omitempty"`
	VadThreshold      float64   `json:"vadThreshold,omitempty"`
	VadSpeechMs       int       `json:"vadSpeechMs,omitempty"`
	VadSilenceMs      int       `json:"vadSilenceMs,omitempty"`
	OwwThreshold      float64   `json:"owwThreshold,omitempty"`
	OwwModel          string    `json:"owwModel,omitempty"`
	StopModel         string    `json:"stopModel,omitempty"`
	StopThreshold     float64   `json:"stopThreshold,omitempty"`
	SaveWakeCaptures  *bool     `json:"saveWakeCaptures,omitempty"`
	WakeCaptureSec    float64   `json:"wakeCaptureSec,omitempty"`
	WakeNearMissFloor *float64  `json:"wakeNearMissFloor,omitempty"`
	SaveStopCaptures  *bool     `json:"saveStopCaptures,omitempty"`
	StopCaptureSec    float64   `json:"stopCaptureSec,omitempty"`
	BargeInEnabled    *bool     `json:"bargeInEnabled,omitempty"`
	BargeInThreshold  float64   `json:"bargeInThreshold,omitempty"`
	DuckDb            *float64  `json:"duckDb,omitempty"`
	HasBeamforming    bool      `json:"hasBeamforming,omitempty"`
	BleProxyEnabled   *bool     `json:"bleProxyEnabled,omitempty"`

	// ListeningAnim: raw led_anim spec for the listening ring (#263).
	// Carried as raw JSON so this package does not depend on the
	// animation renderer's types.
	ListeningAnim json.RawMessage `json:"listeningAnim,omitempty"`
}

// clampAfeMicGainDb caps post-processor gain more tightly than direct mic
// gain: AFE delivers S16, so no pre-quantisation headroom remains to recover.
func clampAfeMicGainDb(db int) int {
	if db < 0 {
		return 0
	}
	if db > 24 {
		return 24
	}
	return db
}

func clampFloat(value, low, high float64) float64 {
	if value < low {
		return low
	}
	if value > high {
		return high
	}
	return value
}

// ─── env helpers ──────────────────────────────────────────────────────────────

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}

func envBool(key string, def bool) bool {
	if v := os.Getenv(key); v != "" {
		return v == "1" || v == "true" || v == "True"
	}
	return def
}

func envStr(key string, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
