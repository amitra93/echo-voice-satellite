# oww_forge — custom wake-word trainer

Trains custom [openWakeWord](https://github.com/dscripka/openWakeWord) models
("hey biscuit", "computer", …) for EchoMuse, entirely from synthetic speech —
no recording sessions needed. Deliberately **separate from the controller**:
training is a heavy, occasional batch job with ~25GB of assets and a fat
PyTorch image, none of which belongs in the always-on controller container.

## How it works

The pipeline is openWakeWord's official automatic-training flow, containerised
and orchestrated by `forge.py`:

1. **Generate** — [piper-sample-generator](https://github.com/rhasspy/piper-sample-generator)
   (LibriTTS-R VITS, ~900 speakers) synthesizes tens of thousands of positive
   clips of the wake phrase, plus *adversarial negatives*: phrases chosen for
   phoneme overlap ("hey biscuit" → "hey bisque", "hay brisket") that teach
   the model precise boundaries. Optionally layered with Google Cloud TTS
   samples for extra voice diversity (see below).
2. **Augment** — clips are convolved with room impulse responses (MIT RIR
   dataset) and mixed with background noise/music (AudioSet + Free Music
   Archive), then converted to openWakeWord input features (melspectrogram →
   frozen Google speech embedding).
3. **Train** — a small classifier head (the same `dnn/32` architecture as the
   stock models) trains against the positives plus ~2,000 hours of
   precomputed negative features (ACAV100M), with false-positive validation
   against an 11-hour held-out set. Output: a single `.onnx` file, typically
   under 1MB — exactly what the controller's `OWWModel` loads.

Versions are pinned in the Dockerfile: openWakeWord @ `368c0371` (with a
one-line patch for its `--convert_to_tflite` argparse bug — string default
`"False"` is truthy, which would end every run importing TensorFlow),
piper-sample-generator @ `v2.0.0` (the last release with the flat layout
openWakeWord's `train.py` imports from — don't bump casually; its
`torch.load` is patched for torch ≥ 2.6's `weights_only` default flip).

## Quickstart — web UI

```bash
cd oww_forge
docker compose up -d --build forge-ui
# → http://<host>:8769
```

The UI covers the whole flow: asset download with live progress, wake-word
creation, build with a streaming log console, Google-TTS mix-in, wav-upload
testing, and `.onnx` download. One job runs at a time (training saturates
the machine anyway); state is derived from disk on every poll, so it
survives container restarts. No auth — LAN tool.

## Quickstart — CLI

```bash
cd oww_forge
docker compose build forge-ui   # same image serves both

# 1. one-time asset download (~25GB — see table below; ./data can be a
#    volume on any disk with room)
docker compose run --rm forge assets

# 2. create a wake word
docker compose run --rm forge new "hey biscuit"

# 3. (optional) mix in Google TTS positives — needs credentials, see below
docker compose run --rm forge google-tts hey_biscuit

# 4. train (GPU: ~1-2h; CPU: overnight)
docker compose run --rm forge build hey_biscuit

# 5. sanity-check against a recording
docker compose run --rm forge test hey_biscuit --wav /data/my_recording.wav
```

Result: `./data/models/hey_biscuit.onnx`.

Every stage is resumable: `assets` skips completed parts, clip generation
tops up to the target count, and `build --from-step augment|train` restarts
mid-pipeline.

### Optional speech corpora

The Forge UI lists and downloads both optional corpora individually:

- **Common Voice 26 English** downloads the complete 88.14GB CC0 archive from
  Mozilla Data Collective, then a separate feature-build action embeds every
  validated clip for use as training negatives. It needs roughly 150GB total
  alongside the standard Forge assets. Mozilla requires accepting its corpus
  terms and an API key: export `MDC_API_KEY` before starting Docker Compose.
- **AMI distant microphone** streams about seven hours of CC-BY-4.0 meeting
  audio into a compact false-positive validation array. No source archive is
  retained.

Enable either corpus per wake word after its feature asset is ready. Common
Voice features resume from `.npy.part` plus a checkpoint. A stale or corrupt
checkpoint is preserved and requires an explicit reset rather than being
silently overwritten.

### Optional-data mix

The original ACAV100M draw remains a 1,024-example negative batch. Optional
speech sources replace part of it rather than increasing the batch: Common
Voice receives 128 examples, FLEURS 96, and VoxPopuli 96; ACAV receives the
remainder. Adversarial negatives and positives remain 50 each. This keeps
multilingual/accent coverage meaningful without allowing any new corpus to
overpower the proven broad ACAV baseline. MUSAN and OpenSLR 28 are augmentation
sources, so they affect generated clips rather than classifier batch sampling.

## GPU / CPU

The default image builds **CUDA 12.8 torch 2.7.1**, which supports Blackwell
cards (RTX 50xx, sm_120) as well as older generations — note that
notebook-era torch 2.1/cu121 cannot drive an RTX 5060 Ti at all. Fallback is
automatic: if no CUDA device is visible at runtime, torch runs on CPU and
`forge.py build` logs which device it's using (the UI shows it in the header
badge).

- Host **with** the nvidia container runtime: use `forge` / `forge-ui` as-is.
- Host **without** it: `docker compose run --rm forge-cpu …` (same image, no
  GPU reservation), or build with `GPU: "0"` for a ~3GB CPU-only image
  instead of ~10GB.

### ROCm Feature Extraction

The ROCm compose file can embed optional speech corpora through runtime
conversion of openWakeWord's frozen ONNX feature models. Assets remain on the
proven `onnx` backend by default until the local ROCm gate passes.
`--feature-backend auto` selects ROCm PyTorch when available and ONNX CPU
otherwise; `--feature-backend torch` requires the GPU path.

The tuned ONNX corpus path uses six extraction workers, feature batches of 256,
and two bounded audio-decoder workers. These defaults were selected on the
six-core Ryzen host; the bounded queue prevents compressed audio from growing
without limit in memory.

Before enabling `auto` for a release, run the required local parity test and
measure both extractors on the same predecoded input:

```bash
docker compose -f docker-compose-rocm-wsl.yml run --rm --entrypoint python forge \
  -m unittest discover -s /opt/forge/tests -p 'test_torch_features.py'
docker compose -f docker-compose-rocm-wsl.yml run --rm forge \
  bench-features --backend onnx --clips 10000 --batch-size 64
docker compose -f docker-compose-rocm-wsl.yml run --rm forge \
  bench-features --backend torch --clips 10000 --batch-size 64
```

The ROCm result must be at least 2x the ONNX CPU end-to-end benchmark before
`auto` becomes the default for a release.

### Asset sizes

| Asset | Size | Purpose |
|---|---|---|
| ACAV100M negative features | ~17GB | 2,000h of precomputed non-wake-word features |
| validation features | ~0.5GB | false-positive validation (11h speech/noise/music) |
| AudioSet (2,000 clips, streamed) | ~2GB | background noise for augmentation |
| FMA small (1,000 clips) | ~1GB | background music for augmentation |
| MIT RIRs | ~50MB | room reverb simulation |
| piper LibriTTS-R checkpoint | ~430MB | positive sample synthesis |

### Tuning knobs

`forge.py new` writes `data/wakewords/<name>/config.yml` — edit before
`build`. The interesting fields:

- `n_samples` — 30,000 default; 50,000–100,000 measurably helps difficult phrases.
- `custom_negative_phrases` — add real-world confusions you observe
  ("hey brisket") and retrain; the cheapest fix for false activations.
- `target_false_positives_per_hour` / `max_negative_weight` — the
  false-accept vs. false-reject trade; defaults match upstream guidance.
- Choose a **3-4 syllable phrase**; short words make weak wake words no
  matter how much data you throw at them.

### Pronunciation & accents

The synthetic positives come from an **American** TTS corpus (LibriTTS-R),
and phrases are phonemized with US readings — "hey clara" trains on the
US vowel, not a British "clar-ra". Three levers, in increasing strength:

1. **Phonetic spelling variants** — `target_phrase` accepts multiple
   entries that train *one* model firing on any of them. Comma-separate them
   at creation time (`forge.py new "hey clara, hey clarra"` or in the UI's
   phrase field) and cover how your household actually says it. Trade-off
   observed in practice: covering two pronunciation clusters with the same
   small classifier makes the auto-trainer more conservative (the two-spelling
   `hey_clarra` trained to *zero* false positives/hour but lower recall than
   single-spelling `hey_clara`, 0.43 vs 0.52 on the augmented test set) —
   if a variant model feels deaf, import labeled captures and retrain, or lower
   the device's `owwThreshold` a notch.
2. **Google TTS mix-in** — Forge uses Chirp 3 voices. Select exact locales
   and optionally provide a comma-separated list of exact voice names. The
   sample count applies to every usable locale/voice pair, so `500` over two
   voices available in `en-IN` and `en-PH` produces 2,000 positive clips.
3. **Controller-labeled captures** (best) — use EchoMuse's controller to
   capture activations and near-misses, then label them as "should have
   activated" or "should have ignored". Import the exported ZIP below so the
   positive and negative labels are preserved; do not add arbitrary speech to
   the positive set.

### Import labelled captures from EchoMuse

The EchoMuse controller can record the audio around real wake **activations**
and **near-misses** and let an admin label each clip in the dashboard
(Settings → Training): "should have activated" (positive) or "should have
ignored" (negative). Download the finished dataset there as a `.zip` and bring
it here:

- **UI:** on the wake word's card, **+ Import labeled dataset…** and pick the
  `.zip`, then **Retrain**.
- **CLI:** `docker compose run --rm forge import hey_biscuit --zip /data/hey_biscuit-dataset.zip`

The ZIP is `positive/…` + `negative/…`; Forge converts every clip to 16kHz
mono, names them `custom_*`, and splits **10% of each polarity into the test
set** (the same `TEST_FRACTION` as Google-TTS positives) so the held-out
evaluation stays honest. Positives labelled from near-misses teach the model
the voices/pronunciations it is currently missing; negatives labelled from
false activations are the cheapest fix for false wakes — the real-world
equivalent of `custom_negative_phrases`. Imported clips displace synthetic
ones at generate time while preserving the positive/negative labels from the
controller.

### Testing a built model

Three ways: the UI's **🎤 Record test** (browser mic → score; needs
HTTPS or localhost for mic permission), **Test file…** (upload any audio
file), or `forge.py test <name> --wav <files-or-dir>`. Scores near 1.0 on
your voice and near 0.0 on ordinary speech are what you want; the
controller's default threshold is ~0.5.

### Google TTS positives (optional)

`forge.py google-tts <name>` synthesizes Chirp 3 samples. The requested sample
count applies to every usable locale/voice pair; pass `--voices` with a
comma-separated exact voice list to constrain it, or leave it empty to use all
matching Chirp 3 voices. Each request uses only the selected voice name,
locale, and reported gender; Chirp 3 speaking-rate and pitch variation are not
sent. The UI's **Queries per second** setting controls the request pacer and
defaults to 2; the CLI equivalent is `--qps`. Clips land in the same positive train/test dirs — the subsequent Piper
generation counts them toward `n_samples`, so Google clips displace rather than
expand the configured positive set. After adding Chirp clips or importing a
labelled dataset, select **Retrain**: Forge detects changed clips and
automatically rebuilds feature arrays before training.

Setup: create a GCP service account with the Text-to-Speech API enabled and
provide its JSON key to the deployment through
`GOOGLE_APPLICATION_CREDENTIALS`. Forge never accepts, inspects, or removes
this credential through its browser UI. **Usually free**: the API's always-free
tier covers ~1M premium-voice characters/month and a 2,000-clip wake-word run
is ~25k characters (~2% of it). Past the free tier it's ~$16/1M chars; the
command prints an estimate and asks before running.

## Installing a model into EchoMuse

Use the dashboard: **Config tab → Wake word → “+ Custom model”** and pick
the `.onnx` from `data/models/`. The upload lands in the controller's
persisted data volume (`oww_models/` beside the SQLite DB, so it survives
image upgrades), the model appears as a tile alongside the stock ones and
is auto-selected for the device you uploaded from. The OWW listener
hot-reloads on config change (same path as switching stock models), and
the ESPHome layer pushes the new wake-word name to Home Assistant
automatically. A custom tile's `×` deletes the file (refused while any
device or the global default still selects it).

