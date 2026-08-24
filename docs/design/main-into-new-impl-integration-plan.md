# Plan: integrate `origin/main` into `new_impl`

Status: approved. This is an implementation plan and decision record, not an
instruction to merge blindly. It was assessed against `origin/main` at
`757c952` and `new_impl` at `26c4c7a`, with merge base `c6f9add`.

## Goal

Bring useful work from `origin/main` into `new_impl` without undoing the
HACS turn-engine cutover, Sendspin/music-sync work, wake-capture training,
or the removal of the Early Access add-on channel.

At the time of assessment, the branches have 77 `origin/main`-only commits
and 80 `new_impl`-only commits. The local `main` branch is stale and already
contained by `new_impl`; all integration work must target `origin/main` after
a fresh fetch.

## Settled decisions

- **Merge shape:** create a merge commit from `origin/main`; do not replay 77
  commits with an unreviewed cherry-pick sequence.
- **Architecture:** retain HACS, `em_turn_engine`, `em_audio_frame`,
  `em_ha_sidechannels`, Sendspin, and the current device protocol. Do not
  restore the ESPHome impersonation backend.
- **Release channel:** retain deletion of `controller-ea/`,
  `sync_channels.py`, EA tag triggers, channel parity tests, and per-channel
  ports. Historical `-ea.N` release notes may remain as neutral prerelease
  history only.
- **Audio:** keep `new_impl`'s current audio chain. Do not add upstream
  `em_limiter.py`, `em_mbc.py`, dynamic bass-guard settings, output-chain UI,
  or their migrations in this integration.
- **Documentation:** keep one root `CLAUDE.md`; manually incorporate only
  still-valid upstream guidance. Do not take the old root/controller/device
  split.
- **Forge:** keep the current datasets, labelled capture import, ROCm feature
  store, freshness manifests, Chirp 3 support, rate configuration, and live
  scoring. Adapt all still-useful upstream Forge operational features around
  that base.

## Safety rules

1. Start from a clean worktree and fetch before acting:

   ```bash
   git fetch origin
   git switch new_impl
   git status --short
   git branch backup/new-impl-before-main-merge
   git switch -c integrate/main-into-new-impl
   git merge --no-commit --no-ff origin/main
   ```

2. Resolve conflicts by behavior, not by choosing `ours` or `theirs` for a
   whole file. Never use `git checkout --ours .` or `git checkout --theirs .`.
3. Keep all existing migrations byte-for-byte and append compatibility work;
   migrations are append-only.
4. Preserve upstream history in one merge commit after verification. Do not
   force-push or rewrite either existing branch.
5. Treat device/compiler/audio changes as hardware-sensitive. Host tests do
   not prove a device binary boots or sounds correct.

## Migration compatibility gate

This is the highest-risk conflict. Both lines independently use schema v19:

- `new_impl` v19 adds `turns.state`; v20-v21 add turn-engine latency fields.
- `origin/main` v19 adds `devices.esphome_mac` for the retired ESPHome
  transport.

Do not renumber, replace, or edit existing v1-v21 entries. Preserve
`new_impl`'s v1-v21 list exactly. Do not restore the obsolete ESPHome migration
as a new schema requirement. Append v22 only if a compatibility fix is needed:

1. Inspect `PRAGMA table_info(turns)` in a v22 Python fixup.
2. Add `turns.state` only when it is absent, so a database upgraded on the
   `origin/main` line can run the HACS turn engine.
3. Leave any pre-existing `devices.esphome_mac` column untouched but unused.
4. Test fresh, v18, `new_impl` v19/v21, and `origin/main` v19 databases;
   also verify the newer-schema refusal path.

## Incoming commit decisions

Each incoming `origin/main` commit is listed below. “Follow” is the concrete
implementation or review action after the merge conflict is exposed.

### Foundation, CI, dependencies, and documentation

