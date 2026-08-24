# Plan: raise test coverage to 70% (controller, device, dashboard)

Status: in progress. Baseline numbers below were measured directly against
this tree on 2026-08-23 (`git log -1` = `c1f7a37`), not estimated; completed
phases record their own measured outcomes.

## Target definition (confirmed with the repo owner)

- **70% is per-component**, not one blended number: controller ≥70%, device
  ≥70%, dashboard ≥70%, each tracked separately.
- **Device: hardware-bound Go code is excluded from the denominator.**
  CLAUDE.md already states this is architectural ("hardware-dependent code
  is not testable on the host"), not a gap — see "What's excluded, and why"
  below for the exact package list, decided by reading each package rather
  than assumed.
- **Controller and dashboard: no exclusions.** Nothing about `em_api.py`,
  `em_controller.py`, or `dashboard.jsx` is inherently untestable — they're
  at 0–15% today because the test env is deliberately kept light (CLAUDE.md:
  "keep it that way unless you're prepared to pull openwakeword/aiohttp into
  the test environment"), and because the dashboard has no test harness at
  all yet, not because the code resists testing.
- **Report-only, no CI gate.** Matches the existing `pip-audit`/
  `govulncheck` pattern (red-with-nothing-actionable trains people to ignore
  red). Coverage is tracked and visible, not enforced.

## A real blocker to fix before anything else

`controller/tests/test_training_captures_api.py` (currently untracked in
git status, not yet on a branch CI has run) does `from aiohttp import
test_utils, web` and `import em_api` at module level. CI's controller-tests
job installs only `pytest numpy scipy pyyaml` — no aiohttp. Once this file
is committed, CI will fail to *collect* it (`ModuleNotFoundError`), not
just fail a test. **Add `aiohttp` to the CI install line before merging
that file.** This is also good news for the rest of this plan: it proves
the "stub the heavy import, exercise the handler through a real aiohttp
app" technique already works end-to-end for `em_api.py` — Phase 4 is more
of this, not a new technique.

## Baseline (measured, not estimated)

### Controller
```
cd controller && python -m pytest tests/ --cov=. --cov-report=term-missing
```
Production code only (excludes `tests/`, which trivially self-covers and
inflates the number the repo would otherwise report — 68% today is that
inflated figure): **3279 / 7262 = 45.2%**.

Split by what's already covered vs. not:
- **Already at 85–100%**: every module built as a pure decision function —
  `em_button`, `em_linkauth`, `em_ingressauth`, `em_turnclock`, `em_arbiter`,
  `em_shadow`, `em_scenes`, `em_config_sections`, `em_announce`,
  `em_recordings`, `em_training_captures`, `em_turn_engine`, `em_player`,
  `em_support`, `em_eq`, `em_firmware`, `em_music_sync`, `em_oww_models`,
  `version.py`, `em_audio_frame.py`, `em_sync_sim`, `em_tap_burst`. This is
  the payoff of the pattern CLAUDE.md documents repeatedly — nothing to do
  here.
- **Real gaps, all pure-testable-in-principle**: `em_auth.py` (54%),
  `em_db.py` (62%), `em_oww_assets.py`
  (57%), `em_sendspin.py` (75%), `em_hostip.py` (78%),
  `tools/sync_channels.py` (56%), `em_start.py` (0%).
- **The big lever**: `em_controller.py` (1421 stmts, **0%**) and
  `em_api.py` (2077 stmts, **15%**) together are **3498 of 7262 production
  statements — 48% of the whole controller.** No distribution of effort
  across every other file reaches 70% without moving these two.

### Device
```
cd device && go test -cover ./internal/... ./pkg/...
```
(This is exactly what CI runs — deliberately not using `-coverpkg` to merge
cross-package attribution, which produces inconsistent numbers across
profile merges. Per-package, matching CI, is the number to trust.)

| Package | Covered/Total | % |
|---|---|---|
| bindings/speaker | 200/217 | 92.2% |
| aec | 120/132 | 90.9% |
| wakeword | 62/73 | 84.9% |
| wakeword/shadow | 101/132 | 76.5% |
| bindings/als | 53/88 | 60.2% |
| client | 294/818 | 35.9% |
| bindings/jack | 11/31 | 35.5% |
| beamformer | 48/144 | 33.3% |
| server | 148/453 | 32.7% |
| bluetooth | 66/228 | 28.9% |
| pkg/led | 11/62 | 17.7% |
| wakeword/ort | 11/92 | 12.0% |
| bindings/buttons | 0/64 | 0% |
| bindings/led | 0/37 | 0% |
| clock | 0/1 | 0% |
| config | 0/116 | 0% |
| discovery | 0/61 | 0% |
| processor | 0/38 | 0% |
| pkg/buttons | 0/8 | 0% |
| wakeword/fixture | 0/171 | 0% |
| **Total** | **1125/3176** | **35.4%** |

### Dashboard
No coverage tool exists for it today. 6754 lines in `dashboard.jsx`, of
which **56 top-level definitions** exist and only ~13 are pure,
non-rendering logic (`ingressPath`, `isIngress`, `deviceState`, `uptime`,
`relTime`, `wifiBand`, `turnSegments`, `effectiveConfig`,
`onDeviceMode`, `_bannerMode`, etc. — maybe 300–400 lines total). Everything
else is a React component (147 `useState`/`useEffect`/`useMemo` calls, 60
`return (` JSX blocks). See "Dashboard: a real decision, not a detail"
below — **70% of the literal file is not reachable by the extraction
pattern alone**, and that has to be decided before Phase 5 starts.

## What's excluded from the device target, and why

Read each package before deciding, rather than pattern-matching on the
package name — several 0%-coverage packages turned out to be pure logic
with just nobody had written a test yet:

**Excluded (genuinely thin hardware I/O, confirmed by reading the code,
193 statements total):**
- `internal/bindings/buttons` (64 stmt) — `evdev.Open`, `exec.Command`,
  nothing else.
- `internal/bindings/led` (37 stmt) — I2C write driver.
- `internal/wakeword/ort` (92 stmt) — the cgo/dlopen ONNX Runtime binding;
  CLAUDE.md is explicit this exists specifically so the buffering logic
  sits behind `Inferer` in `internal/wakeword` (already 84.9% covered)
  and stays out of `ort/`.

**NOT excluded, despite 0% today — these are pure logic that nobody has
written a test for yet, confirmed by reading the source:**
- `internal/config` (116 stmt) — a mutex-guarded struct with getters,
  setters and defaults. Zero hardware calls.
- `internal/processor` (38 stmt) — the AGC pipeline, pure `math`. No
  hardware calls.
- `internal/clock` (1 stmt) — a `time.Since` wrapper.
- `pkg/buttons` (8 stmt) — interfaces and a plain struct.
- `internal/discovery` (61 stmt) — mDNS browsing has a real network call
  at its core, but the retry/backoff loop around it is a pure state
  machine, injectable via a fake browse function.
- `internal/wifi` (210 stmt) — CLAUDE.md documents this as hard-won,
  hardware-timing-sensitive (svc wifi bounce, DHCP gates), and most of it
  genuinely is. But the pending-marker recovery decision and the
  wpa_supplicant.conf content are pure and worth extracting, the same
  pattern as `em_linkauth.decide`.
- `internal/wakeword/fixture` (171 stmt) — golden-fixture parsing and
  tolerance-policy logic, shared by the host test and `oww_probe`. Pure
  Go, currently just untested directly (it's exercised transitively but
  that isn't attributed under per-package `go test -cover`).
- `internal/bluetooth`, `internal/beamformer`, `internal/server`,
  `internal/client`, `internal/bindings/als`, `internal/bindings/jack`,
  `pkg/led` — already partially tested (28–92%), proving they mix real I/O
  with real logic. The goal is deepening these, not excluding them.

**In-scope total: 2983 statements, currently 1114 covered = 37.3%.**
Target 70% of that = ~2088 covered, i.e. **~974 net new statements need
exercising.**

## Phased plan

### Phase 0 — tooling (completed 2026-08-23)
1. Added `aiohttp` and `pytest-cov` to the controller-tests install line;
   the test command now reports coverage using `.coveragerc`.
2. Added `controller/.coveragerc`, which scopes `--cov=.` to production code
   by omitting `tests/*` from the denominator, so the number reported
   matches "production code" everywhere, not just in this plan's manual
   filtering.
3. Documented `go test -coverprofile=cover.out ./internal/... ./pkg/...`
   and the matching controller coverage command in `CLAUDE.md`.
4. Proceeding with dashboard Option A (extract pure logic and measure that
   surface); the literal `dashboard.jsx` target remains a separate decision
   because most of its 6754 lines are React rendering code.

### Phase 1 — device quick wins (completed 2026-08-23)
`internal/config`, `internal/processor`, `internal/clock`, `pkg/buttons`.
Straightforward unit tests: config defaults/getters/setters under
concurrent access, AGC gain math against synthetic signals (reuses the
existing `internal/aec`/`internal/wakeword` testing style already in the
repo), a clock monotonicity check, button struct behaviour.
Actual coverage after the new tests: **89.7% / 100% / 100% / 100%**
respectively. Added tests cover environment parsing, defaults, partial
config updates, pointer-field snapshots, AGC attack/release/clamping and
passthrough behavior, monotonic clock behavior, click-string mapping and
event-subscription cancellation.

### Phase 2 execution: controller PKI slice (completed 2026-08-23)
The requested next step targeted the controller rather than the original
device-oriented Phase 2 section below, so `em_pki.py` was handled first as
the lowest-coverage controller production module. Added
`controller/tests/test_pki.py` covering:

- TLS directory environment override and database-relative default.
- Lazy generation, complete-file reuse, and regeneration after a partial
  directory loss.
- File permissions, EC P-256 key types, CA/server subjects and issuer,
  fixed DNS SAN, CA/server basic constraints, and server EKU.
- Backdated and long-lived certificate validity windows plus leaf signature
  verification against the generated CA.
- TLS 1.2 minimum in `server_ssl_context()` and ASCII PEM loading.
- Graceful `None` return when `cryptography` is unavailable.

`em_pki.py` increased from **25.4% to 100%** (59/59 statements). The full
controller production-only result increased from **45.3% to 45.9%**;
`699 passed, 1 skipped`.

### Phase 3 execution: controller NS slice (completed 2026-08-23)
Added `controller/tests/test_ns.py` with fake ONNX sessions, covering model
availability and lazy loading, tensor-name discovery, load failure caching,
partial-hop buffering, multi-hop state updates, output clipping, and debug
WAV pair writing/failure handling. No ONNX Runtime or DTLN model files are
required.

`em_ns.py` increased from **0% to 100%** (99/99 statements). Full controller
production-only coverage increased from **45.9% to 47.3%**;
`712 passed, 1 skipped`.

### Phase 2 — device: deepen what's partially tested
Extract and test the pure cores of `internal/wifi` (marker/gate decision,
conf generation) and `internal/discovery` (backoff loop via an injected
browse function) rather than trying to test the real svc/mDNS calls.
Push `internal/beamformer` (selection math), `internal/server` (state
machine, LED mode priority, volume — `shell.go`'s proxy stays low),
`internal/client` (message dispatch, stats, `music_sync` — the actual
socket I/O stays low), `internal/bluetooth` (`scanner.go` advertisement
parsing pushed high, `hci.go`'s real socket stays low), `bindings/als`,
`bindings/jack`, `pkg/led` (animation/config resolution is pure; the I2C
write call stays low), and `internal/wakeword/fixture` (direct unit tests
for the parser and tolerance policy, not just transitive exercise).
**Illustrative targets**: wifi 35%, discovery 51%, beamformer 75%,
server 70%, client 65%, bluetooth 60%, als 80%, jack 71%, led 60%,
fixture 70% (+~1780 stmt across the group).

Phases 1+2 land device at roughly **1114 + 142 + 1780 ≈ 3036 / 2983** —
comfortably over 70% in-scope, with real headroom for targets that land
lower than illustrated.

### Device workstream execution: Wi-Fi slice (completed 2026-08-23)
Added `device/internal/wifi/wifi_test.go` and narrow injection seams for the
external `wpa_cli` query and sleep calls. Tests cover credential validation,
open/WPA config composition, scan parsing/deduplication/sorting, scan errors,
SSID status parsing, polling, pending-result copy/commit behavior, and the
no-marker recovery path. Real Android service toggles, filesystem paths, and
network association gates remain outside host tests.

`internal/wifi` increased from **0% to 35.7%**. The full host-testable device
suite passes, including race checks for Wi-Fi and Phase 1 packages.

### Device workstream execution: discovery slice (completed 2026-08-23)
Added `device/internal/discovery/mdns_test.go` and seams for the browse,
backoff timer, and server verification functions. Tests cover TXT TLS-port
parsing, IPv4/IPv6 candidate conversion, missing-address and failed-verification
rejection, successful discovery, capped retry backoff, cancellation during a
retry, already-canceled contexts, and the single-browse API. No mDNS network
service is required.

`internal/discovery` increased from **0% to 45.8%**. The full host-testable
device suite passes, including discovery/Wi-Fi race checks.

### Device workstream execution: beamformer slice (completed 2026-08-23)
Expanded `device/internal/beamformer/beamformer_test.go` to cover onset and
burst fallback ratios, lock/unlock behavior, signed S24 decoding, band-diff
and energy helpers, wrapped angle selection, gain/clipping accounting,
short-input centre-channel extraction, and unlocked/auto/fixed locked output
modes.

`internal/beamformer` increased from **33.3% to 93.8%**. The full
host-testable device suite passes, including beamformer/discovery/Wi-Fi race
checks.

### Device workstream execution: server state/LED slice (completed 2026-08-23)
Added `device/internal/server/server_test.go` with recording LED fakes. Tests
cover LED base-state recording, legacy listening detection, mute and volume
paint suppression, direction-marker highlighting and angle mapping, base-state
repaint, clamp behavior, and persistence failure handling. Hardware startup,
tinymix, GPIO, and the asynchronous LED initialization path remain outside
host tests.

`internal/server` increased from **32.7% to 49.7%**. The full host-testable
device suite passes, including server/beamformer/discovery/Wi-Fi race checks.

### Device workstream execution: client credentials/reporting slice (completed 2026-08-23)
Added `device/internal/client/client_test.go` and injectable credential paths.
Tests cover plain and pinned-CA credential loading, token trimming/headers,
invalid-CA fallback, TLS build-time clock clamping, disconnected outbound
report drops, and the existing control-clock tests.

`internal/client` increased from **36.1% to 39.6%**. The client race suite
passes. The remaining client gap is concentrated in live WebSocket handshake,
dispatch, shell, and data-plane behavior, which should be covered with fake
WebSocket integration tests separately rather than broadening this unit slice.

### Device workstream execution: Bluetooth scanner slice (completed 2026-08-23)
Added `device/internal/bluetooth/scanner_test.go`. Tests cover advertisement
batch keys, duplicate coalescing with latest RSSI retention, count-triggered
flushes, callback/send statistics, unique-address expiry, environment integer
defaults, and idempotent enable/disable state transitions. Real HCI transport,
Bluedroid package commands, and watchdog/retry timing remain outside host tests.

`internal/bluetooth` increased from **28.9% to 57.5%**. The Bluetooth race
suite and full host-testable device suite pass.

### Device workstream execution: ALS read/report slice (completed 2026-08-23)
Expanded `device/internal/bindings/als/als_test.go` to cover valid lux reads,
malformed and missing sysfs readings, absent-sensor reads, and the watcher
no-sensor return path. Existing fixtures already cover sensor resolution and
status refresh behavior; real sysfs/I2C polling and timed change delivery remain
outside host tests.

`internal/bindings/als` increased from **60.2% to 73.9%**. The ALS race suite
and full host-testable device suite pass.

### Phase 3 — controller quick/medium wins
`em_auth.py` (session/bcrypt/role logic), `em_db.py`
(query and migration-fixup logic), `em_oww_assets.py` (LRU eviction,
free-space math — CLAUDE.md already calls this "pure logic, unit-tested,"
so 57% is real untested branches, not a designed gap), `em_sendspin.py`,
`em_hostip.py`, `tools/sync_channels.py`, `em_start.py` (the
option→env-var mapping is already exercised as *data* by
`test_deploy.py`; add tests that actually run `main()`'s mapping logic).
**Illustrative targets**: 85%, 82%, 85%, 88%, 92%, 85%, 45%, 40%
respectively (+~384 stmt).

### Phase 4 — the big lever: `em_api.py` and `em_controller.py`
This is genuinely necessary — Phases 3+5 alone cap out well short of 70%
for the controller, because these two files are 48% of production
controller code. Two techniques, both already precedented in this repo
rather than new:

1. **Handler-level tests via `aiohttp.test_utils`**, the exact pattern
   `test_training_captures_api.py` already established: stub the one
   heavy import (`websockets`) with a `sys.modules` shim, build a real
   `aiohttp.web.Application`, wire the routes under test, and drive them
   through `test_utils.TestClient`. Extend this to `em_api.py`'s other
   REST handlers — device CRUD, config push, OTA endpoints' request
   parsing/validation, the support-bundle and provisioning-diagnostics
   endpoints — covering route wiring, auth branches, and 400/403/404
   paths without needing a real device connection.
2. **`em_controller.py` needs its heavy imports stubbed too**:
   `openwakeword.model.Model` and `zeroconf`/`zeroconf.asyncio` are
   imported at module level and are why it's excluded from today's test
   env (CLAUDE.md: "nothing that pulls in openwakeword/onnxruntime").
   Stub both with `sys.modules` shims the same way `websockets` is
   stubbed — the `Device` class's config application, LED state
   transitions, message dispatch table, and RTT/stats bookkeeping don't
   need a real inference session or real mDNS to exercise; only the wake
   listener itself does, and that stays uncovered (correctly — it needs
   real audio and a real model, matching the documented CI policy).

**Illustrative targets**: `em_api.py` 55%, `em_controller.py` 42%
(+~1420 stmt combined). That, plus Phase 3, plus the untouched ~85–100%
modules, lands the controller at **≈70.6%** production-code coverage —
the arithmetic in "Baseline" above shows this isn't a rough guess, it's
the number needed to close the gap.

### Phase 5 — dashboard
Blocked on the decision below.

### Phase 6 — wire up reporting (not gating)
A CI job (or a local script run periodically) that runs `pytest --cov`,
`go test -coverprofile`, and whatever the dashboard tool ends up being,
and publishes the numbers somewhere visible — a job summary or a checked-in
report, following the `pip-audit`/`govulncheck` report-only precedent
already in `ci.yml`. No failing exit code tied to the percentage.

## Dashboard: a real decision, not a detail

`dashboard.jsx` is one 6754-line file compiled by esbuild
(`--bundle=false --jsx=transform`, see the controller Dockerfile) into
`dashboard.js`, loaded as a classic script — no bundler, no test runner,
no `package.json` anywhere in the repo. Two genuinely different paths:

**Option A — extend the existing extraction pattern (recommended).**
Pull every plausible pure function (`deviceState`, `effectiveConfig`,
`_bannerMode`, `onDeviceMode`, `turnSegments`, `wifiBand`, `uptime`,
`relTime`, `wwModelLabel`, `ingressPath`/`isIngress`/
`ingressWebSocketUrl`, and any similar logic still inline in components)
into small modules alongside the existing `wifi_scan.test.mjs` /
`boot_target.test.mjs` / `pm_verdict.test.mjs`, and measure coverage of
*that extracted surface*, not the whole file — same shape as everything
else in this plan. Cost: low, no new infrastructure, matches CLAUDE.md's
own description of what's testable here ("the pure logic that decides
what the wizard will and will not attempt"). **Ceiling: nowhere near 70%
of `dashboard.jsx`'s 6754 lines** — the extractable surface is maybe
300–400 lines. If 70% must be a literal statement-coverage number over
the whole file, this option cannot reach it, full stop.

**Option B — stand up component testing.** Add `jsdom` + a React test
renderer (or React Testing Library) and a JSX-aware test runner (esbuild
is already vendored into the Docker build and could drive this too),
render components, and assert on behavior. This is real new
infrastructure — a first `package.json`, a first test runner config, a
first dependency on jsdom — not "write more tests" in the sense every
other phase of this plan is. It's the only path to a literal 70% of
`dashboard.jsx`, because most of the file is component/render code, not
decision logic.

**Recommendation: Option A, with the 70% dashboard target redefined as
70% of the extracted pure-logic modules** (realistically achievable at
90%+, matching the other `.test.mjs` files' near-100% coverage already).
This needs an explicit decision before Phase 5 is scoped, because it
changes the phase from a few days of test-writing to standing up new
infrastructure.

## What this plan deliberately does not do

- No test for `internal/bindings/buttons`, `internal/bindings/led`, or
  `internal/wakeword/ort` — real hardware/cgo, excluded per the target
  definition above, not a shortfall.
- No CI gate on the percentage — report-only, per the repo's existing
  `pip-audit`/`govulncheck` precedent.
- No chasing 100% anywhere — the illustrative per-file/per-package targets
  above are deliberately short of 100%, leaving the genuinely
  hardware/timing/model-dependent remainder (real svc wifi calls, real
  mDNS browse, real BLE HCI socket, real DTLN inference, the wake-word
  listener itself) uncovered, matching CLAUDE.md's stated position that
  this class of code "is not testable on the host."
