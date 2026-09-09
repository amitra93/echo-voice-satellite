package capture

import "testing"

func TestSnapshotEndingKeepsSequenceOrderAndWraps(t *testing.T) {
	r := New(3)
	for i := 65534; i <= 65536; i++ {
		r.Push(uint16(i), []byte{byte(i)})
	}
	got, complete := r.SnapshotFrom(0, 2)
	if !complete {
		t.Fatal("complete wrapped snapshot was rejected")
	}
	if len(got) != 3 || got[0].Sequence != 65534 || got[1].Sequence != 65535 || got[2].Sequence != 0 {
		t.Fatalf("snapshot = %#v", got)
	}
}

func TestSnapshotEndingReturnsCopies(t *testing.T) {
	r := New(2)
	r.Push(1, []byte{1})
	got, complete := r.SnapshotFrom(1, 0)
	if !complete {
		t.Fatal("snapshot was rejected")
	}
	got[0].PCM[0] = 9
	got, _ = r.SnapshotFrom(1, 0)
	if got[0].PCM[0] != 1 {
		t.Fatalf("snapshot aliases ring storage: %v", got[0].PCM)
	}
}

func TestNewClampsSubOneCapacityToDefault(t *testing.T) {
	r := New(0)
	if r.capacity != DefaultFrames {
		t.Fatalf("New(0) capacity = %d, want DefaultFrames (%d)", r.capacity, DefaultFrames)
	}
	r = New(-5)
	if r.capacity != DefaultFrames {
		t.Fatalf("New(-5) capacity = %d, want DefaultFrames (%d)", r.capacity, DefaultFrames)
	}
}

func TestSnapshotFromRejectsUnknownActivation(t *testing.T) {
	r := New(5)
	r.Push(1, []byte{1})
	if got, complete := r.SnapshotFrom(99, 0); complete || got != nil {
		t.Fatalf("unknown activation accepted: %#v", got)
	}
}

func TestSnapshotFromExcludesFramesAfterAGapFollowingActivation(t *testing.T) {
	r := New(5)
	r.Push(1, []byte{1})
	r.Push(2, []byte{2}) // activation
	r.Push(4, []byte{4}) // gap after the activation — sequence jumps 2 -> 4
	got, complete := r.SnapshotFrom(2, 1)
	if !complete {
		t.Fatalf("expected a complete snapshot up to the activation, got complete=%v", complete)
	}
	if len(got) == 0 || got[len(got)-1].Sequence != 2 {
		t.Fatalf("snapshot must stop at the activation, excluding the post-gap frame: %#v", got)
	}
}

// TestSnapshotFromRejectsAnInteriorGap covers the "never bridge a gap"
// loop specifically (as opposed to TestSnapshotDoesNotBridgeGap, which
// hits the earlier begin<0 short-history check): begin is non-negative
// here, but a gap sits strictly between begin and the activation.
func TestSnapshotFromRejectsAnInteriorGap(t *testing.T) {
	r := New(5)
	for _, sequence := range []uint16{1, 2, 4, 5} {
		r.Push(sequence, []byte{byte(sequence)})
	}
	if got, complete := r.SnapshotFrom(5, 2); complete || got != nil {
		t.Fatalf("interior gap accepted: %#v", got)
	}
}

func TestSnapshotEndingAtRejectsNonPositiveFrameCount(t *testing.T) {
	r := New(5)
	r.Push(1, []byte{1})
	if got, complete := r.SnapshotEndingAt(1, 0); complete || got != nil {
		t.Fatalf("zero frameCount accepted: %#v", got)
	}
	if got, complete := r.SnapshotEndingAt(1, -1); complete || got != nil {
		t.Fatalf("negative frameCount accepted: %#v", got)
	}
}

func TestSnapshotDoesNotBridgeGap(t *testing.T) {
	r := New(5)
	r.Push(1, []byte{1})
	r.Push(3, []byte{3})
	r.Push(4, []byte{4})
	got, complete := r.SnapshotFrom(3, 2)
	if complete || got != nil {
		t.Fatalf("incomplete snapshot accepted: %#v", got)
	}
}

func TestSnapshotRequiresFullPreroll(t *testing.T) {
	r := New(5)
	r.Push(10, []byte{1})
	if got, complete := r.SnapshotFrom(10, 2); complete || got != nil {
		t.Fatalf("short preroll accepted: %#v", got)
	}
}

func TestSnapshotEndingAtExcludesNewerAndReturnsGapSuffix(t *testing.T) {
	r := New(8)
	for _, sequence := range []uint16{1, 2, 4, 5, 6, 7} {
		r.Push(sequence, []byte{byte(sequence)})
	}
	got, complete := r.SnapshotEndingAt(6, 5)
	if complete || len(got) != 3 || got[0].Sequence != 4 || got[2].Sequence != 6 {
		t.Fatalf("gap snapshot = %#v complete=%v", got, complete)
	}
}

func TestSnapshotEndingAtWrapsAndReturnsCopies(t *testing.T) {
	r := New(4)
	for _, sequence := range []uint16{65534, 65535, 0, 1} {
		r.Push(sequence, []byte{byte(sequence)})
	}
	got, complete := r.SnapshotEndingAt(0, 3)
	if !complete || len(got) != 3 || got[0].Sequence != 65534 || got[2].Sequence != 0 {
		t.Fatalf("wrapped snapshot = %#v complete=%v", got, complete)
	}
	got[0].PCM[0] = 9
	again, _ := r.SnapshotEndingAt(0, 3)
	if again[0].PCM[0] == 9 {
		t.Fatal("capture snapshot aliases ring storage")
	}
}
