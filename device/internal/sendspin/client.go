package sendspin

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	deviceclock "github.com/wilbowes/EchoMuse/internal/clock"
)

const (
	defaultRequiredLeadMs = 4000
	defaultMinBufferMs    = 1000
	defaultBufferCapacity = 480000
)

// ScheduledSink is the timestamped music renderer. It deliberately matches
// the existing music_sync receiver so native and legacy transports share one
// renderer during the capability-gated rollout.
type ScheduledSink interface {
	MusicSyncStart(generation uint32) bool
	MusicSyncPCM(generation, sequence uint32, targetUs int64, pcm []byte) bool
	MusicSyncClear(generation uint32) bool
	MusicSyncEnd(generation uint32) bool
}

// Conn is the small gorilla/websocket surface the client needs. Keeping it
// narrow makes the protocol state machine host-testable without a TCP server.
type Conn interface {
	ReadMessage() (messageType int, p []byte, err error)
	WriteMessage(messageType int, data []byte) error
	SetReadDeadline(t time.Time) error
	Close() error
}

// Client is one native Sendspin player. All renderer calls happen from its
// read loop, preserving stream ordering without a second queue.
type Client struct {
	ID, Name string
	Sink     ScheduledSink
	NowUs    func() int64

	OnVolume func(int)
	OnMute   func(bool)

	filter     *TimeFilter
	decoder    *Decoder
	generation uint32
	sequence   uint32
	volume     int
	muted      bool
	mu         sync.Mutex

	// Audio telemetry is owned by the read loop and reset for every stream.
	// It distinguishes transport/decode gaps from a renderer or ALSA fault.
	lastAudioAt      time.Time
	lastTargetUs     int64
	lastSamples      int
	audioFrames      uint64
	audioBytes       uint64
	maxArrivalGap    time.Duration
	maxDecode        time.Duration
	maxTargetErrorUs int64
	nextAudioLog     time.Time
	stateUpdates     chan []byte
}

func NewClient(id, name string, sink ScheduledSink) *Client {
	return &Client{ID: id, Name: name, Sink: sink, NowUs: deviceclock.NowUs,
		filter: NewTimeFilter(), volume: 100, stateUpdates: make(chan []byte, 1)}
}

// SetPlayerState seeds state from the device before the first hello. It does
// not invoke callbacks: the caller already applied the physical state and MA
// merely needs an accurate initial report.
func (c *Client) SetPlayerState(volume int, muted bool) {
	if volume < 0 {
		volume = 0
	}
	if volume > 100 {
		volume = 100
	}
	c.mu.Lock()
	c.volume, c.muted = volume, muted
	c.mu.Unlock()
}

// SetLocalVolume updates the MA player state after a physical device change.
// Keep only the newest update if the websocket writer is briefly busy.
func (c *Client) SetLocalVolume(volume int) {
	if volume < 0 {
		volume = 0
	}
	if volume > 100 {
		volume = 100
	}
	c.mu.Lock()
	c.volume = volume
	muted := c.muted
	c.mu.Unlock()
	state, err := EncodeClientState(ClientState{State: "synchronized", Volume: &volume, Muted: &muted})
	if err != nil {
		return
	}
	select {
	case c.stateUpdates <- state:
	default:
		<-c.stateUpdates
		c.stateUpdates <- state
	}
}

