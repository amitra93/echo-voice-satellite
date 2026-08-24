package config

import (
	"sync"
	"testing"
)

func boolPtr(b bool) *bool        { return &b }
func intPtr(i int) *int           { return &i }
func floatPtr(f float64) *float64 { return &f }

// ─── env helpers ────────────────────────────────────────────────────────────

func TestEnvInt(t *testing.T) {
	t.Setenv("EM_TEST_ENVINT", "42")
	if got := envInt("EM_TEST_ENVINT", 7); got != 42 {
		t.Errorf("envInt(set) = %d, want 42", got)
	}
	if got := envInt("EM_TEST_ENVINT_UNSET", 7); got != 7 {
		t.Errorf("envInt(unset) = %d, want default 7", got)
	}
	t.Setenv("EM_TEST_ENVINT_BAD", "not-a-number")
	if got := envInt("EM_TEST_ENVINT_BAD", 9); got != 9 {
		t.Errorf("envInt(unparsable) = %d, want default 9", got)
	}
}

func TestEnvFloat(t *testing.T) {
	t.Setenv("EM_TEST_ENVFLOAT", "3.25")
	if got := envFloat("EM_TEST_ENVFLOAT", 1.0); got != 3.25 {
		t.Errorf("envFloat(set) = %v, want 3.25", got)
	}
	if got := envFloat("EM_TEST_ENVFLOAT_UNSET", 1.5); got != 1.5 {
		t.Errorf("envFloat(unset) = %v, want default 1.5", got)
	}
	t.Setenv("EM_TEST_ENVFLOAT_BAD", "nope")
	if got := envFloat("EM_TEST_ENVFLOAT_BAD", 2.0); got != 2.0 {
		t.Errorf("envFloat(unparsable) = %v, want default 2.0", got)
	}
}

func TestEnvBool(t *testing.T) {
	cases := []struct {
		val  string
		want bool
	}{
		{"1", true},
		{"true", true},
		{"True", true},
		{"0", false},
		{"false", false},
		{"garbage", false},
	}
	for _, c := range cases {
		t.Setenv("EM_TEST_ENVBOOL", c.val)
		if got := envBool("EM_TEST_ENVBOOL", false); got != c.want {
			t.Errorf("envBool(%q) = %v, want %v", c.val, got, c.want)
		}
	}
	if got := envBool("EM_TEST_ENVBOOL_UNSET", true); got != true {
		t.Errorf("envBool(unset) = %v, want default true", got)
	}
}

func TestEnvStr(t *testing.T) {
	t.Setenv("EM_TEST_ENVSTR", "hello")
	if got := envStr("EM_TEST_ENVSTR", "default"); got != "hello" {
		t.Errorf("envStr(set) = %q, want hello", got)
	}
	if got := envStr("EM_TEST_ENVSTR_UNSET", "default"); got != "default" {
		t.Errorf("envStr(unset) = %q, want default", got)
	}
}

// ─── clampMicGainDb ─────────────────────────────────────────────────────────

func TestClampMicGainDb(t *testing.T) {
	cases := []struct {
		in, want int
	}{
		{-10, 0},
		{-1, 0},
		{0, 0},
		{24, 24},
		{42, 42},
		{43, 42},
		{1000, 42},
	}
	for _, c := range cases {
		if got := clampMicGainDb(c.in); got != c.want {
			t.Errorf("clampMicGainDb(%d) = %d, want %d", c.in, got, c.want)
		}
	}
}

// ─── normaliseOnDevice ──────────────────────────────────────────────────────

