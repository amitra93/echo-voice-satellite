from em_wake_audio import FRAME_COUNT, PREROLL_FRAMES, FrameRing


def test_ring_returns_only_frames_after_activation_sequence():
    ring = FrameRing(4)
    for sequence in range(10, 14):
        ring.append(sequence, bytes([sequence]))

    assert ring.after(11) == [b"\x0c", b"\x0d"]


def test_ring_returns_two_seconds_before_activation_through_latest_frame():
    ring = FrameRing()
    for sequence in range(100, 100 + PREROLL_FRAMES + 3):
        ring.append(sequence, bytes([sequence & 0xff]))

    result = ring.from_activation(100 + PREROLL_FRAMES)

    assert result.complete is True
    assert result.start_sequence == 100
    assert result.frames == tuple(bytes([sequence & 0xff])
                                 for sequence in range(100, 100 + PREROLL_FRAMES + 3))


def test_ring_returns_latest_two_second_window_without_activation_sequence():
    ring = FrameRing()
    for sequence in range(100, 100 + PREROLL_FRAMES + 3):
        ring.append(sequence, bytes([sequence & 0xff]))

    result = ring.from_latest()

    assert result.complete is True
    assert result.start_sequence == 103
    assert result.frames == tuple(
        bytes([sequence & 0xff])
        for sequence in range(103, 100 + PREROLL_FRAMES + 3)
    )


def test_ring_preroll_spans_sequence_rollover():
    ring = FrameRing()
    start = 65535 - PREROLL_FRAMES + 1
    sequences = [(start + i) & 0xffff for i in range(PREROLL_FRAMES + 2)]
    for sequence in sequences:
        ring.append(sequence, sequence.to_bytes(2, "big"))

    result = ring.from_activation((start + PREROLL_FRAMES) & 0xffff)

    assert result.complete is True
    assert result.start_sequence == start & 0xffff
    assert result.frames == tuple(sequence.to_bytes(2, "big") for sequence in sequences)


def test_stream_restart_clears_previous_sequence_epoch():
    ring = FrameRing()
    ring.append(500, b"old")
    ring.append(501, b"old-next")
    ring.append(0, b"new")
    ring.append(1, b"new-next")

    result = ring.from_activation(0, preroll_frames=0)

    assert result.complete is True
    assert result.frames == (b"new", b"new-next")


def test_missing_preroll_start_uses_post_activation_fallback():
    ring = FrameRing()
    for sequence in range(PREROLL_FRAMES, PREROLL_FRAMES + 3):
        ring.append(sequence, bytes([sequence]))

    result = ring.from_activation(PREROLL_FRAMES)

    assert result.complete is False
    assert result.start_sequence == 0
    assert result.reason == "requested preroll start is outside the PCM ring"
    assert result.frames == (bytes([PREROLL_FRAMES + 1]), bytes([PREROLL_FRAMES + 2]))


def test_ring_capacity_covers_preroll_and_control_delay():
    ring = FrameRing()
    for sequence in range(FRAME_COUNT + 10):
        ring.append(sequence, bytes([sequence & 0xff]))

    result = ring.from_activation(FRAME_COUNT - PREROLL_FRAMES + 5)

    assert result.complete is True


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
