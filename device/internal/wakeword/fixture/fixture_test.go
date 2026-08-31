package fixture

import (
	"encoding/binary"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
)

func appendU32(dst []byte, value int) []byte {
	var b [4]byte
	binary.LittleEndian.PutUint32(b[:], uint32(value))
	return append(dst, b[:]...)
}

func appendI16(dst []byte, value int16) []byte {
	var b [2]byte
	binary.LittleEndian.PutUint16(b[:], uint16(value))
	return append(dst, b[:]...)
}

func appendF32s(dst []byte, values ...float32) []byte {
	var b [4]byte
	for _, value := range values {
		binary.LittleEndian.PutUint32(b[:], math.Float32bits(value))
		dst = append(dst, b[:]...)
	}
	return dst
}

func validFixtureBytes(samples, records, frames int) []byte {
	raw := []byte(ortMagic)
	raw = appendU32(raw, samples)
	for i := 0; i < samples; i++ {
		raw = appendI16(raw, int16(i))
	}
	raw = appendU32(raw, records)
	for i := 0; i < records; i++ {
		raw = appendU32(raw, wakeword.ChunkSamples)
		raw = appendU32(raw, frames)
		for j := 0; j < frames*wakeword.MelBins; j++ {
			raw = appendF32s(raw, float32(j))
		}
		for j := 0; j < wakeword.FeatDim; j++ {
			raw = appendF32s(raw, float32(j)/10)
		}
		raw = appendF32s(raw, 0.25)
	}
	for j := 0; j < wakeword.MelWindow*wakeword.MelBins; j++ {
		raw = appendF32s(raw, 1)
	}
	for j := 0; j < wakeword.FeatDim; j++ {
		raw = appendF32s(raw, 2)
	}
	for j := 0; j < wakeword.FeatWindow*wakeword.FeatDim; j++ {
		raw = appendF32s(raw, 3)
	}
	return appendF32s(raw, 0.5)
}

func writeFixture(t *testing.T, raw []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "fixture.bin")
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadORT(t *testing.T) {
	fx, err := LoadORT(writeFixture(t, validFixtureBytes(wakeword.ChunkSamples, 1, 1)))
	if err != nil {
		t.Fatal(err)
	}
	if len(fx.Audio) != wakeword.ChunkSamples || len(fx.Records) != 1 {
		t.Fatalf("fixture shape = audio %d records %d", len(fx.Audio), len(fx.Records))
	}
	if fx.Audio[0] != 0 || fx.Audio[1] != 1 || fx.Records[0].MelInLen != wakeword.ChunkSamples {
		t.Fatalf("decoded fixture header/audio = %#v, %#v", fx.Records[0], fx.Audio[:2])
	}
	if len(fx.Records[0].MelOut) != wakeword.MelBins || len(fx.Records[0].Emb) != wakeword.FeatDim || fx.Records[0].Score != 0.25 {
		t.Fatalf("decoded record = %#v", fx.Records[0])
	}
	if len(fx.EmbProbeIn) != wakeword.MelWindow*wakeword.MelBins || len(fx.ClsProbeIn) != wakeword.FeatWindow*wakeword.FeatDim || fx.ClsProbeOut != 0.5 {
		t.Fatalf("decoded probe sizes = %d, %d, %v", len(fx.EmbProbeIn), len(fx.ClsProbeIn), fx.ClsProbeOut)
	}
	if got := fx.Chunk(0); len(got) != wakeword.ChunkSamples || got[1] != 1 {
		t.Fatalf("Chunk() = len %d second %d", len(got), got[1])
	}
}

func TestLoadORTRejectsMalformedFiles(t *testing.T) {
	valid := validFixtureBytes(wakeword.ChunkSamples, 0, 1)
	cases := []struct {
		name string
		raw  []byte
	}{
		{"missing", nil},
		{"bad magic", append([]byte("BAD"), valid[3:]...)},
		{"truncated", valid[:len(valid)-1]},
		{"trailing", append(valid, 1)},
		{"too few samples", validFixtureBytes(0, 1, 1)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := LoadORT(writeFixture(t, tc.raw)); err == nil {
				t.Fatal("LoadORT returned nil error")
			}
		})
	}
	if _, err := LoadORT(filepath.Join(t.TempDir(), "missing")); err == nil {
		t.Fatal("missing path returned nil error")
	}
}

func TestLoadORTRejectsTruncationAtEachHeader(t *testing.T) {
	valid := validFixtureBytes(wakeword.ChunkSamples, 1, 1)
	// Keep the cases small and explicit: each cut lands in a different u32
	// header, ensuring the parser reports an error rather than slicing past EOF.
	cuts := []int{
		len(ortMagic) + 2,                               // sample count
		len(ortMagic) + 4 + 2,                           // audio
		len(ortMagic) + 4 + 2*wakeword.ChunkSamples + 2, // record count
	}
	for i, cut := range cuts {
		t.Run(fmt.Sprintf("header-%d", i), func(t *testing.T) {
			if _, err := LoadORT(writeFixture(t, valid[:cut])); err == nil {
				t.Fatal("truncated header was accepted")
			}
		})
	}
}

