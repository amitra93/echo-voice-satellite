package buttons_test

import (
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/buttons"
)

func TestClickTypeString(t *testing.T) {
	cases := []struct {
		click buttons.ClickType
		want  string
	}{
		{buttons.DotClick, "dot"},
		{buttons.VolumeUpClick, "volume_up"},
		{buttons.VolumeDownClick, "volume_down"},
		{buttons.MuteClick, "mute"},
		// An unrecognised code must fall back to "unknown" rather than
		// panicking or returning garbage — this is what a firmware/driver
		// mismatch would produce, and it should read as "unknown", not
		// crash the caller.
		{buttons.ClickType(9999), "unknown"},
		{buttons.ClickType(0), "unknown"},
	}
	for _, c := range cases {
		click := c.click
		got := click.String()
		if got != c.want {
			t.Errorf("ClickType(%d).String() = %q, want %q", c.click, got, c.want)
		}
	}
}

func TestEventSubscriptionCancel(t *testing.T) {
	called := false
	sub := buttons.NewEventSubscription(func() { called = true })
	if called {
		t.Fatalf("cancel func ran before Cancel() was called")
	}
	sub.Cancel()
	if !called {
		t.Fatalf("Cancel() did not invoke the underlying cancel func")
	}
}

func TestEventSubscriptionCancelIdempotentCallCount(t *testing.T) {
	// context.CancelFunc is safe to call more than once; EventSubscription
	// adds nothing on top that would make a second Cancel() unsafe, so
	// pin that calling it twice doesn't panic.
	calls := 0
	sub := buttons.NewEventSubscription(func() { calls++ })
	sub.Cancel()
	sub.Cancel()
	if calls != 2 {
		t.Fatalf("expected the wrapped func to run on every Cancel() call, got %d calls", calls)
	}
}
