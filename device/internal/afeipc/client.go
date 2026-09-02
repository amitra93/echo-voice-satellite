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
	"sync"
	"syscall"
	"time"
)

// closeTimeout bounds how long Close waits for the helper to answer the
// close request and then to actually exit, before giving up and killing it.
// A var, not a const, so a test can shrink it rather than spend real
// wall-clock seconds proving the timeout fires.
var closeTimeout = 3 * time.Second

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
	pid     int // helper's own kernel pid, reported in the Open response; 0 until known
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
	response, err := c.call(Open, payload)
	if err != nil {
		return err
	}
	var opened struct {
		Pid int `json:"pid"`
	}
	// A missing or unparsable pid just leaves it 0 (unknown) — Close falls
	// back to killing our local process handle in that case, the same as
	// before this field existed.
	if json.Unmarshal(response, &opened) == nil && opened.Pid > 0 {
		c.mu.Lock()
		c.pid = opened.Pid
		c.mu.Unlock()
	}
	return nil
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

// Close asks the helper to shut down, then waits for it to actually exit.
// Both waits are bounded: ReadRecorder blocks inside a native call for as
// long as the process runs, dispatched on its own goroutine per request (see
// helper.go), so a recorder that never received real audio — the AudioFlinger
// session held by an unrelated stuck process, the case this was written
// after — leaves that goroutine blocked forever. That in turn blocks
// runHelper's h.requests.Wait(), so the helper never reaches its own
// deferred cleanup and never exits on EOF: without a bound here, Close would
// hang forever right along with it, and so would the caller's shutdown path.
// A helper that misses the deadline is killed outright instead.
func (c *Client) Close() error {
	c.mu.Lock()
	already := c.closed
	pid := c.pid
	c.mu.Unlock()
	if already {
		return nil
	}

	callDone := make(chan struct{})
	go func() {
		_, _ = c.call(Close, nil)
		close(callDone)
	}()
	select {
	case <-callDone:
	case <-time.After(closeTimeout):
		log.Printf("afeipc: helper did not answer close within %s — closing anyway", closeTimeout)
	}

	c.mu.Lock()
	if !c.closed {
		c.closed = true
		close(c.done)
	}
	c.mu.Unlock()
	_ = c.in.Close()
	_ = c.out.Close()

	waitDone := make(chan error, 1)
	go func() { waitDone <- c.cmd.Wait() }()
	select {
	case err := <-waitDone:
		return err
	case <-time.After(closeTimeout):
	}

	// su hands the real helper to magiskd rather than keeping it as our
	// child, so cmd.Process is only the local su stub — killing it is not
	// guaranteed to reach the actual process holding the audio session, and
	// that ambiguity is exactly how one was left running for days. The pid
	// the helper reported at Open can be killed directly because the daemon
	// runs as root, regardless of how su and magiskd relayed it.
	log.Printf("afeipc: helper did not exit within %s after close — killing it (pid=%d)", closeTimeout, pid)
	if pid > 0 {
		_ = syscall.Kill(pid, syscall.SIGKILL)
	}
	if c.cmd.Process != nil {
		_ = c.cmd.Process.Kill()
	}
	select {
	case err := <-waitDone:
		return err
	case <-time.After(closeTimeout):
		return fmt.Errorf("afeipc: helper did not exit even after SIGKILL")
	}
}