| Commit | Disposition | Follow |
|---|---|---|
| `2d8c4fb` Fix ingress login crash | Merge directly | Port the `_q1`-based `get_user_by_ha_id` repair and its regression test. Verify `POST /api/auth/ingress` with a real DB. |
| `757be08` Build controller image on PRs | Merge with adaptation | Add the relevant-path image build job while retaining current coverage and Forge jobs. Trigger for controller image inputs, not old ESPHome assumptions. |
| `3ee262a` Bump `x/sys` | Ignore | It requires Go 1.25 and is superseded by `5bf686e` for the pinned Go 1.24 compiler. |
| `42849ee` Action dependency bumps | Merge | Take current action majors across workflows, then validate all action inputs against the new workflow shapes. |
| `7bdb62d` Align controller-image actions | Merge with `42849ee` | Apply after restoring the controller-image job. |
| `6bda5de` Runtime dependencies and Dependabot | Merge with adaptation | Take supported `aiohttp`, `websockets`, `cryptography`, and `zeroconf` updates; retain `aiosendspin`. Audit for protobuf imports and remove the obsolete dependency instead of carrying ESPHome-specific explanation. Add the onnxruntime minor-update guard. |
| `55205ff` Linux rooting requirement | Merge | Update rooting documentation; current instructions must not imply macOS works. |
| `a7c5e16` Project direction | Merge with adaptation | Add the portable/non-Amazon direction to root guidance without restoring old architecture prose. |
| `199da92` Writing guidance | Merge | Add bottom-line-first communication guidance to root `CLAUDE.md` or `CONTRIBUTING.md`. |
| `a9b4e2f` Split CLAUDE files | Ignore structure; merge guidance manually | Keep one root `CLAUDE.md`. Select valid operational guidance and reject ESPHome/EA statements. |
| `6ac40a9` License, notice, contributing | Merge with adaptation | Add `LICENSE` and `CONTRIBUTING.md`. Rebuild `NOTICE.md` from current dependencies; do not name deleted aioesphomeapi files. Update test commands for HACS and current controller scope. |
| `fd04e22` AFE decision docs | Merge with adaptation | Preserve portable-audio rationale in root documentation and retain current hardware details. |
| `3dfb782` Explain `f1r30s.zip` | Merge | Correct rooting/setup explanation directly. |
| `961351d` Firmware build CI | Merge | Restore the pinned compiler-image firmware build job before accepting Go-module automation. Do not substitute host compilation. |
| `5bf686e` `x/sys v0.41.0` | Merge after `961351d` | Update `device/go.mod` and `go.sum`; run host tests and pinned-container build. |
| `717f7f1` Controller import/boot check | Merge with adaptation | Build/import/boot the packaged controller image, listing current HACS/turn-engine modules. Do not import deleted `em_esphome`. Add owners for current high-risk modules. |
| `aae5fc0` Pin import image | Adapt concept | Prefer smoke-testing the image built by the current PR rather than an old published ESPHome image. If a base image is used, pin a post-HACS digest. |
| `205c8c4` Tool DB discovery | Merge | Apply to shell/OTA tools, optionally checking canonical `DB_PATH` before fallback locations. |
| `ce723da` Embedded binary version | Merge | Accept clean version parsing and dashboard rollback wording; test clean `git describe` version forms. |
| `c342b91` Loop-lag reader | Merge with adaptation | Read live controller state from the running module, not a second import. Audit other stateful module reads for the same error. |
| `757c952` Journal correction | Merge with adaptation | Preserve relevant factual journal corrections in chronological location; remove EA/ESPHome-only narrative. |

### Audio: intentionally not adopted

These commits are accounted for but should not introduce code in this merge.
The selected policy is to retain the current 85 Hz subsonic filter, EQ/warmth
profile, Sendspin processing, and device codec DRC.

| Commit | Disposition | Follow |
|---|---|---|
| `bbf7cc4` Look-ahead limiter | Ignore by decision | Do not add `em_limiter.py`; retain current clipping/DRC behavior. Consider separately only after hardware measurements show a current problem. |
| `022db75` Dynamic bass guard | Ignore by decision | Do not add `em_mbc.py` or replace the 85 Hz high-pass. A future proposal must compare chains on hardware rather than stack both. |
| `23dc52e` Live output-chain changes | Ignore by decision | Current EQ changes remain as implemented; do not add live limiter/guard machinery. |
| `bbe3aa1` Carry output-chain settings live | Ignore by decision | No new output-chain settings are being adopted. |
| `6f25c0c`, `72ae329` Output-chain docs | Adapt only if still factual | Do not document a chain that is not merged. Preserve only independent measurements that accurately describe current hardware. |
| `ba6f499` Bass guard default | Ignore | Depends on rejected dynamic bass guard. |
| `0c0ca41` Chain instrumentation/UI | Ignore | Depends on rejected limiter/bass guard controls. |
| `59ae4c2` Bypassed clipping counter | Ignore | Depends on rejected limiter. |
| `ed94900` Audio state map | Adapt selectively | Keep only state-machine documentation compatible with HACS and Sendspin; upstream claim that music bypasses the chain is false on `new_impl`. |

### Release channel and obsolete ESPHome work

These changes must not be restored. Resolve any merge conflicts by retaining
the deletion or current HACS implementation.

