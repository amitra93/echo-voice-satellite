package client

import (
	"bytes"
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
)

func testWAV(pcm []byte, rate uint32, channels, bits uint16, metadata bool) []byte {
	var body bytes.Buffer
	body.WriteString("WAVE")
	body.WriteString("fmt ")
	binary.Write(&body, binary.LittleEndian, uint32(16))
	binary.Write(&body, binary.LittleEndian, uint16(1))
	binary.Write(&body, binary.LittleEndian, channels)
	binary.Write(&body, binary.LittleEndian, rate)
	binary.Write(&body, binary.LittleEndian, rate*uint32(channels)*uint32(bits/8))
	binary.Write(&body, binary.LittleEndian, channels*(bits/8))
	binary.Write(&body, binary.LittleEndian, bits)
	if metadata {
		body.WriteString("JUNK")
		binary.Write(&body, binary.LittleEndian, uint32(3))
		body.Write([]byte{1, 2, 3, 0}) // RIFF chunks are word-aligned.
	}
	body.WriteString("data")
	binary.Write(&body, binary.LittleEndian, uint32(len(pcm)))
	body.Write(pcm)

	var out bytes.Buffer
	out.WriteString("RIFF")
	binary.Write(&out, binary.LittleEndian, uint32(body.Len()))
	out.Write(body.Bytes())
	return out.Bytes()
}

func TestReadTestWAVAcceptsControllerFormatAndMetadata(t *testing.T) {
	pcm := bytes.Repeat([]byte{1, 2}, 160)
	got, err := readTestWAV(bytes.NewReader(testWAV(pcm, 16000, 1, 16, true)))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, pcm) {
		t.Fatal("decoded PCM differs from WAV data chunk")
	}
}

func TestReadTestWAVRejectsWrongAudioFormat(t *testing.T) {
	_, err := readTestWAV(bytes.NewReader(testWAV([]byte{0, 0}, 48000, 2, 16, false)))
	if err == nil {
		t.Fatal("accepted WAV that was not 16 kHz mono PCM")
	}
}

func TestCleanupTestAudioDeletesTheTemporaryFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "query.wav")
	if err := os.WriteFile(path, []byte("temporary"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := cleanupTestAudio(path); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("temporary test audio still exists: %v", err)
	}
	if err := cleanupTestAudio(path); err != nil {
		t.Fatalf("cleanup must be idempotent: %v", err)
	}
}
