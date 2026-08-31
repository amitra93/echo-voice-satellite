//go:build server

package afeipc

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"strings"
	"sync"

	"github.com/wilbowes/EchoMuse/internal/opensl"
)

// RunHelper serves the OpenSL IPC protocol as the system-UID helper mode of
// the firmware binary. The root daemon never enters this path.
func RunHelper(defaultLib string) error {
	if os.Getuid() != 1000 { // Android's well-known system UID.
		return fmt.Errorf("afeipc: refusing to run as UID %d; launch with su system -c", os.Getuid())
	}
	if defaultLib == "" {
		defaultLib = "libOpenSLES.so"
	}
	return runHelper(os.Stdin, os.Stdout, os.Stderr, defaultLib)
}

type helper struct {
	engine      *opensl.Engine
	recorder    *opensl.Recorder
	player      *opensl.Player
	musicVol    int
	volumeDirty bool
	volumeMu    sync.Mutex
	responses   sync.Mutex
	requests    sync.WaitGroup
	out         io.Writer
}

type openRequest struct {
	Library        string `json:"library"`
	Preset         int    `json:"preset"`
	RecorderRate   int    `json:"recorder_rate"`
	RecorderPeriod int    `json:"recorder_period_frames"`
	RecorderBufs   int    `json:"recorder_buffers"`
	PlayerRate     int    `json:"player_rate"`
	PlayerBytes    int    `json:"player_buffer_bytes"`
	PlayerBufs     int    `json:"player_buffers"`
}

func runHelper(in io.Reader, out, logOutput io.Writer, defaultLib string) error {
	logger := log.New(logOutput, "", log.LstdFlags)
	h := &helper{out: out}
	defer h.close()
	for {
		request, err := ReadFrame(in)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			logger.Printf("protocol failure: %v", err)
			break
		}
		h.requests.Add(1)
		go func(request Frame) {
			defer h.requests.Done()
			if err := h.handle(request, defaultLib); err != nil {
				_ = h.writeError(request.RequestID, err)
			}
		}(request)
	}
	h.requests.Wait()
	return nil
}

func (h *helper) writeJSON(id uint32, typ Type, value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	h.responses.Lock()
	defer h.responses.Unlock()
	return (Frame{Type: typ, RequestID: id, Payload: payload}).WriteFrame(h.out)
}

func (h *helper) writeError(id uint32, err error) error {
	return h.writeJSON(id, Error, map[string]string{"error": err.Error()})
}

