"""Generate the E2E query-test audio corpus from manifest.json.

This is a one-time (or occasional, when the manifest changes) dev-time step,
not something the controller or CI runs. It needs the same heavyweight,
network-fetching TTS stack oww_forge already carries — onnxruntime,
piper-phonemize, soundfile, and librosa are pinned in oww_forge/requirements.txt
and baked into its Docker image — so this script is meant to run there, not in
the lightweight controller container e2e_query_test.py targets.

Usage, from a shell with oww_forge's dependencies and piper_voices.py
importable (e.g. inside the oww_forge image, or a venv built from
oww_forge/requirements.txt with oww_forge/ on PYTHONPATH):

    python generate_fixtures.py --assets /path/to/oww_forge/data

--assets is the oww_forge asset volume: piper voice ONNX files download there
once and are cached across runs (same layout piper_voices.py already uses for
wake-word training positives). Point it at oww_forge/data if you have one, or
any writable directory — it'll be created and populated on first use.

Silence and noise fixtures (the N4/N5 "not real speech" cases) need only the
stdlib and are generated even without the piper stack present, so a partial
environment still produces a partial, useful corpus rather than nothing.

Output lands beside this file (controller/tests/fixtures/e2e_audio/*.wav),
gitignored — see docs/e2e-query-testing.md for the full workflow, including
running this inside the oww_forge container and copying the results out.
"""

from __future__ import annotations

import argparse
import json
import random
import struct
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"

SAMPLE_RATE = 16_000  # matches em_test_audio's normalization target; the
                       # controller re-encodes on upload regardless, so the
                       # synthetic fixtures don't need to match a real voice's
                       # native rate.


def log(msg: str) -> None:
    print(f"[gen-e2e-fixtures] {msg}", flush=True)


def _write_pcm(path: Path, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def gen_silence(path: Path, duration_s: float) -> None:
    _write_pcm(path, [0] * int(SAMPLE_RATE * duration_s))


def gen_noise(path: Path, duration_s: float, amplitude: int = 800) -> None:
    # amplitude is well under full scale (32767) — this is meant to read as
    # room hiss/background noise, not a loud burst that could itself cross a
    # stopword/VAD threshold in an unrelated way.
    rng = random.Random(0)  # fixed seed: the corpus must be reproducible
    n = int(SAMPLE_RATE * duration_s)
    _write_pcm(path, [rng.randint(-amplitude, amplitude) for _ in range(n)])


def _append_silence(wav_bytes: bytes, duration_s: float) -> bytes:
    """Append duration_s of silence to an in-memory WAV, preserving its own
    sample rate/width/channels (piper's output, not SAMPLE_RATE above)."""
    import io

    src = io.BytesIO(wav_bytes)
    with wave.open(src, "rb") as r:
        params = r.getparams()
        frames = r.readframes(r.getnframes())
    pad_frames = int(params.framerate * duration_s)
    pad = b"\x00" * (pad_frames * params.sampwidth * params.nchannels)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        w.writeframes(frames + pad)
    return out.getvalue()


def _import_piper_voices():
    # Imported lazily so silence/noise generation works without the piper
    # stack installed at all (see module docstring). Three attempts, in
    # order: already on PYTHONPATH (e.g. invoked via forge.py's own
    # ENTRYPOINT, or PYTHONPATH set explicitly); the oww_forge image's fixed
    # install path (COPY'd to /opt/forge by its Dockerfile) — needed because
    # running this script directly, as `docker cp` + `python script.py`
    # does, puts THIS script's directory on sys.path[0], not /opt/forge;
    # and finally a sibling oww_forge/ directory for the "full repo
    # checkout, deps installed locally" case. HERE may not even have 3
    # parents when copied somewhere shallow like /tmp — checked rather than
    # indexed unconditionally.
    try:
        import piper_voices
        return piper_voices
    except ImportError:
        pass
    candidates = [Path("/opt/forge")]
    if len(HERE.parents) > 3:
        candidates.append(HERE.parents[3] / "oww_forge")  # repo root / oww_forge
    for candidate in candidates:
        if (candidate / "piper_voices.py").is_file():
            sys.path.insert(0, str(candidate))
            import piper_voices
            return piper_voices
    sys.exit(
        "piper_voices module not found. Run this inside the oww_forge "
        "image (e.g. `docker exec -e PYTHONPATH=/opt/forge <container> "
        "python generate_fixtures.py`), or from a full repo checkout with "
        "oww_forge's requirements installed."
    )


def gen_piper(path: Path, phrase: str, assets: Path, pad_silence_s: float = 0.0) -> None:
    piper_voices = _import_piper_voices()
    wav_bytes = piper_voices.preview(phrase, assets)
    if pad_silence_s:
        wav_bytes = _append_silence(wav_bytes, pad_silence_s)
    path.write_bytes(wav_bytes)


def generate_case(case: dict, assets: Path, force: bool) -> list[str]:
    """Generate the case's main fixture plus any setup_phrases fixtures.

    Returns the list of fixture filenames written (for logging); a case with
    no synthesizable audio (upload_reject/busy-without-its-own-fixture)
    produces nothing here.
    """
    written = []
    source = case.get("source")

    def _one(fixture: str, phrase: str | None, pad: float = 0.0) -> None:
        dest = HERE / fixture
        if dest.exists() and not force:
            log(f"skip (exists): {fixture}")
            return
        if source == "silence":
            gen_silence(dest, case.get("duration_s", 3.0))
        elif source == "noise":
            gen_noise(dest, case.get("duration_s", 3.0))
        elif source == "piper":
            gen_piper(dest, phrase, assets, pad)
        else:
            return
        written.append(fixture)
        log(f"wrote {fixture}")

    fixture = case.get("fixture")
    if fixture:
        _one(fixture, case.get("phrase"), case.get("pad_silence_s", 0.0))

    for i, setup_phrase in enumerate(case.get("setup_phrases", [])):
        setup_fixture = f"{case['id']}-setup-{i}.wav"
        _one(setup_fixture, setup_phrase)
        case.setdefault("_setup_fixtures", []).append(setup_fixture)

    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    ap.add_argument("--assets", type=Path, default=HERE / "piper_assets",
                     help="oww_forge asset volume for cached Piper voices")
    ap.add_argument("--case", action="append", help="generate only this case id (repeatable)")
    ap.add_argument("--force", action="store_true", help="regenerate even if the file already exists")
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    cases = manifest["cases"]
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c["id"] in wanted]
        missing = wanted - {c["id"] for c in cases}
        if missing:
            sys.exit(f"unknown case id(s): {', '.join(sorted(missing))}")

    args.assets.mkdir(parents=True, exist_ok=True)
    total = 0
    for case in cases:
        total += len(generate_case(case, args.assets, args.force))
    log(f"done — {total} file(s) written to {HERE}")


if __name__ == "__main__":
    main()