// InitialMessages performs the hello plus the first state and time request.
// The caller writes these before reading so MA has our player contract before
// it can send a stream.
func (c *Client) InitialMessages() ([][]byte, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	lead, minBuffer, delay := defaultRequiredLeadMs, defaultMinBufferMs, 0
	volume, muted := c.volume, c.muted
	hello, err := EncodeClientHello(c.ID, c.Name, []string{RolePlayer, RoleController}, PlayerSupport{
		SupportedFormats: []SupportedAudioFormat{
			{Codec: CodecFLAC, Channels: 1, SampleRate: 48000, BitDepth: 16},
			{Codec: CodecPCM, Channels: 1, SampleRate: 48000, BitDepth: 16},
		}, BufferCapacity: defaultBufferCapacity, SupportedCommands: []string{"volume", "mute"},
		// DO NOT add "pause"/"stop"/"play" etc. here. player_support.
		// supported_commands is validated against a strict schema by MA's
		// aiosendspin server (observed: ghcr.io/music-assistant/server:stable,
		// error "Malformed client/hello: Field \"payload\" of type
		// ClientHelloPayload... has invalid value") — an unrecognised entry
		// fails client/hello validation and the connection is closed (code
		// 1000, no reason, before MA even sends its own server/hello) before
		// the player is registered at all. This is not the same list as the
		// design doc's "play, pause, stop, next, previous, seek, volume,
		// mute" — that describes controller@v1-role commands a client can
		// SEND to control the whole queue, not values this field accepts.
		// Confirmed by directly connecting to MA and diffing the accepted vs
		// rejected hello payloads; see the group/update handling below for
		// how pause/stop are actually detected instead.
	})
	if err != nil {
		return nil, err
	}
	state, err := EncodeClientState(ClientState{State: "synchronized", Volume: &volume, Muted: &muted,
		StaticDelayMs: &delay, RequiredLeadMs: &lead, MinBufferMs: &minBuffer})
	if err != nil {
		return nil, err
	}
	now := c.now()
	timeMsg, err := EncodeClientTime(now)
	if err != nil {
		return nil, err
	}
	return [][]byte{hello, state, timeMsg}, nil
}

// HandleText applies one MA control message. It returns outbound state updates
// caused by server commands; the network loop sends them after handling.
func (c *Client) HandleText(raw []byte) ([][]byte, error) {
	typ, payload, err := DecodeType(raw)
	if err != nil {
		return nil, err
	}
	// Transport control (pause/stop/play) is diagnosed by what MA actually
	// sends, not by reading the spec — server/hello, server/state and
	// server/time are excluded because they are frequent/verbose and
	// carry nothing about playback intent.
	switch typ {
	case TypeServerHello, TypeServerState, TypeServerTime:
	default:
		log.Printf("[sendspin] recv %s %s", typ, payload)
	}
	switch typ {
	case TypeServerHello, TypeServerState:
		return nil, nil
	case TypeGroupUpdate:
		return nil, c.handleGroupUpdate(payload)
	case TypeServerTime:
		st, err := ParseServerTime(payload)
		if err != nil {
			return nil, err
		}
		now := c.now()
		measurement := ((st.ServerReceived - st.ClientTransmitted) + (st.ServerTransmitted - now)) / 2
		maxError := ((now - st.ClientTransmitted) - (st.ServerTransmitted - st.ServerReceived)) / 2
		if maxError < 0 {
			return nil, fmt.Errorf("sendspin: invalid server/time duration")
		}
		c.filter.Update(measurement, maxError, now)
		return nil, nil
	case TypeStreamStart:
		p, err := ParseStreamStart(payload)
		if err != nil || p == nil {
			return nil, err
		}
		d, err := NewDecoder(*p)
		if err != nil {
			return nil, err
		}
		c.mu.Lock()
		c.generation++
		if c.generation == 0 {
			c.generation++
		}
		c.sequence = 0
		c.decoder = d
		c.lastAudioAt = time.Time{}
		c.lastTargetUs, c.lastSamples = 0, 0
		c.audioFrames, c.audioBytes = 0, 0
		c.maxArrivalGap, c.maxDecode, c.maxTargetErrorUs = 0, 0, 0
		c.nextAudioLog = time.Now().Add(5 * time.Second)
		generation := c.generation
		c.mu.Unlock()
		if c.Sink == nil || !c.Sink.MusicSyncStart(generation) {
			return nil, errors.New("sendspin: scheduled sink refused stream")
		}
		return nil, nil
	case TypeStreamClear, TypeStreamEnd:
		include, err := StreamRolesInclude(payload, RolePlayer)
		if err != nil || !include {
			return nil, err
		}
		c.mu.Lock()
		generation := c.generation
		c.mu.Unlock()
		if generation == 0 || c.Sink == nil {
			return nil, nil
		}
		if typ == TypeStreamClear {
			c.Sink.MusicSyncClear(generation)
		} else {
			c.Sink.MusicSyncEnd(generation)
		}
		return nil, nil
	case TypeServerCmd:
		cmd, err := ParseServerCommand(payload)
		if err != nil || cmd == nil {
			return nil, err
		}
		return c.applyCommand(cmd)
	default:
		return nil, nil // Forward-compatible: unknown message types are ignored.
	}
}

