package wifi

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestValidateCredentials(t *testing.T) {
	cases := []struct {
		name string
		ssid string
		psk  string
		ok   bool
	}{
		{"open network", "Cafe", "", true},
		{"valid WPA", "Cafe", "password", true},
		{"empty SSID", "", "password", false},
		{"quote in SSID", `Cafe"`, "password", false},
		{"backslash in PSK", "Cafe", `pass\word`, false},
		{"short PSK", "Cafe", "short", false},
		{"long PSK", "Cafe", strings.Repeat("x", 64), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := validate(tc.ssid, tc.psk) == nil; got != tc.ok {
				t.Fatalf("validate(%q, %q) success = %v, want %v", tc.ssid, tc.psk, got, tc.ok)
			}
		})
	}
}

func TestComposeConfOpenNetwork(t *testing.T) {
	conf := composeConf("Guest WiFi", "")

	for _, want := range []string{
		"ctrl_interface=" + wpaSockDir,
		"update_config=1",
		"network={",
		"\tssid=\"Guest WiFi\"",
		"\tkey_mgmt=NONE",
		"\tpriority=1",
	} {
		if !strings.Contains(conf, want) {
			t.Errorf("composeConf missing %q in:\n%s", want, conf)
		}
	}
	if strings.Contains(conf, "\tpsk=") {
		t.Error("open network unexpectedly contains a PSK")
	}
}

func TestComposeConfWPA2Network(t *testing.T) {
	conf := composeConf("Home", "correct horse battery staple")

	if !strings.Contains(conf, "\tpsk=\"correct horse battery staple\"") {
		t.Fatalf("composeConf omitted PSK:\n%s", conf)
	}
	if !strings.Contains(conf, "\tkey_mgmt=WPA-PSK") {
		t.Fatalf("composeConf omitted WPA key management:\n%s", conf)
	}
	if strings.Contains(conf, "key_mgmt=NONE") {
		t.Error("WPA network unexpectedly uses key_mgmt=NONE")
	}
}

