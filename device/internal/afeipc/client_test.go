package afeipc

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"testing"
	"time"
)

func TestHelperCommandDefaultsToFirmwareBinary(t *testing.T) {
	cmd := helperCommand("")
	if got, want := cmd.Args, []string{"su", "system", "-c", "/data/local/bin/server --afe-helper"}; len(got) != len(want) {
		t.Fatalf("args = %#v, want %#v", got, want)
	} else {
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("args = %#v, want %#v", got, want)
			}
		}
	}
}

func TestHelperCommandHonorsOverride(t *testing.T) {
	cmd := helperCommand("/data/local/bin/test-helper --verbose")
	if got, want := cmd.Args[3], "/data/local/bin/test-helper --verbose"; got != want {
		t.Fatalf("command = %q, want %q", got, want)
	}
}

func TestClientRequestWrappersRoundTrip(t *testing.T) {
	requestReader, requestWriter := io.Pipe()
	responseReader, responseWriter := io.Pipe()
	c := &Client{
		in:      requestWriter,
		out:     responseReader,
		pending: make(map[uint32]chan callResult),
		done:    make(chan struct{}),
	}
	go c.readLoop()

	const requests = 8
	serverDone := make(chan error, 1)
	go func() {
		for i := 0; i < requests; i++ {
			request, err := ReadFrame(requestReader)
			if err != nil {
				serverDone <- err
				return
			}
			if request.RequestID != uint32(i+1) {
				serverDone <- &testError{message: "request IDs were not monotonic"}
				return
			}
			if err := (Frame{Type: Response, RequestID: request.RequestID, Payload: []byte("ok")}).WriteFrame(responseWriter); err != nil {
				serverDone <- err
				return
			}
		}
		responseWriter.Close()
		serverDone <- nil
	}()

	if err := c.Open(OpenOptions{Library: "libafe.so", Preset: 2, RecorderRate: 16000, RecorderPeriodFrames: 128, RecorderBuffers: 4, PlayerRate: 48000, PlayerBufferBytes: 4096, PlayerBuffers: 3}); err != nil {
		t.Fatal(err)
	}
	if err := c.StartRecorder(); err != nil {
		t.Fatal(err)
	}
	if err := c.StopRecorder(); err != nil {
		t.Fatal(err)
	}
	if payload, err := c.ReadRecorder(); err != nil || string(payload) != "ok" {
		t.Fatalf("ReadRecorder() = %q, %v", payload, err)
	}
	if err := c.WritePlayer([]byte{1, 2, 3}); err != nil {
		t.Fatal(err)
	}
	if err := c.ClearPlayer(); err != nil {
		t.Fatal(err)
	}
	if err := c.StopPlayer(); err != nil {
		t.Fatal(err)
	}
	if err := c.SetPlayerVolume(127); err != nil {
		t.Fatal(err)
	}

	select {
	case err := <-serverDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("helper did not process requests")
	}
	select {
	case <-c.done:
	case <-time.After(time.Second):
		t.Fatal("client did not stop after helper EOF")
	}
	requestWriter.Close()
}

func TestClientReadLoopDispatchesErrorsAndUnexpectedResponses(t *testing.T) {
	for _, tc := range []struct {
		name    string
		payload []byte
		typ     Type
		want    string
	}{
		{name: "named error", typ: Error, payload: mustJSON(t, map[string]string{"error": "helper failed"}), want: "helper failed"},
		{name: "unnamed error", typ: Error, payload: []byte("not json"), want: "afeipc: helper error"},
		{name: "unexpected type", typ: Status, payload: nil, want: "afeipc: unexpected response type 10"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var input bytes.Buffer
			if err := (Frame{Type: tc.typ, RequestID: 1, Payload: tc.payload}).WriteFrame(&input); err != nil {
				t.Fatal(err)
			}
			result := make(chan callResult, 1)
			c := &Client{out: io.NopCloser(bytes.NewReader(input.Bytes())), pending: map[uint32]chan callResult{1: result}, done: make(chan struct{})}
			c.readLoop()
			got := <-result
			if got.err == nil || got.err.Error() != tc.want {
				t.Fatalf("error = %v, want %q", got.err, tc.want)
			}
		})
	}
}

func TestClientCallRejectsClosedClient(t *testing.T) {
	c := &Client{closed: true, pending: make(map[uint32]chan callResult)}
	if _, err := c.call(Status, nil); err == nil || err.Error() != "afeipc: client closed" {
		t.Fatalf("closed call error = %v", err)
	}
	if err := c.Close(); err != nil {
		t.Fatalf("Close() on closed client = %v", err)
	}
}

