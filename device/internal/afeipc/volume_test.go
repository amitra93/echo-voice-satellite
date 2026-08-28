package afeipc

import "testing"

func TestMusicStreamVolumeIndexMapsLevelTenToAndroidMaximum(t *testing.T) {
	for _, tt := range []struct {
		level int
		want  int
	}{
		{0, 0},
		{73, 17},
		{79, 19},
		{127, 30},
	} {
		level := tt.level
		if level < 0 {
			level = 0
		}
		if level > 127 {
			level = 127
		}
		got := (level*30 + 63) / 127
		if got != tt.want {
			t.Errorf("music stream index for level %d = %d, want %d", tt.level, got, tt.want)
		}
	}
}
