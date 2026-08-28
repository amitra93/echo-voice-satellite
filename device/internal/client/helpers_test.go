package client

import (
	"bytes"
	"context"
	"encoding/binary"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/discovery"
	"github.com/wilbowes/EchoMuse/pkg/speaker"
)

type helperSpeaker struct {
	helperMusicSyncReceiver
	periods, music int
	eos, musicEOS  int
}

func (s *helperSpeaker) Init() error             { return nil }
func (s *helperSpeaker) PumpPeriod([]byte) error { s.periods++; return nil }
func (s *helperSpeaker) EndStream()              { s.eos++ }
func (s *helperSpeaker) Flush()                  {}
func (s *helperSpeaker) PumpMusic([]byte) error  { s.music++; return nil }
func (s *helperSpeaker) EndMusicStream()         { s.musicEOS++ }
func (s *helperSpeaker) FlushMusic()             {}
func (s *helperSpeaker) SetDuck(float64)         {}
func (s *helperSpeaker) Close()                  {}

func wavChunk(kind string, payload []byte) []byte {
	chunk := make([]byte, 8+len(payload))
	copy(chunk, kind)
	binary.LittleEndian.PutUint32(chunk[4:], uint32(len(payload)))
	copy(chunk[8:], payload)
	return chunk
}

func wavHeader() []byte {
	header := make([]byte, 12)
	copy(header[:4], "RIFF")
	copy(header[8:], "WAVE")
	return header
}

func validWAV() []byte {
	fmt := make([]byte, 16)
	binary.LittleEndian.PutUint16(fmt[0:], 1)
	binary.LittleEndian.PutUint16(fmt[2:], 1)
	binary.LittleEndian.PutUint32(fmt[4:], 16000)
	binary.LittleEndian.PutUint16(fmt[14:], 16)
	return append(append(wavHeader(), wavChunk("JUNK", []byte{1, 2})...), wavChunk("fmt ", fmt)...)
}

func TestReadTestWAVRejectsMalformedAndInvalidFormats(t *testing.T) {
	cases := []struct {
		name string
		data []byte
	}{
		{"short header", []byte("RIFF")},
		{"wrong header", []byte("RIFFxxxxNOPE!!!!")},
		{"missing data chunk", validWAV()},
		{"short fmt", append(wavHeader(), wavChunk("fmt ", make([]byte, 15))...)},
		{"oversized fmt", append(wavHeader(), wavChunk("fmt ", make([]byte, 4097))...)},
		{"bad format", append(append(wavHeader(), wavChunk("fmt ", make([]byte, 16))...), wavChunk("data", []byte{1, 2})...)},
		{"empty data", append(append(wavHeader(), validWAV()[20:]...), wavChunk("data", nil)...)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if pcm, err := readTestWAV(bytes.NewReader(tc.data)); err == nil || pcm != nil {
				t.Fatalf("readTestWAV() = %v, %v; want error", pcm, err)
			}
		})
	}
}

func TestReadTestWAVReadsDataAfterOddMetadataChunk(t *testing.T) {
	data := append(validWAV(), wavChunk("data", []byte{1, 2, 3, 4})...)
	// RIFF chunks are padded to an even boundary; include the pad after JUNK.
	data = append(wavHeader(), wavChunk("JUNK", []byte{9})...)
	data = append(data, 0)
	fmt := make([]byte, 16)
	binary.LittleEndian.PutUint16(fmt[0:], 1)
	binary.LittleEndian.PutUint16(fmt[2:], 1)
	binary.LittleEndian.PutUint32(fmt[4:], 16000)
	binary.LittleEndian.PutUint16(fmt[14:], 16)
	data = append(data, wavChunk("fmt ", fmt)...)
	data = append(data, wavChunk("data", []byte{1, 2, 3, 4})...)
	pcm, err := readTestWAV(bytes.NewReader(data))
	if err != nil || !bytes.Equal(pcm, []byte{1, 2, 3, 4}) {
		t.Fatalf("readTestWAV() = %v, %v; want PCM payload", pcm, err)
	}
}

type helperMusicSyncReceiver struct {
	starts, pcms, clears, ends int
}

func (r *helperMusicSyncReceiver) MusicSyncStart(uint32) bool { r.starts++; return true }
func (r *helperMusicSyncReceiver) MusicSyncPCM(uint32, uint32, int64, []byte) bool {
	r.pcms++
	return true
}
func (r *helperMusicSyncReceiver) MusicSyncClear(uint32) bool { r.clears++; return true }
func (r *helperMusicSyncReceiver) MusicSyncEnd(uint32) bool   { r.ends++; return true }

