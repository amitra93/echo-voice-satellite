from em_wake_audio import FrameRing


def test_ring_returns_only_frames_after_activation_sequence():
    ring = FrameRing(4)
    for sequence in range(10, 14):
        ring.append(sequence, bytes([sequence]))

    assert ring.after(11) == [b"\x0c", b"\x0d"]


def test_ring_handles_sequence_rollover_with_an_exact_activation_match():
    ring = FrameRing(4)
    for sequence in (65534, 65535, 0, 1):
        ring.append(sequence, sequence.to_bytes(2, "big"))

    assert ring.after(65535) == [b"\x00\x00", b"\x00\x01"]


def test_missing_activation_sequence_does_not_guess_a_rewind_boundary():
    ring = FrameRing(3)
    ring.append(7, b"a")
    ring.append(8, b"b")

    assert ring.after(6) == []
