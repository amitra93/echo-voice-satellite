//go:build server

// Package opensl is the phase-0, runtime-loaded OpenSL ES wrapper. It is not
// used by the production server yet; afe_probe is its only caller.
package opensl

/*
#cgo CFLAGS: -O2
#cgo LDFLAGS: -ldl
#include <string.h>
#include "shim.h"
*/
import "C"

import (
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"unsafe"
)

var ErrClosed = errors.New("opensl: closed")

type Preset int

const (
	PresetMic Preset = iota
	PresetVoiceRecognition
	PresetVoiceCommunication
)

func (p Preset) String() string {
	switch p {
	case PresetMic:
		return "MIC"
	case PresetVoiceRecognition:
		return "VOICE_RECOGNITION"
	case PresetVoiceCommunication:
		return "VOICE_COMMUNICATION"
	default:
		return fmt.Sprintf("Preset(%d)", int(p))
	}
}

type Engine struct{ eng C.em_engine }

var (
	engineMu sync.Mutex
	engines  = map[string]*Engine{}
)

func Open(path string) (*Engine, error) {
	engineMu.Lock()
	defer engineMu.Unlock()
	if e := engines[path]; e != nil {
		return e, nil
	}
	cp := C.CString(path)
	defer C.free(unsafe.Pointer(cp))
	e := &Engine{}
	if err := goErr(C.em_sl_open(cp, &e.eng)); err != nil {
		return nil, fmt.Errorf("opensl: open %s: %w", path, err)
	}
	engines[path] = e
	return e, nil
}

func (e *Engine) Close() {
	if e == nil {
		return
	}
	engineMu.Lock()
	for path, current := range engines {
		if current == e {
			delete(engines, path)
		}
	}
	engineMu.Unlock()
	C.em_engine_close(&e.eng)
}

var (
	callbackMu sync.Mutex
	nextID     int64
	recorders  = map[int64]*Recorder{}
	players    = map[int64]*Player{}
)

func newHandle() int64 { return atomic.AddInt64(&nextID, 1) }

//export em_go_recorder_cb
func em_go_recorder_cb(ctx C.longlong) {
	callbackMu.Lock()
	r := recorders[int64(ctx)]
	callbackMu.Unlock()
	if r != nil {
		r.complete()
	}
}

//export em_go_player_cb
func em_go_player_cb(ctx C.longlong) {
	callbackMu.Lock()
	p := players[int64(ctx)]
	callbackMu.Unlock()
	if p != nil {
		p.complete()
	}
}

type Recorder struct {
	rec      C.em_recorder
	handle   int64
	bufBytes int
	inflight chan int
	frames   chan []byte
	drops    atomic.Uint64
	closed   atomic.Bool
}

func (e *Engine) NewRecorder(p Preset, rateHz, periodFrames, buffers int) (*Recorder, error) {
	if periodFrames <= 0 || buffers < 2 {
		return nil, fmt.Errorf("opensl: invalid recorder geometry: periodFrames=%d buffers=%d", periodFrames, buffers)
	}
	r := &Recorder{
		bufBytes: periodFrames * 2,
		inflight: make(chan int, buffers),
		frames:   make(chan []byte, buffers*4),
	}
	r.handle = newHandle()
	callbackMu.Lock()
	recorders[r.handle] = r
	callbackMu.Unlock()
	cp := C.em_recorder_open(&e.eng, C.int(p), C.int(rateHz), C.int(r.bufBytes), C.int(buffers), C.longlong(r.handle), &r.rec)
	if err := goErr(cp); err != nil {
		callbackMu.Lock()
		delete(recorders, r.handle)
		callbackMu.Unlock()
		return nil, fmt.Errorf("opensl: NewRecorder(%s): %w", p, err)
	}
	if err := goErr(C.em_recorder_prime(&r.rec)); err != nil {
		r.Close()
		return nil, fmt.Errorf("opensl: prime recorder: %w", err)
	}
	for i := 0; i < buffers; i++ {
		r.inflight <- i
	}
	return r, nil
}

