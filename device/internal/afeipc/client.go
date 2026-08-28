package afeipc

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"
)

// Client multiplexes requests over the helper's ordered byte stream. Capture
// reads and player writes must overlap or the 80ms capture cadence would
// underfeed the 48kHz player.
type Client struct {
	cmd     *exec.Cmd
	in      io.WriteCloser
	out     io.ReadCloser
	writeMu sync.Mutex
	mu      sync.Mutex
	n       uint32
	closed  bool
	pending map[uint32]chan callResult
	done    chan struct{}
}
type callResult struct {
	payload []byte
	err     error
}

type OpenOptions struct {
	Library, Helper                                     string
	Preset                                              int
	RecorderRate, RecorderPeriodFrames, RecorderBuffers int
	PlayerRate, PlayerBufferBytes, PlayerBuffers        int
}

func Start(helper string) (*Client, error) {
	cmd := helperCommand(helper)
	cmd.Stderr = os.Stderr
	in, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("afeipc: helper stdin: %w", err)
	}
	out, err := cmd.StdoutPipe()
	if err != nil {
		in.Close()
		return nil, fmt.Errorf("afeipc: helper stdout: %w", err)
	}
	if err := cmd.Start(); err != nil {
		in.Close()
		out.Close()
		return nil, fmt.Errorf("afeipc: start helper: %w", err)
	}
	c := &Client{cmd: cmd, in: in, out: out, pending: make(map[uint32]chan callResult), done: make(chan struct{})}
	go c.readLoop()
	return c, nil
}

const defaultHelperCommand = "/data/local/bin/server --afe-helper"

func helperCommand(helper string) *exec.Cmd {
	if helper == "" {
		helper = defaultHelperCommand
	}
	return exec.Command("su", "system", "-c", helper)
}

func (c *Client) Open(o OpenOptions) error {
	payload, err := json.Marshal(map[string]any{"library": o.Library, "preset": o.Preset,
		"recorder_rate": o.RecorderRate, "recorder_period_frames": o.RecorderPeriodFrames,
		"recorder_buffers": o.RecorderBuffers, "player_rate": o.PlayerRate,
		"player_buffer_bytes": o.PlayerBufferBytes, "player_buffers": o.PlayerBuffers})
	if err != nil {
		return err
	}
	_, err = c.call(Open, payload)
	return err
}
func (c *Client) StartRecorder() error          { _, err := c.call(StartRecorder, nil); return err }
func (c *Client) StopRecorder() error           { _, err := c.call(StopRecorder, nil); return err }
func (c *Client) ReadRecorder() ([]byte, error) { return c.call(ReadRecorder, nil) }
func (c *Client) WritePlayer(p []byte) error    { _, err := c.call(WritePlayer, p); return err }
func (c *Client) ClearPlayer() error            { _, err := c.call(ClearPlayer, nil); return err }
func (c *Client) StopPlayer() error             { _, err := c.call(StopPlayer, nil); return err }
func (c *Client) SetPlayerVolume(level int) error {
	p := make([]byte, 4)
	// Send the EchoMuse level, not the derived millibel value: the helper also
	// has to keep Android's STREAM_MUSIC policy index in step with the player.
	binary.BigEndian.PutUint32(p, uint32(level))
	_, err := c.call(SetPlayerVolume, p)
	return err
}

func (c *Client) call(typ Type, payload []byte) ([]byte, error) {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return nil, errors.New("afeipc: client closed")
	}
	c.n++
	id := c.n
	result := make(chan callResult, 1)
	c.pending[id] = result
	c.mu.Unlock()
	c.writeMu.Lock()
	err := (Frame{Type: typ, RequestID: id, Payload: payload}).WriteFrame(c.in)
	c.writeMu.Unlock()
	if err != nil {
		c.finish(id, callResult{err: err})
	}
	r := <-result
	return r.payload, r.err
}

func (c *Client) finish(id uint32, result callResult) {
	c.mu.Lock()
	if ch := c.pending[id]; ch != nil {
		delete(c.pending, id)
		ch <- result
	}
	c.mu.Unlock()
}

func (c *Client) readLoop() {
	for {
		frame, err := ReadFrame(c.out)
		if err != nil {
			c.mu.Lock()
			if !c.closed {
				c.closed = true
				close(c.done)
			}
			for id, ch := range c.pending {
				delete(c.pending, id)
				ch <- callResult{err: err}
			}
			c.mu.Unlock()
			return
		}
		if frame.Type == Error {
			var response struct {
				Error string `json:"error"`
			}
			msg := "afeipc: helper error"
			if json.Unmarshal(frame.Payload, &response) == nil && response.Error != "" {
				msg = response.Error
			}
			c.finish(frame.RequestID, callResult{err: errors.New(msg)})
		} else if frame.Type == Response {
			c.finish(frame.RequestID, callResult{payload: frame.Payload})
		} else {
			c.finish(frame.RequestID, callResult{err: fmt.Errorf("afeipc: unexpected response type %d", frame.Type)})
		}
	}
}

func (c *Client) Close() error {
	c.mu.Lock()
	already := c.closed
	c.mu.Unlock()
	if already {
		return nil
	}
	_, _ = c.call(Close, nil)
	c.mu.Lock()
	if !c.closed {
		c.closed = true
		close(c.done)
	}
	c.mu.Unlock()
	_ = c.in.Close()
	_ = c.out.Close()
	return c.cmd.Wait()
}