func TestClientFinishIgnoresUnknownRequest(t *testing.T) {
	c := &Client{pending: make(map[uint32]chan callResult)}
	c.finish(42, callResult{payload: []byte("ignored")})
}

func TestClientCloseCompletesHelperRequest(t *testing.T) {
	requestReader, requestWriter := io.Pipe()
	responseReader, responseWriter := io.Pipe()
	c := &Client{
		cmd:     exec.Command("true"),
		in:      requestWriter,
		out:     responseReader,
		pending: make(map[uint32]chan callResult),
		done:    make(chan struct{}),
	}
	if err := c.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go c.readLoop()
	go func() {
		request, err := ReadFrame(requestReader)
		if err != nil {
			return
		}
		_ = (Frame{Type: Response, RequestID: request.RequestID}).WriteFrame(responseWriter)
	}()

	if err := c.Close(); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
}

func TestClientOpenCapturesHelperPid(t *testing.T) {
	requestReader, requestWriter := io.Pipe()
	responseReader, responseWriter := io.Pipe()
	c := &Client{
		in:      requestWriter,
		out:     responseReader,
		pending: make(map[uint32]chan callResult),
		done:    make(chan struct{}),
	}
	go c.readLoop()
	go func() {
		request, err := ReadFrame(requestReader)
		if err != nil {
			return
		}
		payload := mustJSON(t, map[string]any{"ok": true, "pid": 4242})
		_ = (Frame{Type: Response, RequestID: request.RequestID, Payload: payload}).WriteFrame(responseWriter)
	}()

	if err := c.Open(OpenOptions{}); err != nil {
		t.Fatal(err)
	}
	if c.pid != 4242 {
		t.Fatalf("pid = %d, want 4242", c.pid)
	}
}

// TestClientCloseKillsHelperThatNeverExits reproduces the fault that orphaned
// a real device's AFE helper for days: a helper stuck inside a blocked
// native call never answers the close request and never exits on EOF, so
// without a bound Close would hang forever right along with it — and the
// process would keep holding the audio session it never released. The real
// helper is never our child (su hands it to magiskd), so this stands in a
// real, killable OS process for "the pid Open reported" and checks Close
// reaches it directly rather than only touching its own cmd.Process.
func TestClientCloseKillsHelperThatNeverExits(t *testing.T) {
	old := closeTimeout
	closeTimeout = 50 * time.Millisecond
	t.Cleanup(func() { closeTimeout = old })

	requestReader, requestWriter := io.Pipe()
	responseReader, _ := io.Pipe() // never written to: the helper never answers and never EOFs
	cmd := exec.Command("sleep", "5")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	c := &Client{
		cmd:     cmd,
		in:      requestWriter,
		out:     responseReader,
		pending: make(map[uint32]chan callResult),
		done:    make(chan struct{}),
		pid:     cmd.Process.Pid,
	}
	go c.readLoop()
	go func() {
		_, _ = ReadFrame(requestReader) // drain the close request; never respond
	}()

	closeDone := make(chan error, 1)
	go func() { closeDone <- c.Close() }()
	select {
	case err := <-closeDone:
		if err == nil {
			t.Fatal("Close() = nil, want an error reporting the kill")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Close() did not return once the helper became unresponsive")
	}

	if cmd.ProcessState == nil || cmd.ProcessState.Success() {
		t.Fatalf("helper process state = %v, want killed", cmd.ProcessState)
	}
}

func TestStartReportsHelperLaunchFailure(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(dir+"/su", []byte("not executable"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	if _, err := Start("helper"); err == nil {
		t.Fatal("Start unexpectedly succeeded with an unexecutable helper launcher")
	}
}

func TestStartStartsHelperAndHandlesEarlyExit(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(dir+"/su", []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	c, err := Start("helper")
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-c.done:
	case <-time.After(time.Second):
		t.Fatal("client did not notice helper exit")
	}
	if err := c.cmd.Wait(); err != nil {
		t.Fatalf("helper wait = %v", err)
	}
}

func TestClientCallReturnsWriteFailure(t *testing.T) {
	c := &Client{
		in:      failingWriter{},
		pending: make(map[uint32]chan callResult),
		done:    make(chan struct{}),
	}
	if _, err := c.call(Status, nil); err == nil || err.Error() != "afeipc: write header: write failed" {
		t.Fatalf("write failure = %v, want write failed", err)
	}
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, &testError{message: "write failed"} }
func (failingWriter) Close() error              { return nil }

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	payload, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

type testError struct{ message string }

func (e *testError) Error() string { return e.message }
