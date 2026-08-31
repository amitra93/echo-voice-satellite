package sendspin

import (
	"bytes"
	"errors"
	"fmt"

	"github.com/mewkiz/flac/frame"
)

var (
	errUnsupportedFormat = errors.New("sendspin: unsupported audio format")
	errMissingFLACHeader = errors.New("sendspin: FLAC stream missing codec header")
)

// Decoder turns one Sendspin audio chunk into mono S16LE PCM. Sendspin FLAC
// chunks contain complete FLAC frames, so frame.Parse is intentionally used
// instead of buffering an unbounded stream on the device.
type Decoder struct {
	codec      string
	channels   int
	bitDepth   int
	sampleRate int
}

// NewDecoder validates the negotiated player stream. Echo hardware is mono
// 48kHz/S16 at the wire boundary; accepting a format we cannot render would
// make a connected player silently play at the wrong speed or pitch.
func NewDecoder(p StreamStartPlayer) (*Decoder, error) {
	if p.SampleRate != 48000 || p.Channels != 1 || p.BitDepth != 16 {
		return nil, fmt.Errorf("%w: %s/%dHz/%dch/%dbit", errUnsupportedFormat,
			p.Codec, p.SampleRate, p.Channels, p.BitDepth)
	}
	switch p.Codec {
	case CodecPCM:
	case CodecFLAC:
		if len(p.CodecHeader) == 0 {
			return nil, errMissingFLACHeader
		}
	default:
		return nil, fmt.Errorf("%w: codec %q", errUnsupportedFormat, p.Codec)
	}
	return &Decoder{codec: p.Codec, channels: p.Channels, bitDepth: p.BitDepth, sampleRate: p.SampleRate}, nil
}

// Decode returns a newly-owned mono S16LE PCM buffer. PCM chunks are copied so
// callers can retain them after gorilla/websocket reuses its read buffer.
func (d *Decoder) Decode(payload []byte) ([]byte, error) {
	if d == nil {
		return nil, errUnsupportedFormat
	}
	if d.codec == CodecPCM {
		if len(payload) == 0 || len(payload)%2 != 0 {
			return nil, fmt.Errorf("%w: malformed PCM chunk", errUnsupportedFormat)
		}
		return append([]byte(nil), payload...), nil
	}
	f, err := frame.Parse(bytes.NewReader(payload))
	if err != nil {
		return nil, fmt.Errorf("sendspin: decode FLAC frame: %w", err)
	}
	if len(f.Subframes) != 1 || f.BitsPerSample != 16 {
		return nil, fmt.Errorf("%w: FLAC frame channels=%d bits=%d", errUnsupportedFormat, len(f.Subframes), f.BitsPerSample)
	}
	samples := f.Subframes[0].Samples
	if len(samples) == 0 {
		return nil, fmt.Errorf("sendspin: empty FLAC frame")
	}
	out := make([]byte, len(samples)*2)
	for i, sample := range samples {
		if sample < -32768 || sample > 32767 {
			return nil, fmt.Errorf("%w: FLAC sample outside S16", errUnsupportedFormat)
		}
		out[i*2] = byte(sample)
		out[i*2+1] = byte(sample >> 8)
	}
	return out, nil
}
