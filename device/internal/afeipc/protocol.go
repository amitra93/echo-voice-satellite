// Package afeipc defines the deliberately small boundary used by the optional
// system-UID OpenSL helper. It has no cgo or Android dependencies so framing
// and error handling can be tested on the host.
package afeipc

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
)

const (
	Version       = 1
	MaxPayload    = 1 << 20
	headerSize    = 16
	requestOffset = 8
)

var magic = [4]byte{'E', 'M', 'A', 'F'}

type Type uint8

const (
	Open Type = iota + 1
	StartRecorder
	StopRecorder
	ReadRecorder
	WritePlayer
	ClearPlayer
	StopPlayer
	SetPlayerVolume
	Close
	Status
)

const (
	Response Type = 0x80
	Error    Type = 0xff
)

type Frame struct {
	Type      Type
	RequestID uint32
	Payload   []byte
}

// IsHelperMode reports whether the firmware was explicitly started as the
// system-UID OpenSL helper rather than as the root daemon.
func IsHelperMode(args []string) bool {
	for _, arg := range args {
		if arg == "--afe-helper" {
			return true
		}
	}
	return false
}

func (f Frame) WriteFrame(w io.Writer) error {
	if len(f.Payload) > MaxPayload {
		return fmt.Errorf("afeipc: payload is %d bytes, maximum is %d", len(f.Payload), MaxPayload)
	}
	header := make([]byte, headerSize)
	copy(header[:4], magic[:])
	header[4] = Version
	header[5] = byte(f.Type)
	binary.BigEndian.PutUint32(header[requestOffset:requestOffset+4], f.RequestID)
	binary.BigEndian.PutUint32(header[12:], uint32(len(f.Payload)))
	if err := writeAll(w, header); err != nil {
		return fmt.Errorf("afeipc: write header: %w", err)
	}
	if len(f.Payload) != 0 {
		if err := writeAll(w, f.Payload); err != nil {
			return fmt.Errorf("afeipc: write payload: %w", err)
		}
	}
	return nil
}

func writeAll(w io.Writer, data []byte) error {
	for len(data) != 0 {
		n, err := w.Write(data)
		if err != nil {
			return err
		}
		if n <= 0 || n > len(data) {
			return io.ErrShortWrite
		}
		data = data[n:]
	}
	return nil
}

func ReadFrame(r io.Reader) (Frame, error) {
	header := make([]byte, headerSize)
	if _, err := io.ReadFull(r, header); err != nil {
		return Frame{}, fmt.Errorf("afeipc: read header: %w", err)
	}
	if string(header[:4]) != string(magic[:]) {
		return Frame{}, errors.New("afeipc: bad magic")
	}
	if header[4] != Version {
		return Frame{}, fmt.Errorf("afeipc: unsupported version %d", header[4])
	}
	n := binary.BigEndian.Uint32(header[12:])
	if n > MaxPayload {
		return Frame{}, fmt.Errorf("afeipc: payload is %d bytes, maximum is %d", n, MaxPayload)
	}
	payload := make([]byte, n)
	if _, err := io.ReadFull(r, payload); err != nil {
		return Frame{}, fmt.Errorf("afeipc: read payload: %w", err)
	}
	return Frame{Type: Type(header[5]), RequestID: binary.BigEndian.Uint32(header[requestOffset : requestOffset+4]), Payload: payload}, nil
}
