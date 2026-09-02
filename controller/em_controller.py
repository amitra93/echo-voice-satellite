"""
EchoMuse Controller
===================

WebSocket server. Echo Dot devices connect via mDNS discovery.

mDNS advertisement is handled internally — no separate container required.

Architecture:
- Advertise _emcontroller._tcp on SERVER_PORT (zeroconf, host network)
- Devices open THREE connections:
    /control — JSON control plane (buttons, LEDs, mic_start/stop, ping,
                                   register, config, log, pending)
    /data    — binary data plane (mic PCM frames in, speaker PCM frames out)
    /shell   — raw binary stdin/stdout (demand-opened for shell sessions
                                        and OTA binary transfer)
- HTTP API and dashboard SPA served by aiohttp on API_PORT

Device WebSocket protocol:
  /control — Device → Server:
    {"type": "register", "device_id": "G0K0XXXXXXXX", "ip": "...",
     "version": "v2.0.1", "capabilities": [...]}
    {"type": "button", "clickType": 138, "down": false}
    {"type": "log", "level": "info", "message": "..."}
    {"type": "playback_stats", "periods": 123, "underruns": 0}
    {"type": "pong"}

  /control — Server → Device:
    {"type": "ack",     "device_id": "..."}
    {"type": "pending"}
    {"type": "config",  "adcDigitalGain": 100, ...}
    {"type": "leds",    "leds": [...]}
    {"type": "mic_start"}
    {"type": "mic_stop"}
    {"type": "ping"}

  /data — Device → Server:
    <binary> [0x01][seq_hi][seq_lo][PCM mono S16_LE 2560 bytes]

  /data — Server → Device:
    <binary> [0x02][PCM mono S16_LE 48kHz — 4096 bytes per period]
    <binary> [0x03] end of audio stream

  /shell — bidirectional raw binary (demand-opened by device on
           receipt of shell_open control message — not yet implemented
           in this revision; shell connections come inbound from the
           Go binary to the controller's /shell/{device_id} path)
"""

import asyncio
import collections
import contextlib
import json
import logging
import os
import socket
import struct
import time

from aiohttp import web
from zeroconf.asyncio import AsyncZeroconf
from zeroconf import ServiceInfo
import websockets
from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

import em_db as db
import em_auth as auth
import em_api as api
import em_pki
import em_hostip
import em_linkauth
import em_eq
import em_limiter
import em_mbc
import em_scenes
import em_stop
import em_arbiter
import em_button
import em_tap_burst
import em_turn_engine as turn_engine
import em_ha_sidechannels as ha_sidechannels
import em_oww_models
import em_training_captures
import em_capture_upload
import em_player
import em_volume
import em_clock

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"

# Any non-empty string is truthy in Python, so `os.environ.get("DEBUG")`
# alone turned debug logging ON for DEBUG=0 — and em_start.py renders a
# false add-on option as exactly that string, so an untouched "Debug
# logging" toggle would have shipped every add-on install at DEBUG level.
# Same `== "1"` convention as REQUIRE_DEVICE_TLS, widened to the word
# spellings because DEBUG went undocumented for long enough that a
# container user's .env may already say `true`.
DEBUG = os.environ.get("DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format=_LOG_FORMAT,
)
log = logging.getLogger("echomuse")

# Keep the last few hundred lines in memory so a support bundle can carry the
# controller's own log, not just the relayed per-device one.
api.install_log_ring(_LOG_FORMAT)

logging.getLogger("websockets.server").setLevel(logging.CRITICAL)


