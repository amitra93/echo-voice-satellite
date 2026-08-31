package clock

import "time"

var processStart = time.Now()

// NowUs is the device-local monotonic timeline used by both clock probes and
// scheduled audio. Its origin is arbitrary and intentionally not wall time.
func NowUs() int64 {
	return time.Since(processStart).Microseconds()
}
