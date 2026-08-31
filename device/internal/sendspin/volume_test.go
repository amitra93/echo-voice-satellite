package sendspin

import "testing"

func TestVolumeScaleConversion(t *testing.T) {
	for _, test := range []struct {
		name       string
		ma, device int
	}{
		{"silence", 0, 0},
		{"button floor", 10, 73},
		{"middle", 50, 97},
		{"unity", 100, 127},
		{"low clamps", 0, 0},
		{"high clamps", 100, 127},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := MAVolumeToDevice(test.ma); got != test.device {
				t.Fatalf("MAVolumeToDevice(%d) = %d, want %d", test.ma, got, test.device)
			}
		})
	}
	if got := MAVolumeToDevice(-1); got != 0 {
		t.Fatalf("MAVolumeToDevice(-1) = %d, want 0", got)
	}
	if got := MAVolumeToDevice(101); got != 127 {
		t.Fatalf("MAVolumeToDevice(101) = %d, want 127", got)
	}
	if got := DeviceVolumeToMA(0); got != 0 {
		t.Fatalf("DeviceVolumeToMA(0) = %d, want 0", got)
	}
	if got := DeviceVolumeToMA(73); got != 10 {
		t.Fatalf("DeviceVolumeToMA(73) = %d, want 10", got)
	}
	if got := DeviceVolumeToMA(97); got != 50 {
		t.Fatalf("DeviceVolumeToMA(97) = %d, want 50", got)
	}
	if got := DeviceVolumeToMA(127); got != 100 {
		t.Fatalf("DeviceVolumeToMA(127) = %d, want 100", got)
	}
}