| Commit | Disposition | Follow |
|---|---|---|
| `c9848ba`, `1a27903`, `361a2de`, `c1ccbca`, `d90a752`, `e903dd8`, `725f621`, `1a8df6d`, `81f5d28`, `d8f164b`, `5f3cfde`, `1769459`, `7d4b394` | Ignore release markers | Do not recreate EA config, tags, channel docs, or generated changelogs. Keep neutral prerelease history only in the stable changelog if useful. |
| `af0d2fb` Channel-specific satellite ports | Ignore | Both EA and ESPHome per-device servers are gone. Leave legacy database seeds/columns alone for migration compatibility. |
| `1e72dd2` Explain EA build base | Adapt history only | If retained, call historical builds “prereleases”; do not describe a live EA channel. |
| `61ca04f` Controller 2.20.2 release | Adapt changelog only | Keep useful release notes in `controller/CHANGELOG.md`; do not write `controller-ea/`. |
| `e445040` Stored ESPHome identity | Ignore | Do not add `esphome_mac`, MAC helpers, migration, or tests. HACS does not need a fabricated ESPHome identity. |
| `399f5c3` ESPHome trace guard | Ignore | It only changes deleted `em_esphome.py`. |
| `e392afc`, `bf4b374` Intermediate barge outcomes | Do not port directly | Use `16293a2` as the final behavioral specification instead. |

### Controller correctness and capability work

| Commit | Disposition | Follow |
|---|---|---|
| `4c9a5ec` OWW reset warm-up | Merge with adaptation | Add a warm-up gate after model construction/reset in `wake_word_listener()` and `_barge_watcher()`. Continue feeding frames but suppress threshold actions and near-miss accounting until real audio replaces seeded embeddings. |
| `88a553f` Reachable HA admins | Merge with adaptation | Add `ha_admin_count()` for HA-backed admins only; use it in ingress login and refresh the dashboard’s cached role from `/api/auth/me`. Do not let unrelated local users block the first reachable HA admin. |
| `4db6eca` Self-barge prevention | Merge with adaptation | Port the two-consecutive-frame barge decision and safer default after checking stored fleet configuration. Existing values need an explicit migration/operator policy; defaults alone do not change stored config. |
| `613bfb2` Wake listener supervision | Merge with adaptation | Supervise and restart the live wake task with backoff, timestamp `oww_paused`, and recover a stuck pause only when `voice_lock` is free. |
| `a2ba343` Unreachable initializers | Merge | Move `playback_send_ms` and `playback_eq_ms` into `Device.__init__`; keep an AST/regression guard against statements after unconditional return. |
| `012a43f` Per-message isolation | Merge with adaptation | Extract current control dispatch helpers, catch non-cancellation exceptions per message/frame, log and continue. Registration/auth failures remain fatal. Do not copy old ESPHome dispatch paths. |
| `9ff543b` Denoiser dry floor | Merge with adaptation | Port dry-floor blending and zero reporting to `em_ns`, then integrate it in `em_turn_engine._send_mic()`. Captures must remain byte-identical to post-NS `MIC_PCM` payloads. |
| `1423e62` Wake asset reconciliation | Merge with adaptation | Plan all stock classifiers, verify selected classifier checksum on connect, set `oww_model_ready=False` only on known mismatch, repair in background, then resend effective config. |
| `99628d3` Immediate local listening animation | Merge with adaptation | Resolve and send `listeningAnim` with initial config; refresh on scene changes. Device wake crossing starts its local animation before reporting. |
| `16293a2` Cause-based turn ends | Merge with adaptation | Add `Turn.end_reason`; first reason wins. Button = `cancelled`, mute = `muted`, wake interruption = `barged`. Make persistence derive outcome from reason. This records a barge but does not solve the separate HACS pipeline-abort gap. |
| `3606365` Users dashboard | Merge with adaptation | Add the Users pane to current Settings tabs, use existing API endpoints, show last-admin refusal, and pair with role refresh from `88a553f`. |

### Media, certificates, and update behavior

