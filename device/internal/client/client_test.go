package client

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/buttons"
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