func TestDispatchMusicSyncFrameRoutesValidFramesAndRejectsMalformed(t *testing.T) {
	r := &helperMusicSyncReceiver{}
	frames := [][]byte{}
	for _, kind := range []byte{musicSyncStart, musicSyncClear, musicSyncEnd} {
		frame, err := encodeMusicSyncControl(kind, 7)
		if err != nil {
			t.Fatal(err)
		}
		frames = append(frames, frame)
	}
	pcm, err := encodeMusicSyncPCM(musicSyncPCMFrame{Generation: 7, Sequence: 2, TargetUs: 99, PCM: []byte{1, 2}})
	if err != nil {
		t.Fatal(err)
	}
	frames = append(frames, pcm)
	for _, frame := range frames {
		if !dispatchMusicSyncFrame(frame, r) {
			t.Fatalf("dispatch rejected valid frame %v", frame[0])
		}
	}
	if r.starts != 1 || r.pcms != 1 || r.clears != 1 || r.ends != 1 {
		t.Fatalf("receiver calls = %+v", r)
	}
	var nilReceiver speaker.MusicSyncReceiver
	if dispatchMusicSyncFrame(nil, nilReceiver) || dispatchMusicSyncFrame([]byte{0xff}, r) {
		t.Fatal("dispatch accepted invalid input")
	}
}

func TestControlClientStateAndNetworkHelpers(t *testing.T) {
	c := &ControlClient{}
	c.OnLEDAnim(nil)
	c.OnDisconnected(nil)
	c.OnConnected(nil)
	c.OnPending(nil)
	c.OnConfigApplied(nil)
	c.OnVolumeSet(nil)
	c.OnMuteToggle(nil)
	c.OnSpeakerFlush(nil)
	c.OnMusicFlush(nil)
	c.OnDuck(nil)
	c.OnWifiChange(nil)
	c.OnWifiCommit(nil)
	c.OnWifiScan(nil)
	c.OnTestAudio(nil)
	c.OnTestAudioCleanup(nil)
	if c.IsConnected() {
		t.Fatal("new client reported a connection")
	}
	c.conn = &websocket.Conn{}
	if !c.IsConnected() {
		t.Fatal("client with a connection reported disconnected")
	}
	c.conn = nil
	if got := c.lastKnownServer(); got != nil {
		t.Fatalf("lastKnownServer() = %#v, want nil", got)
	}

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	accepted := make(chan struct{})
	go func() {
		conn, err := listener.Accept()
		if err == nil {
			close(accepted)
			conn.Close()
		}
	}()
	if !probeTCP(listener.Addr().String(), time.Second) {
		t.Fatal("probeTCP rejected a listening endpoint")
	}
	select {
	case <-accepted:
	case <-time.After(time.Second):
		t.Fatal("probeTCP did not connect")
	}
	if probeTCP("127.0.0.1:1", time.Millisecond) {
		t.Fatal("probeTCP accepted a closed endpoint")
	}
}

func TestSendStatsDropsSafelyWhenDisconnected(t *testing.T) {
	c := &ControlClient{}
	rssi := -55
	temp := 42.5
	c.SendStats(DeviceStats{
		CPUPct: 12.5, MemUsedMb: 10, MemTotalMb: 20,
		StorageUsedMb: 30, StorageTotalMb: 40, WifiRssi: &rssi,
		WifiSsid: "Home", LinkSpeedMbps: 100, WifiFreqMhz: 5180,
		WifiBssid: "00:11:22:33:44:55", TxBytes: 1, RxBytes: 2,
		TxErrors: 3, TxDropped: 4, RxCrcErrors: 5, Ble: "ble",
		OwwShadow: "shadow", AmbientLux: new(int), CPUTempC: &temp,
		MaxTempC: &temp, CoresOnline: 2, CoresTotal: 4, ThermalCoreLimit: 4,
	})
}

