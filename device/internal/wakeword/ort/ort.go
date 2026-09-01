// Package ort implements wakeword.Inferer with ONNX Runtime, so the streaming
// pipeline in the parent package can actually score audio on the device.
//
// The runtime is dlopen'd at RUNTIME (see shim.h) rather than linked, so this
// package builds and the firmware boots with no libonnxruntime.so present —
// Open just returns an error and the caller keeps using controller-side wake
// word. Only ONNX Runtime's MIT-licensed C header is vendored, in include/;
// the 12.3MB library is distributed separately.
//
// Feeding it: the parent package owns all buffering and calls the three
// methods here in a fixed order per 80ms chunk. Nothing in this package is
// safe for concurrent use — one Inferer belongs to one goroutine, matching
// wakeword.Detector's own contract. ORT sessions are not goroutine-safe with
// the single-threaded, spin-free options used here, and adding a mutex would
// invite exactly the concurrent use the design rules out.
//
// Configuration is not a matter of taste. DefaultOptions encodes what was
// measured on an Echo Dot Gen 2 (Cortex-A7 @ ~1GHz, one core typically
// online): one thread, XNNPACK, spinning off — 36.2% of one core against
// 243% for ORT's own defaults. See Options.
package ort

/*
#cgo CFLAGS: -I${SRCDIR}/include -O2
#cgo LDFLAGS: -ldl

#include "shim.h"
*/
import "C"

import (
	"errors"
	"fmt"
	"sync"
	"unsafe"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
)

// Tensor shapes, fixed by the openWakeWord model exports.
//
//	melspectrogram: [1, samples]        -> [1, 1, frames, 32]
//	embedding:      [1, 76, 32, 1]      -> [1, 1, 1, 96]
//	classifier:     [1, 16, 96]         -> [1, 1]
//
// Only the melspectrogram input is variable: the parent package feeds it the
// new chunk plus 480 samples of left context, or less on the very first chunk.

// Options controls session creation. The zero value is not useful — use
// DefaultOptions and adjust from there.
type Options struct {
	// Threads is the intra-op thread count. Keep at 1: the wake stream is
	// duty-cycled (29ms of work per 80ms frame), so extra threads cut latency
	// we do not need and raise total CPU, which we do (63% of a core at two
	// threads against 36% at one, measured).
	Threads int

	// XNNPACK requests the XNNPACK execution provider, a ~1.5x speedup with
	// bit-identical output. If the runtime lacks it the CPU provider is used
	// instead and XNNPACKActive reports false — a slower detector, not a
	// broken one.
	XNNPACK bool

	// AllowSpinning leaves ORT's thread-pool spin-wait enabled. It defaults
	// off and should stay off for anything duty-cycled: between inferences
	// the pool burns CPU waiting for work that arrives 60ms later.
	AllowSpinning bool
}

// DefaultOptions returns the configuration measured as optimal on the device.
func DefaultOptions() Options {
	return Options{Threads: 1, XNNPACK: true, AllowSpinning: false}
}

// Runtime is a dlopen'd ONNX Runtime library and the OrtEnv created against
// it, which owns the thread pools the sessions share.
type Runtime struct {
	rt      C.em_runtime
	version string
}

// Open loads libonnxruntime.so from libPath (an absolute path, or a soname to
// be resolved by the dynamic linker) and creates its environment.
//
// Opening the SAME path twice returns the same Runtime rather than a second
// environment with a second set of thread pools — on a device with one core
// typically online, duplicating those is pure cost. Different paths are
// independent, which is what keeps a failure to load one library from being
// reported as a bookkeeping error about another.
func Open(libPath string) (*Runtime, error) {
	openMu.Lock()
	defer openMu.Unlock()

	if r := runtimes[libPath]; r != nil {
		return r, nil
	}

	cPath := C.CString(libPath)
	defer C.free(unsafe.Pointer(cPath))

	r := &Runtime{}
	if err := goErr(C.em_ort_open(cPath, &r.rt)); err != nil {
		return nil, fmt.Errorf("ort: open %s: %w", libPath, err)
	}
	r.version = C.GoString(C.em_ort_version(&r.rt))
	runtimes[libPath] = r
	return r, nil
}

var (
	openMu   sync.Mutex
	runtimes = map[string]*Runtime{}
)

// Version reports the loaded library's version string, e.g. "1.19.2".
func (r *Runtime) Version() string { return r.version }

