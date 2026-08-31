package sendspin

import "testing"

// The exact status format captured from the device
// (/proc/asound/card0/pcm23p/sub0/status).
const realStatus = `state: RUNNING
owner_pid   : 1215
trigger_time: 1787757972.035414923
tstamp      : 1787820126.753771314
delay       : 6944
avail       : 1248
avail_max   : 6192
-----
hw_ptr      : 835937568
appl_ptr    : 835944512
`

func TestParsePcmStatusReal(t *testing.T) {
	st, err := ParsePcmStatus([]byte(realStatus))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if !st.Running() {
		t.Fatalf("state=%q want RUNNING", st.State)
	}
	if st.HwPtr != 835937568 {
		t.Fatalf("hw_ptr=%d", st.HwPtr)
	}
	if st.ApplPtr != 835944512 {
		t.Fatalf("appl_ptr=%d", st.ApplPtr)
	}
	if st.Delay != 6944 {
		t.Fatalf("delay=%d", st.Delay)
	}
	if st.Avail != 1248 {
		t.Fatalf("avail=%d", st.Avail)
	}
	// 1787820126.753771314 -> ns
	if want := int64(1787820126)*1_000_000_000 + 753771314; st.TstampNs != want {
		t.Fatalf("tstamp ns=%d want %d", st.TstampNs, want)
	}
	if want := int64(1787757972)*1_000_000_000 + 35414923; st.TriggerNs != want {
		t.Fatalf("trigger ns=%d want %d", st.TriggerNs, want)
	}
}

func TestParsePcmStatusRejectsNonPlayback(t *testing.T) {
	if _, err := ParsePcmStatus([]byte("state: closed\n")); err == nil {
		t.Fatal("expected error when hw_ptr is absent")
	}
}

func TestParseSecNsPaddingAndTruncation(t *testing.T) {
	cases := map[string]int64{
		"5.5":          5_500_000_000,             // padded to 9 digits
		"5.000000001":  5_000_000_001,             // full ns
		"5":            5_000_000_000,             // no fractional part
		"5.1234567890": 5_000_000_000 + 123456789, // truncated to 9
	}
	for in, want := range cases {
		if got := parseSecNs(in); got != want {
			t.Fatalf("parseSecNs(%q)=%d want %d", in, got, want)
		}
	}
}

func TestPlaybackClockTracksMeasuredRate(t *testing.T) {
	c := NewPlaybackClock(48000)
	if c.Rate() != 48000 {
		t.Fatalf("initial rate=%v", c.Rate())
	}
	// Feed samples advancing at ~47973 fps (the measured real rate): 47973
	// frames per 1e9 ns. Many samples drive the EWMA toward it.
	hw := int64(1_000_000)
	ts := int64(10_000_000_000)
	for i := 0; i < 200; i++ {
		hw += 47973
		ts += 1_000_000_000
		c.Observe(PcmStatus{State: "RUNNING", HwPtr: hw, TstampNs: ts, Delay: 6944})
	}
	if r := c.Rate(); r < 47960 || r > 47986 {
		t.Fatalf("rate did not converge to ~47973: %v", r)
	}
	// AudibleFrameAt half a second advances by ~rate/2 frames from hw_ptr.
	// delay describes appl_ptr-hw_ptr and must not move the hardware clock.
	base := hw
	got, ok := c.AudibleFrameAt(ts + 500_000_000)
	if !ok {
		t.Fatal("AudibleFrameAt not ready")
	}
	if d := got - base; d < 23980 || d > 23995 {
		t.Fatalf("audible advance=%d want ~23986", d)
	}
}

func TestPlaybackClockIgnoresQueueDelayChanges(t *testing.T) {
	c := NewPlaybackClock(48000)
	c.Observe(PcmStatus{State: "RUNNING", HwPtr: 100_000, TstampNs: 1_000_000_000, Delay: 2048})
	first, ok := c.AudibleFrameAt(1_000_000_000)
	if !ok || first != 100_000 {
		t.Fatalf("first frame=(%d, %v), want (100000, true)", first, ok)
	}
	c.Observe(PcmStatus{State: "RUNNING", HwPtr: 148_000, TstampNs: 2_000_000_000, Delay: 8192})
	second, ok := c.AudibleFrameAt(2_000_000_000)
	if !ok || second != 148_000 {
		t.Fatalf("second frame=(%d, %v), want (148000, true)", second, ok)
	}
	if second-first != 48_000 {
		t.Fatalf("delay change moved playback clock by %d frames, want 48000", second-first)
	}
}

func TestPlaybackClockResetsWhenStopped(t *testing.T) {
	c := NewPlaybackClock(48000)
	c.Observe(PcmStatus{State: "RUNNING", HwPtr: 1000, TstampNs: 1_000_000_000, Delay: 100})
	c.Observe(PcmStatus{State: "XRUN", HwPtr: 2000, TstampNs: 2_000_000_000})
	if c.Running() {
		t.Fatal("expected not running after XRUN")
	}
	if _, ok := c.AudibleFrameAt(3_000_000_000); ok {
		t.Fatal("AudibleFrameAt should be unavailable after a stop")
	}
}