func TestGetSerialNoUsesGetpropAndHasFailureFallback(t *testing.T) {
	dir := t.TempDir()
	getprop := filepath.Join(dir, "getprop")
	if err := os.WriteFile(getprop, []byte("#!/bin/sh\nprintf 'SERIAL-7\\n'\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Cleanup(func() { _ = os.Setenv("PATH", oldPath) })
	if err := os.Setenv("PATH", dir); err != nil {
		t.Fatal(err)
	}
	if got := GetSerialNo(); got != "SERIAL-7" {
		t.Fatalf("GetSerialNo() = %q, want SERIAL-7", got)
	}
	if err := os.Setenv("PATH", t.TempDir()); err != nil {
		t.Fatal(err)
	}
	if got := GetSerialNo(); got != "unknown-device" {
		t.Fatalf("GetSerialNo(failure) = %q, want unknown-device", got)
	}
}

func TestControlConnectRejectsPendingAndUnexpectedHandshake(t *testing.T) {
	for _, first := range []string{"pending", "surprise"} {
		t.Run(first, func(t *testing.T) {
			upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
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
				_ = conn.WriteJSON(map[string]string{"type": first})
			}))
			defer server.Close()
			addr := strings.TrimPrefix(server.URL, "http://")
			c := NewControlClient("handshake-test", nil, nil, nil)
			err := c.connect(context.Background(), &discovery.ServerInfo{Addr: addr, Host: strings.Split(addr, ":")[0]}, NewDataClient("handshake-test", nil, nil))
			if first == "pending" {
				if err != errPending {
					t.Fatalf("pending handshake error = %v, want errPending", err)
				}
			} else if err == nil || !strings.Contains(err.Error(), "unexpected first message") {
				t.Fatalf("unexpected handshake error = %v", err)
			}
		})
	}
}

func TestMusicSyncDecodersRejectInvalidFrames(t *testing.T) {
	if _, err := encodeMusicSyncControl(musicSyncStart, 0); err == nil {
		t.Fatal("accepted zero control generation")
	}
	if _, err := encodeMusicSyncControl(0xff, 1); err == nil {
		t.Fatal("accepted unknown control type")
	}
	if _, err := encodeMusicSyncPCM(musicSyncPCMFrame{Generation: 1, PCM: []byte{1}}); err == nil {
		t.Fatal("accepted odd PCM payload")
	}
	for _, raw := range [][]byte{nil, []byte{musicSyncPCM}, []byte{musicSyncPCM, 0, 0, 0, 1}} {
		if _, err := decodeMusicSyncPCM(raw); err == nil {
			t.Fatalf("accepted malformed PCM frame %v", raw)
		}
	}
	for _, raw := range [][]byte{nil, []byte{musicSyncStart}, []byte{0xff, 0, 0, 0, 1}, {musicSyncStart, 0, 0, 0, 0}} {
		if _, _, err := decodeMusicSyncControl(raw); err == nil {
			t.Fatalf("accepted malformed control frame %v", raw)
		}
	}
}

func TestDataClientHelpersReplaceReadyAddress(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.NotifyReady("first")
	d.NotifyReady("second")
	select {
	case got := <-d.readyCh:
		if got != "second" {
			t.Fatalf("ready address = %q, want latest address", got)
		}
	default:
		t.Fatal("NotifyReady did not publish an address")
	}

	d.SetShadowScorer(nil)
	if d.ShadowScorer() != nil {
		t.Fatal("nil shadow scorer was not retained")
	}
}

func TestDataClientStartMicWithoutConnectionIsSafe(t *testing.T) {
	d := NewDataClient("test", nil, nil)
	d.StartMic(false)
	d.StopMic()
	if d.micActive {
		t.Fatal("StartMic activated without a connection")
	}
}

func TestDataConnectDispatchesAudioPlanes(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Errorf("upgrade: %v", err)
			return
		}
		defer conn.Close()
		var identify map[string]string
		if err := conn.ReadJSON(&identify); err != nil {
			t.Errorf("identify: %v", err)
			return
		}
		if identify["type"] != "identify" {
			t.Errorf("identify = %#v", identify)
		}
		frames := [][]byte{
			{frameTypeSpeaker, 1, 2},
			{frameTypeEOS},
			{frameTypeMusic, 3, 4},
			{frameTypeMusicEOS},
			{frameTypeMusicSyncStart, 0, 0, 0, 7},
			{frameTypeMusicSyncPCM, 0, 0, 0, 7, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 99, 1, 2},
			{0xff},
		}
		for _, frame := range frames {
			if err := conn.WriteMessage(websocket.BinaryMessage, frame); err != nil {
				return
			}
		}
		_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, "done"), time.Now().Add(time.Second))
	}))
	defer server.Close()

	s := &helperSpeaker{}
	d := NewDataClient("data-test", nil, s)
	addr := "ws://" + strings.TrimPrefix(server.URL, "http://")
	if err := d.connect(context.Background(), addr); err == nil {
		t.Fatal("data connect unexpectedly succeeded after server close")
	}
	if s.periods != 1 || s.eos != 1 || s.music != 1 || s.musicEOS != 1 || s.starts != 1 || s.pcms != 1 {
		t.Fatalf("speaker calls = %+v", s)
	}
}
