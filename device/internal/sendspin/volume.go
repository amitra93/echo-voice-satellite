package sendspin

const (
	deviceVolumeFloor  = 73
	deviceVolumeStep   = 6
	deviceVolumeLevels = 10
	deviceVolumeMax    = deviceVolumeFloor + (deviceVolumeLevels-1)*deviceVolumeStep
)

// DeviceVolumeToMA converts the codec's unity-gain-limited volume scale to
// Sendspin's 0-100 player scale. The device deliberately has ten canonical
// audible levels, so report those as 10%, 20%, ... 100%; zero remains silence.
func DeviceVolumeToMA(level int) int {
	if level <= 0 {
		return 0
	}
	if level <= deviceVolumeFloor {
		return 10
	}
	if level >= deviceVolumeMax {
		return 100
	}
	step := (level-deviceVolumeFloor+deviceVolumeStep/2)/deviceVolumeStep + 1
	return step * 100 / deviceVolumeLevels
}

// MAVolumeToDevice converts Sendspin's player scale to the codec's
// unity-gain-limited volume scale. Values snap to the same ten levels used by
// the physical buttons and LED arc, keeping all three views consistent.
func MAVolumeToDevice(volume int) int {
	if volume <= 0 {
		return 0
	}
	if volume >= 100 {
		return deviceVolumeMax
	}
	step := (volume*deviceVolumeLevels + 50) / 100
	if step < 1 {
		step = 1
	}
	return deviceVolumeFloor + (step-1)*deviceVolumeStep
}
