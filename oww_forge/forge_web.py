"""Web UI for oww_forge — a thin aiohttp layer over forge.py.

Everything heavy (asset downloads, training) runs as a forge.py subprocess —
one at a time, streaming to a log file the UI tails. State is derived from
the /data tree on every poll, so the UI survives container restarts and
stays honest about what actually exists on disk.

No auth: this is a LAN batch tool, same trust model as `docker compose run`.
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent))
import forge

LOGS = forge.DATA / "logs"
TMP = forge.DATA / "tmp"
STATIC = Path(__file__).parent / "static"

FORGE_PY = str(Path(__file__).parent / "forge.py")


class Job:
    def __init__(self, kind: str, label: str, argv: list):
        self.kind = kind
        self.label = label
        self.started = time.time()
        self.rc = None
        LOGS.mkdir(parents=True, exist_ok=True)
        self.log_path = LOGS / f"{int(self.started)}_{kind}.log"
        self._logf = open(self.log_path, "wb", buffering=0)
        self.proc = subprocess.Popen(
            [sys.executable, "-u", FORGE_PY, *argv],
            stdout=self._logf,
            stderr=subprocess.STDOUT,
        )

    def poll(self):
        if self.rc is None:
            rc = self.proc.poll()
            if rc is not None:
                self.rc = rc
                self._logf.close()
        return self.rc

    def as_dict(self):
        self.poll()
        return {
            "kind": self.kind,
            "label": self.label,
            "running": self.rc is None,
            "rc": self.rc,
            "started": self.started,
        }


_job: Job | None = None
_gpu_info: dict | None = None


def _start_job(kind: str, label: str, argv: list) -> None:
    global _job
    if _job and _job.poll() is None:
        raise web.HTTPConflict(text=f"a job is already running: {_job.label}")
    _job = Job(kind, label, argv)


def _gpu() -> dict:
    """Probe CUDA once, in a subprocess (importing torch here would pin ~1GB)."""
    global _gpu_info
    # ROCm can report a device a few seconds after the container starts. Do
    # not cache that transient false result for the lifetime of the UI.
    if _gpu_info is None or not _gpu_info["available"]:
        try:
            out = subprocess.run(
                [sys.executable, "-c",
                 "import torch,json;print(json.dumps({'available':torch.cuda.is_available(),"
                 "'device':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,"
                 "'torch':torch.__version__}))"],
                capture_output=True, text=True, timeout=60,
            )
            _gpu_info = json.loads(out.stdout.strip().splitlines()[-1])
        except Exception as e:
            _gpu_info = {"available": False, "device": None, "error": str(e)}
    return _gpu_info


def _count(path: Path, pattern: str = "*.wav") -> int:
    return sum(1 for _ in path.glob(pattern)) if path.is_dir() else 0


def _size_mb(path: Path) -> float:
    return round(path.stat().st_size / 1e6, 1) if path.exists() else 0


def _assets_state() -> list:
    neg = forge.FEATURES_DIR / forge.NEGATIVE_FEATURES
    val = forge.FEATURES_DIR / forge.VALIDATION_FEATURES
    return [
        {"part": "piper", "label": "Piper LibriTTS-R checkpoint",
         "present": forge.PIPER_CKPT.exists(), "detail": f"{_size_mb(forge.PIPER_CKPT)} MB"},
        {"part": "features", "label": "Negative + validation features",
         "present": neg.exists() and val.exists(),
          "detail": f"{_size_mb(neg) / 1000:.1f} GB + {_size_mb(val)} MB"},
        {"part": "common_voice", "label": "Common Voice 26 English archive",
         "present": bool(list(forge.COMMON_VOICE_DIR.glob("*.tar.gz"))),
         "detail": "~110 GB including features; CC0; needs MDC_API_KEY"},
        {"part": "common_voice_features", "label": "Common Voice training features",
         "present": (forge.FEATURES_DIR / forge.COMMON_VOICE_FEATURES).exists(),
         "detail": f"{_size_mb(forge.FEATURES_DIR / forge.COMMON_VOICE_FEATURES) / 1000:.1f} GB; all validated clips"},
        {"part": "ami", "label": "AMI distant-mic validation",
         "present": (forge.FEATURES_DIR / forge.AMI_VALIDATION_FEATURES).exists(),
          "detail": f"{_size_mb(forge.FEATURES_DIR / forge.AMI_VALIDATION_FEATURES)} MB; CC-BY-4.0, ~7h"},
        {"part": "fleurs", "label": "FLEURS multilingual speech",
         "present": (forge.FEATURES_DIR / forge.FLEURS_FEATURES).exists(),
         "detail": f"{_size_mb(forge.FEATURES_DIR / forge.FLEURS_FEATURES)} MB; priority 1, ~0.5 GB feature target"},
        {"part": "voxpopuli", "label": "VoxPopuli accent speech",
         "present": (forge.FEATURES_DIR / forge.VOXPOPULI_FEATURES).exists(),
         "detail": f"{_size_mb(forge.FEATURES_DIR / forge.VOXPOPULI_FEATURES)} MB; priority 2, ~0.5 GB target, CC0"},
        {"part": "musan", "label": "MUSAN speech, music, noise",
         "present": forge.dir_has_files(forge.MUSAN_DIR, "*.wav"),
         "detail": "11 GB archive; priority 4, CC-BY-4.0"},
        {"part": "slr28", "label": "OpenSLR 28 RIR and noise",
         "present": forge.dir_has_files(forge.SLR28_RIR_DIR, "*.wav"),
         "detail": "1.3 GB; priority 5, Apache-2.0"},
        {"part": "rirs", "label": "MIT room impulse responses",
         "present": forge.dir_has_files(forge.RIR_DIR),
         "detail": f"{_count(forge.RIR_DIR)} clips"},
        {"part": "audioset", "label": "AudioSet background noise",
         "present": forge.dir_has_files(forge.AUDIOSET_DIR),
         "detail": f"{_count(forge.AUDIOSET_DIR)} clips"},
        {"part": "fma", "label": "FMA background music",
         "present": forge.dir_has_files(forge.FMA_DIR),
         "detail": f"{_count(forge.FMA_DIR)} clips"},
    ]


def _wakewords_state() -> list:
    words = []
    if not forge.WAKEWORDS.is_dir():
        return words
    for cfg_path in sorted(forge.WAKEWORDS.glob("*/config.yml")):
        try:
            cfg = yaml.safe_load(cfg_path.read_text())
        except Exception:
            continue
        name = cfg["model_name"]
        work = Path(cfg["output_dir"]) / name
        model = forge.MODELS / f"{name}.onnx"
        words.append({
            "name": name,
            "phrases": cfg.get("target_phrase", []),
            "confusables": cfg.get("custom_negative_phrases", []),
            "google_tts_languages": cfg.get("google_tts_languages", "en-US,en-GB,en-AU,en-IN,en-PH,en-SG,en-ZA"),
            "google_tts_voices": cfg.get("google_tts_voices", ""),
            "google_tts_samples_per_voice": cfg.get("google_tts_samples_per_voice", 250),
            "n_samples": cfg.get("n_samples"),
            "steps": cfg.get("steps"),
            "clips_train": _count(work / "positive_train"),
            "clips_test": _count(work / "positive_test"),
            "features_built": (work / "positive_features_train.npy").exists(),
            "model_built": model.exists(),
            "model_size_kb": round(model.stat().st_size / 1e3) if model.exists() else None,
            "model_mtime": model.stat().st_mtime if model.exists() else None,
            "use_common_voice_negatives": bool(cfg.get("use_common_voice_negatives")),
            "use_ami_farfield_validation": bool(cfg.get("use_ami_farfield_validation")),
            "use_fleurs_negatives": bool(cfg.get("use_fleurs_negatives")),
            "use_voxpopuli_negatives": bool(cfg.get("use_voxpopuli_negatives")),
            "use_musan_background": bool(cfg.get("use_musan_background")),
            "use_slr28_augmentation": bool(cfg.get("use_slr28_augmentation")),
        })
    return words


async def api_state(request):
    return web.json_response({
        "gpu": _gpu(),
        "assets": _assets_state(),
        "credentials": {"mdc_api_key": forge.masked_mdc_api_key()},
        "wakewords": _wakewords_state(),
        "job": _job.as_dict() if _job else None,
    })


async def api_log(request):
    offset = int(request.query.get("offset", 0))
    if _job is None or not _job.log_path.exists():
        return web.json_response({"offset": 0, "data": ""})
    with open(_job.log_path, "rb") as f:
        f.seek(offset)
        data = f.read(65536)
    return web.json_response({"offset": offset + len(data),
                              "data": data.decode("utf-8", "replace")})


async def api_assets_download(request):
    body = await request.json() if request.can_read_body else {}
    only = body.get("only")
    argv = ["assets"] + (["--only", only] if only else [])
    _start_job("assets", f"downloading assets{f' ({only})' if only else ''}", argv)
    return web.json_response({"ok": True})


async def api_mdc_api_key(request):
    body = await request.json()
    key = (body.get("api_key") or "").strip()
    if not key:
        raise web.HTTPBadRequest(text="an API key is required")
    forge.save_mdc_api_key(key)
    return web.json_response({"ok": True, "masked": forge.masked_mdc_api_key()})


async def api_wakeword_create(request):
    body = await request.json()
    phrase = (body.get("phrase") or "").strip()
    if not phrase:
        raise web.HTTPBadRequest(text="phrase is required")
    ns = SimpleNamespace(
        phrase=phrase,
        name=(body.get("name") or "").strip() or None,
        samples=int(body.get("samples") or 30000),
        samples_val=int(body.get("samples_val") or 2000),
        steps=int(body.get("steps") or 50000),
        confusables=(body.get("confusables") or "").strip(),
        google_tts_languages=(body.get("google_tts_languages") or "en-US,en-GB,en-AU,en-IN,en-PH,en-SG,en-ZA").strip(),
        google_tts_voices=(body.get("google_tts_voices") or "").strip(),
        google_tts_samples_per_voice=int(body.get("google_tts_samples_per_voice") or 250),
        force=False,
    )
    try:
        forge.cmd_new(ns)
    except SystemExit as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"ok": True, "name": ns.name or forge.slugify(phrase)})


def _require_wakeword(name: str) -> None:
    if not (forge.WAKEWORDS / name / "config.yml").exists():
        raise web.HTTPNotFound(text=f"unknown wake word: {name}")


def _to_wav16k(src: Path, dest: Path) -> None:
    """Any browser/phone audio (webm/opus, m4a, mp3, wav…) → 16kHz mono wav."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(dest)],
        check=True, timeout=60,
    )