// Models names the three ONNX files the pipeline needs. Melspec and Embedding
// are openWakeWord's shared feature models; Classifier is the per-wake-word
// head, so it is the only one that changes when the wake word does.
type Models struct {
	Melspec    string
	Embedding  string
	Classifier string
}

type model struct {
	m    C.em_model
	name string

	// out is reused across calls: inference runs 12.5 times a second forever,
	// so the steady state must not allocate per frame. Each model has its own
	// buffer, which is what lets Melspec's result stay valid across an Embed
	// call — the parent package interleaves them.
	out []float32
}

func (r *Runtime) load(path, name string, o Options) (*model, error) {
	if path == "" {
		return nil, fmt.Errorf("ort: no path for the %s model", name)
	}
	cPath := C.CString(path)
	defer C.free(unsafe.Pointer(cPath))

	md := &model{name: name}
	err := goErr(C.em_model_load(&r.rt, cPath, C.int(o.Threads), cBool(o.XNNPACK),
		cBool(o.AllowSpinning), &md.m))
	if err != nil {
		return nil, fmt.Errorf("ort: load %s model %s: %w", name, path, err)
	}
	return md, nil
}

// Inferer runs the three models. It satisfies wakeword.Inferer.
type Inferer struct {
	mel, emb, cls *model

	xnnpack bool
}

// Classifier is an additional phrase-specific classifier head. It deliberately
// owns only its classifier session; melspectrogram and embedding sessions stay
// on the primary Inferer and are shared by every head.
type Classifier struct{ cls *model }

func (r *Runtime) NewClassifier(path string, o Options) (*Classifier, error) {
	if o.Threads < 1 {
		return nil, fmt.Errorf("ort: Threads must be at least 1, got %d", o.Threads)
	}
	m, err := r.load(path, "classifier", o)
	if err != nil {
		return nil, err
	}
	return &Classifier{cls: m}, nil
}

func (c *Classifier) Close() error {
	if c.cls != nil {
		C.em_model_free(&c.cls.m)
		c.cls = nil
	}
	return nil
}

func (c *Classifier) Classify(feats []float32) (float32, error) {
	if c.cls == nil {
		return 0, ErrClosed
	}
	if want := wakeword.FeatWindow * wakeword.FeatDim; len(feats) != want {
		return 0, fmt.Errorf("ort: classify wants %d values, got %d", want, len(feats))
	}
	shape := [3]C.int64_t{1, wakeword.FeatWindow, wakeword.FeatDim}
	out, err := runModel(c.cls, feats, shape[:])
	if err != nil {
		return 0, err
	}
	if len(out) != 1 {
		return 0, fmt.Errorf("ort: classify produced %d values, want 1", len(out))
	}
	return out[0], nil
}

// NewInferer opens the three sessions. The caller owns the result and must
// Close it; a partially-constructed Inferer is torn down on error rather than
// leaking sessions.
func (r *Runtime) NewInferer(m Models, o Options) (*Inferer, error) {
	if o.Threads < 1 {
		return nil, fmt.Errorf("ort: Threads must be at least 1, got %d", o.Threads)
	}
	inf := &Inferer{}
	var err error
	if inf.mel, err = r.load(m.Melspec, "melspectrogram", o); err != nil {
		return nil, err
	}
	// XNNPACK availability is a property of the runtime build, so whether it
	// attached to the first session settles it for all three.
	inf.xnnpack = inf.mel.m.xnnpack != 0
	if inf.emb, err = r.load(m.Embedding, "embedding", o); err != nil {
		inf.Close()
		return nil, err
	}
	if inf.cls, err = r.load(m.Classifier, "classifier", o); err != nil {
		inf.Close()
		return nil, err
	}
	return inf, nil
}

// XNNPACKActive reports whether the XNNPACK provider was attached. False means
// the CPU provider is in use: same numbers, roughly 1.5x the CPU.
func (i *Inferer) XNNPACKActive() bool { return i.xnnpack }

// Close releases the sessions. Safe to call on a partially built Inferer and
// safe to call twice.
func (i *Inferer) Close() error {
	for _, m := range []*model{i.mel, i.emb, i.cls} {
		if m != nil {
			C.em_model_free(&m.m)
		}
	}
	i.mel, i.emb, i.cls = nil, nil, nil
	return nil
}

// ErrClosed is returned by the inference methods after Close.
var ErrClosed = errors.New("ort: inferer is closed")

