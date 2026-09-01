// Package stopword owns the generation-tagged local stop arm. It deliberately
// contains no audio or transport code so stale and duplicate handling stays
// deterministic and host-testable.
package stopword

import (
	"errors"
	"sync"
	"time"
)

var ErrInvalidArm = errors.New("stopword: invalid arm")

type Arm struct {
	TurnID     string
	Generation uint64
	Phase      string
	ExpiresAt  time.Time
}

type Manager struct {
	mu         sync.Mutex
	current    *Arm
	generation uint64
}

func validPhase(phase string) bool {
	return phase == "thinking" || phase == "playback" || phase == "timer"
}

// Arm installs a newer live arm. Expiry is relative to the device monotonic
// clock, avoiding the Echo's unreliable wall clock.
func (m *Manager) Arm(turnID string, generation uint64, phase string, expiry time.Duration) error {
	if turnID == "" || generation == 0 || !validPhase(phase) || expiry <= 0 {
		return ErrInvalidArm
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if generation <= m.generation {
		return ErrInvalidArm
	}
	m.generation = generation
	m.current = &Arm{TurnID: turnID, Generation: generation, Phase: phase, ExpiresAt: time.Now().Add(expiry)}
	return nil
}

// Disarm accepts only the generation currently in force. An old controller
// message must not cancel a newer response's protection.
func (m *Manager) Disarm(generation uint64) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.current == nil || m.current.Generation != generation {
		return false
	}
	m.current = nil
	return true
}

// Active reports whether a non-expired arm exists. Expiry also disarms it so a
// later delayed crossing cannot be accepted.
func (m *Manager) Active() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.expireLocked(time.Now())
	return m.current != nil
}

// Accept atomically consumes the live arm. The caller owns the returned arm
// and can flush audio before sending its best-effort controller notification.
func (m *Manager) Accept() (Arm, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.expireLocked(time.Now())
	if m.current == nil {
		return Arm{}, false
	}
	arm := *m.current
	m.current = nil
	return arm, true
}

func (m *Manager) expireLocked(now time.Time) {
	if m.current != nil && !now.Before(m.current.ExpiresAt) {
		m.current = nil
	}
}
