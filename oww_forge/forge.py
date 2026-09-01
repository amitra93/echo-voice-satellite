#!/usr/bin/env python3
"""oww_forge — train custom openWakeWord models for EchoMuse.

Runs inside the oww-forge container (see Dockerfile / docker-compose.yml).
Everything persistent lives on the /data volume:

    /data/assets/       shared training assets (downloaded once, ~25GB)
    /data/wakewords/    one directory per wake word (config + clips + features)
    /data/models/       finished .onnx models, ready to install

Typical flow:

    forge.py assets                    # one-time, ~25GB of downloads
    forge.py new "hey biscuit"         # writes wakewords/hey_biscuit/config.yml
    forge.py google-tts hey_biscuit    # OPTIONAL extra positives via Google TTS
    forge.py build hey_biscuit         # generate → augment → train → models/hey_biscuit.onnx
    forge.py test hey_biscuit --wav some_recording.wav
"""

import argparse
import hashlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
import os
import re
import random
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from time import monotonic, sleep

DATA = Path(os.environ.get("FORGE_DATA", "/data"))
ASSETS = DATA / "assets"
WAKEWORDS = DATA / "wakewords"
MODELS = DATA / "models"
CREDENTIALS = DATA / "credentials.json"
FORGE_DIR = Path(__file__).parent
TRAIN_PY = "/opt/openwakeword/openwakeword/train.py"
FORGE_VERSION = "1"
MODEL_KINDS = ("wake", "stop")
STOP_DEFAULT_PHRASE = "stop"
STOP_DEFAULT_CONFUSABLES = "top,shop,drop,start,don't"

PIPER_CKPT = ASSETS / "piper" / "en_US-libritts_r-medium.pt"
PIPER_CKPT_URL = (
    "https://github.com/rhasspy/piper-sample-generator/releases/download/"
    "v2.0.0/en_US-libritts_r-medium.pt"
)
# voice config expected at <ckpt>.json; lives in the repo (which the
# Dockerfile replaces with the /data symlink), not in the release assets
PIPER_CKPT_JSON_URL = (
    "https://raw.githubusercontent.com/rhasspy/piper-sample-generator/"
    "195e3bd967d54589c2137c9de2b22ad526ba6b6f/models/en_US-libritts_r-medium.pt.json"
)
FEATURES_DIR = ASSETS / "features"
HF_FEATURES_REPO = "davidscripka/openwakeword_features"
NEGATIVE_FEATURES = "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
VALIDATION_FEATURES = "validation_set_features.npy"
# Full Common Voice 26 English is 88.14GB. Keeping its archive, generated
# features, and the core forge assets requires roughly 150GB of storage.
COMMON_VOICE_FEATURES = "common_voice_26_en_features.npy"
COMMON_VOICE_DIR = ASSETS / "common_voice_26_en"
COMMON_VOICE_DATASET_ID = "cmqim2hn800ssnr07gvmpcnwu"
AMI_VALIDATION_FEATURES = "ami_sdm_validation_features.npy"
AMI_REPO = "edinburghcstr/ami"
AMI_CONFIG = "sdm"
FLEURS_FEATURES = "fleurs_multilingual_features.npy"
FLEURS_CONFIGS = ("en_us", "es_419", "fr_fr", "de_de", "pt_br", "hi_in", "ar_eg", "ja_jp", "ko_kr", "cmn_hans_cn")
VOXPOPULI_FEATURES = "voxpopuli_accent_features.npy"
VOXPOPULI_CONFIGS = ("en_accented", "de", "fr", "es", "pl", "it", "nl", "ro", "cs")
FEATURE_CLIPS_PER_CONFIG = 2_000
MUSAN_DIR = ASSETS / "musan_16k"
MUSAN_ARCHIVE = ASSETS / "musan.tar.gz"
MUSAN_URL = "https://openslr.trmal.net/resources/17/musan.tar.gz"
SLR28_DIR = ASSETS / "slr28"
SLR28_ARCHIVE = ASSETS / "rirs_noises.zip"
SLR28_URL = "https://openslr.trmal.net/resources/28/rirs_noises.zip"
SLR28_RIR_DIR = SLR28_DIR / "rirs"
SLR28_NOISE_DIR = SLR28_DIR / "noise"
# Keep the feature batch at its current 1024 examples. Optional sources
# replace ACAV draws instead of silently making negatives dominate positives.
OPTIONAL_NEGATIVE_BATCH = {"CommonVoice_en": 128, "FLEURS": 96, "VoxPopuli": 96}
FEATURE_CLIP_SAMPLES = 32_000
FEATURE_NCPU = 6
FEATURE_BATCH_SIZE = 256
DECODE_WORKERS = 2
LOG_PROGRESS_INTERVAL = 10.0
RIR_DIR = ASSETS / "mit_rirs"
AUDIOSET_DIR = ASSETS / "audioset_16k"
# agkphysics/AudioSet stores bal_train as ~40 parquet shards (the old
# bal_trainNN.tar files the openWakeWord notebook used are gone). A handful
# of shards yields the ~2k clips the notebook worked with.
AUDIOSET_PARQUET_URLS = [
    f"https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train/{i:02d}.parquet"
    for i in range(0, 4)
]
FMA_DIR = ASSETS / "fma_16k"

ASSET_PARTS = ["piper", "features", "common_voice", "common_voice_features", "ami", "fleurs", "voxpopuli", "musan", "slr28", "rirs", "audioset", "fma"]


def log(msg: str) -> None:
    print(f"[forge] {msg}", flush=True)


def _duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:
        return "ETA unavailable"
    seconds = int(seconds)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _progress(label: str, done: int, total: int | None, started: float,
              last_log: float, force: bool = False) -> float:
    """Log bounded progress output with a rate and ETA, returning log time."""
    now = monotonic()
    if not force and now - last_log < LOG_PROGRESS_INTERVAL:
        return last_log
    elapsed = max(now - started, 0.001)
    rate = done / elapsed
    eta = (total - done) / rate if total and rate > 0 else None
    suffix = f"/{total:,}" if total else ""
    log(f"{label}: {done:,}{suffix} at {rate:,.1f}/s; {_duration(eta)}")
    return now


def mdc_api_key() -> str | None:
    """Return the saved MDC key without ever logging it."""
    try:
        saved = json.loads(CREDENTIALS.read_text()).get("mdc_api_key")
    except (OSError, ValueError, TypeError):
        saved = None
    return saved or os.environ.get("MDC_API_KEY") or None


def masked_mdc_api_key() -> str | None:
    key = mdc_api_key()
    if not key:
        return None
    return "*" * min(len(key), 16)


def save_mdc_api_key(key: str) -> None:
    """Persist the key privately in the Forge volume for background jobs."""
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"mdc_api_key": key}, indent=2) + "\n"
    fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
    finally:
        os.chmod(CREDENTIALS, 0o600)


