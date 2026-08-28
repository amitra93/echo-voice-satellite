package client

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// fanoutMic is a minimal AFE-like mono mic.Subscribable.
type fanoutMic struct {
	mu     sync.Mutex
	subs   []chan []byte
	stopCh chan struct{}
}

func newFanoutMic() *fanoutMic {
	m := &fanoutMic{stopCh: make(chan struct{})}
	raw := make([]byte, 1280*2)
	for i := range raw {
		raw[i] = byte(i % 251)
	}
	go func() {
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-m.stopCh:
				return
			case <-ticker.C:
				m.mu.Lock()
				for _, ch := range m.subs {
					select {
					case ch <- raw:
					default:
					}
				}
				m.mu.Unlock()
			}
		}
	}()
	return m
}

func (m *fanoutMic) Subscribe() chan []byte {
	ch := make(chan []byte, 32)
	m.mu.Lock()
	m.subs = append(m.subs, ch)
	m.mu.Unlock()
	return ch
}

func (m *fanoutMic) Unsubscribe(ch chan []byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for i, s := range m.subs {
		if s == ch {
			m.subs = append(m.subs[:i], m.subs[i+1:]...)
			close(ch)
			return
		}
	}
}

func (m *fanoutMic) close() { close(m.stopCh) }

// dialTestWS stands up a WebSocket sink and returns a client conn to it.
func dialTestWS(t *testing.T) (*websocket.Conn, func()) {
	t.Helper()
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		for {
			if _, _, err := c.ReadMessage(); err != nil {
				return
			}
		}
	}))
	url := "ws://" + strings.TrimPrefix(srv.URL, "http://")
	conn, _, err := websocket.DefaultDialer.Dial(url, nil)
	if err != nil {
		srv.Close()
		t.Fatalf("dial test ws: %v", err)
	}
	return conn, func() {
		conn.Close()
		srv.Close()
	}
}

// TestStreamRestartOverlapIsRaceFree drives the StopMic/StartMic sequence the
// controller sends after voice turns while AFE mic data is flowing.
func TestStreamRestartOverlapIsRaceFree(t *testing.T) {
	mic := newFanoutMic()
	defer mic.close()
	conn, cleanup := dialTestWS(t)
	defer cleanup()

	d := NewDataClient("race-test", mic, nil)
	d.connMu.Lock()
	d.conn = conn
	d.connMu.Unlock()

	for i := 0; i < 100; i++ {
		lockMic := i%2 == 0 // alternate turn stream / wake stream
		d.StartMic(lockMic)
		time.Sleep(2 * time.Millisecond) // let a couple of periods flow
		d.StopMic()
		// No settling delay: the replacement StartMic in the next iteration
		// racing the superseded goroutine's drain is the scenario under test.
	}

	d.StopMic()
	// Give lingering goroutines time to exit so their deferred cleanup runs
	// (and the race detector observes it) before the test tears down.
	time.Sleep(100 * time.Millisecond)
}

// TestContextCancelReleasesMicStream reproduces the Office zombie-stream
// incident (2026-07-16): the control client cancels the data context on a
// control-WS reconnect while the data TCP path is still healthy. Before the
// ctx watcher in connect(), cancellation did nothing to an established
// connection — the old streamMic kept micActive forever and every
// mic_start on the replacement connection was refused ("already active"),
// leaving the device deaf to wake words. The fix must (a) close the
// connection so connect() returns promptly, and (b) release the mic stream
// so a StartMic against a new connection succeeds.
func TestContextCancelReleasesMicStream(t *testing.T) {
	mic := newFanoutMic()
	defer mic.close()

	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		for {
			if _, _, err := c.ReadMessage(); err != nil {
				return
			}
		}
	}))
	defer srv.Close()
	// connect expects a full base URL ("ws://host:port") — a bare host:port
	// makes the dial fail instantly with a malformed-URL error, and this
	// test's poll loop would eat that silently until its deadline.
	addr := "ws://" + strings.TrimPrefix(srv.URL, "http://")

	d := NewDataClient("zombie-test", mic, nil)

	ctx, cancel := context.WithCancel(context.Background())
	connectDone := make(chan error, 1)
	go func() { connectDone <- d.connect(ctx, addr) }()

	// Wait for connect to publish the conn, then start the wake stream on it.
	// Generous deadline: cold CI runners have missed 2s.
	deadline := time.Now().Add(10 * time.Second)
	for {
		d.connMu.Lock()
		ready := d.conn != nil
		d.connMu.Unlock()
		if ready {
			break
		}
		select {
		case err := <-connectDone:
			t.Fatalf("connect returned before publishing conn: %v", err)
		default:
		}
		if time.Now().After(deadline) {
			t.Fatal("connect never published conn")
		}
		time.Sleep(5 * time.Millisecond)
	}
	d.StartMic(false)

	// The control reconnect path: cancel the data context. connect() must
	// return promptly (not wait out a read deadline) and release the stream.
	cancel()
	select {
	case <-connectDone:
	case <-time.After(3 * time.Second):
		t.Fatal("connect did not return after context cancellation — established conn not torn down")
	}

	deadline = time.Now().Add(2 * time.Second)
	for {
		d.micMu.Lock()
		active := d.micActive
		d.micMu.Unlock()
		if !active {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("mic stream still active after cancelled connection exited — zombie stream holds micActive")
		}
		time.Sleep(5 * time.Millisecond)
	}

	// A replacement connection's StartMic must now succeed.
	conn2, cleanup2 := dialTestWS(t)
	defer cleanup2()
	d.connMu.Lock()
	d.conn = conn2
	d.connMu.Unlock()
	d.StartMic(false)
	d.micMu.Lock()
	restarted := d.micActive
	d.micMu.Unlock()
	if !restarted {
		t.Fatal("StartMic on replacement connection refused")
	}
	d.StopMic()
	time.Sleep(100 * time.Millisecond)
}

func TestVADPeriodRMSAndNoSpeechTimeoutOverride(t *testing.T) {
	if got := vadPeriodRMS(nil); got != 0 {
		t.Fatalf("empty RMS = %v", got)
	}
	pcm := []byte{0, 0, 0, 0, 0, 64, 0, 192} // 0, 0, 0.5, -0.5
	if got := vadPeriodRMS(pcm); got < 0.35 || got > 0.36 {
		t.Fatalf("RMS = %v", got)
	}
	old := noSpeechTimeoutForTest
	t.Cleanup(func() { noSpeechTimeoutForTest = old })
	noSpeechTimeoutForTest = 17 * time.Millisecond
	if got := effectiveNoSpeechTimeout(); got != 17*time.Millisecond {
		t.Fatalf("override = %v", got)
	}
	noSpeechTimeoutForTest = 0
	if got := effectiveNoSpeechTimeout(); got != noSpeechTimeout {
		t.Fatalf("default = %v", got)
	}
}