func (h *helper) handle(request Frame, defaultLib string) error {
	switch request.Type {
	case Open:
		var in openRequest
		if err := json.Unmarshal(request.Payload, &in); err != nil {
			return fmt.Errorf("open request: %w", err)
		}
		if in.Library == "" {
			in.Library = defaultLib
		}
		if h.engine != nil {
			return errors.New("already open")
		}
		engine, err := opensl.Open(in.Library)
		if err != nil {
			return err
		}
		recorder, err := engine.NewRecorder(opensl.Preset(in.Preset), in.RecorderRate, in.RecorderPeriod, in.RecorderBufs)
		if err != nil {
			engine.Close()
			return fmt.Errorf("recorder: %w", err)
		}
		player, err := engine.NewPlayer(in.PlayerRate, in.PlayerBytes, in.PlayerBufs)
		if err != nil {
			recorder.Close()
			engine.Close()
			return fmt.Errorf("player: %w", err)
		}
		h.engine, h.recorder, h.player = engine, recorder, player
		return h.writeJSON(request.RequestID, Response, map[string]any{"ok": true})
	case StartRecorder:
		if h.recorder == nil {
			return errors.New("recorder is not open")
		}
		if err := h.recorder.Start(); err != nil {
			return err
		}
		return h.writeJSON(request.RequestID, Response, map[string]any{"ok": true})
	case StopRecorder:
		if h.recorder == nil {
			return errors.New("recorder is not open")
		}
		if err := h.recorder.Stop(); err != nil {
			return err
		}
		return h.writeJSON(request.RequestID, Response, map[string]any{"ok": true})
	case ReadRecorder:
		if h.recorder == nil {
			return errors.New("recorder is not open")
		}
		data, err := h.recorder.Read()
		if err != nil {
			return err
		}
		h.responses.Lock()
		defer h.responses.Unlock()
		return (Frame{Type: Response, RequestID: request.RequestID, Payload: data}).WriteFrame(h.out)
	case WritePlayer:
		if h.player == nil {
			return errors.New("player is not open")
		}
		if err := h.player.Write(request.Payload); err != nil {
			return err
		}
		// AudioPolicy may not have selected the speaker route when the boot-time
		// volume restore runs. Reapply after the first real buffer reaches the
		// OpenSL player, when STREAM_MUSIC has an active route.
		h.volumeMu.Lock()
		if h.volumeDirty {
			if err := setMusicStreamVolume(h.musicVol); err != nil {
				h.volumeMu.Unlock()
				return err
			}
			h.volumeDirty = false
		}
		h.volumeMu.Unlock()
		return h.writeJSON(request.RequestID, Response, map[string]any{"ok": true})
	case ClearPlayer:
		if h.player == nil {
			return errors.New("player is not open")
		}
		if err := h.player.Clear(); err != nil {
			return err
		}
		return h.writeJSON(request.RequestID, Response, map[string]any{"ok": true})
	case StopPlayer:
		if h.player == nil {
			return errors.New("player is not open")
		}
		if err := h.player.Stop(); err != nil {
			return err
		}
		return h.writeJSON(request.RequestID, Response, map[string]any{"ok": true})
	case SetPlayerVolume:
		if h.player == nil {
			return errors.New("player is not open")
		}
		if len(request.Payload) != 4 {
			return fmt.Errorf("player volume payload is %d bytes, want 4", len(request.Payload))
		}
		level := int(binary.BigEndian.Uint32(request.Payload))
		h.volumeMu.Lock()
		h.musicVol = level
		h.volumeDirty = true
		if err := setMusicStreamVolume(level); err != nil {
			h.volumeMu.Unlock()
			return err
		}
		h.volumeMu.Unlock()
		return h.writeJSON(request.RequestID, Response, map[string]any{"ok": true})
	case Status:
		return h.writeJSON(request.RequestID, Response, map[string]any{"recorder": h.recorder != nil, "player": h.player != nil})
	case Close:
		return h.writeJSON(request.RequestID, Response, map[string]any{"ok": true})
	default:
		return fmt.Errorf("unknown request type %d", request.Type)
	}
}

// Android's OpenSL player is mixed as STREAM_MUSIC. Its per-track volume can
// be 0dB while AudioPolicy still attenuates the route (the Echo's default is
// index 21/30, about -4dB). The system-UID helper is the only existing EchoMuse
// process allowed to make this framework call. Transaction 4 is
// IAudioService.setStreamVolume on FireOS 5.1; the package name must be the
// system package, not com.android.shell, or AudioService rejects it.
func setMusicStreamVolume(level int) error {
	index := musicStreamVolumeIndex(level)
	out, err := exec.Command("service", "call", "audio", "4",
		"i32", "3", "i32", fmt.Sprint(index), "i32", "0", "s16", "android").CombinedOutput()
	if err != nil {
		return fmt.Errorf("set STREAM_MUSIC volume index %d: %w (%s)", index, err, strings.TrimSpace(string(out)))
	}
	text := string(out)
	if strings.Contains(text, "not allowed") || strings.Contains(text, "Exception") {
		return fmt.Errorf("set STREAM_MUSIC volume index %d rejected: %s", index, strings.TrimSpace(text))
	}
	return nil
}

func (h *helper) close() {
	if h.player != nil {
		h.player.Close()
		h.player = nil
	}
	if h.recorder != nil {
		h.recorder.Close()
		h.recorder = nil
	}
	if h.engine != nil {
		h.engine.Close()
		h.engine = nil
	}
}
