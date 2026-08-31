package wifi

import (
	"errors"
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

// ─── Change() / RecoverIfPending() against a fake filesystem and reload ────
//
// confPath/backupPath/markerPath and reload() are vars specifically so this
// safety-critical file-handling logic (back up → mark pending → apply →
// verify → revert-on-failure) can run against a throwaway directory and a
// controllable outcome instead of the real Android paths and svc/wpa_cli,
// neither of which exist on a host. See the doc comments on those vars.

// withTempWifiPaths points confPath/backupPath/markerPath at a throwaway
// directory and restores the real paths on cleanup.
func withTempWifiPaths(t *testing.T) (conf, backup, marker string) {
	t.Helper()
	dir := t.TempDir()
	conf = filepath.Join(dir, "wpa_supplicant.conf")
	backup = filepath.Join(dir, "wpa_supplicant.conf.bak")
	marker = filepath.Join(dir, "pending")
	oldConf, oldBackup, oldMarker := confPath, backupPath, markerPath
	confPath, backupPath, markerPath = conf, backup, marker
	t.Cleanup(func() { confPath, backupPath, markerPath = oldConf, oldBackup, oldMarker })
	return conf, backup, marker
}

// withFastWifiWaits shrinks every timeout Change()/RecoverIfPending() poll
// against and makes the retry sleep a no-op. Off real hardware svc/wpa_cli
// do not exist, so any condition gated on them can never become true —
// without this, every test below would burn the real 45-90s production
// timeouts finding that out.
func withFastWifiWaits(t *testing.T) {
	t.Helper()
	oldAssociate, oldIP, oldReconnect, oldSleep := associateTimeout, ipTimeout, reconnectTimeout, sleep
	associateTimeout, ipTimeout, reconnectTimeout = time.Millisecond, time.Millisecond, time.Millisecond
	sleep = func(time.Duration) {}
	t.Cleanup(func() {
		associateTimeout, ipTimeout, reconnectTimeout, sleep = oldAssociate, oldIP, oldReconnect, oldSleep
	})
}

func withFakeReload(t *testing.T, fn func(content string) error) {
	t.Helper()
	old := reload
	reload = fn
	t.Cleanup(func() { reload = old })
}

func resetChangeState(t *testing.T) {
	t.Helper()
	oldPending, oldInFlight := pending, inFlight
	pending, inFlight = nil, false
	t.Cleanup(func() { pending, inFlight = oldPending, oldInFlight })
}

// The worst case the safety design has to answer for: a change that cannot
// be applied AND cannot be undone. The marker must survive so
// RecoverIfPending retries on the next start — losing it here would strand
// the device on a half-applied conf with no way back.
func TestChangeLeavesMarkerWhenRevertAlsoFails(t *testing.T) {
	resetChangeState(t)
	withFastWifiWaits(t)
	conf, backup, marker := withTempWifiPaths(t)
	if err := os.WriteFile(conf, []byte("old-conf"), 0o600); err != nil {
		t.Fatal(err)
	}
	withFakeReload(t, func(string) error {
		return errors.New("svc wifi disable: no such file or directory")
	})

	Change("Home", "password", nil)

	result := PendingResult()
	if result == nil || result.OK || !strings.Contains(result.Error, "svc wifi disable") {
		t.Fatalf("result = %#v", result)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("marker removed after a failed revert — recovery on restart is now impossible: %v", err)
	}
	if _, err := os.Stat(backup); err != nil {
		t.Fatalf("backup removed after a failed revert: %v", err)
	}
}

// The common case: the new conf cannot be applied, but reverting to the old
// one works. Marker and backup must both be cleaned up — a leftover marker
// here would make a future RecoverIfPending "restore" a network the device
// never actually left.
func TestChangeCleansUpAfterASuccessfulRevert(t *testing.T) {
	resetChangeState(t)
	withFastWifiWaits(t)
	conf, backup, marker := withTempWifiPaths(t)
	if err := os.WriteFile(conf, []byte("old-conf"), 0o600); err != nil {
		t.Fatal(err)
	}
	calls := 0
	withFakeReload(t, func(string) error {
		calls++
		if calls == 1 {
			return errors.New("did not associate")
		}
		return nil // the revert's own reload succeeds
	})

	Change("Home", "password", nil)

	result := PendingResult()
	if result == nil || result.OK || !strings.Contains(result.Error, "did not associate") {
		t.Fatalf("result = %#v", result)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("marker still present after a successful revert")
	}
	if _, err := os.Stat(backup); !os.IsNotExist(err) {
		t.Fatalf("backup still present after a successful revert")
	}
}

// reload() succeeding is not the same as the change succeeding: the device
// must actually observe association to the NEW ssid, or a supplicant that
// silently kept the old network would read as success. This drives that
// gate through wpaCli — the same seam CurrentSSID uses — so it needs no
// real hardware.
func TestChangeRevertsWhenAssociationNeverHappens(t *testing.T) {
	resetChangeState(t)
	withFastWifiWaits(t)
	conf, _, _ := withTempWifiPaths(t)
	if err := os.WriteFile(conf, []byte("old-conf"), 0o600); err != nil {
		t.Fatal(err)
	}
	withFakeReload(t, func(string) error { return nil }) // both directions "succeed"
	oldWpaCli := wpaCli
	wpaCli = func(...string) (string, error) { return "wpa_state=SCANNING", nil } // never COMPLETED
	t.Cleanup(func() { wpaCli = oldWpaCli })

	Change("Home", "password", nil)

	result := PendingResult()
	if result == nil || result.OK || !strings.Contains(result.Error, "did not associate") {
		t.Fatalf("result = %#v, want a did-not-associate failure", result)
	}
}

// A marker with no backup means a previous run reverted its conf but could
// not remove the marker, or the backup was lost — there is nothing to
// restore, so this must clear the marker and stop, not report a result.
func TestRecoverIfPendingWithMarkerButNoBackupClearsMarkerOnly(t *testing.T) {
	resetChangeState(t)
	_, _, marker := withTempWifiPaths(t)
	if err := os.WriteFile(marker, []byte(`{"newSsid":"Home"}`), 0o600); err != nil {
		t.Fatal(err)
	}

	RecoverIfPending()

	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("marker not cleared when there was no backup to restore from")
	}
	if PendingResult() != nil {
		t.Fatalf("a marker-without-backup recovery must not report a result — there was nothing to restore")
	}
}

