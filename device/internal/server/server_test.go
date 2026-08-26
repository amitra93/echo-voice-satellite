package server

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

type recordingLEDController struct {
	sets [][]led.Led
}

type failingLEDController struct{ get, set bool }

func (c *failingLEDController) Init() error { return nil }
func (c *failingLEDController) GetNumLEDs() (int, error) {
	if c.get {
		return 0, os.ErrNotExist
	}
	return numLEDs, nil
}
func (c *failingLEDController) SetLEDs(...led.Led) error {
	if c.set {
		return os.ErrPermission
	}
	return nil
}

func (c *recordingLEDController) Init() error { return nil }

func (c *recordingLEDController) GetNumLEDs() (int, error) { return numLEDs, nil }

func (c *recordingLEDController) SetLEDs(values ...led.Led) error {
	copyValues := append([]led.Led(nil), values...)
	c.sets = append(c.sets, copyValues)
	return nil
}

func newTestServer(c *recordingLEDController) *Server {
	vc := newVolumeController(func() led.Controller { return c })
	mc := newMuteController(func() led.Controller { return c }, nil)
	return &Server{ledController: c, volume: vc, mute: mc}
}

func listeningFrame() []led.Led {
	values := make([]led.Led, numLEDs)
	for i := range values {
		values[i] = led.Led{ID: i, R: 0, G: 100, B: 0}
	}
	return values
}

func TestClampAdd(t *testing.T) {
	if got := clampAdd(10, 20); got != 30 {
		t.Fatalf("clampAdd(10, 20) = %d, want 30", got)
	}
	if got := clampAdd(250, 20); got != 255 {
		t.Fatalf("clampAdd overflow = %d, want 255", got)
	}
}

func TestVolumeStepDownMarksVolumeAuthoritative(t *testing.T) {
	s := newTestServer(&recordingLEDController{})
	s.volume.Set(80, false)
	s.volumeSeeded.Store(false)
	s.VolumeStepDown()
	if !s.VolumeSeeded() {
		t.Fatal("VolumeStepDown did not mark volume authoritative")
	}
}

func TestGetUptimeReturnsNonnegativeDuration(t *testing.T) {
	uptime, err := getUptime()
	if err != nil {
		t.Fatal(err)
	}
	if uptime < 0 {
		t.Fatalf("getUptime() = %s, want nonnegative duration", uptime)
	}
}

func TestClearLedsHandlesControllerErrors(t *testing.T) {
	clearLeds(&failingLEDController{get: true})
	clearLeds(&failingLEDController{set: true})
}

func TestSetLEDsRecordsAndPaintsBaseState(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	frame := listeningFrame()
	listening := true

	s.SetLEDs(frame, &listening)

	if len(c.sets) != 1 {
		t.Fatalf("SetLEDs made %d hardware calls, want 1", len(c.sets))
	}
	if !s.listeningLEDs || s.ledMode != ledModeSystem {
		t.Fatalf("SetLEDs state: listening=%v mode=%v", s.listeningLEDs, s.ledMode)
	}
	if s.baseLEDs[0] != frame[0] {
		t.Fatalf("base LED 0 = %#v, want %#v", s.baseLEDs[0], frame[0])
	}
}

func TestSetLEDsUsesLegacyGreenListeningHeuristic(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)

	s.SetLEDs(listeningFrame(), nil)
	if !s.listeningLEDs {
		t.Fatal("all-green legacy frame should be recognized as listening")
	}

	nonListening := listeningFrame()
	nonListening[2].B = 1
	s.SetLEDs(nonListening, nil)
	if s.listeningLEDs {
		t.Fatal("non-green legacy frame should not be recognized as listening")
	}
}

func TestSetLEDsSuppressesPaintWhileMutedButUpdatesBase(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.mute.muted = true
	frame := listeningFrame()

	s.SetLEDs(frame, nil)

	if len(c.sets) != 0 {
		t.Fatalf("muted SetLEDs painted %d times, want 0", len(c.sets))
	}
	if s.baseLEDs[0] != frame[0] {
		t.Fatal("muted SetLEDs failed to retain base LED state")
	}
}

