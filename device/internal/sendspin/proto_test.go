package sendspin

import (
	"bytes"
	"encoding/json"
	"testing"
)

func decodePayload[T any](t *testing.T, data []byte, wantType string) T {
	t.Helper()
	typ, raw, err := DecodeType(data)
	if err != nil {
		t.Fatalf("DecodeType: %v", err)
	}
	if typ != wantType {
		t.Fatalf("type=%q want %q", typ, wantType)
	}
	var v T
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatalf("unmarshal payload: %v", err)
	}
	return v
}

func TestEncodeClientHelloShape(t *testing.T) {
	support := PlayerSupport{
		SupportedFormats: []SupportedAudioFormat{
			{Codec: CodecFLAC, Channels: 1, SampleRate: 48000, BitDepth: 16},
			{Codec: CodecPCM, Channels: 1, SampleRate: 48000, BitDepth: 16},
		},
		BufferCapacity:    480000,
		SupportedCommands: []string{"volume", "mute"},
	}
	data, err := EncodeClientHello("echomuse-TEST", "Study",
		[]string{RolePlayer, RoleController, RoleMetadata}, support)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	p := decodePayload[clientHelloPayload](t, data, TypeClientHello)
	if p.ClientID != "echomuse-TEST" || p.Name != "Study" || p.Version != 1 {
		t.Fatalf("hello identity fields wrong: %+v", p)
	}
	if len(p.SupportedRoles) != 3 || p.SupportedRoles[0] != "player@v1" {
		t.Fatalf("roles wrong: %v", p.SupportedRoles)
	}
	if p.PlayerSupport.BufferCapacity != 480000 ||
		len(p.PlayerSupport.SupportedFormats) != 2 ||
		p.PlayerSupport.SupportedFormats[0].Codec != "flac" {
		t.Fatalf("player_support wrong: %+v", p.PlayerSupport)
	}
}

func TestEncodeClientTimeAndGoodbye(t *testing.T) {
	data, _ := EncodeClientTime(123456)
	p := decodePayload[clientTimePayload](t, data, TypeClientTime)
	if p.ClientTransmitted != 123456 {
		t.Fatalf("client_transmitted=%d", p.ClientTransmitted)
	}
	gb, _ := EncodeClientGoodbye("user_request")
	g := decodePayload[clientGoodbyePayload](t, gb, TypeClientGoodbye)
	if g.Reason != "user_request" {
		t.Fatalf("goodbye reason=%q", g.Reason)
	}
}

func TestEncodeClientStateOmitsNil(t *testing.T) {
	vol := 80
	muted := false
	data, _ := EncodeClientState(ClientState{State: "synchronized", Volume: &vol, Muted: &muted})
	_, raw, _ := DecodeType(data)
	s := string(raw)
	// volume/muted present, but the unset lead/buffer/static fields must be omitted.
	if !bytes.Contains([]byte(s), []byte(`"volume":80`)) {
		t.Fatalf("volume missing: %s", s)
	}
	if bytes.Contains([]byte(s), []byte("required_lead_time_ms")) ||
		bytes.Contains([]byte(s), []byte("min_buffer_ms")) {
		t.Fatalf("nil fields were not omitted: %s", s)
	}
}

func TestParseServerTime(t *testing.T) {
	raw := json.RawMessage(`{"client_transmitted":100,"server_received":150,"server_transmitted":160}`)
	st, err := ParseServerTime(raw)
	if err != nil {
		t.Fatal(err)
	}
	if st.ClientTransmitted != 100 || st.ServerReceived != 150 || st.ServerTransmitted != 160 {
		t.Fatalf("server time wrong: %+v", st)
	}
}

