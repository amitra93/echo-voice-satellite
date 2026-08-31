"""Feature extraction compatible with openWakeWord's ONNX AudioFeatures.

The source models remain the pinned ONNX files distributed with openWakeWord.
They are converted at runtime so ROCm PyTorch can execute the same graphs.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path


MEL_WINDOW = 76
MEL_STEP = 8
EMBEDDING_DIM = 96
MODEL_VERSION = "onnx2torch-1.5.15-clip-rewrite-v1"


def _models_dir() -> Path:
    from openwakeword import __file__ as oww_init

    return Path(oww_init).parent / "resources" / "models"


def _install_torchvision_stub() -> None:
    """Avoid a CUDA torchvision dependency for graphs that do not use its ops.

    onnx2torch imports NMS and ROIAlign converters unconditionally. The two
    openWakeWord graphs contain neither operation, so a placeholder preserves
    conversion while keeping the image's pinned ROCm torch wheel intact.
    """
    try:
        import torchvision  # noqa: F401
    except ModuleNotFoundError:
        unavailable = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("torchvision operation is unavailable in oww_forge"))
        torchvision = types.ModuleType("torchvision")
        ops = types.ModuleType("torchvision.ops")
        ops.nms = unavailable
        ops.box_convert = unavailable
        ops.roi_align = unavailable
        torchvision.ops = ops
        sys.modules["torchvision"] = torchvision
        sys.modules["torchvision.ops"] = ops


def _converted_mel_model(path: Path):
    """Convert the mel graph after replacing its dynamic Clip with Min/Max.

    onnx2torch 1.5.15 cannot convert a Clip whose lower bound is calculated
    in the graph. Min/Max is exactly equivalent for this model and remains a
    runtime conversion of the pinned ONNX weights.
    """
    import onnx
    from onnx import helper

    model = onnx.load(path)
    replacement = []
    for node in model.graph.node:
        if node.op_type == "Clip" and len(node.input) == 3 and node.input[1] == "onnx::Clip_36":
            lower = f"{node.output[0]}__lower"
            replacement.append(helper.make_node("Max", [node.input[0], node.input[1]], [lower]))
            replacement.append(helper.make_node("Min", [lower, node.input[2]], list(node.output)))
        else:
            replacement.append(node)
    del model.graph.node[:]
    model.graph.node.extend(replacement)

    _install_torchvision_stub()
    from onnx2torch import convert

    return convert(model)


class TorchAudioFeatures:
    """Batch feature extractor matching ``AudioFeatures.embed_clips``."""

    backend = "torch"

    def __init__(self, device: str = "cuda"):
        import torch

        if device != "cuda":
            raise ValueError("TorchAudioFeatures requires device='cuda'")
        if not torch.cuda.is_available():
            raise RuntimeError("the torch backend requires an available ROCm/CUDA GPU")
        models = _models_dir()
        self.device = torch.device(device)
        self.mel_model = _converted_mel_model(models / "melspectrogram.onnx").eval().to(self.device)
        _install_torchvision_stub()
        from onnx2torch import convert

        self.embedding_model = convert(models / "embedding_model.onnx").eval().to(self.device)
        self.device_name = torch.cuda.get_device_name(self.device)

    def describe(self) -> dict:
        return {"backend": self.backend, "device": self.device_name, "model_version": MODEL_VERSION}

    def _raw_mels(self, clips):
        import torch

        tensor = torch.as_tensor(clips, dtype=torch.float32, device=self.device)
        with torch.inference_mode():
            # Output is [clips, 1, frames, mel_bins], preserving raw PCM scale.
            return self.mel_model(tensor).squeeze(1)

    def _mels(self, clips):
        return self._raw_mels(clips) / 10 + 2

    def embed_clips(self, clips, batch_size: int = 128, ncpu: int = 1):
        """Return float32 [clips, windows, 96] features from int16 PCM clips."""
        del ncpu
        import numpy as np
        import torch

        clips = np.asarray(clips)
        if clips.ndim != 2 or clips.dtype != np.int16:
            raise ValueError("input must be a [clips, samples] int16 PCM array")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")

        outputs = []
        with torch.inference_mode():
            for start in range(0, len(clips), batch_size):
                mels = self._mels(clips[start:start + batch_size])
                if mels.shape[1] < MEL_WINDOW:
                    raise ValueError("embedding model requires at least 76 mel frames")
                # unfold yields [clips, windows, mel_bins, window]; transpose it
                # into the frozen embedding model's NHWC input layout.
                windows = mels.unfold(1, MEL_WINDOW, MEL_STEP).permute(0, 1, 3, 2)
                windows = windows.reshape(-1, MEL_WINDOW, 32, 1)
                embedded = self.embedding_model(windows).reshape(
                    mels.shape[0], -1, EMBEDDING_DIM)
                outputs.append(embedded.cpu().numpy().astype(np.float32, copy=False))
        return np.concatenate(outputs, axis=0)


class OnnxAudioFeatures:
    """Compatibility adapter for upstream's CPU-only feature extractor."""

    backend = "onnx"

    def __init__(self, ncpu: int = 1):
        from openwakeword.utils import AudioFeatures

        self.ncpu = ncpu
        self._features = AudioFeatures(inference_framework="onnx", device="cpu", ncpu=ncpu)

    def describe(self) -> dict:
        return {"backend": self.backend, "device": "cpu", "model_version": "openwakeword-onnx",
                "workers": self.ncpu}

    def embed_clips(self, clips, batch_size: int = 128, ncpu: int = 1):
        return self._features.embed_clips(clips, batch_size=batch_size, ncpu=ncpu)


def make_feature_extractor(backend: str = "auto", ncpu: int = 1):
    """Select an explicit backend; ``torch`` never silently falls back."""
    if backend not in {"auto", "torch", "onnx"}:
        raise ValueError("feature backend must be auto, torch, or onnx")
    if backend == "onnx":
        return OnnxAudioFeatures(ncpu=ncpu)

    import torch

    if backend == "torch":
        return TorchAudioFeatures()
    return TorchAudioFeatures() if torch.cuda.is_available() else OnnxAudioFeatures(ncpu=ncpu)
