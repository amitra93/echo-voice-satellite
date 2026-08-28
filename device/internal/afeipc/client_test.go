package afeipc

import "testing"

func TestHelperCommandDefaultsToFirmwareBinary(t *testing.T) {
	cmd := helperCommand("")
	if got, want := cmd.Args, []string{"su", "system", "-c", "/data/local/bin/server --afe-helper"}; len(got) != len(want) {
		t.Fatalf("args = %#v, want %#v", got, want)
	} else {
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("args = %#v, want %#v", got, want)
			}
		}
	}
}

func TestHelperCommandHonorsOverride(t *testing.T) {
	cmd := helperCommand("/data/local/bin/test-helper --verbose")
	if got, want := cmd.Args[3], "/data/local/bin/test-helper --verbose"; got != want {
		t.Fatalf("command = %q, want %q", got, want)
	}
}