| Commit | Disposition | Follow |
|---|---|---|
| `355105e` Extra CA trust | Merge with adaptation | Add `EM_EXTRA_CA_CERT`, add-on option, `/ssl` map, Docker/startup installation, and tests. Do not recreate EA add-on files. It applies to HTTPS media, not the current PCM TTS socket. |
| `fe59504` Verified HTTPS decoder | Merge with adaptation | Use TLS verification for HTTPS media only, drain ffmpeg stderr, and report abnormal decoder exits. Pair with private-CA support. |
| `9d133d4` Dead source timeout | Merge with adaptation | Bound steady-state decoder reads, terminate a source that produces no audio, EOS cleanly, and report idle. Integrate stderr diagnostics from `fe59504`. |
| `832bd22` Update interval zero | Merge first | Define zero/negative update interval as automatic-checks off. |
| `722359d` Disabled update traffic | Merge after `832bd22` | Ensure cached and uncached paths make no automatic GitHub call while disabled; retain explicit check-now behavior. |
| `2a26f91` Update-off presentation | Merge after prior two | Expose `update_checks_enabled` and show “Auto-checks off” in the dashboard/documentation. |
| `67f0b84` Upload size | Merge | Set app transport limit above the 50 MB application limit and re-raise `aiohttp` HTTP exceptions. |

### Forge operations and release delivery

| Commit | Disposition | Follow |
|---|---|---|
| `2bd5cce` Forge run control/UI/image | Merge with adaptation | Preserve current Forge feature-store/data workflow. Add process-group cancellation, theme support, deploy compose, and release workflow. **Do not take the browser-managed Google service-account upload/check/remove UI:** Chirp credentials are deployment-owned and supplied only through `GOOGLE_APPLICATION_CREDENTIALS`. Keep MDC credentials separate from Google credentials. Publish CPU amd64/arm64 and CUDA amd64; do not advertise WSL ROCm as portable. |
| `ad86f67` Transient Google errors | Merge with adaptation | Retire a voice only for permanent request/auth/permission errors. Quota/network/service exhaustion fails a clip, logs retry exhaustion, and leaves the voice eligible. |
| `6b2fff4` Piper languages/preview | Merge with adaptation | Add `piper_voices.py`, Docker inclusion, catalogue selection, preview endpoint/UI, and tests. Integrate with current manifests and distinguish generated filenames by source. |
| `30567b3` Quota retry | Adapt | Retain current request pacer and retry controls; complete the permanent-versus-transient classification from `ad86f67`. |
| `0a58a48` Forge UI focus/alignment | Mostly already covered | Keep current focus-preserving render behavior. Port only harmless cosmetic alignment after visual review. |
| `c6ccf41` Forge release lessons | Adapt docs | Preserve current corpus/ROCm/import documentation; add only valid image, cancellation, credential, preview, and release lessons. |

### Release/docs follow-ups

| Commit | Disposition | Follow |
|---|---|---|
| `f7f8e96` Day/release documentation | Adapt | Preserve useful behavior notes in chronological journal/changelog locations, stripping EA/ESPHome instructions. |
| `b7aa190` Cost record | Adapt | Preserve applicable engineering-cost facts in `JOURNAL.md`; insert chronologically. |

## Implementation order

1. Fetch, create the backup/integration branches, and start the no-commit
   merge.
2. Resolve structural conflicts: retain HACS, Sendspin, training capture,
   current tests, and EA/ESPHome deletions.
3. Resolve `em_db.MIGRATIONS` using the compatibility gate before accepting
   any runtime database change.
4. Apply foundation work: action/dependency updates, pinned firmware build,
   controller image build/smoke test, licenses, and rooting documentation.
5. Apply controller correctness in this order:
   `2d8c4fb`, `a2ba343`, `613bfb2`, `012a43f`, `4c9a5ec`, `4db6eca`,
   `88a553f`, `3606365`, `9ff543b`, `1423e62`, `99628d3`, `16293a2`.
6. Apply media and update work in this order:
   `355105e` + `fe59504` + `9d133d4`; then `832bd22` + `722359d` +
   `2a26f91`; then `67f0b84`, `205c8c4`, `ce723da`, `c342b91`.
7. Integrate Forge operational features as focused commits on the integration
   branch, preserving the current Forge architecture.
8. Resolve one-root-`CLAUDE.md`, README, setup, changelog, and journal text.
9. Review every deleted/added path for accidental resurrection of EA or
   ESPHome code. Finish the merge only when the conflict index is empty.

## Verification

Run all relevant tests after each focused group and once on the final merge:

```bash
cd controller && python -m pytest tests/
cd ../device && go test ./internal/... ./pkg/... && go vet ./internal/... ./pkg/...
```

Also run HACS integration tests, Forge tests, dashboard JavaScript tests,
controller image build/import/boot smoke tests, the pinned firmware-container
build, and the migration matrix above. Run `git diff --check` and search for
active `controller-ea`, `sync_channels`, and `em_esphome` references before
committing.

The final commit should be a merge commit on `integrate/main-into-new-impl`.
After review and real-device validation of device-affecting changes,
fast-forward `new_impl` to that merge commit and retain the backup branch until
the resulting controller has been exercised.
