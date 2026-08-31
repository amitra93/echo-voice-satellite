package sendspin

import (
	"bytes"
	"testing"
)

func TestNewDecoderAcceptsPCMAndCopiesChunks(t *testing.T) {
	d, err := NewDecoder(StreamStartPlayer{Codec: CodecPCM, SampleRate: 48000, Channels: 1, BitDepth: 16})
	if err != nil {
		t.Fatal(err)
	}
	in := []byte{1, 2, 3, 4}
	out, err := d.Decode(in)
	if err != nil || !bytes.Equal(out, in) {
		t.Fatalf("Decode()=%v, %v", out, err)
	}
	in[0] = 99
	if out[0] == in[0] {
		t.Fatal("PCM decoder did not copy its input")
	}
}

func TestNewDecoderRejectsFormatsTheSpeakerCannotRender(t *testing.T) {
	for _, p := range []StreamStartPlayer{
		{Codec: CodecOpus, SampleRate: 48000, Channels: 1, BitDepth: 16},
		{Codec: CodecPCM, SampleRate: 44100, Channels: 1, BitDepth: 16},
		{Codec: CodecPCM, SampleRate: 48000, Channels: 2, BitDepth: 16},
		{Codec: CodecPCM, SampleRate: 48000, Channels: 1, BitDepth: 24},
		{Codec: CodecFLAC, SampleRate: 48000, Channels: 1, BitDepth: 16},
	} {
		if _, err := NewDecoder(p); err == nil {
			t.Fatalf("NewDecoder(%+v) unexpectedly succeeded", p)
		}
	}
}

func TestDecoderRejectsMalformedChunks(t *testing.T) {
	d, _ := NewDecoder(StreamStartPlayer{Codec: CodecPCM, SampleRate: 48000, Channels: 1, BitDepth: 16})
	if _, err := d.Decode(nil); err == nil {
		t.Fatal("empty PCM accepted")
	}
	if _, err := d.Decode([]byte{1}); err == nil {
		t.Fatal("odd PCM accepted")
	}
	flac, _ := NewDecoder(StreamStartPlayer{Codec: CodecFLAC, SampleRate: 48000, Channels: 1, BitDepth: 16, CodecHeader: []byte("fLaC")})
	if _, err := flac.Decode([]byte{0}); err == nil {
		t.Fatal("malformed FLAC accepted")
	}
}