def slugify(phrase: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


def download(url: str, dest: Path) -> None:
    """Plain HTTP download with a .part temp file so interrupts don't leave
    a truncated file that idempotency checks would then treat as complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    log(f"downloading {url}")

    started = monotonic()
    last_log = started

    def hook(blocks, block_size, total):
        nonlocal last_log
        done = blocks * block_size
        last_log = _progress("download", done, total if total > 0 else None,
                             started, last_log)

    urllib.request.urlretrieve(url, part, reporthook=hook)
    part.rename(dest)
    log(f"  → {dest} ({dest.stat().st_size / 1e6:.0f} MB)")


def write_wav_16k(dest: Path, audio, sr: int) -> None:
    import librosa
    import numpy as np
    import soundfile as sf

    audio = np.asarray(audio, dtype="float32")
    if audio.ndim > 1:  # stereo → mono
        audio = audio.mean(axis=1)
    if sr != 16000:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    sf.write(dest, audio, 16000, subtype="PCM_16")


def dir_has_files(path: Path, pattern: str = "*") -> bool:
    return path.is_dir() and next(path.glob(pattern), None) is not None


# ---------------------------------------------------------------- assets

def fetch_piper() -> None:
    if PIPER_CKPT.exists():
        log(f"piper checkpoint present: {PIPER_CKPT}")
    else:
        download(PIPER_CKPT_URL, PIPER_CKPT)
    ckpt_json = PIPER_CKPT.with_suffix(".pt.json")
    if not ckpt_json.exists():
        download(PIPER_CKPT_JSON_URL, ckpt_json)


def fetch_features() -> None:
    from huggingface_hub import hf_hub_download

    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    for fname in (NEGATIVE_FEATURES, VALIDATION_FEATURES):
        if (FEATURES_DIR / fname).exists():
            log(f"features present: {fname}")
            continue
        log(f"downloading {fname} (resumable — rerun on interrupt; the negative set is ~17GB)")
        hf_hub_download(
            repo_id=HF_FEATURES_REPO,
            filename=fname,
            repo_type="dataset",
            local_dir=FEATURES_DIR,
        )
        log(f"  → {FEATURES_DIR / fname}")


def fetch_common_voice() -> None:
    """Download the complete official Common Voice 26 English archive.

    Mozilla requires acceptance of the data terms and an API key. Its SDK
    retains resumable download state under /data rather than container cache.
    Feature extraction is intentionally a separate command because processing
    1.9m validated clips is a multi-day CPU task.
    """
    key = mdc_api_key()
    if not key:
        sys.exit("Common Voice needs MDC_API_KEY. Accept its terms and create a key at "
                 "https://mozilladatacollective.com/api-reference, then save it in Forge settings.")
    try:
        from datacollective import download_dataset, get_dataset_details
    except ImportError:
        sys.exit("the datacollective package is unavailable; rebuild the forge image")
    os.environ["MDC_API_KEY"] = key
    part = next(iter(COMMON_VOICE_DIR.glob("*.tar.gz.part")), None)
    initial_bytes = part.stat().st_size if part else 0
    try:
        details = get_dataset_details(COMMON_VOICE_DATASET_ID)
        total_bytes = int(details.sizeBytes)
        target = COMMON_VOICE_DIR / details.filename
    except Exception as error:
        details = None
        total_bytes = None
        target = None
        log(f"Common Voice archive: total size unavailable ({error})")

    if target is not None and target.exists():
        size = target.stat().st_size
        log(f"Common Voice archive: already complete ({size / 1e9:.2f}/"
            f"{total_bytes / 1e9:.2f} GB, 100%)")
        return

    log("Common Voice archive: downloading; progress refreshes every 60s")
    if total_bytes:
        log(f"Common Voice archive: {initial_bytes / 1e9:.2f}/{total_bytes / 1e9:.2f} GB "
            f"({initial_bytes / total_bytes:.1%}); ETA pending")

    # The SDK owns the resumable HTTP transfer. Run it in a worker while this
    # process polls the .part file, so the UI receives useful progress without
    # replacing the SDK's checksum/resume behavior.
    from concurrent.futures import ThreadPoolExecutor

    started = monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(download_dataset, COMMON_VOICE_DATASET_ID,
                             download_directory=str(COMMON_VOICE_DIR),
                             show_progress=False)
        last_log = 0.0
        while not future.done():
            now = monotonic()
            if now - last_log >= 60 or last_log == 0.0:
                part = next(iter(COMMON_VOICE_DIR.glob("*.tar.gz.part")), None)
                done = part.stat().st_size if part else initial_bytes
                elapsed = max(now - started, 0.001)
                rate = max(done - initial_bytes, 0) / elapsed
                eta = ((total_bytes - done) / rate) if total_bytes and rate > 0 else None
                if total_bytes:
                    log(f"Common Voice archive: {done / 1e9:.2f}/{total_bytes / 1e9:.2f} GB "
                        f"({done / total_bytes:.1%}); {rate / 1e6:.1f} MB/s; {_duration(eta)}")
                else:
                    log(f"Common Voice archive: {done / 1e9:.2f} GB; "
                        f"{rate / 1e6:.1f} MB/s; ETA unavailable")
                last_log = now
            sleep(1)
        archive = future.result()
    if total_bytes:
        log(f"Common Voice archive: complete ({total_bytes / 1e9:.2f} GB)")
    log(f"  → {archive}")


def _audio_16k_clip(audio, sr: int):
    import librosa
    import numpy as np

    audio = np.asarray(audio, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=-1)
    if sr != 16000:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    audio = audio[:FEATURE_CLIP_SAMPLES]
    if len(audio) < FEATURE_CLIP_SAMPLES:
        audio = np.pad(audio, (0, FEATURE_CLIP_SAMPLES - len(audio)))
    return (np.clip(audio, -1, 1) * 32767).astype("int16")


def _decode_audio_bytes(raw: bytes, name: str):
    """Encoded audio container bytes → (float32 array, sample rate).

    datasets-server's parquet exports store the ORIGINAL container bytes in an
    `audio.bytes` column (wav for FLEURS, ogg/opus for VoxPopuli). soundfile /
    libsndfile 1.2 reads all of those directly; ffmpeg is the fallback for
    anything it rejects.
    """
    import io

    import soundfile as sf

    try:
        data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
        return data, sr
    except Exception:
        import subprocess
        import tempfile

        suffix = ("." + name.rsplit(".", 1)[-1] if "." in name else ".ogg")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / f"in{suffix}"
            dst = Path(tmp) / "out.wav"
            src.write_bytes(raw)
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                 "-ar", "16000", "-ac", "1", "-f", "wav", str(dst)],
                check=True, timeout=120,
            )
            data, sr = sf.read(str(dst), dtype="float32", always_2d=False)
            return data, sr


def _row_to_clip(row) -> "np.ndarray":
    """One dataset row → one int16 16k mono clip, whatever shape the row is.

    Streaming rows carry decoded arrays (`audio.array` + `.sampling_rate`);
    locally-prefetched parquet rows carry raw container bytes (`audio.bytes`).
    """
    a = row["audio"]
    if isinstance(a, dict) and a.get("bytes") is not None:
        arr, sr = _decode_audio_bytes(a["bytes"], a.get("path") or "clip")
    else:
        arr, sr = a["array"], a["sampling_rate"]
    return _audio_16k_clip(arr, sr)


def _iter_parquet_audio_rows(path: Path):
    """Yield `{"audio": {"bytes": …}}` rows from a local parquet shard."""
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(str(path))
    names = pf.schema_arrow.names
    acol = "audio" if "audio" in names else names[0]
    for batch in pf.iter_batches(batch_size=64, columns=[acol]):
        col = batch.column(0)
        for i in range(len(col)):
            yield {"audio": col[i].as_py()}


def _prefetch_shard(repo: str, config: str, split: str) -> list[Path]:
    """Download this config's converted parquet shards to the hub cache.

    Discovers shard filenames via the Hub API rather than guessing a pattern:
    the two repos this serves lay them out differently — google/fleurs nests
    single files under `parquet-data/<config>/<split>-00000-of-00001.parquet`,
    while facebook/voxpopuli puts MULTI-shard splits at the repo root
    (`en_accented/test-00000-of-00002.parquet` + `-of-00001-`). Guessing got a
    404 on VoxPopuli and silently cost it the fast path (observed 2026-08-23).

    Uses hf_hub_download per shard, so each honours HF_HUB_ENABLE_HF_TRANSFER=1
    (multi-connection Rust downloader). Returns shards in filename order so row
    order matches what streaming would have yielded. Raises if nothing matches;
    the caller falls back to streaming.
    """
    import re

    from huggingface_hub import HfApi, hf_hub_download

    files = HfApi().list_repo_files(repo_id=repo, repo_type="dataset")
    pat = re.compile(
        rf"^(?:parquet-data/)?{re.escape(config)}/{re.escape(split)}-\d+-of-\d+\.parquet$")
    wanted = sorted(f for f in files if pat.match(f))
    if not wanted:
        raise FileNotFoundError(
            f"no {config}/{split}-*.parquet shards found in dataset {repo}")
    return [Path(hf_hub_download(repo_id=repo, repo_type="dataset", filename=f))
            for f in wanted]


def iter_decoded_audio_members(archive: Path, wanted_names: set[str] | None = None,
                               max_workers: int = DECODE_WORKERS):
    """Yield decoded clips with a bounded decode queue while reading a tar.

    Tar members are read in archive order by one reader. At most twice the
    worker count is buffered, preventing decoder threads from racing ahead and
    retaining an unbounded amount of compressed audio in memory.
    """
    import io
    import tarfile

    import soundfile as sf

    def decode_member(item):
        name, payload = item
        try:
            audio, sr = sf.read(io.BytesIO(payload), dtype="float32")
            return name, _audio_16k_clip(audio, sr), None
        except Exception as error:
            return name, None, error

    def members():
        with tarfile.open(archive, "r|gz") as tar:
            for member in tar:
                if not member.isfile() or not member.name.lower().endswith(
                        (".mp3", ".wav", ".flac", ".ogg")):
                    continue
                if wanted_names is not None and Path(member.name).name not in wanted_names:
                    continue
                source = tar.extractfile(member)
                if source is not None:
                    yield member.name, source.read()

    pending = deque()
    exhausted = False
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        member_iter = iter(members())
        while pending or not exhausted:
            while not exhausted and len(pending) < max_workers * 2:
                try:
                    pending.append(pool.submit(decode_member, next(member_iter)))
                except StopIteration:
                    exhausted = True
            if not pending:
                continue
            yield pending.popleft().result()


def _feature_extractor(backend: str, ncpu: int = FEATURE_NCPU):
    """Create and announce the one feature backend used by an asset job."""
    from torch_features import make_feature_extractor

    extractor = make_feature_extractor(backend, ncpu=ncpu)
    details = extractor.describe()
    log(f"feature backend: {details['backend']} on {details['device']} ({details['model_version']})")
    return extractor


def _stream_feature_configs(repo: str, configs: tuple[str, ...], dest: Path,
                             label: str, backend: str = "onnx",
                             ncpu: int = FEATURE_NCPU,
                             batch_size: int = FEATURE_BATCH_SIZE) -> None:
    """Build a bounded, balanced feature file from Hugging Face configs.

    Each language/accent gets the same cap, preventing large English or
    European subsets from erasing the phonetic diversity this asset adds.
    """
    import datasets
    import numpy as np

    if dest.exists():
        log(f"{label} features present: {dest}")
        return
    embedder = _feature_extractor(backend, ncpu)
    chunks = []
    skipped = []
    overall_started = monotonic()
    for config_index, config in enumerate(configs, start=1):
        log(f"streaming {label} {config} (up to {FEATURE_CLIPS_PER_CONFIG:,} clips)")
        batch, count, config_started, last_log = [], 0, monotonic(), monotonic()
        split = "test" if repo == "facebook/voxpopuli" and config == "en_accented" else "train"
        # One bad shard must not cost every clip already streamed from EARLIER
        # configs. Observed on hardware: FLEURS fr_fr throws
        # pyarrow.lib.ArrowNotImplementedError ("Nested data conversions not
        # implemented for chunked array outputs") from inside the HF streaming
        # iterator itself — not a per-clip decode error the inner try/except
        # already covers, but a config-level failure to even read the shard.
        # Before this fix that exception propagated out of the whole function,
        # so a single unreadable language discarded every already-embedded
        # chunk and the feature file was NEVER written — every FLEURS build
        # attempt failed this way and none left so much as a partial file.
        # The fix is to isolate each config's read behind its own try/except:
        # skip it, keep what already streamed, and continue — a smaller,
        # imbalanced-but-real feature file beats losing the whole asset to one
        # config's incompatibility.
        #
        # Fetch mode: by default each config's parquet is PREFETCHED via
        # hf_hub_download (multi-connection via hf_transfer, resumable), then
        # read locally — single-stream streaming measured 0.1–4 MB/s on the
        # us.aws.cdn.hf.co route and restarted from zero after every timeout.
        # Any prefetch problem (odd sharding, hub error) falls back to the old
        # streaming path below; FORGE_FETCH_MODE=stream forces it outright.
        try:
            rows_iter = None
            if os.environ.get("FORGE_FETCH_MODE", "local") != "stream":
                try:
                    _t0 = monotonic()
                    _shards = _prefetch_shard(repo, config, split)
                    _mb = sum(p.stat().st_size for p in _shards) / 1e6
                    log(f"  {label} {config}: {_mb:.0f} MB of parquet "
                        f"fetched locally ({len(_shards)} shard(s)) in "
                        f"{_duration(monotonic() - _t0)}")
                    # Chain shards in filename order — row order then matches
                    # what streaming would have yielded.
                    rows_iter = itertools.chain.from_iterable(
                        _iter_parquet_audio_rows(p) for p in _shards)
                except Exception as error:
                    log(f"  {label} {config}: local prefetch unavailable "
                        f"({error}) — falling back to streaming")
            if rows_iter is None:
                ds = datasets.load_dataset(repo, name=config, split=split, streaming=True)
                rows_iter = iter(ds)
            for row in rows_iter:
                try:
                    batch.append(_row_to_clip(row))
                except Exception as error:
                    log(f"  skipping unreadable {config} clip: {error}")
                    continue
                count += 1
                last_log = _progress(f"{label} {config} [{config_index}/{len(configs)}]",
                                     count, FEATURE_CLIPS_PER_CONFIG, config_started, last_log)
                if len(batch) == batch_size:
                    chunks.append(embedder.embed_clips(np.stack(batch), batch_size=batch_size, ncpu=ncpu))
                    batch = []
                if count >= FEATURE_CLIPS_PER_CONFIG:
                    break
            if batch:
                chunks.append(embedder.embed_clips(np.stack(batch), batch_size=batch_size, ncpu=ncpu))
            _progress(f"{label} {config} [{config_index}/{len(configs)}]", count,
                      FEATURE_CLIPS_PER_CONFIG, config_started, last_log, force=True)
        except Exception as error:
            log(f"  {label} {config} could not be read and is being skipped "
                f"(clips already gathered from other configs are kept): {error}")
            skipped.append(config)
            continue
    if skipped:
        log(f"{label}: skipped {len(skipped)}/{len(configs)} config(s) that "
            f"could not be read: {', '.join(skipped)}")
    if not chunks:
        sys.exit(f"{label} yielded no readable audio")
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with open(part, "wb") as f:
        np.save(f, np.concatenate(chunks, axis=0))
    part.rename(dest)
    log(f"  → {dest} ({dest.stat().st_size / 1e6:.0f} MB)")


def fetch_fleurs(backend: str = "onnx") -> None:
    _stream_feature_configs("google/fleurs", FLEURS_CONFIGS,
                            FEATURES_DIR / FLEURS_FEATURES, "FLEURS", backend)


def fetch_voxpopuli(backend: str = "onnx") -> None:
    _stream_feature_configs("facebook/voxpopuli", VOXPOPULI_CONFIGS,
                            FEATURES_DIR / VOXPOPULI_FEATURES, "VoxPopuli", backend)


def fetch_musan() -> None:
    import tarfile

    if dir_has_files(MUSAN_DIR, "*.wav"):
        log(f"MUSAN clips present: {MUSAN_DIR}")
        return
    if not MUSAN_ARCHIVE.exists():
        download(MUSAN_URL, MUSAN_ARCHIVE)
    log("extracting MUSAN audio")
    started, last_log, count = monotonic(), monotonic(), 0
    with tarfile.open(MUSAN_ARCHIVE, "r:gz") as archive:
        for member in archive:
            if member.isfile() and member.name.endswith(".wav"):
                target = MUSAN_DIR / member.name.replace("/", "_")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source:
                    target.write_bytes(source.read())
                    count += 1
                    last_log = _progress("MUSAN extraction", count, None,
                                         started, last_log)
    _progress("MUSAN extraction", count, None, started, last_log, force=True)
    log(f"  → {_count_wavs(MUSAN_DIR)} clips in {MUSAN_DIR}")


def _count_wavs(path: Path) -> int:
    return sum(1 for _ in path.glob("*.wav")) if path.is_dir() else 0


def fetch_slr28() -> None:
    import zipfile

    if dir_has_files(SLR28_RIR_DIR, "*.wav") and dir_has_files(SLR28_NOISE_DIR, "*.wav"):
        log(f"SLR28 audio present: {SLR28_DIR}")
        return
    if not SLR28_ARCHIVE.exists():
        download(SLR28_URL, SLR28_ARCHIVE)
    log("extracting OpenSLR 28 RIRs and noise")
    with zipfile.ZipFile(SLR28_ARCHIVE) as archive:
        names = [name for name in archive.namelist() if name.endswith(".wav")]
    started, last_log = monotonic(), monotonic()
    with zipfile.ZipFile(SLR28_ARCHIVE) as archive:
        for count, name in enumerate(names, start=1):
            if not name.endswith(".wav"):
                continue
            target_dir = SLR28_RIR_DIR if "/RIRS_" in name or "/simulated_rirs/" in name else SLR28_NOISE_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / name.replace("/", "_")).write_bytes(archive.read(name))
            last_log = _progress("OpenSLR 28 extraction", count, len(names),
                                 started, last_log)
    _progress("OpenSLR 28 extraction", len(names), len(names), started, last_log, force=True)
    log(f"  → {_count_wavs(SLR28_RIR_DIR)} RIRs + {_count_wavs(SLR28_NOISE_DIR)} noises")


def fetch_ami_validation(backend: str = "onnx") -> None:
    """Create compact AMI distant-microphone false-positive validation data."""
    import datasets
    import numpy as np

    dest = FEATURES_DIR / AMI_VALIDATION_FEATURES
    if dest.exists():
        log(f"AMI far-field validation features present: {dest}")
        return
    log("streaming AMI distant-mic validation audio (~7 hours)")
    ds = datasets.load_dataset(AMI_REPO, name=AMI_CONFIG, split="validation", streaming=True)
    embedder = _feature_extractor(backend)
    chunks, batch = [], []
    started, last_log, count = monotonic(), monotonic(), 0

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        embedded = embedder.embed_clips(np.stack(batch), batch_size=FEATURE_BATCH_SIZE,
                                        ncpu=FEATURE_NCPU)
        chunks.append(embedded.reshape(-1, embedded.shape[-1]))
        nonlocal_count[0] += len(batch)
        batch = []

    nonlocal_count = [0]
    for row in ds:
        try:
            audio = row["audio"]
            batch.append(_audio_16k_clip(audio["array"], audio["sampling_rate"]))
            count += 1
            last_log = _progress("AMI feature extraction", count, None, started, last_log)
        except Exception as error:
            log(f"  skipping unreadable clip: {error}")
            continue
        if len(batch) == FEATURE_BATCH_SIZE:
            flush()
    flush()
    _progress("AMI feature extraction", nonlocal_count[0], None, started, last_log, force=True)
    if not chunks:
        sys.exit("AMI yielded no readable validation audio")
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with open(part, "wb") as f:
        np.save(f, np.concatenate(chunks, axis=0))
    part.rename(dest)
    log(f"  → {dest} ({dest.stat().st_size / 1e6:.0f} MB)")


def build_common_voice_features(backend: str = "onnx") -> None:
    """Embed every validated clip from the downloaded Common Voice archive."""
    import csv
    import io
    import tarfile

    import numpy as np
    import soundfile as sf
    from feature_store import ResumableFeatureWriter, archive_checksum

    dest = FEATURES_DIR / COMMON_VOICE_FEATURES
    if dest.exists():
        log(f"Common Voice features present: {dest}")
        return
    archives = sorted(COMMON_VOICE_DIR.glob("*.tar.gz"))
    if len(archives) != 1:
        sys.exit("download Common Voice first (forge.py assets --only common_voice)")
    archive = archives[0]
    with tarfile.open(archive, "r:gz") as tar:
        # `invalidated.tsv` sorts before `validated.tsv`; suffix matching
        # silently selected the invalidated metadata and produced no usable
        # clip set for this corpus.
        tsv = next((m for m in tar if Path(m.name).name == "validated.tsv"), None)
        if tsv is None or (source := tar.extractfile(tsv)) is None:
            sys.exit("Common Voice archive has no readable validated.tsv")
        # Some Common Voice sentence fields exceed csv's 128 KiB default.
        # We only need the path column, but the parser still validates every
        # field in each row before yielding it.
        csv.field_size_limit(sys.maxsize)
        valid_names = {Path(row["path"]).name for row in
                       csv.DictReader(io.TextIOWrapper(source, encoding="utf-8"),
                                      delimiter="\t") if row.get("path")}
    if not valid_names:
        sys.exit("Common Voice validated.tsv has no clips")
    log(f"embedding {len(valid_names):,} validated Common Voice clips")

    def clips():
        """Read the tar sequentially, but decode at most four clips in flight."""
        for name, clip, error in iter_decoded_audio_members(archive, valid_names):
            if error is not None:
                log(f"  skipping unreadable Common Voice clip {name}: {error}")
                continue
            yield clip

    iterator = clips()
    try:
        first = next(iterator)
    except StopIteration:
        sys.exit("Common Voice archive has no validated audio clips")
    embedder = _feature_extractor(backend)
    frames = embedder.embed_clips(np.stack([first]), batch_size=1, ncpu=FEATURE_NCPU).shape[1]
    writer = ResumableFeatureWriter(
        dest, (len(valid_names), frames, 96), archive_checksum(archive),
        embedder.describe()["model_version"])
    if writer.written:
        log(f"resuming Common Voice features at {writer.written:,}/{len(valid_names):,} clips")
    batch = []
    started = monotonic()
    last_log = monotonic()

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        embedded = embedder.embed_clips(np.stack(batch), batch_size=FEATURE_BATCH_SIZE,
                                        ncpu=FEATURE_NCPU)
        writer.append(embedded)
        batch = []
        nonlocal_state[0] = _progress("Common Voice feature extraction", writer.written,
                                      len(valid_names), started, nonlocal_state[0])

    nonlocal_state = [last_log]
    for index, clip in enumerate([first], start=0):
        if index >= writer.written:
            batch.append(clip)
    for index, clip in enumerate(iterator, start=1):
        if index < writer.written:
            continue
        batch.append(clip)
        if len(batch) == FEATURE_BATCH_SIZE:
            flush()
    flush()
    writer.complete()
    log(f"  → {dest} {writer.shape} ({dest.stat().st_size / 1e9:.1f} GB)")


def fetch_rirs() -> None:
    if dir_has_files(RIR_DIR, "*.wav"):
        log(f"RIRs present: {RIR_DIR}")
        return
    import datasets

    log("downloading MIT environmental impulse responses (~270 clips)")
    RIR_DIR.mkdir(parents=True, exist_ok=True)
    ds = datasets.load_dataset(
        "davidscripka/MIT_environmental_impulse_responses",
        split="train",
        streaming=True,
    )
    n = 0
    for row in ds:
        audio = row["audio"]
        name = Path(audio.get("path") or f"rir_{n}.wav").name
        write_wav_16k(RIR_DIR / name, audio["array"], audio["sampling_rate"])
        n += 1
    log(f"  → {n} RIR wavs in {RIR_DIR}")


def fetch_audioset(max_clips: int) -> None:
    if dir_has_files(AUDIOSET_DIR, "*.wav"):
        log(f"AudioSet clips present: {AUDIOSET_DIR}")
        return
    # Read the parquet shards with pyarrow directly: the shards carry
    # huggingface feature metadata written by a newer `datasets` than our
    # pinned 2.14 can parse, so load_dataset() chokes on them.
    import io

    import pyarrow.parquet as pq
    import soundfile as sf

    log(f"downloading AudioSet background clips (up to {max_clips})")
    AUDIOSET_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    started, last_log = monotonic(), monotonic()
    for url in AUDIOSET_PARQUET_URLS:
        shard = ASSETS / "audioset_shard.parquet"
        download(url, shard)
        try:
            pf = pq.ParquetFile(shard)
            for batch in pf.iter_batches(columns=["audio"], batch_size=32):
                for rec in batch.column(0):
                    try:
                        data, sr = sf.read(io.BytesIO(rec["bytes"].as_py()))
                        write_wav_16k(AUDIOSET_DIR / f"audioset_{n:05d}.wav", data, sr)
                        n += 1
                    except Exception as e:
                        log(f"  skipping clip {n}: {e}")
                    last_log = _progress("AudioSet extraction", n, max_clips,
                                         started, last_log)
                    if n >= max_clips:
                        break
                if n >= max_clips:
                    break
        finally:
            shard.unlink(missing_ok=True)
        if n >= max_clips:
            break
    log(f"  → {n} background clips in {AUDIOSET_DIR}")


def fetch_fma(max_clips: int) -> None:
    if dir_has_files(FMA_DIR, "*.wav"):
        log(f"FMA clips present: {FMA_DIR}")
        return
    import datasets

    log(f"downloading FMA music clips (streaming, {max_clips} × 30s)")
    FMA_DIR.mkdir(parents=True, exist_ok=True)
    ds = datasets.load_dataset("rudraml/fma", name="small", split="train", streaming=True)
    n = 0
    started, last_log = monotonic(), monotonic()
    for row in ds:
        audio = row["audio"]
        write_wav_16k(FMA_DIR / f"fma_{n:05d}.wav", audio["array"], audio["sampling_rate"])
        n += 1
        last_log = _progress("FMA extraction", n, max_clips, started, last_log)
        if n >= max_clips:
            break
    log(f"  → {n} music clips in {FMA_DIR}")


def cmd_assets(args) -> None:
    parts = args.only.split(",") if args.only else ASSET_PARTS
    unknown = set(parts) - set(ASSET_PARTS)
    if unknown:
        sys.exit(f"unknown asset part(s): {', '.join(unknown)} (valid: {', '.join(ASSET_PARTS)})")
    actions = {
        "piper": fetch_piper,
        "features": fetch_features,
        "common_voice": fetch_common_voice,
        "common_voice_features": lambda: build_common_voice_features(args.feature_backend),
        "ami": lambda: fetch_ami_validation(args.feature_backend),
        "fleurs": lambda: fetch_fleurs(args.feature_backend),
        "voxpopuli": lambda: fetch_voxpopuli(args.feature_backend),
        "musan": fetch_musan,
        "slr28": fetch_slr28,
        "rirs": fetch_rirs,
        "audioset": lambda: fetch_audioset(args.audioset_clips),
        "fma": lambda: fetch_fma(args.fma_clips),
    }
    log(f"asset plan: {len(parts)} task(s), sequential download/extract mode")
    for index, part in enumerate(parts, start=1):
        log(f"asset task {index}/{len(parts)} start: {part}")
        actions[part]()
        log(f"asset task {index}/{len(parts)} complete: {part}")
    log("assets done")


def cmd_bench_features(args) -> None:
    """Benchmark extraction only with deterministic predecoded PCM clips."""
    import numpy as np

    rng = np.random.default_rng(20260820)
    clips = rng.integers(-32768, 32767, size=(args.clips, FEATURE_CLIP_SAMPLES),
                         dtype="int16")
    extractor = _feature_extractor(args.backend)
    # Conversion, graph compilation, and allocator startup are not throughput.
    extractor.embed_clips(clips[:min(args.batch_size, len(clips))], batch_size=args.batch_size)
    started = time.perf_counter()
    features = extractor.embed_clips(clips, batch_size=args.batch_size)
    elapsed = time.perf_counter() - started
    result = {
        **extractor.describe(),
        "clips": len(clips),
        "batch_size": args.batch_size,
        "feature_shape": list(features.shape),
        "seconds": round(elapsed, 3),
        "clips_per_second": round(len(clips) / elapsed, 3),
    }
    log("feature benchmark " + json.dumps(result, sort_keys=True))


def missing_assets(config: dict | None = None) -> list:
    missing = []
    if not (PIPER_CKPT.exists() and PIPER_CKPT.with_suffix(".pt.json").exists()):
        missing.append("piper checkpoint + voice config (forge.py assets --only piper)")
    for fname in (NEGATIVE_FEATURES, VALIDATION_FEATURES):
        if not (FEATURES_DIR / fname).exists():
            missing.append(f"{fname} (forge.py assets --only features)")
    if not dir_has_files(RIR_DIR, "*.wav"):
        missing.append("MIT RIRs (forge.py assets --only rirs)")
    if not (dir_has_files(AUDIOSET_DIR, "*.wav") or dir_has_files(FMA_DIR, "*.wav")):
        missing.append("background noise (forge.py assets --only audioset,fma)")
    if config and config.get("use_common_voice_negatives") and not (FEATURES_DIR / COMMON_VOICE_FEATURES).exists():
        missing.append("Common Voice features (forge.py assets --only common_voice_features)")
    if config and config.get("use_ami_farfield_validation") and not (FEATURES_DIR / AMI_VALIDATION_FEATURES).exists():
        missing.append("AMI far-field validation (forge.py assets --only ami)")
    if config and config.get("use_fleurs_negatives") and not (FEATURES_DIR / FLEURS_FEATURES).exists():
        missing.append("FLEURS multilingual features (forge.py assets --only fleurs)")
    if config and config.get("use_voxpopuli_negatives") and not (FEATURES_DIR / VOXPOPULI_FEATURES).exists():
        missing.append("VoxPopuli accent features (forge.py assets --only voxpopuli)")
    if config and config.get("use_musan_background") and not dir_has_files(MUSAN_DIR, "*.wav"):
        missing.append("MUSAN background audio (forge.py assets --only musan)")
    if config and config.get("use_slr28_augmentation") and not dir_has_files(SLR28_RIR_DIR, "*.wav"):
        missing.append("OpenSLR 28 RIR/noise audio (forge.py assets --only slr28)")
    return missing


# ---------------------------------------------------------------- new

def cmd_new(args) -> None:
    # comma-separated variants train ONE model that fires on any of them —
    # the lever for pronunciation/accent coverage
    kind = getattr(args, "kind", "wake")
    if kind not in MODEL_KINDS:
        sys.exit(f"unknown model kind: {kind}")
    phrases = [p.strip().lower() for p in args.phrase.split(",") if p.strip()]
    confusables = [p.strip().lower() for p in args.confusables.split(",") if p.strip()]
    if not phrases:
        sys.exit("empty phrase")
    if args.google_tts_qps <= 0:
        sys.exit("queries per second must be positive")
    name = args.name or slugify(phrases[0])
    ww_dir = WAKEWORDS / name
    cfg_path = ww_dir / "config.yml"
    if cfg_path.exists() and not args.force:
        sys.exit(f"{cfg_path} already exists (use --force to overwrite the config)")
    ww_dir.mkdir(parents=True, exist_ok=True)
    template = (FORGE_DIR / "config.template.yml").read_text()
    cfg = (
        template.replace("@NAME@", name)
        .replace("@MODEL_KIND@", kind)
        .replace("@PHRASES@", "\n".join(f'  - "{p}"' for p in phrases))
        .replace("@N_SAMPLES@", str(args.samples))
        .replace("@N_SAMPLES_VAL@", str(args.samples_val))
        .replace("@STEPS@", str(args.steps))
        .replace("@OUTPUT_DIR@", str(ww_dir))
        .replace("custom_negative_phrases: []", "custom_negative_phrases: " +
                  (json.dumps(confusables) if confusables else "[]"))
    )
    cfg = cfg.replace('google_tts_languages: "en-US,en-GB,en-AU,en-IN,en-PH,en-SG,en-ZA"',
                      f"google_tts_languages: {json.dumps(args.google_tts_languages)}")
    cfg = cfg.replace('google_tts_voices: ""', f"google_tts_voices: {json.dumps(args.google_tts_voices)}")
    cfg = cfg.replace("google_tts_samples_per_voice: 250",
                      f"google_tts_samples_per_voice: {args.google_tts_samples_per_voice}")
    cfg = cfg.replace("google_tts_qps: 2", f"google_tts_qps: {args.google_tts_qps}")
    cfg_path.write_text(cfg)
    log(f"created {cfg_path}")
    log(f"kind: {kind}  phrases: {phrases}  positives: {args.samples}  steps: {args.steps}")
    if confusables:
        log(f"confusables: {confusables}")
    if kind == "stop":
        log("stop guidance: include labelled post-AFE stop, false-stop, and playback-negative captures; synthetic recall alone cannot approve this model")
    log(f"next: forge.py build {name}   (optionally forge.py google-tts {name} first)")


# ---------------------------------------------------------------- build

BUILD_STEPS = ["generate", "augment", "train"]
STEP_FLAGS = {"generate": "--generate_clips", "augment": "--augment_clips", "train": "--train_model"}
FEATURE_FILES = ("positive_features_train.npy", "positive_features_test.npy",
                 "negative_features_train.npy", "negative_features_test.npy")
FEATURE_MANIFEST = ".feature_sources.json"
CLIP_DIRS = ("positive_train", "positive_test", "negative_train", "negative_test")
SOURCE_NAMES = ("custom", "piper", "google")
SOURCE_INVENTORY = ".source_inventory.json"
_SOURCE_INVENTORY_MEMORY: dict[str, tuple[tuple[int, ...], dict]] = {}


def clip_source(path: Path) -> str:
    """Classify existing flat-layout clips without changing their storage."""
    if path.name.startswith(("custom_", "user_")):
        return "custom"
    if path.name.startswith("google_"):
        return "google"
    return "piper"


def _wav_duration_seconds(path: Path) -> float:
    import wave

    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / wav.getframerate()
    except (OSError, EOFError, wave.Error):
        return 0.0


def source_inventory(work_dir: Path) -> dict:
    """Return source/polarity/split counts and durations, caching WAV headers."""
    work_dir.mkdir(parents=True, exist_ok=True)
    directory_paths = [work_dir / f"{polarity}_{split}"
                       for polarity in ("positive", "negative")
                       for split in ("train", "test")]
    directory_mtimes = tuple(
        path.stat().st_mtime_ns if path.is_dir() else 0
        for path in directory_paths
    )
    memory_key = str(work_dir.resolve())
    cached_memory = _SOURCE_INVENTORY_MEMORY.get(memory_key)
    if cached_memory and cached_memory[0] == directory_mtimes:
        return cached_memory[1]
    cache_path = work_dir / SOURCE_INVENTORY
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {"files": {}}
    if (cache.get("directories") == list(directory_mtimes)
            and isinstance(cache.get("inventory"), dict)):
        inventory = cache["inventory"]
        _SOURCE_INVENTORY_MEMORY[memory_key] = (directory_mtimes, inventory)
        return inventory
    files = cache.get("files", {})
    current = {}
    inventory = {
        polarity: {source: {"train": {"count": 0, "seconds": 0.0},
                             "test": {"count": 0, "seconds": 0.0}}
                   for source in SOURCE_NAMES}
        for polarity in ("positive", "negative")
    }
    for polarity in ("positive", "negative"):
        for split in ("train", "test"):
            directory = work_dir / f"{polarity}_{split}"
            for path in sorted(directory.glob("*.wav")) if directory.exists() else ():
                stat = path.stat()
                key = f"{polarity}_{split}/{path.name}"
                prior = files.get(key, {})
                if prior.get("size") == stat.st_size and prior.get("mtime_ns") == stat.st_mtime_ns:
                    seconds = prior.get("seconds", 0.0)
                else:
                    seconds = _wav_duration_seconds(path)
                current[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                "seconds": seconds}
                bucket = inventory[polarity][clip_source(path)][split]
                bucket["count"] += 1
                bucket["seconds"] += seconds
    for polarity in inventory.values():
        for source in polarity.values():
            source["total"] = {
                "count": source["train"]["count"] + source["test"]["count"],
                "seconds": source["train"]["seconds"] + source["test"]["seconds"],
            }
    part = cache_path.with_name(cache_path.name + ".part")
    part.write_text(json.dumps({"version": 2, "directories": list(directory_mtimes),
                                "files": current, "inventory": inventory},
                               sort_keys=True))
    part.replace(cache_path)
    _SOURCE_INVENTORY_MEMORY[memory_key] = (directory_mtimes, inventory)
    return inventory


def resolve_training_mix(inventory: dict, polarity: str, requested: dict | None) -> dict:
    """Resolve a source mix into deterministic train draw counts."""
    available = {source: inventory[polarity][source]["train"]["count"]
                 for source in SOURCE_NAMES}
    total = sum(available.values())
    if not total:
        return {"natural": True, "draws": {source: 0 for source in SOURCE_NAMES}}
    requested = requested or {}
    specified = {source: requested.get(source) for source in SOURCE_NAMES}
    if all(value is None for value in specified.values()):
        return {"natural": True, "draws": available}
    if any(not isinstance(value, (int, float)) for value in specified.values()):
        raise ValueError(f"{polarity} mix must specify all source weights or leave all blank")
    if any(value < 0 for value in specified.values()) or sum(specified.values()) != 100:
        raise ValueError(f"{polarity} mix weights must be non-negative and total 100")
    if any(specified[source] and not available[source] for source in SOURCE_NAMES):
        raise ValueError(f"{polarity} mix assigns weight to an unavailable source")
    draws = {source: int(total * specified[source] // 100) for source in SOURCE_NAMES}
    remainder = total - sum(draws.values())
    for source in sorted(SOURCE_NAMES, key=lambda item: (-specified[item], item))[:remainder]:
        draws[source] += 1
    return {"natural": False, "draws": draws}


def _materialize_mix_view(work_dir: Path, config: dict) -> tuple[Path, dict]:
    """Create an upstream-compatible weighted clip workspace without mutating sources."""
    import yaml

    inventory = source_inventory(work_dir)
    staging = work_dir / ".mix_staging"
    shutil.rmtree(staging, ignore_errors=True)
    stage_work = staging / config["model_name"]
    resolved = {}
    seed = config.get("training_mix_seed", 20260828)
    for polarity in ("positive", "negative"):
        mix = resolve_training_mix(inventory, polarity, (config.get("training_mix") or {}).get(polarity))
        resolved[polarity] = mix
        for split in ("train", "test"):
            destination = stage_work / f"{polarity}_{split}"
            destination.mkdir(parents=True, exist_ok=True)
            by_source = {source: sorted((work_dir / f"{polarity}_{split}").glob("*.wav"))
                         for source in SOURCE_NAMES}
            for source, paths in by_source.items():
                if split == "test":
                    chosen = paths
                else:
                    draws = mix["draws"][source]
                    rng = random.Random(f"{seed}:{polarity}:{source}")
                    shuffled = paths[:]
                    rng.shuffle(shuffled)
                    chosen = [shuffled[index % len(shuffled)] for index in range(draws)] if shuffled else []
                for index, src in enumerate(chosen):
                    dst = destination / f"mix_{source}_{index:06d}_{src.name}"
                    os.link(src, dst)
    staged_config = dict(config)
    staged_config["output_dir"] = str(staging)
    # A train-only restart consumes the canonical feature arrays generated by
    # an earlier augment run. Augment will overwrite these staged copies.
    for filename in FEATURE_FILES:
        source = work_dir / filename
        if source.exists():
            shutil.copy2(source, stage_work / filename)
    staged_config_path = staging / "training_config.yml"
    staged_config_path.write_text(yaml.safe_dump(staged_config, sort_keys=False))
    return staged_config_path, resolved


def feature_sources_manifest(work_dir: Path, config: dict) -> dict:
    """Describe the audio and augmentation options used to make feature arrays."""
    clips = []
    for directory in CLIP_DIRS:
        for path in sorted((work_dir / directory).glob("*.wav")):
            stat = path.stat()
            clips.append({"path": f"{directory}/{path.name}",
                          "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return {
        "version": 1,
        "clips": clips,
        "augmentation": {
            "augmentation_rounds": config.get("augmentation_rounds"),
            "use_musan_background": bool(config.get("use_musan_background")),
            "use_slr28_augmentation": bool(config.get("use_slr28_augmentation")),
        },
        "training_mix": config.get("training_mix"),
        "training_mix_seed": config.get("training_mix_seed", 20260828),
    }


def features_stale(work_dir: Path, config: dict) -> bool:
    """Whether feature arrays are absent or do not match their source clips."""
    if not all((work_dir / filename).exists() for filename in FEATURE_FILES):
        return True
    try:
        recorded = json.loads((work_dir / FEATURE_MANIFEST).read_text())
    except (OSError, json.JSONDecodeError):
        return True
    return recorded != feature_sources_manifest(work_dir, config)


def write_feature_manifest(work_dir: Path, config: dict) -> None:
    """Atomically mark all successfully regenerated feature arrays as current."""
    path = work_dir / FEATURE_MANIFEST
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(feature_sources_manifest(work_dir, config),
                               sort_keys=True, separators=(",", ":")))
    part.replace(path)


def cmd_build(args) -> None:
    name = args.name
    cfg_path = WAKEWORDS / name / "config.yml"
    if not cfg_path.exists():
        sys.exit(f"no such wake word: {cfg_path} missing (run forge.py new first)")
    import yaml

    config = yaml.safe_load(cfg_path.read_text())
    work_dir = Path(config["output_dir"]) / config["model_name"]
    missing = missing_assets(config)
    if missing:
        sys.exit("missing training assets:\n  - " + "\n  - ".join(missing))

    # Keep the human-editable opt-ins out of upstream train.py's schema. The
    # resolved values are ordinary upstream keys, so manual config edits and
    # command-line builds behave identically to the UI.
    feature_files = config.setdefault("feature_data_files", {})
    batch_sizes = config.setdefault("batch_n_per_class", {})
    optional_features = {
        "CommonVoice_en": ("use_common_voice_negatives", COMMON_VOICE_FEATURES),
        "FLEURS": ("use_fleurs_negatives", FLEURS_FEATURES),
        "VoxPopuli": ("use_voxpopuli_negatives", VOXPOPULI_FEATURES),
    }
    enabled_optional = []
    for source, (setting, filename) in optional_features.items():
        if config.get(setting):
            feature_files[source] = str(FEATURES_DIR / filename)
            enabled_optional.append(source)
        else:
            feature_files.pop(source, None)
            batch_sizes.pop(source, None)
    # Optional sources substitute for ACAV rather than expanding the negative
    # batch. This keeps the proven positive/negative balance stable.
    reserved = sum(OPTIONAL_NEGATIVE_BATCH[source] for source in enabled_optional)
    batch_sizes["ACAV100M_sample"] = max(256, 1024 - reserved)
    for source in enabled_optional:
        batch_sizes[source] = OPTIONAL_NEGATIVE_BATCH[source]
    backgrounds = [str(AUDIOSET_DIR), str(FMA_DIR)]
    if config.get("use_musan_background"):
        backgrounds.append(str(MUSAN_DIR))
    config["background_paths"] = backgrounds
    config["background_paths_duplication_rate"] = [1] * len(backgrounds)
    rirs = [str(RIR_DIR)]
    if config.get("use_slr28_augmentation"):
        rirs.append(str(SLR28_RIR_DIR))
        backgrounds.append(str(SLR28_NOISE_DIR))
        config["background_paths"] = backgrounds
        config["background_paths_duplication_rate"] = [1] * len(backgrounds)
    config["rir_paths"] = rirs
    if config.get("use_ami_farfield_validation"):
        config["false_positive_validation_data_path"] = str(FEATURES_DIR / AMI_VALIDATION_FEATURES)
    else:
        config["false_positive_validation_data_path"] = str(FEATURES_DIR / VALIDATION_FEATURES)
    cfg_path.write_text(yaml.safe_dump(config, sort_keys=False))

    import torch

    if torch.cuda.is_available():
        log(f"torch {torch.__version__} — CUDA: {torch.cuda.get_device_name(0)}")
    else:
        log(f"torch {torch.__version__} — no CUDA device visible, falling back to CPU "
            "(generation and training will be slow)")

    steps = BUILD_STEPS[BUILD_STEPS.index(args.from_step):]
    if args.only_step:
        steps = [args.only_step]

    staged_config_path = None
    resolved_mix = None
    for step in steps:
        log(f"=== step: {step} ===")
        # Generate can add Piper clips, so inspect sources only after it runs.
        # Upstream checks only whether feature files exist; a manifest is what
        # prevents fresh Chirp/imported clips from being trained as stale data.
        if step == "augment" and features_stale(work_dir, config):
            log("feature sources changed or are untracked — rebuilding all feature arrays")
            args.overwrite = True
        if step == "train" and features_stale(work_dir, config):
            sys.exit("training clips changed since feature generation; rerun from augment")
        # Generation owns the canonical clip directories. Augment/train run in
        # a disposable hard-linked view so source weights never alter the
        # original files or the held-out source-specific test sets.
        if step == "augment":
            staged_config_path, resolved_mix = _materialize_mix_view(work_dir, config)
            log("resolved training mix: " + json.dumps(resolved_mix, sort_keys=True))
        training_config = cfg_path if step == "generate" else staged_config_path
        if training_config is None:
            staged_config_path, resolved_mix = _materialize_mix_view(work_dir, config)
            training_config = staged_config_path
        cmd = [sys.executable, TRAIN_PY, "--training_config", str(training_config), STEP_FLAGS[step]]
        if args.overwrite and step == "augment":
            cmd.append("--overwrite")
        subprocess.run(cmd, check=True)
        if step == "augment":
            stage_work = Path(config["output_dir"]) / config["model_name"] / ".mix_staging" / config["model_name"]
            for filename in FEATURE_FILES:
                shutil.copy2(stage_work / filename, work_dir / filename)
            write_feature_manifest(work_dir, config)

    if "train" in steps:
        src = (Path(config["output_dir"]) / config["model_name"] / ".mix_staging" /
               f"{name}.onnx")
        # Test doubles and older local wrappers can still write to the
        # canonical output path. Real upstream runs use the staged path.
        if not src.exists():
            src = WAKEWORDS / name / f"{name}.onnx"
        if not src.exists():
            sys.exit(f"training finished but {src} was not produced — check the logs above")
        MODELS.mkdir(parents=True, exist_ok=True)
        dest = MODELS / f"{name}.onnx"
        shutil.copy2(src, dest)
        write_model_manifest(name, config, dest)
        log(f"model ready: {dest} ({dest.stat().st_size / 1e3:.0f} kB)")
        log("install into EchoMuse: see oww_forge/README.md §Installing")
        evaluate_model(name)


def score_wav_file(oww_model, path: Path) -> float:
    """Score a single WAV file against an openWakeWord model instance."""
    import librosa
    import numpy as np
    import soundfile as sf

    audio, sr = sf.read(path, dtype="int16")
    if sr != 16000:
        f = audio.astype("float32") / 32768.0
        f = librosa.resample(f.T if f.ndim > 1 else f, orig_sr=sr, target_sr=16000)
        audio = (np.clip(f if f.ndim == 1 else f.mean(axis=0), -1, 1) * 32767).astype("int16")
    if audio.ndim > 1:
        audio = audio.mean(axis=1).astype("int16")
    oww_model.reset()
    peak = 0.0
    for i in range(0, len(audio) - 1280, 1280):
        scores = oww_model.predict(audio[i : i + 1280])
        peak = max(peak, max(scores.values()))
    return float(peak)


def _format_eval_row(label: str, scores: list[float], threshold_hi: float = 0.5, threshold_lo: float = 0.3) -> str:
    import numpy as np
    if not scores:
        return f"{label:<28}: 0 clips"
    arr = np.array(scores)
    hi_pct = 100 * np.mean(arr >= threshold_hi)
    lo_pct = 100 * np.mean(arr >= threshold_lo)
    return (f"{label:<28}: {len(scores):4d} clips | mean={arr.mean():.3f} | max={arr.max():.3f} | "
            f">={threshold_hi:.1f}: {hi_pct:5.1f}% | >={threshold_lo:.1f}: {lo_pct:5.1f}%")


def _capture_kind(path: Path) -> str | None:
    """Stop provenance retained by import_labeled_dataset's custom filename."""
    match = re.match(r"^custom_(stop_act|stop_miss|false_stop|playback_negative)_", path.name)
    return match.group(1) if match else None


def write_model_manifest(name: str, config: dict, model_path: Path) -> Path:
    """Write Forge provenance beside an ONNX model without changing its wire format."""
    kind = config.get("model_kind", "wake")
    if kind not in MODEL_KINDS:
        kind = "wake"
    manifest = {
        "kind": kind,
        "target_phrases": config.get("target_phrase", []),
        "forge_version": FORGE_VERSION,
        "training_provenance": {
            "model_name": config.get("model_name", name),
            "training_mix": config.get("training_mix"),
        },
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
    }
    path = model_path.with_suffix(".manifest.json")
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    part.replace(path)
    return path


def _score_files_parallel(model_path: Path, files: list[Path], num_workers: int = 6) -> dict[Path, float]:
    """Score a list of audio files across worker threads with per-thread ONNX models."""
    from concurrent.futures import ThreadPoolExecutor
    from openwakeword.model import Model

    if not files:
        return {}

    def worker_loop(chunk):
        m = Model(wakeword_models=[str(model_path)], inference_framework="onnx")
        return [(p, score_wav_file(m, p)) for p in chunk]

    chunks = [files[i::num_workers] for i in range(num_workers) if files[i::num_workers]]
    results = {}
    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        for chunk_res in pool.map(worker_loop, chunks):
            for p, score in chunk_res:
                results[p] = score
    return results


def evaluate_model(name: str) -> None:
    """Evaluate a trained model against its test sets and output a granular report."""
    from collections import defaultdict
    import numpy as np

    model_path = MODELS / f"{name}.onnx"
    if not model_path.exists():
        log(f"evaluation skipped: {model_path} not found")
        return

    cfg_path = WAKEWORDS / name / "config.yml"
    work_dir = WAKEWORDS / name / name
    cfg = {}
    if cfg_path.exists():
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text())
        if cfg.get("output_dir") and cfg.get("model_name"):
            work_dir = Path(cfg["output_dir"]) / cfg["model_name"]

    if cfg_path.exists() and features_stale(work_dir, cfg):
        log("WARNING: test clips or augmentation settings changed after the feature build; "
            "this model was not trained on the current dataset")

    pos_dir = work_dir / "positive_test"
    neg_dir = work_dir / "negative_test"
    pos_files = sorted(pos_dir.glob("*.wav")) if pos_dir.exists() else []
    neg_files = sorted(neg_dir.glob("*.wav")) if neg_dir.exists() else []

    if not pos_files and not neg_files:
        log("evaluation skipped: no test audio in positive_test or negative_test")
        return

    log("=" * 72)
    kind = cfg.get("model_kind", "wake")
    threshold = float(cfg.get("stop_threshold", 0.75 if kind == "stop" else 0.5))
    log(f" MODEL EVALUATION REPORT: {name}.onnx ({kind})")
    log("=" * 72)

    pos_scores = []
    if pos_files:
        log("\n--- Positive Test Clips (Target Wake Word) ---")
        by_source = defaultdict(list)
        by_gtts_locale = defaultdict(list)
        pos_map = _score_files_parallel(model_path, pos_files)
        pos_scores = [pos_map[p] for p in pos_files]
        for p, score in pos_map.items():
            if p.name.startswith("google_"):
                by_source["Google Chirp 3"].append(score)
                parts = p.name.split("_")
                locale = parts[1] if len(parts) > 1 else "unknown"
                by_gtts_locale[locale].append(score)
            elif p.name.startswith("user_") or p.name.startswith("custom_"):
                by_source["Custom / Recorded"].append(score)
            else:
                by_source["Piper / Synthetic"].append(score)

        log(_format_eval_row("  Overall Positives", pos_scores, threshold, 0.3))
        for src, sc in sorted(by_source.items()):
            log(_format_eval_row(f"    - {src}", sc, threshold, 0.3))
        if by_gtts_locale:
            log("    Google Chirp 3 by Locale:")
            for loc, sc in sorted(by_gtts_locale.items()):
                log(_format_eval_row(f"      • {loc}", sc, 0.5, 0.3))

    neg_scores = []
    if neg_files:
        log("\n--- Negative Test Clips (Confusables & Adversarials) ---")
        by_neg_source = defaultdict(list)
        neg_map = _score_files_parallel(model_path, neg_files)
        neg_scores = [neg_map[p] for p in neg_files]
        for p, score in neg_map.items():
            if p.name.startswith("google_"):
                by_neg_source["Google Chirp Confusables"].append(score)
            elif p.name.startswith("custom_") or p.name.startswith("user_"):
                by_neg_source["Custom / Recorded"].append(score)
            else:
                by_neg_source["Piper Adversarial"].append(score)

        log(_format_eval_row("  Overall Negatives", neg_scores, threshold, 0.2))
        for src, sc in sorted(by_neg_source.items()):
            log(_format_eval_row(f"    - {src}", sc, threshold, 0.2))

    if kind == "stop":
        all_map = {**(_score_files_parallel(model_path, pos_files) if pos_files else {}),
                   **(_score_files_parallel(model_path, neg_files) if neg_files else {})}
        groups = {label: [] for label in ("stop_act", "stop_miss", "false_stop", "playback_negative")}
        for path, score in all_map.items():
            capture_kind = _capture_kind(path)
            if capture_kind:
                groups[capture_kind].append(score)
        log("\n--- Post-AFE Stop Captures ---")
        for label, scores in groups.items():
            polarity = "positive" if label in ("stop_act", "stop_miss") else "negative"
            log(_format_eval_row(f"  {label} ({polarity})", scores, threshold, 0.2))

    log("-" * 72)
    if pos_scores and neg_scores:
        pos_arr = np.array(pos_scores)
        neg_arr = np.array(neg_scores)
        neg_p995 = float(np.percentile(neg_arr, 99.5))
        pos_mean = float(np.mean(pos_arr))
        rec_min = max(0.30, round(neg_p995 + 0.10, 2))
        rec_max = min(0.60, max(rec_min + 0.10, round(pos_mean, 2)))
        log(f" Suggested EchoMuse wake threshold : {rec_min:.2f} – {rec_max:.2f}")
        log(f"   (99.5% Confusables rejected below {neg_p995:.3f} | Positive test mean: {pos_mean:.3f})")
    log("=" * 72)


