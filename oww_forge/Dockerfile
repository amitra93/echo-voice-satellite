# oww_forge — openWakeWord custom wake-word trainer.
#
# Build args:
#   GPU=1    → CUDA torch wheels (large image, fast piper generation + training)
#   GPU=0    → CPU-only torch (default; ~3GB image, generation/training is slower)
#   GPU=rocm → AMD ROCm 7.2 torch wheels for Radeon (RDNA3/4) under WSL2. The
#              wheels are the ".lw." ROCDXG/WSL build; librocdxg.so and /dev/dxg
#              are provided by the host at RUN time, not baked in — always run
#              this variant via docker-compose.rocm.yml, which mounts them.
#
# All pins are deliberate: openwakeword's train.py imports `generate_samples`
# from the piper-sample-generator repo ROOT, which only exists up to the
# v2.0.0 tag (the repo became a package afterwards). Don't bump that pin
# without re-checking train.py's import.

FROM python:3.11-slim

ARG GPU=0

# build-essential: webrtcvad ships as sdist only and compiles a C extension.
# libgomp1/libnuma1/libelf1/libdrm2: runtime deps of the AMD ROCm torch wheels
# (GPU=rocm); harmless for the CPU/CUDA variants and small, so installed
# unconditionally to keep the apt layer cache-shared across build args.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git wget ca-certificates libsndfile1 ffmpeg build-essential \
        libgomp1 libnuma1 libelf1 libdrm2 \
    && rm -rf /var/lib/apt/lists/*

# torch first, so the CPU/GPU split doesn't invalidate the rest of the cache.
# cu128 is required for Blackwell cards (RTX 50xx = sm_120); these wheels fall
# back to CPU cleanly at runtime when no GPU is visible.
RUN if [ "$GPU" = "1" ]; then \
        pip install --no-cache-dir torch==2.7.1 torchaudio==2.7.1 \
            --index-url https://download.pytorch.org/whl/cu128; \
    elif [ "$GPU" = "rocm" ]; then \
        pip install --no-cache-dir \
            --find-links https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/ \
            "https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/torch-2.7.1%2Brocm7.2.0.lw.git262e50d5-cp311-cp311-linux_x86_64.whl" \
            "https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/torchaudio-2.7.1%2Brocm7.2.0.git95c61b41-cp311-cp311-linux_x86_64.whl"; \
    else \
        pip install --no-cache-dir torch==2.7.1 torchaudio==2.7.1 \
            --index-url https://download.pytorch.org/whl/cpu; \
    fi

COPY requirements.txt /opt/forge/requirements.txt
RUN pip install --no-cache-dir -r /opt/forge/requirements.txt
# onnx2torch declares a generic torchvision dependency that resolves to CUDA
# wheels and replaces ROCm torch. The converted openWakeWord graphs have no
# torchvision operators; torch_features supplies the narrow import shim.
RUN pip install --no-cache-dir --no-deps onnx2torch==1.5.15

# openWakeWord, pinned to a known-good main commit (train.py + example config
# verified against this SHA — see oww_forge/README.md)
RUN git clone https://github.com/dscripka/openWakeWord /opt/openwakeword \
    && git -C /opt/openwakeword checkout 368c03716d1e92591906a84949bc477f3a834455 \
    && pip install --no-cache-dir -e /opt/openwakeword \
    # Upstream bug: --convert_to_tflite defaults to the STRING "False", which
    # is truthy — without this patch every --train_model run ends by importing
    # tensorflow (not installed here; we ship ONNX only) and crashing.
    && sed -i 's/if args.convert_to_tflite:/if args.convert_to_tflite is True:/' \
         /opt/openwakeword/openwakeword/train.py

# piper-sample-generator, pinned to v2.0.0 (last flat-layout release: train.py
# does `from generate_samples import generate_samples` against the repo root).
# The models/ dir is symlinked into /data so the ~430MB LibriTTS-R checkpoint
# lives on the volume (downloaded by `forge.py assets`), not in the image.
RUN git clone https://github.com/rhasspy/piper-sample-generator /opt/piper-sample-generator \
    && git -C /opt/piper-sample-generator checkout 195e3bd967d54589c2137c9de2b22ad526ba6b6f \
    && rm -rf /opt/piper-sample-generator/models \
    && ln -s /data/assets/piper /opt/piper-sample-generator/models \
    # torch >= 2.6 defaults torch.load to weights_only=True; the LibriTTS-R
    # checkpoint is a full pickled model object, so loading needs the old mode.
    && sed -i 's/torch.load(model_path)/torch.load(model_path, weights_only=False)/' \
         /opt/piper-sample-generator/generate_samples.py \
    && grep -q "weights_only=False" /opt/piper-sample-generator/generate_samples.py

# Frozen feature-extraction models (melspectrogram + Google speech embedding).
# train.py expects them inside the openwakeword package resources.
RUN OWW_RES=/opt/openwakeword/openwakeword/resources/models \
    && mkdir -p $OWW_RES \
    && for f in melspectrogram.onnx embedding_model.onnx melspectrogram.tflite embedding_model.tflite; do \
         wget -q -O $OWW_RES/$f \
           https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/$f; \
       done

# deep-phonemizer's checkpoint load also predates torch 2.6's weights_only
# flip; it's only reached for out-of-dictionary words (e.g. phonetic spelling
# variants like "clarra"), so it survives dictionary-word test runs.
RUN sed -i 's/torch.load(checkpoint_path, map_location=device)/torch.load(checkpoint_path, map_location=device, weights_only=False)/' \
      /usr/local/lib/python3.11/site-packages/dp/model/model.py \
    && grep -q "weights_only=False" /usr/local/lib/python3.11/site-packages/dp/model/model.py

COPY forge.py google_tts.py piper_voices.py forge_web.py torch_features.py feature_store.py bench_decode_features.py config.template.yml /opt/forge/
COPY static/ /opt/forge/static/
COPY tests/ /opt/forge/tests/

WORKDIR /data
EXPOSE 8769
ENTRYPOINT ["python", "/opt/forge/forge.py"]
CMD ["--help"]
