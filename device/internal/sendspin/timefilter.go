// Package sendspin implements the on-device Sendspin client that talks to
// Music Assistant directly (MA -> device), replacing the controller-side
// aiosendspin client and the 0x06/0x07 frame forwarding.
package sendspin

import "math"

// TimeFilter is a 1-to-1 port of aiosendspin's SendspinTimeFilter
// (client/time_sync.py), itself a port of the ESPHome implementation: a 2-D
// Kalman filter over [offset, drift] that maps server timestamps onto the
// local clock from NTP-style measurements. The arithmetic order is preserved
// exactly against the Python reference so the two agree to the rounded output
// (verified by testdata/timefilter_fixture.json).
type TimeFilter struct {
	lastUpdate int64
	count      int

	offset float64
	drift  float64

	offsetCovariance      float64
	offsetDriftCovariance float64
	driftCovariance       float64

	processVariance      float64
	driftProcessVariance float64
	forgetVarianceFactor float64

	// Latest transform parameters (the Python TimeElement), read by the
	// compute_* conversions independently of an in-flight update.
	elemLastUpdate int64
	elemOffset     float64
	elemDrift      float64
	elemUseDrift   bool
}

const (
	adaptiveForgettingCutoff           = 3.0
	maxErrorScale                      = 0.5
	driftSignificanceThresholdSquared  = 2.0 * 2.0
)

// NewTimeFilter constructs the filter with the reference defaults
// (process_std_dev=0, forget_factor=2, drift_process_std_dev=1e-11).
func NewTimeFilter() *TimeFilter {
	return newTimeFilter(0.0, 2.0, 1e-11)
}

func newTimeFilter(processStdDev, forgetFactor, driftProcessStdDev float64) *TimeFilter {
	t := &TimeFilter{
		processVariance:      processStdDev * processStdDev,
		driftProcessVariance: driftProcessStdDev * driftProcessStdDev,
		forgetVarianceFactor: forgetFactor * forgetFactor,
		offsetCovariance:     math.Inf(1),
	}
	return t
}

// Update processes one NTP-style measurement.
//
//	measurement: ((T2-T1)+(T3-T4))/2 in microseconds.
//	maxError:    ((T4-T1)-(T3-T2))/2, half the round-trip, in microseconds.
//	timeAdded:   client timestamp when the measurement was taken, microseconds.
func (t *TimeFilter) Update(measurement, maxError, timeAdded int64) {
	if timeAdded <= t.lastUpdate {
		// Non-monotonic timestamps guard against a backwards dt in predict.
		return
	}

	dt := float64(timeAdded - t.lastUpdate)
	t.lastUpdate = timeAdded

	updateStdDev := float64(maxError) * maxErrorScale
	measurementVariance := updateStdDev * updateStdDev

	// First measurement establishes the offset baseline.
	if t.count <= 0 {
		t.count++
		t.offset = float64(measurement)
		t.offsetCovariance = measurementVariance
		t.drift = 0.0
		t.setElement(false)
		return
	}

	// Second measurement: initial drift from finite differences.
	if t.count == 1 {
		t.count++
		t.drift = (float64(measurement) - t.offset) / dt
		t.offset = float64(measurement)
		t.driftCovariance = (t.offsetCovariance + measurementVariance) / (dt * dt)
		t.offsetCovariance = measurementVariance
		t.setElement(false)
		return
	}

	// Kalman prediction: x_k|k-1 = F * x_k-1|k-1
	offset := t.offset + t.drift*dt
	dtSquared := dt * dt

	driftProcessVariance := dt * t.driftProcessVariance
	newDriftCovariance := t.driftCovariance + driftProcessVariance

	offsetDriftProcessVariance := 0.0
	newOffsetDriftCovariance := t.offsetDriftCovariance +
		t.driftCovariance*dt + offsetDriftProcessVariance

	offsetProcessVariance := dt * t.processVariance
	newOffsetCovariance := t.offsetCovariance +
		2*t.offsetDriftCovariance*dt +
		t.driftCovariance*dtSquared +
		offsetProcessVariance

	// Innovation + adaptive forgetting.
	residual := float64(measurement) - offset
	maxResidualCutoff := float64(maxError) * adaptiveForgettingCutoff

	if t.count < 100 {
		t.count++
	} else if math.Abs(residual) > maxResidualCutoff {
		newDriftCovariance *= t.forgetVarianceFactor
		newOffsetDriftCovariance *= t.forgetVarianceFactor
		newOffsetCovariance *= t.forgetVarianceFactor
	}

	// Kalman update.
	uncertainty := 1.0 / (newOffsetCovariance + measurementVariance)
	offsetGain := newOffsetCovariance * uncertainty
	driftGain := newOffsetDriftCovariance * uncertainty

	t.offset = offset + offsetGain*residual
	t.drift += driftGain * residual

	t.driftCovariance = newDriftCovariance - driftGain*newOffsetDriftCovariance
	t.offsetDriftCovariance = newOffsetDriftCovariance - driftGain*newOffsetCovariance
	t.offsetCovariance = newOffsetCovariance - offsetGain*newOffsetCovariance

	useDrift := t.drift*t.drift >
		driftSignificanceThresholdSquared*t.driftCovariance
	t.setElement(useDrift)
}

func (t *TimeFilter) setElement(useDrift bool) {
	t.elemLastUpdate = t.lastUpdate
	t.elemOffset = t.offset
	t.elemDrift = t.drift
	t.elemUseDrift = useDrift
}

// ComputeServerTime converts a client timestamp to the server domain.
func (t *TimeFilter) ComputeServerTime(clientTime int64) int64 {
	effectiveDrift := 0.0
	if t.elemUseDrift {
		effectiveDrift = t.elemDrift
	}
	dt := float64(clientTime - t.elemLastUpdate)
	offset := roundHalfEven(t.elemOffset + effectiveDrift*dt)
	return clientTime + offset
}

// ComputeClientTime converts a server timestamp to the client domain.
func (t *TimeFilter) ComputeClientTime(serverTime int64) int64 {
	effectiveDrift := 0.0
	if t.elemUseDrift {
		effectiveDrift = t.elemDrift
	}
	return roundHalfEven(
		(float64(serverTime) - t.elemOffset + effectiveDrift*float64(t.elemLastUpdate)) /
			(1.0 + effectiveDrift))
}

// Reset clears all filter state.
func (t *TimeFilter) Reset() {
	t.count = 0
	t.lastUpdate = 0
	t.offset = 0.0
	t.drift = 0.0
	t.offsetCovariance = math.Inf(1)
	t.offsetDriftCovariance = 0.0
	t.driftCovariance = 0.0
	t.setElement(false)
	t.elemLastUpdate = 0
}

// Count returns the number of measurements processed.
func (t *TimeFilter) Count() int { return t.count }

// IsSynchronized reports whether the filter is ready (>=2 measurements and a
// finite offset covariance).
func (t *TimeFilter) IsSynchronized() bool {
	return t.count >= 2 && !math.IsInf(t.offsetCovariance, 0)
}

// Error returns the offset standard-deviation estimate in microseconds.
func (t *TimeFilter) Error() int64 { return roundHalfEven(math.Sqrt(t.offsetCovariance)) }

// Offset returns the current filtered offset estimate in microseconds.
func (t *TimeFilter) Offset() float64 { return t.offset }

// roundHalfEven matches Python's round() (banker's rounding) so the ported
// conversions agree with the reference to the exact integer.
func roundHalfEven(x float64) int64 {
	r := math.RoundToEven(x)
	return int64(r)
}