func TestSetDirectionLEDsHighlightsListeningRing(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	frame := listeningFrame()
	listening := true
	s.SetLEDs(frame, &listening)
	c.sets = nil

	s.SetDirectionLEDs(0)

	if len(c.sets) != 1 {
		t.Fatalf("direction overlay made %d hardware calls, want 1", len(c.sets))
	}
	painted := c.sets[0]
	if painted[4].R != 150 || painted[4].G != 250 || painted[4].B != 150 {
		t.Fatalf("primary direction LED = %#v, want brightened LED 4", painted[4])
	}
	if painted[3].G != 160 || painted[5].G != 160 {
		t.Fatalf("neighbor direction LEDs = %#v, %#v", painted[3], painted[5])
	}
}

func TestSetDirectionLEDsSkipsInvalidOrNonListeningStates(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)

	s.SetDirectionLEDs(-1)
	if len(c.sets) != 0 {
		t.Fatal("negative angle should not paint")
	}

	s.SetDirectionLEDs(90)
	if len(c.sets) != 0 {
		t.Fatal("non-listening state should not paint")
	}

	listening := true
	s.SetLEDs(listeningFrame(), &listening)
	c.sets = nil
	s.volume.displayActive = true
	s.SetDirectionLEDs(90)
	if len(c.sets) != 0 {
		t.Fatal("volume display should suppress direction overlay")
	}
}

func TestSetLEDsSuppressesPaintDuringVolumeDisplay(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.volume.displayActive = true

	s.SetLEDs(listeningFrame(), nil)

	if len(c.sets) != 0 {
		t.Fatal("volume display should suppress controller LED paint")
	}
}

func TestPaintBaseLEDsCopiesIDsAndHandlesNilController(t *testing.T) {
	c := &recordingLEDController{}
	s := newTestServer(c)
	s.baseLEDs[3] = led.Led{ID: 99, R: 1, G: 2, B: 3}
	s.paintBaseLEDs()
	if len(c.sets) != 1 || c.sets[0][3].ID != 3 {
		t.Fatalf("paintBaseLEDs output = %#v, want copied ID 3", c.sets)
	}

	nilServer := &Server{}
	nilServer.paintBaseLEDs() // nil hardware is expected during boot
}

func TestStatePersistenceHandlesUnreadableAndRenamePaths(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	saveDeviceState(path, deviceState{Muted: true})
	if _, ok := loadDeviceState(path); !ok {
		t.Fatal("saved state was not readable")
	}
	if err := os.WriteFile(path, []byte("not-json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, ok := loadDeviceState(path); ok {
		t.Fatal("invalid JSON was accepted")
	}

	// A path whose parent is a regular file exercises the mkdir failure path;
	// saveDeviceState is deliberately void and must not panic.
	parent := filepath.Join(t.TempDir(), "parent")
	if err := os.WriteFile(parent, []byte("file"), 0o644); err != nil {
		t.Fatal(err)
	}
	saveDeviceState(filepath.Join(parent, "state.json"), deviceState{Muted: true})
}

func TestMuteControllerNotifiesAndPersistsBothTransitions(t *testing.T) {
	m := newMuteController(func() led.Controller { return nil }, nil)
	var changes []bool
	var persists int
	m.SetOnMuteChange(func(muted bool) { changes = append(changes, muted) })
	m.persist = func() { persists++ }

	m.Toggle()
	m.Toggle()
	if m.IsMuted() || len(changes) != 2 || !changes[0] || changes[1] {
		t.Fatalf("mute transitions = muted:%v changes:%v", m.IsMuted(), changes)
	}
	if persists != 2 {
		t.Fatalf("persist calls = %d, want 2", persists)
	}
}

func TestMuteRestoreSetsStateBeforeHardwareIsReady(t *testing.T) {
	m := newMuteController(func() led.Controller { return nil }, nil)
	m.RestoreMuted()
	if !m.IsMuted() {
		t.Fatal("RestoreMuted did not restore muted state")
	}
}

func TestServerVolumeAndMuteCallbacks(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })
	mc := newMuteController(func() led.Controller { return nil }, nil)
	s := &Server{volume: vc, mute: mc}
	var volume int
	var muted bool
	s.SetVolumeChangeCallback(func(level int) { volume = level })
	s.SetMuteChangeCallback(func(value bool) { muted = value })
	s.SetVolume(12)
	s.MuteToggle()
	if volume != 12 || !muted || !s.IsMuted() || s.VolumeLevel() != 12 {
		t.Fatalf("server state volume=%d muteCallback=%v muted=%v level=%d", volume, muted, s.IsMuted(), s.VolumeLevel())
	}
	s.LEDModeDirection()
	s.CancelVolumeDisplay()
	s.RestoreMuteRing()
}
