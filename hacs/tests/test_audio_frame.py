"""Dedicated codec tests — mirrors controller/tests/test_phase0_audio.py's
coverage of em_audio_frame.py, since this module is a deliberate byte-for-byte
duplicate of it (see audio_frame.py's docstring) and must stay one.
"""

import pytest

from custom_components.echo_voice_satellite.audio_frame import (
    AudioProtocolError,
    MIC_EOS,
    MIC_FRAME_BYTES,
    MIC_PCM,
    Sequence,
    TTS_EOS,
    TTS_FRAME_BYTES,
    TTS_PCM,
    decode_frame,
    encode_frame,
)


def test_mic_pcm_round_trips_at_exact_frame_size():
    payload = bytes(range(256)) * (MIC_FRAME_BYTES // 256)
    assert len(payload) == MIC_FRAME_BYTES
    frame = decode_frame(encode_frame(MIC_PCM, 5, payload))
    assert (frame.frame_type, frame.sequence, frame.payload) == (MIC_PCM, 5, payload)


def test_tts_pcm_accepts_up_to_and_rejects_over_the_frame_limit():
    at_limit = b"\x00\x01" * (TTS_FRAME_BYTES // 2)
    frame = decode_frame(encode_frame(TTS_PCM, 0, at_limit))
    assert frame.payload == at_limit

    over_limit = at_limit + b"\x00\x01"
    with pytest.raises(AudioProtocolError, match="TTS_PCM"):
        encode_frame(TTS_PCM, 0, over_limit)


def test_tts_pcm_rejects_odd_length_and_empty_payload():
    with pytest.raises(AudioProtocolError):
        encode_frame(TTS_PCM, 0, b"\x00\x01\x02")  # odd
    with pytest.raises(AudioProtocolError):
        encode_frame(TTS_PCM, 0, b"")  # empty


def test_mic_pcm_rejects_wrong_size_either_direction():
    with pytest.raises(AudioProtocolError):
        encode_frame(MIC_PCM, 0, b"\x00" * (MIC_FRAME_BYTES - 1))
    with pytest.raises(AudioProtocolError):
        encode_frame(MIC_PCM, 0, b"\x00" * (MIC_FRAME_BYTES + 1))


@pytest.mark.parametrize("eos_type", [MIC_EOS, TTS_EOS])
def test_eos_frames_carry_no_payload(eos_type):
    frame = decode_frame(encode_frame(eos_type, 3))
    assert frame.payload == b""
    with pytest.raises(AudioProtocolError):
        encode_frame(eos_type, 3, b"\x00\x00")


def test_encode_rejects_unknown_frame_type_and_bad_sequence():
    with pytest.raises(AudioProtocolError):
        encode_frame(0x99, 0)
    with pytest.raises(AudioProtocolError):
        encode_frame(MIC_EOS, -1)
    with pytest.raises(AudioProtocolError):
        encode_frame(MIC_EOS, 0x1_0000_0000)


def test_decode_rejects_truncated_header():
    with pytest.raises(AudioProtocolError, match="truncated"):
        decode_frame(b"\x01\x00\x00")


def test_decode_rejects_nonzero_flags_byte():
    raw = bytearray(encode_frame(MIC_EOS, 0))
    raw[1] = 0x01
    with pytest.raises(AudioProtocolError, match="flags"):
        decode_frame(bytes(raw))


def test_sequence_rejects_out_of_order_and_frame_after_eos():
    seq = Sequence()
    seq.accept(decode_frame(encode_frame(MIC_PCM, 0, b"\x00" * MIC_FRAME_BYTES)))
    with pytest.raises(AudioProtocolError, match="sequence error"):
        seq.accept(decode_frame(encode_frame(MIC_PCM, 2, b"\x00" * MIC_FRAME_BYTES)))

    seq2 = Sequence()
    seq2.accept(decode_frame(encode_frame(TTS_EOS, 0)))
    assert seq2.eos is True
    with pytest.raises(AudioProtocolError, match="after EOS"):
        seq2.accept(decode_frame(encode_frame(TTS_PCM, 1, b"\x00\x00")))
