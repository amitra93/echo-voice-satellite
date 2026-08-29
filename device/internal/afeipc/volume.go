package afeipc

// musicStreamVolumeIndex reserves Android's upper ten music steps for the
// physical Echo volume buttons. Their canonical raw values 73, 79, ... 127
// therefore map exactly to 21, 22, ... 30. Remote levels below the physical
// floor retain access to quiet playback and mute through Android's 0..20 range.
func musicStreamVolumeIndex(level int) int {
	if level < 0 {
		level = 0
	}
	if level > 127 {
		level = 127
	}
	if level < 73 {
		return (level*20 + 36) / 72
	}
	return 21 + ((level-73)*9+27)/54
}
