from __future__ import annotations

import pytest

from em_sync_sim import SimulatedDevice, simulate_group


def test_devices_with_different_offsets_and_drift_play_together():
    devices = [
        SimulatedDevice(offset_us=-2_000_000, drift_ppm=125),
        SimulatedDevice(offset_us=3_500_000, drift_ppm=-90),
    ]
    targets = [10_000_000, 11_000_000, 12_000_000]
    results = simulate_group(devices, targets, lead_us=500_000)

    played = [result for result in results if result.output_server_us is not None]
    assert len(played) == len(devices) * len(targets)
    assert max(abs(result.output_server_us - result.target_server_us) for result in played) <= 2
    for target in targets:
        times = [result.output_server_us for result in played if result.target_server_us == target]
        assert max(times) - min(times) <= 2


def test_jitter_before_the_deadline_does_not_change_output_time():
    results = simulate_group(
        [SimulatedDevice(offset_us=1_000, drift_ppm=50)],
        [10_000_000, 10_100_000, 10_200_000],
        lead_us=500_000,
        network_delays_us=[[0, 100_000, 499_999]],
    )
    assert all(not result.late and result.output_server_us == result.target_server_us for result in results)


def test_loss_and_late_delivery_are_distinguished():
    results = simulate_group(
        [SimulatedDevice(offset_us=0, drift_ppm=0)],
        [10_000_000, 10_100_000, 10_200_000],
        lead_us=100_000,
        network_delays_us=[[0, 100_001, 50_000]],
        lost={(0, 2)},
    )
    assert results[0].output_server_us == 10_000_000
    assert results[1].late and not results[1].lost and results[1].output_server_us is None
    assert results[2].lost and not results[2].late and results[2].output_server_us is None


def test_delay_exactly_at_deadline_is_not_late():
    result = simulate_group(
        [SimulatedDevice(offset_us=100, drift_ppm=0)],
        [10_000_000],
        lead_us=100_000,
        network_delays_us=[[100_000]],
    )[0]
    assert not result.late
    assert result.output_server_us == result.target_server_us


@pytest.mark.parametrize("lead", [-1, 0, 1])
def test_lead_boundary(lead):
    if lead < 0:
        with pytest.raises(ValueError):
            simulate_group([SimulatedDevice(0, 0)], [10], lead_us=lead)
    else:
        result = simulate_group([SimulatedDevice(0, 0)], [10], lead_us=lead)[0]
        assert result.output_server_us == 10