func TestNormaliseOnDevice(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"off", OnDeviceOff},
		{"", OnDeviceOff},
		{"shadow", OnDeviceShadow},
		{"  Shadow  ", OnDeviceShadow},
		{"ON", OnDeviceOn},
		{"on", OnDeviceOn},
		// Unrecognised must degrade to off, never guess at shadow or on —
		// see the doc comment on normaliseOnDevice for why.
		{"bogus", OnDeviceOff},
		{"trigger", OnDeviceOff},
	}
	for _, c := range cases {
		if got := normaliseOnDevice(c.in); got != c.want {
			t.Errorf("normaliseOnDevice(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// ─── Device.Apply ───────────────────────────────────────────────────────────

func TestApplyIgnoresZeroFields(t *testing.T) {
	// A partial config push must never zero out fields it didn't mention —
	// that's the whole point of Apply's "non-zero means set" contract.
	d := &Device{initialised: true, VadThreshold: 0.01, OwwModel: "orig", MicGainDb: 12}
	d.Apply(ConfigMessage{})
	if d.VadThreshold != 0.01 || d.OwwModel != "orig" || d.MicGainDb != 12 {
		t.Fatalf("Apply(empty) changed fields: %+v", d)
	}
}

func TestApplySetsProvidedFields(t *testing.T) {
	d := &Device{initialised: true}
	mg := 30
	d.Apply(ConfigMessage{
		VadThreshold: 0.02,
		OwwModel:     "new_model",
		MicGainDb:    &mg,
	})
	if d.VadThreshold != 0.02 {
		t.Errorf("VadThreshold = %v, want 0.02", d.VadThreshold)
	}
	if d.OwwModel != "new_model" {
		t.Errorf("OwwModel = %q, want new_model", d.OwwModel)
	}
	if d.MicGainDb != 30 {
		t.Errorf("MicGainDb = %d, want 30 (clamped-in-range passthrough)", d.MicGainDb)
	}
}

func TestApplyClampsMicGainDb(t *testing.T) {
	d := &Device{initialised: true}
	d.Apply(ConfigMessage{MicGainDb: intPtr(100)})
	if d.MicGainDb != 42 {
		t.Fatalf("Apply did not clamp MicGainDb: got %d, want 42", d.MicGainDb)
	}
}

func TestApplyNormalisesOwwOnDevice(t *testing.T) {
	d := &Device{initialised: true}
	d.Apply(ConfigMessage{OwwOnDevice: "not-a-real-mode"})
	if d.OwwOnDevice != OnDeviceOff {
		t.Fatalf("Apply did not normalise OwwOnDevice: got %q, want %q", d.OwwOnDevice, OnDeviceOff)
	}
}

// DuckDb is negative-going, so it's pointer-typed specifically so that a
// legitimate 0dB setting is distinguishable from "field absent" — this is
// the one field in the message that inverts the usual non-zero rule.
func TestApplyDuckDbZeroIsALegitimateValue(t *testing.T) {
	d := &Device{initialised: true, DuckDb: -18}
	d.Apply(ConfigMessage{DuckDb: floatPtr(0)})
	if d.DuckDb != 0 {
		t.Fatalf("Apply(DuckDb=0) left DuckDb = %v, want 0", d.DuckDb)
	}
}

func TestApplyLoadsDefaultsWhenNotInitialised(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{VadThreshold: 0.5})
	if !d.initialised {
		t.Fatalf("Apply did not mark the device initialised")
	}
	if d.VadThreshold != 0.5 {
		t.Fatalf("explicit field lost after implicit loadDefaults: VadThreshold = %v, want 0.5", d.VadThreshold)
	}
	// A field the message didn't mention must have picked up its default,
	// not been left at the Go zero value.
	if d.OwwModel == "" {
		t.Fatalf("OwwModel is empty; loadDefaults should have populated it")
	}
}

func TestApplyBoolPointerFieldsReplaceDirectly(t *testing.T) {
	d := &Device{initialised: true}
	d.Apply(ConfigMessage{
		BargeInEnabled:     boolPtr(true),
		BeamformingEnabled: boolPtr(false),
		AgcEnabled:         boolPtr(false),
		AecEnabled:         boolPtr(true),
		BleProxyEnabled:    boolPtr(true),
	})
	if !d.BargeInEnabled {
		t.Errorf("BargeInEnabled not applied")
	}
	if d.BeamformingEnabled {
		t.Errorf("BeamformingEnabled not applied")
	}
	if d.AgcEnabled == nil || *d.AgcEnabled != false {
		t.Errorf("AgcEnabled not applied: %v", d.AgcEnabled)
	}
	if d.AecEnabled == nil || *d.AecEnabled != true {
		t.Errorf("AecEnabled not applied: %v", d.AecEnabled)
	}
	if d.BleProxyEnabled == nil || *d.BleProxyEnabled != true {
		t.Errorf("BleProxyEnabled not applied: %v", d.BleProxyEnabled)
	}
}

// ─── Device.Snapshot ────────────────────────────────────────────────────────

func TestSnapshotDefaultsWhenPointersNil(t *testing.T) {
	d := &Device{initialised: true}
	snap := d.Snapshot()
	if snap.AgcEnabled == nil || *snap.AgcEnabled != true {
		t.Errorf("Snapshot AgcEnabled default = %v, want true", snap.AgcEnabled)
	}
	if snap.AecEnabled == nil || *snap.AecEnabled != false {
		t.Errorf("Snapshot AecEnabled default = %v, want false", snap.AecEnabled)
	}
	if snap.BleProxyEnabled == nil || *snap.BleProxyEnabled != false {
		t.Errorf("Snapshot BleProxyEnabled default = %v, want false", snap.BleProxyEnabled)
	}
}

// This is the C4 fix the code comment describes: Snapshot must copy values
// into fresh locals, never hand out a pointer into the live mutex-guarded
// struct. A caller reading the snapshot after the config changes underneath
// it (a later Apply()) would otherwise see a torn or unexpected value.
func TestSnapshotDoesNotAliasLiveFields(t *testing.T) {
	agc := true
	d := &Device{initialised: true, BeamAngle: 45, AgcEnabled: &agc}
	snap := d.Snapshot()

	d.BeamAngle = 999
	*d.AgcEnabled = false

	if *snap.BeamAngle != 45 {
		t.Errorf("snapshot BeamAngle aliased live field: got %v, want 45", *snap.BeamAngle)
	}
	if *snap.AgcEnabled != true {
		t.Errorf("snapshot AgcEnabled aliased live pointer: got %v, want true", *snap.AgcEnabled)
	}
}

// ─── Get ────────────────────────────────────────────────────────────────────

func TestGetIsASingleton(t *testing.T) {
	a := Get()
	b := Get()
	if a != b {
		t.Fatalf("Get() returned different pointers on repeated calls")
	}
}

func TestApplyAndSnapshotAreSafeConcurrently(t *testing.T) {
	d := &Device{}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				d.Apply(ConfigMessage{VadThreshold: float64(i + j + 1)})
				d.Snapshot()
			}
		}(i)
	}
	wg.Wait()
	if d.Snapshot().VadThreshold <= 0 {
		t.Fatalf("concurrent Apply/Snapshot left an invalid threshold")
	}
}