func TestParseStreamStartWithCodecHeader(t *testing.T) {
	// codec_header is a JSON base64 string; "ZkxhQw==" decodes to "fLaC".
	raw := json.RawMessage(`{"player":{"codec":"flac","sample_rate":48000,"channels":1,"bit_depth":16,"codec_header":"ZkxhQw=="}}`)
	p, err := ParseStreamStart(raw)
	if err != nil {
		t.Fatal(err)
	}
	if p == nil || p.Codec != "flac" || p.SampleRate != 48000 || p.Channels != 1 {
		t.Fatalf("stream/start player wrong: %+v", p)
	}
	if string(p.CodecHeader) != "fLaC" {
		t.Fatalf("codec_header decode = %q want fLaC", p.CodecHeader)
	}
}

func TestParseStreamStartNoPlayer(t *testing.T) {
	p, err := ParseStreamStart(json.RawMessage(`{"visualizer":{}}`))
	if err != nil || p != nil {
		t.Fatalf("expected nil player, got %+v err=%v", p, err)
	}
}

func TestStreamRolesInclude(t *testing.T) {
	cases := []struct {
		raw  string
		want bool
	}{
		{`{"roles":[]}`, true},               // empty = all
		{`{"roles":["player@v1"]}`, true},    // explicit
		{`{"roles":["_player@v1"]}`, true},   // underscore-prefixed
		{`{"roles":["metadata@v1"]}`, false}, // other role only
	}
	for _, c := range cases {
		got, err := StreamRolesInclude(json.RawMessage(c.raw), RolePlayer)
		if err != nil {
			t.Fatalf("%s: %v", c.raw, err)
		}
		if got != c.want {
			t.Fatalf("StreamRolesInclude(%s)=%v want %v", c.raw, got, c.want)
		}
	}
}

func TestParseServerCommandVolumeMute(t *testing.T) {
	p, err := ParseServerCommand(json.RawMessage(`{"player":{"command":"volume","volume":42}}`))
	if err != nil || p == nil {
		t.Fatalf("parse: %v %+v", err, p)
	}
	if p.Command != "volume" || p.Volume == nil || *p.Volume != 42 {
		t.Fatalf("volume command wrong: %+v", p)
	}
	m, _ := ParseServerCommand(json.RawMessage(`{"player":{"command":"mute","mute":true}}`))
	if m.Command != "mute" || m.Mute == nil || *m.Mute != true {
		t.Fatalf("mute command wrong: %+v", m)
	}
}

func TestParseGroupUpdate(t *testing.T) {
	g, err := ParseGroupUpdate(json.RawMessage(`{"playback_state":"playing","group_id":"g1","group_name":"Study"}`))
	if err != nil {
		t.Fatal(err)
	}
	if g.PlaybackState != "playing" || g.GroupName != "Study" {
		t.Fatalf("group update wrong: %+v", g)
	}
}

func TestBinaryFrameRoundTrip(t *testing.T) {
	payload := []byte{0x01, 0x02, 0x03, 0x04}
	frame := PackAudioChunk(-1234567, payload) // negative ts must survive (signed)
	chunk, isAudio, err := ParseBinaryFrame(frame)
	if err != nil || !isAudio {
		t.Fatalf("parse: isAudio=%v err=%v", isAudio, err)
	}
	if chunk.TimestampUs != -1234567 {
		t.Fatalf("timestamp=%d", chunk.TimestampUs)
	}
	if !bytes.Equal(chunk.Payload, payload) {
		t.Fatalf("payload=%v", chunk.Payload)
	}
}

func TestBinaryFrameNonAudioAndShort(t *testing.T) {
	// type 16 = visualizer loudness — well-formed, not audio, no error.
	notAudio := make([]byte, binaryHeaderSize+2)
	notAudio[0] = 16
	_, isAudio, err := ParseBinaryFrame(notAudio)
	if err != nil || isAudio {
		t.Fatalf("non-audio: isAudio=%v err=%v", isAudio, err)
	}
	if _, _, err := ParseBinaryFrame([]byte{4, 0, 0}); err == nil {
		t.Fatal("expected error on short frame")
	}
}
