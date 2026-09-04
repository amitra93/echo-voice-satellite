# Controller dev tools

Programmatic device access via the controller API, for development sessions
without a dashboard. All three run **inside the controller container** (they
mint a temporary session token directly in the SQLite `sessions` table, use
it as `Authorization: Bearer`, and delete it afterwards):

```bash
docker cp controller/tools/devshell.py echomuse-controller:/tmp/
docker exec echomuse-controller python /tmp/devshell.py "<shell command>" ["<another>" ...]
```

- **devshell.py** — run commands on a device over the `/shell` proxy (PTY
  mode; output includes echoed input — device is mksh + busybox, no
  tail/sed/head, use `busybox <applet>`). Defaults to the device hardcoded at
  the top; `-d <serial>` picks another (repeatable) and `--all` runs against
  every currently-connected device, which is what most diagnostic questions
  want. **Read-only in practice**: the shell is a child of the server, so
  stopping the service kills the shell mid-command and takes the device down
  until it is power cycled.
- **ota.py** — push a locally built binary: `docker cp device/build/server
  echomuse-controller:/tmp/server-new` first, then
  `python /tmp/ota.py <device_id>` (upload → `/api/devices/{id}/update`).
- **pull_so.py** — pull a file off the device (busybox base64 over the
  shell, echo disabled, split end-markers). Writes the decoded file to
  stdout: `python /tmp/pull_so.py /system/lib64/libled_hal.so > out.so`.
- **push_file.py** — the other direction, for arbitrary paths (`ota.py`
  only writes the firmware slot): `python /tmp/push_file.py <device_id>
  /tmp/oww_probe /data/local/tmp/oww_probe --chmod 755`. Reconnects every
  30s because the shell plane drops after ~50s under load, so it handles
  multi-megabyte files; **deletes the destination first** unless
  `--resume`, since resuming on size alone will append to a different file
  and then report success. Success means md5 agreement, nothing less.
- **e2e_query_test.py** — drives the audio-upload E2E query test harness
  (device Test tab's `test_audio`/`test_turn` path) against a real device,
  using the case catalog in `controller/tests/fixtures/e2e_audio/manifest.json`:
  `python /tmp/e2e_query_test.py -s <SERIAL> --fixtures-dir /tmp/e2e_audio --all`.
  `<SERIAL>` is `ro.serialno`, same as `-d` above. See
  `docs/e2e-query-testing.md` for the full workflow, including generating the
  fixture corpus first.
