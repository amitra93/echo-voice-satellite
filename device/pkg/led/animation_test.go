package led

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestParseAnimation(t *testing.T) {
	animation, err := ParseAnimation("# ignored\nloop\n10:0f3,abc,invalid\n20:fff,000\n")
	if err != nil {
		t.Fatal(err)
	}
	if !animation.Looped || len(animation.Steps) != 2 {
		t.Fatalf("animation = %#v, want loop with two steps", animation)
	}
	if got := animation.Steps[0].Duration.Milliseconds(); got != 10 {
		t.Fatalf("first duration = %dms, want 10ms", got)
	}
	if len(animation.Steps[0].LedConfig) != 2 {
		t.Fatalf("first LED config len = %d, want 2 valid LEDs", len(animation.Steps[0].LedConfig))
	}
	if got := animation.Steps[0].LedConfig[0]; got != (Led{ID: 0, R: 0, G: 255, B: 51}) {
		t.Fatalf("first LED = %#v", got)
	}
	if got := animation.Steps[0].LedConfig[1]; got.ID != 1 || got.R != 170 || got.G != 187 || got.B != 204 {
		t.Fatalf("second LED = %#v", got)
	}
}

func TestParseAnimationErrors(t *testing.T) {
	for _, input := range []string{
		"bad:fff\n",
		"10:ggg\n",
	} {
		if _, err := ParseAnimation(input); err == nil {
			t.Errorf("ParseAnimation(%q) returned nil error", input)
		}
	}
	if animation, err := ParseAnimation("\ncomment without a colon\n"); err != nil || len(animation.Steps) != 0 {
		t.Fatalf("ignored lines parsed as animation: %#v, %v", animation, err)
	}
}

func TestAnimatorGetAnimation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "animation")
	if err := os.WriteFile(path, []byte("1:fff\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	a := NewAnimator(nil)
	animation, err := a.GetAnimation(path)
	if err != nil || len(animation.Steps) != 1 {
		t.Fatalf("GetAnimation() = %#v, %v", animation, err)
	}
	if _, err := a.GetAnimation(filepath.Join(dir, "missing")); err == nil {
		t.Fatal("missing animation returned nil error")
	}
}

type recordingController struct {
	calls    int
	last     []Led
	err      error
	onSetLED func()
}

func (c *recordingController) Init() error { return nil }

func (c *recordingController) GetNumLEDs() (int, error) { return ledCount, nil }

func (c *recordingController) SetLEDs(leds ...Led) error {
	c.calls++
	c.last = append([]Led(nil), leds...)
	if c.onSetLED != nil {
		c.onSetLED()
	}
	return c.err
}

func TestAnimatorPlayNonLooped(t *testing.T) {
	controller := &recordingController{}
	a := NewAnimator(controller)
	animation := Animation{Steps: []AnimationStep{{LedConfig: []Led{{ID: 2, R: 1}}}}}
	if err := a.Play(animation, context.Background()); err != nil {
		t.Fatal(err)
	}
	if controller.calls != 1 || len(controller.last) != 1 || controller.last[0].ID != 2 {
		t.Fatalf("controller calls = %d, last = %#v", controller.calls, controller.last)
	}
}

func TestAnimatorPlayCancellationAndLoop(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	controller := &recordingController{onSetLED: cancel}
	a := NewAnimator(controller)
	animation := Animation{Looped: true, Steps: []AnimationStep{{LedConfig: []Led{{ID: 1}}}}}
	if err := a.Play(animation, ctx); err != nil {
		t.Fatal(err)
	}
	if controller.calls != 1 {
		t.Fatalf("loop continued after cancellation: %d calls", controller.calls)
	}
}

func TestAnimatorPlayReturnsControllerError(t *testing.T) {
	want := errors.New("i2c failed")
	controller := &recordingController{err: want}
	err := NewAnimator(controller).Play(Animation{Steps: []AnimationStep{{}}}, context.Background())
	if !errors.Is(err, want) {
		t.Fatalf("Play() error = %v, want %v", err, want)
	}
}

func TestAnimatorPlayReturnsContextError(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 0)
	defer cancel()
	if err := NewAnimator(&recordingController{}).Play(Animation{Steps: []AnimationStep{{LedConfig: []Led{{}}}}}, ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Play() error = %v, want context.DeadlineExceeded", err)
	}
}
