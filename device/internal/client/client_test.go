package client

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/discovery"
	"github.com/wilbowes/EchoMuse/pkg/buttons"
	"github.com/wilbowes/EchoMuse/pkg/led"
)

func TestLoadLinkCredsWithoutFilesIsPlainAndUnauthenticated(t *testing.T) {
	oldCA, oldToken := credCAPath, credTokenPath
	t.Cleanup(func() { credCAPath, credTokenPath = oldCA, oldToken })
	credCAPath = t.TempDir() + "/ca.pem"
	credTokenPath = t.TempDir() + "/token"

	creds := loadLinkCreds()
	if creds.tlsConf != nil || creds.token != "" {
		t.Fatalf("missing credentials loaded as %#v", creds)
	}
	if got := creds.header(); len(got) != 0 {
		t.Fatalf("plain credentials produced headers: %#v", got)
	}
	if creds.dialer().TLSClientConfig != nil {
		t.Fatal("plain credentials produced a TLS config")
	}
}

func TestCapabilitiesAnnounceNativeSendspin(t *testing.T) {
	for _, capability := range capabilities() {
		if capability == "sendspin_native" {
			return
		}
	}
	t.Fatal("sendspin_native capability is not announced")
}

func TestLoadLinkCredsLoadsPinnedCAAndTrimmedToken(t *testing.T) {
	oldCA, oldToken := credCAPath, credTokenPath
	t.Cleanup(func() { credCAPath, credTokenPath = oldCA, oldToken })
	dir := t.TempDir()
	credCAPath = dir + "/ca.pem"
	credTokenPath = dir + "/token"

	certPEM := testCertificatePEM(t)
	if err := writeFile(credCAPath, certPEM); err != nil {
		t.Fatal(err)
	}
	if err := writeFile(credTokenPath, []byte(" secret-token \n")); err != nil {
		t.Fatal(err)
	}

	creds := loadLinkCreds()
	if creds.tlsConf == nil {
		t.Fatal("valid CA did not produce TLS config")
	}
	if creds.tlsConf.ServerName != tlsServerName || creds.tlsConf.MinVersion != tls.VersionTLS12 {
		t.Fatalf("TLS config = %#v", creds.tlsConf)
	}
	if creds.token != "secret-token" {
		t.Fatalf("token = %q, want trimmed token", creds.token)
	}
	header := creds.header()
	if got := header.Get("X-EM-Token"); got != "secret-token" {
		t.Fatalf("token header = %q", got)
	}
	if creds.dialer().HandshakeTimeout != 10*time.Second {
		t.Fatal("unexpected websocket handshake timeout")
	}
}

func TestLoadLinkCredsIgnoresInvalidCAButLoadsToken(t *testing.T) {
	oldCA, oldToken := credCAPath, credTokenPath
	t.Cleanup(func() { credCAPath, credTokenPath = oldCA, oldToken })
	dir := t.TempDir()
	credCAPath = dir + "/ca.pem"
	credTokenPath = dir + "/token"
	if err := writeFile(credCAPath, []byte("not a certificate")); err != nil {
		t.Fatal(err)
	}
	if err := writeFile(credTokenPath, []byte("token")); err != nil {
		t.Fatal(err)
	}

	creds := loadLinkCreds()
	if creds.tlsConf != nil || creds.token != "token" {
		t.Fatalf("invalid CA handling = %#v", creds)
	}
}

func TestTLSNowRespectsFutureFirmwareBuildTime(t *testing.T) {
	old := BuildUnix
	t.Cleanup(func() { BuildUnix = old })
	future := time.Now().Add(time.Hour).Unix()
	BuildUnix = "not-a-number"
	now := tlsNow()
	if now.Before(time.Now().Add(-time.Second)) {
		t.Fatalf("invalid build time moved clock backwards: %v", now)
	}
	BuildUnix = stringInt64(future)
	if got := tlsNow(); got.Before(time.Unix(future, 0)) {
		t.Fatalf("tlsNow() = %v, below future build time", got)
	}
}

func TestTLSNowIgnoresPastBuildTime(t *testing.T) {
	old := BuildUnix
	t.Cleanup(func() { BuildUnix = old })
	BuildUnix = stringInt64(time.Now().Add(-time.Hour).Unix())
	if got := tlsNow(); got.Before(time.Now().Add(-2 * time.Second)) {
		t.Fatalf("tlsNow() = %v, unexpectedly old", got)
	}
}

