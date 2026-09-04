# End-to-End Voice Query Testing

A scripted regression suite for the voice-turn path — timers, general
queries, follow-ups, media — driven over the same audio-upload harness the
dashboard's device **Test** tab uses (`POST /api/devices/{id}/test_audio` +
`POST /api/devices/{id}/test_turn`). From the moment the WAV lands on the
device, this is a genuine device round trip: the Echo streams it onto its
real microphone plane exactly like a real utterance, so the controller
cannot tell it apart from one. It exercises wake admission, the turn engine,
HACS, the configured Home Assistant Assist pipeline, TTS playback, and
(where relevant) timer/media side effects — all without a human speaking
into a room, for everything short of what's listed in
["What this harness cannot reach"](#what-this-harness-cannot-reach) below.

Companion to `docs/timer-validation.md`, which this doesn't replace —
that document's hardware acceptance checklist still applies for the timer
paths this harness can't drive (alarm ringing, FIFO queueing, muted expiry).

## How it fits together

- **`controller/tests/fixtures/e2e_audio/manifest.json`** — the single
  source of truth. One entry per case: the phrase (or synthetic content) to
  speak, preconditions, and what a pass looks like. Both pieces below read
  it; edit only this file to add or change a case.
- **`controller/tests/fixtures/e2e_audio/generate_fixtures.py`** — a
  one-time (or occasional) dev-time step that turns the manifest's phrases
  into WAV files using Piper TTS (reusing `oww_forge/piper_voices.py`'s
  `preview()` helper — the same single-clip synthesis it already uses for
  pronunciation previews), plus a couple of synthetic silence/noise clips
  via the stdlib alone. Fixtures are **gitignored**, not committed — same
  policy as `oww_forge/data/`, since they're bytes regenerated
  deterministically from the manifest, not authored content.
- **`controller/tools/e2e_query_test.py`** — the runner. Uploads a fixture,
  triggers a turn, polls `GET /api/devices/{id}/turns` for the resulting
  row, and asserts on it. Follows the same conventions as the other
  `controller/tools/` scripts (see that directory's README): standalone,
  mints its own short-lived admin session token directly in the `sessions`
  table, and is meant to run inside the controller container or against its
  bind-mounted data directory.

## One-time setup: generating the fixture corpus

Needs oww_forge's TTS dependencies (`onnxruntime`, `piper-phonemize`,
`soundfile`, `librosa` — already pinned in `oww_forge/requirements.txt` and
baked into its Docker image) for the spoken cases; the silence/noise cases
need only the stdlib. Piper voice files download once to an asset directory
and are cached across runs, the same way `piper_voices.py` already caches
them for wake-word training positives.

From inside the oww_forge image (has `piper_voices.py` on `PYTHONPATH`
already):

```bash
docker cp controller/tests/fixtures/e2e_audio oww-forge:/tmp/e2e_audio
docker exec oww-forge python /tmp/e2e_audio/generate_fixtures.py \
    --assets /data/e2e_piper_voices
docker cp oww-forge:/tmp/e2e_audio/. controller/tests/fixtures/e2e_audio/
```

Or from a full repo checkout with oww_forge's requirements installed in a
local venv (lighter weight — `piper_voices.preview()` needs onnxruntime/
piper-phonemize/soundfile/librosa, not the full torch-based training stack):

```bash
python controller/tests/fixtures/e2e_audio/generate_fixtures.py
```

Run it once with no `--case` filter — a couple of gating cases (`G-busy`)
deliberately reuse another case's fixture file rather than generating their
own, so a full pass is what makes every case runnable. Re-run (`--force` to
regenerate existing files) whenever a phrase changes in the manifest.

## Running the suite

```bash
docker cp controller/tools/e2e_query_test.py echomuse-controller:/tmp/
docker cp controller/tests/fixtures/e2e_audio echomuse-controller:/tmp/e2e_audio
docker exec echomuse-controller python /tmp/e2e_query_test.py \
    -s <SERIAL> --fixtures-dir /tmp/e2e_audio --list
docker exec echomuse-controller python /tmp/e2e_query_test.py \
    -s <SERIAL> --fixtures-dir /tmp/e2e_audio --all --results-out /tmp/results.json
```

`<SERIAL>` is the device's `ro.serialno` — the same id `adb -s <SERIAL>`
uses, and exactly the controller's `device_id`. If the controller's data
directory is bind-mounted to the host (the common dev-compose shape), this
runs directly from a checkout instead, no `docker cp` needed:

```bash
python controller/tools/e2e_query_test.py -s <SERIAL> \
    --db controller/data/echomuse.db --category timers
```

Useful flags: `--case <id>` (repeatable) to run one or a few cases,
`--category normal,timers,followup,media,gating` to filter, `--list` to
browse the catalog without running anything, `--results-out <path>` to save
a JSON record. The device must be approved, connected, unmuted-capable
(cases needing mute manage it themselves), and advertise the `test_audio`
capability — the runner checks this up front and refuses early with a clear
reason rather than letting every case fail individually.

**Mark results with the controller and firmware version used**, the same
convention `timer-validation.md` asks for — `--results-out` records
`firmware_ver` for you; note the controller version alongside it by hand.

## What a pass means

Each turn case asserts on the persisted turn row: `outcome`, `stt_text`
(substring checks — proof the device's audio reached STT and was heard
correctly, not a full-sentence match, since exact wording varies), and
`tts_text` (what the response actually said, persisted by the turn engine's
`tts-text` action alongside `stt_text` — CLAUDE.md's turn-engine notes cover
this in more detail). This means most cases are verifiable **without a
human listening to the speaker** — the response text is data, not something
you have to be in the room for. Cases that also have a real-world side
effect (a timer created, music started) are still worth a manual glance —
`tts_text` proves HA understood and answered, not that the intent executed
correctly — and each such case's `manual_followup` note in the manifest
says what to check.

## Case catalog

Generated from `manifest.json` — regenerate this list rather than editing it
by hand if the manifest changes (`e2e_query_test.py -s <SERIAL> --list`
prints the same information).

### normal — general queries, not timers or media

| id | what it tests |
|---|---|
| `N1-general-knowledge` | A factual query with a single-answer reply. |
| `N2-time-query` | Built-in time intent; works on a local (non-LLM) pipeline too. |
| `N3-explicit-noop` | `HassNevermind`-shaped phrase. Verified on hardware: the configured LLM pipeline answers conversationally rather than through the silent built-in intent — both shapes are accepted. |
| `N4-silence` | Nothing but silence. Verified on hardware: does **not** take a fast no-speech path — rides the full `em_announce.ANNOUNCE_TIMEOUT_S` (120s) backstop and ends `audio_timeout`, bounded but not fast (see Known limitations). |
| `N5-noise` | Low-amplitude noise, not speech — must complete gracefully. Verified on hardware: same `audio_timeout`/120s path as `N4-silence`. |
| `N6-near-max-length` | Speech padded to ~110s, under `TEST_AUDIO_MAX_SECONDS` (120s) — the upload/convert/transfer path at a near-boundary length. |

### gating — device/link state and upload boundaries (negative paths)

| id | what it tests |
|---|---|
| `G-empty-upload` | 0-byte upload → `400 empty_upload`. No device turn involved. |
| `G-oversized-upload` | Upload over `TEST_AUDIO_MAX_INPUT` (50MB) → `413 too_large`. |
| `G-muted` | Muted device refuses `test_turn` → `409 device_muted`. The runner mutes/restores the device itself. |
| `G-busy` | A second `test_turn` while one is running → `409 test_turn_busy`. |

### timers — LLM-backed pipeline (see `docs/design/timers-design.md`)

| id | what it tests |
|---|---|
| `T1-create-named` | Named timer create. Occasionally flaky — see Known limitations. |
| `T2-create-unnamed` | Unnamed create — the configured LLM should derive a duration-based name. |
| `T3-cancel-named` | Two-turn: create, then cancel by name. Occasionally flaky — see Known limitations. |
| `T4-pause-resume` | Two-turn: create, then pause by name. Occasionally flaky — see Known limitations. |
| `T5-status-ordinal` | Two-turn: create, then `HassTimerStatus` by name. |
| `T6-cancel-all` | Three-turn: two creates, then HA-native `cancel all`. |
| `T7-timer-during-music` | Timer creation while music plays underneath (manual precondition: start playback first). |

### followup — continued conversation

| id | what it tests |
|---|---|
| `F1-continuation-shaped` | An underspecified request likely to make HA set `continue_conversation`. Checks for a raised `continuation` turn — see limitation below. |
| `F2-single-shot-no-continuation` | Control case: a fully-resolved question must **not** raise a continuation turn. |

### media

| id | what it tests |
|---|---|
| `M1-play-music` | "Play some jazz" — outcome and reply text only; whether playback actually starts depends on a configured media source. |
| `M2-query-during-music` | A query while music plays (manual precondition) — duck/pause invariants from `docs/audio-states.md`. |
| `M3-stop-music` | Spoken stop while music plays (manual precondition). |

## What this harness cannot reach

Structural limits of "upload one WAV, trigger one turn" — these need a
human and/or a second device, and stay on the hardware acceptance
checklist, not in the manifest:

- **True no-rewake follow-up.** `continue_conversation` re-arms the
  device's **live** mic, not a second uploaded file — the harness can
  raise the first half (see `F1-continuation-shaped`) but the follow-up
  utterance itself has to be spoken into the room.
- **Barge-in.** Needs a real wake word firing while TTS/music is already
  playing; nothing here injects audio mid-turn.
- **Multi-device wake arbitration.** Needs two physical devices in earshot
  of the same utterance.
- **HA-initiated announcements.** These start from Home Assistant
  (`assist_satellite.announce`), not from the device — there's no
  device-side trigger for this harness to drive.
- **Timer alarm ringing, FIFO queueing, and dismissal.** `_run_timer_speech_turn`
  (the live-mic "stop" dismissal) and the alarm queue itself are exercised
  by `docs/timer-validation.md`'s hardware acceptance checklist, not here —
  this harness can create/cancel/pause timers (see the `timers` category)
  but can't make one finish and ring on cue.
- **Muted timer expiry**, disconnect/reconnect mid-alarm, and action-button
  interplay (single/double/triple-tap, hold, mute interaction) — also
  `timer-validation.md` / manual hardware territory.
- **Offline-device and unsupported-firmware rejection paths.** Real, but
  need a device actually disconnected or actually on old firmware — not
  something an API call can force onto a live one.

## Known limitations

- **One WAV per triggered turn.** The harness can queue a fixed sequence of
  independent turns (`setup_phrases`) but can't inject a second file mid-turn.
- **One test turn at a time per device** (`test_turn_busy`) — cases run
  sequentially, never in parallel, and `G-busy` exploits this deliberately.
- **The TTS response is text, not audio.** `tts_text` is what HA said, not
  a recording of it — there's no captured audio to compare against, by
  design (see CLAUDE.md's turn-engine notes). If you need to hear it, opt a
  device into `saveUtterances` for the mic side, or just listen live.
- **STT/LLM output is not perfectly deterministic.** Substring assertions
  are chosen to be robust to normal phrasing variance; a occasional flake
  on a borderline case is a prompt to loosen that assertion, not
  necessarily a regression.
- **A clause boundary right before critical content can get truncated by
  live STT endpointing.** Root-caused on hardware, not just observed:
  Piper renders a real prosodic pause before a subordinate clause (e.g.
  "...two minutes**,** called pasta" — confirmed by Gemini's own transcript
  inserting that comma on a passing run), and depending on real-time
  network/delivery jitter around that pause, the live STT's automatic VAD
  occasionally treats it as end-of-speech and ends the turn early,
  silently dropping everything after the pause (`T1-create-named`,
  `T3-cancel-named`, `T4-pause-resume` all reproduced this at least once;
  all three also passed cleanly on other runs, including immediate
  single-case reruns of the exact same fixture). This is in the STT
  integration's live endpointing, not in the turn engine or in this
  harness's sequencing — a settle delay between cases does not fix it, and
  a failure here is worth a rerun before treating it as a regression. The
  actionable mitigation, if it reproduces often enough to be worth it, is
  rephrasing the fixture to avoid a clause boundary immediately before the
  content the assertion checks (e.g. state the timer's name before its
  duration).

## Verification log

Run against a real device (`ro.serialno` identity, firmware advertising
`test_audio`) and a real Home Assistant instance with an LLM-backed Assist
pipeline, 2026-09-03. Recorded here because it's the evidence that the
harness — not just its code — actually works end to end; update this log
rather than deleting it on the next real run.

- All `normal`, `gating`, `followup`, and the automatable `media` case
  passed. `T7`/`M2`/`M3` skipped as designed (non-interactive, manual
  precondition).
- All six automatable `timers` cases passed in an isolated
  `--category timers` run. Timer lifecycle was cross-checked directly
  against the controller's `timer-events` log (not just `tts_text`): every
  timer created across the whole run — including the paused one — was
  correctly swept by `T6-cancel-all`'s `cancel all timers`, and the pause
  in `T4-pause-resume` logged as `updated`, never `cancelled`.
- Two real bugs were found and fixed in the harness itself by this run,
  not by inspection: a `turn_id`-vs-`since` race where polling could return
  a *previous* case's already-terminal row and report success while the
  server was still mid-turn (surfaced as cascading `test_turn_busy` on
  every case behind it), and mute state only being restored at the end of
  a whole run instead of per-case. Both are described in code comments at
  their fix sites in `e2e_query_test.py`.
- One real, still-open finding surfaced in the *system* rather than the
  harness: `N4-silence`/`N5-noise` take ~120s (the `ANNOUNCE_TIMEOUT_S`
  backstop) rather than a fast no-speech path, and a subset of `timers`
  cases occasionally truncate on a clause boundary — both documented in
  the manifest and in Known limitations above, with the concrete evidence
  (measured `total_ms`, observed transcripts) rather than a guess.
