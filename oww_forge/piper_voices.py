"""Extra positive samples from Piper's published voices, in any language.

Every clip the standard pipeline generates is American English:
piper-sample-generator ships `en_US-libritts_r-medium`, and its only other
English checkpoint is also US. So however you actually say the phrase — a
British or Australian or Indian English, or a language that is not English
at all — is the *least* represented thing in a 30,000-clip training set.
Spelling variants ("hey clarra" for a British "clara") paper over that; this
addresses it directly, by training on voices that already say it that way.

The sample generator cannot help here. It loads a pickled VITS `.pt` and only
four exist (US and German, French, Dutch), while Piper publishes ONNX
inference voices across more than thirty languages. This runs those directly,
and needs nothing new to do it: `piper-phonemize` is already pinned as a
generator dependency and `onnxruntime` is already in the image, so Piper
inference is just phonemize → ids → session.run.

Which voices to use is a CHOICE, read from the published catalogue at
runtime, never a constant in this file. Multi-speaker voices are preferred
wherever a language has one, because speaker identity is the axis that buys
the most variety per download: `en_GB-vctk` carries 109 speakers and
`en_US-libritts_r` 904, against 1 for most named voices.

Clips land in the wake word's positive_train/positive_test directories.
openWakeWord's generate step counts existing files toward n_samples, so
anything added before `forge.py build` displaces that many US clips rather
than growing the set — which is the point: it changes the MIX.

Note the wake word model itself still rests on openWakeWord's frozen English
speech embedding, so a non-English wake word is not equally well served by
the rest of the pipeline. This removes one limitation, not all of them.
"""

import itertools
import json
import random
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import numpy as np

VOICES_DIR_NAME = "piper_voices"
HF_REPO = "https://huggingface.co/rhasspy/piper-voices"
HF_BASE = f"{HF_REPO}/resolve/main"
VOICES_INDEX_URL = f"{HF_REPO}/raw/main/voices.json"

# The catalogue is FETCHED, not hardcoded. Piper publishes voices in more
# than thirty languages and this project is not built for one household: a
# baked-in list of English voices would make every other language a code
# change, which is the same trap as shipping a wake word that only answers
# to one accent. The index is ~240KB, cached on the assets volume, and names
# every voice with its speaker count and file path.
#
# Ranking is by speaker count for a reason that holds in any language: one
# multi-speaker voice carries more acoustic variety than a pile of
# single-speaker ones and costs one download. en_GB-vctk-medium has 109
# speakers, en_US-libritts_r has 904, and most named voices have exactly 1.
INDEX_CACHE = "voices_index.json"

# Around each voice config's own defaults rather than a fixed set: a value
# that sounds natural for one voice is not necessarily natural for another,
# and an unnatural positive teaches the model the wrong thing.
LENGTH_SCALES = [0.85, 0.95, 1.0, 1.1, 1.2]   # multipliers, not absolutes
NOISE_SCALES  = [0.9, 1.0, 1.1]
TEST_FRACTION = 0.1
TARGET_RATE = 16000


def log(msg: str) -> None:
    print(f"[piper] {msg}", flush=True)


def voice_dir(assets: Path) -> Path:
    return assets / VOICES_DIR_NAME


def index(assets: Path, refresh: bool = False) -> dict:
    """The published voice catalogue, cached on the assets volume."""
    path = voice_dir(assets) / INDEX_CACHE
    if refresh or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        log("fetching the Piper voice catalogue")
        with urllib.request.urlopen(VOICES_INDEX_URL, timeout=60) as r:
            path.write_bytes(r.read())
    return json.loads(path.read_text())


def catalogue(assets: Path) -> list:
    """
    Every voice, as plain dicts, best first. Ordered by speaker count because
    that is what buys training variety, then by quality.
    """
    quality_rank = {"high": 0, "medium": 1, "low": 2, "x_low": 3}
    out = []
    for name, v in index(assets).items():
        lang = v.get("language", {}) or {}
        out.append({
            "name": name,
            "language": lang.get("code") or name.split("-")[0],
            "language_name": lang.get("name_english") or "",
            "country": lang.get("country_english") or "",
            "quality": v.get("quality"),
            "speakers": v.get("num_speakers") or 1,
        })
    out.sort(key=lambda v: (-v["speakers"], quality_rank.get(v["quality"], 9), v["name"]))
    return out


