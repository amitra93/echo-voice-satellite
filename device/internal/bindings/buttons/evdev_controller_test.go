package buttons

import (
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/buttons"
	evdev "github.com/gvalkov/golang-evdev"
)

func keyEvent(code uint16, value int32) evdev.InputEvent {
	return evdev.InputEvent{Type: evdev.EV_KEY, Code: code, Value: value}
}

func synEvent() evdev.InputEvent {
	return evdev.InputEvent{Type: evdev.EV_SYN, Code: 0, Value: 0}
}

func TestClassifyEvent_FiltersNonKeyEvents(t *testing.T) {
	st := newBtnEventState()
	btn := buttons.Button{Type: buttons.DotButton}

	// A raw EV_SYN separator must never be mistaken for a Code==0
	// continuation of the previous click type — see the historical bug
	// documented on classifyEvent.
	action := classifyEvent(btn, st, synEvent(), time.Now())
	if action.Kind != actionNone {
		t.Fatalf("expected actionNone for EV_SYN, got %v", action.Kind)
	}
	// And it must not have perturbed beforeDown, so a real subsequent
	// press is still seen as a down transition.
	if st.beforeDown {
		t.Fatalf("EV_SYN must not flip beforeDown")
	}
}

func TestClassifyEvent_CodeZeroContinuation(t *testing.T) {
	st := newBtnEventState()
	btn := buttons.Button{Type: buttons.DotButton}
	now := time.Now()

	// Press with an explicit click code.
	down := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 1), now)
	if down.Kind != actionEmit || down.Event.ClickType != buttons.DotClick || !down.Event.Down {
		t.Fatalf("unexpected down action: %+v", down)
	}

	// Release arrives as Code==0 (a real firmware quirk this driver has to
	// handle) — it must be attributed to the click type from the press.
	up := classifyEvent(btn, st, keyEvent(0, 0), now.Add(50*time.Millisecond))
	if up.Kind != actionEmit {
		t.Fatalf("expected actionEmit for code==0 release, got %v", up.Kind)
	}
	if up.Event.ClickType != buttons.DotClick {
		t.Fatalf("code==0 release should carry the prior click type, got %v", up.Event.ClickType)
	}
	if up.Event.Down {
		t.Fatalf("release event must have Down=false")
	}
	if up.Event.HeldMs < 40 || up.Event.HeldMs > 100 {
		t.Fatalf("expected heldMs ~50, got %d", up.Event.HeldMs)
	}
}

func TestClassifyEvent_RepeatSuppression(t *testing.T) {
	st := newBtnEventState()
	btn := buttons.Button{Type: buttons.DotButton}
	now := time.Now()

	first := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 1), now)
	if first.Kind != actionEmit {
		t.Fatalf("expected first down to emit, got %v", first.Kind)
	}

	// A held key sends repeat events (Value==1 again, or a re-sent same
	// state) that must be swallowed rather than re-fired as a new press.
	repeat := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 1), now.Add(10*time.Millisecond))
	if repeat.Kind != actionNone {
		t.Fatalf("expected repeat down to be suppressed, got %v", repeat.Kind)
	}

	up := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 0), now.Add(20*time.Millisecond))
	if up.Kind != actionEmit || up.Event.Down {
		t.Fatalf("expected release to emit, got %+v", up)
	}

	// A repeated release must likewise be suppressed.
	repeatUp := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 0), now.Add(30*time.Millisecond))
	if repeatUp.Kind != actionNone {
		t.Fatalf("expected repeat up to be suppressed, got %v", repeatUp.Kind)
	}
}

