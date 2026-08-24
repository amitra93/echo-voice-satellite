"""Extra positive training samples via Google Cloud Text-to-Speech.

Piper's LibriTTS-R generator provides sample volume (hundreds of speakers,
cheap, local); Google's Neural2/Studio/Chirp voices add a different — and
much higher-fidelity — acoustic character. A modest layer of Google samples
(a few thousand) on top of the Piper set adds voice diversity the model
can't get from a single TTS family.

Clips are written straight into the wake word's positive_train/positive_test
directories. openWakeWord's generate step counts existing files toward
n_samples, so Google clips added *before* `forge.py build` simply displace
that many Piper generations rather than growing the set.

Auth: set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON with the
Cloud Text-to-Speech API enabled (compose maps /data/google-credentials.json).
"""

import io
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TEST_FRACTION = 0.1
# per-character prices (USD per 1M chars) for a rough estimate only
EST_PRICE_PER_MCHAR = 16.0
VOICE_LIST_RETRIES = (15, 30, 60, 120)
TTS_RETRIES = (15, 30, 60)
DEFAULT_QPS = 2.0


def log(msg: str) -> None:
    print(f"[google-tts] {msg}", flush=True)


def _list_voices_with_retry(client):
    """Wait through transient per-minute TTS quota exhaustion."""
    from google.api_core.exceptions import ResourceExhausted

    for attempt, delay in enumerate((0, *VOICE_LIST_RETRIES)):
        if delay:
            log(f"voice inventory rate-limited; retrying in {delay}s")
            time.sleep(delay)
        try:
            return client.list_voices()
        except ResourceExhausted:
            if attempt == len(VOICE_LIST_RETRIES):
                raise


class _RequestPacer:
    """Serialize TTS calls so worker concurrency cannot burst the quota."""

    def __init__(self, interval: float):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_request = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(self.next_request - now, 0.0)
            self.next_request = max(now, self.next_request) + self.interval
        if delay:
            time.sleep(delay)