// handleGroupUpdate flushes the scheduled queue when MA reports the group has
// stopped.
//
// THIS, NOT AN EXPLICIT PAUSE/STOP COMMAND, IS HOW MA SIGNALS PAUSE/STOP.
// player_support.supported_commands has no room for them (see the comment on
// SupportedCommands in InitialMessages) — MA's aiosendspin server hard-rejects
// client/hello and never even opens the session if it's tried. Captured live
// against a real pause: MA sends stream/end, then
// group/update{playback_state:"stopped"}, and nothing else until the user
// resumes. Without a flush here the renderer drains whatever MA had already
// pushed ahead of real time (RequiredLeadMs) — routinely several seconds,
// per the scheduled-music telemetry — so the room keeps playing for that long
// after the user asked it to stop.
//
// "stopped" is what makes this safe to flush unconditionally, unlike
// stream/end alone: a track ending naturally is ALSO a stream/end, but the
// group stays "playing" through an immediate transition to the next track (a
// skip is stream/end followed immediately by a new stream/start, captured
// live, with no group/update in between) — so this never fires on an
// ordinary track change, only when MA itself has decided nothing should be
// audible right now.
func (c *Client) handleGroupUpdate(payload json.RawMessage) error {
	g, err := ParseGroupUpdate(payload)
	if err != nil {
		return err
	}
	if g.PlaybackState != "stopped" {
		return nil
	}
	c.mu.Lock()
	generation, sink := c.generation, c.Sink
	c.mu.Unlock()
	if generation != 0 && sink != nil {
		sink.MusicSyncClear(generation)
	}
	return nil
}

func (c *Client) applyCommand(cmd *ServerCommand) ([][]byte, error) {
	c.mu.Lock()
	changed := false
	if cmd.Command == "volume" && cmd.Volume != nil {
		c.volume = *cmd.Volume
		changed = true
	}
	if cmd.Command == "mute" && cmd.Mute != nil {
		c.muted = *cmd.Mute
		changed = true
	}
	volume, muted := c.volume, c.muted
	c.mu.Unlock()
	if cmd.Volume != nil && c.OnVolume != nil {
		c.OnVolume(volume)
	}
	if cmd.Mute != nil && c.OnMute != nil {
		c.OnMute(muted)
	}
	if !changed {
		return nil, nil
	}
	state, err := EncodeClientState(ClientState{State: "synchronized", Volume: &volume, Muted: &muted})
	if err != nil {
		return nil, err
	}
	return [][]byte{state}, nil
}

