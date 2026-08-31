"""Crash-safe, resumable storage for fixed-shape feature arrays."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np


class CheckpointError(RuntimeError):
    """A partial feature file cannot safely be resumed."""


def archive_checksum(path: Path) -> str:
    """Use the downloader's checksum sidecar, or calculate one when absent."""
    sidecar = path.with_suffix(path.suffix + ".checksum")
    try:
        value = sidecar.read_text(encoding="ascii").strip()
        if len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower()):
            return value.lower()
    except OSError:
        pass

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ResumableFeatureWriter:
    """Append feature rows to ``.npy.part`` with an independently durable count."""

    FORMAT_VERSION = 1

    def __init__(self, dest: Path, shape: tuple[int, ...], source_checksum: str,
                 extractor_version: str):
        self.dest = dest
        self.part = dest.with_suffix(dest.suffix + ".part")
        self.checkpoint = self.part.with_suffix(self.part.suffix + ".json")
        self.shape = tuple(shape)
        self._identity = {
            "format": self.FORMAT_VERSION,
            "shape": list(self.shape),
            "dtype": "float32",
            "source_checksum": source_checksum,
            "extractor_version": extractor_version,
        }
        self.written = 0

        if self.part.exists() or self.checkpoint.exists():
            self._resume()
        else:
            self.part.parent.mkdir(parents=True, exist_ok=True)
            self.output = np.lib.format.open_memmap(
                self.part, mode="w+", dtype="float32", shape=self.shape)
            self._write_checkpoint()

    def _resume(self) -> None:
        if not self.part.exists() or not self.checkpoint.exists():
            raise CheckpointError(
                f"incomplete feature checkpoint for {self.dest}; preserve it and reset explicitly")
        try:
            saved = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CheckpointError(f"corrupt feature checkpoint {self.checkpoint}; reset explicitly") from error
        expected = {**self._identity, "written": saved.get("written")}
        if saved != expected or not isinstance(saved["written"], int) or not 0 <= saved["written"] <= self.shape[0]:
            raise CheckpointError(
                f"incompatible feature checkpoint for {self.dest}; preserve it and reset explicitly")
        try:
            self.output = np.lib.format.open_memmap(self.part, mode="r+")
        except (OSError, ValueError) as error:
            raise CheckpointError(f"unreadable feature data {self.part}; reset explicitly") from error
        if self.output.dtype != np.float32 or self.output.shape != self.shape:
            raise CheckpointError(
                f"feature data shape differs from checkpoint for {self.dest}; reset explicitly")
        self.written = saved["written"]

    def _write_checkpoint(self) -> None:
        payload = {**self._identity, "written": self.written}
        temp = self.checkpoint.with_suffix(self.checkpoint.suffix + ".tmp")
        temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(self.checkpoint)

    def append(self, features) -> None:
        features = np.asarray(features, dtype="float32")
        end = self.written + len(features)
        if features.shape[1:] != self.shape[1:] or end > self.shape[0]:
            raise ValueError("feature batch does not fit checkpoint shape")
        self.output[self.written:end] = features
        self.output.flush()
        self.written = end
        self._write_checkpoint()

    def complete(self) -> None:
        if self.written != self.shape[0]:
            raise CheckpointError(f"cannot complete {self.dest}: {self.written}/{self.shape[0]} rows")
        self.output.flush()
        self.part.replace(self.dest)
        self.checkpoint.unlink()
