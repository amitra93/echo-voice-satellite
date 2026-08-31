"""
Tests for forge._stream_feature_configs — the shared FLEURS/VoxPopuli feature
builder.

Reproduces the bug found in production job logs: FLEURS' `fr_fr` config threw
`pyarrow.lib.ArrowNotImplementedError` from inside the HF streaming iterator
(not a per-clip decode error, which the existing inner try/except already
handled) and that exception propagated out of the whole function — discarding
every clip already embedded from EARLIER configs (`en_us`, `es_419` had
already streamed 2,000 clips each) and leaving the destination `.npy` file
never written, with no partial file either. Every observed FLEURS build
attempt failed this way.

The fix isolates each config's read behind its own try/except so one bad
shard is skipped — with a logged warning naming it — while clips already
gathered from other configs are kept. These tests fake out `datasets` and the
feature embedder (both heavy/network/GPU dependencies unavailable in this
test environment) to exercise exactly that control flow.
"""

import sys
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1]))

import forge  # noqa: E402


class FakeDataset:
    """A minimal stand-in for a streaming HF `datasets.IterableDataset`."""

    def __init__(self, rows, fail_immediately=False, error=None):
        self._rows = rows
        self._fail_immediately = fail_immediately
        self._error = error or RuntimeError(
            "pyarrow.lib.ArrowNotImplementedError: Nested data conversions "
            "not implemented for chunked array outputs"
        )

    def __iter__(self):
        if self._fail_immediately:
            # Matches what was observed on hardware: the crash happened on the
            # very first row, right after "Resolving data files" completed —
            # no clips for that config were ever readable.
            raise self._error
        yield from self._rows


def _row(n_samples=32000):
    return {"audio": {"array": np.zeros(n_samples, dtype="float32"), "sampling_rate": 16000}}


class FakeEmbedder:
    """`embed_clips` just has to return one row per input clip."""

    def embed_clips(self, clips, batch_size=None, ncpu=None):
        return np.ones((len(clips), 1), dtype="float32")


class StreamFeatureConfigsTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules_datasets = sys.modules.get("datasets")
        self._orig_modules_librosa = sys.modules.get("librosa")
        self._orig_feature_extractor = forge._feature_extractor
        self._orig_features_dir = forge.FEATURES_DIR
        forge._feature_extractor = lambda backend, ncpu: FakeEmbedder()
        # Pin these tests to the LEGACY streaming path: the default local-first
        # prefetch path would attempt a real hf_hub_download (network) wherever
        # huggingface_hub is installed, and is covered by its own tests below.
        self._orig_fetch_mode = None
        if "FORGE_FETCH_MODE" in __import__("os").environ:
            self._orig_fetch_mode = __import__("os").environ["FORGE_FETCH_MODE"]
        __import__("os").environ["FORGE_FETCH_MODE"] = "stream"
        self._log_lines = []
        self._orig_log = forge.log
        forge.log = lambda msg: self._log_lines.append(msg)
        # _audio_16k_clip does an unconditional `import librosa`, even though
        # it is only USED when resampling (sr != 16000); every fake clip here
        # is already 16kHz, so a bare stub — never called — is enough, and
        # keeps this test independent of whether librosa happens to be
        # installed in whatever environment runs it.
        if "librosa" not in sys.modules:
            sys.modules["librosa"] = types.ModuleType("librosa")
            self._added_librosa_stub = True
        else:
            self._added_librosa_stub = False

    def tearDown(self):
        if self._orig_modules_datasets is None:
            sys.modules.pop("datasets", None)
        else:
            sys.modules["datasets"] = self._orig_modules_datasets
        if self._added_librosa_stub:
            sys.modules.pop("librosa", None)
        forge._feature_extractor = self._orig_feature_extractor
        forge.log = self._orig_log
        forge.FEATURES_DIR = self._orig_features_dir
        import os
        if self._orig_fetch_mode is None:
            os.environ.pop("FORGE_FETCH_MODE", None)
        else:
            os.environ["FORGE_FETCH_MODE"] = self._orig_fetch_mode

    def _fake_datasets_module(self, rows_by_config):
        fake = types.ModuleType("datasets")

        def load_dataset(repo, name, split, streaming):
            return rows_by_config[name]

        fake.load_dataset = load_dataset
        sys.modules["datasets"] = fake

    def test_a_bad_config_is_skipped_and_earlier_chunks_are_kept(self):
        self._fake_datasets_module({
            "good1": FakeDataset([_row(), _row()]),
            "bad":   FakeDataset([], fail_immediately=True),
            "good2": FakeDataset([_row()]),
        })
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.npy"
            forge.FEATURES_DIR = Path(tmp)
            forge._stream_feature_configs(
                "fake/repo", ("good1", "bad", "good2"), dest, "TestAsset",
                batch_size=2,
            )
            self.assertTrue(dest.exists(), "feature file must still be written "
                            "when only SOME configs fail")
            saved = np.load(dest)
            # 2 (good1) + 1 (good2) = 3 clips survived; "bad" contributed none.
            self.assertEqual(saved.shape[0], 3)

        skip_lines = [l for l in self._log_lines if "bad" in l and "skipped" in l.lower()]
        self.assertTrue(skip_lines, f"expected a skip warning naming 'bad', got: {self._log_lines}")
        summary = [l for l in self._log_lines if l.startswith("TestAsset: skipped")]
        self.assertEqual(summary, ["TestAsset: skipped 1/3 config(s) that could not be read: bad"])

    def test_a_config_failing_mid_stream_still_keeps_its_own_earlier_batches(self):
        # "flaky" yields two full rows (one batch of size 2, embedded and
        # appended to chunks) and THEN raises — the already-embedded batch
        # from this same config must survive, only the unflushed remainder is
        # lost.
        class FlakyThenFail(FakeDataset):
            def __iter__(self):
                yield _row()
                yield _row()
                raise RuntimeError("stream dropped mid-config")

        self._fake_datasets_module({"flaky": FlakyThenFail([])})
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.npy"
            forge.FEATURES_DIR = Path(tmp)
            forge._stream_feature_configs(
                "fake/repo", ("flaky",), dest, "TestAsset", batch_size=2,
            )
            saved = np.load(dest)
            self.assertEqual(saved.shape[0], 2)

    def test_every_config_failing_exits_rather_than_writing_an_empty_file(self):
        self._fake_datasets_module({
            "bad1": FakeDataset([], fail_immediately=True),
            "bad2": FakeDataset([], fail_immediately=True),
        })
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.npy"
            forge.FEATURES_DIR = Path(tmp)
            with self.assertRaises(SystemExit):
                forge._stream_feature_configs(
                    "fake/repo", ("bad1", "bad2"), dest, "TestAsset", batch_size=2,
                )
            self.assertFalse(dest.exists())

    def test_dest_already_present_is_a_no_op(self):
        # Locks in existing resumability: a call that finds the destination
        # already on disk must not touch `datasets` at all.
        self._fake_datasets_module({})  # no configs registered — would KeyError if touched
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.npy"
            dest.write_bytes(b"already here")
            forge._stream_feature_configs(
                "fake/repo", ("whatever",), dest, "TestAsset", batch_size=2,
            )
            self.assertEqual(dest.read_bytes(), b"already here")