// HandleBinary decodes and schedules one audio chunk. Playback is held until
// the NTP filter has two accepted measurements; playing with an unknown clock
// is a wrong answer, not an acceptable degraded mode.
func (c *Client) HandleBinary(raw []byte) error {
	chunk, audio, err := ParseBinaryFrame(raw)
	if err != nil || !audio {
		return err
	}
	if !c.filter.IsSynchronized() {
		return nil
	}
	c.mu.Lock()
	generation, decoder := c.generation, c.decoder
	c.mu.Unlock()
	if generation == 0 || decoder == nil || c.Sink == nil {
		return nil
	}
	decodeStart := time.Now()
	pcm, err := decoder.Decode(chunk.Payload)
	if err != nil {
		return err
	}
	decodeElapsed := time.Since(decodeStart)
	now := time.Now()
	targetUs := c.filter.ComputeClientTime(chunk.TimestampUs)
	c.mu.Lock()
	c.sequence++
	sequence := c.sequence
	if !c.lastAudioAt.IsZero() {
		if gap := now.Sub(c.lastAudioAt); gap > c.maxArrivalGap {
			c.maxArrivalGap = gap
		}
		want := c.lastTargetUs + int64(c.lastSamples)*1_000_000/48000
		if err := targetUs - want; err < 0 {
			err = -err
			if err > c.maxTargetErrorUs {
				c.maxTargetErrorUs = err
			}
		} else if err > c.maxTargetErrorUs {
			c.maxTargetErrorUs = err
		}
	}
	c.lastAudioAt = now
	c.lastTargetUs = targetUs
	c.lastSamples = len(pcm) / 2
	c.audioFrames++
	c.audioBytes += uint64(len(pcm))
	if decodeElapsed > c.maxDecode {
		c.maxDecode = decodeElapsed
	}
	if !c.nextAudioLog.IsZero() && !now.Before(c.nextAudioLog) {
		log.Printf("[sendspin] audio frames=%d bytes=%d maxGap=%s maxDecode=%s maxTargetError=%dus",
			c.audioFrames, c.audioBytes, c.maxArrivalGap.Round(time.Microsecond),
			c.maxDecode.Round(time.Microsecond), c.maxTargetErrorUs)
		c.audioFrames, c.audioBytes = 0, 0
		c.maxArrivalGap, c.maxDecode, c.maxTargetErrorUs = 0, 0, 0
		c.nextAudioLog = now.Add(5 * time.Second)
	}
	c.mu.Unlock()
	if !c.Sink.MusicSyncPCM(generation, sequence, targetUs, pcm) {
		return errors.New("sendspin: scheduled sink refused audio")
	}
	return nil
}

func (c *Client) now() int64 {
	if c.NowUs != nil {
		return c.NowUs()
	}
	return deviceclock.NowUs()
}

// Run connects, performs the handshake and runs until ctx cancellation or a
// socket error. Reconnect/backoff belongs to the device wiring, so one failed
// session is observable and testable on its own.
func (c *Client) Run(ctx context.Context, url string) error {
	conn, _, err := websocket.DefaultDialer.DialContext(ctx, url, nil)
	if err != nil {
		return err
	}
	return c.RunConn(ctx, conn)
}

func (c *Client) RunConn(ctx context.Context, conn Conn) error {
	sessionCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	// Gorilla permits one concurrent reader and one concurrent writer, but not
	// multiple writers. Keep all writes serialized while the time-sync ticker
	// runs independently of the blocking read loop.
	var writeMu sync.Mutex
	write := func(messageType int, data []byte) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		return conn.WriteMessage(messageType, data)
	}
	tickerDone := make(chan struct{})
	go func() {
		ticker := time.NewTicker(TimeSyncInterval)
		defer ticker.Stop()
		defer close(tickerDone)
		for {
			select {
			case <-sessionCtx.Done():
				// Closing the socket unblocks ReadMessage during shutdown.
				_ = conn.Close()
				return
			case <-ticker.C:
				timeMsg, err := EncodeClientTime(c.now())
				if err != nil || write(websocket.TextMessage, timeMsg) != nil {
					return
				}
			case state := <-c.stateUpdates:
				if write(websocket.TextMessage, state) != nil {
					return
				}
			}
		}
	}()
	defer func() {
		cancel()
		_ = conn.Close()
		<-tickerDone
	}()

	initial, err := c.InitialMessages()
	if err != nil {
		return err
	}
	for _, msg := range initial {
		if err := write(websocket.TextMessage, msg); err != nil {
			return err
		}
	}
	for {
		select {
		case <-ctx.Done():
			goodbye, _ := EncodeClientGoodbye("shutdown")
			_ = conn.WriteMessage(websocket.TextMessage, goodbye)
			return ctx.Err()
		default:
		}
		kind, raw, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		var outbound [][]byte
		if kind == websocket.TextMessage {
			outbound, err = c.HandleText(raw)
		} else if kind == websocket.BinaryMessage {
			err = c.HandleBinary(raw)
		}
		if err != nil {
			return err
		}
		for _, msg := range outbound {
			if err := write(websocket.TextMessage, msg); err != nil {
				return err
			}
		}
	}
}

// TimeSyncInterval is public so device wiring and tests share the same cadence.
const TimeSyncInterval = 5 * time.Second