async def _save_uploads(request, field_name: str) -> list:
    TMP.mkdir(parents=True, exist_ok=True)
    reader = await request.multipart()
    paths = []
    async for field in reader:
        if field.name != field_name:
            continue
        suffix = Path(field.filename or "clip.webm").suffix or ".webm"
        dest = TMP / f"up_{int(time.time() * 1000)}_{len(paths)}{suffix}"
        with open(dest, "wb") as f:
            while chunk := await field.read_chunk():
                f.write(chunk)
        paths.append(dest)
    return paths


async def api_import_dataset(request):
    """A labelled dataset ZIP (positive/ negative/) exported from the EchoMuse
    dashboard → the wake word's train/test dirs, split TEST_FRACTION for test.
    Convert + split live in forge.import_labeled_dataset so the CLI and the UI
    behave identically."""
    name = request.match_info["name"]
    _require_wakeword(name)
    if _job and _job.poll() is None:
        raise web.HTTPConflict(text="cannot import while a job is running")
    uploads = await _save_uploads(request, "dataset")
    if not uploads:
        raise web.HTTPBadRequest(text="no dataset zip uploaded")
    try:
        counts = forge.import_labeled_dataset(name, uploads[0])
    except FileNotFoundError as e:
        raise web.HTTPNotFound(text=str(e))
    except Exception as e:
        raise web.HTTPBadRequest(text=f"import failed: {e}")
    finally:
        for p in uploads:
            p.unlink(missing_ok=True)
    return web.json_response({"ok": True, "counts": counts})


