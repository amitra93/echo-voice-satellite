# Alarms Design

## Goal

Support wall-clock **alarms** on EchoMuse devices, alongside the existing
countdown **timers**. A user can create, name, list, and cancel alarms through
Home Assistant Assist (voice) and a Home Assistant
Lovelace card. Each fired alarm notifies its originating Echo with the same
continuously repeating alert used for a finished timer.

The three requirements that shape everything below:

1. **Durability.** Alarms survive a controller restart.
2. **One-off and recurring.** "Wake me at 7am tomorrow" and "7am every
   weekday" are both first-class.
3. **Timezone- and DST-correct.** An alarm fires at its intended local wall
   time regardless of daylight-saving transitions.

## Alarms are not timers

Timers and alarms share the *ringing*, and nothing else:

| | Timers (existing) | Alarms (new) |
|---|---|---|
| Time model | relative duration | absolute wall-clock |
| Clock | **monotonic** everywhere | **wall clock** for scheduling |
| Owner of the record | Home Assistant `TimerManager` (in-memory) | **EchoMuse controller** (SQLite) |
| Survives restart | no (by design) | **yes** |
| Recurrence | no | yes |
| Firmware time needed | no | no |

Because the *record* owners differ, the two features do not share a store or a
scheduler. They **do** share the ring: a fired alarm is delivered to the device
through the exact same path as a finished timer (see "Firing reuses the ring").

## Why the controller owns alarms

Home Assistant has **no alarm concept**. Its built-in intents cover timers
(`HassStartTimer`, `HassCancelTimer`, …) but there is no `HassSetAlarm` and no
alarm entity — the only "alarm" in HA is the unrelated security
`alarm_control_panel`. So there is no HA durable record type to defer to, unlike
timers where CLAUDE.md's "Home Assistant owns every durable timer record" rule
applies. Alarms are therefore owned where the durability, recurrence, and DST
logic can be **pure, controller-side, and unit-tested** in the same style as
`em_timers`, `em_turnclock`, and the rest of that family, and where the physical
alarm, LED, queue, and dismissal already live.

Consequences accepted with this choice:

- Alarms are **not** native Home Assistant entities. Management is through
  Assist LLM tools this integration supplies and a dedicated Lovelace card. A
  read-only "next alarm" HA sensor is a possible later addition but is not part
  of the first release.
- The EchoMuse dashboard has **no** alarm UI in the first release. The REST API
  exists, so adding one later is cheap, but the requested surfaces are voice and
  the HA card only.

## The wall-clock exception

The controller and device code use **monotonic** clocks nearly everywhere,
deliberately: the device boots with a bogus pre-NTP clock, and durations must be
immune to wall-clock jumps. Alarm **scheduling** is the one place that must use
the **wall clock**, because "7am" is a wall-clock fact, not a duration. This is
safe here and only here:

- The scheduling runs on the **controller host**, which has a real, NTP-backed
  clock and a real timezone — not on the device.
- The **device still never learns the time.** It rings when told, exactly as for
  a finished timer.
- The alarm's **ring timeout** (how long it rings unanswered) stays **monotonic**
  — `em_timer_alarm.py` is untouched and keeps using `loop.time()`.

Only the "is this alarm due, and when does it next fire" computation uses
`time.time()` and `zoneinfo`.

## Timezone and DST

Each alarm carries an **IANA timezone** string (e.g. `America/New_York`). Its
next fire time is **rebuilt in that zone on every occurrence** and converted to
a UTC epoch for comparison against `time.time()`:

```
next_fire_utc = ZoneInfo-aware local datetime (date + local_time, tz) -> UTC
```

Because the local wall time is re-materialised in the zone each time, a "7:00
every weekday" alarm always fires at 7:00 local, on both sides of a DST
transition, with no correction step. DST edge policy for the *day of* a
transition:

- **Spring-forward gap** (the local time does not exist, e.g. 02:30 where the
  clock jumps 02:00→03:00): fire at the **first valid instant** after the gap.
- **Fall-back ambiguity** (the local time occurs twice): use `fold=0` — the
  **earlier** of the two.

Both are documented and unit-tested.

**Per-device timezone.** Devices have no native timezone. The default is
supplied by the HACS integration, which reports `hass.config.time_zone` to the
controller on register; it is overridable per device through the API. This is
"the timezone of the device". The timezone is controller-side metadata and is
**not** pushed to the device in its `ConfigMessage` — the device never needs it.

## Data model

New `alarms` table (schema **v28**, append-only migration, backed by the
existing pre-migrate backup machinery):

