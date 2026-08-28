//go:build server

package speaker

import "github.com/wilbowes/EchoMuse/internal/afeipc"

type afeSink struct{ c *afeipc.Client }

func newAFESink(c *afeipc.Client) *afeSink { return &afeSink{c: c} }

// Pump converts the renderer's duplicated mono stereo period back to the
// mono OpenSL stream. The renderer remains unchanged and therefore retains its
// mixer, output chain, ducking, EOS and statistics semantics.
func (s *afeSink) Pump(stereo []byte) error {
	mono := make([]byte, len(stereo)/2)
	for i := 0; i+3 < len(stereo); i += 4 {
		mono[i/2], mono[i/2+1] = stereo[i], stereo[i+1]
	}
	return s.c.WritePlayer(mono)
}
func (s *afeSink) Close() { _ = s.c.StopPlayer(); _ = s.c.ClearPlayer() }

// SetVolume controls Android's STREAM_MUSIC policy. The OpenSL track stays
// at unity so user volume is applied exactly once by AudioFlinger.
func (s *afeSink) SetVolume(level int) error { return s.c.SetPlayerVolume(level) }
