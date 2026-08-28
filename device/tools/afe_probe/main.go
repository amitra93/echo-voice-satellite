//go:build server

// afe_probe measures the three OpenSL input presets without changing the
// production server. Run it on a real Echo; its results are the Phase-0
// go/no-go evidence, not a synthetic host benchmark.
package main

import (
	"encoding/binary"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/wilbowes/EchoMuse/internal/opensl"
)

const (
	micRate, micPeriod, micBuffers    = 16000, 1280, 4
	toneRate, tonePeriod, toneBuffers = 48000, 2048, 4
)

type result struct {
	path            string
	frames          int
	startup, maxGap time.Duration
	rms             float64
	drops           uint64
}

func main() {
	lib := flag.String("lib", "libOpenSLES.so", "OpenSL ES library path or soname")
	out := flag.String("out", "/sdcard", "output directory")
	seconds := flag.Int("seconds", 8, "recording duration per preset")
	selection := flag.String("presets", "mic,voice_recognition,voice_communication", "comma-separated presets")
	tone := flag.Bool("tone", true, "play a known tone during each capture")
	toneHz := flag.Float64("tone-hz", 1000, "tone frequency")
	toneAmp := flag.Float64("tone-amp", .25, "tone amplitude, 0..1")
	flag.Parse()
	if *seconds < 1 || *seconds > 300 || *toneAmp < 0 || *toneAmp > 1 {
		fmt.Fprintln(os.Stderr, "afe_probe: seconds must be 1..300 and tone-amp must be 0..1")
		os.Exit(2)
	}
	presets, err := parsePresets(*selection)
	if err != nil {
		fmt.Fprintln(os.Stderr, "afe_probe:", err)
		os.Exit(2)
	}
	// Android's emulated-storage mount can return EEXIST for an existing
	// directory when accessed by a non-owner identity. The production probe is
	// commonly run as `system` to satisfy AudioFlinger's RECORD_AUDIO check, so
	// do not recreate an output directory the caller already prepared.
	if info, err := os.Stat(*out); err != nil {
		if err := os.MkdirAll(*out, 0755); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
	} else if !info.IsDir() {
		fmt.Fprintf(os.Stderr, "afe_probe: output path is not a directory: %s\n", *out)
		os.Exit(1)
	}

	engine, err := opensl.Open(*lib)
	if err != nil {
		fmt.Fprintf(os.Stderr, "afe_probe: OpenSL ES unavailable: %v\n", err)
		os.Exit(1)
	}
	defer engine.Close()
	fmt.Println("OpenSL ES engine opened")
	results := make(map[opensl.Preset]result)
	for _, preset := range presets {
		r, err := record(engine, preset, *seconds, *out, *tone, *toneHz, *toneAmp)
		if err != nil {
			fmt.Printf("%s: FAIL: %v\n", preset, err)
			continue
		}
		results[preset] = r
		fmt.Printf("%s: frames=%d startup=%s maxGap=%s rms=%.1f dBFS drops=%d file=%s\n",
			preset, r.frames, r.startup.Round(time.Millisecond), r.maxGap.Round(time.Millisecond), dbfs(r.rms), r.drops, r.path)
	}
	printERLE(results)
}

func parsePresets(value string) ([]opensl.Preset, error) {
	var result []opensl.Preset
	for _, token := range strings.Split(value, ",") {
		switch strings.ToLower(strings.TrimSpace(token)) {
		case "mic":
			result = append(result, opensl.PresetMic)
		case "voice_recognition", "vr":
			result = append(result, opensl.PresetVoiceRecognition)
		case "voice_communication", "vc":
			result = append(result, opensl.PresetVoiceCommunication)
		case "":
		default:
			return nil, fmt.Errorf("unknown preset %q", token)
		}
	}
	if len(result) == 0 {
		return nil, fmt.Errorf("no presets given")
	}
	return result, nil
}

