package sendspin

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

type sinkCall struct {
	generation, sequence uint32
	target               int64
	pcm                  []byte
}

type fakeSink struct {
	starts, clears, ends int
	pcm                  []sinkCall
}

type fakeConn struct {
	reads []struct {
		kind int
		data []byte
		err  error
	}
	writes [][]byte
	closed bool
}

func (c *fakeConn) ReadMessage() (int, []byte, error) {
	if len(c.reads) == 0 {
		return 0, nil, errors.New("end")
	}
	r := c.reads[0]
	c.reads = c.reads[1:]
	return r.kind, r.data, r.err
}
func (c *fakeConn) WriteMessage(_ int, data []byte) error {
	c.writes = append(c.writes, append([]byte(nil), data...))
	return nil
}
func (c *fakeConn) SetReadDeadline(time.Time) error { return nil }
func (c *fakeConn) Close() error                    { c.closed = true; return nil }

func (s *fakeSink) MusicSyncStart(uint32) bool { s.starts++; return true }
func (s *fakeSink) MusicSyncPCM(g, seq uint32, target int64, pcm []byte) bool {
	s.pcm = append(s.pcm, sinkCall{g, seq, target, append([]byte(nil), pcm...)})
	return true
}
func (s *fakeSink) MusicSyncClear(uint32) bool { s.clears++; return true }
func (s *fakeSink) MusicSyncEnd(uint32) bool   { s.ends++; return true }

func serverMessage(t *testing.T, typ string, payload any) []byte {
	t.Helper()
	b, err := encodeMessage(typ, payload)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestClientStartsOnlyAfterSyncAndSchedulesPCM(t *testing.T) {
	sink := &fakeSink{}
	c := NewClient("id", "Study", sink)
	now := int64(130)
	c.NowUs = func() int64 { return now }

	if _, err := c.HandleText(serverMessage(t, TypeStreamStart, map[string]any{"player": map[string]any{
		"codec": CodecPCM, "sample_rate": 48000, "channels": 1, "bit_depth": 16,
	}})); err != nil {
		t.Fatal(err)
	}
	if sink.starts != 1 {
		t.Fatalf("starts=%d", sink.starts)
	}
	// A chunk before the second NTP sample is ignored rather than played with
	// an uncalibrated presentation clock.
	if err := c.HandleBinary(PackAudioChunk(500, []byte{1, 2})); err != nil {
		t.Fatal(err)
	}
	if len(sink.pcm) != 0 {
		t.Fatal("audio scheduled before time sync")
	}

	for _, p := range []ServerTime{
		{ClientTransmitted: 100, ServerReceived: 110, ServerTransmitted: 120},
		{ClientTransmitted: 200, ServerReceived: 210, ServerTransmitted: 220},
	} {
		if _, err := c.HandleText(serverMessage(t, TypeServerTime, p)); err != nil {
			t.Fatal(err)
		}
		now += 100
	}
	if !c.filter.IsSynchronized() {
		t.Fatal("client did not synchronize after two samples")
	}
	if err := c.HandleBinary(PackAudioChunk(500, []byte{1, 2, 3, 4})); err != nil {
		t.Fatal(err)
	}
	if len(sink.pcm) != 1 || sink.pcm[0].generation != 1 || sink.pcm[0].sequence != 1 || sink.pcm[0].target != 500 {
		t.Fatalf("scheduled calls=%+v", sink.pcm)
	}
}

func TestClientRoutesPlayerStreamLifecycleAndCommands(t *testing.T) {
	sink := &fakeSink{}
	c := NewClient("id", "Study", sink)
	var volume int
	var mute bool
	c.OnVolume = func(v int) { volume = v }
	c.OnMute = func(v bool) { mute = v }
	if _, err := c.HandleText(serverMessage(t, TypeStreamStart, map[string]any{"player": map[string]any{
		"codec": CodecPCM, "sample_rate": 48000, "channels": 1, "bit_depth": 16,
	}})); err != nil {
		t.Fatal(err)
	}
	// Non-player lifecycle messages must not stop this stream.
	if _, err := c.HandleText(serverMessage(t, TypeStreamClear, map[string]any{"roles": []string{RoleMetadata}})); err != nil {
		t.Fatal(err)
	}
	if sink.clears != 0 {
		t.Fatal("cleared non-player stream")
	}
	if _, err := c.HandleText(serverMessage(t, TypeStreamClear, map[string]any{"roles": []string{}})); err != nil {
		t.Fatal(err)
	}
	if _, err := c.HandleText(serverMessage(t, TypeStreamEnd, map[string]any{"roles": []string{RolePlayer}})); err != nil {
		t.Fatal(err)
	}
	if sink.clears != 1 || sink.ends != 1 {
		t.Fatalf("lifecycle clear=%d end=%d", sink.clears, sink.ends)
	}
	out, err := c.HandleText(serverMessage(t, TypeServerCmd, map[string]any{"player": map[string]any{
		"command": "volume", "volume": 42,
	}}))
	if err != nil || volume != 42 || len(out) != 1 {
		t.Fatalf("volume cmd err=%v volume=%d outbound=%d", err, volume, len(out))
	}
	out, err = c.HandleText(serverMessage(t, TypeServerCmd, map[string]any{"player": map[string]any{
		"command": "mute", "mute": true,
	}}))
	if err != nil || !mute || len(out) != 1 {
		t.Fatalf("mute cmd err=%v mute=%v outbound=%d", err, mute, len(out))
	}
	_, raw, _ := DecodeType(out[0])
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatal(err)
	}
}

func TestClientInitialMessagesAdvertiseFormatsAndState(t *testing.T) {
	c := NewClient("id", "Study", &fakeSink{})
	c.NowUs = func() int64 { return 123 }
	c.SetPlayerState(42, true)
	messages, err := c.InitialMessages()
	if err != nil || len(messages) != 3 {
		t.Fatalf("InitialMessages err=%v len=%d", err, len(messages))
	}
	typ, _, _ := DecodeType(messages[0])
	if typ != TypeClientHello {
		t.Fatalf("first message=%s", typ)
	}
	typ, _, _ = DecodeType(messages[2])
	if typ != TypeClientTime {
		t.Fatalf("last message=%s", typ)
	}
	state := decodePayload[clientStatePayload](t, messages[1], TypeClientState)
	if state.Player == nil || state.Player.Volume == nil || *state.Player.Volume != 42 ||
		state.Player.Muted == nil || !*state.Player.Muted {
		t.Fatalf("seeded state missing from hello: %+v", state.Player)
	}
}

func TestClientRunConnWritesHandshakeAndCloses(t *testing.T) {
	c := NewClient("id", "Study", &fakeSink{})
	c.NowUs = func() int64 { return 100 }
	conn := &fakeConn{reads: []struct {
		kind int
		data []byte
		err  error
	}{
		{kind: websocket.TextMessage, data: serverMessage(t, TypeServerHello, ServerHello{})},
		{err: errors.New("socket closed")},
	}}
	if err := c.RunConn(context.Background(), conn); err == nil {
		t.Fatal("RunConn returned nil after socket error")
	}
	if !conn.closed || len(conn.writes) != 3 {
		t.Fatalf("closed=%v writes=%d", conn.closed, len(conn.writes))
	}
}

func TestManagerAcceptsEmptyAndReplacementConfiguration(t *testing.T) {
	m := NewManager(NewClient("id", "Study", &fakeSink{}))
	m.Configure("")
	m.Configure("   ")
	// This starts a cancellable background dial only after a real address;
	// close immediately so the test never relies on a network endpoint.
	m.Configure("ws://127.0.0.1:1/sendspin")
	m.Close()
}
