"""
em_training_captures.py — wake-event audio capture for retraining
==================================================================

Records short 16kHz clips of the audio leading up to a wake-word ACTIVATION or
a NEAR-MISS, so an admin can listen and label each one "should have activated"
(positive) or "should have ignored" (negative), and hand the labelled result to
oww_forge for retraining.

The audio is captured entirely controller-side. The device already streams the
always-on wake stream (16kHz mono S16_LE) continuously, and the OWW listener in
em_controller already scores it and detects near-misses — so a small per-device
rolling ring buffer there is all the pre-roll this needs. No device firmware
change, no OTA, no new wire protocol. See
docs/design/2026-08-23-wake-capture-labeling-design.md.

Storage mirrors em_recordings and em_oww_models: files live under
`training_captures/` beside the SQLite DB, inside the persisted Docker volume.
Captures are GROUPED BY WAKE-WORD MODEL STEM (em_oww_models.prediction_key), not
by device, because that is the unit oww_forge retrains and the unit an admin
triages:

    training_captures/<model_stem>/{untriaged,positive,negative}/
        <device>_<ts_ms>_<kind>_<score_milli>.wav

Retention is asymmetric on purpose. Untriaged is capped per wake word
(UNTRIAGED_CAP, env EM_WAKE_CAPTURE_CAP) and pruned oldest-first — an opt-in
noisy room could otherwise fill a disk with raw speech. Labelled positive and
negative clips are NEVER auto-pruned: they are the training set, and losing them
silently would defeat the whole feature. They persist until exported and deleted
by hand.

Pure path/filesystem logic (no aiohttp, no db import) so it can be unit tested;
em_controller writes through it and em_api serves and exports from it.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import threading
import time
import zipfile
import wave
from pathlib import Path

import em_recordings

log = logging.getLogger("echomuse.training_captures")

CAPTURES_SUBDIR = "training_captures"

# The three triage states a capture can be in. Order is display order.
BUCKETS = ("untriaged", "positive", "negative")

# What triggered the capture. The wake kinds remain compatible with existing
# filenames. Stop kinds preserve the post-AFE scenario in the export manifest;
# the admin's positive/negative label remains the training polarity.
KINDS = ("act", "miss", "stop_act", "stop_miss", "false_stop", "playback_negative")

# Trim edits live beside the WAV rather than changing the source capture. This
# keeps undo/relabel non-destructive while exports still contain edited audio.
TRIM_SUFFIX = ".trim.json"
META_SUFFIX = ".meta.json"
_UPLOAD_LOCK = threading.RLock()

# The near-miss floor em_controller already uses for its near-miss counter.
# Anything at or below this is noise and is not worth a clip.
NEAR_MISS_FLOOR = 0.05

# Per-wake-word cap on UNTRIAGED clips. Env-overridable the same way EM_OWW_DIR
# is, so an operator can widen the labelling backlog without a code change.
# 200 clips of 2s at 16kHz mono ≈ 13MB per wake word.
try:
    UNTRIAGED_CAP = max(1, int(os.environ.get("EM_WAKE_CAPTURE_CAP", "200")))
except ValueError:
    UNTRIAGED_CAP = 200

# Wire format — reuse em_recordings so there is one definition of it.
SAMPLE_RATE = em_recordings.SAMPLE_RATE  # 16000

# ── Capture-window policy (consumed by em_controller's wake listener) ─────────
#
# These live here rather than in em_controller so the hot-path decision
# (plan_snapshot) is a pure function the test suite can exercise without
# importing openwakeword/aiohttp — the same reasoning that moved em_button and
# em_linkauth out of their call sites.
#
# The rolling ring is bounded to the longest pre-roll a device could ask for; a
# clip keeps the configured `wake_capture_sec` of its tail.
RING_MAX_SEC = 5.0
# One utterance crosses the near-miss floor on many consecutive 80ms frames, so
# without a refractory window a single "hey jarvis" would write dozens of
# near-duplicate clips. 3s collapses each utterance to one capture.
CAPTURE_DEBOUNCE_S = 3.0
# Below this a clip is not worth writing (one 80ms wake frame = 2560 bytes);
# the refractory window is deliberately NOT started on such a snapshot, so the
# next full frame can still produce the real clip.
MIN_CAPTURE_BYTES = int(0.08 * SAMPLE_RATE) * 2


def plan_snapshot(ring, wake_capture_sec: float, now: float,
                  last_capture_mono: float) -> bytes | None:
    """
    Decide whether one wake detection should produce a capture, and return the
    PCM slice to write — or None to skip (debounced, or too little audio
    buffered yet).

    Pure so em_controller's per-frame decision is testable without the
    controller. The caller updates its debounce clock only when this returns
    a clip, so a skipped-because-too-short detection does not arm the window.

    `now` and `last_capture_mono` are a monotonic clock (the event loop's);
    `ring` is the rolling wake-stream buffer (bytes or bytearray).
    """
    if now - last_capture_mono < CAPTURE_DEBOUNCE_S:
        return None
    sec = max(0.1, min(float(wake_capture_sec or 0.0), RING_MAX_SEC))
    nbytes = int(sec * SAMPLE_RATE * 2)
    nbytes -= nbytes % 2   # keep the slice on an S16 sample boundary
    if nbytes <= 0:
        return None
    pcm = bytes(ring[-nbytes:])
    if len(pcm) < MIN_CAPTURE_BYTES:
        return None
    return pcm

# A model stem is a directory name AND comes back off disk, so it is validated
# like any other path component rather than trusted. Matches the shell-/URL-safe
# stems em_oww_models.prediction_key produces.
_MODEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

# device_id is ro.serialno (hex), ts_ms a wall-clock millisecond stamp, kind one
# of KINDS, score the detection score ×1000 (an integer, so filenames stay
# shell-safe and sortable). Validated on the way back out of the filesystem.
_NAME_RE = re.compile(
    r"^(?P<device>[A-Za-z0-9_.-]{1,64})_(?P<ts>\d{1,19})_"
    r"(?P<kind>act|miss|stop_act|stop_miss|false_stop|playback_negative)_"
    r"(?P<score>\d{1,5})\.wav$"
)


def captures_dir(db_path: str | None = None) -> Path:
    """
    Resolve the captures root: `training_captures/` beside the SQLite DB
    (DB_PATH env, same default as em_controller). Absolute, so it stays valid
    regardless of the process cwd.
    """
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "echomuse.db")
    return Path(db_path).resolve().parent / CAPTURES_SUBDIR


def safe_model(model: str) -> str | None:
    """The wake-word model stem as a path component, or None if it isn't one."""
    if model and _MODEL_RE.fullmatch(model):
        return model
    return None


def _score_milli(score: float) -> int:
    """Detection score → integer thousandths, clamped to the filename field."""
    try:
        m = int(round(float(score) * 1000))
    except (TypeError, ValueError):
        m = 0
    return max(0, min(m, 99999))


def filename(device_id: str, ts_ms: int, kind: str, score: float) -> str | None:
    """Canonical filename for a capture, or None if unnameable."""
    safe = em_recordings.safe_device_id(device_id)
    if safe is None or kind not in KINDS:
        return None
    try:
        ts = int(ts_ms)
    except (TypeError, ValueError):
        return None
    if ts < 0:
        return None
    return f"{safe}_{ts}_{kind}_{_score_milli(score):04d}.wav"


def parse_filename(name: str) -> dict | None:
    """Structured fields for a capture filename, or None if it does not parse."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    return {
        "device_id": m.group("device"),
        "ts_ms": int(m.group("ts")),
        "kind": m.group("kind"),
        "score": int(m.group("score")) / 1000.0,
        "name": name,
    }


def _bucket_dir(model: str, bucket: str, db_path: str | None = None) -> Path | None:
    safe = safe_model(model)
    if safe is None or bucket not in BUCKETS:
        return None
    return captures_dir(db_path) / safe / bucket


def save(model: str, device_id: str, pcm: bytes, kind: str, score: float,
         db_path: str | None = None, cap: int = UNTRIAGED_CAP,
         sample_rate: int = SAMPLE_RATE, ts_ms: int | None = None) -> str | None:
    """
    Write one capture into the wake word's `untriaged/` bucket and prune it back
    to `cap` files (oldest first). Returns the filename written, or None if
    nothing was written. Blocking (runs in an executor at the call site).

    `ts_ms` is stamped here from wall-clock time by default so em_controller —
    which deliberately does not import `time`, using the event loop's monotonic
    clock — need not supply one.
    """
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    name = filename(device_id, ts_ms, kind, score)
    directory = _bucket_dir(model, "untriaged", db_path)
    if name is None or directory is None or not pcm:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    # Write-then-rename: a partially written WAV the API then serves is worse
    # than no capture at all.
    tmp = path.with_suffix(".wav.part")
    tmp.write_bytes(em_recordings.encode_wav(pcm, sample_rate))
    tmp.replace(path)
    prune_untriaged(model, db_path=db_path, cap=cap)
    log.info(
        f"[training] captured {kind} for '{model}' from {device_id} "
        f"({em_recordings.duration_ms(len(pcm))}ms) → untriaged/{name}"
    )
    return name


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + META_SUFFIX)


def _read_meta(path: Path) -> dict | None:
    try:
        data = json.loads(_meta_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _fsync_file(path: Path, data: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _find_uploaded(model: str, device_id: str, capture_id: str,
                   db_path: str | None = None) -> Path | None:
    for bucket in BUCKETS:
        directory = _bucket_dir(model, bucket, db_path)
        if directory is None or not directory.is_dir():
            continue
        for sidecar in directory.glob(f"*.wav{META_SUFFIX}"):
            wav = sidecar.with_name(sidecar.name[:-len(META_SUFFIX)])
            meta = _read_meta(wav)
            if (wav.is_file() and meta is not None
                    and meta.get("device_id") == device_id
                    and meta.get("captureId") == capture_id):
                return wav
    return None


def save_uploaded(model: str, device_id: str, metadata: dict, pcm: bytes,
                  db_path: str | None = None, cap: int = UNTRIAGED_CAP) -> str | None:
    """Durably commit an uploaded capture; exact duplicates return its name."""
    safe = em_recordings.safe_device_id(device_id)
    capture_id = metadata.get("captureId")
    kind = metadata.get("kind")
    score = metadata.get("score")
    directory = _bucket_dir(model, "untriaged", db_path)
    if safe is None or not isinstance(capture_id, str) or kind not in {"act", "miss"}:
        return None
    if directory is None or not pcm:
        return None
    with _UPLOAD_LOCK:
        existing = _find_uploaded(model, safe, capture_id, db_path)
        if existing is not None:
            existing_meta = _read_meta(existing)
            return existing.name if existing_meta == {**metadata, "device_id": safe} else None
        ts_ms = int(time.time() * 1000)
        name = filename(safe, ts_ms, kind, score)
        if name is None:
            return None
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        while path.exists():
            ts_ms += 1
            name = filename(safe, ts_ms, kind, score)
            path = directory / name
        committed_meta = {**metadata, "device_id": safe}
        wav_tmp = path.with_name(path.name + ".part")
        meta = _meta_path(path)
        meta_tmp = meta.with_name(meta.name + ".part")
        try:
            _fsync_file(wav_tmp, em_recordings.encode_wav(pcm, SAMPLE_RATE))
            _fsync_file(
                meta_tmp,
                json.dumps(committed_meta, sort_keys=True, separators=(",", ":")).encode(),
            )
            meta_tmp.replace(meta)
            # WAV is the commit marker: readers cannot observe uploaded speech
            # until its provenance sidecar is already durable and in place.
            wav_tmp.replace(path)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            wav_tmp.unlink(missing_ok=True)
            meta_tmp.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            log.warning("[training] uploaded capture commit failed: %s", exc)
            return None
        prune_untriaged(model, db_path=db_path, cap=cap)
        return name


def _list_names(directory: Path | None) -> list[dict]:
    """Parsed, valid capture entries in a directory, newest first."""
    if directory is None or not directory.is_dir():
        return []
    entries = [parse_filename(child.name) for child in directory.iterdir()]
    parsed = []
    for entry in entries:
        if entry is None:
            continue
        meta = _read_meta(directory / entry["name"])
        if meta is not None:
            entry = {**entry, "upload": meta}
        parsed.append(entry)
    parsed.sort(key=lambda e: e["ts_ms"], reverse=True)
    return parsed


def list_captures(model: str, bucket: str, db_path: str | None = None) -> list[dict]:
    """This wake word's captures in one bucket, newest first (parsed metadata)."""
    return _list_names(_bucket_dir(model, bucket, db_path))


def counts(model: str, db_path: str | None = None) -> dict:
    """{bucket: file_count} for one wake word."""
    return {b: len(list_captures(model, b, db_path)) for b in BUCKETS}


def list_models(db_path: str | None = None) -> list[dict]:
    """
    Every wake word that has any captures on disk, with its bucket counts.
    Sorted by model stem so the dashboard order is stable.
    """
    root = captures_dir(db_path)
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or safe_model(child.name) is None:
            continue
        out.append({"model": child.name, "counts": counts(child.name, db_path)})
    return out


def resolve(model: str, name: str, bucket: str | None = None,
            db_path: str | None = None) -> Path | None:
    """
    Path of an existing capture, or None. `name` must parse. If `bucket` is
    given the file must be in it; otherwise every bucket is searched.
    """
    if parse_filename(name) is None:
        return None
    buckets = (bucket,) if bucket else BUCKETS
    for b in buckets:
        directory = _bucket_dir(model, b, db_path)
        if directory is None:
            continue
        path = directory / name
        if path.is_file():
            return path
    return None


def _trim_path(path: Path) -> Path:
    return path.with_name(path.name + TRIM_SUFFIX)


def _read_trim(path: Path) -> dict | None:
    try:
        data = json.loads(_trim_path(path).read_text())
        start = float(data["start_ms"])
        end = float(data["end_ms"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        return None
    return {"start_ms": start, "end_ms": end}


def _write_trim(path: Path, start_ms: float, end_ms: float) -> bool:
    try:
        start = float(start_ms)
        end = float(end_ms)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        return False
    try:
        with wave.open(str(path), "rb") as source:
            duration_ms = source.getnframes() * 1000 / source.getframerate()
    except (OSError, wave.Error, ZeroDivisionError):
        return False
    if end > duration_ms or start >= duration_ms:
        return False
    meta = _trim_path(path)
    tmp = meta.with_suffix(meta.suffix + ".part")
    try:
        tmp.write_text(json.dumps({"start_ms": start, "end_ms": end}))
        tmp.replace(meta)
    except OSError as e:
        log.warning(f"[training] Could not write trim metadata for {path.name}: {e}")
        return False
    return True


def _cropped_wav(path: Path, trim: dict | None) -> bytes:
    """Return the original WAV or the exact frame range selected by the admin."""
    if trim is None:
        return path.read_bytes()
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        start = max(0, int(trim["start_ms"] * params.framerate / 1000))
        end = min(params.nframes, int(trim["end_ms"] * params.framerate / 1000))
        if end <= start:
            return path.read_bytes()
        source.setpos(start)
        frames = source.readframes(end - start)
    out = io.BytesIO()
    with wave.open(out, "wb") as dest:
        dest.setparams(params)
        dest.setnframes(0)
        dest.writeframes(frames)
    return out.getvalue()


def label(model: str, name: str, target: str, db_path: str | None = None,
          start_ms: float | None = None, end_ms: float | None = None) -> bool:
    """
    Move a capture into a bucket, from WHEREVER it currently is. Returns success.

    `target` may be any of BUCKETS, so this covers all three admin actions with
    one primitive: labelling an untriaged clip (`untriaged → positive/negative`),
    correcting a mislabel (`positive ↔ negative`), and sending a sorted clip
    back to the queue (`… → untriaged`). A wrong label is otherwise permanent
    unless discarded, and a wrong label in the training set is worse than a
    missing clip.

    Idempotent: a capture already in `target` is a no-op success, so a
    double-click cannot fail.
    """
    if target not in BUCKETS:
        return False
    src = resolve(model, name, db_path=db_path)
    dest_dir = _bucket_dir(model, target, db_path)
    if src is None or dest_dir is None:
        return False
    dest = dest_dir / name
    if (start_ms is None) != (end_ms is None):
        return False
    if start_ms is not None and not _write_trim(src, start_ms, end_ms):
        return False
    if src == dest:
        return True
    dest_dir.mkdir(parents=True, exist_ok=True)
    trim_src = _trim_path(src)
    trim_dest = _trim_path(dest)
    meta_src = _meta_path(src)
    meta_dest = _meta_path(dest)
    moved: list[tuple[Path, Path]] = []
    try:
        for source, target in (
            (src, dest), (trim_src, trim_dest), (meta_src, meta_dest),
        ):
            if source.is_file():
                source.replace(target)
                moved.append((source, target))
    except OSError as e:
        # Keep the capture and both metadata sidecars together if any rename
        # fails (for example, on a full or read-only volume).
        for source, target in reversed(moved):
            try:
                if target.is_file() and not source.exists():
                    target.replace(source)
            except OSError:
                pass
        log.warning(f"[training] Could not move {name} → {target}: {e}")
        return False
    return True


def discard(model: str, name: str, db_path: str | None = None) -> bool:
    """Delete a capture from whichever bucket holds it. Returns success."""
    path = resolve(model, name, db_path=db_path)
    if path is None:
        return False
    try:
        path.unlink()
        _trim_path(path).unlink(missing_ok=True)
        _meta_path(path).unlink(missing_ok=True)
    except OSError as e:
        log.warning(f"[training] Could not discard {name}: {e}")
        return False
    return True


def prune_untriaged(model: str, db_path: str | None = None,
                    cap: int = UNTRIAGED_CAP) -> list[str]:
    """
    Delete all but the `cap` newest UNTRIAGED captures for a wake word. Returns
    the filenames removed. Never raises — a failed unlink costs disk, not a
    detection. Labelled buckets are untouched.
    """
    directory = _bucket_dir(model, "untriaged", db_path)
    if directory is None:
        return []
    entries = list_captures(model, "untriaged", db_path)  # newest first
    removed: list[str] = []
    for entry in entries[max(cap, 0):]:
        try:
            (directory / entry["name"]).unlink()
            _trim_path(directory / entry["name"]).unlink(missing_ok=True)
            _meta_path(directory / entry["name"]).unlink(missing_ok=True)
            removed.append(entry["name"])
        except OSError as e:
            log.warning(f"[training] Could not prune {entry['name']}: {e}")
    return removed


def export_zip(model: str, db_path: str | None = None,
               delete_after: bool = False) -> bytes:
    """
    A ZIP of a wake word's LABELLED captures, laid out as `positive/…` and
    `negative/…` — the exact shape oww_forge's import expects. Untriaged is
    excluded: an export is a finished dataset, not the triage queue.

    A `manifest.json` rides alongside for provenance: which wake word, when it
    was exported, and the per-file kind/score/device, so a retrained model can
    be traced back to the clips (and score distribution) it came from. oww_forge
    imports the audio and ignores the manifest, so it costs nothing there.
    When `delete_after` is true, successfully exported labelled captures (and
    their trim metadata) are removed after the ZIP is fully assembled.
    """
    manifest = {
        "model": model,
        "exported_at": int(time.time()),
        "buckets": {},
        "clips": [],
    }
    buf = io.BytesIO()
    exported_paths: list[Path] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for bucket in ("positive", "negative"):
            directory = _bucket_dir(model, bucket, db_path)
            entries = list_captures(model, bucket, db_path) if directory else []
            written = 0
            for entry in entries:
                path = directory / entry["name"]
                if path.is_file():
                    trim = _read_trim(path)
                    z.writestr(f"{bucket}/{entry['name']}", _cropped_wav(path, trim))
                    exported_paths.append(path)
                    clip = {
                        "bucket": bucket, "name": entry["name"],
                        "kind": entry["kind"], "score": entry["score"],
                        "device_id": entry["device_id"], "ts_ms": entry["ts_ms"],
                        "trim": trim,
                    }
                    if entry.get("upload") is not None:
                        clip["upload"] = entry["upload"]
                    manifest["clips"].append(clip)
                    meta = _meta_path(path)
                    if meta.is_file():
                        z.write(meta, f"{bucket}/{meta.name}")
                    written += 1
            manifest["buckets"][bucket] = written
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    if delete_after:
        for path in exported_paths:
            try:
                path.unlink()
                _trim_path(path).unlink(missing_ok=True)
                _meta_path(path).unlink(missing_ok=True)
            except OSError as e:
                log.warning(f"[training] Could not remove exported capture {path.name}: {e}")
    return buf.getvalue()


def delete_device(device_id: str, db_path: str | None = None) -> int:
    """
    Remove every capture belonging to a device, across all wake words and
    buckets. Returns the count deleted. Wired into db.delete_device so a removed
    device leaves no recognisable speech behind, the parity em_recordings has.
    """
    safe = em_recordings.safe_device_id(device_id)
    root = captures_dir(db_path)
    if safe is None or not root.is_dir():
        return 0
    deleted = 0
    for model_dir in root.iterdir():
        if not model_dir.is_dir():
            continue
        for bucket in BUCKETS:
            bdir = model_dir / bucket
            if not bdir.is_dir():
                continue
            for child in bdir.iterdir():
                parsed = parse_filename(child.name)
                if parsed and parsed["device_id"] == safe:
                    try:
                        child.unlink()
                        _trim_path(child).unlink(missing_ok=True)
                        _meta_path(child).unlink(missing_ok=True)
                        deleted += 1
                    except OSError as e:
                        log.warning(f"[training] Could not delete {child.name}: {e}")
    return deleted
