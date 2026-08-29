package afeipc

// musicStreamVolumeIndex converts an EchoMuse volume level to Android's
// STREAM_MUSIC index. The ten user-facing levels occupy 3..30; lower levels
// retain quiet playback through 0..2, with 0 remaining mute.
func musicStreamVolumeIndex(level int) int {
	if level < 0 {
		level = 0
	}
	if level > 127 {
		level = 127
	}
	if level < 73 {
		return (level*2 + 36) / 72
	}
	return 3 + ((level-73)*27+27)/54
}
