package server

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDeviceStateRoundtrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "etc", "state.json")

	// Missing file → not ok.
	if _, ok := loadDeviceState(path); ok {
		t.Fatal("expected ok=false for missing file")
	}

	// Save creates parent dirs and persists the muted flag.
	saveDeviceState(path, deviceState{Muted: true})
	st, ok := loadDeviceState(path)
	if !ok || !st.Muted {
		t.Fatalf("roundtrip failed: ok=%v st=%+v", ok, st)
	}

	saveDeviceState(path, deviceState{Muted: false})
	st, ok = loadDeviceState(path)
	if !ok || st.Muted {
		t.Fatalf("overwrite failed: ok=%v st=%+v", ok, st)
	}

	// Corrupt file → not ok, no panic.
	if err := os.WriteFile(path, []byte("{truncated"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, ok := loadDeviceState(path); ok {
		t.Fatal("expected ok=false for corrupt file")
	}
}

// TestSaveDeviceStateSurvivesMkdirFailure exercises the "parent cannot be
// created" branch: a regular file sitting where a directory component is
// needed. saveDeviceState must log and return, not panic.
func TestSaveDeviceStateSurvivesMkdirFailure(t *testing.T) {
	dir := t.TempDir()
	blocker := filepath.Join(dir, "blocker")
	if err := os.WriteFile(blocker, []byte("not a directory"), 0o644); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(blocker, "etc", "state.json")
	saveDeviceState(path, deviceState{Muted: true}) // must not panic
	if _, ok := loadDeviceState(path); ok {
		t.Fatal("state was somehow persisted despite the blocked mkdir")
	}
}

// TestSaveDeviceStateSurvivesWriteFailure exercises the tmp-file write
// error branch: the ".tmp" path is itself a pre-existing directory, so
// os.WriteFile cannot open it.
func TestSaveDeviceStateSurvivesWriteFailure(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	if err := os.Mkdir(path+".tmp", 0o755); err != nil {
		t.Fatal(err)
	}
	saveDeviceState(path, deviceState{Muted: true}) // must not panic
	if _, ok := loadDeviceState(path); ok {
		t.Fatal("state was somehow persisted despite the blocked write")
	}
}

// TestSaveDeviceStateSurvivesRenameFailure exercises the final rename
// error branch: the destination path is itself a pre-existing directory,
// so renaming the completed tmp file onto it fails.
func TestSaveDeviceStateSurvivesRenameFailure(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	if err := os.Mkdir(path, 0o755); err != nil {
		t.Fatal(err)
	}
	saveDeviceState(path, deviceState{Muted: true}) // must not panic
	if _, err := os.Stat(path + ".tmp"); err != nil {
		t.Fatal("expected the tmp file to have been written before the failed rename")
	}
}