def languages(assets: Path) -> list:
    """Languages that have at least one voice, with the best speaker count."""
    best = {}
    for v in catalogue(assets):
        cur = best.setdefault(v["language"], {
            "language": v["language"],
            "label": " ".join(x for x in (v["language_name"], v["country"]) if x)
                     or v["language"],
            "voices": 0, "max_speakers": 0,
        })
        cur["voices"] += 1
        cur["max_speakers"] = max(cur["max_speakers"], v["speakers"])
    return sorted(best.values(), key=lambda l: l["label"])


def default_voices(assets: Path, language: str) -> list:
    """
    The most useful voice (or voices) for a language: the multi-speaker one
    if there is one, otherwise the best few single-speaker voices, since
    variety has to come from somewhere.
    """
    cands = [v for v in catalogue(assets) if v["language"] == language]
    if not cands:
        sys.exit(f"no Piper voices published for {language}")
    if cands[0]["speakers"] > 1:
        return [cands[0]["name"]]
    return [v["name"] for v in cands[:4]]


def _voice_path(assets: Path, name: str) -> str:
    """Repo subdirectory for a voice, derived from its name via the index."""
    entry = index(assets).get(name)
    if entry is None:
        sys.exit(f"unknown voice {name} — see `forge.py voices` for the catalogue")
    files = entry.get("files") or {}
    for f in files:
        if f.endswith(".onnx"):
            return f.rsplit("/", 1)[0]
    # Fall back to Piper's own layout: <lang-family>/<code>/<voice>/<quality>
    lang, rest = name.split("-", 1)
    voice, quality = rest.rsplit("-", 1)
    return f"{lang.split('_')[0]}/{lang}/{voice}/{quality}"


def ensure_voice(assets: Path, name: str) -> tuple[Path, dict]:
    """Download a voice's ONNX + config once. Both or neither."""
    subdir = _voice_path(assets, name)
    d = voice_dir(assets)
    d.mkdir(parents=True, exist_ok=True)
    onnx, cfg = d / f"{name}.onnx", d / f"{name}.onnx.json"
    for path, url in ((onnx, f"{HF_BASE}/{subdir}/{name}.onnx"),
                      (cfg,  f"{HF_BASE}/{subdir}/{name}.onnx.json")):
        if path.exists():
            continue
        # .part first: a truncated download is the right size to look
        # complete to the `exists()` check above on the next run.
        log(f"downloading {path.name}")
        part = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, part)
        part.rename(path)
    return onnx, json.loads(cfg.read_text())


def _session(onnx: Path):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.log_severity_level = 3
    # One thread per session: parallelism comes from the pool below, and
    # letting both fan out oversubscribes the machine.
    so.intra_op_num_threads = 1
    so.inter_op_num_threads = 1
    return ort.InferenceSession(str(onnx), so, providers=["CPUExecutionProvider"])


def _phoneme_ids(text: str, cfg: dict) -> list:
    from piper_phonemize import phoneme_ids_espeak, phonemize_espeak

    voice = cfg["espeak"]["voice"]
    ids = []
    for sentence in phonemize_espeak(text, voice):
        ids.extend(phoneme_ids_espeak(sentence, cfg["phoneme_id_map"]))
    return ids


def _to_16k(audio: np.ndarray, sr: int) -> np.ndarray:
    if sr == TARGET_RATE:
        return audio
    import librosa

    return librosa.resample(audio, orig_sr=sr, target_sr=TARGET_RATE)


_preview_cache = {}
_preview_lock = Lock()


