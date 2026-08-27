package sendspin

import (
	"bufio"
	"bytes"
	"errors"
	"strconv"
	"strings"
)

// PcmStatus is the parsed content of an ALSA substream status file
// (/proc/asound/card0/pcm23p/sub0/status). hw_ptr is the DMA read position in
// frames and tstamp is the CLOCK_MONOTONIC instant it was sampled — together
// they give a sub-period-accurate playback position, which is why decision 4B
// schedules against this rather than CLOCK_MONOTONIC alone. hw_ptr is already
// the hardware playback position; delay is appl_ptr-hw_ptr, not an additional
// offset to subtract from hw_ptr.
type PcmStatus struct {
	State     string
	HwPtr     int64 // frames the hardware has consumed
	ApplPtr   int64 // frames the application has written
	Delay     int64 // frames between appl_ptr and the speaker
	Avail     int64
	TstampNs  int64 // monotonic ns at which HwPtr was sampled
	TriggerNs int64 // monotonic ns the stream started
}

// Running reports whether the substream was actively playing when sampled.
func (s PcmStatus) Running() bool { return s.State == "RUNNING" }

var errNoHwPtr = errors.New("pcm status: no hw_ptr field")

// ParsePcmStatus parses the ALSA status file. Unknown keys and the "-----"
// separator are ignored; a missing hw_ptr is an error (the file was not a
// playback status), but a missing tstamp is tolerated as 0.
func ParsePcmStatus(data []byte) (PcmStatus, error) {
	var st PcmStatus
	seenHwPtr := false
	sc := bufio.NewScanner(bytes.NewReader(data))
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "-----") {
			continue
		}
		key, val, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		val = strings.TrimSpace(val)
		switch key {
		case "state":
			st.State = val
		case "hw_ptr":
			st.HwPtr = parseInt(val)
			seenHwPtr = true
		case "appl_ptr":
			st.ApplPtr = parseInt(val)
		case "delay":
			st.Delay = parseInt(val)
		case "avail":
			st.Avail = parseInt(val)
		case "tstamp":
			st.TstampNs = parseSecNs(val)
		case "trigger_time":
			st.TriggerNs = parseSecNs(val)
		}
	}
	if err := sc.Err(); err != nil {
		return st, err
	}
	if !seenHwPtr {
		return st, errNoHwPtr
	}
	return st, nil
}

func parseInt(s string) int64 {
	n, _ := strconv.ParseInt(strings.TrimSpace(s), 10, 64)
	return n
}

// parseSecNs turns an ALSA "seconds.nanoseconds" timestamp into ns. The
// fractional part is exactly 9 digits on this kernel, but it is padded/
// truncated defensively so a differently-formatted field cannot skew the ns.
func parseSecNs(s string) int64 {
	s = strings.TrimSpace(s)
	secStr, fracStr, ok := strings.Cut(s, ".")
	sec := parseInt(secStr)
	if !ok {
		return sec * 1_000_000_000
	}
	if len(fracStr) > 9 {
		fracStr = fracStr[:9]
	}
	for len(fracStr) < 9 {
		fracStr += "0"
	}
	return sec*1_000_000_000 + parseInt(fracStr)
}

// PlaybackClock tracks the measured hardware playback rate from successive
// status samples. The DAC's real rate differs from the 48000 nominal (measured
// 47973 fps on this hardware), and it is that measured rate that scheduled
// playback must run against to avoid slow drift.
type PlaybackClock struct {
	nominalRate float64
	rate        float64
	alpha       float64 // EWMA weight for a new instantaneous rate sample

	haveLast bool
	lastHw   int64
	lastTsNs int64
	running  bool
}

// NewPlaybackClock starts at the nominal rate until real samples refine it.
func NewPlaybackClock(nominalRate float64) *PlaybackClock {
	return &PlaybackClock{nominalRate: nominalRate, rate: nominalRate, alpha: 0.1}
}

// Observe feeds a fresh status sample and refines the rate estimate. A stopped
// or non-advancing substream resets the tracking so a later start re-anchors.
func (c *PlaybackClock) Observe(s PcmStatus) {
	c.running = s.Running()
	if !c.running || s.TstampNs == 0 {
		c.haveLast = false
		return
	}
	if c.haveLast && s.TstampNs > c.lastTsNs && s.HwPtr > c.lastHw {
		dtSec := float64(s.TstampNs-c.lastTsNs) / 1e9
		inst := float64(s.HwPtr-c.lastHw) / dtSec
		// Reject implausible instantaneous rates (a wrapped counter or a stale
		// read) rather than letting them poison the EWMA.
		if inst > c.nominalRate*0.5 && inst < c.nominalRate*1.5 {
			c.rate = (1-c.alpha)*c.rate + c.alpha*inst
		}
	}
	c.haveLast = true
	c.lastHw = s.HwPtr
	c.lastTsNs = s.TstampNs
}

// Rate returns the current measured frames-per-second estimate.
func (c *PlaybackClock) Rate() float64 { return c.rate }

// Running reports the substream state at the last Observe.
func (c *PlaybackClock) Running() bool { return c.running }

// AudibleFrameAt projects the hardware playback position at monotonic time
// nowNs, using the measured rate. ALSA delay is the queued distance from
// appl_ptr back to hw_ptr; subtracting it here double-counts that queue and
// makes occupancy changes jump the presentation clock.
func (c *PlaybackClock) AudibleFrameAt(nowNs int64) (int64, bool) {
	if !c.haveLast {
		return 0, false
	}
	elapsedSec := float64(nowNs-c.lastTsNs) / 1e9
	return c.lastHw + int64(c.rate*elapsedSec), true
}
