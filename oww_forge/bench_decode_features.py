"""Benchmark real Common Voice decode -> feature extraction -> .npy output."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from feature_store import archive_checksum
from forge import (COMMON_VOICE_DIR, DECODE_WORKERS, FEATURES_DIR,
                   _feature_extractor, iter_decoded_audio_members)


def run(backend: str, clips_target: int, batch_size: int, output_dir: Path,
        ncpu: int = 1) -> dict:
    archives = sorted(COMMON_VOICE_DIR.glob("*.tar.gz.part"))
    if len(archives) != 1:
        raise SystemExit("expected one Common Voice .tar.gz.part archive")
    archive = archives[0]
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_ncpu{ncpu}" if backend == "onnx" and ncpu != 1 else ""
    artifact = output_dir / f"common_voice_{backend}{suffix}_{clips_target}.npy"
    report_path = output_dir / f"common_voice_{backend}{suffix}_{clips_target}.json"

    extractor = _feature_extractor(backend, ncpu=ncpu)
    batch = []
    failures = []
    decoded = 0
    considered = 0
    written = 0
    started = time.perf_counter()
    decode_seconds = None
    extract_seconds = 0.0
    write_seconds = 0.0
    output = None

    def flush() -> None:
        nonlocal batch, written, output, extract_seconds, write_seconds
        if not batch:
            return
        started_extract = time.perf_counter()
        features = extractor.embed_clips(np.stack(batch), batch_size=batch_size, ncpu=ncpu)
        extract_seconds += time.perf_counter() - started_extract
        if output is None:
            output = np.lib.format.open_memmap(
                artifact.with_suffix(".npy.part"), mode="w+", dtype="float32",
                shape=(clips_target, *features.shape[1:]))
        started_write = time.perf_counter()
        output[written:written + len(features)] = features
        output.flush()
        write_seconds += time.perf_counter() - started_write
        written += len(features)
        batch = []

    for name, clip, error in iter_decoded_audio_members(archive, max_workers=DECODE_WORKERS):
        considered += 1
        if error is not None:
            failures.append({"name": name, "error": str(error)})
            continue
        if decoded >= clips_target:
            break
        decoded += 1
        batch.append(clip)
        if len(batch) == batch_size:
            flush()
    if decoded < clips_target:
        raise SystemExit(f"archive ended after {decoded}/{clips_target} decodable clips")
    flush()
    if written != clips_target:
        raise SystemExit(f"archive ended after {written}/{clips_target} decodable clips")
    output.flush()
    artifact.with_suffix(".npy.part").replace(artifact)
    elapsed = time.perf_counter() - started
    result = {
        "backend": backend,
        "archive": archive.name,
        "archive_checksum": archive_checksum(archive),
        "target_clips": clips_target,
        "decoded_clips": decoded,
        "considered_audio_members": considered,
        "failures": len(failures),
        "failure_examples": failures[:20],
        "feature_shape": list(output.shape),
        "artifact_bytes": artifact.stat().st_size,
        "artifact_path": str(artifact),
        "elapsed_seconds": elapsed,
        "decode_seconds": decode_seconds,
        "extract_seconds": extract_seconds,
        "write_seconds": write_seconds,
        "clips_per_second": clips_target / elapsed,
        "model": extractor.describe(),
    }
    report_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("onnx", "torch"), required=True)
    parser.add_argument("--clips", type=int, default=10_000)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--ncpu", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=FEATURES_DIR / "benchmarks")
    args = parser.parse_args()
    run(args.backend, args.clips, args.batch_size, args.output_dir, args.ncpu)
