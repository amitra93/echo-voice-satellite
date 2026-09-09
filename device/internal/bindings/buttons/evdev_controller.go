package buttons

import (
	"context"
	"errors"
	"time"
	"github.com/wilbowes/EchoMuse/pkg/buttons"
	evdev "github.com/gvalkov/golang-evdev"
	"os/exec"
)

const dotButton = "/dev/input/event1"
const volumeButton = "/dev/input/event2"

// VolumeCallback is called on volume button release with direction "up" or "down".
type VolumeCallback func(direction string)

// MuteCallback is called on mute button release.
type MuteCallback func()

type EvDevController struct {
	volumeCallback func(direction string)
	muteCallback   func()
}

// SetVolumeCallback registers a function to be called on volume button events.
// Must be called before SubscribeToButton.
func (e *EvDevController) SetVolumeCallback(cb func(direction string)) {
	e.volumeCallback = cb
}

// SetMuteCallback registers a function to be called on mute button events.
// Must be called before SubscribeToButton.
func (e *EvDevController) SetMuteCallback(cb func()) {
	e.muteCallback = cb
}

// Init the button listeners
// Kills alexa's native button functions
func (e *EvDevController) Init() error {
	cmd := exec.Command("stop", "acebutton")
	return cmd.Run()
}

func (e *EvDevController) SubscribeToButton(callback buttons.ButtonClickCallback) (*buttons.EventSubscription, error) {
	if callback == nil {
		return nil, errors.New("callback can't be nil")
	}

	dotBtn := e.GetDotButton()
	volBtn := e.GetVolumeButton()
	dotDevice, err := evdev.Open(dotButton)
	if err != nil {
		return nil, err
	}
	volDevice, err := evdev.Open(volumeButton)
	if err != nil {
		return nil, err
	}

	ctx, cancel := context.WithCancel(context.Background())
	eventSub := buttons.NewEventSubscription(cancel)

	readBtn := func(btn buttons.Button, btnDevice *evdev.InputDevice) {
		defer btnDevice.Release()

		st := newBtnEventState()

		for {
			if ctx.Err() != nil {
				return
			}

			inputEvent, err := btnDevice.ReadOne()
			if err != nil {
				return
			}

			// classifyEvent holds all the decision logic (EV_SYN filtering,
			// the code==0 continuation quirk, repeat suppression, hold
			// timing, volume/mute routing) so it can be unit tested without
			// a real /dev/input device — see its doc comment.
			action := classifyEvent(btn, st, *inputEvent, time.Now())

			switch action.Kind {
			case actionNone:
				continue
			case actionVolume:
				if e.volumeCallback != nil {
					e.volumeCallback(action.Volume)
				}
			case actionMute:
				if e.muteCallback != nil {
					e.muteCallback()
				}
			case actionEmit:
				callback(action.Event)
			}
		}
	}

	go readBtn(dotBtn, dotDevice)
	go readBtn(volBtn, volDevice)

	return eventSub, nil
}

// btnEventState is the per-device-goroutine state readBtn accumulates
// across events: which click type a Code==0 continuation event refers to,
// whether the button is currently down (for repeat suppression), and the
// down-timestamp per click type. Keyed by click type, not a single
// timestamp, because the dot device carries the mute button too, and
// interleaving the two must not attribute one button's hold to the other.
type btnEventState struct {
	beforeClickType buttons.ClickType
	beforeDown      bool
	downAt          map[buttons.ClickType]time.Time
}

func newBtnEventState() *btnEventState {
	return &btnEventState{downAt: map[buttons.ClickType]time.Time{}}
}

type btnActionKind int

const (
	// actionNone means the event carries no decision — a non-EV_KEY event
	// (notably the EV_SYN separator, see classifyEvent) or a repeat of the
	// already-current down/up state.
	actionNone btnActionKind = iota
	actionVolume
	actionMute
	actionEmit
)

// btnAction is what readBtn's loop should do in response to one evdev
// event, as decided by classifyEvent.
type btnAction struct {
	Kind btnActionKind
	// Volume is "up" or "down", set only when Kind == actionVolume.
	Volume string
	// Event is the click event to dispatch, set only when Kind == actionEmit.
	Event buttons.ButtonClickEvent
}

// classifyEvent applies one evdev input event against accumulated state and
// decides what readBtn's loop should do next. Pure — no device I/O, no
// callback invocation — so it is host-testable without a real
// /dev/input device. Must stay behaviourally identical to the inline
// version it was extracted from.
func classifyEvent(btn buttons.Button, st *btnEventState, ev evdev.InputEvent, now time.Time) btnAction {
	// Only key events. Every key press is followed immediately by an
	// EV_SYN separator whose Code and Value are both 0 — and without this
	// filter that SYN fell through to the Code==0 branch, took the
	// previous click type, computed Value==1 as FALSE, and fired a
	// "release" microseconds after the press.
	//
	// So the button has always acted on the SYN rather than on the real
	// release, which is why it felt instant and why the actual release (a
	// genuine transition to 0) was then swallowed as a no-change. Invisible
	// until something needed to know how long the button was held: heldMs
	// came out at ~0 every time.
	if ev.Type != evdev.EV_KEY {
		return btnAction{Kind: actionNone}
	}

	clickType := buttons.ClickType(ev.Code)
	if ev.Code != 0 {
		st.beforeClickType = clickType
	} else {
		clickType = st.beforeClickType
	}

	down := ev.Value == 1
	if st.beforeDown == down {
		return btnAction{Kind: actionNone}
	}
	st.beforeDown = down

	// Intercept volume events on volume device
	if btn.Type == buttons.VolumeButton && !down {
		switch clickType {
		case buttons.VolumeUpClick:
			return btnAction{Kind: actionVolume, Volume: "up"}
		case buttons.VolumeDownClick:
			return btnAction{Kind: actionVolume, Volume: "down"}
		}
		return btnAction{Kind: actionNone}
	}

	// Intercept mute on dot device
	if btn.Type == buttons.DotButton && !down && clickType == buttons.MuteClick {
		return btnAction{Kind: actionMute}
	}

	var heldMs int64
	if down {
		st.downAt[clickType] = now
	} else if t, ok := st.downAt[clickType]; ok {
		heldMs = now.Sub(t).Milliseconds()
		delete(st.downAt, clickType)
	}

	return btnAction{
		Kind: actionEmit,
		Event: buttons.ButtonClickEvent{
			Button:    btn,
			ClickType: clickType,
			Down:      down,
			HeldMs:    heldMs,
		},
	}
}

func (e *EvDevController) GetVolumeButton() buttons.Button {
	return buttons.Button{
		Type: buttons.VolumeButton,
	}
}

func (e *EvDevController) GetDotButton() buttons.Button {
	return buttons.Button{
		Type: buttons.DotButton,
	}
}

func NewButtonController() (*EvDevController, error) {
	controller := &EvDevController{}
	if err := controller.Init(); err != nil {
		return nil, err
	}
	return controller, nil
}