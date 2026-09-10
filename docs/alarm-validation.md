# Alarm validation

Alarms are wall-clock notifications (see `docs/design/alarms-design.md`). The
pure scheduling, DST, persistence, delivery, and API logic is covered by the
host unit suites; this document is the on-hardware / on-Home-Assistant
checklist for the parts those suites cannot reach (Assist LLM tools, the
Lovelace card, and a real device ringing).

## What the unit suites already cover

- `controller/tests/test_alarms.py` — next-fire for once/daily/weekly, DST
  spring-forward gap and fall-back fold, weekday masks, one-off in the past,
  the "next occurrence" date resolution, and validation.
- `controller/tests/test_alarm_scheduler.py` — due detection, no double-fire,
  grace-window late fire, skip-stale, muted/offline still advancing, one-off
  disable, and one bad alarm not stalling the rest.
- `controller/tests/test_db_alarms.py` — the v28 schema, CRUD, the
  scheduler-owned columns being unwritable through `update_alarm`, and the
  `delete_device` cascade.
- `controller/tests/test_alarms_api.py` — the REST CRUD handlers, validation,
  the timezone default, foreign-device rejection, and the fire_alarm /
  `_deliver_finished` timer/alarm parity (present/muted/offline).
- `hacs/tests/test_alarm_tools.py` — one-off/periodic tool payload shaping,
  weekday conversion, strict date/time validation, allowlisted results, and the
  four required tool names.
- `hacs/tests/test_alarm_card.py` — multi-controller client routing.
- `hacs/tests/test_client.py` — the alarm REST client methods.

## Assist LLM tools

The integration automatically contributes these four tools to Home Assistant's
built-in **Assist** LLM API:

- `EchomuseSetOneOffAlarm`
- `EchomuseSetPeriodicAlarm`
- `EchomuseCancelAlarm`
- `EchomuseListAlarms`

There are no alarm IntentHandlers and no sentence files to install. The
conversation agent must support HA tool calling and be configured to use the
Assist API (often labelled **Control Home Assistant**). The tools are offered
only for a request originating from an EchoMuse satellite; that device context
selects the controller and the Echo whose alarms may change.

## Installing the card

Add `www/echo-voice-alarms-card.js` as a dashboard resource (Settings →
Dashboards → Resources, or `lovelace: resources:` in YAML mode), then add a
card of type `custom:echo-voice-alarms-card`. Optional config: `title`, and
`device_id` to scope the card to one Echo.

## On-HA / hardware checklist

1. **Tool discovery.** Use HA's conversation prepare/debug surface with an
   EchoMuse satellite as the source. Confirm exactly the four `Echomuse*Alarm`
   tools above appear. Repeat without an EchoMuse `device_id`; no alarm tools
   should be offered.
2. **Create through Assist.** Ask the tool-capable LLM to set a 7am one-off
   alarm. Confirm the spoken reply, that the alarm appears in the card, and it
   survives a controller restart
   (`docker compose restart` / add-on restart) — it must reload from SQLite.
3. **Recurring.** Ask for a 7am weekday alarm. Confirm the LLM calls
   `EchomuseSetPeriodicAlarm` with weekday names, not a bitmask. Let it fire two
   consecutive weekdays; confirm each day rings (the per-occurrence id must not
   dedupe the second day) and the weekend is skipped.
4. **One-off.** Set a dated 6am alarm. Confirm it rings once and then shows
   disabled/gone rather than repeating.
5. **Cancel / list.** Ask for alarms, then cancel one. Confirm the LLM calls
   `EchomuseListAlarms` first and passes its exact id to `EchomuseCancelAlarm`.
6. **Ring behaviour.** Confirm the fired alarm reuses the timer ring: amber LED
   pulse, music duck/restore, and dismissal by spoken stop word, action-button
   tap, and the card's Delete action.
7. **Mute policy.** Mute the device, let an alarm come due: it must produce no
   sound and no LED, and (if recurring) still schedule the next day.
8. **Offline / missed.** Stop the controller before a fire time and start it
   again within 10 minutes (grace): the occurrence fires once on startup. Start
   it back after >10 minutes: the occurrence is skipped and the next future one
   is scheduled.
9. **DST.** Simulate by setting an alarm for a time on a transition day (or
   temporarily set the device timezone to one near a transition). A 7am alarm
   must stay 7am local across the change — this is covered by unit tests, but
   confirm once end to end.
10. **Timezone.** Confirm a new alarm created without an explicit timezone uses
   the device's timezone (seeded from `hass.config.time_zone` on integration
   setup).
