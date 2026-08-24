# ROCm Feature Extraction Implementation Plan

**Status:** Implemented with changes. Runtime PyTorch conversion, CPU fallback,
and parity tests shipped. Ordinary CI remains GPU-free; the ROCm benchmark and
the proposed throughput gate remain manual validation work.

This plan covers replacing the CPU-only ONNX feature-extraction path with a
native PyTorch implementation that can run on the AMD ROCm GPU. It must not
change the feature representation used by openWakeWord.

## Summary

First establish ONNX CPU baselines and contract tests, then port the frozen mel
and embedding graphs to PyTorch. Prove tensor parity before benchmarking ROCm,
add resumable bounded-batch processing, and validate the complete pipeline on
real speech before enabling it by default. ONNX CPU remains the fallback.

## Implementation Decisions

The following choices are fixed for this implementation:

- Convert the frozen ONNX models to PyTorch at runtime with a pinned
  `onnx2torch` dependency. ONNX remains the only model-weight source.
- Stop the currently running Common Voice job before implementation begins.
  Preserve its partially downloaded archive so a later job can resume it.
- Require at least a 2x end-to-end throughput improvement on the 10,000-clip
  benchmark before ROCm becomes the `auto` default.
- Run synthetic contract, CPU parity, backend-selection, and resume tests in
  ordinary CI. Treat the full ROCm parity and benchmark command as a required
  local gate because no ROCm GitHub runner is available.
- Fail and preserve files when a partial feature checkpoint is stale, corrupt,
  or incompatible. Never delete or replace it without an explicit reset.
- Keep asset work in the existing sequential download-then-extract order. Do
  not shard downloads or Common Voice processing in this implementation.
- The Forge log is a 120-line moving window in the UI. Asset tasks report their
  queue position, current status, rate, and ETA when a total is known; stages
  without a reliable total explicitly report that ETA is unavailable.

## Scope

The first implementation targets direct corpus feature generation for:

- Common Voice
- FLEURS
- VoxPopuli
- AMI

The upstream openWakeWord augmentation pipeline remains on its existing ONNX
path initially. It can be migrated separately after this path is proven.

## Validation Gates

Each phase is a gate. Do not proceed to the next phase when its tests fail.

### 1. Establish Baselines

Benchmark the current ONNX CPU implementation before changing it.

First stop the active Common Voice job without deleting its partial archive.
Record the archive path and byte count before and after stopping it to verify
that the download was preserved.

Use deterministic synthetic audio and representative Common Voice clips. Test
batch sizes `1`, `64`, `256`, and `512` where supported.

Record separately:

- Audio decode and resampling time
- Mel inference time
- Embedding inference time
- Feature-array write time
- End-to-end clips per second
- CPU utilization and memory use

These measurements establish whether model inference is actually the bottleneck.

### 2. Add Contract Tests Before Conversion

The tests should define the required feature contract independently of the new
backend:

- Input is 16kHz mono PCM.
- Raw int16 magnitude is cast to float32 without normalization to `[-1, 1]`.
- A 32,000-sample clip produces 197 mel frames.
- Mel output is transformed with `mel / 10 + 2`.
- Embedding windows are `76 x 32` mel frames.
- Window stride is 8 mel frames.
- Embedding width is 96.
- A two-second clip produces `[16, 96]` features.
- Partial final batches are handled correctly.
- Output dtype is float32.

The current ONNX backend should pass these tests first. The PyTorch backend
must then pass the same tests.

### 3. Convert and Run Per-Stage Parity Tests

Convert the frozen models from:

```text
/opt/openwakeword/openwakeword/resources/models/melspectrogram.onnx
/opt/openwakeword/openwakeword/resources/models/embedding_model.onnx
```

The graphs use operations that should map cleanly to PyTorch:

- Mel model: convolution, matrix multiplication, logarithm, clipping, and
  scalar arithmetic.
- Embedding model: convolution, LeakyReLU, max, and max pooling.

Use a pinned `onnx2torch` dependency for runtime conversion. Do not generate or
check in a second set of model weights.

Compare ONNX CPU and PyTorch outputs at every stage:

- Raw mel output
- Transformed mel output
- Embedding-model input windows
- 96-element embeddings
- Complete `[N, 16, 96]` feature arrays

Use the existing fixture tolerance policy:

```text
absolute error <= 1e-5 + 1e-4 * tensor scale
```

Test both deterministic synthetic inputs and real speech clips. Reuse the
existing wake-word fixtures under `device/internal/wakeword/testdata/` where
their shapes apply.

No performance optimization or full-corpus run is allowed until parity passes.

### 4. Microbenchmark Model Computation

After parity passes, benchmark model computation with already-decoded audio so
decode cost does not obscure the result.

Compare:

- ONNX CPU
- PyTorch CPU
- PyTorch ROCm

Warm up the models before timing. Synchronize the ROCm device around timing
boundaries with `torch.cuda.synchronize()`.