def _log_task_exception(task: asyncio.Task) -> None:
    """
    Standard done-callback for fire-and-forget asyncio.create_task() calls.

    Without this, an exception raised inside a task nobody awaits vanishes
    silently — asyncio only surfaces it via a "Task exception was never
    retrieved" warning at garbage-collection time, easy to miss in normal
    logs. Attach via task.add_done_callback(_log_task_exception) at every
    fire-and-forget create_task() call site (see M1 in the 2026-07-05
    review — currently applied to the button-triggered voice turn task).
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(f"Unhandled exception in background task {task.get_name()}: {exc}", exc_info=exc)


# ─── Config ───────────────────────────────────────────────────────────────────

SERVER_HOST  = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT  = int(os.environ.get("SERVER_PORT", "8767"))
# Device-link TLS listener (wss) — same three WS planes as SERVER_PORT,
# wrapped in TLS with the em_pki-generated cert. 0 disables. Devices pick
# it up from the tls_port mDNS TXT property and dial wss iff they hold the
# pushed CA file (see device/internal/client/tlscreds.go).
SERVER_TLS_PORT = int(os.environ.get("SERVER_TLS_PORT", "8770"))
# Enforcing posture: reject device connections that are not TLS + a valid
# per-device token. Leave 0 until the whole fleet shows tls=true —
# a plain, tokenless connection is the legacy default and must keep
# working during the rollout.
REQUIRE_DEVICE_TLS = os.environ.get("REQUIRE_DEVICE_TLS", "0") == "1"
API_PORT     = int(os.environ.get("API_PORT", "8768"))
# The address devices are told to dial. Detected from the routing table when
# unset — never a literal, which used to send every unconfigured deployment
# to a developer's own machine. See em_hostip.
SERVER_IP    = em_hostip.server_ip(os.environ.get("SERVER_IP"))
MDNS_NAME    = os.environ.get("MDNS_NAME", "echomuse")
DB_PATH      = os.environ.get("DB_PATH", "echomuse.db")
MUSIC_ASSISTANT_URL = api.normalize_music_assistant_url(
    os.environ.get("MUSIC_ASSISTANT_URL", "")
)

# Device approval mode — overridden by system_config after db.init()
DEVICE_APPROVAL = os.environ.get("DEVICE_APPROVAL", "strict")

# Speaker — must match PcmSpeaker constants in Go. The wire carries MONO
# 48kHz (the device duplicates to stereo at the ALSA write — shipping two
# identical channels to a mono speaker doubled TTS bandwidth for nothing,
# and halving it matters on marginal 2.4GHz links).
SPEAKER_RATE   = 48000
SPEAKER_PERIOD = 2048
SPEAKER_BYTES  = SPEAKER_PERIOD * 2       # 4096 bytes/period (mono S16)

# The device holds playback until ~this much audio is buffered (or EOS
# arrives) — primePeriods in pcm_speaker.go. The post-playback drain sleep
# must allow for the delayed start.
SPEAKER_PRIME_SECONDS = 1.1

# Control-plane RTT probing. 5s rather than the old 30s keepalive cadence:
# characterising jitter needs samples, and one tiny JSON message per device
# per 5s is negligible next to the 256 kbps continuous mic upload each
# device already holds open. Samples are aggregated in memory and flushed on
# the existing ~30s stats report, so the DB cost is unchanged.
PING_INTERVAL_SEC = 5.0
# A sample at or above this counts as an excursion. 200ms is well clear of a
# healthy hop (Office measures 264ms median for a whole audio round trip
# including frame batching) while catching the ~1s tail under investigation.
RTT_EXCURSION_MS = 200

# Outstanding pings older than this are abandoned — a reply that late is not
# a latency measurement, it is a lost packet, and keeping them would grow
# ping_sent without bound across a long disconnect.
PING_TIMEOUT_SEC = 60.0

# LEDs
NUM_LEDS = 12

# Wake detector defaults are device configuration, not controller environment.
DEFAULT_WAKE_MODEL = "hey_jarvis_v0.1"
DEFAULT_WAKE_THRESHOLD = 0.5

# mDNS re-registration interval — keeps IGMP membership alive on the LAN
MDNS_REFRESH_INTERVAL = 120

# Binary frame types
MIC_FRAME_TYPE     = 0x01
VAD_END_TYPE       = 0x04
# Distinct from VAD_END_TYPE — device never detected speech at all within its
# local no-speech grace period (see device/internal/client/data.go
# noSpeechTimeout), as opposed to VAD_END_TYPE which means speech was
# detected and then ended normally. Each frame type queues its matching
# string sentinel (turn_engine.VAD_SENTINEL_END / VAD_SENTINEL_TIMEOUT) so the
# type travels with the queue item — B5 fix, 2026-07-07; the old None +
# device.last_vad_was_timeout side-channel let a second sentinel overwrite
# the first's flag before it was consumed. The turn engine's _send_mic
# differentiates the two outcomes.
VAD_NO_SPEECH_TIMEOUT_TYPE = 0x05
SPEAKER_FRAME_TYPE = 0x02
SPEAKER_EOS_TYPE   = 0x03
MIC_HEADER_LEN     = 3   # [type][seq_hi][seq_lo]

# Volume conversion lives in em_volume so the scale has ONE definition and a
# test — it used to be spelled `/ 175` in three separate modules.
VOLUME_MAX_DEVICE = em_volume.DEVICE_VOLUME_MAX
_device_level_to_ha = em_volume.device_level_to_ha
_ha_volume_to_device = em_volume.ha_volume_to_device

# ─── Device registry ──────────────────────────────────────────────────────────

# How long a speaker stream will wait, IN TOTAL, for a dropped data connection
# to come back before giving up on the rest of it.
#
# A brief Wi-Fi blip mid-stream used to truncate the audio outright: send_data
# saw data_ws was None, logged, and dropped every remaining frame, so a
# reconnect a second later arrived to find the audio already thrown away
# (reported by @kopiro in #28, on long read-aloud responses).
#
# The budget is per STREAM, not per frame, and that distinction is the whole
# design. send_data is called once per audio period; a per-frame wait means a
# device that is genuinely gone stalls every remaining frame in turn, so a
# stream that should abort in seconds instead drains for hours holding the
# voice lock. Spending one shared budget across the stream rides out a blip
# and still fails fast on a real disconnect.
#
# 3s because it is covering a reconnect, not an outage: measured RTT
# excursions on this fleet peak around 1.7s, and the device's own buffer holds
# ~5.5s, so a blip inside this window is inaudible.
DATA_RECONNECT_GRACE_S = 3.0

class Device:
    def __init__(
        self,
        device_id: str,
        ip: str,
        capabilities: list,
        control_ws: WebSocketServerProtocol,
    ):
        self.device_id    = device_id
        self.ip           = ip
        self.capabilities = capabilities
        self.control_ws   = control_ws
        # Set from the register message; None on firmware that predates it.
        self.ambient_light_status: dict | None = None

        self.data_ws: WebSocketServerProtocol | None = None
        # Remaining reconnect grace for the speaker stream in flight. Armed by
        # begin_data_stream(); spent down by send_data so the whole stream
        # shares one budget rather than each frame having its own.
        self._data_grace_left: float = 0.0
        self.voice_lock   = asyncio.Lock()
        self.cancel_event = asyncio.Event()
        self.voice_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self.oww_paused   = asyncio.Event()  # set during voice turn
        self.oww_paused_since: float | None = None

        # Transient state — read by em_api._merge_device()
        self.speaking  = False
        self.muted     = False
        self.listening = False
        self.thinking  = False
        self.timer_firing = False
        self.timer_alarm_audio_ready = False
        self.timer_alarm_listen_after = 0.0
        self.timer_stop_turn_id: int | None = None

        # Volume as HA float (0.0–1.0). Initialised from stored config in
        # handle_control() after config is read; updated on volume_state
        # messages from the device and persisted back to config.
        # Default matches DEFAULT_DEVICE_CONFIG startupVolume=85.
        self.volume: float = _device_level_to_ha(85)

        self.data_ready = asyncio.Event()

        # Device-reported wake detector configuration and readiness.
        self.oww_model:     str   = DEFAULT_WAKE_MODEL
        self.stop_model:    str   = "stop"
        self.stop_threshold: float = 0.75
        # Updated by stop runtime reports. False is an explicit mandatory
        # readiness failure; unknown starts false so output cannot claim safety.
        self.stop_model_ready: bool = False
        self.stop_state: em_stop.StopState = em_stop.StopState()
        # Seeded from wall clock, not 0: the device's own stopword.Manager
        # generation counter is long-lived across control-plane reconnects
        # (it lives on DataClient, tied to the process, not the connection),
        # but this Device object — and this counter — is recreated from
        # scratch every time the CONTROLLER restarts. Starting at 0 meant a
        # freshly restarted controller sent generation 1, 2, 3... while the
        # device still remembered a much higher generation from before the
        # restart, and the device's monotonic check (generation > its last
        # seen) rejected every arm as "invalid arm" until the controller's
        # counter organically climbed back past whatever the device
        # remembered — silently disabling the local "stop" word AND
        # (because the wake and stop heads share one scorer) muddying
        # observability for wake-word barge-in, for however many turns that
        # took. Milliseconds-since-epoch is billions of turns of headroom
        # above the +1-or+2-per-turn pace this actually advances at, so a
        # collision with a real device's remembered generation cannot happen
        # in practice, and it is monotonic across repeated controller
        # restarts within a process's lifetime the same way epoch time
        # always is.
        self.stop_generation: int = int(time.time() * 1000)
        # Multi-device wake arbitration window (ms, 0 = off). Only
        # consulted when 2+ devices are connected — a solo fleet never
        # pays the latency.
        self.wake_arb_ms:   int   = 300
        # saveUtterances: keep this turn's ASR-bound mic audio and write it
        # to recordings/ at turn end (em_recordings). Read per turn, so
        # switching it off stops the next turn being captured, not the one
        # already streaming.
        self.save_utterances: bool = False
        # This turn's captured mic audio, handed from _stream_mic_audio to
        # _persist_turn (which owns the write — it has the rowid the
        # filename is keyed on) and consumed there.
        self.last_utterance_pcm: bytes | None = None
        # saveWakeCaptures controls device-selected wake capture uploads.
        self.save_wake_captures: bool = False
        # saveStopCaptures controls device-selected post-AFE stop capture uploads.
        self.save_stop_captures: bool = False
        self.eq_bands:      list  = [0.0] * 8
        self.eq_loudness:   bool  = False
        self.bass_guard_enabled: bool  = True
        self.bass_guard_db:      float = em_mbc.DEFAULT_BASS_GUARD_DB
        self.limiter_enabled:   bool  = True
        self.limiter_release:   float = em_limiter.DEFAULT_RELEASE_MS
        # LED ring scene — render-ready palette/spinner from em_scenes,
        # refreshed on connect and on any config push carrying led* keys.
        self.led_scene:     dict  = em_scenes.resolve({})
        self.stats:         dict | None = None
        # In-flight wifi_scan awaiter (set by the API handler). Change
        # pending/result state lives in api._wifi_states instead — this
        # Device object dies with the connection when the network switches.
        self.wifi_scan_future: asyncio.Future | None = None
        # A matching wake_status from the device is the sole readiness authority.
        self.oww_model_ready: bool = False
        self.oww_classifier_md5: str | None = None
        # One provisional device-originated wake may be waiting for HA to accept.
        self.wake_request_id: str | None = None
        self.wake_admission_task: asyncio.Task | None = None

        # Barge-in (§3.2): the device's local wake scorer submits a
        # wake_request during thinking or TTS playback. The controller grants
        # it, cancels the exact HA turn, and the turn loop re-enters fresh.
        self.barge_in_enabled = False
        # An admitted device-originated barge suppresses the terminal LED cue
        # while its replacement turn repaints the listening animation.
        self.barge_detected = False
        # False until config is pushed, so a device connecting before then
        # keeps the historical tap-starts-a-turn behaviour.
        self.button_single_tap_event = False
        self.button_multi_tap_ms = 0
        self.tap_burst = em_tap_burst.TapCoalescer(
            lambda name: ha_sidechannels.button_event(self.device_id, name),
            enabled=lambda: self.button_single_tap_event,
            on_error=_log_task_exception,
        )
        self.barge_request_id: str | None = None
        # Controller-only TTS makeup gain. Applied before the output limiter,
        # never to music, so it improves speech intelligibility/loudness
        # without changing the user's media volume.
        self.tts_gain_db = 0.0

        # Recent voice-turn traces (turn_record-shaped dicts, appended by
        # em_turn_engine._remember_turn at turn completion) — powers the
        # Status tab's observability panel.
        # Hydrated from the persistent turns table on connect (handle_control),
        # appended live; bounded.
        self.turn_history: collections.deque = collections.deque(maxlen=50)

        # Wake detection detail for the turn about to start. Device wake
        # admission sets it; button and continuation turns leave it empty.
        self.last_wake: dict | None = None

        # Playback stats rendezvous. The device reports playback_stats when
        # its buffer drains, the controller persists the turn when its
        # (deliberately overestimated) drain sleep ends — either can happen
        # first. last_turn_id covers stats-after-persist: set at persist for
        # turns that played audio, consumed by handle_control (cleared on
        # use so an announcement's report can't overwrite a turn's stats).
        # pending_playback_stats covers stats-before-persist: (ts, periods,
        # underruns) stashed by handle_control. em_esphome._persist_turn used
        # to fold this into the turn record if fresh (staleness window kept a
        # long-ago announcement's stats out of an unrelated later turn) — the
        # turn engine does not yet do this fold-in; see em_turn_engine.py's
        # module docstring for the tracked gap.
        self.last_turn_id: int | None = None
        self.pending_playback_stats: tuple | None = None

        # Controller-side playback timing (v7 instrumentation).
        # playback_send_t0 is set when the first 0x02 of a response goes
        # out and consumed when the device's playback_stats lands — the
        # difference is the true delivery window, as opposed to
        # playback_send_ms, which only times writing into the socket and
        # completes almost instantly however slow the link is.
        self.playback_send_t0: float | None = None
        self.playback_send_ms: int = -1
        self.playback_eq_ms: int = -1
        # Set when the device reports playback_stats for the stream being
        # played. This is the authoritative "the audio has finished" signal
        # — the device emits it once its audio channel has drained after
        # EOS, i.e. when the last period has gone to ALSA. Cleared at the
        # start of every speaker stream; awaited by _run_post_turn_playback
        # in place of the wall-clock estimate that used to clear the ring
        # while the device was still playing (up to 6.1s early, 2026-07-24).
        self.playback_done = asyncio.Event()
        # Exclusive ownership of the speaker plane, including device drain.
        self.speaker_lock = asyncio.Lock()
        # Outcome of the most recently persisted turn, set by em_esphome and
        # consumed once by the turn loop's ring cleanup (see _leds_turn_end).
        self.last_turn_outcome: str | None = None

        # ── Control-plane RTT ────────────────────────────────────────────
        # End-to-end latency is the one thing the RF layer cannot tell us on
        # this hardware: the MTK driver leaves retry/discard/missed-beacon
        # at zero in /proc/net/wireless and reports NOISE=9999, and there is
        # no `iw` binary. So the tx/rx/error counters cannot distinguish a
        # healthy link from a struggling one — while RTT measures the thing
        # that actually degrades the experience, needs no driver support,
        # and discriminates between the live hypotheses: contention makes
        # latency track LOAD, whereas WiFi power-save makes it spike when
        # IDLE, quantised to the beacon interval.
        #
        # Accumulated in memory and flushed on the device's existing ~30s
        # stats report, so this costs no write the loop wasn't making.
        self.ping_seq   = 0
        self.ping_sent: dict[int, float] = {}   # seq -> monotonic send time
        self.ping_busy: dict[int, bool]  = {}   # seq -> device busy at send
        self.clock_probe_seq = 0
        self.clock_probe_sent: dict[int, int] = {}
        self.clock_probe_busy: dict[int, bool] = {}
        self.clock_sync = em_clock.ClockSync()
        self.rtt_last_ms: int | None = None
        self.rtt_sum_ms  = 0
        self.rtt_count   = 0
        self.rtt_min_ms: int | None = None
        self.rtt_max_ms: int | None = None
        self.rtt_excursions      = 0   # samples over RTT_EXCURSION_MS
        self.rtt_excursions_idle = 0   # ...of which the device was idle
        # Denominator for the above. Without it, "every excursion happened
        # while idle" is vacuous: almost every SAMPLE is idle, because
        # devices spend most of their life not in a turn. The discriminator
        # is the excursion RATE per state, not the raw count.
        self.rtt_samples_idle    = 0

    def is_busy(self) -> bool:
        """Whether this device was doing anything when a ping went out."""
        return bool(
            self.voice_lock.locked()
            or self.speaking
            or em_player.is_playing(self.device_id)
        )

    def record_rtt(self, rtt_ms: int, was_busy: bool) -> None:
        self.rtt_last_ms = rtt_ms
        self.rtt_sum_ms += rtt_ms
        self.rtt_count  += 1
        if not was_busy:
            self.rtt_samples_idle += 1
        if self.rtt_min_ms is None or rtt_ms < self.rtt_min_ms:
            self.rtt_min_ms = rtt_ms
        if self.rtt_max_ms is None or rtt_ms > self.rtt_max_ms:
            self.rtt_max_ms = rtt_ms
        if rtt_ms >= RTT_EXCURSION_MS:
            self.rtt_excursions += 1
            if not was_busy:
                self.rtt_excursions_idle += 1

    def drain_rtt(self) -> dict:
        """Take the accumulated window and reset. Empty dict if no samples."""
        if not self.rtt_count:
            return {}
        out = {
            "rttSumMs":         self.rtt_sum_ms,
            "rttSamples":       self.rtt_count,
            "rttMinMs":         self.rtt_min_ms,
            "rttMaxMs":         self.rtt_max_ms,
            "rttExcursions":    self.rtt_excursions,
            "rttExcursionsIdle": self.rtt_excursions_idle,
            "rttSamplesIdle":   self.rtt_samples_idle,
        }
        self.rtt_sum_ms = self.rtt_count = 0
        self.rtt_min_ms = self.rtt_max_ms = None
        self.rtt_excursions = self.rtt_excursions_idle = 0
        self.rtt_samples_idle = 0
        return out

    async def send_control(self, msg: dict):
        try:
            await self.control_ws.send(json.dumps(msg))
        except Exception as e:
            log.warning(f"[{self.device_id}] Control send failed: {e}")

    def begin_data_stream(self) -> None:
        """
        Arm the reconnect grace for one speaker stream.

        Called at the start of each stream so the budget is fresh, and so a
        stream that already spent it cannot borrow from the next one.
        """
        self._data_grace_left = DATA_RECONNECT_GRACE_S

    async def _await_data_reconnect(self, budget: float) -> float:
        """Wait up to `budget` seconds for the data plane. Returns time spent."""
        step = 0.1
        waited = 0.0
        while waited < budget:
            await asyncio.sleep(step)
            waited += step
            if self.data_ws is not None:
                log.info(f"[{self.device_id}] Data connection back after "
                         f"{waited:.1f}s — resuming stream")
                break
        return waited

    async def send_data(self, data: bytes):
        if self.data_ws is None and self._data_grace_left > 0:
            # Ride out a blip rather than discarding the rest of the audio.
            # The budget is spent down, so a device that never returns costs
            # the stream DATA_RECONNECT_GRACE_S once, not once per frame.
            self._data_grace_left -= await self._await_data_reconnect(
                self._data_grace_left)
        if self.data_ws is None:
            log.warning(f"[{self.device_id}] No data connection")
            return
        try:
            await self.data_ws.send(data)
        except Exception as e:
            log.warning(f"[{self.device_id}] Data send failed: {e}")

    async def set_leds(self, leds: list, listening: bool | None = None):
        # The optional listening flag tells the device explicitly that this
        # frame is the listening ring (enables its direction overlay).
        # Pre-scene firmware inferred it from the ring being all-green —
        # that heuristic breaks for every non-green scene, so newer
        # firmware trusts this flag when present and old firmware just
        # ignores the extra key.
        msg = {"type": "leds", "leds": leds}
        if listening is not None:
            msg["listening"] = listening
        await self.send_control(msg)

    @property
    def led_anim_capable(self) -> bool:
        return "led_anim" in (self.capabilities or [])

    @property
    def audio_mix_capable(self) -> bool:
        """
        Whether this firmware holds music on its own plane and mixes it with
        voice at the ALSA write.

        When it does, a voice turn DUCKS the music instead of pausing it —
        which is the only place ducking can happen. The music feed runs
        LEAD_S=4s ahead of realtime, so the next four seconds are already on
        the device when a wake word fires, and audio that has left here
        cannot be ducked from here. Without the capability the controller
        keeps the pause/resume path: a device that cannot mix would never
        play the 0x04 stream at all, which is silence rather than degraded
        behaviour.
        """
        return "audio_mix" in (self.capabilities or [])

    @property
    def sendspin_native_capable(self) -> bool:
        """Whether this device connects to Music Assistant directly."""
        return "sendspin_native" in (self.capabilities or [])

    @property
    def output_chain_capable(self) -> bool:
        """Whether firmware owns voice-output DSP for its speaker path."""
        return "output_chain" in (self.capabilities or [])

    @property
    def button_hold_capable(self) -> bool:
        """Measures hold time — and so was offered the HA event entity."""
        return "button_hold" in (self.capabilities or [])

    @property
    def wake_request_capable(self) -> bool:
        """Whether firmware implements mandatory local wake admission."""
        return "wake_request_v1" in (self.capabilities or [])

    @property
    def stopword_capable(self) -> bool:
        """Whether firmware can locally flush and report an armed stop word."""
        return "stopword" in (self.capabilities or [])

    async def send_led_anim(self, anim: dict):
        """
        Hand the ring to the device's local animation engine (led_anim
        capability, v2.9+ firmware). The device renders frames on its own
        ticker until a newer led_anim/leds message replaces the spec or
        its ttlSec dead-man expires — so a controller stall or WiFi jitter
        can no longer make the spinner judder, and a dead controller can't
        leave the ring lit.
        """
        await self.send_control({"type": "led_anim", "anim": anim})

    async def ping(self):
        await self.send_control({"type": "ping"})

    async def mic_start(self):
        await self.send_control({"type": "mic_start"})

    async def mic_start_turn(self):
        """Start mic for a voice turn — signals device to lock the best directional mic."""
        await self.send_control({"type": "mic_start", "lock_mic": True})

    async def mic_stop(self):
        await self.send_control({"type": "mic_stop"})

    async def beam_lock(self):
        # Lock the beamformer onto the speaker's perimeter mic mid-stream —
        # no stream restart. Device no-ops if already locked or if
        # beamformingEnabled is false in its config.
        await self.send_control({"type": "beam_lock"})

    async def beam_unlock(self):
        await self.send_control({"type": "beam_unlock"})

    async def push_config(self, **kwargs):
        await self.send_control({"type": "config", **kwargs})

    async def _set_speaking(self, value: bool) -> None:
        """
        Set the speaking flag AND tell the dashboard.

        The single writer, because the flag and the push had drifted apart:
        stream_speaker/stream_speaker_chunks set it, and nothing pushed the
        transition. _push_device_state has always carried `speaking` and the
        dashboard has always rendered it above `thinking` — but the only
        pushes were at listening, thinking and turn end, so a turn read
        listening -> thinking -> idle and **never showed Speaking at all**. It
        appeared only when the dashboard's 5s poll of /api/devices happened to
        land mid-playback, which for a typical ~2s response it usually did not.

        WHEN each edge fires, and how true each is:

        - **False is device truth.** The playback functions wait on the
          device's own `playback_stats`, sent once its audio channel drains
          after EOS, and clear the flag there.
        - **True is still a controller-side ESTIMATE** — the first period put
          on the wire. The device holds audio until roughly
          SPEAKER_PRIME_SECONDS is queued (primePeriods, pcm_speaker.go), so
          the tile leads the speaker by up to that much. Closing that gap needs
          the DEVICE to report the moment it starts, which no released firmware
          does; `playback_stats` is the only playback message it sends.

        Guarded rather than plain, because one caller is stream_speaker's
        finally, which is also reached when barge-in cancels the task
        mid-send: the flag assignment is synchronous and always happens, and a
        push that cannot complete is not worth failing a speaker stream over —
        turn end pushes the same state moments later.
        """
        if self.speaking == value:
            return
        self.speaking = value
        if value:
            # Mutually exclusive phases. Leaving `thinking` set meant the tile
            # FELL BACK to Thinking the moment speaking cleared, instead of
            # going quiet — which is what made an early clear look like the
            # device had started thinking again mid-response.
            self.thinking = False
        try:
            await _push_device_state(self)
        except BaseException:
            pass

    async def stream_speaker(self, pcm: bytes):
        """Stream resampled mono 48kHz PCM as 0x02 frames, then 0x03 EOS."""
        self.begin_data_stream()
        await self._set_speaking(True)
        try:
            offset = 0
            while offset < len(pcm):
                if self.cancel_event.is_set():
                    break
                chunk = pcm[offset:offset + SPEAKER_BYTES]
                if len(chunk) < SPEAKER_BYTES:
                    # Pad the final partial period with silence — without this,
                    # up to one full period (~42ms at 48kHz) of the last word is
                    # silently dropped because the old loop required a full period.
                    chunk = chunk + bytes(SPEAKER_BYTES - len(chunk))
                await self.send_data(bytes([SPEAKER_FRAME_TYPE]) + chunk)
                offset += SPEAKER_BYTES
        finally:
            # NOT where speaking clears — see _run_post_turn_playback. This
            # returns when the last byte is written to the socket, which
            # completes near-instantly however slow the link is; the device
            # still has its whole buffer to play.
            # EOS must go out on EVERY exit, including task cancellation
            # (barge-in cancels this task mid-send): the device's barge-in
            # flush discards 0x02 frames until it sees this stream's 0x03 —
            # a stream that ends without one would leave the discard armed
            # and swallow the next turn's audio. shield() lets the send
            # complete even though this task is mid-cancellation; the
            # original CancelledError still propagates after the finally.
            try:
                await asyncio.shield(self.send_data(bytes([SPEAKER_EOS_TYPE])))
            except BaseException:
                pass  # WS gone / re-cancelled — device flush self-heals on reconnect

    async def stream_speaker_chunks(self, pcm_chunks, stream_eq=None):
        """
        Stream an asynchronous PCM source as one device speaker session.

        StreamingEQ stays alive for the complete response so its biquad state
        crosses HTTP chunk boundaries without clicks. Devices with an
        output_chain receive the decoded PCM directly instead. Partial device
        periods are retained until more PCM arrives and padded only once, at
        the true end of the response.
        """
        self.begin_data_stream()
        pending = bytearray()
        total_pcm = 0
        eq_seconds = 0.0
        # Accumulated time spent WRITING to the socket, excluding time waiting
        # for the source to produce audio. send_ms is documented as socket-write
        # time that "completes near-instantly however slow the link is" — timing
        # the whole streaming loop instead would fold HA's synthesis time into
        # it and make it read like delivery, which is the misreading that cost
        # an investigation on 2026-07-20.
        send_seconds = 0.0
        first_send_time = None
        try:
            async for pcm in pcm_chunks:
                if self.cancel_event.is_set():
                    break
                total_pcm += len(pcm)
                if stream_eq is None:
                    pending.extend(pcm)
                else:
                    eq_started = asyncio.get_event_loop().time()
                    pending.extend(stream_eq.process(pcm))
                    eq_seconds += asyncio.get_event_loop().time() - eq_started

                while len(pending) >= SPEAKER_BYTES:
                    if self.cancel_event.is_set():
                        break
                    chunk = bytes(pending[:SPEAKER_BYTES])
                    del pending[:SPEAKER_BYTES]
                    if first_send_time is None:
                        first_send_time = asyncio.get_event_loop().time()
                        self.playback_send_t0 = first_send_time
                        await self._set_speaking(True)
                        log.info(
                            f"[{self.device_id}] First streamed PCM period "
                            "sent to device"
                        )
                    _t_send = asyncio.get_event_loop().time()
                    await self.send_data(bytes([SPEAKER_FRAME_TYPE]) + chunk)
                    send_seconds += asyncio.get_event_loop().time() - _t_send

            if pending and not self.cancel_event.is_set():
                chunk = bytes(pending)
                chunk += bytes(SPEAKER_BYTES - len(chunk))
                if first_send_time is None:
                    first_send_time = asyncio.get_event_loop().time()
                    self.playback_send_t0 = first_send_time
                    await self._set_speaking(True)
                    log.info(
                        f"[{self.device_id}] First streamed PCM period "
                        "sent to device"
                    )
                _t_send = asyncio.get_event_loop().time()
                await self.send_data(bytes([SPEAKER_FRAME_TYPE]) + chunk)
                send_seconds += asyncio.get_event_loop().time() - _t_send
        finally:
            # NOT where speaking clears — see the note in stream_speaker.
            # One EOS terminates the complete response. Sending EOS per HTTP
            # chunk would make the device repeatedly prime and flush.
            try:
                await asyncio.shield(self.send_data(bytes([SPEAKER_EOS_TYPE])))
            except BaseException:
                pass

        return total_pcm, int(eq_seconds * 1000), first_send_time, int(send_seconds * 1000)

    # Delimits the speaker-stream methods from module-level playback runners.
    # Those runners, not socket writers, clear speaking after playback_stats.
    def _speaker_stream_methods_end(self) -> None:
        pass


# The live device registry — keyed by device_id (ro.serialno).
# em_api receives a reference to this dict at startup.
_devices: dict[str, Device] = {}

# Peak event-loop lag observed since start, in ms (see
# event_loop_lag_monitor). Read by the API for /api/system/status.
_loop_lag_peak_ms: float = 0.0

# One arbiter for the fleet — elects a single responder when one
# utterance wakes several Echos (see em_arbiter.py).
_wake_arbiter = em_arbiter.WakeArbiter()

# Shell session coordination — keyed by device_id.
#
# _shell_pending:   Future resolved with the device ws when handle_shell receives it.
# _shell_dashboard: dashboard WebSocket set by em_api for interactive sessions.
_shell_pending:    dict[str, asyncio.Future] = {}
_shell_dashboard:  dict[str, object]         = {}


def get_device(device_id: str) -> Device | None:
    return _devices.get(device_id)


def _limiter_for(device):
    """Adapter: a Device's limiter config -> em_limiter.for_stream."""
    return em_limiter.for_stream(
        SPEAKER_RATE,
        device.limiter_enabled,
        em_limiter.DEFAULT_THRESHOLD_DB,
        device.limiter_release,
    )


