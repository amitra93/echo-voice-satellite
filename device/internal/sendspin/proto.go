package sendspin

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
)

// Wire protocol constants, ported from aiosendspin's models. Control messages
// are JSON with the envelope {"payload":{…},"type":"<type>"}; audio arrives as
// binary WebSocket frames with a 9-byte big-endian header.
const (
	RolePlayer     = "player@v1"
	RoleController = "controller@v1"
	RoleMetadata   = "metadata@v1"

	CodecFLAC = "flac"
	CodecPCM  = "pcm"
	CodecOpus = "opus"

	TypeClientHello   = "client/hello"
	TypeClientTime    = "client/time"
	TypeClientState   = "client/state"
	TypeClientGoodbye = "client/goodbye"

	TypeServerHello  = "server/hello"
	TypeServerTime   = "server/time"
	TypeServerState  = "server/state"
	TypeServerCmd    = "server/command"
	TypeGroupUpdate  = "group/update"
	TypeStreamStart  = "stream/start"
	TypeStreamClear  = "stream/clear"
	TypeStreamEnd    = "stream/end"

	// Binary message header: message_type(1) + timestamp_us(8) big-endian.
	binaryHeaderSize = 9
	binAudioChunk    = 4
)

var errShortBinary = errors.New("sendspin: binary frame shorter than header")

// envelope is the outer {payload,type} wrapper.
type envelope struct {
	Payload json.RawMessage `json:"payload"`
	Type    string          `json:"type"`
}

func encodeMessage(msgType string, payload any) ([]byte, error) {
	p, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	return json.Marshal(envelope{Payload: p, Type: msgType})
}

// DecodeType returns the message type and the raw payload of a JSON control
// message without committing to a payload shape.
func DecodeType(data []byte) (string, json.RawMessage, error) {
	var e envelope
	if err := json.Unmarshal(data, &e); err != nil {
		return "", nil, err
	}
	return e.Type, e.Payload, nil
}

// ── Client -> Server payloads ────────────────────────────────────────────────

type SupportedAudioFormat struct {
	Codec      string `json:"codec"`
	Channels   int    `json:"channels"`
	SampleRate int    `json:"sample_rate"`
	BitDepth   int    `json:"bit_depth"`
}

type PlayerSupport struct {
	SupportedFormats  []SupportedAudioFormat `json:"supported_formats"`
	BufferCapacity    int                    `json:"buffer_capacity"`
	SupportedCommands []string               `json:"supported_commands"`
}

type clientHelloPayload struct {
	ClientID       string        `json:"client_id"`
	Name           string        `json:"name"`
	Version        int           `json:"version"`
	SupportedRoles []string      `json:"supported_roles"`
	PlayerSupport  PlayerSupport `json:"player_support"`
}

// EncodeClientHello builds the client/hello message advertising the player
// (and optionally controller/metadata) roles and the supported formats in
// priority order (first is preferred).
func EncodeClientHello(clientID, name string, roles []string, support PlayerSupport) ([]byte, error) {
	return encodeMessage(TypeClientHello, clientHelloPayload{
		ClientID: clientID, Name: name, Version: 1,
		SupportedRoles: roles, PlayerSupport: support,
	})
}

type clientTimePayload struct {
	ClientTransmitted int64 `json:"client_transmitted"`
}

// EncodeClientTime builds a client/time message stamping the transmit instant
// (t1) for the NTP-style exchange.
func EncodeClientTime(clientTransmitted int64) ([]byte, error) {
	return encodeMessage(TypeClientTime, clientTimePayload{ClientTransmitted: clientTransmitted})
}

type playerState struct {
	State             string   `json:"state,omitempty"`
	Volume            *int     `json:"volume,omitempty"`
	Muted             *bool    `json:"muted,omitempty"`
	StaticDelayMs     *int     `json:"static_delay_ms,omitempty"`
	RequiredLeadMs    *int     `json:"required_lead_time_ms,omitempty"`
	MinBufferMs       *int     `json:"min_buffer_ms,omitempty"`
	SupportedCommands []string `json:"supported_commands,omitempty"`
}

type clientStatePayload struct {
	State  string       `json:"state,omitempty"`
	Player *playerState `json:"player,omitempty"`
}

// ClientState is the state a player reports (volume/mute + the initial buffer
// parameters). Pointers are omitted when nil so incremental updates carry only
// what changed.
type ClientState struct {
	State          string
	Volume         *int
	Muted          *bool
	StaticDelayMs  *int
	RequiredLeadMs *int
	MinBufferMs    *int
}

// EncodeClientState builds a client/state message.
func EncodeClientState(s ClientState) ([]byte, error) {
	return encodeMessage(TypeClientState, clientStatePayload{
		State: s.State,
		Player: &playerState{
			Volume: s.Volume, Muted: s.Muted, StaticDelayMs: s.StaticDelayMs,
			RequiredLeadMs: s.RequiredLeadMs, MinBufferMs: s.MinBufferMs,
		},
	})
}

type clientGoodbyePayload struct {
	Reason string `json:"reason"`
}