Test batch sizes `1`, `64`, `256`, and `512`, recording:

- Clips per second
- Mel throughput
- Embedding throughput
- Peak GPU memory
- GPU utilization
- CPU utilization

Select the largest stable batch that improves end-to-end throughput without
causing memory pressure.

### 5. Test Resumable Feature Output

Before processing Common Voice, test the feature writer with a tiny generated
archive.

The checkpoint must include:

- Source archive checksum
- Completed clip count
- Expected output shape
- Extractor/backend version

Test that:

- An interrupted write resumes at the correct row.
- A changed archive rejects the old checkpoint.
- A backend or model-version change rejects incompatible partial output.
- Corrupt or truncated checkpoints fail safely.
- The final `.npy` is renamed from `.part` only after completion.
- Completed rows are never silently overwritten.
- A stale, corrupt, or incompatible checkpoint is preserved and produces an
  actionable error requiring an explicit reset.

### 6. Implement the GPU Pipeline

Add `oww_forge/torch_features.py` with an interface compatible with the
existing `AudioFeatures.embed_clips()` usage.

Support explicit backends:

- `auto`: PyTorch GPU when available, otherwise ONNX CPU
- `torch`: require the PyTorch GPU path and fail if unavailable
- `onnx`: current compatibility path

The PyTorch implementation must:

- Use `.eval()` and `torch.inference_mode()`.
- Use `torch.cuda` APIs, which target ROCm in this image.
- Vectorize embedding windows with `torch.unfold()` where practical.
- Process bounded GPU batches.
- Avoid retaining decoded audio or feature tensors unnecessarily.

Use bounded producer queues for MP3 decode/resampling and GPU inference. Use
pinned CPU buffers and asynchronous copies only after the simple synchronous
path is correct and measured.

### 7. Benchmark 10,000 Real Clips

Run at least 10,000 Common Voice clips through the complete pipeline.

Measure:

- Decode throughput
- Resampling throughput
- GPU inference throughput
- Feature write throughput
- End-to-end clips per second
- GPU queue starvation time
- CPU and GPU utilization
- Estimated full-corpus duration

Compare against the original ONNX CPU path. If decoding dominates, optimize
the producer path rather than increasing GPU batch size. ROCm must deliver at
least 2x the ONNX CPU end-to-end clips-per-second result before it can become
the `auto` default.

### 8. Run A/B Training Validation

Generate two otherwise identical small feature sets:

- ONNX CPU backend
- PyTorch ROCm backend

Train identical classifier configurations and compare:

- Validation false-positive rate
- Positive recall
- Score distributions
- Training convergence
- Final model behavior on the same evaluation clips

Classifier weights do not need to match exactly, but behavior must be
statistically equivalent.

### 9. Integrate and Document

After the gates pass:

- Replace direct `AudioFeatures` construction in Common Voice, FLEURS,
  VoxPopuli, and AMI builders with the backend selector.
- Add backend and device information to Forge job logs.
- Report completed clips, total clips, throughput, and ETA.
- Document CPU fallback and ROCm requirements.
- Keep ONNX CPU available as the compatibility fallback.
- Make `auto` the default only after the A/B validation succeeds.

## Final Regression Gate

Run all of the following before enabling the backend by default:

- Forge Python unit tests
- Feature contract tests
- ONNX/PyTorch parity tests
- CPU fallback tests
- ROCm integration tests
- Resume and corruption tests
- A short real-data asset job
- A small end-to-end model build

## Acceptance Criteria

The implementation is accepted only when:

- PyTorch and ONNX match at every tested feature stage within the documented
  tolerance.
- Generated feature arrays have the expected shape, dtype, and openWakeWord
  preprocessing semantics.
- The ROCm path passes the 10,000-clip real-data benchmark and delivers at
  least 2x the ONNX CPU end-to-end throughput.
- Interrupted jobs resume safely, reject stale or corrupt checkpoints, and
  publish no partial final `.npy` file. Rejected partial files are preserved
  until an explicit reset.
- CPU fallback and explicit `onnx` backend operation remain functional.
- A/B-trained classifiers show statistically equivalent validation behavior.
- Logs report backend, device, progress, throughput, and ETA accurately.
- Unit, parity, resume, ROCm integration, and small end-to-end regression tests
  all pass.
- The documented local ROCm parity and benchmark gate passes before `auto` is
  enabled; ordinary CI remains runnable without a GPU or private corpus.

## Execution Order

```text
baseline benchmark
-> contract tests
-> model conversion
-> per-stage parity tests
-> model microbenchmark
-> resumability tests
-> bounded decode/GPU pipeline
-> 10,000-clip benchmark
-> A/B classifier validation
-> default backend enablement
```

Correctness gates come before performance work. No full Common Voice feature
build should start until parity, resumability, and the 10,000-clip benchmark
have passed.
