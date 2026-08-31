package client

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/gorilla/websocket"
)

const (
	TestAudioPath     = "/data/local/tmp/echomuse-test-query.wav"
	testAudioMaxBytes = 120 * 16000 * 2
)

// readTestWAV accepts the exact format produced by the controller: PCM S16,
// mono, 16 kHz. Walking RIFF chunks instead of assuming a 44-byte header keeps
// ordinary WAV metadata from shifting the data offset.
func readTestWAV(r io.ReadSeeker) ([]byte, error) {
	header := make([]byte, 12)
	if _, err := io.ReadFull(r, header); err != nil {
		return nil, err
	}
	if string(header[:4]) != "RIFF" || string(header[8:]) != "WAVE" {
		return nil, errors.New("not a RIFF/WAVE file")
	}

	var formatOK bool
	for {
		chunk := make([]byte, 8)
		if _, err := io.ReadFull(r, chunk); err != nil {
			return nil, fmt.Errorf("WAV has no usable data chunk: %w", err)
		}
		size := int64(binary.LittleEndian.Uint32(chunk[4:]))
		switch string(chunk[:4]) {
		case "fmt ":
			if size < 16 || size > 4096 {
				return nil, errors.New("invalid WAV fmt chunk")
			}
			buf := make([]byte, size)
			if _, err := io.ReadFull(r, buf); err != nil {
				return nil, err
			}
			formatOK = binary.LittleEndian.Uint16(buf[0:2]) == 1 &&
				binary.LittleEndian.Uint16(buf[2:4]) == 1 &&
				binary.LittleEndian.Uint32(buf[4:8]) == 16000 &&
				binary.LittleEndian.Uint16(buf[14:16]) == 16
		case "data":
			if !formatOK {
				return nil, errors.New("WAV must be 16 kHz mono 16-bit PCM")
			}
			if size <= 0 || size > testAudioMaxBytes {
				return nil, errors.New("WAV data is empty or exceeds 120 seconds")
			}
			pcm := make([]byte, size)
			if _, err := io.ReadFull(r, pcm); err != nil {
				return nil, err
			}
			return pcm, nil
		default:
			if _, err := r.Seek(size, io.SeekCurrent); err != nil {
				return nil, err
			}
		}
		if size%2 != 0 {
			if _, err := r.Seek(1, io.SeekCurrent); err != nil {
				return nil, err
			}
		}
	}
}

// StreamTestAudio replaces the live mic stream with a temporary WAV, paced at
// the same 80 ms cadence and framed identically. The controller then uses the
// normal voice turn, HA pipeline, TTS, EQ and speaker path unchanged.
func (d *DataClient) StreamTestAudio() error {
	f, err := os.Open(TestAudioPath)
	if err != nil {
		return err
	}
	pcm, err := readTestWAV(f)
	f.Close()
	if err != nil {
		return err
	}

	d.StopMic()
	var seq uint16
	send := func(payload []byte) error {
		frame := make([]byte, 3+len(payload))
		frame[0] = frameTypeMic
		binary.BigEndian.PutUint16(frame[1:3], seq)
		seq++
		copy(frame[3:], payload)

		d.connMu.Lock()
		defer d.connMu.Unlock()
		if d.conn == nil {
			return errors.New("data connection is not available")
		}
		d.conn.SetWriteDeadline(time.Now().Add(wsWriteWait))
		return d.conn.WriteMessage(websocket.BinaryMessage, frame)
	}

	for offset := 0; offset < len(pcm); offset += vadOwwChunkBytes {
		end := offset + vadOwwChunkBytes
		chunk := make([]byte, vadOwwChunkBytes)
		if end > len(pcm) {
			end = len(pcm)
		}
		copy(chunk, pcm[offset:end])
		if err := send(chunk); err != nil {
			return err
		}
		time.Sleep(80 * time.Millisecond)
	}
	return send([]byte{frameTypeVADEnd})
}

// CleanupTestAudio is commanded after the normal TTS playback has drained.
func (d *DataClient) CleanupTestAudio() error {
	return cleanupTestAudio(TestAudioPath)
}

func cleanupTestAudio(path string) error {
	err := os.Remove(path)
	if os.IsNotExist(err) {
		return nil
	}
	return err
}