def _guard_for(device):
    """Adapter: a Device's bass-guard config -> em_mbc.for_stream."""
    return em_mbc.for_stream(
        SPEAKER_RATE,
        device.bass_guard_enabled,
        device.bass_guard_db,
    )


async def _push_device_state(device: Device) -> None:
    """Push current transient device state to dashboard clients."""
    await api._push_event({
        "type":      "device_update",
        "device_id": device.device_id,
        "state": {
            "connected": True,
            "speaking":  device.speaking,
            "muted":     device.muted,
            "listening": device.listening,
            "thinking":  device.thinking,
            "timer_firing": device.timer_firing,
        },
    })


# ─── LED helpers ──────────────────────────────────────────────────────────────

def _make_leds(r, g, b):
    return [{"id": i, "r": r, "g": g, "b": b} for i in range(NUM_LEDS)]


async def leds_off(device: Device):
    if device.led_anim_capable:
        await device.send_led_anim({"pattern": "off"})
    else:
        await device.set_leds(_make_leds(0, 0, 0))


# Turn outcomes that get a distinguishing ring cue at turn end. Everything
# else ("ok", "cancelled") ends silently: the user either heard a reply or
# pressed the button themselves, so a cue would be noise.
#
# Rhythm carries the meaning, not colour — a new colour would collide with
# red (mute), orange (link down) or cyan (volume), and the point of the
# cue is to be understandable without a legend. One slow throb reads as
# "nothing heard"; fast blinks read as "something went wrong".
_OUTCOME_ANIM = {
    "no_speech":     "nospeech_anim",
    "no_tts":        "error_anim",
    "tts_error":     "error_anim",
    "timeout":       "error_anim",
    "stream_timeout": "error_anim",
    # Not an error — HA took the wake word and ended the run deliberately,
    # which the satellite setup flow does on every prompt. Needs a cue
    # because the turn is over in milliseconds: without one the ring lights
    # and clears too fast to register, and a device that worked perfectly
    # looks like it glitched.
    "pipeline_refused": "ack_anim",
}


async def _leds_turn_end(device: Device):
    """
    Clear the ring at turn end, playing a brief self-clearing cue first if
    the turn ended in a way the user would otherwise have no signal for.

    The cue anims carry a 1s TTL, so the device retires them on its own
    ticker with no follow-up message — nothing to leak if the controller
    dies in between, and a continuation/barge repaint simply supersedes it
    via the animator's generation counter.
    """
    outcome = device.last_turn_outcome
    device.last_turn_outcome = None
    key = _OUTCOME_ANIM.get(outcome or "")
    # Barge-in re-enters a fresh turn immediately and repaints listening —
    # a cue there would flash for a few frames and read as a glitch.
    if key and device.led_anim_capable and not device.barge_detected:
        anim = device.led_scene.get(key)
        if anim:
            log.info(f"[{device.device_id}] Turn ended '{outcome}' — ring cue")
            await device.send_led_anim(anim)
            return
    await leds_off(device)


async def leds_listening(device: Device):
    if device.led_anim_capable:
        await device.send_led_anim(device.led_scene["listening_anim"])
    else:
        await device.set_leds(device.led_scene["listening"], listening=True)


async def leds_spin_green(device: Device, stop_event: asyncio.Event):
    # Name is historical — the spinner renders whatever the device's scene
    # says (head+trail dot for solid scenes, rotating palette for pride).
    #
    # led_anim firmware animates locally: one message starts the spinner,
    # the device runs it on its own ticker (controller event-loop stalls
    # and WiFi jitter can't judder it), and this task just waits to send
    # the stop. Legacy firmware falls back to controller-rendered frames.
    if device.led_anim_capable:
        try:
            await device.send_led_anim(device.led_scene["spin_anim"])
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await leds_off(device)
        return
    spin_frame = device.led_scene["spin_frame"]
    pos = 0
    try:
        while not stop_event.is_set():
            await device.set_leds(spin_frame(pos))
            pos = (pos + 1) % NUM_LEDS
            await asyncio.sleep(0.08)
    except asyncio.CancelledError:
        pass
    finally:
        await leds_off(device)


# ─── Audio conversion ─────────────────────────────────────────────────────────

# (The numpy linear-interpolation resample_to_48k that used to live here is
# gone. TTS no longer arrives via a fetched URL at all — em_turn_engine.py
# receives it as 24kHz PCM chunks over the per-turn audio WebSocket, streamed
# by the HACS integration as HA's TTS engine produces them, and does its own
# small 24→48 linear-interp upsample before EQ. See docs/design/full-duplex-plan.md's
# "TTS conversion" section.)


# ─── Voice pipeline ───────────────────────────────────────────────────────────


