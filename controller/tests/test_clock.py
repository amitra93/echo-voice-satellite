from __future__ import annotations

import pytest

from em_clock import ClockSync


def probe(t1, *, offset=5_000, forward=4_000, processing=100, return_path=4_000):
    t2 = t1 + forward + offset
    t3 = t2 + processing
    t4 = t3 - offset + return_path
    return t1, t2, t3, t4


def test_reconstructs_offset_and_timestamp_conversion():
    clock = ClockSync(min_samples=2, min_span_us=0)
    for t1 in (1_000_000, 1_500_000):
        assert clock.update(*probe(t1))

    assert clock.synchronized
    assert clock.offset_us == pytest.approx(5_000, abs=1)
    assert clock.controller_to_device(2_000_000) == 2_005_000
    assert clock.device_to_controller(2_005_000) == 2_000_000


def test_asymmetric_network_delay_is_reflected_in_offset_error():
    clock = ClockSync(min_samples=2, min_span_us=0)
    # NTP-style midpoint estimates contain half the forward/return asymmetry;
    # they must not silently claim the unobservable true offset.
    assert clock.update(*probe(1_000_000, offset=5_000, forward=2_000, return_path=8_000))
    assert clock.offset_us == pytest.approx(2_000, abs=1)


def test_sample_history_is_bounded_and_latest_window_drives_fit():
    clock = ClockSync(max_samples=3, min_samples=2, min_span_us=0)
    for i in range(6):
        assert clock.update(*probe(1_000_000 + i * 1_000_000, offset=1_000 + i * 100))
    assert len(clock.samples) == 3
    assert clock.samples[0].controller_us == 4_004_050
    assert clock.drift_ppm == pytest.approx(100, abs=1)


def test_rejects_zero_rtt_and_cross_domain_order_is_not_assumed():
    clock = ClockSync(min_samples=2, min_span_us=0)
    # Device timestamps are deliberately numerically below controller
    # timestamps because each monotonic clock has an arbitrary origin.
    assert clock.update(10_000_000, 2_000, 2_100, 10_008_000)
    assert not clock.update(20_000_000, 3_000, 3_100, 20_000_100)
    assert clock.rejected == 1


def test_constructor_rejects_invalid_limits():
    with pytest.raises(ValueError):
        ClockSync(max_rtt_us=0)
    with pytest.raises(ValueError):
        ClockSync(max_samples=1)
    with pytest.raises(ValueError):
        ClockSync(min_samples=1)
    with pytest.raises(ValueError):
        ClockSync(min_span_us=-1)
    with pytest.raises(ValueError):
        ClockSync(max_samples=2, min_samples=3)


def test_exact_rtt_boundary_is_accepted_and_one_microsecond_over_is_rejected():
    accepted = ClockSync(max_rtt_us=8_000, min_samples=2, min_span_us=0)
    assert accepted.update(*probe(1_000_000))

    rejected = ClockSync(max_rtt_us=8_000, min_samples=2, min_span_us=0)
    assert not rejected.update(*probe(1_000_000, return_path=4_001))
    assert rejected.rejected == 1


def test_conversion_requires_a_sample_and_zero_processing_is_valid():
    clock = ClockSync(min_samples=2, min_span_us=0)
    with pytest.raises(RuntimeError):
        clock.controller_to_device(1)
    with pytest.raises(RuntimeError):
        clock.device_to_controller(1)
    assert clock.update(*probe(1_000_000, processing=0))


def test_zero_timestamp_origin_is_valid():
    clock = ClockSync(min_samples=2, min_span_us=0)
    assert clock.update(*probe(0))
    assert clock.update(*probe(1_000_000))
    assert clock.synchronized


@pytest.mark.parametrize(
    "values",
    [
        (-1, 0, 0, 1),
        (0, -1, 0, 1),
        (0, 0, -1, 1),
        (0, 0, 0, -1),
        (0.0, 0, 0, 1),
        (0, False, 0, 1),
    ],
)
def test_rejects_negative_and_non_integer_timestamps(values):
    clock = ClockSync(min_samples=2, min_span_us=0)
    assert not clock.update(*values)
    assert clock.rejected == 1


def test_tracks_clock_drift():
    clock = ClockSync(min_samples=4, min_span_us=1)
    for index, t1 in enumerate((1_000_000, 2_000_000, 3_000_000, 4_000_000)):
        # Device offset grows by 100us per second: 100ppm.
        assert clock.update(*probe(t1, offset=5_000 + index * 100))

    assert clock.synchronized
    assert clock.drift_ppm == pytest.approx(100, abs=1)
    assert clock.controller_to_device(5_000_000) == 5_005_400


def test_conversion_round_trip_remains_within_one_microsecond_under_drift():
    clock = ClockSync(min_samples=4, min_span_us=1)
    for index, t1 in enumerate((1_000_000, 2_000_000, 3_000_000, 4_000_000)):
        assert clock.update(*probe(t1, offset=7_000 + index * 250))
    for controller_us in (4_000_000, 4_500_000, 9_000_000):
        device_us = clock.controller_to_device(controller_us)
        assert abs(clock.device_to_controller(device_us) - controller_us) <= 1


def test_rejects_invalid_order_and_high_rtt_without_changing_estimate():
    clock = ClockSync(min_samples=2, min_span_us=0, max_rtt_us=20_000)
    assert clock.update(*probe(1_000_000))
    assert not clock.update(2_000_000, 2_001_000, 2_001_100, 2_101_100)
    assert not clock.update(3_000_000, 2_999_000, 3_000_000, 3_001_000)
    assert clock.rejected == 2
    assert clock.offset_us == pytest.approx(5_000, abs=1)


def test_reset_requires_fresh_samples():
    clock = ClockSync(min_samples=2, min_span_us=0)
    assert clock.update(*probe(1_000_000))
    assert clock.update(*probe(2_000_000))
    assert clock.synchronized
    clock.reset()
    assert not clock.synchronized
    with pytest.raises(RuntimeError):
        clock.controller_to_device(3_000_000)
