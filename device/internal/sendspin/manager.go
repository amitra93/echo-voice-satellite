package sendspin

import (
	"context"
	"log"
	"strings"
	"sync"
	"time"
)

// Manager owns reconnecting one native player when controller config changes.
// Replacing an address cancels the old socket before dialing the new one, so a
// stale config cannot leave two MA players feeding the same speaker.
type Manager struct {
	client *Client
	mu     sync.Mutex
	url    string
	cancel context.CancelFunc
}

func NewManager(client *Client) *Manager { return &Manager{client: client} }

func (m *Manager) Configure(url string) {
	url = strings.TrimSpace(url)
	m.mu.Lock()
	if url == m.url {
		m.mu.Unlock()
		return
	}
	if m.cancel != nil {
		m.cancel()
		m.cancel = nil
	}
	m.url = url
	if url == "" {
		m.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	m.cancel = cancel
	m.mu.Unlock()
	go m.run(ctx, url)
}

func (m *Manager) Close() { m.Configure("") }

func (m *Manager) run(ctx context.Context, url string) {
	backoff := time.Second
	for ctx.Err() == nil {
		if err := m.client.Run(ctx, url); err != nil && ctx.Err() == nil {
			log.Printf("[sendspin] connection to %s ended: %v; retrying in %s", url, err, backoff)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		if backoff < 30*time.Second {
			backoff *= 2
		}
	}
}
