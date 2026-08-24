"""Tests for controller-side DTLN streaming noise suppression."""

import sys
import types
import wave

import numpy as np
import pytest

import em_ns


class _Entry:
    def __init__(self, name, shape):
        self.name = name
        self.shape = shape


class _FakeSession:
    def __init__(self, mask=1.0, enhanced=0.0, output_kind="mask"):
        self.mask = mask
        self.enhanced = enhanced
        self.output_kind = output_kind
        self.calls = []

    def get_inputs(self):
        return [_Entry("data_in", (1, 1, 257)), _Entry("state_in", em_ns.STATE_SHAPE)]

    def get_outputs(self):
        return [_Entry("data_out", (1, 1, 257)), _Entry("state_out", em_ns.STATE_SHAPE)]

    def run(self, outputs, inputs):
        self.calls.append((outputs, inputs))
        if self.output_kind == "mask":
            return [
                np.full((1, 1, 257), self.mask, dtype=np.float32),
                np.ones(em_ns.STATE_SHAPE, dtype=np.float32),
            ]
        return [
            np.full((1, 1, 512), self.enhanced, dtype=np.float32),
            np.ones(em_ns.STATE_SHAPE, dtype=np.float32),
        ]


@pytest.fixture(autouse=True)
def reset_ns_globals(monkeypatch):
    monkeypatch.setattr(em_ns, "_sessions", None)
    monkeypatch.setattr(em_ns, "_load_failed", False)


def _sessions(session_1=None, session_2=None):
    first = session_1 or _FakeSession(output_kind="mask")
    second = session_2 or _FakeSession(output_kind="enhanced")
    return (
        (first, "data_in", "state_in", "data_out", "state_out"),
        (second, "data_in", "state_in", "data_out", "state_out"),
    )


def test_available_requires_both_model_files(tmp_path, monkeypatch):
    monkeypatch.setattr(em_ns, "MODEL_DIR", str(tmp_path))
    assert em_ns.available() is False

    (tmp_path / "model_1.onnx").touch()
    assert em_ns.available() is False

    (tmp_path / "model_2.onnx").touch()
    assert em_ns.available() is True


def test_available_is_true_when_sessions_are_loaded(monkeypatch):
    monkeypatch.setattr(em_ns, "_sessions", _sessions())
    assert em_ns.available() is True


def test_io_names_identifies_state_by_shape():
    sess = _FakeSession()

    assert em_ns._io_names(sess) == (
        "data_in",
        "state_in",
        "data_out",
        "state_out",
    )


def test_get_sessions_loads_both_models_and_configures_single_threading(monkeypatch, tmp_path):
    monkeypatch.setattr(em_ns, "MODEL_DIR", str(tmp_path))
    loaded = []

    class Options:
        pass

    def session(path, options, providers):
        loaded.append((path, options, providers))
        return _FakeSession()

    fake_ort = types.SimpleNamespace(
        SessionOptions=Options,
        InferenceSession=session,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    result = em_ns._get_sessions()

    assert result is not None
    assert len(result) == 2
    assert [item[0] for item in loaded] == [
        str(tmp_path / "model_1.onnx"),
        str(tmp_path / "model_2.onnx"),
    ]
    for _, options, providers in loaded:
        assert options.intra_op_num_threads == 1
        assert options.inter_op_num_threads == 1
        assert options.log_severity_level == 3
        assert providers == ["CPUExecutionProvider"]


def test_get_sessions_caches_success(monkeypatch):
    sessions = _sessions()
    monkeypatch.setattr(em_ns, "_sessions", sessions)

    assert em_ns._get_sessions() is sessions


def test_get_sessions_marks_load_failure_and_does_not_retry(monkeypatch):
    attempts = []

    class Options:
        pass

    def session(*args, **kwargs):
        attempts.append(args)
        raise RuntimeError("bad model")

    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        types.SimpleNamespace(SessionOptions=Options, InferenceSession=session),
    )

    assert em_ns._get_sessions() is None
    assert em_ns._load_failed is True
    assert em_ns._get_sessions() is None
    assert len(attempts) == 1


def test_streaming_denoiser_requires_loadable_sessions(monkeypatch):
    monkeypatch.setattr(em_ns, "_get_sessions", lambda: None)

    with pytest.raises(RuntimeError, match="DTLN models not loadable"):
        em_ns.StreamingDenoiser()