func record(e *opensl.Engine, preset opensl.Preset, seconds int, out string, withTone bool, hz, amp float64) (result, error) {
	recorder, err := e.NewRecorder(preset, micRate, micPeriod, micBuffers)
	if err != nil {
		return result{}, err
	}
	defer recorder.Close()
	var player *opensl.Player
	var toneDone chan struct{}
	if withTone {
		player, err = e.NewPlayer(toneRate, tonePeriod*2, toneBuffers)
		if err != nil {
			return result{}, err
		}
		defer player.Close()
		toneDone = make(chan struct{})
		go func() {
			defer close(toneDone)
			playTone(player, hz, amp, time.Duration(seconds+1)*time.Second)
		}()
		time.Sleep(150 * time.Millisecond)
	}
	start := time.Now()
	if err := recorder.Start(); err != nil {
		return result{}, err
	}
	defer recorder.Stop()
	deadline := time.Now().Add(time.Duration(seconds) * time.Second)
	var samples []int16
	var last time.Time
	var startup, maxGap time.Duration
	for time.Now().Before(deadline) {
		buf, err := recorder.Read()
		if err != nil {
			return result{}, err
		}
		now := time.Now()
		if last.IsZero() {
			startup = now.Sub(start)
		} else if gap := now.Sub(last); gap > maxGap {
			maxGap = gap
		}
		last = now
		samples = append(samples, decode(buf)...)
	}
	if toneDone != nil {
		<-toneDone
	}
	path := filepath.Join(out, "afe_probe_"+strings.ToLower(preset.String())+".wav")
	if err := writeWAV(path, samples, micRate); err != nil {
		return result{}, err
	}
	skip := len(samples) * 3 / 10
	return result{path: path, frames: len(samples), startup: startup, maxGap: maxGap, rms: rms(samples[skip:]), drops: recorder.Drops()}, nil
}

func playTone(p *opensl.Player, hz, amp float64, duration time.Duration) {
	buf := make([]byte, tonePeriod*2)
	end, sample := time.Now().Add(duration), 0
	for time.Now().Before(end) {
		for i := 0; i < tonePeriod; i++ {
			v := amp * math.Sin(2*math.Pi*hz*float64(sample)/toneRate)
			binary.LittleEndian.PutUint16(buf[i*2:], uint16(int16(v*math.MaxInt16)))
			sample++
		}
		if err := p.Write(buf); err != nil {
			return
		}
	}
}

func decode(data []byte) []int16 {
	samples := make([]int16, len(data)/2)
	for i := range samples {
		samples[i] = int16(binary.LittleEndian.Uint16(data[i*2:]))
	}
	return samples
}

func rms(samples []int16) float64 {
	if len(samples) == 0 {
		return 0
	}
	var sum float64
	for _, sample := range samples {
		v := float64(sample) / 32768
		sum += v * v
	}
	return math.Sqrt(sum / float64(len(samples)))
}

func dbfs(value float64) float64 {
	if value <= 0 {
		return -120
	}
	return 20 * math.Log10(value)
}

func printERLE(results map[opensl.Preset]result) {
	base, ok := results[opensl.PresetMic]
	if !ok {
		fmt.Println("ERLE: MIC baseline unavailable")
		return
	}
	for _, preset := range []opensl.Preset{opensl.PresetVoiceRecognition, opensl.PresetVoiceCommunication} {
		if value, exists := results[preset]; exists {
			fmt.Printf("ERLE %s: %.1f dB\n", preset, dbfs(base.rms)-dbfs(value.rms))
		}
	}
	if value, exists := results[opensl.PresetVoiceRecognition]; exists {
		erle := dbfs(base.rms) - dbfs(value.rms)
		if erle > 9 {
			fmt.Println("GO: VOICE_RECOGNITION exceeds the approximate 7-9 dB Speex baseline")
		} else {
			fmt.Println("NO-GO: VOICE_RECOGNITION does not materially exceed the approximate 7-9 dB Speex baseline")
		}
	}
}

func writeWAV(path string, samples []int16, rate int) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	dataSize := uint32(len(samples) * 2)
	write := func(value any) error { return binary.Write(f, binary.LittleEndian, value) }
	if _, err = f.WriteString("RIFF"); err != nil {
		return err
	}
	if err = write(uint32(36) + dataSize); err != nil {
		return err
	}
	if _, err = f.WriteString("WAVEfmt "); err != nil {
		return err
	}
	if err = write(uint32(16)); err != nil {
		return err
	}
	if err = write(uint16(1)); err != nil {
		return err
	}
	if err = write(uint16(1)); err != nil {
		return err
	}
	if err = write(uint32(rate)); err != nil {
		return err
	}
	if err = write(uint32(rate * 2)); err != nil {
		return err
	}
	if err = write(uint16(2)); err != nil {
		return err
	}
	if err = write(uint16(16)); err != nil {
		return err
	}
	if _, err = f.WriteString("data"); err != nil {
		return err
	}
	if err = write(dataSize); err != nil {
		return err
	}
	for _, sample := range samples {
		if err = write(sample); err != nil {
			return err
		}
	}
	return nil
}