def preview(text: str, assets: Path, voice: str = None, speaker: int = 0,
            language: str = None) -> bytes:
    """
    One clip of `text`, as WAV bytes, for listening to before committing to a
    training run.

    This exists because the phrase field accepts comma-separated spelling
    variants whose whole purpose is pronunciation — "hey clarra" for a British
    reading of "clara" — and until now the only way to find out what a variant
    actually sounds like was to generate 30,000 clips and train on them. The
    speaker is fixed by default so two variants can be compared without the
    voice changing underneath the comparison.
    """
    import io

    import soundfile as sf

    voice = voice or default_voices(assets, language or "en_GB")[0]
    with _preview_lock:
        if voice not in _preview_cache:
            onnx, cfg = ensure_voice(assets, voice)
            _preview_cache[voice] = (_session(onnx), cfg)
        sess, cfg = _preview_cache[voice]

    ids = _phoneme_ids(text, cfg)
    inf = cfg["inference"]
    feeds = {
        "input": np.array([ids], dtype=np.int64),
        "input_lengths": np.array([len(ids)], dtype=np.int64),
        "scales": np.array([inf["noise_scale"], inf["length_scale"],
                            inf["noise_w"]], dtype=np.float32),
    }
    if cfg["num_speakers"] > 1:
        feeds["sid"] = np.array([speaker % cfg["num_speakers"]], dtype=np.int64)
    audio = sess.run(None, feeds)[0].squeeze().astype(np.float32)
    peak = float(np.abs(audio).max())
    if peak > 0:
        audio = audio / peak * 0.9

    buf = io.BytesIO()
    # At the voice's own rate, not the training rate: this is for human ears,
    # and 16kHz would make every variant sound worse than it is.
    sf.write(buf, audio, cfg["audio"]["sample_rate"], format="WAV", subtype="PCM_16")
    return buf.getvalue()


def synthesize(phrases, n_samples: int, train_dir: Path, test_dir: Path,
               assets: Path, voices=None, language: str = None) -> None:
    import soundfile as sf

    voices = list(voices or default_voices(assets, language or "en_GB"))
    loaded = []
    for name in voices:
        onnx, cfg = ensure_voice(assets, name)
        loaded.append((name, _session(onnx), cfg))
        log(f"{name}: {cfg['num_speakers']} speakers, "
            f"{cfg['audio']['sample_rate']}Hz")

    train_dir.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    combos = []
    for name, sess, cfg in loaded:
        for sid in range(cfg["num_speakers"]):
            for ls, ns in itertools.product(LENGTH_SCALES, NOISE_SCALES):
                combos.append((name, sess, cfg, sid, ls, ns))
    random.shuffle(combos)
    log(f"{len(combos)} speaker/prosody combinations available")

    jobs = []
    for i, (name, sess, cfg, sid, ls, ns) in enumerate(
            itertools.islice(itertools.cycle(combos), n_samples)):
        out_dir = test_dir if random.random() < TEST_FRACTION else train_dir
        dest = out_dir / f"piper_gb_{i:06d}_{name}_s{sid}.wav"
        jobs.append((phrases[i % len(phrases)], name, sess, cfg, sid, ls, ns, dest))

    done = [0]
    lock = Lock()

    def synth_one(job):
        phrase, _name, sess, cfg, sid, ls_mul, ns_mul, dest = job
        try:
            ids = _phoneme_ids(phrase, cfg)
            inf = cfg["inference"]
            scales = np.array([
                inf["noise_scale"] * ns_mul,
                inf["length_scale"] * ls_mul,
                inf["noise_w"],
            ], dtype=np.float32)
            feeds = {
                "input": np.array([ids], dtype=np.int64),
                "input_lengths": np.array([len(ids)], dtype=np.int64),
                "scales": scales,
            }
            if cfg["num_speakers"] > 1:
                feeds["sid"] = np.array([sid], dtype=np.int64)
            audio = sess.run(None, feeds)[0].squeeze()
            audio = _to_16k(audio.astype(np.float32), cfg["audio"]["sample_rate"])
            # Piper returns float in roughly [-1, 1]; normalise per clip so a
            # quiet speaker is not systematically quieter than a loud one in
            # the training set, then leave headroom.
            peak = float(np.abs(audio).max())
            if peak > 0:
                audio = audio / peak * 0.9
            sf.write(dest, audio, TARGET_RATE, subtype="PCM_16")
        except Exception as e:
            return f"{type(e).__name__}: {str(e)[:100]}"
        with lock:
            done[0] += 1
            if done[0] % 200 == 0:
                log(f"…{done[0]}/{n_samples}")
        return None

    errors = {}
    # Modest fan-out: each session is single-threaded, and the phonemizer is
    # the serial part. More workers than cores buys nothing here.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for err in pool.map(synth_one, jobs):
            if err:
                errors[err] = errors.get(err, 0) + 1

    if errors:
        log(f"{sum(errors.values())} clips failed:")
        for msg, n in sorted(errors.items(), key=lambda kv: -kv[1])[:3]:
            log(f"  {n}x {msg}")
    log(f"wrote {done[0]} clips → {train_dir.parent}")
