# Improving a wake word from real captures

EchoMuse can record short clips of the audio around real wake events — both
**activations** (it fired) and **near-misses** (it almost fired) — so you can
label what *should* have happened and retrain the wake-word model on your own
household's voices, room and noise. This is the single most effective way to
fix a wake word that misses you or fires at the TV.

The loop is: **enable → label → export → import → retrain → install.**

> **Privacy.** These clips are recognisable speech recorded inside your home.
> Capture is **off by default**, every capture screen is **admin-only**, and
> the clips are **excluded from support bundles**. Deleting a device deletes
> its captures. Turn the feature off when you have collected enough.

## 1. Enable capture

In the dashboard, open a device → **Config → Wake word** (or **Settings →
Config** for the whole fleet) and turn on **Save wake captures**. Optionally
set **Capture length** — how many seconds *before* each detection to keep
(default 2.0s, enough to contain a 3–4 syllable phrase).

Clips now collect on the controller, grouped by wake word. Nothing is sent
anywhere; they sit in `training_captures/` beside the database, in the
persisted volume.

Leave it on until you have a useful batch — a few dozen of each label is enough
to move the model; a few hundred is better.

## 2. Label the clips

Open **Settings → Training** (admin only). The tab shows a badge with how many
clips are waiting.

Pick a wake word, then work through the queue one clip at a time:

- Press **Space** (or the audio controls) to replay.
- **A** / **Should have activated** → the wake word *was* said; this is a
  **positive** example. (Use this for a near-miss where you clearly said the
  phrase but it didn't fire — that is exactly what teaches better recall.)
- **I** / **Should have ignored** → this was *not* the wake word; a
  **negative** example. (Use this for an activation that fired on the TV or
  chatter — that teaches fewer false wakes.)
- **D** / **Discard** → unusable clip (silence, cut off), delete it.
- **U** / **Undo** → sent the last one to the wrong bucket? This moves it back
  to the queue.

The label, not what triggered the capture, decides positive vs negative — an
activation can be a negative (false wake) and a near-miss can be a positive
(missed wake). Judge each clip on what you hear.

## 3. Export the dataset

When you have labelled a batch, click **Download dataset (.zip)**. The ZIP
contains `positive/` and `negative/` folders (plus a `manifest.json` recording
what came from where — oww_forge ignores it).

## 4. Import into oww_forge and retrain

In the [oww_forge](../oww_forge/README.md) UI, on the wake word's card, click
**+ Import labeled dataset…** and pick the ZIP, then **Retrain**. (CLI
equivalent: `docker compose run --rm forge import <name> --zip /data/<file>.zip`.)

Forge converts every clip to 16kHz mono, adds them to the training set, and
holds **10%** of each polarity out for the test set — the same split policy as
its synthetic data — so the model's reported accuracy stays honest. Your real
clips displace synthetic ones rather than simply inflating the set.

## 5. Install the retrained model

When the build finishes you have a new `.onnx`. Install it the usual way —
dashboard **Config → Wake word → + Custom model** (see the oww_forge README's
*Installing a model into EchoMuse* section) — and the device hot-reloads onto
it.

Then judge it: activations should score near 1.0 on your voice, and the clips
that used to false-fire should now score low. Capture another round if needed —
each pass pulls the model further toward your household.

## Scope notes

- Capture is **controller-side**. It reuses the continuous wake-audio stream the
  device already sends, so there is no device/firmware change and no extra load
  on the Echo.
- The **untriaged** queue is capped per wake word (200 by default, oldest
  dropped; override with `EM_WAKE_CAPTURE_CAP`). Labelled clips are never
  auto-deleted — they are your training set — so export and clear them yourself
  when done.
- In on-device wake mode (`owwOnDevice: on`), captures reflect what the
  **controller** scores in parallel. A near-miss that only the device heard
  (and the controller did not independently cross the near-miss floor on) is
  not captured.