Equivalent API (`em_api.py`):

```bash
curl -X POST http://<controller>:8768/api/oww_models/upload \
     -H "Authorization: Bearer <token>" \
     -F model=@data/models/hey_biscuit.onnx
# → {"model": {"name": "hey_biscuit", "path": "/app/data/oww_models/hey_biscuit.onnx", …}}
# then set owwModel to that path via /api/devices/<id>/config or the global config
```

Dropping a file into `controller/data/oww_models/` by hand works too —
`GET /api/oww_models` scans the directory per request, so it shows up on
the next dashboard load.

`owwModel` stores the **file path** for custom models (stock models stay
plain names). Note openwakeword keys its prediction dict by the filename
*stem*, not the path — the controller maps path → stem everywhere it reads
scores (`em_oww_models.prediction_key`), so keep filenames unique.

## Layout

```
oww_forge/
  Dockerfile           pinned training environment (openWakeWord + piper + deps)
  docker-compose.yml   forge-ui (web) + forge/forge-cpu (CLI) services
  forge.py             CLI: assets | new | google-tts | import | build | test | ui
  forge_web.py         aiohttp web UI (port 8769) — thin layer over forge.py
  static/index.html    the web frontend (single file, no build step)
  google_tts.py        Google Cloud TTS positive-sample generator
  config.template.yml  per-wake-word training config template
  data/                (gitignored) assets, per-word workdirs, finished models
```
