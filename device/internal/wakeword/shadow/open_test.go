package shadow

import (
	"os"
	"path/filepath"
	"testing"
)

func withTempOwwDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	old := os.Getenv("EM_OWW_DIR")
	hadOld := os.Getenv("EM_OWW_DIR") != ""
	if err := os.Setenv("EM_OWW_DIR", dir); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if hadOld {
			_ = os.Setenv("EM_OWW_DIR", old)
		} else {
			_ = os.Unsetenv("EM_OWW_DIR")
		}
	})
	return dir
}

func TestDirUsesEnvOverrideOrDefault(t *testing.T) {
	old, had := os.LookupEnv("EM_OWW_DIR")
	t.Cleanup(func() {
		if had {
			_ = os.Setenv("EM_OWW_DIR", old)
		} else {
			_ = os.Unsetenv("EM_OWW_DIR")
		}
	})

	_ = os.Unsetenv("EM_OWW_DIR")
	if got := Dir(); got != DefaultDir {
		t.Fatalf("Dir() with no override = %q, want %q", got, DefaultDir)
	}
	_ = os.Setenv("EM_OWW_DIR", "/tmp/custom-oww")
	if got := Dir(); got != "/tmp/custom-oww" {
		t.Fatalf("Dir() with override = %q, want /tmp/custom-oww", got)
	}
}

func TestClassifierMD5ReadsInstalledFileOrEmptyWhenMissing(t *testing.T) {
	dir := withTempOwwDir(t)
	if got := ClassifierMD5("hey_test_v0.1"); got != "" {
		t.Fatalf("ClassifierMD5 for a missing file = %q, want empty", got)
	}
	content := []byte("not really onnx, just bytes to hash")
	if err := os.WriteFile(filepath.Join(dir, "hey_test_v0.1.onnx"), content, 0o644); err != nil {
		t.Fatal(err)
	}
	got := ClassifierMD5("hey_test_v0.1")
	if got == "" || len(got) != 32 {
		t.Fatalf("ClassifierMD5 for an installed file = %q, want a 32-char md5 hex digest", got)
	}
}

// TestOpenRejectsEmptyModel and TestOpenReportsMissingModelFiles exercise
// Open/OpenWithHead's validation that runs BEFORE the ONNX Runtime dlopen —
// the one part of this file reachable on a host with no runtime installed
// (see CLAUDE.md on internal/wakeword/ort/: NewInferer/Open genuinely need
// a real .so and stay untested here).
func TestOpenRejectsEmptyModel(t *testing.T) {
	withTempOwwDir(t)
	if _, err := Open("", 0.5, nil); err == nil {
		t.Fatal("Open(\"\") should reject an unconfigured model before touching the runtime")
	}
}

func TestOpenReportsMissingModelFiles(t *testing.T) {
	withTempOwwDir(t) // empty dir — none of the three required files exist
	_, err := Open("hey_test_v0.1", 0.5, nil)
	if err == nil {
		t.Fatal("Open should fail when the model files are not installed")
	}
}

func TestOpenWithHeadRejectsEmptyModels(t *testing.T) {
	withTempOwwDir(t)
	if _, err := OpenWithHead("", 0.5, nil, "stop", 0.5, nil, nil, nil); err == nil {
		t.Fatal("OpenWithHead should reject an unconfigured wake model")
	}
	if _, err := OpenWithHead("wake", 0.5, nil, "", 0.5, nil, nil, nil); err == nil {
		t.Fatal("OpenWithHead should reject an unconfigured stop model")
	}
}

func TestOpenWithHeadReportsMissingModelFiles(t *testing.T) {
	withTempOwwDir(t)
	_, err := OpenWithHead("hey_test_v0.1", 0.5, nil, "stop_v1", 0.5, nil, nil, nil)
	if err == nil {
		t.Fatal("OpenWithHead should fail when the model files are not installed")
	}
}

// TestModelStemMirrorsPython pins agreement with
// controller/em_oww_models.py::prediction_key. The device looks for
// <stem>.onnx, so a stem that disagrees with the controller's means shadow mode
// silently never starts — which is exactly what happened on the first
// deployment, because filepath.Ext treats the ".1" of "hey_mycroft_v0.1" as a
// file extension.
func TestModelStemMirrorsPython(t *testing.T) {
	cases := []struct{ in, want string }{
		// Built-in names are passed through untouched. A version suffix is not
		// an extension.
		{"hey_mycroft_v0.1", "hey_mycroft_v0.1"},
		{"hey_jarvis_v0.1", "hey_jarvis_v0.1"},
		{"alexa_v0.1", "alexa_v0.1"},
		{"hey_clara", "hey_clara"},
		// A custom model is stored as a path and keys as its filename stem.
		{"/app/data/oww_models/hey_clara.onnx", "hey_clara"},
		{"/app/data/oww_models/hey_clarra_v2.1.onnx", "hey_clarra_v2.1"},
		{"hey_clara.onnx", "hey_clara"},
		// Whitespace from a hand-edited config should not produce a path with a
		// space in it.
		{"  hey_mycroft_v0.1  ", "hey_mycroft_v0.1"},
		{"", ""},
	}
	for _, c := range cases {
		if got := ModelStem(c.in); got != c.want {
			t.Errorf("ModelStem(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