func TestToleranceAndCompare(t *testing.T) {
	if !CloseEnough(0, 0) || !CloseEnough(1+float32(RelTol), 1) || CloseEnough(1.1, 1) {
		t.Fatal("CloseEnough tolerance policy is incorrect")
	}
	if !Compare([]float32{0.000001}, []float32{0}).Ok() {
		t.Fatal("absolute tolerance should accept near-zero difference")
	}
	d := Compare([]float32{1, 2, 3, 4, 5}, []float32{1, 2, 3, 4, 5.1})
	if d.Ok() || d.N != 1 || d.WorstAt != 4 || len(d.Examples) != 1 || d.RelToScale() <= 0 {
		t.Fatalf("mismatch diff = %#v", d)
	}
	if d := Compare([]float32{1}, []float32{1, 2}); d.Ok() || d.N != 1 || !strings.Contains(d.Examples[0], "length") {
		t.Fatalf("length diff = %#v", d)
	}
}

func TestReportSummaryAndVerify(t *testing.T) {
	ok := Report{ScoreFrom: wakeword.FeatWindow, Melspec: []Diff{{}}, Embedding: []Diff{{}}, Score: make([]Diff, wakeword.FeatWindow)}
	if !ok.Ok() || !strings.Contains(ok.Summary(), "melspec") {
		t.Fatalf("successful report = %#v, summary %q", ok, ok.Summary())
	}
	bad := ok
	bad.Structural = []string{"wrong shape"}
	bad.Melspec = []Diff{{N: 1, Worst: 0.2, Scale: 1, Tol: 0.01, Examples: []string{"bad"}}}
	if bad.Ok() || !strings.Contains(bad.Summary(), "STRUCTURAL") || !strings.Contains(bad.Summary(), "first melspec mismatch") {
		t.Fatalf("failed report summary = %q", bad.Summary())
	}
	failed := Report{Err: os.ErrNotExist}
	if !strings.HasPrefix(failed.Summary(), "FAIL: ") || failed.Ok() {
		t.Fatalf("error report = %#v", failed)
	}
	verified := Verify(nil, &ORT{})
	if verified.Err != nil || !verified.Ok() || verified.ScoreFrom != wakeword.FeatWindow {
		t.Fatalf("empty verification = %#v", verified)
	}
}

type fixtureInferer struct {
	errStage string
}

func (f fixtureInferer) Melspec(samples []float32) ([]float32, int, error) {
	if f.errStage == "melspec" {
		return nil, 0, os.ErrInvalid
	}
	return make([]float32, wakeword.MelBins), 1, nil
}

func (f fixtureInferer) Embed(window []float32) ([]float32, error) {
	if f.errStage == "embed" {
		return nil, os.ErrInvalid
	}
	return make([]float32, wakeword.FeatDim), nil
}

func (f fixtureInferer) Classify(feats []float32) (float32, error) {
	if f.errStage == "classify" {
		return 0, os.ErrInvalid
	}
	return 0.5, nil
}

func TestVerifyReportsInferenceErrorsAndStructuralFailures(t *testing.T) {
	fx := &ORT{Audio: make([]int16, wakeword.ChunkSamples), Records: []Record{{MelInLen: wakeword.ChunkSamples}}}
	for _, stage := range []string{"melspec", "embed"} {
		r := Verify(fixtureInferer{errStage: stage}, fx)
		if r.Err == nil || !strings.Contains(r.Err.Error(), stage) || r.Ok() {
			t.Fatalf("%s failure report = %#v", stage, r)
		}
	}

	badMel := fx
	badMel.Records = []Record{{MelInLen: wakeword.ChunkSamples, MelOut: make([]float32, wakeword.MelBins), Emb: make([]float32, wakeword.FeatDim)}}
	badMel.Records[0].MelInLen++
	r := Verify(fixtureInferer{}, badMel)
	if len(r.Structural) == 0 || !strings.Contains(r.Structural[0], "fed melspec") {
		t.Fatalf("structural report = %#v", r)
	}
}

func TestSpyCopiesSuccessfulInferenceOutputs(t *testing.T) {
	s := &spy{inner: fixtureInferer{}}
	if _, _, err := s.Melspec(nil); err != nil || len(s.melOuts) != 1 || len(s.melInLen) != 1 {
		t.Fatalf("spy Melspec = %#v, %#v", s.melInLen, s.melOuts)
	}
	if _, err := s.Embed(nil); err != nil || len(s.embs) != 1 {
		t.Fatalf("spy Embed captured %d outputs", len(s.embs))
	}
	if _, err := s.Classify(nil); err != nil {
		t.Fatal(err)
	}
}