# ---------------------------------------------------------------- google-tts

def cmd_google_tts(args) -> None:
    import yaml

    cfg_path = WAKEWORDS / args.name / "config.yml"
    if not cfg_path.exists():
        sys.exit(f"no such wake word: {cfg_path} missing (run forge.py new first)")
    cfg = yaml.safe_load(cfg_path.read_text())
    out_base = Path(cfg["output_dir"]) / cfg["model_name"]

    import google_tts

    google_tts.synthesize(
        phrases=cfg["target_phrase"],
        samples_per_voice=args.samples,
        train_dir=out_base / "positive_train",
        test_dir=out_base / "positive_test",
        languages=args.languages.split(","),
        voice_names=args.voices.split(","),
        assume_yes=args.yes,
        qps=args.qps if args.qps is not None else cfg.get("google_tts_qps", google_tts.DEFAULT_QPS),
    )
    confusables = cfg.get("custom_negative_phrases") or []
    if confusables:
        log(f"adding Google TTS confusables: {confusables}")
        google_tts.synthesize(
            phrases=confusables,
            samples_per_voice=args.samples,
            train_dir=out_base / "negative_train",
            test_dir=out_base / "negative_test",
            languages=args.languages.split(","),
            voice_names=args.voices.split(","),
            assume_yes=args.yes,
            qps=args.qps if args.qps is not None else cfg.get("google_tts_qps", google_tts.DEFAULT_QPS),
        )