// Melspec computes mel frames for the given samples, which are raw int16
// magnitudes cast to float32 — NOT normalised to ±1. The parent package's
// melspec() is the only correct caller; see its comment for why.
//
// The returned slice aliases an internal buffer that the next Melspec call
// overwrites. That is safe for the parent package, which copies what it keeps
// into its mel ring before pushing more audio.
func (i *Inferer) Melspec(samples []float32) ([]float32, int, error) {
	if i.mel == nil {
		return nil, 0, ErrClosed
	}
	if len(samples) == 0 {
		return nil, 0, errors.New("ort: melspec called with no samples")
	}
	shape := [2]C.int64_t{1, C.int64_t(len(samples))}
	out, err := i.run(i.mel, samples, shape[:])
	if err != nil {
		return nil, 0, err
	}
	if len(out)%wakeword.MelBins != 0 {
		return nil, 0, fmt.Errorf("ort: melspec produced %d values, not a multiple of %d bins",
			len(out), wakeword.MelBins)
	}
	return out, len(out) / wakeword.MelBins, nil
}

// Embed runs the embedding model over one 76x32 window.
func (i *Inferer) Embed(window []float32) ([]float32, error) {
	if i.emb == nil {
		return nil, ErrClosed
	}
	if want := wakeword.MelWindow * wakeword.MelBins; len(window) != want {
		return nil, fmt.Errorf("ort: embed wants %d values, got %d", want, len(window))
	}
	shape := [4]C.int64_t{1, wakeword.MelWindow, wakeword.MelBins, 1}
	out, err := i.run(i.emb, window, shape[:])
	if err != nil {
		return nil, err
	}
	if len(out) != wakeword.FeatDim {
		return nil, fmt.Errorf("ort: embed produced %d values, want %d", len(out), wakeword.FeatDim)
	}
	return out, nil
}

// Classify runs the wake-word head over the 16x96 feature window and returns
// the score.
func (i *Inferer) Classify(feats []float32) (float32, error) {
	if i.cls == nil {
		return 0, ErrClosed
	}
	if want := wakeword.FeatWindow * wakeword.FeatDim; len(feats) != want {
		return 0, fmt.Errorf("ort: classify wants %d values, got %d", want, len(feats))
	}
	shape := [3]C.int64_t{1, wakeword.FeatWindow, wakeword.FeatDim}
	out, err := runModel(i.cls, feats, shape[:])
	if err != nil {
		return 0, err
	}
	if len(out) != 1 {
		return 0, fmt.Errorf("ort: classify produced %d values, want 1", len(out))
	}
	return out[0], nil
}

// run is the single path into C: it hands ORT a pointer to Go memory (read
// only for the duration of the call, which cgo permits for a slice of
// non-pointer data) and copies the malloc'd output into the model's reusable
// buffer before returning, so no C allocation outlives this function and the
// steady state does not allocate.
func (i *Inferer) run(m *model, in []float32, shape []C.int64_t) ([]float32, error) {
	return runModel(m, in, shape)
}

func runModel(m *model, in []float32, shape []C.int64_t) ([]float32, error) {
	var (
		outPtr *C.float
		outN   C.size_t
	)
	err := goErr(C.em_model_run(&m.m,
		(*C.float)(unsafe.Pointer(&in[0])), C.size_t(len(in)),
		&shape[0], C.size_t(len(shape)),
		&outPtr, &outN))
	if err != nil {
		return nil, fmt.Errorf("ort: run %s: %w", m.name, err)
	}
	if outPtr == nil {
		return nil, fmt.Errorf("ort: run %s: no output", m.name)
	}
	defer C.free(unsafe.Pointer(outPtr))
	m.out = append(m.out[:0], unsafe.Slice((*float32)(unsafe.Pointer(outPtr)), int(outN))...)
	return m.out, nil
}

// goErr converts the shim's malloc'd char* convention into a Go error, freeing
// the message. NULL means success.
func goErr(msg *C.char) error {
	if msg == nil {
		return nil
	}
	defer C.free(unsafe.Pointer(msg))
	return errors.New(C.GoString(msg))
}

func cBool(b bool) C.int {
	if b {
		return 1
	}
	return 0
}

// Compile-time proof that this package satisfies the interface the streaming
// pipeline is written against.
var _ wakeword.Inferer = (*Inferer)(nil)
var _ wakeword.Classifier = (*Classifier)(nil)