// A restore that fails at startup must leave the marker in place so the
// NEXT start retries — clearing it here would silently give up on ever
// getting back to the old network.
func TestRecoverIfPendingLeavesMarkerWhenRestoreFails(t *testing.T) {
	resetChangeState(t)
	_, backup, marker := withTempWifiPaths(t)
	if err := os.WriteFile(marker, []byte(`{"newSsid":"Home"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(backup, []byte("old-conf"), 0o600); err != nil {
		t.Fatal(err)
	}
	withFakeReload(t, func(string) error {
		return errors.New("svc wifi disable: no such file or directory")
	})

	RecoverIfPending()

	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("marker removed despite the restore failing — next start will not retry: %v", err)
	}
	if PendingResult() != nil {
		t.Fatalf("a failed startup restore must not report a result yet — RecoverIfPending will retry")
	}
}

// The success case this whole mechanism exists for: a crash or power cycle
// left a pending marker, and the previous network is restored on the next
// start with no operator action, then reported once a connection exists to
// carry it.
func TestRecoverIfPendingRestoresAndReportsOnSuccess(t *testing.T) {
	resetChangeState(t)
	_, backup, marker := withTempWifiPaths(t)
	if err := os.WriteFile(marker, []byte(`{"newSsid":"Guest"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(backup, []byte("old-conf"), 0o600); err != nil {
		t.Fatal(err)
	}
	withFakeReload(t, func(string) error { return nil })

	RecoverIfPending()

	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("marker still present after a successful restore")
	}
	if _, err := os.Stat(backup); !os.IsNotExist(err) {
		t.Fatalf("backup still present after a successful restore")
	}
	result := PendingResult()
	if result == nil || result.OK || result.SSID != "Guest" || !strings.Contains(result.Error, "restarted") {
		t.Fatalf("result = %#v", result)
	}
}