func (r *Recorder) FrameSize() int { return r.bufBytes }
func (r *Recorder) Start() error   { return goErr(C.em_recorder_start(&r.rec)) }
func (r *Recorder) Stop() error    { return goErr(C.em_recorder_stop(&r.rec)) }

func (r *Recorder) Read() ([]byte, error) {
	b, ok := <-r.frames
	if !ok {
		return nil, ErrClosed
	}
	return b, nil
}

func (r *Recorder) Drops() uint64 { return r.drops.Load() }

func (r *Recorder) Close() {
	if r == nil || r.closed.Swap(true) {
		return
	}
	callbackMu.Lock()
	delete(recorders, r.handle)
	callbackMu.Unlock()
	C.em_recorder_close(&r.rec)
	close(r.frames)
}

func (r *Recorder) complete() {
	var idx int
	select {
	case idx = <-r.inflight:
	default:
		return
	}
	b := C.GoBytes(C.em_recorder_bufptr(&r.rec, C.int(idx)), C.int(r.bufBytes))
	select {
	case r.frames <- b:
	default:
		r.drops.Add(1)
	}
	if err := goErr(C.em_recorder_enqueue(&r.rec, C.int(idx))); err != nil {
		return
	}
	select {
	case r.inflight <- idx:
	default:
	}
}

type Player struct {
	play     C.em_player
	handle   int64
	bufBytes int
	free     chan int
	inflight chan int
	closed   atomic.Bool
}

func (e *Engine) NewPlayer(rateHz, maxBufBytes, buffers int) (*Player, error) {
	if maxBufBytes <= 0 || buffers < 2 {
		return nil, fmt.Errorf("opensl: invalid player geometry: maxBufBytes=%d buffers=%d", maxBufBytes, buffers)
	}
	p := &Player{bufBytes: maxBufBytes, free: make(chan int, buffers), inflight: make(chan int, buffers)}
	p.handle = newHandle()
	callbackMu.Lock()
	players[p.handle] = p
	callbackMu.Unlock()
	if err := goErr(C.em_player_open(&e.eng, C.int(rateHz), C.int(maxBufBytes), C.int(buffers), C.longlong(p.handle), &p.play)); err != nil {
		callbackMu.Lock()
		delete(players, p.handle)
		callbackMu.Unlock()
		return nil, fmt.Errorf("opensl: NewPlayer: %w", err)
	}
	for i := 0; i < buffers; i++ {
		p.free <- i
	}
	return p, nil
}

func (p *Player) MaxFrameBytes() int { return p.bufBytes }

func (p *Player) Write(data []byte) error {
	if len(data) == 0 {
		return nil
	}
	if len(data) > p.bufBytes {
		return fmt.Errorf("opensl: period of %d bytes exceeds %d-byte buffer", len(data), p.bufBytes)
	}
	idx, ok := <-p.free
	if !ok {
		return ErrClosed
	}
	C.memcpy(C.em_player_bufptr(&p.play, C.int(idx)), unsafe.Pointer(&data[0]), C.size_t(len(data)))
	if err := goErr(C.em_player_enqueue(&p.play, C.int(idx), C.int(len(data)))); err != nil {
		p.free <- idx
		return err
	}
	p.inflight <- idx
	return nil
}

func (p *Player) Clear() error { return goErr(C.em_player_clear(&p.play)) }
func (p *Player) Stop() error  { return goErr(C.em_player_stop(&p.play)) }
func (p *Player) Close() {
	if p == nil || p.closed.Swap(true) {
		return
	}
	callbackMu.Lock()
	delete(players, p.handle)
	callbackMu.Unlock()
	C.em_player_close(&p.play)
	close(p.free)
}

func (p *Player) complete() {
	select {
	case idx := <-p.inflight:
		select {
		case p.free <- idx:
		default:
		}
	default:
	}
}

func goErr(msg *C.char) error {
	if msg == nil {
		return nil
	}
	defer C.free(unsafe.Pointer(msg))
	return errors.New(C.GoString(msg))
}
