"""Drive the audio-upload E2E query test harness against a real device.

Uploads each fixture in controller/tests/fixtures/e2e_audio/manifest.json to
POST /api/devices/{id}/test_audio, triggers it with
POST /api/devices/{id}/test_turn — the same path the dashboard's device Test
tab uses, which streams the WAV onto the device's real microphone plane, so
from that point the controller cannot tell it apart from a real utterance —
then polls GET /api/devices/{id}/turns for the resulting row and asserts on
its outcome/stt_text/tts_text. See docs/e2e-query-testing.md for the full
case catalog and what this harness structurally cannot reach (barge-in,
multi-device arbitration, live-speech follow-ups, announcements — those stay
manual/hardware-only).

Run inside the controller container, or on a host with direct access to its
SQLite DB and API port — the same assumptions controller/tools/devshell.py
and ota.py make. It mints its own short-lived admin session token straight
into the sessions table and drops it on exit, so no long-lived API key is
needed:

    docker cp controller/tools/e2e_query_test.py echomuse-controller:/tmp/
    docker cp controller/tests/fixtures/e2e_audio echomuse-controller:/tmp/e2e_audio
    docker exec echomuse-controller python /tmp/e2e_query_test.py \\
        -s <SERIAL> --fixtures-dir /tmp/e2e_audio --list

<SERIAL> is the device's ro.serialno — the same id `adb -s <SERIAL>` uses to
reach it, and exactly the controller's device_id (em_controller.py keys its
device registry on ro.serialno).

If the controller's data directory is bind-mounted to the host (the common
dev-compose shape, e.g. network_mode: host with ./controller/data:/app/data),
this can run directly from a repo checkout instead, with no docker cp:

    python controller/tools/e2e_query_test.py -s <SERIAL> --db controller/data/echomuse.db --all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE.parent / "tests" / "fixtures" / "e2e_audio" / "manifest.json"

# Bounds. Generous rather than tight: this exercises a real HA pipeline over
# real hardware, and a slow LLM response is a pass, not a flake, right up to
# the point where something has actually wedged.
#
# 180s, not something tighter: a turn with nothing for HA to say (e.g. an
# empty transcript from silent/noise audio) can legitimately ride the full
# em_announce.ANNOUNCE_TIMEOUT_S (120s) backstop before ending as
# audio_timeout — measured directly against real hardware, not a guess. A
# shorter poll window doesn't fail that case faster, it just gives up on it
# while the server is still mid-turn, which then fails every case queued
# behind it with test_turn_busy. See N4-silence's manifest note.
TURN_POLL_INTERVAL_S = 1.0
TURN_POLL_TIMEOUT_S = 180.0
CONTINUATION_POLL_TIMEOUT_S = 10.0
MUTE_SETTLE_TIMEOUT_S = 5.0
SETTLE_DELAY_S = 3.0
OVERSIZED_UPLOAD_BYTES = 50 * 1024 * 1024 + 1024  # just over TEST_AUDIO_MAX_INPUT


# ─── DB / session token — same pattern as devshell.py and ota.py ───────────
#
# Inlined rather than shared: these tools are docker-cp'd into the container
# one file at a time and must stay standalone (see controller/tools/README.md).

def resolve_db(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("EM_DB", "").strip()
    if env:
        return env
    for c in ("/data/echomuse.db", "/app/data/echomuse.db"):
        if os.path.exists(c):
            return c
    return "/app/data/echomuse.db"


def make_token(db: str) -> str:
    tok = secrets.token_hex(32)
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            sys.exit(
                f"no admin user in {db} — bootstrap one via the dashboard "
                "before running this"
            )
        now = int(time.time())
        con.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (tok, row[0], now, now + 3600),
        )
        con.commit()
    finally:
        con.close()
    return tok


def drop_token(db: str, tok: str) -> None:
    con = sqlite3.connect(db)
    con.execute("DELETE FROM sessions WHERE token = ?", (tok,))
    con.commit()
    con.close()


# ─── result bookkeeping ─────────────────────────────────────────────────────

class CaseOutcome:
    PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

    def __init__(self, case_id: str, status: str, detail: str = "", turn: dict | None = None):
        self.case_id = case_id
        self.status = status
        self.detail = detail
        self.turn = turn

    def as_dict(self) -> dict:
        return {"id": self.case_id, "status": self.status, "detail": self.detail}


# ─── the runner ─────────────────────────────────────────────────────────────

class Runner:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, serial: str, fixtures_dir: Path):
        self.s = session
        self.base = base_url.rstrip("/")
        self.serial = serial
        self.fixtures = fixtures_dir

    # -- thin HTTP wrappers -------------------------------------------------

    async def get(self, path: str, **kw):
        async with self.s.get(f"{self.base}{path}", **kw) as r:
            body = await r.json()
            return r.status, body

    async def post(self, path: str, **kw):
        async with self.s.post(f"{self.base}{path}", **kw) as r:
            try:
                body = await r.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                body = {}
            return r.status, body

    async def delete(self, path: str, **kw):
        async with self.s.delete(f"{self.base}{path}", **kw) as r:
            try:
                body = await r.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                body = {}
            return r.status, body

    # -- device state ---------------------------------------------------

    async def device(self) -> dict:
        status, body = await self.get(f"/api/devices/{self.serial}")
        if status != 200:
            sys.exit(f"GET /api/devices/{self.serial} -> {status}: {body}")
        return body

    async def ensure_mute(self, desired: bool) -> bool:
        """Toggle the device to the desired mute state and confirm it took.

        mute_toggle is a toggle, not a set — the controller deliberately
        never guesses the resulting state (test_mute_button.py pins this),
        so the only way to know it applied is to poll the device's own
        reported state afterward.
        """
        dev = await self.device()
        if bool(dev.get("muted")) == desired:
            return True
        status, _ = await self.post(f"/api/devices/{self.serial}/media",
                                     json={"command": "mute_toggle"})
        if status != 200:
            return False
        deadline = time.monotonic() + MUTE_SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            dev = await self.device()
            if bool(dev.get("muted")) == desired:
                return True
            await asyncio.sleep(0.3)
        return False

    # -- upload / trigger -------------------------------------------------

    async def upload_file(self, path: Path) -> tuple[int, dict]:
        form = aiohttp.FormData()
        form.add_field("audio", path.read_bytes(), filename=path.name, content_type="audio/wav")
        return await self.post(f"/api/devices/{self.serial}/test_audio", data=form)

    async def upload_raw(self, data: bytes) -> tuple[int, dict]:
        # _post_test_audio branches on content_type; anything not starting
        # with "multipart/" takes the raw-body path, which is exactly what
        # the empty/oversized negative-path cases want to hit directly.
        timeout = aiohttp.ClientTimeout(total=90)
        async with self.s.post(
            f"{self.base}/api/devices/{self.serial}/test_audio",
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=timeout,
        ) as r:
            try:
                body = await r.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                body = {}
            return r.status, body

    async def trigger(self) -> tuple[int, dict]:
        return await self.post(f"/api/devices/{self.serial}/test_turn", json={})

    async def trigger_with_retry(self, timeout: float = TURN_POLL_TIMEOUT_S) -> tuple[int, dict]:
        """trigger(), retrying on test_turn_busy rather than failing the case.

        Measured on hardware: a turn whose reply ends on a question (this
        pipeline does this often — "How can I help you?") sets
        continue_conversation, and the ORIGINAL test task keeps running
        through that continuation loop (test_turn_active is keyed on that
        whole task, not on any one turn row) even after the row this runner
        was waiting on has already gone terminal. So "the turn I waited for
        finished" does not imply "the device is free for the next trigger" —
        busy here is routinely transient, not a real conflict, and is worth
        riding out rather than surfacing as a case failure. G-busy calls
        .trigger() directly instead, since it exists to observe exactly this
        409 deliberately.
        """
        deadline = time.monotonic() + timeout
        status, body = await self.trigger()
        while status == 409 and body.get("code") == "test_turn_busy" and time.monotonic() < deadline:
            await asyncio.sleep(TURN_POLL_INTERVAL_S)
            status, body = await self.trigger()
        return status, body

    # -- turn polling -------------------------------------------------------
    #
    # Watermarked on turn_id, not on a `since` timestamp. Two back-to-back
    # cases can easily fall within any timestamp lookback margin small
    # enough to also tolerate clock/round-trip skew, and get_turns's ts
    # ordering means a poll landing before the CURRENT turn's row exists
    # would then happily return the PREVIOUS case's already-terminal row as
    # if it were this one — the run finishing early while the server is
    # still mid-turn, tripping test_turn_busy on whatever runs next. A
    # rowid watermark has no such window: any row with turn_id > watermark
    # is strictly newer than anything visible when the watermark was taken.

    async def latest_turn_id(self) -> int:
        status, turns = await self.get(f"/api/devices/{self.serial}/turns", params={"limit": "1"})
        if status == 200 and turns:
            return turns[-1].get("turn_id", 0)
        return 0

    async def wait_for_turn(self, after_id: int, trigger: str = "test_audio",
                             timeout: float = TURN_POLL_TIMEOUT_S) -> dict | None:
        """Poll for the first turns row matching `trigger` with turn_id >
        after_id that has reached a terminal outcome (outcome is no longer
        NULL). Only one test_turn can run per device at a time
        (test_turn_busy), so there is at most one such row to find."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, turns = await self.get(
                f"/api/devices/{self.serial}/turns", params={"limit": "20"},
            )
            if status == 200:
                matches = [t for t in turns
                           if t.get("trigger") == trigger and t.get("turn_id", 0) > after_id]
                if matches and matches[0].get("outcome") is not None:
                    return matches[0]
            await asyncio.sleep(TURN_POLL_INTERVAL_S)
        return None

    async def saw_continuation(self, after_id: int,
                                timeout: float = CONTINUATION_POLL_TIMEOUT_S) -> dict | None:
        """Best-effort: did a `continuation` turn get raised after the main
        one (turn_id > after_id, which should be the main turn's own id)?
        Its own outcome is not asserted — without live speech in the room it
        will typically end no_speech, which is expected (see
        F1-continuation-shaped's note in the manifest)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, turns = await self.get(
                f"/api/devices/{self.serial}/turns", params={"limit": "20"},
            )
            if status == 200:
                for t in turns:
                    if t.get("trigger") == "continuation" and t.get("turn_id", 0) > after_id:
                        return t
            await asyncio.sleep(TURN_POLL_INTERVAL_S)
        return None

    # -- one case -------------------------------------------------------

    async def run_setup_phrase(self, case_id: str, i: int) -> str | None:
        fixture = self.fixtures / f"{case_id}-setup-{i}.wav"
        if not fixture.is_file():
            return f"missing setup fixture {fixture.name} — run generate_fixtures.py"
        status, body = await self.upload_file(fixture)
        if status != 201:
            return f"setup upload failed: {status} {body}"
        watermark = await self.latest_turn_id()
        status, body = await self.trigger_with_retry()
        if status != 202:
            return f"setup trigger failed: {status} {body}"
        turn = await self.wait_for_turn(watermark)
        if turn is None:
            return f"setup phrase {i} never reached a terminal outcome"
        return None  # setup outcome itself is not asserted

    async def run_case(self, case: dict) -> CaseOutcome:
        cid = case["id"]
        pre = case.get("preconditions", {})

        if pre.get("manual_setup"):
            if not sys.stdin.isatty():
                return CaseOutcome(cid, CaseOutcome.SKIP,
                                    f"needs manual setup, not interactive: {pre['manual_setup']}")
            print(f"\n[{cid}] {pre['manual_setup']}")
            input("Press Enter once ready... ")

        # Asserted unconditionally, not just when a case names "mute": every
        # case implicitly assumes unmuted unless it says otherwise, and a
        # prior case leaving the device muted (e.g. G-muted) must not bleed
        # into the next one. Restoring only at the very end of the whole run
        # was tried first and is exactly the bug this replaced.
        if not await self.ensure_mute(bool(pre.get("mute", False))):
            return CaseOutcome(cid, CaseOutcome.SKIP, "could not confirm device mute state")

        kind = case.get("kind", "turn")
        expect = case.get("expect", {})

        if kind == "upload_reject":
            payload = b"" if case["payload"] == "empty" else b"\x00" * OVERSIZED_UPLOAD_BYTES
            status, body = await self.upload_raw(payload)
            return self._check_reject(case, status, body)

        if kind in ("reject", "busy") or "fixture" in case:
            fixture_path = self.fixtures / case["fixture"]
            if not fixture_path.is_file():
                return CaseOutcome(cid, CaseOutcome.SKIP,
                                    f"missing fixture {fixture_path.name} — run generate_fixtures.py")

        if kind == "reject":
            status, body = await self.upload_file(fixture_path)
            if status != 201:
                return CaseOutcome(cid, CaseOutcome.FAIL, f"upload unexpectedly failed: {status} {body}")
            # A leftover continuation from an EARLIER case (see
            # trigger_with_retry's note) can make the device answer
            # test_turn_busy here for a reason that has nothing to do with
            # what THIS case is testing (e.g. device_muted) — retry past
            # that specific code so the assertion below checks the real
            # rejection reason, not stale unrelated busy-ness.
            deadline = time.monotonic() + TURN_POLL_TIMEOUT_S
            status, body = await self.trigger()
            while (status == 409 and body.get("code") == "test_turn_busy"
                   and case["expect"].get("error_code") != "test_turn_busy"
                   and time.monotonic() < deadline):
                await asyncio.sleep(TURN_POLL_INTERVAL_S)
                status, body = await self.trigger()
            return self._check_reject(case, status, body)

        if kind == "busy":
            status, body = await self.upload_file(fixture_path)
            if status != 201:
                return CaseOutcome(cid, CaseOutcome.FAIL, f"upload unexpectedly failed: {status} {body}")
            watermark = await self.latest_turn_id()
            status1, body1 = await self.trigger_with_retry()
            if status1 != 202:
                return CaseOutcome(cid, CaseOutcome.FAIL, f"first trigger unexpectedly failed: {status1} {body1}")
            status2, body2 = await self.trigger()
            result = self._check_reject(case, status2, body2)
            # Let the long first turn finish before the next case starts,
            # or its trailing turn/playback would bleed into it.
            await self.wait_for_turn(watermark)
            return result

        # kind == "turn"
        for i, _ in enumerate(case.get("setup_phrases", [])):
            err = await self.run_setup_phrase(cid, i)
            if err:
                return CaseOutcome(cid, CaseOutcome.SKIP, f"setup phrase {i}: {err}")

        status, body = await self.upload_file(fixture_path)
        if status != 201:
            return CaseOutcome(cid, CaseOutcome.FAIL, f"upload failed: {status} {body}")
        watermark = await self.latest_turn_id()
        status, body = await self.trigger_with_retry()
        if status != 202:
            return CaseOutcome(cid, CaseOutcome.FAIL, f"trigger failed: {status} {body}")

        turn = await self.wait_for_turn(watermark)
        if turn is None:
            return CaseOutcome(cid, CaseOutcome.FAIL, "turn never reached a terminal outcome (timeout)")

        continuation = None
        if "continuation_expected" in expect:
            continuation = await self.saw_continuation(turn.get("turn_id", watermark))

        return self._check_turn(case, turn, continuation)

    # -- assertions -------------------------------------------------------

    @staticmethod
    def _check_reject(case: dict, status: int, body: dict) -> CaseOutcome:
        cid, expect = case["id"], case["expect"]
        want_status = expect.get("status")
        want_code = expect.get("error_code")
        got_code = body.get("code")
        if status != want_status or (want_code and got_code != want_code):
            return CaseOutcome(cid, CaseOutcome.FAIL,
                                f"expected {want_status}/{want_code}, got {status}/{got_code}")
        return CaseOutcome(cid, CaseOutcome.PASS, f"{status} {got_code}")

    @staticmethod
    def _check_turn(case: dict, turn: dict, continuation: dict | None) -> CaseOutcome:
        cid, expect = case["id"], case["expect"]
        problems = []

        outcome_in = expect.get("outcome_in")
        if outcome_in and turn.get("outcome") not in outcome_in:
            problems.append(f"outcome={turn.get('outcome')!r} not in {outcome_in}")

        stt = (turn.get("stt_text") or "").lower()
        for needle in expect.get("stt_contains", []):
            if needle.lower() not in stt:
                problems.append(f"stt_text missing {needle!r} (got {turn.get('stt_text')!r})")

        tts = turn.get("tts_text") or ""
        for needle in expect.get("tts_contains", []):
            if needle.lower() not in tts.lower():
                problems.append(f"tts_text missing {needle!r} (got {tts!r})")

        if "min_tts_chars" in expect and len(tts) < expect["min_tts_chars"]:
            problems.append(f"tts_text too short ({len(tts)} < {expect['min_tts_chars']}): {tts!r}")

        if "continuation_expected" in expect:
            want = expect["continuation_expected"]
            got = continuation is not None
            if want and not got:
                problems.append("expected a continuation turn, none was raised")
            elif not want and got:
                problems.append(f"unexpected continuation turn raised: {continuation}")

        detail = f"outcome={turn.get('outcome')} stt={turn.get('stt_text')!r} tts={tts[:80]!r}"
        if continuation is not None:
            detail += f" continuation.outcome={continuation.get('outcome')}"
        if problems:
            return CaseOutcome(cid, CaseOutcome.FAIL, "; ".join(problems) + " | " + detail, turn)
        return CaseOutcome(cid, CaseOutcome.PASS, detail, turn)


# ─── driver ─────────────────────────────────────────────────────────────────

def load_manifest(path: Path) -> list[dict]:
    return json.loads(path.read_text())["cases"]


def select_cases(cases: list[dict], args) -> list[dict]:
    if args.case:
        wanted = set(args.case)
        selected = [c for c in cases if c["id"] in wanted]
        missing = wanted - {c["id"] for c in selected}
        if missing:
            sys.exit(f"unknown case id(s): {', '.join(sorted(missing))}")
        return selected
    if args.category:
        wanted = set(args.category.split(","))
        return [c for c in cases if c["category"] in wanted]
    return cases  # --all


async def async_main(args) -> int:
    manifest_path = args.manifest
    fixtures_dir = args.fixtures_dir or manifest_path.parent
    cases = load_manifest(manifest_path)
    # Filtered before --list branches off too, so `--list --category timers`
    # narrows the same way running it would, rather than always dumping the
    # whole catalog regardless of --case/--category.
    selected = select_cases(cases, args)

    if args.list:
        for c in selected:
            print(f"{c['id']:<32} {c['category']:<8} {c.get('note', '')}")
        return 0

    if not selected:
        sys.exit("no cases selected — use --case, --category, or --all")

    db = resolve_db(args.db)
    tok = make_token(db)
    results: list[CaseOutcome] = []
    original_mute = None
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {tok}"}) as session:
            runner = Runner(session, args.controller, args.serial, fixtures_dir)

            dev = await runner.device()
            if not dev.get("approved"):
                sys.exit(f"device {args.serial} is not approved")
            if not dev.get("connected"):
                sys.exit(f"device {args.serial} is not connected")
            if "test_audio" not in (dev.get("capabilities") or []):
                sys.exit(f"device {args.serial} firmware does not advertise test_audio")
            original_mute = bool(dev.get("muted"))

            for case in selected:
                result = await runner.run_case(case)
                results.append(result)
                print(f"[{result.status:<4}] {case['id']:<32} {result.detail}")
                # Settle time on real hardware, not just courtesy. A short
                # pause is cheap next to real per-case turn latency (seconds)
                # and reduces the chance of one case's tail (e.g. a device
                # still unwinding a long test_audio file) overlapping the
                # next one's start. It is NOT a full fix for the
                # multi-clause-phrase flake documented on T1/T3/T4 in the
                # manifest — that reproduced even with this delay in place
                # and traces to Gemini's live VAD, not device sequencing.
                await asyncio.sleep(SETTLE_DELAY_S)

            if original_mute is not None:
                await runner.ensure_mute(original_mute)
            # Hygiene only: the next real test_audio upload would overwrite
            # this device's stored WAV anyway, but leaving a stale one
            # sitting on the controller between runs is needless.
            await runner.delete(f"/api/devices/{args.serial}/test_audio")
    finally:
        drop_token(db, tok)

    passed = sum(1 for r in results if r.status == CaseOutcome.PASS)
    failed = sum(1 for r in results if r.status == CaseOutcome.FAIL)
    skipped = sum(1 for r in results if r.status == CaseOutcome.SKIP)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped ({len(results)} total)")

    if args.results_out:
        args.results_out.write_text(json.dumps({
            "ts": time.time(),
            "device_id": args.serial,
            "firmware_ver": dev.get("firmware_ver"),
            "results": [r.as_dict() for r in results],
        }, indent=2))
        print(f"results written to {args.results_out}")

    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--serial", required=True, help="device_id / ro.serialno")
    ap.add_argument("--controller", default="http://127.0.0.1:8768")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--fixtures-dir", type=Path, default=None)
    ap.add_argument("--db", default=None, help="override EM_DB resolution")
    ap.add_argument("--case", action="append", help="run only this case id (repeatable)")
    ap.add_argument("--category", help="comma-separated category filter, e.g. normal,timers")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true", help="list cases and exit")
    ap.add_argument("--results-out", type=Path, default=None)
    args = ap.parse_args()

    if not (args.list or args.case or args.category or args.all):
        ap.error("one of --list, --case, --category, or --all is required")

    sys.exit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
