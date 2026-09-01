package shadow

import (
	"crypto/md5"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword/ort"
)

func ClassifierMD5(model string) string {
	b, err := os.ReadFile(filepath.Join(Dir(), ModelStem(model)+".onnx"))
	if err != nil {
		return ""
	}
	sum := md5.Sum(b)
	return hex.EncodeToString(sum[:])
}

// DefaultDir is where the ONNX Runtime library and the models live on the
// device. Overridable with EM_OWW_DIR, mainly so the same binary can be
// pointed at a scratch directory while iterating.
//
// The library is NOT part of the firmware: it is 12.3MB, it changes far less
// often than the binary, and embedding it would double both the OTA payload and
// the space taken by the A/B slots. It is installed out of band, and its
// absence is an ordinary, expected condition — see Open.
const DefaultDir = "/data/local/share/echomuse/oww"

// Dir returns the configured model directory.
func Dir() string {
	if d := os.Getenv("EM_OWW_DIR"); d != "" {
		return d
	}
	return DefaultDir
}

// ModelStem turns the controller's owwModel value into the classifier filename
// stem, mirroring em_oww_models.prediction_key EXACTLY.
//
// That value is a bare name for built-in models ("hey_mycroft_v0.1") and a file
// PATH for custom ones. Python's rule is: strip to the filename stem only when
// the value ends in ".onnx", otherwise pass it through untouched.
//
// The untouched case is the whole point, and getting it wrong is not
// hypothetical — an earlier version used filepath.Ext, which sees ".1" as the
// extension of "hey_mycroft_v0.1" and went looking for hey_mycroft_v0.onnx. A
// version suffix is not a file extension.
func ModelStem(owwModel string) string {
	name := strings.TrimSpace(owwModel)
	if !strings.HasSuffix(name, ".onnx") {
		return name
	}
	return strings.TrimSuffix(filepath.Base(name), ".onnx")
}

// Open loads ONNX Runtime and the three models for owwModel, and starts a
// Scorer.
//
// A missing library or model returns an error and is NOT a failure of the
// device: shadow mode is off until someone installs them, the caller logs it
// once and carries on with controller-side wake word. That is the whole reason
// the runtime is dlopen'd rather than linked.
func Open(owwModel string, threshold float32, onCross func(score, threshold float32, at time.Time, sequence uint16)) (*Scorer, error) {
	dir := Dir()
	stem := ModelStem(owwModel)
	if stem == "" {
		return nil, fmt.Errorf("shadow: no wake word model configured")
	}

	models := ort.Models{
		Melspec:    filepath.Join(dir, "melspectrogram.onnx"),
		Embedding:  filepath.Join(dir, "embedding_model.onnx"),
		Classifier: filepath.Join(dir, stem+".onnx"),
	}
	// Checked up front so the log names the missing file. ORT's own error for a
	// bad path mentions neither which of the three it was nor what was expected
	// there, and this is a path people will get wrong while installing by hand.
	for what, p := range map[string]string{
		"melspectrogram model": models.Melspec,
		"embedding model":      models.Embedding,
		"classifier model":     models.Classifier,
	} {
		if _, err := os.Stat(p); err != nil {
			return nil, fmt.Errorf("shadow: %s not installed at %s", what, p)
		}
	}

	lib := filepath.Join(dir, "libonnxruntime.so")
	rt, err := ort.Open(lib)
	if err != nil {
		return nil, fmt.Errorf("shadow: %w", err)
	}
	inf, err := rt.NewInferer(models, ort.DefaultOptions())
	if err != nil {
		return nil, fmt.Errorf("shadow: %w", err)
	}

	s := NewScorer(inf, threshold, onCross)
	s.closers = []io.Closer{inf}
	s.info = fmt.Sprintf("onnxruntime %s, model %s, xnnpack=%v",
		rt.Version(), stem, inf.XNNPACKActive())
	return s, nil
}

// OpenWithHead loads one shared feature pipeline plus a second classifier head.
// It is used when local wake and stop scoring are both enabled: loading a second
// full scorer would duplicate the expensive mel and embedding sessions.
func OpenWithHead(owwModel string, threshold float32, onCross func(score, threshold float32, at time.Time, sequence uint16), headModel string, headThreshold float32, enabled func() bool, onHeadCross func(score, threshold float32, at time.Time, sequence uint16)) (*Scorer, error) {
	dir := Dir()
	wakeStem, headStem := ModelStem(owwModel), ModelStem(headModel)
	if wakeStem == "" || headStem == "" {
		return nil, fmt.Errorf("shadow: wake and stop models must be configured")
	}
	models := ort.Models{Melspec: filepath.Join(dir, "melspectrogram.onnx"), Embedding: filepath.Join(dir, "embedding_model.onnx"), Classifier: filepath.Join(dir, wakeStem+".onnx")}
	for what, p := range map[string]string{"melspectrogram model": models.Melspec, "embedding model": models.Embedding, "wake classifier model": models.Classifier, "stop classifier model": filepath.Join(dir, headStem+".onnx")} {
		if _, err := os.Stat(p); err != nil {
			return nil, fmt.Errorf("shadow: %s not installed at %s", what, p)
		}
	}
	rt, err := ort.Open(filepath.Join(dir, "libonnxruntime.so"))
	if err != nil {
		return nil, fmt.Errorf("shadow: %w", err)
	}
	inf, err := rt.NewInferer(models, ort.DefaultOptions())
	if err != nil {
		return nil, fmt.Errorf("shadow: %w", err)
	}
	head, err := rt.NewClassifier(filepath.Join(dir, headStem+".onnx"), ort.DefaultOptions())
	if err != nil {
		_ = inf.Close()
		return nil, fmt.Errorf("shadow: %w", err)
	}
	s := NewScorerWithHeads(inf, threshold, onCross, Head{Classifier: head, Threshold: headThreshold, Enabled: enabled, OnCross: onHeadCross})
	s.closers = []io.Closer{inf, head}
	s.info = fmt.Sprintf("onnxruntime %s, wake %s + stop %s, xnnpack=%v", rt.Version(), wakeStem, headStem, inf.XNNPACKActive())
	return s, nil
}
