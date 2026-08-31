"""
VoiceAssistantAnnounceFinished is HA's completion signal, and HA BLOCKS on it.

`assist_satellite.entity.async_internal_announce` documents `async_announce`
as "should block until the announcement is done playing", holds
`_is_announcing` and the RESPONDING state for its duration, and raises
`SatelliteBusyError` if another announcement arrives meanwhile. The old
ESPHome-impersonation satellite (`em_esphome.py`, deleted — docs/design/full-duplex-plan.md
Phase 4) implemented that by awaiting our reply
(`send_voice_assistant_announcement_await_response`); it used to answer
synchronously in the message handler, before a byte had played, so the
`assist_satellite.announce` service returned early and two chained
announcements overlapped on the device instead of queueing behind HA's own
guard.

Both directions are pinned here, because they pull against each other and the
code reads fine either way: the reply must not come early, and it must always
come.

The sequencing lives in `em_announce` rather than the satellite for the reason
`em_linkauth.decide` is split out: the suite cannot import heavy modules
(zeroconf, aiohttp, the database). Async tests run through `asyncio.run()`,
the idiom the rest of the suite uses; pytest-asyncio is not in the test
environment and this is not worth adding it for.

`em_announce.run`/`play_media` have no live caller today: the new turn
engine's own announcement path
(`em_turn_engine.create_turn(kind="announcement")`) reimplements the
never-reply-early / always-reply / bounded-timeout properties inline rather
than calling into this module — see `em_turn_engine.py`'s `_run_turn`
(bounded by `em_announce.ANNOUNCE_TIMEOUT_S`, the one live import that keeps
this module from being fully orphaned) and `_outcome_for`. These tests stay
because they are the specification those properties still have to meet, not
because the code path under test runs in production — the "wiring pinned
against the source" section below, which pinned exactly how the now-deleted
ESPHome satellite called into this module, does not survive that; only the
sequencing tests do.
"""

import asyncio

import em_announce


def fetch_returning(pcm):
    async def _fetch(url):
        return pcm

    return _fetch


def fetch_raising(exc):
    async def _fetch(url):
        raise exc

    return _fetch


async def play_nothing(pcm):
    return None


class Replies:
    """Records what was reported to HA, and when."""

    def __init__(self):
        self.calls = []

    def __call__(self, ok):
        self.calls.append(ok)


# ── The reply lands after playback, not before ───────────────────────────────


def test_the_reply_waits_for_playback_to_finish():
    """
    The whole point. While the audio is playing HA must still be blocked, so a
    second announcement queues rather than talking over the first.
    """
    replies = Replies()
    playing = asyncio.Event()
    release = asyncio.Event()

    async def slow_play(pcm):
        playing.set()
        await release.wait()

    async def main():
        task = asyncio.create_task(
            em_announce.run(
                "http://ha/x.flac",
                fetch=fetch_returning(b"\x00\x00" * 100),
                play=slow_play,
                on_finished=replies,
            )
        )
        await playing.wait()
        during = list(replies.calls)
        release.set()
        await task
        return during

    during = asyncio.run(main())
    assert during == [], "reported finished while the audio was still playing"
    assert replies.calls == [True]


def test_success_is_reported_when_the_audio_reached_the_speaker():
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=play_nothing,
            on_finished=replies,
        )
    )
    assert replies.calls == [True]


# ── The reply always comes ───────────────────────────────────────────────────


def test_a_fetch_failure_still_replies():
    """
    Not replying parks HA for five minutes holding _is_announcing, after which
    every announcement fails SatelliteBusyError. success=False is strictly
    better than silence.
    """
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_raising(RuntimeError("ha unreachable")),
            play=play_nothing,
            on_finished=replies,
        )
    )
    assert replies.calls == [False]


def test_a_failing_playback_still_replies():
    replies = Replies()

    async def boom(pcm):
        raise RuntimeError("device gone")

    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=boom,
            on_finished=replies,
        )
    )
    assert replies.calls == [False]


def test_an_empty_media_id_still_replies():
    replies = Replies()
    asyncio.run(
        em_announce.run("", fetch=fetch_returning(b""), play=play_nothing, on_finished=replies)
    )
    assert replies.calls == [False]


def test_no_playback_callback_is_not_a_success():
    """
    Audio fetched but nothing to play it on — the physical device is not
    connected. HA should not be told the announcement happened.
    """
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=None,
            on_finished=replies,
        )
    )
    assert replies.calls == [False]


def test_empty_audio_is_not_a_success():
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b""),
            play=play_nothing,
            on_finished=replies,
        )
    )
    assert replies.calls == [False]


def test_a_wedged_playback_gives_up_and_replies():
    """
    Our cap has to fire before HA's, or HA is the one left holding the
    announcement. The layer below is already bounded; this is the guard for
    when it isn't.
    """
    replies = Replies()

    async def wedged(pcm):
        await asyncio.sleep(30)

    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=wedged,
            on_finished=replies,
            timeout=0.05,
        )
    )
    assert replies.calls == [False]


def test_exactly_one_reply_per_announcement():
    """
    A second AnnounceFinished has no run to belong to — HA pairs it with
    whatever it is waiting for next.
    """
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=play_nothing,
            on_finished=replies,
        )
    )
    assert len(replies.calls) == 1


def test_a_play_callback_reporting_failure_is_not_a_success():
    """
    The device can cancel mid-playback — a mute, a button press — and then the
    user did not hear the announcement. Reporting success for it is untrue, and
    success is the one fact this reply carries.
    """
    replies = Replies()

    async def cancelled(pcm):
        return False

    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=cancelled,
            on_finished=replies,
        )
    )
    assert replies.calls == [False]


def test_a_play_callback_with_no_opinion_counts_as_played():
    """
    Most callbacks return None. Treating that as failure would report every
    ordinary announcement as failed.
    """
    replies = Replies()
    asyncio.run(
        em_announce.run(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=play_nothing,
            on_finished=replies,
        )
    )
    assert replies.calls == [True]


# ── the other way HA announces ───────────────────────────────────────────────


def test_play_media_announce_plays_without_replying():
    """
    HA has TWO announce paths and only one waits for a completion message.
    `play_media` with announce=true is an ordinary media_player command;
    sending AnnounceFinished for it answers a question nobody asked.
    """
    played = []

    async def play(pcm):
        played.append(len(pcm))

    ok = asyncio.run(
        em_announce.play_media(
            "http://ha/x.flac",
            fetch=fetch_returning(b"\x00\x00" * 100),
            play=play,
        )
    )
    assert ok is True
    assert played == [200]


def test_our_cap_sits_under_has():
    """
    HA's _ANNOUNCEMENT_TIMEOUT_SEC is 5 minutes. Ours must be comfortably
    below it — the point of a cap here is to be the side that gives up first.
    """
    assert em_announce.ANNOUNCE_TIMEOUT_S < 300

# The "wiring, pinned against the source" section that used to follow this
# point checked em_esphome.EchoMuseSatellite.handle_message's exact dispatch
# shape (AnnounceFinished not answered synchronously, ANNOUNCING sent
# synchronously, both announce paths sharing one callback resolver, no
# self._method() call dispatching to a method that doesn't exist). None of
# it has an equivalent in the HACS assist_satellite.py that replaced it —
# there is no handle_message dispatch table anymore, HA's own
# AssistSatelliteEntity owns that shape. The properties that still matter
# (announce completion timing, callback resolution) are exercised directly
# against the real class in hacs/tests/test_assist_satellite.py, which is a
# stronger guarantee than static source scanning: a typo'd self._method()
# call fails the test by raising, rather than by a missing string.
