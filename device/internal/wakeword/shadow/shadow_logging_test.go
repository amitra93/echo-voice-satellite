package shadow

import (
	"bytes"
	"log"
	"testing"
	"time"
)

// fixedInferer returns controllable scores for testing the logging gate.
type fixedInferer struct {
	score float32
}

func (f *fixedInferer) Melspec(samples []float32) ([]float32, int, error) {
	frames := 8
	return make([]float32, frames*32), frames, nil
}
func (f *fixedInferer) Embed(window []float32) ([]float32, error) {
	return make([]float32, 96), nil
}
func (f *fixedInferer) Classify(feats []float32) (float32, error) {
	return f.score, nil
}

func TestShadowLogsOnlyAboveOnePercent(t *testing.T) {
	var buf bytes.Buffer
	orig := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(orig)

	inf := &fixedInferer{score: 0.005}
	s := NewScorer(inf, 0.5, nil)
	defer s.Close()

	// Warm up the detector (needs ~16 embeddings).
	frame := make([]int16, 1280)
	for i := 0; i < 20; i++ {
		s.Push(frame)
		time.Sleep(5 * time.Millisecond)
	}
	time.Sleep(100 * time.Millisecond)
	buf.Reset()

	// Score 0.005 should not log (below 0.01 gate).
	inf.score = 0.005
	s.Push(frame)
	time.Sleep(80 * time.Millisecond)
	if buf.Len() != 0 {
		t.Fatalf("expected no log for score 0.005, got %q", buf.String())
	}

	// Score 0.02 should log.
	inf.score = 0.02
	s.Push(frame)
	time.Sleep(80 * time.Millisecond)
	if !bytes.Contains(buf.Bytes(), []byte("[wake]")) {
		t.Fatalf("expected [wake] log for score 0.02, got %q", buf.String())
	}
	if !bytes.Contains(buf.Bytes(), []byte("0.0200")) {
		t.Fatalf("expected score 0.0200 in log, got %q", buf.String())
	}
	buf.Reset()

	// Score exactly 0.01 should not log (gate is >0.01).
	inf.score = 0.01
	s.Push(frame)
	time.Sleep(80 * time.Millisecond)
	if bytes.Contains(buf.Bytes(), []byte("[wake]")) {
		t.Fatalf("expected no log for score == 0.01, got %q", buf.String())
	}

	// Score 0.0101 should log.
	inf.score = 0.0101
	s.Push(frame)
	time.Sleep(80 * time.Millisecond)
	if !bytes.Contains(buf.Bytes(), []byte("[wake]")) {
		t.Fatalf("expected [wake] log for score 0.0101, got %q", buf.String())
	}
}

func TestStopHeadLogsOnlyAboveOnePercent(t *testing.T) {
	var buf bytes.Buffer
	orig := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(orig)

	headScore := float32(0.005)
	inf := &fixedInferer{score: 0.5}
	mockClassifier := &fixedClassifier{score: headScore}
	h := Head{
		Classifier: mockClassifier,
		Threshold:  0.5,
		Enabled:    func() bool { return true },
	}
	s := NewScorerWithHeads(inf, 0.5, nil, h)
	defer s.Close()

	frame := make([]int16, 1280)
	for i := 0; i < 20; i++ {
		s.Push(frame)
		time.Sleep(5 * time.Millisecond)
	}
	time.Sleep(100 * time.Millisecond)
	buf.Reset()

	// Head 0.005 should not log.
	mockClassifier.score = 0.005
	s.Push(frame)
	time.Sleep(80 * time.Millisecond)
	if bytes.Contains(buf.Bytes(), []byte("[stopword]")) {
		t.Fatalf("expected no [stopword] log for 0.005, got %q", buf.String())
	}
	buf.Reset()

	// Head 0.02 should log.
	mockClassifier.score = 0.02
	s.Push(frame)
	time.Sleep(80 * time.Millisecond)
	if !bytes.Contains(buf.Bytes(), []byte("[stopword]")) {
		t.Fatalf("expected [stopword] log for 0.02, got %q", buf.String())
	}
}

type fixedClassifier struct {
	score float32
}

func (m *fixedClassifier) Classify(feats []float32) (float32, error) {
	return m.score, nil
}