def test_streaming_denoiser_carries_partial_hop_and_processes_later_data(monkeypatch):
    first = _FakeSession(mask=1.0, enhanced=0.25, output_kind="mask")
    second = _FakeSession(mask=1.0, enhanced=0.25, output_kind="enhanced")
    monkeypatch.setattr(em_ns, "_get_sessions", lambda: _sessions(first, second))
    denoiser = em_ns.StreamingDenoiser()

    assert denoiser.process(b"\x01") == b""
    assert denoiser._pending == b"\x01"

    # Complete one 128-sample hop. The pending byte is included and the
    # output is exactly one hop of S16_LE audio.
    output = denoiser.process(b"\x00" * (em_ns.BLOCK_SHIFT * 2 - 1))
    assert len(output) == em_ns.BLOCK_SHIFT * 2
    assert len(first.calls) == 1
    assert len(second.calls) == 1
    assert denoiser._pending == b""
    assert np.all(np.frombuffer(output, dtype=np.int16) > 0)


def test_streaming_denoiser_preserves_state_and_processes_multiple_hops(monkeypatch):
    first = _FakeSession(mask=1.0, enhanced=0.1, output_kind="mask")
    second = _FakeSession(mask=1.0, enhanced=0.1, output_kind="enhanced")
    monkeypatch.setattr(em_ns, "_get_sessions", lambda: _sessions(first, second))
    denoiser = em_ns.StreamingDenoiser()

    output = denoiser.process(b"\x00" * (em_ns.BLOCK_SHIFT * 2 * 3))

    assert len(output) == em_ns.BLOCK_SHIFT * 2 * 3
    assert len(first.calls) == 3
    assert len(second.calls) == 3
    assert np.all(denoiser._states_1 == 1.0)
    assert np.all(denoiser._states_2 == 1.0)


def test_streaming_denoiser_clips_output_to_s16_range(monkeypatch):
    first = _FakeSession(mask=1.0, enhanced=2.0, output_kind="mask")
    second = _FakeSession(mask=1.0, enhanced=2.0, output_kind="enhanced")
    monkeypatch.setattr(em_ns, "_get_sessions", lambda: _sessions(first, second))
    denoiser = em_ns.StreamingDenoiser()

    output = denoiser.process(b"\x00" * (em_ns.BLOCK_SHIFT * 2))
    samples = np.frombuffer(output, dtype=np.int16)

    assert samples.min() >= -32768
    # The implementation deliberately clips to 0.999969 before multiplying
    # by 32768, leaving one integer step of headroom below positive full scale.
    assert samples.max() == 32766


def test_dump_debug_pair_is_noop_without_directory_or_raw(monkeypatch, tmp_path):
    monkeypatch.setattr(em_ns, "DEBUG_DIR", "")
    em_ns.dump_debug_pair("turn", b"raw", b"ns")

    monkeypatch.setattr(em_ns, "DEBUG_DIR", str(tmp_path))
    em_ns.dump_debug_pair("turn", b"", b"ns")
    assert list(tmp_path.iterdir()) == []


def test_dump_debug_pair_writes_mono_16k_wavs(monkeypatch, tmp_path):
    monkeypatch.setattr(em_ns, "DEBUG_DIR", str(tmp_path))
    monkeypatch.setattr(em_ns.time, "strftime", lambda _: "20260101-010203")

    em_ns.dump_debug_pair("turn", b"raw-pcm!", b"denoised-pcm")

    files = sorted(tmp_path.glob("*.wav"))
    assert [path.name for path in files] == [
        "20260101-010203_turn_ns.wav",
        "20260101-010203_turn_raw.wav",
    ]
    for path, expected in zip(files, (b"denoised-pcm", b"raw-pcm!")):
        with wave.open(str(path), "rb") as stream:
            assert stream.getnchannels() == 1
            assert stream.getsampwidth() == 2
            assert stream.getframerate() == 16000
            assert stream.readframes(stream.getnframes()) == expected


def test_dump_debug_pair_swallows_write_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(em_ns, "DEBUG_DIR", str(tmp_path))

    def fail(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(em_ns.os, "makedirs", fail)
    em_ns.dump_debug_pair("turn", b"raw", b"ns")
