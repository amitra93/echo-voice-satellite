//go:build server

package afeipc

import "testing"

func TestMusicStreamVolumeIndexReservesTopTenStepsForPhysicalButtons(t *testing.T) {
	for _, tt := range []struct {
		level int
		want  int
	}{
		{0, 0},
		{72, 20},
		{73, 21},
		{79, 22},
		{85, 23},
		{91, 24},
		{97, 25},
		{103, 26},
		{109, 27},
		{115, 28},
		{121, 29},
		{127, 30},
	} {
		got := musicStreamVolumeIndex(tt.level)
		if got != tt.want {
			t.Errorf("music stream index for level %d = %d, want %d", tt.level, got, tt.want)
		}
	}
}
