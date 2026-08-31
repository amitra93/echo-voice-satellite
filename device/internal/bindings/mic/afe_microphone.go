//go:build server

package mic

import (
	"context"
	"errors"
	"sync"

	"github.com/wilbowes/EchoMuse/internal/afeipc"
	pkgmic "github.com/wilbowes/EchoMuse/pkg/mic"
)

const afeSubscribers = 64

// AFEMicrophone receives the mono S16 stream produced by Amazon's
// VOICE_RECOGNITION pipeline. The helper owns the Android permission boundary.
type AFEMicrophone struct {
	c    *afeipc.Client
	mu   sync.Mutex
	subs []chan []byte
	stop chan struct{}
}

func NewAFEMicrophone(c *afeipc.Client) *AFEMicrophone {
	return &AFEMicrophone{c: c, stop: make(chan struct{})}
}

func (m *AFEMicrophone) Init() error {
	if err := m.c.StartRecorder(); err != nil {
		return err
	}
	go m.readLoop()
	return nil
}

func (m *AFEMicrophone) Subscribe() chan []byte {
	ch := make(chan []byte, afeSubscribers)
	m.mu.Lock()
	m.subs = append(m.subs, ch)
	m.mu.Unlock()
	return ch
}

func (m *AFEMicrophone) Unsubscribe(ch chan []byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for i, current := range m.subs {
		if current == ch {
			m.subs = append(m.subs[:i], m.subs[i+1:]...)
			close(ch)
			return
		}
	}
}

func (m *AFEMicrophone) readLoop() {
	defer func() {
		m.mu.Lock()
		for _, ch := range m.subs {
			close(ch)
		}
		m.subs = nil
		m.mu.Unlock()
	}()
	for {
		select {
		case <-m.stop:
			return
		default:
		}
		frame, err := m.c.ReadRecorder()
		if err != nil {
			return
		}
		m.mu.Lock()
		for _, ch := range m.subs {
			copyFrame := append([]byte(nil), frame...)
			select {
			case ch <- copyFrame:
			default:
			}
		}
		m.mu.Unlock()
	}
}

func (m *AFEMicrophone) Listen(callback pkgmic.AudioCallback, ctx context.Context) error {
	if callback == nil {
		return errors.New("callback can't be nil")
	}
	ch := m.Subscribe()
	defer m.Unsubscribe(ch)
	for {
		select {
		case <-ctx.Done():
			return nil
		case frame, ok := <-ch:
			if !ok {
				return nil
			}
			callback(frame)
		}
	}
}

func (m *AFEMicrophone) Close() {
	select {
	case <-m.stop:
	default:
		close(m.stop)
	}
	_ = m.c.StopRecorder()
}
