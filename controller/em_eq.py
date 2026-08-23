"""
em_eq.py — Output EQ for EchoMuse Controller
=============================================

Applies a biquad filter chain to mono S16_LE PCM (Piper TTS output) before
it is resampled and streamed to the device speaker.

Eight independently controllable bands covering the Echo Dot Gen 2's useful
output range. Each band is a gain value in dB; 0.0 = flat (no effect).

Band centre frequencies and types:
  0:  125 Hz  — low shelf
  1:  250 Hz  — peaking, Q=1.4
  2:  500 Hz  — peaking, Q=1.4
  3: 1000 Hz  — peaking, Q=1.4
  4: 2000 Hz  — peaking, Q=1.4
  5: 3500 Hz  — peaking, Q=1.4
  6: 5500 Hz  — peaking, Q=1.4
  7: 8000 Hz  — high shelf

All filter design uses the Audio EQ Cookbook by Robert Bristow-Johnson.
High-pass uses scipy.signal.butter (already a dependency via openwakeword).

Usage:
    import em_eq
    eq_pcm = em_eq.apply(voice_response, SPEAKER_RATE, bands=[0]*8, loudness=False)
"""

import math
import logging
import numpy as np
from scipy.signal import sosfilt

log = logging.getLogger("echomuse.eq")

EQ_FREQUENCIES = [125, 250, 500, 1000, 2000, 3500, 5500, 8000]
NUM_BANDS       = len(EQ_FREQUENCIES)
DEFAULT_BANDS   = [0.0] * NUM_BANDS
_PEAK_Q         = 1.4   # ~1 octave bandwidth for middle bands


# ─── Biquad primitives ────────────────────────────────────────────────────────

def _peak_sos(fc: float, gain_db: float, Q: float, fs: float) -> np.ndarray:
    """Peaking parametric EQ biquad (Audio EQ Cookbook)."""
    A     = 10 ** (gain_db / 40.0)
    w0    = 2 * math.pi * fc / fs
    cw    = math.cos(w0)
    alpha = math.sin(w0) / (2 * Q)
    b0 = 1 + alpha * A;  b1 = -2 * cw;  b2 = 1 - alpha * A
    a0 = 1 + alpha / A;  a1 = -2 * cw;  a2 = 1 - alpha / A
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _loshelf_sos(fc: float, gain_db: float, fs: float) -> np.ndarray:
    """Low shelf biquad (Audio EQ Cookbook, S=1)."""
    A     = 10 ** (gain_db / 40.0)
    w0    = 2 * math.pi * fc / fs
    cw    = math.cos(w0)
    sqA   = math.sqrt(A)
    alpha = math.sin(w0) / math.sqrt(2)   # S=1
    b0 =      A * ((A+1) - (A-1)*cw + 2*sqA*alpha)
    b1 =  2 * A * ((A-1) - (A+1)*cw)
    b2 =      A * ((A+1) - (A-1)*cw - 2*sqA*alpha)
    a0 =           (A+1) + (A-1)*cw + 2*sqA*alpha
    a1 =     -2 * ((A-1) + (A+1)*cw)
    a2 =           (A+1) + (A-1)*cw - 2*sqA*alpha
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _hishelf_sos(fc: float, gain_db: float, fs: float) -> np.ndarray:
    """High shelf biquad (Audio EQ Cookbook, S=1)."""
    A     = 10 ** (gain_db / 40.0)
    w0    = 2 * math.pi * fc / fs
    cw    = math.cos(w0)
    sqA   = math.sqrt(A)
    alpha = math.sin(w0) / math.sqrt(2)   # S=1
    b0 =      A * ((A+1) + (A-1)*cw + 2*sqA*alpha)
    b1 = -2 * A * ((A-1) + (A+1)*cw)
    b2 =      A * ((A+1) + (A-1)*cw - 2*sqA*alpha)
    a0 =           (A+1) - (A-1)*cw + 2*sqA*alpha
    a1 =      2 * ((A-1) - (A+1)*cw)
    a2 =           (A+1) - (A-1)*cw - 2*sqA*alpha
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _highpass_sos(fc: float, fs: float, Q: float = 0.7071) -> np.ndarray:
    """Highpass biquad (Audio EQ Cookbook) to attenuate sub-bass below driver cutoff."""
    w0    = 2 * math.pi * fc / fs
    cw    = math.cos(w0)
    alpha = math.sin(w0) / (2 * Q)
    b0 = (1 + cw) / 2.0;  b1 = -(1 + cw);  b2 = (1 + cw) / 2.0
    a0 =  1 + alpha;      a1 = -2 * cw;     a2 =  1 - alpha
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])