// EncodeClientGoodbye builds the clean-leave message.
func EncodeClientGoodbye(reason string) ([]byte, error) {
	return encodeMessage(TypeClientGoodbye, clientGoodbyePayload{Reason: reason})
}

// ── Server -> Client payloads ────────────────────────────────────────────────

type ServerHello struct {
	ServerID string `json:"server_id"`
	Name     string `json:"name"`
	Version  int    `json:"version"`
}

// ServerTime carries the three NTP timestamps (t1,t2,t3); the client supplies
// t4 (its receive instant) to close the exchange.
type ServerTime struct {
	ClientTransmitted int64 `json:"client_transmitted"`
	ServerReceived    int64 `json:"server_received"`
	ServerTransmitted int64 `json:"server_transmitted"`
}

// StreamStartPlayer is the audio format for a starting stream. CodecHeader is
// the decoder init payload (e.g. the FLAC STREAMINFO), base64 in JSON.
type StreamStartPlayer struct {
	Codec       string `json:"codec"`
	SampleRate  int    `json:"sample_rate"`
	Channels    int    `json:"channels"`
	BitDepth    int    `json:"bit_depth"`
	CodecHeader []byte `json:"codec_header"`
}

type streamStartPayload struct {
	Player *StreamStartPlayer `json:"player"`
}

type rolesPayload struct {
	Roles []string `json:"roles"`
}

// ServerCommand is a controller-role command from MA (volume/mute/transport).
type ServerCommand struct {
	Command       string `json:"command"`
	Volume        *int   `json:"volume"`
	Mute          *bool  `json:"mute"`
	StaticDelayMs *int   `json:"static_delay_ms"`
}

type serverCommandPayload struct {
	Player *ServerCommand `json:"player"`
}

// GroupUpdate carries the group playback state + identity.
type GroupUpdate struct {
	PlaybackState string `json:"playback_state"`
	GroupID       string `json:"group_id"`
	GroupName     string `json:"group_name"`
}

// ParseServerHello etc. decode a payload of the matching type.
func ParseServerHello(p json.RawMessage) (ServerHello, error) {
	var v ServerHello
	err := json.Unmarshal(p, &v)
	return v, err
}

func ParseServerTime(p json.RawMessage) (ServerTime, error) {
	var v ServerTime
	err := json.Unmarshal(p, &v)
	return v, err
}

// ParseStreamStart returns the player format (may be nil if the stream/start
// targets only non-player roles).
func ParseStreamStart(p json.RawMessage) (*StreamStartPlayer, error) {
	var v streamStartPayload
	if err := json.Unmarshal(p, &v); err != nil {
		return nil, err
	}
	return v.Player, nil
}

// StreamRolesIncludePlayer reports whether a stream/clear or stream/end targets
// the player role. Per the spec, an entry may be "player@v1" or "_"-prefixed;
// an empty list means all roles.
func StreamRolesInclude(p json.RawMessage, role string) (bool, error) {
	var v rolesPayload
	if err := json.Unmarshal(p, &v); err != nil {
		return false, err
	}
	if len(v.Roles) == 0 {
		return true, nil
	}
	for _, r := range v.Roles {
		if r == role || r == "_"+role {
			return true, nil
		}
	}
	return false, nil
}

// ParseServerCommand returns the player command (may be nil).
func ParseServerCommand(p json.RawMessage) (*ServerCommand, error) {
	var v serverCommandPayload
	if err := json.Unmarshal(p, &v); err != nil {
		return nil, err
	}
	return v.Player, nil
}

func ParseGroupUpdate(p json.RawMessage) (GroupUpdate, error) {
	var v GroupUpdate
	err := json.Unmarshal(p, &v)
	return v, err
}

// ── Binary audio frames ──────────────────────────────────────────────────────

// AudioChunk is a decoded binary audio message: the server presentation
// timestamp and the codec payload (FLAC/PCM/Opus).
type AudioChunk struct {
	TimestampUs int64
	Payload     []byte
}

// ParseBinaryFrame decodes a binary WebSocket frame. ok is false (with a nil
// error) for a well-formed non-audio binary frame (artwork/visualizer), which
// the caller ignores.
func ParseBinaryFrame(data []byte) (chunk AudioChunk, isAudio bool, err error) {
	if len(data) < binaryHeaderSize {
		return AudioChunk{}, false, errShortBinary
	}
	if data[0] != binAudioChunk {
		return AudioChunk{}, false, nil
	}
	ts := int64(binary.BigEndian.Uint64(data[1:binaryHeaderSize]))
	return AudioChunk{TimestampUs: ts, Payload: data[binaryHeaderSize:]}, true, nil
}

// PackAudioChunk builds a binary audio frame (used by tests and any loopback).
func PackAudioChunk(timestampUs int64, payload []byte) []byte {
	out := make([]byte, binaryHeaderSize+len(payload))
	out[0] = binAudioChunk
	binary.BigEndian.PutUint64(out[1:binaryHeaderSize], uint64(timestampUs))
	copy(out[binaryHeaderSize:], payload)
	return out
}

func (c AudioChunk) String() string {
	return fmt.Sprintf("AudioChunk(ts=%dus, %d bytes)", c.TimestampUs, len(c.Payload))
}