func TestClassifyEvent_VolumeUpAndDown(t *testing.T) {
	btn := buttons.Button{Type: buttons.VolumeButton}
	now := time.Now()

	for _, tc := range []struct {
		code uint16
		want string
	}{
		{uint16(buttons.VolumeUpClick), "up"},
		{uint16(buttons.VolumeDownClick), "down"},
	} {
		st := newBtnEventState()
		// The volume-intercept branch only fires on release (!down) — a
		// press on the volume device is not special-cased and falls
		// through to the generic emit path just like any other button
		// press, so it still flips the repeat-suppression state via a
		// generic actionEmit rather than actionNone.
		if a := classifyEvent(btn, st, keyEvent(tc.code, 1), now); a.Kind != actionEmit {
			t.Fatalf("volume press falls through to generic emit, got %v", a.Kind)
		}
		release := classifyEvent(btn, st, keyEvent(tc.code, 0), now.Add(5*time.Millisecond))
		if release.Kind != actionVolume {
			t.Fatalf("expected actionVolume, got %v", release.Kind)
		}
		if release.Volume != tc.want {
			t.Fatalf("expected volume %q, got %q", tc.want, release.Volume)
		}
	}
}

func TestClassifyEvent_VolumeButtonUnknownClickIsIgnored(t *testing.T) {
	st := newBtnEventState()
	btn := buttons.Button{Type: buttons.VolumeButton}
	now := time.Now()

	classifyEvent(btn, st, keyEvent(uint16(buttons.MuteClick), 1), now)
	release := classifyEvent(btn, st, keyEvent(uint16(buttons.MuteClick), 0), now.Add(5*time.Millisecond))
	if release.Kind != actionNone {
		t.Fatalf("unmatched click type on volume device must be ignored, got %v", release.Kind)
	}
}

func TestClassifyEvent_MuteOnDotDevice(t *testing.T) {
	st := newBtnEventState()
	btn := buttons.Button{Type: buttons.DotButton}
	now := time.Now()

	classifyEvent(btn, st, keyEvent(uint16(buttons.MuteClick), 1), now)
	release := classifyEvent(btn, st, keyEvent(uint16(buttons.MuteClick), 0), now.Add(5*time.Millisecond))
	if release.Kind != actionMute {
		t.Fatalf("expected actionMute, got %v", release.Kind)
	}
}

func TestClassifyEvent_DotClickHeldMs(t *testing.T) {
	st := newBtnEventState()
	btn := buttons.Button{Type: buttons.DotButton}
	now := time.Now()

	downAction := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 1), now)
	if downAction.Kind != actionEmit || downAction.Event.HeldMs != 0 {
		t.Fatalf("press must emit with HeldMs 0, got %+v", downAction)
	}

	held := 750 * time.Millisecond
	upAction := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 0), now.Add(held))
	if upAction.Kind != actionEmit {
		t.Fatalf("release must emit, got %v", upAction.Kind)
	}
	if upAction.Event.HeldMs != held.Milliseconds() {
		t.Fatalf("expected HeldMs %d, got %d", held.Milliseconds(), upAction.Event.HeldMs)
	}
	// downAt must be cleared on release so a stray later release for the
	// same click type (with nothing currently down) reports HeldMs 0
	// rather than resurrecting the earlier timestamp.
	if _, ok := st.downAt[buttons.DotClick]; ok {
		t.Fatalf("downAt entry must be deleted after release")
	}
}

func TestClassifyEvent_ReleaseWithNoPriorDownIsSuppressed(t *testing.T) {
	st := newBtnEventState()
	btn := buttons.Button{Type: buttons.DotButton}

	// beforeDown starts false (no press seen yet), so a "release" event
	// (down==false) reads as a repeat of the current state and is
	// suppressed by the same beforeDown==down check that swallows repeat
	// events — not emitted with a fabricated HeldMs of 0.
	action := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 0), time.Now())
	if action.Kind != actionNone {
		t.Fatalf("expected actionNone for a release with no prior down, got %v", action.Kind)
	}
}