def _loudness_sos(fs: float) -> np.ndarray:
    """Speech and acoustic presence boost for small enclosure at listening volumes."""
    presence = _peak_sos(2500, 4.0, 0.8, fs)
    warmth = _loshelf_sos(180, 2.5, fs)
    return np.vstack([presence, warmth])


# ─── Public API ───────────────────────────────────────────────────────────────

def build_sos(bands: list, sample_rate: int, loudness: bool = False, subsonic: bool = True) -> np.ndarray:
    """
    Build a stacked SOS matrix for the given band gains and sample rate.

    Exposed separately so callers can cache the matrix when bands haven't
    changed between calls.
    """
    sections = []
    if subsonic:
        sections.append(_highpass_sos(85.0, sample_rate))
    for i, (fc, gain_db) in enumerate(zip(EQ_FREQUENCIES, bands)):
        if i == 0:
            sections.append(_loshelf_sos(fc, gain_db, sample_rate))
        elif i == NUM_BANDS - 1:
            sections.append(_hishelf_sos(fc, gain_db, sample_rate))
        else:
            sections.append(_peak_sos(fc, gain_db, _PEAK_Q, sample_rate))
    if loudness:
        sections.append(_loudness_sos(sample_rate))
    return np.vstack(sections)


def apply(
    pcm: bytes,
    sample_rate: int,
    bands: list | None = None,
    loudness: bool = False,
    subsonic: bool = True,
) -> bytes:
    """
    Apply EQ to mono S16_LE PCM. Returns mono S16_LE PCM at the same rate.

    Args:
        pcm:         Raw mono S16_LE PCM bytes (decoded TTS audio).
        sample_rate: Sample rate of pcm (SPEAKER_RATE = 48000 in the
                     playback pipeline since the 48k decode change).
        bands:       List of NUM_BANDS (8) gain values in dB. None = flat.
        loudness:    Add presence and warmth boost if True.
        subsonic:    Add 85Hz highpass protection filter if True.

    Returns:
        EQ-processed mono S16_LE PCM bytes, same length as input.
    """
    if len(pcm) < 2:
        return pcm

    if bands is None:
        bands = DEFAULT_BANDS

    if len(bands) != NUM_BANDS:
        log.warning(f"[eq] Expected {NUM_BANDS} bands, got {len(bands)} — padding with zeros")
        bands = list(bands) + [0.0] * (NUM_BANDS - len(bands))

    # Short-circuit if everything is flat, loudness is off, and subsonic is off
    if not loudness and not subsonic and all(b == 0.0 for b in bands):
        return pcm

    samples  = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    sos      = build_sos(bands, sample_rate, loudness=loudness, subsonic=subsonic)
    filtered = sosfilt(sos, samples)
    filtered = np.clip(filtered, -32768, 32767).astype(np.int16)
    return filtered.tobytes()


class StreamingEQ:
    """
    Chunk-by-chunk EQ with filter state carried across calls — for audio
    that can't be processed as one buffer (music streams). apply() on
    independent chunks would reset the biquad states at every boundary
    and click; this is bit-identical to apply() over the concatenation.
    """

    def __init__(self, sample_rate: int, bands: list | None = None,
                 loudness: bool = False, subsonic: bool = True):
        if bands is None:
            bands = DEFAULT_BANDS
        if len(bands) != NUM_BANDS:
            bands = list(bands) + [0.0] * (NUM_BANDS - len(bands))
        if not loudness and not subsonic and all(b == 0.0 for b in bands):
            self._sos = None  # flat — pure passthrough
        else:
            self._sos = build_sos(bands, sample_rate, loudness=loudness, subsonic=subsonic)
            self._zi  = np.zeros((self._sos.shape[0], 2), dtype=np.float64)

    def process(self, pcm: bytes) -> bytes:
        if self._sos is None or len(pcm) < 2:
            return pcm
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        filtered, self._zi = sosfilt(self._sos, samples, zi=self._zi)
        return np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()

    def reset(self) -> None:
        if self._sos is not None:
            self._zi = np.zeros((self._sos.shape[0], 2), dtype=np.float64)

    apply = process