| column | meaning |
|---|---|
| `id` | opaque alarm id (uuid) |
| `device_id` | originating EchoMuse device |
| `label` | user free text ("weekday", "gym"); nullable |
| `local_time` | `HH:MM` wall time |
| `recurrence` | `once` \| `daily` \| `weekly` |
| `weekday_mask` | bitmask Mon..Sun for `weekly` (covers weekdays/weekends/specific days) |
| `date` | ISO local date for `once` |
| `tz` | IANA timezone string |
| `enabled` | 0/1 |
| `next_fire_utc` | cached next occurrence as UTC epoch (recomputed on write/fire) |
| `last_fired_occurrence` | id of the last occurrence delivered — the double-fire guard across ticks and restarts |
| `created_by` | user/integration that created it |

`db.delete_device` deletes that device's alarms explicitly (like recordings —
nothing cascades to it implicitly).

## Pure logic: `em_alarms.py`

A clock-free module in the `em_timers`/`em_turnclock` family. All decisions are
functions taking `now_utc` as a float, so they test with plain numbers:

- `next_fire(spec, now_utc) -> float | None` — the next UTC epoch this alarm
  should fire at, applying the recurrence and DST policy above. `None` for a
  disabled or spent one-off.
- `advance(spec, after_utc) -> spec` — recompute `next_fire_utc` after a fire.
- `is_due(spec, now_utc, grace_s) -> bool` — due now, within a grace window.
- Occurrence identity helper producing the per-occurrence id used to dedupe
  (see below).

No asyncio, no DB, no device access.

## Firing reuses the ring

A fired alarm is injected as a **synthetic finished timer** into the same
`em_timers.AlarmSession` the device already uses, then handed to the existing
`em_timer_alarm.TimerAlarmRunner`. This reuses, unchanged: the FIFO alarm queue
(so an alarm and a timer that come due together queue rather than double-play the
speaker), music duck/restore, the amber `timer_anim` LED pulse, the
unanswered-ring monotonic timeout, and all three dismissal paths (spoken stop
word, action-button, card).

Two things make this safe:

- **Per-occurrence unique id.** The synthetic event's `timer_id` is
  `alarm:<alarm_id>:<occurrence>` (occurrence = the local date it fired for). A
  recurring alarm therefore presents a *different* id each day, so
  `AlarmSession`'s fingerprint/`_cancelled` dedup never suppresses the next day's
  ring.
- **Shared delivery helper.** The `finished`-handling body of
  `_post_timer_event` (offline discard, muted discard, `alarm_changed`,
  `runner.start`, `timer.alarm` snapshot push) is factored into a shared
  `_deliver_finished(device_id, session, event)` so the HA-timer path and the
  alarm path are identical and tested once.

## Policies

