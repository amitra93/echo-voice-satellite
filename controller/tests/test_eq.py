import math

import numpy as np
import pytest

import em_eq
import em_limiter

RATE = 48000


def _sine(freq: float, seconds: float = 0.5, amp: float = 0.25) -> bytes:
    t = np.arange(int(RATE * seconds)) / RATE
    pcm = (np.sin(2 * math.pi * freq * t) * amp * 32767).astype(np.int16)
    return pcm.tobytes()


def _rms(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(x * x)))


def test_flat_bands_are_transparent():
    pcm = _sine(1000)
    out = em_eq.apply(pcm, RATE, bands=[0.0] * 8, subsonic=False)
    assert len(out) == len(pcm)
    # 0 dB everywhere should be within a fraction of a dB of identity.
    ratio = _rms(out) / _rms(pcm)
    assert 0.97 < ratio < 1.03


def test_none_bands_default_to_flat():
    pcm = _sine(1000)
    assert abs(_rms(em_eq.apply(pcm, RATE, subsonic=False)) - _rms(pcm)) / _rms(pcm) < 0.03


def test_band_boost_raises_its_own_frequency_only():
    # Test low shelf band boost in isolation without subsonic cutoff
    low = _sine(60)
    high = _sine(8000)
    bands = [6.0, 0, 0, 0, 0, 0, 0, 0]  # +6 dB low shelf
    low_gain = _rms(em_eq.apply(low, RATE, bands=bands, subsonic=False)) / _rms(low)
    high_gain = _rms(em_eq.apply(high, RATE, bands=bands, subsonic=False)) / _rms(high)
    assert low_gain > 1.7          # ~+6 dB ≈ ×2
    assert 0.9 < high_gain < 1.1   # shelf must not leak into the top band


def test_cut_reduces_level():
    pcm = _sine(1000)
    out = em_eq.apply(pcm, RATE, bands=[0, 0, 0, -12.0, 0, 0, 0, 0], subsonic=False)
    assert _rms(out) / _rms(pcm) < 0.5


def test_short_and_empty_input_pass_through():
    assert em_eq.apply(b"", RATE) == b""
    assert em_eq.apply(b"\x01", RATE) == b"\x01"


def test_wrong_band_count_still_returns_audio():
    pcm = _sine(1000)
    out = em_eq.apply(pcm, RATE, bands=[0.0, 0.0])  # padded internally
    assert len(out) == len(pcm)


def test_streaming_eq_matches_batch_apply():
    pcm = _sine(1000, seconds=0.4)
    bands = [3.0, 0, -2.0, 0, 0, 4.0, 0, 1.0]
    want = em_eq.apply(pcm, RATE, bands=bands, subsonic=True)
    eq = em_eq.StreamingEQ(RATE, bands=bands, subsonic=True)
    out = b""
    for i in range(0, len(pcm), 4096):
        out += eq.process(pcm[i:i + 4096])
    # Filter state carries across chunks — output must match the
    # whole-buffer path (same filters, same float32 pipeline).
    got = np.frombuffer(out, dtype=np.int16).astype(np.int32)
    ref = np.frombuffer(want, dtype=np.int16).astype(np.int32)
    assert np.abs(got - ref).max() <= 1  # ±1 LSB float rounding


def test_streaming_eq_flat_is_passthrough():
    eq = em_eq.StreamingEQ(RATE, bands=[0.0] * 8, subsonic=False)
    chunk = _sine(500, seconds=0.05)
    assert eq.process(chunk) == chunk


def test_subsonic_filter_attenuates_sub_bass():
    sub = _sine(35)
    mid = _sine(1000)
    out_sub = em_eq.apply(sub, RATE, bands=[0.0] * 8, subsonic=True)
    out_mid = em_eq.apply(mid, RATE, bands=[0.0] * 8, subsonic=True)
    assert _rms(out_sub) / _rms(sub) < 0.35  # ~-10dB at 35Hz
    assert 0.98 < _rms(out_mid) / _rms(mid) < 1.02


def test_loudness_presence_and_warmth_boost():
    # Loudness adds speech presence (2.5kHz) and warmth (180Hz)
    presence_tone = _sine(2500, seconds=0.2)
    warmth_tone = _sine(180, seconds=0.2)
    flat_presence = _rms(em_eq.apply(presence_tone, RATE, bands=[0.0] * 8, loudness=False, subsonic=False))
    boost_presence = _rms(em_eq.apply(presence_tone, RATE, bands=[0.0] * 8, loudness=True, subsonic=False))
    flat_warmth = _rms(em_eq.apply(warmth_tone, RATE, bands=[0.0] * 8, loudness=False, subsonic=False))
    boost_warmth = _rms(em_eq.apply(warmth_tone, RATE, bands=[0.0] * 8, loudness=True, subsonic=False))

    assert boost_presence / flat_presence > 1.35  # ~+4dB presence boost
    assert boost_warmth / flat_warmth > 1.15      # ~+1.5dB warmth boost


def test_streaming_eq_reset_clears_filter_memory():
    bands = [6.0, 0, 0, 0, 0, 0, 0, 0]
    eq = em_eq.StreamingEQ(RATE, bands=bands, subsonic=True)
    # Process audio with energy
    eq.process(_sine(125, seconds=0.1))
    assert not np.all(eq._zi == 0.0)
    # Reset must zero out state
    eq.reset()
    assert np.all(eq._zi == 0.0)