func TestClassifyEvent_SequentialDotAndMuteHoldsAreIndependent(t *testing.T) {
	st := newBtnEventState()
	btn := buttons.Button{Type: buttons.DotButton}
	now := time.Now()

	// Dot press/release fully completes, THEN mute press/release — this is
	// the realistic case downAt's per-click-type keying exists for (the
	// dot device carries both buttons on one goroutine/state), with no
	// overlap between the two holds. beforeDown is a single flag shared
	// across click types, so true simultaneous overlap is not what this
	// state machine supports; see
	// TestClassifyEvent_StaleClickTypeFromSuppressedInterveningPress for
	// what actually happens when a second press arrives before the first
	// releases.
	dotDown := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 1), now)
	if dotDown.Kind != actionEmit {
		t.Fatalf("expected dot press to emit, got %v", dotDown.Kind)
	}
	dotUp := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 0), now.Add(110*time.Millisecond))
	if dotUp.Kind != actionEmit || dotUp.Event.HeldMs != 110 {
		t.Fatalf("expected dot hold 110ms, got %+v", dotUp)
	}

	classifyEvent(btn, st, keyEvent(uint16(buttons.MuteClick), 1), now.Add(200*time.Millisecond))
	muteUp := classifyEvent(btn, st, keyEvent(uint16(buttons.MuteClick), 0), now.Add(400*time.Millisecond))
	if muteUp.Kind != actionMute {
		t.Fatalf("expected actionMute for mute release, got %v", muteUp.Kind)
	}
	// The mute intercept branch returns before touching downAt, so a
	// dot-hold entry from a fully separate, already-completed press must
	// not still be sitting in the map.
	if _, ok := st.downAt[buttons.DotClick]; ok {
		t.Fatalf("stale downAt entry for dot click type after its own release")
	}
}

func TestClassifyEvent_StaleClickTypeFromSuppressedInterveningPress(t *testing.T) {
	st := newBtnEventState()
	btn := buttons.Button{Type: buttons.DotButton}
	now := time.Now()

	// Press click type A.
	a := classifyEvent(btn, st, keyEvent(uint16(buttons.DotClick), 1), now)
	if a.Kind != actionEmit {
		t.Fatalf("expected first press to emit, got %v", a.Kind)
	}

	// A second down event for a DIFFERENT click type arrives before A is
	// released. beforeDown is already true, so this is suppressed by the
	// repeat check — but classifyEvent updates beforeClickType from the
	// event's Code BEFORE that check runs, so the mutation still lands.
	// The eventual release, delivered as a Code==0 continuation, is then
	// attributed to this second (never-really-down) click type rather
	// than the one physically held, and downAt has no entry for it —
	// pinning this rather than "fixing" it, since it is the extraction's
	// job to reproduce the inline behaviour exactly, not improve on it.
	b := classifyEvent(btn, st, keyEvent(uint16(buttons.VolumeUpClick), 1), now.Add(5*time.Millisecond))
	if b.Kind != actionNone {
		t.Fatalf("expected the second down to be suppressed as a repeat, got %v", b.Kind)
	}

	release := classifyEvent(btn, st, keyEvent(0, 0), now.Add(200*time.Millisecond))
	if release.Kind != actionEmit {
		t.Fatalf("expected release to emit, got %v", release.Kind)
	}
	if release.Event.ClickType != buttons.VolumeUpClick {
		t.Fatalf("expected the stale click type to win, got %v", release.Event.ClickType)
	}
	if release.Event.HeldMs != 0 {
		t.Fatalf("expected HeldMs 0 since downAt has no entry for the stale click type, got %d", release.Event.HeldMs)
	}
}

func TestSubscribeToButton_NilCallback(t *testing.T) {
	e := &EvDevController{}
	sub, err := e.SubscribeToButton(nil)
	if err == nil || sub != nil {
		t.Fatalf("expected error and nil subscription for nil callback, got sub=%v err=%v", sub, err)
	}
}

func TestGetDotAndVolumeButton(t *testing.T) {
	e := &EvDevController{}
	if got := e.GetDotButton().Type; got != buttons.DotButton {
		t.Fatalf("expected DotButton, got %v", got)
	}
	if got := e.GetVolumeButton().Type; got != buttons.VolumeButton {
		t.Fatalf("expected VolumeButton, got %v", got)
	}
}

func TestSetVolumeAndMuteCallback(t *testing.T) {
	e := &EvDevController{}
	called := false
	e.SetVolumeCallback(func(direction string) { called = direction == "up" })
	e.volumeCallback("up")
	if !called {
		t.Fatalf("expected volume callback to be invoked")
	}

	muted := false
	e.SetMuteCallback(func() { muted = true })
	e.muteCallback()
	if !muted {
		t.Fatalf("expected mute callback to be invoked")
	}
}