func TestComposeConfUsesAndroidPropertiesWhenAvailable(t *testing.T) {
	dir := t.TempDir()
	getprop := filepath.Join(dir, "getprop")
	if err := os.WriteFile(getprop, []byte("#!/bin/sh\ncase \"$1\" in\nro.product.name) echo Dot;;\nro.product.manufacturer) echo Amazon;;\nro.product.model) echo Model2;;\nro.serialno) echo Serial2;;\n*) echo fallback;;\nesac\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Cleanup(func() { _ = os.Setenv("PATH", oldPath) })
	if err := os.Setenv("PATH", dir); err != nil {
		t.Fatal(err)
	}
	conf := composeConf("Home", "password")
	for _, want := range []string{"device_name=Dot", "manufacturer=Amazon", "model_name=Model2", "serial_number=Serial2"} {
		if !strings.Contains(conf, want) {
			t.Errorf("composeConf missing %q:\n%s", want, conf)
		}
	}
}

func TestScanDeduplicatesAndSortsResults(t *testing.T) {
	oldCli, oldSleep := wpaCli, sleep
	t.Cleanup(func() { wpaCli, sleep = oldCli, oldSleep })

	var calls []string
	wpaCli = func(args ...string) (string, error) {
		calls = append(calls, strings.Join(args, " "))
		if len(calls) == 1 {
			return "", nil
		}
		return strings.Join([]string{
			"bssid\tfreq\tsignal\tflags\tssid",
			"aa\t2412\t-45\t[WPA2]\tHome",
			"bb\t5180\t-60\t[WPA2]\tHome",
			"cc\t2412\t-70\t[WPA2]\tCafe",
			"dd\t2412\t-30\t[WPA2]\t\\x00\\x00",
			"bad\t2412\tnope\t[]\tIgnored",
			"short\trow",
			"empty\t2412\t-20\t[]\t",
		}, "\n"), nil
	}
	sleep = func(time.Duration) {}

	nets, err := Scan()
	if err != nil {
		t.Fatalf("Scan() error: %v", err)
	}
	if got, want := calls, []string{"scan", "scan_results"}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("wpaCli calls = %v, want %v", got, want)
	}
	if len(nets) != 2 {
		t.Fatalf("got %d networks, want 2: %#v", len(nets), nets)
	}
	if nets[0] != (Network{SSID: "Home", Signal: -45}) || nets[1] != (Network{SSID: "Cafe", Signal: -70}) {
		t.Fatalf("networks = %#v, want strongest-first deduplicated results", nets)
	}
}

func TestScanReportsTriggerAndResultErrors(t *testing.T) {
	oldCli, oldSleep := wpaCli, sleep
	t.Cleanup(func() { wpaCli, sleep = oldCli, oldSleep })

	wpaCli = func(args ...string) (string, error) {
		return "failed", &testError{message: strings.Join(args, " ")}
	}
	if _, err := Scan(); err == nil || !strings.Contains(err.Error(), "scan trigger") {
		t.Fatalf("Scan trigger error = %v, want scan trigger context", err)
	}

	wpaCli = func(args ...string) (string, error) {
		if args[0] == "scan" {
			return "", nil
		}
		return "failed", &testError{message: "results"}
	}
	sleep = func(time.Duration) {}
	if _, err := Scan(); err == nil || !strings.Contains(err.Error(), "scan_results") {
		t.Fatalf("Scan result error = %v, want scan_results context", err)
	}
}

type testError struct{ message string }

func (e *testError) Error() string { return e.message }

func TestCurrentSSID(t *testing.T) {
	oldCli := wpaCli
	t.Cleanup(func() { wpaCli = oldCli })

	wpaCli = func(args ...string) (string, error) {
		return "wpa_state=COMPLETED\nssid=Home\n", nil
	}
	if got := CurrentSSID(); got != "Home" {
		t.Fatalf("CurrentSSID() = %q, want Home", got)
	}

	wpaCli = func(args ...string) (string, error) {
		return "wpa_state=SCANNING\nssid=Old\n", nil
	}
	if got := CurrentSSID(); got != "" {
		t.Fatalf("CurrentSSID(unassociated) = %q, want empty", got)
	}
	wpaCli = func(args ...string) (string, error) { return "wpa_state=COMPLETED\nfreq=2412\n", nil }
	if got := CurrentSSID(); got != "" {
		t.Fatalf("CurrentSSID(without ssid) = %q", got)
	}
}

func TestCurrentSSIDTrimsStatusLines(t *testing.T) {
	oldCli := wpaCli
	t.Cleanup(func() { wpaCli = oldCli })
	wpaCli = func(args ...string) (string, error) {
		return " wpa_state=COMPLETED\n  ssid=Trimmed\n", nil
	}
	if got := CurrentSSID(); got != "Trimmed" {
		t.Fatalf("CurrentSSID() = %q, want Trimmed", got)
	}
}

func TestWaitForAssociationRequiresTheRequestedSSID(t *testing.T) {
	oldCli, oldSleep := wpaCli, sleep
	t.Cleanup(func() { wpaCli, sleep = oldCli, oldSleep })
	wpaCli = func(args ...string) (string, error) {
		return "wpa_state=COMPLETED\nssid=Other\n", nil
	}
	sleep = func(time.Duration) {}
	if waitForAssociation("Target", 0) { // already expired: must not accept another network
		t.Fatal("accepted association to the wrong SSID")
	}
}

func TestAssociationHelpersRecognizeTheCurrentNetwork(t *testing.T) {
	oldCli := wpaCli
	t.Cleanup(func() { wpaCli = oldCli })
	wpaCli = func(args ...string) (string, error) {
		return "wpa_state=COMPLETED\nssid=Target\n", nil
	}
	if !associated() || !associatedTo("Target") {
		t.Fatal("association helpers rejected the completed target network")
	}
	if !waitForAssociation("Target", time.Second) {
		t.Fatal("waitForAssociation did not accept an immediate target association")
	}
}

func TestWaitFor(t *testing.T) {
	if !waitFor("immediate", time.Second, func() bool { return true }) {
		t.Fatal("waitFor returned false for an immediately true condition")
	}
	if waitFor("expired", 0, func() bool { return false }) {
		t.Fatal("waitFor returned true for an already expired deadline")
	}
}

func TestPendingResultIsCopiedAndCommitClearsIt(t *testing.T) {
	oldPending := pending
	t.Cleanup(func() { pending = oldPending })

	setResult(Result{OK: true, SSID: "Home"})
	one := PendingResult()
	if one == nil || one.SSID != "Home" {
		t.Fatalf("PendingResult() = %#v, want Home result", one)
	}
	one.SSID = "mutated"
	two := PendingResult()
	if two == nil || two.SSID != "Home" {
		t.Fatalf("PendingResult returned aliased result: %#v", two)
	}
	Commit()
	if PendingResult() != nil {
		t.Fatal("Commit did not clear pending result")
	}
}

func TestRecoverIfPendingWithoutMarkerIsNoop(t *testing.T) {
	oldPending := pending
	t.Cleanup(func() { pending = oldPending })
	pending = nil
	RecoverIfPending()
	if PendingResult() != nil {
		t.Fatal("RecoverIfPending created a result without a marker")
	}
}

func TestChangeRejectsInvalidCredentialsWithoutTouchingWiFi(t *testing.T) {
	oldPending, oldInFlight := pending, inFlight
	t.Cleanup(func() { pending, inFlight = oldPending, oldInFlight })
	pending = nil
	inFlight = false
	Change("bad\"ssid", "password", func() bool { t.Fatal("connected callback called"); return false })
	result := PendingResult()
	if result == nil || result.OK || result.SSID != "bad\"ssid" || !strings.Contains(result.Error, "double-quote") {
		t.Fatalf("invalid change result = %#v", result)
	}
}

func TestChangeReportsConcurrentRequest(t *testing.T) {
	oldPending, oldInFlight := pending, inFlight
	t.Cleanup(func() { pending, inFlight = oldPending, oldInFlight })
	pending = nil
	inFlight = true
	Change("Home", "password", nil)
	result := PendingResult()
	if result == nil || result.OK || result.SSID != "Home" || !strings.Contains(result.Error, "already in progress") {
		t.Fatalf("concurrent change result = %#v", result)
	}
	inFlight = false
}

func TestChangeReportsMissingCurrentConfig(t *testing.T) {
	oldPending, oldInFlight := pending, inFlight
	t.Cleanup(func() { pending, inFlight = oldPending, oldInFlight })
	pending = nil
	inFlight = false
	Change("Home", "password", nil)
	result := PendingResult()
	if result == nil || result.OK || result.SSID != "Home" || !strings.Contains(result.Error, "cannot read current config") {
		t.Fatalf("missing config result = %#v", result)
	}
}
