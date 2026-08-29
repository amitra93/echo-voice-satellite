//go:build server

package afeipc

import "testing"

func TestMusicStreamVolumeIndexMapsPhysicalLevelsFromThreeToThirty(t *testing.T) {
	for _, tt := range []struct {
		level int
		want  int
	}{
		{0, 0},
		{72, 2},
		{73, 3},
		{79, 6},
		{85, 9},
		{91, 12},
		{97, 15},
		{103, 18},
		{109, 21},
		{115, 24},
		{121, 27},
		{127, 30},
	} {
		got := musicStreamVolumeIndex(tt.level)
		if got != tt.want {
			t.Errorf("music stream index for level %d = %d, want %d", tt.level, got, tt.want)
		}
	}
}
