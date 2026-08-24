# Wake-word capture, labeling & oww_forge retraining — design

**Date:** 2026-08-23
**Status:** approved

## Goal

Opportunistically record short 16kHz audio clips around wake-word activations
**and** near-misses, let an admin triage them in the dashboard ("should have
activated" / "should have ignored"), and hand the labeled result to
`oww_forge` for retraining — preserving oww_forge's existing 90/10 train/test
split policy.

## Key architectural decision: capture is controller-side

The device **already** streams exactly the audio needed. The always-on wake
stream (`mic_start` without `lock_mic`) sends every 80ms frame continuously —
silence included — as **16kHz mono S16_LE** (`em_controller.CHUNK_BYTES =
1280*2`), and wake-word scoring *and* near-miss detection already run
controller-side in the OWW listener loop (`em_controller.py` ~1780–2160),
consuming that stream off `device.mic_queue`. This holds in every
`owwOnDevice` mode (in `on` mode the controller still scores in parallel for
`ctrl_shadow`).

Therefore pre-roll capture is done **entirely controller-side** by keeping a
per-device rolling ring buffer in that loop and snapshotting it on a
wake/near-miss. **No Go firmware change, no OTA, no device ring buffer, no new
wire protocol.**

## Locked decisions

- **Capture location:** controller-side (above).
- **Triggers:** both activations and near-misses (`score > 0.05`). The label,
  not the trigger, decides positive/negative — an activation marked "should
  have ignored" is a false-accept negative; a near-miss marked "should have
  activated" is a false-reject positive.
- **Retention:** opt-in per device (default OFF). Untriaged is bounded by a
  per-wake-word cap (default 200, env `EM_WAKE_CAPTURE_CAP`); labeled
  positive/negative persist until exported or deleted.
- **oww_forge transport:** controller exposes an admin ZIP export; oww_forge
  gains an "Import labeled dataset" upload action. No cross-container coupling.
- **Clip window:** pre-roll only (configured seconds up to the trigger
  instant), no post-roll. Default pre-roll **2.0s**.
- **Grouping:** by wake-word model stem (`em_oww_models.prediction_key`), so a
  device that switches wake words segregates data and oww_forge imports exactly
  the wake word being retrained.

## Components

### 1. Capture (`em_controller.py`)

- `Device` gains `save_wake_captures: bool`, `wake_capture_sec: float`,
  `wake_ring: bytearray` (sized to a max window ≈ 5s → 160KB), and a
  `_last_capture_mono` debounce field.
- In the OWW listener, append each real mic `payload` to `wake_ring` (trimmed
  to the max window) **only when not muted and not speaking**; skip VAD
  sentinels.
- On the near-miss branch (`0.05 < score < eff_threshold`) and the activation
  branch (`ctrl_hit` / device-decided `oww_wake`), when `save_wake_captures`:
  snapshot the last `wake_capture_sec` of the ring and dispatch a threadpool
  `em_training_captures.save(...)` (never blocking the loop). `model_key` is
  the group; `kind` ∈ {`act`,`miss`}; `score` recorded.
- **Debounce:** at most one capture per device per ~3s (an utterance crosses
  0.05 on many frames).
- Config read into `Device` on config push, same pattern as `saveUtterances`.
  Controller-consumed; device ignores the keys.

### 2. Storage module (`em_training_captures.py`, filesystem-only)

Mirrors `em_recordings.py` — pure, unit-testable, **no DB / no migration**.
- `captures_dir()` = `training_captures/` beside the SQLite DB.
- Layout: `training_captures/<model_stem>/{untriaged,positive,negative}/`.
- `filename(device, ts_ms, kind, score)` / `parse_filename` — carries device,
  timestamp, kind, score; group is the parent dir. Model stem validated as a
  safe path component.
- `save(...)`: write WAV (via `em_recordings.encode_wav`) into `untriaged/`,
  write-then-rename, then prune untriaged to the cap (oldest first).
  positive/negative never auto-pruned.
- `label(model, name, target)` moves untriaged→positive|negative;
  `discard(...)`; `list_captures(model, bucket)`;
  `counts(model)`; `list_models()`; `export_zip(model)` → in-memory ZIP of
  positive/+negative/.
- `delete_device(device_id)` removes that device's files across all
  buckets/wakewords; wired into `db.delete_device` for privacy parity.
- Cap is env-overridable constant `EM_WAKE_CAPTURE_CAP` (default 200).

### 3. Config plumbing

Two new keys in the **wakeword** section of `em_config_sections.py` (fleet +
per-device), plus `DEFAULT_DEVICE_CONFIG` in `em_db.py`, plus the
`CONFIG_SECTIONS` mirror in `dashboard.jsx`:
- `saveWakeCaptures` (bool, default False — opt-in).
- `wakeCaptureSec` (number, default 2.0; UI-clamped ≤ ring max).

### 4. Labeling UI + admin API (`em_api.py`, `dashboard.jsx`)

All `@auth.require_admin` (raw speech; pinned by `test_deploy.py`):
- `GET /api/training_captures` → wake words with `{untriaged,positive,negative}`
  counts.
- `GET /api/training_captures/{model}/captures?bucket=untriaged` → list + parsed
  metadata.
- `GET /api/training_captures/{model}/audio/{name}` → WAV (served like turn
  audio; dashboard fetches via `API.blob`, Bearer-only).
- `POST /api/training_captures/{model}/{name}/label` `{label}` → move.
- `DELETE /api/training_captures/{model}/{name}` → discard.
- `GET /api/training_captures/{model}/export` → `dataset.zip`.

New admin-only dashboard panel **"Wake Training"**: pick a wake word, work the
untriaged queue with an `<audio>` player and two buttons — "Should have
activated" (→positive) / "Should have ignored" (→negative) — plus Discard,
showing kind/score/device/time per clip, and a "Download dataset (.zip)"
button.

### 5. oww_forge import (`forge_web.py`, `forge.py`, `static/index.html`)

- `POST /api/wakewords/{name}/import_dataset` (multipart ZIP) unpacks:
  `positive/*` → `positive_train`+`positive_test`; `negative/*` →
  `negative_train`+`negative_test`. Each file normalized via `_to_wav16k`,
  named `custom_*` so eval buckets positives as "Custom / Recorded"; add a
  matching custom-negative bucket to the eval report.
- **Split preserves oww policy:** reuse `TEST_FRACTION = 0.1` (90/10), applied
  independently to positives and negatives via the stable
  `index % round(1/TEST_FRACTION) == 0` rule `google_tts` already uses.
- UI: "Import labeled dataset…" button beside "+ Family recordings…". CLI
  parity via `forge.py import <name> --zip <path>`.

### 6. Testing, privacy, docs

- `tests/test_training_captures.py` (pure fs: save/prune-to-cap, label move,
  export contents, delete_device, filename parse + ownership) — modeled on
  `test_recordings.py`.
- `test_config_sections.py` already enforces partition/mirror totality.
- `test_deploy.py` shape test pins `require_admin` on audio/export/label routes.
- oww_forge test for the import split ratio + dir placement.
- Privacy: opt-in default OFF; support bundle is an allowlist (excluded);
  `db.delete_device` cascades removal. Documented in `CLAUDE.md` and
  `oww_forge/README.md`.

## Out of scope (YAGNI)

No DB table, no per-turn correlation, no auto-push to oww_forge, no device
firmware change, no post-roll.
