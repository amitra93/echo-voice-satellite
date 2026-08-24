package jack

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// State reads a fixed path, so the tests point it at a temp file by swapping
// the package variable the read goes through.
func withStateFile(t *testing.T, content string) {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "state")
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	old := statePath
	statePath = p
	t.Cleanup(func() { statePath = old })
}

func TestStateValues(t *testing.T) {
	// 0 none, 1 headset (with mic), 2 headphone — the three accdet reports.
	// Both 1 and 2 are "something is plugged in" as far as the speaker amp is
	// concerned; only 0 means the Dot should play to the room again.
	cases := []struct {
		content  string
		want     int
		inserted bool
	}{
		{"0\n", 0, false},
		{"1\n", 1, true},
		{"2\n", 2, true},
		{"1", 1, true}, // no trailing newline
	}
	for _, c := range cases {
		withStateFile(t, c.content)
		got, ok := State()
		if !ok {
			t.Fatalf("State() not ok for %q", c.content)
		}
		if got != c.want {
			t.Fatalf("State() = %d for %q, want %d", got, c.content, c.want)
		}
		if Inserted() != c.inserted {
			t.Fatalf("Inserted() = %v for %q, want %v", Inserted(), c.content, c.inserted)
		}
	}
}

// A device with no accdet must not read as "plug removed" — that would have us
// asserting the speaker amp on hardware we know nothing about.
func TestMissingSwitchIsNotAnUnplug(t *testing.T) {
	old := statePath
	statePath = filepath.Join(t.TempDir(), "does-not-exist")
	t.Cleanup(func() { statePath = old })

	if _, ok := State(); ok {
		t.Fatal("a missing state file must report not-ok")
	}
	if Present() {
		t.Fatal("Present() must be false with no state file")
	}
	if Inserted() {
		t.Fatal("a missing state file must not read as inserted")
	}
}

func TestUnparseableStateIsNotOk(t *testing.T) {
	withStateFile(t, "banana\n")
	if _, ok := State(); ok {
		t.Fatal("unparseable content must report not-ok, not a state")
	}
}

func TestWatchReportsTransitionsAndIgnoresBadReads(t *testing.T) {
	withStateFile(t, "0\n")
	oldInterval := pollInterval
	pollInterval = time.Millisecond
	t.Cleanup(func() { pollInterval = oldInterval })

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	changes := make(chan bool, 2)
	go Watch(ctx, func(inserted bool) { changes <- inserted })

	// The initial state seeds the baseline and must not produce an event.
	time.Sleep(5 * time.Millisecond)
	if err := os.WriteFile(statePath, []byte("1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	select {
	case got := <-changes:
		if !got {
			t.Fatal("insert transition reported as removal")
		}
	case <-time.After(time.Second):
		t.Fatal("watcher did not report insertion")
	}

	if err := os.WriteFile(statePath, []byte("bad\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	time.Sleep(5 * time.Millisecond)
	if len(changes) != 0 {
		t.Fatal("invalid state produced a transition")
	}

	if err := os.WriteFile(statePath, []byte("0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	select {
	case got := <-changes:
		if got {
			t.Fatal("removal transition reported as insertion")
		}
	case <-time.After(time.Second):
		t.Fatal("watcher did not report removal")
	}
	cancel()
}

func TestWatchReturnsWhenSwitchIsMissing(t *testing.T) {
	old := statePath
	statePath = filepath.Join(t.TempDir(), "missing")
	t.Cleanup(func() { statePath = old })
	Watch(context.Background(), func(bool) { t.Fatal("callback fired without switch") })
}
