"""Deterministic multi-device timing simulation for Sendspin validation."""

from __future__ import annotations

from dataclasses import dataclass

from em_clock import ClockSync


@dataclass(frozen=True)
class SimulatedDevice:
    """A device with a fixed monotonic offset and clock-rate error."""

    offset_us: int
    drift_ppm: float
    clock_base_us: int = 1_000_000

    def device_time(self, server_us: int) -> int:
        elapsed = server_us - self.clock_base_us
        return round(server_us + self.offset_us + elapsed * self.drift_ppm / 1_000_000)

    def server_time(self, device_us: int) -> int:
        # Invert the affine clock model. This is the ground truth used only by
        # the simulator to measure output alignment.
        rate = 1.0 + self.drift_ppm / 1_000_000
        return round((device_us - self.offset_us + self.clock_base_us * self.drift_ppm / 1_000_000) / rate)


@dataclass(frozen=True)
class ChunkResult:
    device_index: int
    target_server_us: int
    output_server_us: int | None
    late: bool
    lost: bool


def train_clock(device: SimulatedDevice, *, start_us: int = 5_000_000, count: int = 8) -> ClockSync:
    """Train a controller-side estimator using symmetric 4ms network paths."""
    clock = ClockSync(min_samples=4, min_span_us=500_000)
    for index in range(count):
        t1 = start_us + index * 250_000
        t2 = device.device_time(t1 + 4_000)
        t3 = t2 + 100
        t4 = t1 + 4_000 + 100 + 4_000
        if not clock.update(t1, t2, t3, t4):
            raise AssertionError("simulation clock probe was rejected")
    if not clock.synchronized:
        raise AssertionError("simulation clock did not converge")
    return clock


def simulate_group(
    devices: list[SimulatedDevice],
    target_server_times: list[int],
    *,
    lead_us: int,
    network_delays_us: list[list[int]] | None = None,
    lost: set[tuple[int, int]] | None = None,
) -> list[ChunkResult]:
    """Simulate timestamp scheduling and classify late/lost chunks."""
    if lead_us < 0:
        raise ValueError("lead_us must be non-negative")
    if network_delays_us is None:
        network_delays_us = [[0] * len(target_server_times) for _ in devices]
    if len(network_delays_us) != len(devices) or any(
        len(row) != len(target_server_times) for row in network_delays_us
    ):
        raise ValueError("network_delays_us shape does not match group")
    lost = lost or set()
    results: list[ChunkResult] = []
    for device_index, device in enumerate(devices):
        clock = train_clock(device)
        for chunk_index, target_server_us in enumerate(target_server_times):
            if (device_index, chunk_index) in lost:
                results.append(ChunkResult(device_index, target_server_us, None, False, True))
                continue
            target_device_us = clock.controller_to_device(target_server_us)
            arrival_server_us = target_server_us - lead_us + network_delays_us[device_index][chunk_index]
            arrival_device_us = device.device_time(arrival_server_us)
            if arrival_device_us > target_device_us:
                results.append(ChunkResult(device_index, target_server_us, None, True, False))
                continue
            output_server_us = device.server_time(target_device_us)
            results.append(ChunkResult(device_index, target_server_us, output_server_us, False, False))
    return results
