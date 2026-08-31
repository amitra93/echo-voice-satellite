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
	// Diagnostic for #<pending>: a device was found with its Sendspin
	// connection silently gone (no TCP socket to MA, no error logged, no
	// process restart) after running healthily for ~11 minutes. This log
	// answers the first question — was Configure ever called again with a
	// changed URL, tearing the session down on purpose — which the absence
	// of any other sendspin log line could not.
	log.Printf("[sendspin] manager: reconfigure %q -> %q", m.url, url)
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
		// Unconditional, unlike the line below it: this fires on EVERY
		// return from Run — including a nil error, which the loop would
		// otherwise treat as silent, and including a cancelled ctx, which
		// the existing log line deliberately suppresses. Between them they
		// account for every way this loop could stop mattering without
		// telling anyone.
		err := m.client.Run(ctx, url)
		log.Printf("[sendspin] manager: Run returned err=%v (ctx.Err=%v)", err, ctx.Err())
		if err != nil && ctx.Err() == nil {
			log.Printf("[sendspin] connection to %s ended: %v; retrying in %s", url, err, backoff)
		}
		select {
		case <-ctx.Done():
			log.Printf("[sendspin] manager: ctx cancelled, run loop exiting")
			return
		case <-time.After(backoff):
		}
		if backoff < 30*time.Second {
			backoff *= 2
		}
	}
	log.Printf("[sendspin] manager: run loop exiting, ctx already done at top of loop")
}