func TestOutboundReportsDropSafelyWhenDisconnected(t *testing.T) {
	c := &ControlClient{}
	c.SendButton(buttons.ButtonClickEvent{ClickType: buttons.DotClick, Down: true})
	c.SendMuteState(true)
	c.SendAmbientLight(12)
	c.SendVolumeState(80)
	c.SendLog("info", "hello")
	c.SendWifiResult(false, "Home", "failed")
	c.SendPlaybackStats(10, 2, map[string]int{"min_depth": 3})
	c.SendOwwShadowCross(0.7, 20)
	c.SendOwwWake(0.8, 0.5, 15, 42)
	c.SendBleAdverts([]string{"advert"})
	c.SendWifiScanResult(nil, "scan failed")
}

func TestConnectDispatchesControlMessagesAndAppliesConfig(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	// The handler sends a complete batch after the registration handshake. The
	// client owns the dispatch switch; this exercises it through the wire rather
	// than calling private branches directly.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Errorf("upgrade: %v", err)
			return
		}
		defer conn.Close()
		var register map[string]interface{}
		if err := conn.ReadJSON(&register); err != nil {
			t.Errorf("register: %v", err)
			return
		}
		if register["type"] != "register" {
			t.Errorf("register = %#v", register)
		}
		if err := conn.WriteJSON(map[string]string{"type": "ack"}); err != nil {
			return
		}
		messages := []interface{}{
			map[string]interface{}{"type": "leds", "leds": []led.Led{{ID: 0, G: 10}}, "listening": true},
			map[string]interface{}{"type": "led_anim", "anim": map[string]interface{}{"pattern": "spin"}},
			map[string]interface{}{"type": "mic_start", "lock_mic": true},
			map[string]interface{}{"type": "mic_stop"},
			map[string]interface{}{"type": "volume_set", "level": 77}, map[string]interface{}{"type": "mute_toggle"},
			map[string]interface{}{"type": "config", "vadThreshold": 0.123, "owwThreshold": 0.321},
			map[string]interface{}{"type": "wifi_change", "ssid": "Home", "psk": "password"},
			map[string]interface{}{"type": "wifi_commit"}, map[string]interface{}{"type": "wifi_scan"},
			map[string]interface{}{"type": "speaker_flush"}, map[string]interface{}{"type": "music_flush"},
			map[string]interface{}{"type": "duck", "on": true}, map[string]interface{}{"type": "test_audio"},
			map[string]interface{}{"type": "test_audio_cleanup"}, map[string]interface{}{"type": "unknown"},
		}
		for _, msg := range messages {
			_ = conn.WriteJSON(msg)
		}
		_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, "done"), time.Now().Add(time.Second))
	}))
	defer server.Close()

	type events struct{ leds, anim, start, stop, volume, mute, wifi, commit, scan, speaker, music, test, cleanup, config int }
	var e events
	configSeen := make(chan config.ConfigMessage, 1)
	c := NewControlClient("test-device", func([]led.Led, *bool) { e.leds++ }, func(bool) { e.start++ }, func() { e.stop++ })
	c.OnLEDAnim(func(json.RawMessage) { e.anim++ })
	c.OnVolumeSet(func(int) { e.volume++ })
	c.OnMuteToggle(func() { e.mute++ })
	c.OnWifiChange(func(string, string) { e.wifi++ })
	c.OnWifiCommit(func() { e.commit++ })
	c.OnWifiScan(func() { e.scan++ })
	c.OnSpeakerFlush(func() { e.speaker++ })
	c.OnMusicFlush(func() { e.music++ })
	c.OnDuck(func(bool) { e.music++ })
	c.OnTestAudio(func() { e.test++ })
	c.OnTestAudioCleanup(func() { e.cleanup++ })
	c.OnConfigApplied(func(m config.ConfigMessage) { configSeen <- m })
	addr := strings.TrimPrefix(server.URL, "http://")
	err := c.connect(context.Background(), &discovery.ServerInfo{Addr: addr, Host: strings.Split(addr, ":")[0]}, NewDataClient("test", nil, nil))
	if err == nil {
		t.Fatal("connect unexpectedly succeeded after server close")
	}
	select {
	case <-configSeen:
	case <-time.After(time.Second):
		t.Fatal("config callback did not run")
	}
	time.Sleep(20 * time.Millisecond) // test_audio is intentionally dispatched asynchronously.
	if e != (events{leds: 1, anim: 1, start: 1, stop: 1, volume: 1, mute: 1, wifi: 1, commit: 1, scan: 1, speaker: 1, music: 2, test: 1, cleanup: 1}) {
		t.Fatalf("dispatch counts = %+v", e)
	}
	if got := config.Get().VadThreshold; got != 0.123 {
		t.Fatalf("config VadThreshold = %v", got)
	}
}

func testCertificatePEM(t *testing.T) []byte {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test"},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.Add(time.Hour),
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func writeFile(path string, data []byte) error {
	return os.WriteFile(path, data, 0o600)
}

func stringInt64(value int64) string {
	return strconv.FormatInt(value, 10)
}