class LocalPrefetchPathTests(unittest.TestCase):
    """The default FORGE_FETCH_MODE=local path: hf_hub_download → local parquet."""

    def setUp(self):
        self._orig_modules_datasets = sys.modules.get("datasets")
        self._orig_modules_librosa = sys.modules.get("librosa")
        self._orig_feature_extractor = forge._feature_extractor
        self._orig_features_dir = forge.FEATURES_DIR
        self._orig_prefetch = forge._prefetch_shard
        forge._feature_extractor = lambda backend, ncpu: FakeEmbedder()
        if "librosa" not in sys.modules:
            sys.modules["librosa"] = types.ModuleType("librosa")
            self._added_librosa_stub = True
        else:
            self._added_librosa_stub = False
        self._log_lines = []
        self._orig_log = forge.log
        forge.log = lambda msg: self._log_lines.append(msg)

    def tearDown(self):
        if self._orig_modules_datasets is None:
            sys.modules.pop("datasets", None)
        else:
            sys.modules["datasets"] = self._orig_modules_datasets
        if self._orig_modules_librosa is None:
            sys.modules.pop("librosa", None)
        else:
            sys.modules["librosa"] = self._orig_modules_librosa
        if getattr(self, "_added_librosa_stub", False):
            sys.modules.pop("librosa", None)
        forge._feature_extractor = self._orig_feature_extractor
        forge._prefetch_shard = self._orig_prefetch
        forge.log = self._orig_log
        forge.FEATURES_DIR = self._orig_features_dir

    def test_multi_shard_configs_chain_rows_across_files(self):
        # VoxPopuli layout: MULTIPLE shards per split at repo root. Rows must
        # stream across both files in order without dropping any.
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as e:
            self.skipTest(f"pyarrow unavailable: {e}")
        import tempfile

        def make_shard(rows_bytes):
            f = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
            pq.write_table(pa.table({"audio": pa.array(
                [{"bytes": b, "path": f"{i}.wav"} for i, b in enumerate(rows_bytes)])}), f.name)
            return Path(f.name)

        shard_a = make_shard([b"a1", b"a2"])
        shard_b = make_shard([b"b1"])
        forge._prefetch_shard = lambda repo, config, split: [shard_a, shard_b]

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.npy"
            forge.FEATURES_DIR = Path(tmp)
            forge._stream_feature_configs(
                "fake/repo", ("c1",), dest, "TestAsset", batch_size=2,
            )
            saved = np.load(dest)
            self.assertEqual(saved.shape[0], 3)
        self.assertTrue(any("2 shard(s)" in l for l in self._log_lines))

    def test_prefetch_failure_falls_back_to_streaming_and_still_builds(self):
        calls = {"prefetch": 0}

        def boom(repo, config, split):
            calls["prefetch"] += 1
            raise RuntimeError("simulated hub outage")

        forge._prefetch_shard = boom
        fake = types.ModuleType("datasets")

        def load_dataset(repo, name, split, streaming):
            assert streaming is True  # fallback must use the streaming path
            return FakeDataset([_row(), _row()])

        fake.load_dataset = load_dataset
        sys.modules["datasets"] = fake

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.npy"
            forge.FEATURES_DIR = Path(tmp)
            forge._stream_feature_configs(
                "fake/repo", ("c1",), dest, "TestAsset", batch_size=2,
            )
            self.assertEqual(calls["prefetch"], 1)
            self.assertEqual(np.load(dest).shape[0], 2)
        self.assertTrue(any("falling back to streaming" in l for l in self._log_lines))


class RowToClipTests(unittest.TestCase):
    def setUp(self):
        self._orig_decode = forge._decode_audio_bytes
        # _audio_16k_clip imports librosa unconditionally (used only for
        # resampling; these fixtures are already 16kHz).
        if "librosa" not in sys.modules:
            sys.modules["librosa"] = types.ModuleType("librosa")
            self._added_librosa_stub = True
        else:
            self._added_librosa_stub = False

    def tearDown(self):
        forge._decode_audio_bytes = self._orig_decode
        if self._added_librosa_stub:
            sys.modules.pop("librosa", None)

    def test_streaming_rows_pass_their_array_through(self):
        arr = np.zeros(32000, dtype="float32")
        clip = forge._row_to_clip({"audio": {"array": arr, "sampling_rate": 16000}})
        expected = forge._audio_16k_clip(arr, 16000)
        np.testing.assert_array_equal(clip, expected)

    def test_parquet_rows_route_bytes_through_the_decoder(self):
        seen = {}

        def fake_decode(raw, name):
            seen["raw"], seen["name"] = raw, name
            return np.full(32000, 0.5, dtype="float32"), 16000

        forge._decode_audio_bytes = fake_decode
        clip = forge._row_to_clip({"audio": {"bytes": b"WAVDATA", "path": "x.wav"}})
        self.assertEqual(seen["raw"], b"WAVDATA")
        self.assertEqual(seen["name"], "x.wav")
        expected = forge._audio_16k_clip(
            np.full(32000, 0.5, dtype="float32"), 16000)
        np.testing.assert_array_equal(clip, expected)


class LocalParquetEndToEndTests(unittest.TestCase):
    """_iter_parquet_audio_rows against a real parquet — image-only (pyarrow)."""

    def test_reads_audio_struct_column_as_rows(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as e:
            self.skipTest(f"pyarrow unavailable: {e}")
        import tempfile

        audio_col = pa.array([{"bytes": b"aaa", "path": "a.wav"},
                              {"bytes": b"bbb", "path": "b.wav"}])
        table = pa.table({"audio": audio_col})
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "shard.parquet"
            pq.write_table(table, p)
            rows = list(forge._iter_parquet_audio_rows(p))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["audio"]["bytes"], b"aaa")
        self.assertEqual(rows[1]["audio"]["path"], "b.wav")


if __name__ == "__main__":
    unittest.main()