async def _run_post_turn_playback_unlocked(
    device: Device, voice_response: bytes, cancel_event: asyncio.Event | None = None
) -> None:
    """
    Post-turn timing concern: EQ, stream to device, acoustic-feedback wait.

    voice_response is 48kHz mono S16_LE PCM (_fetch_tts_audio decodes at the
    wire rate now — no controller-side resample). Returns once the device
    audio buffer has drained (or cancel_event fires), so the caller can
    safely restart the mic without acoustic feedback into the next turn.
    """
    if device.output_chain_capable:
        # Firmware applies its own DSP. Keeping this exact buffer intact also
        # keeps the controller's EQ metric truthful: no controller EQ ran.
        speaker_pcm = voice_response
        device.playback_eq_ms = 0
        log.info(f"[{device.device_id}] Output chain: device")
    else:
        # Built here rather than inside _prepare_pcm so the stages survive the
        # call and can be asked what they actually did — see em_eq.describe_*.
        # One response is one buffer, so these are per-response instances and
        # carry no state between turns.
        _limiter = _limiter_for(device)
        _guard   = _guard_for(device)
        log.info(
            f"[{device.device_id}] Output chain: "
             f"{em_eq.describe_chain(device.eq_bands, device.eq_loudness, _limiter, _guard)}"
        )
        # EQ is a solid numpy crunch (hundreds of ms for a long response) — run
        # it off the event loop, which otherwise freezes every device's LED
        # frames, shell proxying, and WS handling right as playback starts
        # (observed as spinner stutter and console typing judder).
        def _prepare_pcm() -> bytes:
             return em_eq.apply(voice_response, SPEAKER_RATE, device.eq_bands,
                                device.eq_loudness, gain_db=device.tts_gain_db,
                                limiter=_limiter, guard=_guard)

        _t_eq0 = asyncio.get_event_loop().time()
        speaker_pcm = await asyncio.get_event_loop().run_in_executor(None, _prepare_pcm)
        device.playback_eq_ms = int(
            (asyncio.get_event_loop().time() - _t_eq0) * 1000
        )
        log.info(
            f"[{device.device_id}] Streaming {len(speaker_pcm)} bytes "
            f"({len(speaker_pcm)//SPEAKER_BYTES} periods) — "
            f"{em_eq.describe_activity(_limiter, _guard)}"
        )
    cancel_signal = cancel_event or device.cancel_event
    cancel_task    = asyncio.create_task(cancel_signal.wait())
    device.playback_done.clear()
    done_task      = asyncio.create_task(device.playback_done.wait())
    stream_task    = asyncio.create_task(device.stream_speaker(speaker_pcm))
    t_stream_start = asyncio.get_event_loop().time()
    # Opens the delivery window measured against the device's
    # playback_stats report (see Device.playback_send_t0).
    device.playback_send_t0 = t_stream_start

    done, _ = await asyncio.wait(
        [stream_task, cancel_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if cancel_task in done:
        log.info(f"[{device.device_id}] Cancelled during playback")
        stream_task.cancel()
    else:
        if not cancel_signal.is_set():
            # Wait for the DEVICE to say it finished, rather than estimating.
            #
            # The old code slept `audio_duration - elapsed` and declared
            # completion. Two things made that wrong, and both bite hardest
            # on exactly the links that need the most patience: `elapsed` is
            # socket-write time (which completes near-instantly however slow
            # the wire is) and was *subtracted*, and the estimate had no
            # visibility of how long the device's own buffer took to drain.
            # Measured 2026-07-24: the ring cleared 6.1s before the audio
            # actually stopped on a Retreat turn, 3.2s early on Lounge.
            #
            # playback_stats is emitted once the device's audio channel has
            # drained after EOS, so it is the real end of audio. The timeout
            # is only a backstop for the report never arriving (device drop,
            # pre-v2.9 firmware): generous, because ending the turn early is
            # the failure we are fixing. cancel_event is still raced — a
            # barge-in or a mute usually lands in this window, and an
            # uncancellable wait here is what caused the 5.7s dead window
            # fixed on 2026-07-10.
            audio_duration = len(speaker_pcm) / (SPEAKER_RATE * 2) + SPEAKER_PRIME_SECONDS
            elapsed        = asyncio.get_event_loop().time() - t_stream_start
            device.playback_send_ms = int(elapsed * 1000)
            timeout        = audio_duration * 2 + 10.0
            log.info(
                f"[{device.device_id}] Socket write took {elapsed:.1f}s "
                f"(NOT delivery — see delivery_ms), awaiting device "
                f"playback_stats (est {audio_duration:.1f}s, timeout {timeout:.1f}s)"
            )
            timeout_task = asyncio.create_task(asyncio.sleep(timeout))
            await asyncio.wait(
                [done_task, cancel_task, timeout_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            timeout_task.cancel()
            if cancel_signal.is_set():
                log.info(f"[{device.device_id}] Cancelled during playback drain")
            elif done_task.done():
                actual = asyncio.get_event_loop().time() - t_stream_start
                log.info(
                    f"[{device.device_id}] Playback complete "
                    f"(device-confirmed after {actual:.1f}s, est {audio_duration:.1f}s)"
                )
            else:
                # Ring held the full backstop. Either the device never
                # reported (worth knowing) or delivery was pathological.
                log.warning(
                    f"[{device.device_id}] Playback completion timed out after "
                    f"{timeout:.1f}s with no playback_stats — clearing ring anyway"
                )

    # The real end of audio, not the end of the socket write. The device
    # reports playback_stats once its audio channel drains after EOS, and
    # everything above waits for exactly that — so this is the one place that
    # knows the speaker has actually stopped. Clearing it in the stream task's
    # finally instead dropped the tile out of Speaking seconds early (the write
    # completes near-instantly), which is the same mistake the ring made until
    # 2026-07-24.
    await device._set_speaking(False)
    cancel_task.cancel()
    done_task.cancel()


async def _run_post_turn_playback(
    device: Device, voice_response: bytes, cancel_event: asyncio.Event | None = None
) -> None:
    """Serialize buffered playback with timer alarms and announcements.

    The locked implementation below waits for playback_stats before
    `_set_speaking(False)`; the wrapper keeps that device-confirmed transition
    inside the exclusive speaker lease.
    """
    async with device.speaker_lock:
        await _run_post_turn_playback_unlocked(device, voice_response, cancel_event)


async def _meter_at_playback_start(pcm_chunks, on_start):
    """
    Pass PCM through untouched, firing `on_start` when the device will have
    begun playing it.

    The device holds audio until roughly SPEAKER_PRIME_SECONDS is queued, or
    until EOS for a response shorter than that (primePeriods, pcm_speaker.go).
    Counting bytes handed to the streamer tracks that closely enough — the
    socket write completes near-instantly however slow the link is, which is
    the same property that makes send_ms useless as a delivery measure.

    Exhaustion fires it too, so a two-word answer still gets a meter. If the
    stream is cancelled or yields nothing, it never fires and the spinner
    simply stays up until the turn's ring cleanup — the failure direction
    that looks like "still working" rather than "dead".
    """
    prime_bytes = int(SPEAKER_PRIME_SECONDS * SPEAKER_RATE * 2)
    sent = 0
    fired = False
    async for chunk in pcm_chunks:
        yield chunk
        if not fired:
            sent += len(chunk)
            if sent >= prime_bytes:
                fired = True
                await on_start()
    if not fired:
        await on_start()


async def _run_streaming_post_turn_playback_unlocked(device: Device, pcm_chunks) -> int:
    """
    Play decoded HA TTS while the HTTP response is still arriving.

    The voice-turn path keeps one ffmpeg decoder, one stateful EQ chain, and
    one device 0x02...0x03 stream alive across all synthesized utterances.
    Closing any layer at an arbitrary network boundary would corrupt decoding,
    reset the EQ filters, or turn each chunk into a separate announcement.
    """
    if device.output_chain_capable:
        stream_eq = None
        log.info(f"[{device.device_id}] Output chain: device")
    else:
        _limiter = _limiter_for(device)
        _guard   = _guard_for(device)
        log.info(
            f"[{device.device_id}] Output chain: "
             f"{em_eq.describe_chain(device.eq_bands, device.eq_loudness, _limiter, _guard)}"
        )
        stream_eq = em_eq.StreamingEQ(
            SPEAKER_RATE,
            device.eq_bands,
            device.eq_loudness,
            gain_db=device.tts_gain_db,
            limiter=_limiter,
            guard=_guard,
        )
    # Cleared BEFORE streaming starts: the device sets it when its audio
    # channel drains after EOS, and a stale set from the previous response
    # would end this turn the moment we started waiting.
    device.playback_done.clear()
    cancel_task = asyncio.create_task(device.cancel_event.wait())
    done_task   = asyncio.create_task(device.playback_done.wait())
    stream_task = asyncio.create_task(
        device.stream_speaker_chunks(pcm_chunks, stream_eq)
    )
    t_stream_start = asyncio.get_event_loop().time()

    try:
        done, _ = await asyncio.wait(
            [stream_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            log.info(f"[{device.device_id}] Cancelled during streamed playback")
            return 0

        total_pcm, eq_ms, first_send_time, send_ms = stream_task.result()
        device.playback_eq_ms = eq_ms
        device.playback_send_ms = send_ms
        if stream_eq is not None:
            log.info(
                f"[{device.device_id}] Output chain activity: "
                f"{em_eq.describe_activity(_limiter, _guard)}"
            )
        stream_elapsed = asyncio.get_event_loop().time() - t_stream_start
        audio_duration = total_pcm / (SPEAKER_RATE * 2) + SPEAKER_PRIME_SECONDS

        if not total_pcm:
            log.info(f"[{device.device_id}] Streamed response contained no audio")
            return 0

        # Wait for the DEVICE to say it finished, exactly as the buffered path
        # does — do NOT sleep a computed `audio_duration - elapsed`.
        #
        # That estimate was removed on 2026-07-24 because it has no visibility
        # of the device's own buffer and cleared the ring 6.1s early on Retreat
        # and 3.2s on Lounge. Streaming makes it worse, not better: the time
        # spent streaming already covers most of the audio duration, so the
        # remainder computes to ~0 and the wait vanishes entirely while up to
        # ~5.5s is still queued in audioChanDepth. playback_stats is emitted
        # once that channel drains after EOS, so it is the real end of audio.
        # The timeout is only a backstop for the report never arriving (device
        # drop, pre-v2.9 firmware). cancel_event is still raced so a barge-in
        # or mute lands promptly.
        timeout = audio_duration * 2 + 10.0
        log.info(
            f"[{device.device_id}] Streamed {total_pcm} bytes "
            f"({total_pcm//SPEAKER_BYTES} periods) in {stream_elapsed:.1f}s "
            f"while HA generated audio (socket writes {send_ms}ms — NOT "
            f"delivery, see delivery_ms); awaiting device playback_stats "
            f"(est {audio_duration:.1f}s, timeout {timeout:.1f}s)"
        )
        timeout_task = asyncio.create_task(asyncio.sleep(timeout))
        try:
            await asyncio.wait(
                [done_task, cancel_task, timeout_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            timeout_task.cancel()
            await asyncio.gather(timeout_task, return_exceptions=True)

        if device.cancel_event.is_set():
            log.info(f"[{device.device_id}] Cancelled during streamed buffer drain")
        elif done_task.done():
            log.info(f"[{device.device_id}] Streamed playback complete (device reported)")
        else:
            log.warning(
                f"[{device.device_id}] No playback_stats after {timeout:.1f}s — "
                f"continuing (device drop or pre-v2.9 firmware)"
            )
        return total_pcm
    finally:
        # See _run_post_turn_playback: the end of audio is what the DEVICE
        # reports, not when the last byte reached the socket. In the finally so
        # a cancel or an error leaves the tile idle rather than stuck Speaking.
        await device._set_speaking(False)
        for t in (cancel_task, done_task):
            t.cancel()
        if not stream_task.done():
            stream_task.cancel()
        await asyncio.gather(cancel_task, done_task, stream_task, return_exceptions=True)
        # Close the async generator explicitly rather than leaving it to the
        # event loop's asyncgen hooks: its finally is what kills ffmpeg, and on
        # a barge-in (a routine path here) deferring that to GC leaves a decoder
        # running for an indeterminate window.
        aclose = getattr(pcm_chunks, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception as e:
                log.debug(f"[{device.device_id}] TTS stream close: {e}")


async def _run_streaming_post_turn_playback(device: Device, pcm_chunks) -> int:
    """Serialize streaming playback with timer alarms and announcements.

    The locked implementation waits for playback_stats before
    `_set_speaking(False)` so a timer alarm cannot enter during device drain.
    """
    async with device.speaker_lock:
        return await _run_streaming_post_turn_playback_unlocked(device, pcm_chunks)


async def _run_voice_locked(device: Device, trigger_label: str = "unknown", is_wakeword: bool = False,
                            initial_audio: tuple[bytes, ...] | None = None,
                            on_transcript=None, stt_only: bool = False,
                            request_id: str | None = None,
                            admission_valid=None):
    """
    is_wakeword: explicit flag for whether this turn was triggered by wake-
    word detection (as opposed to a button press). Used to decide preroll
    discard (see C3) — kept as its own parameter rather than inferred by
    parsing trigger_label (which is a free-form string meant for logging/
    trace display, not a control-flow key) so a future change to the label
    format can't silently change behaviour here.
    """
    # Stop is mandatory protection for every voice response. Do this before
    # interrupting music or lighting the ring: a visible turn that then cannot
    # answer safely is worse than refusing it at the boundary.
    if not device.stopword_capable or not device.stop_model_ready:
        log.warning("[%s] voice response refused: stop word unavailable", device.device_id)
        return False
    drained = 0
    if initial_audio is None:
        while not device.voice_queue.empty():
            try:
                device.voice_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
    if drained:
        log.info(f"[{device.device_id}] Voice turn: drained {drained} stale frames")
    # Voice preempts music: pause an active media session for the whole
    # conversation (incl. continuations) and resume it afterwards. The
    # matching resume_interrupted below only fires if this interrupt
    # actually paused something.
    if request_id is None:
        await em_player.interrupt(device.device_id)
    try:
        async with device.voice_lock:
            log.info(f"[{device.device_id}] Voice turn starting (esphome mode)")
            if request_id is None:
                device.listening = True
                await leds_listening(device)
                await _push_device_state(device)

            async def on_admitted():
                await em_player.interrupt(device.device_id)
                device.listening = True
                await leds_listening(device)
                await _push_device_state(device)

            stop_spin = asyncio.Event()
            spin_task = None
            async def cleanup_esphome():
                device.thinking  = False
                device.listening = False
                await _push_device_state(device)
                stop_spin.set()
                if spin_task and not spin_task.done():
                    spin_task.cancel()
                    try:
                        await spin_task
                    except asyncio.CancelledError:
                        pass
                await _leds_turn_end(device)

            async def on_thinking_esphome():
                nonlocal spin_task
                if stop_spin.is_set():
                    return  # cleanup already ran; turn is over
                if stt_only:
                    return
                device.thinking  = True
                device.listening = False
                await _push_device_state(device)
                log.info(f"[{device.device_id}] Thinking (esphome)")
                if not device.cancel_event.is_set() and (
                    spin_task is None or spin_task.done()
                ):
                    spin_task = asyncio.create_task(
                        leds_spin_green(device, stop_spin)
                    )
                # Barge-in is now reported by the device's local wake scorer
                # through wake_request; no controller inference task runs.

            async def post_turn_play_esphome(pcm_chunks):
                nonlocal spin_task
                if spin_task is None or spin_task.done():
                    spin_task = asyncio.create_task(
                        leds_spin_green(device, stop_spin)
                    )
                meter_refresh_task = None
                if device.led_anim_capable:
                    # Playback ring: throb with the response's live audio
                    # level (device-side "meter" pattern, RMS measured at
                    # the ALSA write). Replaces the thinking spinner on
                    # the device; spin_task keeps waiting on stop_event
                    # and its finally still clears the ring at turn end.
                    #
                    # RAISED WHEN AUDIO STARTS, NOT WHEN PLAYBACK IS SET UP.
                    # The meter renders the live speaker RMS, so before the
                    # device's ALSA write begins it draws an unlit ring. On
                    # the buffered path that was invisible (the fetch had
                    # already completed, so frames flushed at socket speed),
                    # but streaming moves the fetch+decode INSIDE playback:
                    # sending it here left the ring dark from the end of the
                    # spinner until HA returned audio — seconds on a slow
                    # response, and indistinguishable from a failed turn
                    # (user report 2026-07-31). The spinner keeps running
                    # until _meter_on fires, so the handover is continuous.
                    #
                    # A streamed response has no known duration up front.
                    # Refresh the same bounded dead-man TTL while PCM arrives:
                    # long speech cannot outlive its meter, but a controller
                    # crash still lets the device clear the ring on its own.
                    meter = dict(device.led_scene["meter_anim"])
                    meter["ttlSec"] = em_scenes.meter_ttl(0.0)

                    async def _refresh_meter_dead_man() -> None:
                        refresh_seconds = max(1.0, meter["ttlSec"] / 2)
                        while True:
                            await asyncio.sleep(refresh_seconds)
                            await device.send_led_anim(meter)

                    async def _meter_on() -> None:
                        nonlocal meter_refresh_task
                        if meter_refresh_task is not None:
                            return
                        await device.send_led_anim(meter)
                        meter_refresh_task = asyncio.create_task(
                            _refresh_meter_dead_man()
                        )

                    pcm_chunks = _meter_at_playback_start(pcm_chunks, _meter_on)

                if not device.barge_in_enabled:
                    # Acoustic-feedback guard (barge-in off): stop the mic
                    # BEFORE playback, not just in the post-turn finally.
                    # With the mic running through TTS pre-AEC, the device
                    # processed its own speaker echo (63-65 junk frames per
                    # turn measured 2026-07-06) and sent it upstream on the
                    # same Wi-Fi radio receiving the TTS frames (speaker
                    # underruns → audible stutter). The finally's mic_stop
                    # stays as a safety net (StopMic no-ops when already
                    # stopped); restart is owned by the continuation branch /
                    # wake listener / button handler as before.
                    await device.mic_stop()

                try:
                    return await _run_streaming_post_turn_playback(
                        device, pcm_chunks
                    )
                finally:
                    if meter_refresh_task is not None:
                        meter_refresh_task.cancel()
                        await asyncio.gather(
                            meter_refresh_task, return_exceptions=True
                        )

            # P0-1: no mic_start_turn() here on the initial (wake/button)
            # entry — for a wake turn the stream is already running on
            # ch6 and oww_paused routes frames to voice_queue. The
            # acoustic-feedback guard is mic_stop in
            # post_turn_play_esphome, sent immediately before TTS
            # playback; the finally below is only the safety net.
            #
            # Continuation loop: if HA sets continue_conversation on
            # INTENT_END, re-trigger immediately after TTS+drain rather
            # than returning to OWW idle. The reference implementation
            # (linux-voice-assistant) uses a 0.5s settle delay after TTS
            # before opening the mic — that's already covered by
            # the streaming playback path's buffer drain sleep, so no
            # additional delay is needed here.
            #
            # C2 fix (2026-07-05 review): the `finally` below runs
            # device.mic_stop() on every iteration, including the one
            # that decides to continue — previously nothing ever put the
            # stream back before looping into the next trigger_voice_turn,
            # so a continuation turn streamed from a stopped mic and
            # silently timed out as no_speech every time. Fixed by
            # calling device.mic_start() (no lock_mic — same ch6 stream
            # as the wake path; no-ops if somehow already running) in the
            # continuation branch, before looping.
            #
            # C3 fix: preroll_discard is 0 for button/continuation turns
            # (no wake-word tail to remove — discarding real audio here
            # just clips the first word/words, the exact bug P0-1 fixed
            # on the wake path). A ring-backed wake already includes the
            # frames before and through the crossing; discarding the next
            # live frames would create a hole after the prepended audio.
            turn_label      = trigger_label
            turn_initial_audio = initial_audio or ()
            preroll_discard = (
                turn_engine.VOICE_PREROLL_DISCARD
                if is_wakeword and not turn_initial_audio else 0
            )
            while True:
                should_continue = False
                try:
                    should_continue = await turn_engine.trigger_voice_turn(
                        device=device,
                        on_thinking=on_thinking_esphome,
                        post_turn_play=post_turn_play_esphome,
                        trigger_label=turn_label,
                        preroll_discard=preroll_discard,
                        initial_audio=turn_initial_audio,
                        on_transcript=on_transcript,
                        stt_only=stt_only,
                        request_id=request_id,
                        on_admitted=on_admitted if request_id is not None else None,
                        admission_valid=admission_valid,
                    )
                    if (request_id is not None
                            and device.wake_request_id == request_id):
                        device.wake_request_id = None
                finally:
                    # On barge the mic stays up: the user's follow-up
                    # command is already flowing into voice_queue and a
                    # mic_stop/start cycle here would drop the words
                    # spoken in the same breath as the wake word.
                    # For wake_request_v1 the device's wake stream is
                    # always-on and turn audio is gated by GrantMic/
                    # oww_paused, not by mic_start/stop — stopping here
                    # would kill the stream that the next continuation
                    # needs and, for the wake path, drop the GrantMic
                    # flag that makes streamMic send at all.
                    if "wake_request_v1" not in (device.capabilities or []):
                        if not device.barge_detected:
                            await device.mic_stop()
                    await cleanup_esphome()
                    log.info(f"[{device.device_id}] Voice turn complete (esphome mode)")

                if device.barge_detected:
                    # A device-admitted barge cancelled playback and has its
                    # next turn ready to route. Re-enter immediately.
                    device.barge_detected = False
                    request_id = device.barge_request_id
                    device.barge_request_id = None
                    device.cancel_event.clear()
                    log.info(f"[{device.device_id}] Barge-in: starting interrupting turn")
                    turn_label      = "barge-in"
                    preroll_discard = turn_engine.VOICE_PREROLL_DISCARD
                    turn_initial_audio = ()
                    # A fresh deadline for this replacement turn, not the
                    # stale one carried in from whichever request originally
                    # entered this loop — see _make_admission_valid.
                    admission_valid = _make_admission_valid(
                        device, asyncio.get_running_loop().time() + 4.0
                    )
                    # Reset spinner state for the next turn's thinking animation.
                    stop_spin.clear()
                    spin_task = None
                    continue

                if should_continue and not device.cancel_event.is_set():
                    log.info(f"[{device.device_id}] Continuing conversation (HA requested)")
                    # Continuation is controller-initiated, not a new device
                    # wake_request, so it needs a fresh admission window and
                    # must not reuse the original wake's requestId/deadline.
                    # Reusing the original deadline made every continuation
                    # that started >4s after the initial grant (i.e. any
                    # normal-length turn) fail admission_valid() instantly
                    # as "disconnected" — observed as turn 500 in 3ms.
                    request_id = None
                    admission_valid = _make_admission_valid(
                        device, asyncio.get_running_loop().time() + 4.0
                    )
                    # C2 fix: put the mic stream back before looping —
                    # the finally above just stopped it, and the next
                    # trigger_voice_turn will read from voice_queue,
                    # which is fed only while the device stream is
                    # running. No lock_mic — same ch6 stream as wake.
                    await device.mic_start()
                    # Fresh stream starts with the VAD gate closed — the
                    # user must speak again from zero, same onset cost
                    # as any post-mic_stop restart. Acceptable for v1 of
                    # continuation (see review C2 wrinkle note); §3.4's
                    # device preroll ring will fix this properly later.
                    # Drain stale frames accumulated during TTS playback
                    # before the next turn begins — same as post-wake drain.
                    drained = 0
                    while not device.voice_queue.empty():
                        try:
                            device.voice_queue.get_nowait()
                            drained += 1
                        except asyncio.QueueEmpty:
                            break
                    if drained:
                        log.debug(f"[{device.device_id}] Continuation: drained {drained} stale frames")
                    # Re-arm listening state for the follow-up turn.
                    device.listening = True
                    await leds_listening(device)
                    await _push_device_state(device)
                    turn_label      = "continuation"
                    preroll_discard = 0
                    turn_initial_audio = ()
                    # Reset spinner state for the next turn's thinking animation.
                    stop_spin.clear()
                    spin_task = None
                else:
                    break

    finally:
        # Drain voice_queue before clearing oww_paused so stale turn audio
        # cannot become the preamble of the next admitted device wake.
        _drained = 0
        while not device.voice_queue.empty():
            try:
                device.voice_queue.get_nowait()
                _drained += 1
            except asyncio.QueueEmpty:
                break
        if _drained:
            log.info(
                f"[{device.device_id}] oww_paused drain: "
                f"{_drained} stale frames cleared before routing flip"
            )
        device.oww_paused.clear()
        device.oww_paused_since = None
        if "wake_request_v1" in (device.capabilities or []):
            await device.mic_start()
        log.info(f"[{device.device_id}] oww_paused cleared")
        # Release the arbitration claim so another device answering a
        # genuinely new utterance isn't suppressed by a stale window.
        _wake_arbiter.release(device.device_id)
        # Conversation over — un-pause a media session this turn preempted.
        await em_player.resume_interrupted(device.device_id)


async def _run_timer_speech_turn(device: Device, first_frame: bytes) -> None:
    """Use HA STT to confirm speech before dismissing a timer alarm."""
    if device.voice_lock.locked():
        return

    async def on_transcript(text: str) -> None:
        if await api.dismiss_timer_alarm(device.device_id):
            log.info(
                "[%s] Timer alarm dismissed by STT transcript: %r",
                device.device_id, text,
            )

    device.oww_paused.set()
    device.oww_paused_since = asyncio.get_event_loop().time()
    await device.beam_lock()
    try:
        await _run_voice_locked(
            device,
            trigger_label="timer-speech",
            is_wakeword=False,
            initial_audio=(first_frame,),
            on_transcript=on_transcript,
            stt_only=True,
        )
    finally:
        await device.beam_unlock()


# ─── Button handler ───────────────────────────────────────────────────────────

async def handle_button_event(device: Device, event: dict):
    click_type = event.get("clickType")
    down       = event.get("down", True)

    if down:
        return

    if click_type == 138:   # DotClick
        request_id = event.get("requestId")
        async def deny_button(reason: str):
            if isinstance(request_id, str) and request_id:
                await device.send_control({"type": "wake_deny", "requestId": request_id, "reason": reason})
        # A HOLD is a separate gesture, forwarded to HA rather than starting a
        # turn. heldMs is measured on the device: timing the down/up messages
        # here would be at the mercy of RTT excursions measured past 1600ms on
        # this fleet, which would misread taps as holds.
        #
        # Absent heldMs (firmware predating this) reads as a tap, so the action
        # button keeps working exactly as before on devices that have not
        # updated — degrade to old behaviour, never to a wrong answer.
        held_ms = event.get("heldMs") or 0
        # Mute is read from the EVENT where the firmware reports it, falling
        # back to the last mute_state message. The event is authoritative: it
        # carries the state at the instant of the press, where device.muted is
        # whatever the last message left behind.
        muted = bool(event.get("muted", device.muted))

        # A tap dismisses an active alarm locally. Do this before the normal
        # tap-event/voice-turn policy so it cannot become an HA command.
        # Holds remain HA events and use the normal gesture policy below.
        if (api.timer_alarm_ringing(device.device_id)
                and held_ms < turn_engine.BUTTON_HOLD_MS):
            request_id = event.get("requestId")
            if isinstance(request_id, str) and request_id:
                await device.send_control({
                    "type": "wake_deny", "requestId": request_id,
                    "reason": "timer_dismissed",
                })
            if await api.dismiss_timer_alarm(device.device_id):
                log.info(f"[{device.device_id}] Dot button tap dismissed timer alarm")
            return

        action = em_button.decide(
            held_ms=held_ms,
            hold_ms=turn_engine.BUTTON_HOLD_MS,
            muted=muted,
            turn_active=device.voice_lock.locked(),
            # ANDed with the capability — see em_button.decide.
            tap_event=(
                device.button_single_tap_event and device.button_hold_capable
            ),
        )

        if action == em_button.HOLD:
            await deny_button("gesture")
            log.info(f"[{device.device_id}] Dot button held {held_ms}ms → HA event")
            ha_sidechannels.button_event(device.device_id, "long", held_ms)
            return

        if action == em_button.TAP_EVENT:
            await deny_button("gesture")
            window_ms = device.button_multi_tap_ms
            if window_ms <= 0:
                log.info(f"[{device.device_id}] Dot button tap → HA event (single)")
                ha_sidechannels.button_event(device.device_id, "single")
                return

            device.tap_burst.tap(window_ms)
            log.info(
                f"[{device.device_id}] Dot button tap "
                f"{device.tap_burst.count} in burst (window {window_ms}ms)"
            )
            return

        if action == em_button.BLOCKED:
            await deny_button("muted")
            # Only the TURN is blocked. The hold and tap-event above have
            # already been forwarded, muted or not.
            log.info(f"[{device.device_id}] Dot button tap ignored — mic is muted")
            return

        if action == em_button.CANCEL:
            await deny_button("cancelled")
            log.info(f"[{device.device_id}] Dot button — cancelling voice turn")
            device.cancel_event.set()
            turn_engine.cancel_voice_turn(device.device_id, reason="cancelled")
            # Flush the device's speaker too, or cancelling DURING the spoken
            # response only stops the controller feeding it: the ring clears
            # while up to ~5.5s already in audioChanDepth plays out, and the
            # device carries on talking after you have visibly cancelled it.
            #
            # cancel_event alone cannot fix that — it aborts our end, not the
            # audio already on the device. Mute and barge-in both send this
            # for exactly the same reason; the button was the one deliberate
            # cancel that did not.
            await device.send_control({"type": "speaker_flush"})
        else:
            if not isinstance(request_id, str) or not request_id:
                log.warning("[%s] button turn refused: missing requestId", device.device_id)
                return
            if device.wake_request_id is not None:
                await deny_button("busy")
                return
            if not device.stopword_capable or not device.stop_model_ready:
                await deny_button("not_ready")
                return
            if device.data_ws is None:
                await deny_button("no_data")
                return
            device.wake_request_id = request_id
            button_deadline = asyncio.get_running_loop().time() + 4.0

            def button_admission_valid() -> bool:
                return (
                    _devices.get(device.device_id) is device
                    and not device.muted
                    and device.data_ws is not None
                    and device.stopword_capable
                    and device.stop_model_ready
                    and asyncio.get_running_loop().time() < button_deadline
                )

            log.info(f"[{device.device_id}] Dot button → voice turn")
            device.cancel_event.clear()
            device.oww_paused.set()
            device.oww_paused_since = asyncio.get_event_loop().time()
            async def _button_voice_turn():
                try:
                    await _run_voice_locked(
                        device, trigger_label="button", is_wakeword=False,
                        request_id=request_id,
                        admission_valid=button_admission_valid,
                    )
                finally:
                    if device.wake_request_id == request_id:
                        device.wake_request_id = None
                log.info(f"[{device.device_id}] Button turn complete — restarting mic")
                # Post-turn: back to ch6 omni for OWW listening. mic_stop
                # first: if the turn had no TTS (cancel/error/no-speech), the
                # lock_mic stream from mic_start_turn is still running and a
                # bare mic_start would no-op against it — leaving the GATED,
                # beam-locked turn stream as the permanent wake stream. Safe
                # now that streamMic's exit has the ownership check (the
                # stop/start pair can no longer leak a second stream).
                await device.mic_stop()
                await device.mic_start()
            # M1 fix (2026-07-05 review): keep a reference and log exceptions
            # instead of a bare fire-and-forget create_task() — previously
            # any exception raised in this task vanished silently with no
            # log line, standard asyncio fire-and-forget hygiene issue.
            _btn_task = asyncio.create_task(_button_voice_turn())
            _btn_task.add_done_callback(_log_task_exception)


# ─── Control plane handler ────────────────────────────────────────────────────

async def _link_auth_ok(
    ws: WebSocketServerProtocol, device_id: str, secure: bool, plane: str
) -> bool:
    """
    Device-link auth gate, applied to all three WS planes once the
    device_id is known.

    Rules (rollout-safe by construction):
      - a presented token that MISMATCHES a stored one always rejects;
      - a stored token with NO token presented is allowed unless
        REQUIRE_DEVICE_TLS — the DB row is minted before the files land
        on the device, and rejecting in that window would cut off the
        shell plane that the credential push itself rides on;
      - a presented token for a device with NOTHING on record is ignored,
        not rejected (see below);
      - REQUIRE_DEVICE_TLS=1 requires TLS + a matching token, full stop.

    That third rule used to be a rejection, and it made deleting a device a
    one-way door. Delete removes the row, the token is a column on it, and the
    device re-reads its credential file on every dial — so it presented a token
    nothing recognised and was refused on all three planes, INCLUDING the shell
    plane the controller would otherwise push fresh credentials over. The
    device retried forever behind a pulsing orange ring, and the dashboard had
    nothing to show because as far as it was concerned the device did not
    exist.

    Rejecting there also never bought anything. The rule immediately above
    admits a connection presenting no token at all, so an unrecognised token
    was being treated as worse than none while anyone could simply omit the
    header. A device with a stale credential now comes back as pending and
    waits for approval, which is the decision a human should be making anyway.

    `REQUIRE_DEVICE_TLS=1` installs are unaffected: the last rule still
    demands a token that matches a stored one, so a deleted device is refused
    there regardless and re-provisioning over USB stays the intended path.
    """
    presented = None
    try:
        presented = ws.request.headers.get("X-EM-Token")
    except AttributeError:
        pass

    loop = asyncio.get_event_loop()
    expected = await loop.run_in_executor(None, db.get_device_token, device_id)

    verdict = em_linkauth.decide(
        presented=presented,
        expected=expected,
        secure=secure,
        require_tls=REQUIRE_DEVICE_TLS,
    )
    if not verdict.ok:
        log.warning(f"[{plane}] {device_id}: {verdict.reason} — rejecting")
        return False
    if verdict.stale_token:
        # Allowed, but worth seeing in the log: almost always a device that was
        # deleted and has come back carrying the credential from its previous
        # life, which is the answer to "why is this in pending again".
        log.warning(
            f"[{plane}] {device_id}: {verdict.reason}. "
            f"Treating as an unregistered device; it will need approval."
        )
    return True


def _make_admission_valid(device: Device, deadline: float):
    """
    Build the "is this device still eligible for its grant" check used at
    HA-acceptance time, closing over a caller-supplied monotonic deadline
    rather than assuming one. Shared by the ordinary wake path and the
    barge-restart path in _run_voice_locked so they cannot drift apart the
    way they already did once: the barge-restart branch used to reuse
    whichever admission_valid closure _run_voice_locked was originally
    called with, deadline included — for a barge admitted seconds or tens
    of seconds into an active turn, that deadline was for the ORIGINAL wake
    and had already passed. trigger_voice_turn's own freshness check then
    failed instantly once HA accepted the replacement turn's offer, which
    read as "barge-in stops TTS but never opens the follow-up turn — it just
    ends the voice session" from the outside, with nothing to suggest a
    deadline was the cause.
    """
    def admission_valid() -> bool:
        return (
            _devices.get(device.device_id) is device
            and not device.muted
            and device.data_ws is not None
            and device.oww_model_ready
            and device.stopword_capable
            and device.stop_model_ready
            and asyncio.get_running_loop().time() < deadline
        )
    return admission_valid


def _wake_request_admission_gate(device: Device) -> str | None:
    """
    Decide whether an incoming wake_request should be denied before it ever
    reaches _handle_wake_request, or None to let it proceed. Pure and
    dependency-free by construction — split out the way em_button.decide and
    em_linkauth.decide are, because this exact ordering shipped inverted and
    nothing caught it: device.wake_request_id stays set for an admitted
    turn's entire ACTIVE duration (cleared only once trigger_voice_turn
    returns, not just during the admission handshake), so it is non-None for
    the whole window a device-originated barge attempt can arrive in.
    Checking "already busy" before "is this device mid-turn with barge-in
    on" denied every barge request here, before this gate or
    _handle_wake_request's own admit_barge branch ever ran — barge-in could
    not admit under any configuration, and the only test coverage called
    _handle_wake_request directly with wake_request_id pre-set to match,
    which is what the dispatch loop is supposed to establish and is exactly
    what this bug prevented.
    """
    if device.voice_lock.locked():
        if not device.barge_in_enabled:
            return "barge_disabled"
        return None
    if device.wake_request_id is not None:
        return "busy"
    return None


async def _handle_wake_request(device: Device, msg: dict) -> None:
    """Admit one device wake without allowing races across async boundaries."""
    request_id = msg.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        return

    async def deny(reason: str) -> None:
        # Every deny reason below was previously silent — a denial produces
        # no other observable effect (no turn row, no capture-adjacent log
        # line), so a barge-in or wake attempt that never lit the ring was
        # undiagnosable from server logs alone. Log at the point of decision
        # rather than reconstructing it after the fact.
        log.info(f"[{device.device_id}] wake_deny requestId={request_id} reason={reason}")
        await device.send_control({
            "type": "wake_deny", "requestId": request_id, "reason": reason,
        })
        if device.wake_request_id == request_id:
            device.wake_request_id = None

    if "wake_request_v1" not in (device.capabilities or []):
        await deny("unsupported")
        return
    if device.wake_request_id != request_id:
        await deny("busy")
        return
    if msg.get("source") != "wakeword" or msg.get("model") != device.oww_model:
        await deny("not_ready")
        return
    if not device.oww_model_ready:
        await deny("not_ready")
        return
    if device.muted:
        await deny("muted")
        return
    if device.data_ws is None:
        await deny("no_data")
        return
    try:
        score = float(msg["score"])
        threshold = float(msg["threshold"])
        age_ms = int(msg["ageMs"])
        activation_seq = int(msg["activationSeq"])
    except (KeyError, TypeError, ValueError):
        await deny("malformed")
        return
    if not (0 <= score <= 1 and 0 <= threshold <= 1 and 0 <= age_ms <= 10_000
            and 0 <= activation_seq <= 0xFFFF and score >= threshold):
        await deny("malformed")
        return
    if age_ms > 4000:
        await deny("stale")
        return
    request_deadline = asyncio.get_running_loop().time() + max(
        0.0, (4000 - age_ms) / 1000.0
    )
    admission_valid = _make_admission_valid(device, request_deadline)

    if device.voice_lock.locked():
        if not device.barge_in_enabled:
            await deny("barge_disabled")
            return
        await turn_engine.admit_barge(
            device, request_id, score, threshold, activation_seq,
            admission_valid=admission_valid,
        )
        return

    if not device.stopword_capable or not device.stop_model_ready:
        await deny("not_ready")
        return

    claimed = False
    if device.wake_arb_ms > 0 and len(_devices) > 1:
        winner = _wake_arbiter.claim(device.device_id, device.wake_arb_ms / 1000.0)
        if winner != device.device_id:
            await deny("arbitration")
            device.wake_request_id = None
            return
        claimed = True

    device.oww_paused.set()
    device.oww_paused_since = asyncio.get_running_loop().time()
    device.last_wake = {
        "model": device.oww_model, "score": round(score, 4),
        "threshold": round(threshold, 4), "noise_floor": None,
        "activation_seq": activation_seq, "age_ms": age_ms,
    }
    try:
        if _devices.get(device.device_id) is not device:
            await deny("disconnected")
            return
        await _run_voice_locked(
            device, trigger_label=f"wakeword-dev({score:.3f})",
            is_wakeword=True, initial_audio=(), request_id=request_id,
            admission_valid=admission_valid,
        )
    finally:
        if claimed:
            _wake_arbiter.release(device.device_id)
        if device.wake_request_id == request_id:
            device.wake_request_id = None
        device.oww_paused.clear()
        device.oww_paused_since = None


def _wake_status_ready(device: Device, msg: dict) -> bool:
    """Accept readiness only for the selected classifier bytes."""
    model = msg.get("model")
    checksum = msg.get("classifierMd5")
    if not msg.get("ready") or model != device.oww_model or not isinstance(checksum, str):
        return False
    try:
        import em_oww_assets
        source = em_oww_assets.classifier_source(model)
        return source is not None and checksum == em_oww_assets.md5_file(source)
    except Exception:
        log.exception("[%s] failed to resolve wake classifier", device.device_id)
        return False


async def handle_control(ws: WebSocketServerProtocol, secure: bool = False):
    """
    Handle a /control WebSocket connection from a device.
    """
    device = None
    remote = ws.remote_address

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = json.loads(raw)

        if msg.get("type") != "register":
            log.warning(
                f"[control] First message from {remote} was not register — closing"
            )
            await ws.close()
            return

        device_id    = msg["device_id"]

        if not await _link_auth_ok(ws, device_id, secure, "control"):
            await ws.close()
            return
        ip           = msg.get("ip", str(remote[0]))
        version      = msg.get("version")
        capabilities = msg.get("capabilities", [])

        loop         = asyncio.get_event_loop()
        approval_mode = db.get_config("device_approval", DEVICE_APPROVAL)
        row          = await loop.run_in_executor(None, db.get_device, device_id)

        if row is None:
            if approval_mode == "auto":
                label = f"Unknown {device_id[:8]}"
                await loop.run_in_executor(
                    None, db.register_new_device, device_id, ip, version
                )
                await loop.run_in_executor(
                    None, db.approve_device, device_id, label, None
                )
                log.info(
                    f"[control] Auto-approved new device: {device_id} "
                    f"label={label!r}"
                )
                row = await loop.run_in_executor(None, db.get_device, device_id)
            else:
                await loop.run_in_executor(
                    None, db.register_new_device, device_id, ip, version
                )
                await ws.send(json.dumps({"type": "pending"}))
                log.info(
                    f"[control] Unknown device held as pending: {device_id} "
                    f"from {ip}"
                )
                await api.notify_device_pending(device_id, ip)
                db.log_device(
                    device_id, "info", "controller",
                    f"Device seen for first time — pending approval ({ip})"
                )
                await ws.close()
                return

        if not row["approved"]:
            await loop.run_in_executor(
                None, db.upsert_device_seen, device_id, ip, version
            )
            await ws.send(json.dumps({"type": "pending"}))
            log.info(
                f"[control] Device pending approval: {device_id} from {ip}"
            )
            await api.notify_device_pending(device_id, ip)
            await ws.close()
            return

        await loop.run_in_executor(
            None, db.upsert_device_seen, device_id, ip, version
        )

        device = Device(device_id, ip, capabilities, ws)
        # Why the device has no ambient light sensor, when it has none. The
        # capability list says only that it is absent; without the reason,
        # an unfitted chip and an unbound driver are indistinguishable
        # remotely, and #90 needed a shell session on the user's own hardware
        # to tell them apart. Absent on firmware that does not send it, which
        # reads as "not reported" rather than as a fault.
        device.ambient_light_status = msg.get("ambient_light_status")
        # Link-security telemetry for the dashboard: True when this control
        # connection arrived over the TLS listener.
        device.secure = secure
        # Hydrate the observability panel's turn history from the persistent
        # turns table so it survives controller and device restarts.
        try:
            past_turns = await loop.run_in_executor(
                None, db.get_turns, device_id, device.turn_history.maxlen
            )
            device.turn_history.extend(past_turns)
        except Exception as e:
            log.warning(f"[{device_id}] Turn history hydration failed: {e}")
        _devices[device_id] = device

        log.info(
            f"[control] Device connected: {device_id} v={version} "
            f"at {ip} caps={capabilities}"
        )
        db.log_device(
            device_id, "info", "controller",
            f"Connected from {ip} version={version}"
        )

        # Announce the (re)connection to HA-facing event clients. The
        # disconnect path already pushes `device_disconnected`
        # (api.notify_device_disconnected); this is its missing mirror. It
        # matters most on a CONTROLLER restart: the HACS integration's
        # /api/events WS reconnects within a second or two, but the device
        # itself reconnects a few seconds later, so the reconnect-moment
        # snapshot the coordinator captured shows this device still absent /
        # connected=False. Nothing else told HA it was back except the
        # coordinator's periodic REST poll, which push events keep deferring —
        # so entities sat unavailable for hours after a restart (2026-08-19)
        # until a manual reload. Pushing device_update(connected=True) here
        # gives the coordinator a live signal to clear that the instant the
        # device is actually back.
        await _push_device_state(device)

        await device.send_control({"type": "ack", "device_id": device_id})

        config = await loop.run_in_executor(
            None, db.get_effective_device_config, device_id
        )
        # The MA endpoint is system connection metadata for native Sendspin
        # clients, which cache it and reconnect directly to MA.
        if device.sendspin_native_capable:
            config["sendspinServer"] = await loop.run_in_executor(
                None, db.get_config, "music_assistant_url", MUSIC_ASSISTANT_URL
            ) or ""
        await device.send_control({"type": "config", **config})
        device.oww_model     = config.get("owwModel", DEFAULT_WAKE_MODEL)
        device.stop_model    = config.get("stopModel", "stop")
        device.stop_threshold = float(config.get("stopThreshold", 0.75))
        device.wake_arb_ms   = int(config.get("wakeArbitrationMs", 300))
        device.save_utterances = bool(config.get("saveUtterances", False))
        device.save_wake_captures = bool(config.get("saveWakeCaptures", False))
        device.save_stop_captures = bool(config.get("saveStopCaptures", False))
        device.barge_in_enabled = bool(config.get("bargeInEnabled", False))
        device.button_single_tap_event = bool(
            config.get("buttonSingleTapEvent", False)
        )
        device.button_multi_tap_ms = int(config.get("buttonMultiTapMs", 0))
        # Gates ble_adverts forwarding below — replaces em_ble_proxy's
        # reconcile()/DeviceBleProxyServer machinery (Phase 4 cutover): no
        # per-device TCP listener or mDNS entry to bring up anymore, just a
        # config flag checked per batch, same idiom as save_utterances.
        device.ble_proxy_enabled = bool(config.get("bleProxyEnabled", False))
        asyncio.create_task(
            api.reconcile_oww_assets(device_id, device)
        ).add_done_callback(_log_task_exception)
        device.eq_bands      = config.get("eqBands", [0.0] * 8)
        device.eq_loudness   = bool(config.get("eqLoudness", False))
        device.tts_gain_db   = max(0.0, min(12.0, float(config.get("ttsGainDb", 0.0))))
        device.bass_guard_enabled = bool(config.get("bassGuardEnabled", True))
        device.bass_guard_db      = float(config.get(
            "bassGuardDb", em_mbc.DEFAULT_BASS_GUARD_DB))
        device.limiter_enabled   = bool(config.get("limiterEnabled", True))
        device.limiter_release   = float(config.get(
            "limiterRelease", em_limiter.DEFAULT_RELEASE_MS))
        device.led_scene     = em_scenes.resolve(config)
        if device.led_anim_capable and device.led_scene.get("listening_anim"):
            await device.send_control({
                "type": "config",
                "listeningAnim": device.led_scene["listening_anim"],
            })
        # Initialise volume from stored config — device will report its real
        # value via volume_state on connect, but this seeds a sane default
        # in the window before that first message arrives.
        device.volume = _device_level_to_ha(
            int(config.get("startupVolume", 85))
        )
        log.info(f"[control] Config pushed to {device_id} (volume={device.volume:.3f})")

        await leds_off(device)
        await api.notify_device_connected(device_id)
        _device_ref = device
        async def _standalone_play(pcm_bytes: bytes, _d=_device_ref) -> bool:
            # Same acoustic-feedback guard as voice turns: announcements
            # play outside a turn, so the always-on OWW stream is live —
            # stop it for the duration and put it back after. An active
            # media session pauses for the announcement and resumes.
            #
            # cancel_event is cleared first, exactly as a voice turn does.
            # It is set by a cancel (a button press during a turn, a mute)
            # and was ONLY ever cleared when the next voice turn started —
            # so a cancelled turn left it set and _run_post_turn_playback
            # then abandoned every subsequent announcement at "Cancelled
            # during playback", silently, until a turn happened to run.
            # Measured on Test Device 01 on 2026-08-17: a turn cancelled at
            # 12:02:32 killed the next seven announcements over three
            # minutes. An announcement is a new action and nothing that set
            # that flag earlier has any claim on it.
            _d.cancel_event.clear()
            await em_player.interrupt(_d.device_id)
            await _d.mic_stop()
            try:
                await _run_post_turn_playback(_d, pcm_bytes)
            finally:
                await _d.mic_start()
                await em_player.resume_interrupted(_d.device_id)
            # Whether the audio actually reached the speaker. Something that
            # cancelled mid-playback (a mute, a button) means the user did
            # not hear it, and telling HA it finished successfully would be
            # untrue — it is the one thing the announcement reply reports.
            return not _d.cancel_event.is_set()
        # Stashed on the device rather than handed to a per-device ESPHome
        # server (that mechanism is gone — Phase 4 cutover): the turn engine's
        # own announcement path (em_turn_engine.create_turn(kind="announcement"))
        # reads em_api._devices/em_controller directly at call time and does
        # not need this pre-registered, but the capability itself — play PCM
        # on this device's speaker outside of a turn — stays reachable for
        # future callers (e.g. an admin push-TTS action) rather than being
        # silently dropped. Shape (async, returns bool) is pinned by
        # test_deploy.py's test_an_announcement_reports_whether_it_actually_played.
        device.standalone_play = _standalone_play
        # Capabilities before entities are advertised — HA re-reads them on
        # its own poll/event cadence now, not a one-shot ListEntities.
        ha_sidechannels.capabilities(device_id, capabilities)
        ha_sidechannels.wake_model(device_id, device.oww_model)

        # ── Main message loop ─────────────────────────────────────────────

        async def ping_loop():
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                now = loop.time()
                # Abandon replies that never came — a very late pong is a
                # lost packet, not a latency sample.
                stale = [q for q, t in device.ping_sent.items()
                         if now - t > PING_TIMEOUT_SEC]
                for q in stale:
                    device.ping_sent.pop(q, None)
                stale_clock = [q for q, t in device.clock_probe_sent.items()
                               if now - (t / 1_000_000) > PING_TIMEOUT_SEC]
                for q in stale_clock:
                    device.clock_probe_sent.pop(q, None)
                    device.clock_probe_busy.pop(q, None)
                device.ping_seq += 1
                seq = device.ping_seq
                device.ping_sent[seq] = now
                # Record busyness at SEND time: the discriminator is whether
                # the device was doing anything when the probe went out, and
                # by the time the reply lands a turn may have started or
                # ended.
                device.ping_busy[seq] = device.is_busy()
                await device.send_control({"type": "ping", "id": seq})

                # Unlike the legacy RTT ping, this probe carries timestamps
                # from both monotonic clock domains so scheduled audio can be
                # translated into device time. Keep the two probes separate:
                # old firmware ignores clock_probe and continues answering ping.
                device.clock_probe_seq += 1
                clock_id = device.clock_probe_seq
                sent_us = em_clock.monotonic_us()
                device.clock_probe_sent[clock_id] = sent_us
                device.clock_probe_busy[clock_id] = device.is_busy()
                await device.send_control({
                    "type": "clock_probe",
                    "id": clock_id,
                    "controller_sent_us": sent_us,
                })

        ping_task = asyncio.create_task(ping_loop())
        if "wake_request_v1" not in capabilities:
            log.warning("[%s] unsupported firmware: wake_request_v1 is required", device_id)

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                msg_type = msg.get("type")

                if msg_type == "button":
                    await handle_button_event(device, msg)

                elif msg_type == "ambient_light":
                    # A step change in room light, sent by the device the
                    # moment it happens rather than waiting up to 30s for the
                    # stats tick — the timing IS the signal ("someone turned
                    # a light on"). The steady-state value still rides stats.
                    _lux = msg.get("lux")
                    if isinstance(_lux, int):
                        device.stats["ambientLux"] = _lux
                        ha_sidechannels.ambient_light(device_id, _lux)
                        log.info(f"[{device_id}] Ambient light → {_lux} lux")

                elif msg_type == "mute_state":
                    device.muted = msg.get("muted", False)
                    if device.muted and device.voice_lock.locked():
                        # Mute during an active turn terminates it — same
                        # cancel as the dot button, plus speaker_flush so
                        # any in-flight TTS goes silent immediately (the
                        # device shows the red ring the moment the button
                        # is pressed; audio carrying on would contradict
                        # it). The device guards its LED ring while muted,
                        # so the cancelled turn's LED cleanup can't clear
                        # the red ring.
                        log.info(
                            f"[{device_id}] Muted during active turn — "
                            f"cancelling"
                        )
                        device.cancel_event.set()
                        turn_engine.cancel_voice_turn(device_id, reason="muted")
                        await device.send_control({"type": "speaker_flush"})
                    await api._push_event({
                        "type":      "device_update",
                        "device_id": device_id,
                        "state":     {"muted": device.muted},
                    })
                    ha_sidechannels.mute_state(device_id, device.muted)

                elif msg_type == "volume_state":
                    # Device reports its current volume level (raw tinymix index).
                    # Convert to HA float, update in-memory state, persist to
                    # config so the value survives controller and device restarts.
                    raw_level = int(msg.get("level", 85))
                    device.volume = _device_level_to_ha(raw_level)
                    log.debug(
                        f"[{device_id}] volume_state: level={raw_level} "
                        f"→ {device.volume:.3f}"
                    )
                    # Persist — read-modify-write to avoid stomping other fields
                    stored_config = await loop.run_in_executor(
                        None, db.get_device_config, device_id
                    )
                    stored_config["startupVolume"] = raw_level
                    await loop.run_in_executor(
                        None, db.set_device_config, device_id, stored_config
                    )
                    # Notify ESPHome satellite so HA's media player entity updates
                    ha_sidechannels.volume(device_id, device.volume)

                elif msg_type == "stats":
                    device.stats = {
                        "cpuPct":        msg.get("cpuPct"),
                        "memUsedMb":     msg.get("memUsedMb"),
                        "memTotalMb":    msg.get("memTotalMb"),
                        "storageUsedMb": msg.get("storageUsedMb"),
                        "storageTotalMb":msg.get("storageTotalMb"),
                        "wifiRssi":      msg.get("wifiRssi"),
                        "wifiSsid":      msg.get("wifiSsid"),
                        # v7 link telemetry (firmware >= v2.9.6). This dict
                        # is an explicit allowlist, so any new device stat
                        # must be added HERE as well as in DeviceStats and
                        # record_device_stats — all three, or the field is
                        # silently dropped in the relay (2026-07-20).
                        "linkSpeedMbps": msg.get("linkSpeedMbps"),
                        "wifiFreqMhz":   msg.get("wifiFreqMhz"),
                        "wifiBssid":     msg.get("wifiBssid"),
                        "txBytes":       msg.get("txBytes"),
                        "rxBytes":       msg.get("rxBytes"),
                        "txErrors":      msg.get("txErrors"),
                        "txDropped":     msg.get("txDropped"),
                        "rxCrcErrors":   msg.get("rxCrcErrors"),
                        "ble":           msg.get("ble"),
                        # Thermals + CPU topology. coresOnline is not optional
                        # context: cpuPct is a share of ONLINE capacity, so the
                        # same work halves its percentage when hotplug adds a
                        # core. thermalCoreLimit < coresTotal means the thermal
                        # governor is capping capacity.
                        "cpuTempC":         msg.get("cpuTempC"),
                        "maxTempC":         msg.get("maxTempC"),
                        "coresOnline":      msg.get("coresOnline"),
                        "coresTotal":       msg.get("coresTotal"),
                        "thermalCoreLimit": msg.get("thermalCoreLimit"),
                        # Ambient light (TSL2540). None on a device without
                        # the sensor — 0 lux is a real reading (covered), so
                        # the two must not collapse into each other.
                        "ambientLux":       msg.get("ambientLux"),
                        # Device detector health, absent when its local runtime
                        # is unavailable.
                        "wakeDetector":  msg.get("wakeDetector"),
                    }
                    # Detector health summary rides the existing stats upsert.
                    _sh = msg.get("wakeDetector") or {}
                    if _sh:
                        if _sh.get("drops") or _sh.get("errors"):
                            log.warning(
	                                f"[{device_id}] wake detector fell behind: "
                                f"{_sh.get('drops')} frames dropped, "
                                f"{_sh.get('errors')} errors ({_sh.get('lastErr') or '-'}) "
	                                f"— detector health is degraded"
                            )
                    # msg["ble"] (scanner stats: scanning/advertsSeen/
                    # uniqueAddrs/bdAddr/hciErrors/restarts) already lands in
                    # device.stats via the general allowlist merge above, and
                    # the dashboard's Bluetooth panel reads it from there
                    # directly — no separate push needed now that there is no
                    # ESPHome-hosted diagnostic sensor to keep in sync.
                    # Ambient light straight through to HA's sensor entity.
                    # Rides the existing ~30s stats tick — light does not
                    # change fast enough to justify a channel of its own, and
                    # nothing per-frame is the standing rule here.
                    if "ambientLux" in msg:
                        ha_sidechannels.ambient_light(device_id, msg.get("ambientLux"))
                    # Fold into the persistent hourly rollup (CPU/RAM/storage/
                    # RSSI trends) — one cheap upsert per ~30s report. The
                    # last_seen refresh rides the same executor hop: a stats
                    # report IS proof of life, and without it last_seen only
                    # ever recorded the last *connect*.
                    # RTT is controller-measured, not relayed from the
                    # device message, so it is merged in here rather than
                    # coming through the allowlist above. drain_rtt() takes
                    # and resets the window accumulated since the last
                    # report, so no sample is counted twice.
                    _metrics = {**device.stats, **device.drain_rtt()}
                    def _persist_stats(_id=device_id, _s=_metrics, _shadow=_sh):
                        db.record_device_stats(_id, _s)
                        db.touch_device_seen(_id)
                        # Shadow counters ride the SAME executor hop rather
                        # than adding one: the whole point of summarising on
                        # the device is that on-device scoring costs the DB
                        # one upsert per 30s, not one per frame.
                        if _shadow:
                            db.bump_wake_counters(
                                _id,
                                dev_frames=int(_shadow.get("frames") or 0),
                                dev_drops=int(_shadow.get("drops") or 0),
                                dev_crossings=int(_shadow.get("crossings") or 0),
                                dev_max_score=float(_shadow.get("maxScore") or 0.0),
                                # v16: what a drop actually was — slowest
                                # inference vs longest frame gap.
                                dev_max_infer_ms=int(_shadow.get("maxInferMs") or 0),
                                dev_max_gap_ms=int(_shadow.get("maxGapMs") or 0),
                            )
                    await loop.run_in_executor(None, _persist_stats)
                    await api._push_event({
                        "type":      "device_update",
                        "device_id": device_id,
                        "state":     {
                            "stats": device.stats,
                            # Just the toggle now — there is no per-device
                            # ESPHome listener/mDNS state to report anymore;
                            # the scanner stats already ride device.stats.ble.
                            "bleProxy": {"enabled": device.ble_proxy_enabled},
                        },
                    })

                elif msg_type == "wifi_result":
                    # Outcome of a wifi_change. The device re-sends this
                    # until it sees a wifi_commit ack (a single send can
                    # vanish into a half-open TCP connection killed by the
                    # network switch), so: ALWAYS ack — on success the ack
                    # also finalises the change (deletes rollback backup +
                    # pending marker; a failed change already removed both,
                    # so the ack is a no-op there) — and log/record only
                    # the first arrival.
                    ok    = bool(msg.get("ok"))
                    ssid  = msg.get("ssid", "")
                    error = msg.get("error") or ""
                    st, duplicate = api.wifi_record_result(device_id, ok, ssid, error)
                    await device.send_control({"type": "wifi_commit"})
                    if not duplicate:
                        if ok:
                            log.info(f"[{device_id}] WiFi changed to \"{ssid}\" — committed")
                            db.log_device(device_id, "info", "device",
                                          f'WiFi changed to "{ssid}"')
                        else:
                            log.warning(f"[{device_id}] WiFi change to \"{ssid}\" "
                                        f"failed: {error}")
                            db.log_device(device_id, "warning", "device",
                                          f'WiFi change to "{ssid}" failed: {error}')
                        await api._push_event({
                            "type":      "device_update",
                            "device_id": device_id,
                            "state":     {"wifi": st},
                        })

                elif msg_type == "playback_stats":
                    # One report per completed speaker stream (firmware
                    # >= v2.9): periods played + mid-stream underruns.
                    # Attach to the turn persisted just before playback;
                    # consume last_turn_id so a later announcement's report
                    # can't overwrite a turn's stats. Reports with no
                    # pending turn (HA announcements, TTS after a controller
                    # restart) roll into the hourly counters instead.
                    # periods/underruns are read from the top level, which
                    # every firmware sends; "stats" carries the v2.9.6+
                    # delivery-margin fields and is absent on older devices.
                    periods   = int(msg.get("periods", 0))
                    underruns = int(msg.get("underruns", 0))
                    pstats    = msg.get("stats") or {}
                    # Release _run_post_turn_playback: this report IS the
                    # end of audio, and the ring clears on it rather than
                    # on a wall-clock guess.
                    device.playback_done.set()
                    # Delivery window: first speaker frame sent -> this
                    # report. The metric the 07-20 investigation lacked —
                    # "Streaming took Xs" times the socket write and reads
                    # ~0s however slowly the device is really being fed.
                    delivery_ms = -1
                    if device.playback_send_t0 is not None:
                        delivery_ms = int(
                            (loop.time() - device.playback_send_t0) * 1000
                        )
                        device.playback_send_t0 = None
                    turn_id   = device.last_turn_id
                    device.last_turn_id = None
                    if turn_id is not None:
                        await loop.run_in_executor(
                            None, db.set_turn_playback,
                            turn_id, periods, underruns, pstats,
                        )
                        if delivery_ms >= 0:
                            await loop.run_in_executor(
                                None, db.set_turn_delivery, turn_id,
                                device.playback_send_ms,
                                delivery_ms, device.playback_eq_ms,
                            )
                        for rec in reversed(device.turn_history):
                            if rec.get("turn_id") == turn_id:
                                rec["playback_periods"] = periods
                                rec["underruns"]        = underruns
                                break
                    else:
                        # The turn row may not exist yet (device buffers
                        # usually drain before the controller's drain sleep
                        # ends) — stash for _persist_turn to fold in. A
                        # displaced earlier stash was an announcement's:
                        # keep its underruns in the hourly counters.
                        prev = device.pending_playback_stats
                        # Indices 0-2 stay (ts, periods, underruns) so the
                        # existing consumer keeps working; 3-4 carry the v7
                        # delivery detail.
                        device.pending_playback_stats = (
                            asyncio.get_event_loop().time(), periods, underruns,
                            pstats, delivery_ms,
                        )
                        if prev and prev[2]:
                            await loop.run_in_executor(
                                None,
                                lambda: db.bump_wake_counters(
                                    device_id, underruns=prev[2]
                                ),
                            )
                    if underruns:
                        log.warning(
                            f"[{device_id}] Playback underruns: {underruns} "
                            f"in {periods} periods"
                            f"{f' (turn {turn_id})' if turn_id else ''}"
                        )

                elif msg_type == "wake_request":
                    # Device-only wake requests are admitted directly from the
                    # control plane; they must not wait for an idle PCM frame.
                    request_id = msg.get("requestId")
                    if not isinstance(request_id, str) or not request_id:
                        continue
                    deny_reason = _wake_request_admission_gate(device)
                    if deny_reason is not None:
                        await device.send_control({
                            "type": "wake_deny", "requestId": request_id,
                            "reason": deny_reason,
                        })
                        continue
                    # A barge candidate's request_id is not the one
                    # device.wake_request_id currently tracks (the active
                    # turn's) — retargeting it here is what lets
                    # _handle_wake_request's own "is this the request we are
                    # tracking" check pass, and _clear_wake_task below already
                    # handles both admit_barge outcomes correctly: on success
                    # it leaves wake_request_id pointing at this id (matching
                    # what admit_barge sets as device.barge_request_id, for
                    # the running turn loop to clear once the replacement
                    # turn ends); on denial neither is set, so it clears.
                    device.wake_request_id = request_id
                    task = asyncio.create_task(
                        _handle_wake_request(device, msg),
                        name=f"wake-admission-{device_id}",
                    )
                    device.wake_admission_task = task

                    def _clear_wake_task(done, d=device, t=task):
                        if d.wake_admission_task is t:
                            d.wake_admission_task = None
                            if d.barge_request_id != d.wake_request_id:
                                d.wake_request_id = None
                    task.add_done_callback(_clear_wake_task)

                elif msg_type == "stop_status":
                    model = msg.get("model")
                    ready = msg.get("ready")
                    device.stop_model_ready = bool(ready) and model == device.stop_model
                    if device.stop_model_ready:
                        log.info("[%s] stop word ready: %s", device_id, model)
                    else:
                        log.warning("[%s] stop word unavailable: %s", device_id,
                                    msg.get("error") or "model mismatch")

                elif msg_type == "wake_status":
                    checksum = msg.get("classifierMd5")
                    device.oww_model_ready = _wake_status_ready(device, msg)
                    device.oww_classifier_md5 = checksum if device.oww_model_ready else None
                    if not device.oww_model_ready:
                        log.warning("[%s] wake detector unavailable: %s", device_id,
                                    msg.get("error") or "model/checksum mismatch")

                elif msg_type == "wake_started":
                    try:
                        turn_id = int(msg.get("turnId"))
                    except (TypeError, ValueError):
                        continue
                    if not turn_engine.confirm_device_started(
                        device, msg.get("requestId"), turn_id
                    ):
                        log.warning("[%s] stale wake_started ignored", device_id)

                elif msg_type == "stop_detected":
                    # NOTE: run_in_executor takes positional args only — a
                    # kwargs call raises TypeError INSIDE the handler, which
                    # killed the whole control-plane dispatch loop for that
                    # message and tore the device's connections down. The
                    # stop itself never ran. Every bump below is positional.
                    try:
                        turn_id = int(msg.get("turnId"))
                        generation = int(msg.get("generation"))
                        score = float(msg.get("score"))
                        threshold = float(msg.get("threshold"))
                        age_ms = int(msg.get("ageMs"))
                        phase = msg.get("phase")
                    except (TypeError, ValueError):
                        # positional: dev_id, near_misses, near_miss_max,
                        # underruns, dev_frames, dev_drops, dev_crossings,
                        # dev_max_score, dev_max_infer_ms, dev_max_gap_ms,
                        # stops_accepted, stops_stale, stop_model_drops,
                        # stop_model_errors
                        await loop.run_in_executor(
                            None, db.bump_wake_counters,
                            device_id, 0, 0.0, 0, 0, 0, 0, 0.0, 0, 0, 0, 1, 0, 0,
                        )
                        log.warning("[%s] malformed stop_detected dropped", device_id)
                        continue
                    if (not device.stopword_capable or not device.stop_model_ready
                            or score < threshold or age_ms < 0):
                        log.warning("[%s] invalid stop_detected dropped", device_id)
                        continue
                    decision = device.stop_state.detected(
                        turn_id, generation, phase, loop.time()
                    )
                    if decision.action != "accept":
                        await loop.run_in_executor(
                            None, db.bump_wake_counters,
                            device_id, 0, 0.0, 0, 0, 0, 0, 0.0, 0, 0, 0, 1, 0, 0,
                        )
                        log.info("[%s] stop_detected ignored: %s", device_id, decision.reason)
                        continue
                    detection = {
                        "stop_model": device.stop_model,
                        "stop_score": score,
                        "stop_threshold": threshold,
                        "stop_phase": phase,
                        "stop_device_age_ms": age_ms,
                        "stop_cancel_ms": 0,
                    }
                    if phase == "timer" and turn_id == device.timer_stop_turn_id:
                        # Clear before stopping the runner: its asynchronous
                        # state callback must not overwrite `stopped` with the
                        # ordinary timer-completion outcome.
                        device.timer_stop_turn_id = None
                        accepted = await api.dismiss_timer_alarm(device_id)
                        if accepted:
                            await loop.run_in_executor(
                                None, db.update_turn, turn_id,
                                {"outcome": "stopped", **detection},
                            )
                    else:
                        accepted = await turn_engine.stop_voice_turn(
                            device_id, turn_id, detection
                        )
                    if accepted:
                        await loop.run_in_executor(
                            None, db.bump_wake_counters,
                            device_id, 0, 0.0, 0, 0, 0, 0, 0.0, 0, 0, 1, 0, 0, 0,
                        )
                    else:
                        await loop.run_in_executor(
                            None, db.bump_wake_counters,
                            device_id, 0, 0.0, 0, 0, 0, 0, 0.0, 0, 0, 0, 1, 0, 0,
                        )

                elif msg_type == "ble_adverts":
                    # BLE proxy data path — batched adverts from the
                    # device's passive scanner, forwarded to HA via the HACS
                    # integration's remote scanner (Phase 2b), gated on the
                    # same bleProxyEnabled flag that used to gate the
                    # ESPHome BT proxy's TCP listener.
                    if device.ble_proxy_enabled:
                        ha_sidechannels.ble_adverts(
                            device_id, msg.get("adverts") or []
                        )

                elif msg_type == "wifi_scan_result":
                    fut = device.wifi_scan_future
                    if fut is not None and not fut.done():
                        fut.set_result(msg)

                elif msg_type == "log":
                    level   = msg.get("level", "info")
                    message = msg.get("message", "")
                    # _push_log_event PERSISTS as well as pushing, so this must
                    # not also call db.log_device — doing both wrote every
                    # device log line twice, ~6ms apart, which is how half the
                    # device_logs table came to be duplicates. It also matters
                    # for the support bundle: thin_noise keeps the newest three
                    # [mem] lines per device, so duplication halved the readings
                    # a leak hunt actually gets.
                    #
                    # The removed call was a synchronous DB write on the event
                    # loop; _push_log_event does it in an executor.
                    await api._push_log_event(device_id, level, "device", message)

                elif msg_type == "pong":
                    # Solicited pong (carries our sequence id) -> an RTT
                    # sample. Unsolicited keepalive pongs have no id and are
                    # ignored here; pairing one with whatever ping happened
                    # to be outstanding would invent a measurement.
                    _seq = msg.get("id")
                    if _seq is not None:
                        _sent = device.ping_sent.pop(_seq, None)
                        _busy = device.ping_busy.pop(_seq, False)
                        if _sent is not None:
                            _rtt = int((loop.time() - _sent) * 1000)
                            device.record_rtt(_rtt, _busy)
                            if _rtt >= RTT_EXCURSION_MS:
                                log.info(
                                    f"[{device_id}] RTT excursion: {_rtt}ms "
                                    f"({'busy' if _busy else 'idle'})"
                                )
                    pass

                elif msg_type == "clock_probe":
                    _probe_id = msg.get("id")
                    _sent_us = device.clock_probe_sent.pop(_probe_id, None)
                    _busy = device.clock_probe_busy.pop(_probe_id, False)
                    if _sent_us is not None:
                        _received_us = em_clock.monotonic_us()
                        try:
                            accepted = device.clock_sync.update(
                                _sent_us,
                                int(msg["device_received_us"]),
                                int(msg["device_sent_us"]),
                                _received_us,
                            )
                        except (KeyError, TypeError, ValueError):
                            accepted = False
                        if accepted:
                            _rtt = int((_received_us - _sent_us) / 1000)
                            device.record_rtt(_rtt, _busy)
                            if device.clock_sync.synchronized:
                                log.debug(
                                    f"[{device_id}] clock synchronized: "
                                    f"offset={device.clock_sync.offset_us:.0f}us "
                                    f"drift={device.clock_sync.drift_ppm:.1f}ppm"
                                )
                else:
                    log.debug(
                        f"[{device_id}] Unknown control message: {msg_type}"
                    )

        finally:
            ping_task.cancel()

    except asyncio.TimeoutError:
        log.warning(f"[control] Registration timeout from {remote}")

    except websockets.exceptions.ConnectionClosed:
        pass

    except Exception as e:
        log.error(f"[control] Handler error: {e}")

    finally:
        if device:
            # Above the stale check on purpose: per-connection state, and
            # send_button_event resolves by device_id, so an orphaned timer
            # would fire a phantom tap at the replacement connection.
            device.tap_burst.cancel()
            admission = device.wake_admission_task
            if admission is not None and not admission.done():
                admission.cancel()
                await asyncio.gather(admission, return_exceptions=True)
            device.wake_admission_task = None
            if _devices.get(device.device_id) is not device:
                # A replacement connection has already registered for this
                # device_id — this socket is stale. Tearing down shared
                # per-device services here would rip them out from under the
                # live connection: on 2026-07-14 a stale close 4s after a
                # reconnect stopped Lounge's ESPHome listener, so HA's
                # redials hit connection-refused and every turn failed
                # no_ha for 11 hours until the next device bounce.
                log.info(
                    f"[control] Stale connection closed for "
                    f"{device.device_id} — replacement is active, keeping "
                    f"services up"
                )
            else:
                log.info(f"[control] Device disconnected: {device.device_id}")
                db.log_device(
                    device.device_id, "info", "controller", "Disconnected"
                )
                # Stamp the moment it went away, so "last seen" is exact for
                # an offline device rather than up to one stats report stale.
                db.touch_device_seen(device.device_id)
                _devices.pop(device.device_id, None)
                await api.notify_device_disconnected(device.device_id)
                em_player.device_gone(device.device_id)


# ─── Data plane handler ───────────────────────────────────────────────────────

async def _accept_capture_upload(device: Device, ws, completed) -> bool:
    """Validate live identity/privacy, durably store, then acknowledge."""
    metadata = completed.metadata
    model_name = metadata.get("model")
    wake_model = em_oww_models.prediction_key(device.oww_model)
    stop_model = em_oww_models.prediction_key(device.stop_model) if getattr(device, "stop_model", None) else None
    if model_name == wake_model:
        expected_model = wake_model
        expected_md5 = device.oww_classifier_md5
        enabled = device.save_wake_captures
    elif stop_model and model_name == stop_model:
        expected_model = stop_model
        expected_md5 = getattr(device, "stop_classifier_md5", None)
        # Stop captures use the dedicated flag; allow wake flag as fallback if
        # stop flag is not yet configured on older persisted configs.
        enabled = getattr(device, "save_stop_captures", False)
    else:
        return False
    if (not enabled
            or metadata.get("classifierMd5") != expected_md5
            or _devices.get(device.device_id) is not device
            or device.data_ws is not ws):
        return False
    name, created = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: em_training_captures.save_uploaded(
            expected_model, device.device_id, metadata, completed.pcm,
            with_status=True,
        ),
    )
    if name is None:
        return False
    # Re-check opt-in after durable commit: the flag may have flipped while
    # the blocking write was in flight.
    still_enabled = (
        device.save_wake_captures if model_name == wake_model
        else getattr(device, "save_stop_captures", False)
    )
    if not still_enabled:
        if created:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: em_training_captures.discard(expected_model, name),
            )
        return False
    if (_devices.get(device.device_id) is not device
            or device.data_ws is not ws):
        # The committed file is valid and is the retry rendezvous. Removing it
        # here can race a replacement socket that already deduplicated and
        # acknowledged the same capture.
        return False
    await device.send_control({
        "type": "capture_ack", "captureId": metadata["captureId"],
    })
    return True

async def handle_data(ws: WebSocketServerProtocol, secure: bool = False):
    device = None
    remote = ws.remote_address

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        msg = json.loads(raw)

        if msg.get("type") != "identify":
            log.warning(
                f"[data] First message from {remote} was not identify — closing"
            )
            await ws.close()
            return

        device_id = msg["device_id"]

        if not await _link_auth_ok(ws, device_id, secure, "data"):
            await ws.close()
            return

        for _ in range(20):
            device = _devices.get(device_id)
            if device is not None:
                break
            await asyncio.sleep(0.1)

        if device is None:
            log.warning(f"[data] Unknown device_id: {device_id} — closing")
            await ws.close()
            return

        device.data_ws = ws
        device.data_ready.set()
        log.info(f"[data] Data connection established: {device_id}")

        _frame_err_last = float("-inf")
        _frame_err_suppressed = 0
        capture_receiver = em_capture_upload.Receiver()
        async for raw in ws:
            try:
                if not isinstance(raw, bytes):
                    continue
                if raw and raw[0] in {
                    em_capture_upload.CAPTURE_BEGIN,
                    em_capture_upload.CAPTURE_PCM,
                    em_capture_upload.CAPTURE_END,
                }:
                    if not (device.save_wake_captures or getattr(device, "save_stop_captures", False)):
                        capture_receiver.reset()
                        continue
                    completed = capture_receiver.feed(raw)
                    if completed is None:
                        continue
                    await _accept_capture_upload(device, ws, completed)
                    continue
                if len(raw) <= MIC_HEADER_LEN:
                    continue
                if raw[0] != MIC_FRAME_TYPE:
                    continue
                if (len(raw) == MIC_HEADER_LEN + 1
                        and raw[MIC_HEADER_LEN] in (
                            VAD_END_TYPE, VAD_NO_SPEECH_TIMEOUT_TYPE)):
                    sentinel = (
                        turn_engine.VAD_SENTINEL_TIMEOUT
                        if raw[MIC_HEADER_LEN] == VAD_NO_SPEECH_TIMEOUT_TYPE
                        else turn_engine.VAD_SENTINEL_END
                    )
                    if not device.oww_paused.is_set():
                        continue
                    q = device.voice_queue
                    if q.full():
                        try:
                            q.get_nowait()
                            log.warning(
                                f"[{device.device_id}] queue full — dropped one "
                                "frame to deliver VAD sentinel"
                            )
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        q.put_nowait(sentinel)
                    except asyncio.QueueFull:
                        log.error(
                            f"[{device.device_id}] VAD sentinel lost — queue "
                            "still full after drain"
                        )
                    continue
                payload = raw[MIC_HEADER_LEN:]
                # Device-only wake detection owns idle microphone PCM. The
                # controller receives turn audio only after a matching grant.
                if not device.oww_paused.is_set():
                    continue
                q = device.voice_queue
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # Drop the OLDEST frame, not the newest — keeps the tail
                    # contiguous with real time for OWW and STT.
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                now = asyncio.get_event_loop().time()
                if now - _frame_err_last >= 5.0:
                    suffix = (
                        f"; {_frame_err_suppressed} similar frame errors "
                        "suppressed"
                        if _frame_err_suppressed else ""
                    )
                    _frame_err_last = now
                    _frame_err_suppressed = 0
                    log.exception(
                        f"[{device.device_id}] Data frame handler failed — "
                        f"frame dropped, connection kept{suffix}"
                    )
                else:
                    _frame_err_suppressed += 1

    except asyncio.TimeoutError:
        log.warning(f"[data] Identify timeout from {remote}")

    except websockets.exceptions.ConnectionClosed:
        pass

    except Exception as e:
        log.error(f"[data] Handler error: {e}")

    finally:
        if device:
            if device.data_ws is ws:
                device.data_ws = None
                device.data_ready.clear()
            log.info(f"[data] Data connection closed: {device.device_id}")


# ─── Router ───────────────────────────────────────────────────────────────────

# ─── Shell plane handler ──────────────────────────────────────────────────────

async def handle_shell(ws: WebSocketServerProtocol, path: str, secure: bool = False):
    import aiohttp as _aiohttp

    # Path may carry a query: /shell/{device_id}?pty=1 signals that the
    # device actually established a PTY session (it may have been requested
    # but failed to allocate — the device falls back to a plain pipe and
    # omits the flag). The dashboard needs the established mode, not the
    # requested one, to pick its input framing.
    device_id, _, query = path.removeprefix("/shell/").partition("?")
    pty_mode = "pty=1" in query
    if not device_id:
        log.warning("[shell] Missing device_id in path")
        await ws.close()
        return

    if not await _link_auth_ok(ws, device_id, secure, "shell"):
        await ws.close()
        return

    log.info(f"[shell] Device connected: {device_id} (pty={pty_mode})")

    done_future  = _shell_pending.get(device_id)
    dashboard_ws = _shell_dashboard.get(device_id)

    if done_future is None or done_future.done():
        log.warning(f"[shell] No pending shell request for {device_id} — closing")
        await ws.close()
        return

    if dashboard_ws is None:
        log.info(f"[shell] Programmatic session: {device_id}")
        done_future.set_result(ws)
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=300.0)
        except (asyncio.TimeoutError, Exception):
            pass
        log.info(f"[shell] Programmatic session ended: {device_id}")
        return

    log.info(f"[shell] Proxying: {device_id}")

    # Tell the dashboard which mode the device established before any
    # shell bytes flow: PTY sessions use framed input (0x00 stdin /
    # 0x01 resize) and emit terminal escape sequences; pipe sessions
    # (pre-PTY firmware) are raw both ways.
    try:
        await dashboard_ws.send_str(json.dumps({"type": "shell_meta", "pty": pty_mode}))
    except Exception:
        pass

    async def device_to_dashboard():
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    await dashboard_ws.send_bytes(msg)
                else:
                    await dashboard_ws.send_str(msg)
        except Exception:
            pass

    async def dashboard_to_device():
        try:
            async for msg in dashboard_ws:
                if msg.type == _aiohttp.WSMsgType.BINARY:
                    await ws.send(msg.data)
                elif msg.type == _aiohttp.WSMsgType.TEXT:
                    await ws.send(msg.data.encode())
                elif msg.type in (_aiohttp.WSMsgType.CLOSE,
                                  _aiohttp.WSMsgType.ERROR):
                    break
        except Exception:
            pass

    tasks = [
        asyncio.create_task(device_to_dashboard()),
        asyncio.create_task(dashboard_to_device()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        log.info(f"[shell] Session ended: {device_id}")
        if not done_future.done():
            done_future.set_result(None)

async def _route(ws: WebSocketServerProtocol, secure: bool):
    path = ws.request.path if hasattr(ws, "request") else getattr(ws, "path", "/")

    if path == "/control":
        await handle_control(ws, secure)
    elif path == "/data":
        await handle_data(ws, secure)
    elif path.startswith("/shell/"):
        await handle_shell(ws, path, secure)
    else:
        log.warning(f"Unknown WebSocket path: {path} from {ws.remote_address}")
        await ws.close()


async def router(ws: WebSocketServerProtocol):
    await _route(ws, secure=False)


async def router_tls(ws: WebSocketServerProtocol):
    await _route(ws, secure=True)


# ─── mDNS ─────────────────────────────────────────────────────────────────────

def _make_mdns_info(tls_active: bool) -> ServiceInfo:
    props = {"version": "1", "server": MDNS_NAME}
    if tls_active:
        # Devices holding the pushed CA dial wss://<addr>:<tls_port> instead
        # of the plain port. Absent property = pre-TLS controller → plain ws.
        props["tls_port"] = str(SERVER_TLS_PORT)
    return ServiceInfo(
        "_emcontroller._tcp.local.",
        f"{MDNS_NAME}._emcontroller._tcp.local.",
        addresses=[socket.inet_aton(SERVER_IP)],
        port=SERVER_PORT,
        properties=props,
        server=f"{MDNS_NAME}.local.",
    )


async def _mdns_refresh_loop(azc: AsyncZeroconf, info: ServiceInfo) -> None:
    while True:
        await asyncio.sleep(MDNS_REFRESH_INTERVAL)
        try:
            await azc.async_update_service(info)
            log.debug("mDNS registration refreshed")
        except Exception as e:
            log.warning(f"mDNS refresh failed: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def event_loop_lag_monitor(interval: float = 1.0,
                                 warn_ms: float = 250.0) -> None:
    """
    Watch for asyncio event-loop stalls.

    Sleeps for a known interval and measures the overshoot: if the loop is
    blocked by synchronous work, the wake-up is late by roughly the length
    of that block. Anything blocking the loop also delays speaker frames
    reaching the socket, so this is the controller-side counterpart to the
    device's buffer-margin metric — it answers "were we the ones who were
    late?" without needing a profiler attached.

    Costs one wake-up per second and logs only when a threshold is crossed;
    the running peak is exposed on /api/system/status.
    """
    global _loop_lag_peak_ms
    loop = asyncio.get_event_loop()
    next_cpu = 0.0
    while True:
        t0 = loop.time()
        await asyncio.sleep(interval)
        lag_ms = (loop.time() - t0 - interval) * 1000
        if lag_ms > _loop_lag_peak_ms:
            _loop_lag_peak_ms = lag_ms
        # Piggyback the controller's CPU sampling on this ticker rather than
        # starting a second one: it is one os.times() every 30s, and the
        # windowed CPU it feeds is only read when a support bundle is built.
        if loop.time() >= next_cpu:
            next_cpu = loop.time() + api.CPU_SAMPLE_INTERVAL_S
            api.sample_cpu()
        if lag_ms >= warn_ms:
            log.warning(
                f"[loop] event loop stalled {lag_ms:.0f}ms — "
                f"speaker sends and LED frames were delayed by this much"
            )


async def main():
    log.info(f"EchoMuse Controller {api.CONTROLLER_VERSION}")
    db.init(DB_PATH)
    auth.maybe_generate_bootstrap_token()
    em_player.init(
        get_device=_devices.get,
        notify_state=ha_sidechannels.media_state,
    )
    ha_sidechannels.init(_devices.get)

    runner = await api.create_runner(_devices, _shell_pending, _shell_dashboard)
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, API_PORT)
    await site.start()
    log.info(f"Dashboard + API listening on http://{SERVER_HOST}:{API_PORT}")

    release_task       = asyncio.create_task(api.release_poll_loop())
    session_prune_task = asyncio.create_task(api.session_prune_loop())
    loop_lag_task      = asyncio.create_task(event_loop_lag_monitor())

    # Device-link TLS: generate/load the CA + server cert. Failure to set
    # up TLS (missing cryptography package, unwritable dir) must never take
    # the plain listener down with it — the fleet lives on that during
    # rollout.
    tls_ctx = None
    if SERVER_TLS_PORT:
        try:
            tls_dir = em_pki.ensure_pki(DB_PATH)
            if tls_dir:
                tls_ctx = em_pki.server_ssl_context(tls_dir)
                api.set_tls_dir(tls_dir)
        except Exception as e:
            log.error(f"Device-link TLS setup failed — wss listener disabled: {e}")

    azc  = AsyncZeroconf()
    info = _make_mdns_info(tls_active=tls_ctx is not None)
    await azc.async_register_service(info, allow_name_change=True)
    log.info(
        f"mDNS advertising {MDNS_NAME}._emcontroller._tcp.local "
        f"→ {SERVER_IP}:{SERVER_PORT}"
        + (f" (tls_port={SERVER_TLS_PORT})" if tls_ctx else "")
    )
    mdns_task = asyncio.create_task(_mdns_refresh_loop(azc, info))

    log.info(f"WebSocket server starting on {SERVER_HOST}:{SERVER_PORT}")

    try:
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(websockets.serve(
                router,
                SERVER_HOST,
                SERVER_PORT,
                ping_interval=20,
                ping_timeout=10,
                max_size=10 * 1024 * 1024,
            ))
            if tls_ctx is not None:
                await stack.enter_async_context(websockets.serve(
                    router_tls,
                    SERVER_HOST,
                    SERVER_TLS_PORT,
                    ssl=tls_ctx,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=10 * 1024 * 1024,
                ))
                log.info(f"Device-link TLS (wss) listening on {SERVER_HOST}:{SERVER_TLS_PORT}")
            if REQUIRE_DEVICE_TLS:
                log.info("REQUIRE_DEVICE_TLS=1 — plain/tokenless device connections will be rejected")

            log.info("EchoMuse Controller ready — waiting for devices")
            await asyncio.Future()

    finally:
        release_task.cancel()
        session_prune_task.cancel()
        loop_lag_task.cancel()
        mdns_task.cancel()
        await azc.async_unregister_service(info)
        await azc.async_close()
        await runner.cleanup()
        log.info("EchoMuse Controller stopped")


if __name__ == "__main__":
    asyncio.run(main())