# ---------------------------------------------------------------- import

# Audio the dashboard export or a hand-assembled ZIP might carry. Captures from
# EchoMuse are already 16kHz mono WAV; anything else ffmpeg normalises.
DATASET_AUDIO_EXTS = {".wav", ".webm", ".ogg", ".oga", ".mp3", ".m4a", ".flac", ".opus"}


def _convert_16k(src: Path, dest: Path) -> None:
    """Any audio → 16kHz mono s16 WAV, the format train.py augments."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(dest)],
        check=True, timeout=120,
    )


def import_labeled_dataset(name: str, zip_path: Path) -> dict:
    """
    Unpack a labelled dataset ZIP (`positive/…` and `negative/…`, as exported
    by the EchoMuse dashboard) into a wake word's train/test directories.

    Preserves oww_forge's split policy: TEST_FRACTION (0.1) of each polarity is
    held out for test, applied with the same stable `index % round(1/frac) == 0`
    rule google_tts uses, so positives and negatives are split independently and
    a re-run is idempotent per file order. Clips are named `custom_*` so
    forge.py's evaluation buckets them as "Custom / Recorded". Real recordings
    displace synthetic clips at generate time while retaining their supplied
    polarity.

    Returns per-directory counts (plus `skipped`). Raises FileNotFoundError if
    the wake word does not exist.
    """
    import yaml
    import zipfile
    import tempfile
    import uuid
    import google_tts

    cfg_path = WAKEWORDS / name / "config.yml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"no such wake word: {name} (run forge.py new first)")
    cfg = yaml.safe_load(cfg_path.read_text())
    base = Path(cfg["output_dir"]) / cfg["model_name"]
    dirs = {
        ("positive", "train"): base / "positive_train",
        ("positive", "test"):  base / "positive_test",
        ("negative", "train"): base / "negative_train",
        ("negative", "test"):  base / "negative_test",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    test_every = max(2, round(1 / google_tts.TEST_FRACTION))
    counts = {"positive_train": 0, "positive_test": 0,
              "negative_train": 0, "negative_test": 0, "skipped": 0}
    seen = {"positive": 0, "negative": 0}
    # A per-run token, not a wall-clock second: two imports of the same wake
    # word in the same second both start idx at 0, so a `custom_<seconds>_…`
    # name would silently overwrite the first import's clips.
    stamp = uuid.uuid4().hex[:8]

    with zipfile.ZipFile(zip_path) as z, tempfile.TemporaryDirectory() as tmp:
        # The controller manifest is optional for hand-assembled datasets. It
        # retains stop scenario provenance while bucket placement remains the
        # admin-selected positive/negative truth.
        try:
            manifest = json.loads(z.read("manifest.json"))
            manifest_kinds = {clip.get("name"): clip.get("kind")
                              for clip in manifest.get("clips", [])
                              if isinstance(clip, dict)}
        except (KeyError, ValueError, TypeError):
            manifest_kinds = {}
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = info.filename.replace("\\", "/").split("/")
            polarity = next((p for p in parts if p in ("positive", "negative")), None)
            ext = Path(parts[-1]).suffix.lower()
            if polarity is None or ext not in DATASET_AUDIO_EXTS:
                counts["skipped"] += 1
                continue
            idx = seen[polarity]
            seen[polarity] += 1
            split = "test" if idx % test_every == 0 else "train"
            raw = Path(tmp) / f"in_{polarity}_{idx}{ext}"
            raw.write_bytes(z.read(info))
            kind = manifest_kinds.get(Path(parts[-1]).name)
            prefix = f"custom_{kind}" if kind in {"stop_act", "stop_miss", "false_stop", "playback_negative"} else "custom"
            dest = dirs[(polarity, split)] / f"{prefix}_{stamp}_{polarity}_{idx:06d}.wav"
            try:
                _convert_16k(raw, dest)
                counts[f"{polarity}_{split}"] += 1
            except Exception as e:
                counts["skipped"] += 1
                log(f"skip {info.filename}: {e}")
            finally:
                raw.unlink(missing_ok=True)
    return counts


def cmd_import(args) -> None:
    zip_path = Path(args.zip)
    if not zip_path.exists():
        sys.exit(f"dataset zip not found: {zip_path}")
    counts = import_labeled_dataset(args.name, zip_path)
    log(f"imported into '{args.name}': "
        f"positives {counts['positive_train']}+{counts['positive_test']} (train+test), "
        f"negatives {counts['negative_train']}+{counts['negative_test']}, "
        f"skipped {counts['skipped']}")
    log("run `forge.py build` (or the UI's Build) to retrain with the new data")


# ---------------------------------------------------------------- test & eval

def cmd_test(args) -> None:
    from openwakeword.model import Model

    model_path = MODELS / f"{args.name}.onnx"
    if not model_path.exists():
        sys.exit(f"{model_path} not found (run forge.py build first)")
    oww = Model(wakeword_models=[str(model_path)], inference_framework="onnx")

    wavs = []
    for p in args.wav:
        p = Path(p)
        wavs.extend(sorted(p.glob("*.wav")) if p.is_dir() else [p])
    if not wavs:
        sys.exit("no wav files found")

    for wav in wavs:
        peak = score_wav_file(oww, wav)
        print(f"{wav}: peak score {peak:.3f}")


def cmd_eval(args) -> None:
    evaluate_model(args.name)


# ---------------------------------------------------------------- Piper voices

def cmd_voices(args) -> None:
    """List published Piper voices, grouped by language or for one locale."""
    import piper_voices

    if args.language:
        rows = [voice for voice in piper_voices.catalogue(ASSETS)
                if voice["language"] == args.language]
        if not rows:
            sys.exit(f"no Piper voices for {args.language}; run without --language to list locales")
        print(f"{'voice':<40}{'speakers':>9}  quality")
        for voice in rows:
            print(f"{voice['name']:<40}{voice['speakers']:>9}  {voice['quality']}")
        return
    print(f"{'language':<10}{'voices':>7}{'best speakers':>15}  name")
    for language in piper_voices.languages(ASSETS):
        print(f"{language['language']:<10}{language['voices']:>7}"
              f"{language['max_speakers']:>15}  {language['label']}")


def cmd_piper_voices(args) -> None:
    """Add locally-generated Piper positives before the normal build."""
    import yaml
    import piper_voices

    cfg_path = WAKEWORDS / args.name / "config.yml"
    if not cfg_path.exists():
        sys.exit(f"no such wake word: {cfg_path} missing (run forge.py new first)")
    cfg = yaml.safe_load(cfg_path.read_text())
    out_base = Path(cfg["output_dir"]) / cfg["model_name"]
    piper_voices.synthesize(
        phrases=cfg["target_phrase"], n_samples=args.samples,
        train_dir=out_base / "positive_train", test_dir=out_base / "positive_test",
        assets=ASSETS, voices=args.voices.split(",") if args.voices else None,
        language=args.language,
    )
    confusables = cfg.get("custom_negative_phrases") or []
    if confusables:
        log(f"adding Piper confusable negatives: {confusables}")
        piper_voices.synthesize(
            phrases=confusables, n_samples=args.samples,
            train_dir=out_base / "negative_train", test_dir=out_base / "negative_test",
            assets=ASSETS, voices=args.voices.split(",") if args.voices else None,
            language=args.language,
        )


# ---------------------------------------------------------------- ui

def cmd_ui(args) -> None:
    import forge_web

    forge_web.run(host=args.host, port=args.port)


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(prog="forge.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("assets", help="download shared training assets (~25GB total)")
    p.add_argument("--only", help=f"comma-separated subset of: {','.join(ASSET_PARTS)}")
    p.add_argument("--fma-clips", type=int, default=1000,
                   help="number of 30s FMA music clips to fetch (default 1000 ≈ 1GB)")
    p.add_argument("--audioset-clips", type=int, default=2000,
                    help="number of 10s AudioSet noise clips to fetch (default 2000)")
    p.add_argument("--feature-backend", choices=("auto", "torch", "onnx"), default="onnx",
                   help="feature extractor for corpus assets (default: onnx until ROCm gate passes)")
    p.set_defaults(func=cmd_assets)

    p = sub.add_parser("bench-features", help="benchmark predecoded feature extraction")
    p.add_argument("--backend", choices=("torch", "onnx"), required=True)
    p.add_argument("--clips", type=int, default=256)
    p.add_argument("--batch-size", type=int, default=64)
    p.set_defaults(func=cmd_bench_features)

    p = sub.add_parser("new", help="create a wake-word training config")
    p.add_argument("phrase", help='wake phrase; comma-separate pronunciation variants '
                                  '(e.g. "hey clara, hey clarra") — one model fires on any')
    p.add_argument("--name", help="model name (default: slug of the phrase)")
    p.add_argument("--kind", choices=MODEL_KINDS, default="wake",
                   help="model purpose (stop models are evaluated with post-AFE capture groups)")
    p.add_argument("--confusables", default="", help="comma-separated phrases to train as negatives")
    p.add_argument("--google-tts-languages", default="en-US,en-GB,en-AU,en-IN,en-PH,en-SG,en-ZA")
    p.add_argument("--google-tts-voices", default="")
    p.add_argument("--google-tts-samples-per-voice", type=int, default=250)
    p.add_argument("--google-tts-qps", type=float, default=2.0)
    p.add_argument("--samples", type=int, default=30000, help="synthetic positives (default 30000)")
    p.add_argument("--samples-val", type=int, default=2000)
    p.add_argument("--steps", type=int, default=50000, help="max training steps (default 50000)")
    p.add_argument("--force", action="store_true", help="overwrite an existing config.yml")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("build", help="run the training pipeline (generate → augment → train)")
    p.add_argument("name")
    p.add_argument("--from-step", choices=BUILD_STEPS, default="generate",
                   help="resume from this step (clip generation is itself resumable)")
    p.add_argument("--only-step", choices=BUILD_STEPS, help="run a single step")
    p.add_argument("--overwrite", action="store_true",
                   help="recompute augmented features even if they exist")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("google-tts",
                       help="add extra positive samples via Google Cloud TTS (run before build; "
                            "needs GOOGLE_APPLICATION_CREDENTIALS)")
    p.add_argument("name")
    p.add_argument("--samples", type=int, default=250,
                   help="clips per selected Chirp 3 voice and locale pair (default 250)")
    p.add_argument("--languages", default="en-US,en-GB,en-AU,en-IN,en-PH,en-SG,en-ZA",
                    help="comma-separated exact locales")
    p.add_argument("--voices", default="",
                    help="comma-separated exact Chirp 3 voice names; empty selects all matching voices")
    p.add_argument("--qps", type=float, default=None,
                   help="Google TTS requests per second (default: saved config, or 2)")
    p.add_argument("--yes", action="store_true", help="skip the cost-estimate confirmation")
    p.set_defaults(func=cmd_google_tts)

    p = sub.add_parser("voices", help="list published Piper voices by language")
    p.add_argument("--language", help="locale such as en_GB or de_DE; omit to list locales")
    p.set_defaults(func=cmd_voices)

    p = sub.add_parser("piper-voices", help="add Piper positives in another accent or language")
    p.add_argument("name")
    p.add_argument("--samples", type=int, default=4000)
    p.add_argument("--language", default="en_GB", help="locale; see `forge.py voices`")
    p.add_argument("--voices", help="comma-separated voice names, overriding --language")
    p.set_defaults(func=cmd_piper_voices)

    p = sub.add_parser("import",
                       help="import a labelled dataset ZIP (positive/ negative/) exported "
                            "from the EchoMuse dashboard, then build to retrain")
    p.add_argument("name")
    p.add_argument("--zip", required=True, help="dataset .zip from the dashboard's Training tab")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("ui", help="serve the web frontend")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8769)
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("test", help="score wav file(s) against a trained model")
    p.add_argument("name")
    p.add_argument("--wav", nargs="+", required=True, help="wav file(s) or directorie(s)")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("eval", help="evaluate trained model against its positive and negative test clips")
    p.add_argument("name", help="model name")
    p.set_defaults(func=cmd_eval)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.path.insert(0, str(FORGE_DIR))
    main()