async def api_build(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    cfg_path = forge.WAKEWORDS / name / "config.yml"
    cfg = yaml.safe_load(cfg_path.read_text())
    missing = forge.missing_assets(cfg)
    if missing:
        raise web.HTTPConflict(text="missing assets:\n" + "\n".join(missing))
    body = await request.json() if request.can_read_body else {}
    argv = ["build", name]
    if body.get("from_step") and body["from_step"] != "generate":
        argv += ["--from-step", body["from_step"]]
    _start_job("build", f"building '{name}'", argv)
    return web.json_response({"ok": True})


async def api_dataset_options(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    if _job and _job.poll() is None:
        raise web.HTTPConflict(text="cannot change dataset options while a job is running")
    body = await request.json()
    cfg_path = forge.WAKEWORDS / name / "config.yml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["use_common_voice_negatives"] = bool(body.get("use_common_voice_negatives"))
    cfg["use_ami_farfield_validation"] = bool(body.get("use_ami_farfield_validation"))
    cfg["use_fleurs_negatives"] = bool(body.get("use_fleurs_negatives"))
    cfg["use_voxpopuli_negatives"] = bool(body.get("use_voxpopuli_negatives"))
    cfg["use_musan_background"] = bool(body.get("use_musan_background"))
    cfg["use_slr28_augmentation"] = bool(body.get("use_slr28_augmentation"))
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return web.json_response({"ok": True})


async def api_confusables(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    if _job and _job.poll() is None:
        raise web.HTTPConflict(text="cannot change confusables while a job is running")
    body = await request.json()
    phrases = body.get("phrases")
    if not isinstance(phrases, list) or not all(isinstance(phrase, str) for phrase in phrases):
        raise web.HTTPBadRequest(text="phrases must be a list of strings")
    # Preserve entry order while normalizing the text train.py receives.
    normalized = list(dict.fromkeys(phrase.strip().lower() for phrase in phrases if phrase.strip()))
    cfg_path = forge.WAKEWORDS / name / "config.yml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["custom_negative_phrases"] = normalized
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return web.json_response({"ok": True, "phrases": normalized})


async def api_save_google_tts_config(request):
    """Persist Google TTS fields without starting a synthesis job."""
    name = request.match_info["name"]
    _require_wakeword(name)
    body = await request.json()
    try:
        samples = int(body.get("samples"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="samples per voice/locale must be an integer")
    if samples < 1:
        raise web.HTTPBadRequest(text="samples per voice/locale must be positive")
    languages = str(body.get("languages") or "").strip()
    if not languages:
        raise web.HTTPBadRequest(text="at least one locale is required")
    cfg_path = forge.WAKEWORDS / name / "config.yml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["google_tts_samples_per_voice"] = samples
    cfg["google_tts_languages"] = languages
    cfg["google_tts_voices"] = str(body.get("voices") or "").strip()
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return web.json_response({"ok": True, "samples": samples,
                              "languages": languages,
                              "voices": cfg["google_tts_voices"]})


async def api_google_tts(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    body = await request.json() if request.can_read_body else {}
    cfg = yaml.safe_load((forge.WAKEWORDS / name / "config.yml").read_text())
    try:
        samples = int(body.get("samples") or cfg.get("google_tts_samples_per_voice", 250))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="samples per voice/locale must be an integer")
    if samples < 1:
        raise web.HTTPBadRequest(text="samples per voice/locale must be positive")
    langs = (body.get("languages") or cfg.get("google_tts_languages") or "en-US").strip()
    voices = (body.get("voices") or cfg.get("google_tts_voices") or "").strip()
    cfg["google_tts_languages"] = langs
    cfg["google_tts_voices"] = voices
    cfg["google_tts_samples_per_voice"] = samples
    (forge.WAKEWORDS / name / "config.yml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    argv = ["google-tts", name, "--samples", str(samples), "--languages", langs, "--yes"]
    if voices:
        argv += ["--voices", voices]
    scope = voices or "all matching Chirp 3 voices"
    _start_job("google-tts", f"Google TTS × {samples}/voice/locale ({langs}; {scope}) for '{name}'", argv)
    return web.json_response({"ok": True})


async def api_google_tts_voices(request):
    languages = request.query.get("languages", "en-US,en-GB,en-AU,en-IN,en-PH,en-SG,en-ZA")
    import google_tts
    try:
        voices = google_tts.list_chirp3_voices(languages.split(","))
    except Exception as error:
        raise web.HTTPServiceUnavailable(text=str(error))
    return web.json_response({"voices": voices})


async def api_test(request):
    name = request.match_info["name"]
    if not (forge.MODELS / f"{name}.onnx").exists():
        raise web.HTTPNotFound(text="model not built yet")
    uploads = await _save_uploads(request, "wav")
    if not uploads:
        raise web.HTTPBadRequest(text="no audio uploaded")
    wavs = []
    try:
        for src in uploads:
            wav = src.with_suffix(".conv.wav")
            try:
                _to_wav16k(src, wav)
            except Exception as e:
                return web.json_response({"ok": False, "output": f"could not decode audio: {e}"})
            wavs.append(wav)
        out = subprocess.run(
            [sys.executable, FORGE_PY, "test", name, "--wav", *map(str, wavs)],
            capture_output=True, text=True, timeout=300,
        )
        return web.json_response({"ok": out.returncode == 0,
                                  "output": out.stdout + out.stderr})
    finally:
        for p in uploads + wavs:
            p.unlink(missing_ok=True)


async def api_evaluate(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    model_path = forge.MODELS / f"{name}.onnx"
    if not model_path.exists():
        raise web.HTTPConflict(text=f"model {name}.onnx does not exist yet (build first)")
    _start_job("eval", f"evaluating '{name}'", ["eval", name])
    return web.json_response({"ok": True})


async def api_delete(request):
    name = request.match_info["name"]
    _require_wakeword(name)
    if _job and _job.poll() is None and name in _job.label:
        raise web.HTTPConflict(text="a job for this wake word is running")
    shutil.rmtree(forge.WAKEWORDS / name, ignore_errors=True)
    (forge.MODELS / f"{name}.onnx").unlink(missing_ok=True)
    return web.json_response({"ok": True})


async def api_model_download(request):
    name = request.match_info["name"]
    path = forge.MODELS / f"{name}.onnx"
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={
        "Content-Disposition": f'attachment; filename="{name}.onnx"'})


async def index(request):
    return web.FileResponse(STATIC / "index.html")


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/log", api_log)
    app.router.add_post("/api/assets/download", api_assets_download)
    app.router.add_post("/api/settings/mdc-api-key", api_mdc_api_key)
    app.router.add_post("/api/wakewords", api_wakeword_create)
    app.router.add_post("/api/wakewords/{name}/build", api_build)
    app.router.add_post("/api/wakewords/{name}/datasets", api_dataset_options)
    app.router.add_post("/api/wakewords/{name}/confusables", api_confusables)
    app.router.add_post("/api/wakewords/{name}/google-tts-config", api_save_google_tts_config)
    app.router.add_post("/api/wakewords/{name}/google-tts", api_google_tts)
    app.router.add_get("/api/google-tts/voices", api_google_tts_voices)
    app.router.add_post("/api/wakewords/{name}/test", api_test)
    app.router.add_post("/api/wakewords/{name}/evaluate", api_evaluate)
    app.router.add_post("/api/wakewords/{name}/import-dataset", api_import_dataset)
    app.router.add_delete("/api/wakewords/{name}", api_delete)
    app.router.add_get("/api/models/{name}.onnx", api_model_download)
    return app


def run(host: str = "0.0.0.0", port: int = 8769) -> None:
    print(f"[forge-ui] listening on http://{host}:{port}", flush=True)
    web.run_app(make_app(), host=host, port=port, print=None)