def _write_wav16k(audio_content: bytes, dest: Path) -> None:
    """Normalize any Google WAV response to the Forge 16kHz mono contract."""
    import librosa
    import numpy as np
    import soundfile as sf

    audio, sample_rate = sf.read(io.BytesIO(audio_content), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sample_rate != 16000:
        audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)
    audio = np.clip(audio, -1.0, 1.0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sf.write(dest, audio, 16000, subtype="PCM_16")


def list_chirp3_voices(languages):
    """Return available Chirp 3 voice names grouped by supported locale."""
    try:
        from google.cloud import texttospeech
    except ImportError:
        raise RuntimeError("google-cloud-texttospeech is not installed in this image")
    client = texttospeech.TextToSpeechClient()
    languages = tuple(dict.fromkeys(l.strip() for l in languages if l.strip()))
    result = {locale: [] for locale in languages}
    for voice in _list_voices_with_retry(client).voices:
        if "Chirp3" not in voice.name:
            continue
        for locale in languages:
            if locale in voice.language_codes:
                result[locale].append(voice.name)
    for names in result.values():
        names.sort()
    return result


def synthesize(phrases, samples_per_voice, train_dir: Path, test_dir: Path,
               languages, voice_names=(), assume_yes=False, qps=DEFAULT_QPS) -> None:
    """Synthesize every selected Chirp 3 voice/locale pair equally.

    ``samples_per_voice`` applies independently to each usable voice and
    locale pair. A voice requested by name must still be a Chirp 3 voice and
    support at least one requested locale.
    """
    try:
        from google.cloud import texttospeech
    except ImportError:
        sys.exit("google-cloud-texttospeech is not installed in this image")

    try:
        client = texttospeech.TextToSpeechClient()
    except Exception as e:
        sys.exit(
            f"could not create TTS client ({e}) — set GOOGLE_APPLICATION_CREDENTIALS "
            "to a service-account JSON (see oww_forge/README.md)"
        )

    languages = tuple(dict.fromkeys(l.strip() for l in languages if l.strip()))
    requested = tuple(dict.fromkeys(v.strip() for v in voice_names if v.strip()))
    if not languages:
        sys.exit("at least one locale is required")
    if samples_per_voice < 1:
        sys.exit("samples per voice must be positive")
    if qps <= 0:
        sys.exit("queries per second must be positive")

    try:
        inventory = [v for v in _list_voices_with_retry(client).voices if "Chirp3" in v.name]
    except Exception as error:
        sys.exit(f"could not list Chirp 3 voices after quota retries: {error}")
    by_name = {v.name: v for v in inventory}
    missing = sorted(set(requested) - set(by_name))
    if missing:
        sys.exit("requested voices are unavailable or not Chirp 3: " + ", ".join(missing))
    voices = [by_name[name] for name in requested] if requested else inventory
    pairs = [(locale, voice) for locale in languages for voice in voices
             if locale in voice.language_codes]
    if not pairs:
        sys.exit(f"no Chirp 3 voices matched locales {list(languages)}")
    unavailable_locales = [locale for locale in languages if not any(p[0] == locale for p in pairs)]
    if unavailable_locales:
        log("no selected Chirp 3 voices for: " + ", ".join(unavailable_locales))
    selected_voice_names = {voice.name for _, voice in pairs}
    log(f"{len(pairs)} locale/voice pairs, {len(selected_voice_names)} Chirp 3 voices, "
        f"{samples_per_voice:,} samples per pair")

    n_samples = len(pairs) * samples_per_voice
    n_chars = sum(len(p) for p in phrases) / len(phrases) * n_samples
    log(f"~{n_chars} characters ≈ ${n_chars / 1e6 * EST_PRICE_PER_MCHAR:.2f} "
        f"(premium-voice rate, estimate only)")
    if not assume_yes:
        reply = input("continue? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            sys.exit("aborted")

    train_dir.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    jobs = []
    for pair_index, (locale, voice) in enumerate(pairs):
        safe_voice = re.sub(r"[^A-Za-z0-9_.-]+", "_", voice.name)
        for sample_index in range(samples_per_voice):
            index = pair_index * samples_per_voice + sample_index
            phrase = phrases[index % len(phrases)]
            # Stable split makes an interrupted/retried run idempotent.
            out_dir = test_dir if index % round(1 / TEST_FRACTION) == 0 else train_dir
            dest = out_dir / f"google_{locale}_{safe_voice}_{sample_index:06d}.wav"
            jobs.append((phrase, locale, voice, dest))

    bad_voices = set()
    request_pacer = _RequestPacer(1.0 / qps)

    def synth_one(job):
        phrase, locale, voice, dest = job
        if voice.name in bad_voices:
            return 0
        if dest.exists():
            return 1
        req = dict(
            input=texttospeech.SynthesisInput(text=phrase),
            voice=texttospeech.VoiceSelectionParams(
                language_code=locale,
                name=voice.name,
                ssml_gender=voice.ssml_gender,
            ),
        )
        try:
            from google.api_core.exceptions import ResourceExhausted

            for attempt, delay in enumerate((0, *TTS_RETRIES)):
                if delay:
                    time.sleep(delay)
                try:
                    request_pacer.wait()
                    resp = client.synthesize_speech(
                        **req,
                        audio_config=texttospeech.AudioConfig(
                            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                            sample_rate_hertz=16000,
                        ),
                    )
                    break
                except ResourceExhausted:
                    if attempt == len(TTS_RETRIES):
                        raise
            # The service may return Chirp audio at 24kHz despite the requested
            # output rate. Normalize from the WAV header instead of trusting
            # the request parameter.
            _write_wav16k(resp.audio_content, dest)
            return 1
        except Exception:
            bad_voices.add(voice.name)
            return 0

    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for ok in pool.map(synth_one, jobs):
            done += ok
            if done and done % 200 == 0:
                log(f"…{done}/{n_samples}")
    if bad_voices:
        log(f"skipped {len(bad_voices)} incompatible voices: {sorted(bad_voices)[:10]}…")
    log(f"wrote {done} clips → {train_dir.parent}")
    if done < n_samples * 0.5:
        log("WARNING: more than half the requests failed — check API quota/credentials")