- **Mute → discard.** A muted device's alarm produces **no sound and no LED**,
  the same as a muted timer expiry (`_deliver_finished`'s muted branch). This
  preserves the device-sovereign mute invariant; a muted device silently misses
  its wake-up, which the user accepts over breaking mute sovereignty.
- **Offline → best effort.** If the device is not connected at fire time, the
  occurrence is not delivered; a recurring alarm still schedules its next
  occurrence.
- **Missed fire, grace window.** On controller startup and on device reconnect,
  any enabled alarm whose `next_fire_utc` is in the past is fired **once** if it
  is within `ALARM_LATE_GRACE_S` (default **600 s**) *and* the device is present
  and unmuted; otherwise the occurrence is skipped. Either way, recurring alarms
  advance to the next future occurrence and one-offs are disabled. The
  `last_fired_occurrence` guard prevents a fired occurrence from re-firing on the
  next tick or a subsequent restart.

## Scheduler: `em_alarm_scheduler.py`

Asyncio, impure — the counterpart to the pure `em_alarms.py`, the same split as
`em_timer_alarm` vs `em_timers`:

- A **tick loop** (~15–30 s) plus an **event re-arm** on any add/delete.
  Ticking (rather than one long `asyncio.sleep` to the next fire) is what makes
  it robust to NTP steps, host suspend/resume, and DST jumps: a wall-clock jump
  is caught within one tick. Minute-granular alarms make a 15–30 s tick cheap.
- On fire: build the synthetic finished event, route through `_deliver_finished`,
  then `advance` (recurring) or disable (one-off), persisting `next_fire_utc` and
  `last_fired_occurrence`.
- Startup + reconnect reconciliation applies the grace-window policy above.

Wired into `em_controller` startup next to the other long-lived tasks.

## API

Auth: `require_integration_or_admin` (same as `timer-events`), so both the HACS
integration and an admin session can use them.

- `GET  /api/devices/{id}/alarms`
- `POST /api/devices/{id}/alarms`
- `DELETE /api/devices/{id}/alarms/{aid}`
- `GET  /api/alarms` (fleet-wide, for the card)

## Assist LLM tools (HACS)

Home Assistant has no native alarm intent, so this integration contributes four
explicit tools to HA's built-in Assist LLM API: `EchomuseSetOneOffAlarm`,
`EchomuseSetPeriodicAlarm`, `EchomuseCancelAlarm`, and
`EchomuseListAlarms`. They are deliberately **not** IntentHandlers and have no
sentence-matching configuration. The LLM receives them only when the turn's
`device_id` resolves to a registered EchoMuse satellite; a generic chat cannot
choose an Echo to control.

The create tools take 24-hour local time. One-off accepts an optional ISO date
(or selects the next occurrence), while periodic accepts daily or weekly
recurrence and friendly weekday names. Cancellation requires an exact id from
`EchomuseListAlarms`, avoiding a fuzzy request deleting two 7am alarms. Tool
calls resolve the current controller client and Echo at execution time and call
the existing `client.py` `async_alarm_*` methods. The integration reports
`hass.config.time_zone` on register as the default alarm timezone.

## HA Lovelace card

`echo-voice-alarms-card.js` plus WebSocket commands
(`echo_voice_satellite/alarms/{list,create,cancel}`), mirroring
the existing timer card's structure and its per-config-entry hub scoping.
Displays every alarm with its label, originating Echo, local time, recurrence,
completion state, and timezone, and offers create/cancel only. An alarm is
immutable after creation; create a replacement instead of editing it.

## No firmware change

Core alarms reuse `led_anim` `timer_anim`, the speaker, and the stop-word /
action-button / card dismissal paths already built for timers. The device never
learns the wall-clock time.

## Out of scope (first release)

Deferred, each a clean follow-on on the Phase-2 API: snooze, gradual volume
ramp, sunrise LED ramp (needs a device-side animation), music/media alarms,
per-alarm volume/sound, skip-next-occurrence, spoken reminders (TTS instead of
chime), the "next alarm" HA sensor, and an EchoMuse-dashboard alarm UI.

## Phases

1. **Pure logic + persistence.** `em_alarms.py` + `test_alarms.py`; schema v28
   `alarms` table + DB CRUD + `delete_device` cascade + migration test; per-device
   timezone storage.
2. **Scheduler + fire→ring.** `_deliver_finished` refactor (+ timer/alarm parity
   test); `em_alarm_scheduler.py` (+ tests with a fake clock/session/runner: due
   detection, no double-fire, grace-window late fire, skip-stale, muted/offline
   discard, recurring reschedule, one-off disable); REST endpoints (+ auth tests);
   wire into `em_controller` startup.
3. **Assist tools.** Four explicit LLM tools + `client.py` methods + tz
   reporting + HACS tests. No custom intents or sentence files.
4. **Card.** `echo-voice-alarms-card.js` + WS commands + tests + a `test_deploy`
   guard that the card's command list matches the Python allowlist.
5. **Integration + validation.** `em_support` allowlist (pseudonymize alarm
   labels — user free text, like device labels — and never any speech);
   `docs/alarm-validation.md` with a DST-simulation and hardware checklist; run
   all four suites (controller / HACS / device / oww_forge); ship under a
   `controller-v*` tag.

## Tests

- **Pure (`test_alarms.py`):** next-fire for once/daily/weekly; DST spring-forward
  gap and fall-back fold; weekday mask; one-off in the past; advance idempotency;
  timezone override.
- **DB:** v27→v28 additive migration; `delete_device` removes alarms; a newer DB
  still refuses an older controller (existing guard).
- **Scheduler:** fake clock/session/runner — due detection, no double-fire across
  ticks and restart (`last_fired_occurrence`), grace-window late fire,
  skip-stale, muted/offline discard, recurring reschedule, one-off disable.
- **Delivery parity:** `_deliver_finished` behaves identically for a timer
  `finished` and an alarm fire (offline, muted, queue, runner start).
- **HACS:** intent handlers (create/cancel/list), sentence coverage, client
  methods, timezone reporting; card WS command contract.
- **Deploy shape:** endpoints carry the right auth; the card command list matches
  the Python allowlist; the support bundle never contains an alarm label's raw
  text.
