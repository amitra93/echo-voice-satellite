//go:build server

package speaker

import (
	"log"
	"math"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/internal/afeipc"
	deviceclock "github.com/wilbowes/EchoMuse/internal/clock"
	"github.com/wilbowes/EchoMuse/internal/sendspin"
)

const periodSize = 2048
const periodBytes = periodSize * 2 * 2 // 2 channels * 2 bytes = 8192

// The wire carries mono 48kHz. The shared renderer uses stereo internally;
// the AFE sink converts it back to the OpenSL player's mono stream.
const monoPeriodBytes = periodSize * 2 // 4096

// audioCh depth — the WS sender delivers at ~2× realtime, so its lead over
// playback grows ~1s per second played until it hits this cap. At 32
// (~1.3s) any WiFi stall longer than the accumulated lead drained the
// channel mid-stream (audible stutter on the far-AP device). 128 periods
// ≈ 5.5s (~1MB queued as stereo): most responses land on-device entirely
// within the first half of playback.
const audioChanDepth = 128

// primePeriods — playback holds on silence until this many periods are
// queued (or the stream's EOS has arrived, for clips shorter than the
// prime). Six periods provide ~256ms of opening-stall protection while
// keeping the initial response delay small. The controller's post-playback
// drain sleep allows for the delayed start (SPEAKER_PRIME_SECONDS).
const primePeriods = 6

var silencePeriod = make([]byte, periodBytes)

type PcmSpeaker struct {
	sink   audioSink
	stopCh chan struct{}
	// deadCh is closed by silenceLoop on any exit so a pump call can return
	// an error rather than block indefinitely waiting for a dead consumer.
	deadCh chan struct{}

	// Two independent playback planes, mixed before the OpenSL sink.
	//
	// voice carries TTS and announcements (0x02/0x03); music carries the
	// media player (0x04/0x05). They are separate so a voice turn can duck
	// the music instead of pausing it: the music feed runs LEAD_S=4s ahead
	// of realtime, so by the time a wake word fires the next four seconds
	// are already in this buffer, and audio that has left the controller
	// cannot be ducked by the controller. Holding both here means the music
	// keeps its full ~5.5s of link-stall protection AND the duck is instant.
	//
	// All the per-stream machinery — prime gate, discard-until-EOS,
	// underrun accounting, delivery instrumentation — lives in audioStream,
	// so both planes get the behaviour that was worked out on the single
	// one rather than a simplified copy.
	voice *audioStream
	music *audioStream
	// musicSync is the timestamp-paced music plane. It remains separate from
	// the legacy arrival-paced music stream for backward compatibility.
	musicSync *scheduledMusic
	// musicClock is the scheduled plane's presentation timeline: one rendered
	// period of playback time per rendered period, at the measured DAC rate.
	musicClock *scheduledClock
	// musicSyncLogTick counts periods for the periodic [music] telemetry line;
	// only ever touched by silenceLoop, so it needs no synchronisation.
	musicSyncLogTick int
	musicClockTick   int
	// musicStereo is the reused L=R period for the scheduled plane, owned by
	// the ALSA goroutine.
	musicStereo []byte
	oc          chainState

	// duckTarget is the gain applied to MUSIC while it plays under a voice
	// turn, Q15. Written from the control plane (SetDuck) and read by the
	// renderer goroutine every period, hence atomic; the ramp toward it lives in
	// the Mixer, which is single-consumer and needs no synchronisation.
	duckTarget atomic.Int32
	// musicMuted is controlled by Music Assistant's Sendspin player mute. It
	// only suppresses the music plane; privacy mute remains owned by Server.
	musicMuted atomic.Bool
	mixer      Mixer

	// levelTap, when non-nil, receives the RMS level (0..1, normalized
	// int16 full-scale) of every period as it is pumped to ALSA — real
	// audio carries its measured level, silence reports 0. Drives the
	// energy-reactive LED ring ("meter" led_anim pattern): tapping at the
	// ALSA write means the throb tracks what is audible right now, not
	// what PumpPeriod queued ~5.5s of buffer ago. Fixed at construction
	// like echoTap (silenceLoop races any later setter) and must be fast
	// and non-blocking: it runs on the ALSA pump goroutine.
	levelTap func(rms float64)

	// statsCb, when set, receives one StreamStats once per completed
	// stream — at EOS consume in silenceLoop, flush included. Unlike echoTap
	// it fires at most once per response, so a setter (OnStreamStats) is
	// fine: silenceLoop reads it under statsMu only on that cold path.
	statsMu sync.Mutex
	statsCb func(StreamStats)
}

type audioSink interface {
	Pump([]byte) error
	Close()
}

type volumeSink interface {
	SetVolume(level int) error
}

// OnStreamStats registers a per-stream stats callback, reported once when a
// stream reaches its EOS. Invoked on its own goroutine so a slow consumer
// (network send) can never stall the ALSA pump. Safe to call any time
// after New.
func (p *PcmSpeaker) OnStreamStats(cb func(StreamStats)) {
	p.statsMu.Lock()
	p.statsCb = cb
	p.statsMu.Unlock()
}

// SetVolume applies the device volume through Android's STREAM_MUSIC policy.
func (p *PcmSpeaker) SetVolume(level int) {
	if sink, ok := p.sink.(volumeSink); ok {
		if err := sink.SetVolume(level); err != nil {
			log.Printf("OpenSL player volume set failed: %v", err)
		}
	}
}

// NewAFEPcmSpeaker keeps the existing renderer and swaps only its presentation
// sink. The paired client has already opened capture and playback together.
func NewAFEPcmSpeaker(c *afeipc.Client, _ func([]byte), levelTap func(rms float64)) (*PcmSpeaker, error) {
	s := &PcmSpeaker{stopCh: make(chan struct{}), deadCh: make(chan struct{}), levelTap: levelTap, sink: newAFESink(c)}
	s.voice = newAudioStream(audioChanDepth, s.deadCh)
	s.music = newAudioStream(audioChanDepth, s.deadCh)
	s.musicSync = &scheduledMusic{}
	s.musicClock = newScheduledClock(deviceclock.NowUs)
	s.duckTarget.Store(unityGain)
	s.mixer.SetGainImmediate(unityGain)
	go s.silenceLoop()
	return s, nil
}

// Init satisfies the shared Speaker interface. The paired AFE helper has
// already opened playback before this renderer is constructed.
func (p *PcmSpeaker) Init() error { return nil }

// silenceLoop is the renderer path: every period, take what each plane has
// to offer, mix them, and pump. When neither has audio it pumps silence,
// which is what paces this loop — OpenSL blocks in Pump at realtime rate, so
// there is no timer anywhere and no busy-waiting.
//
// It replaced a blocking select on a single channel. Two planes cannot be
// waited on that way without one starving the other, and mixing needs both
// in hand at the same instant; a non-blocking take from each, with silence
// as the floor, keeps the pacing property that made the original correct.
//
// Closes deadCh on any exit so blocked pump callers unblock with an error
// rather than hanging.
func (p *PcmSpeaker) silenceLoop() {
	defer close(p.deadCh)
	var musicRenderMax, outputChainMax, pumpMax time.Duration
	for {
		select {
		case <-p.stopCh:
			return
		default:
		}

		var voice, music []byte
		logMusic := false
		renderStart := time.Now()
		if p.voice.ready(primePeriods) {
			voice = p.voice.take()
		} else if p.voice.playing {
			p.report(p.voice.drained(), "voice")
		}
		if p.musicSync.hasStream() {
			p.musicClockTick++
			if p.musicClockTick >= 24 {
				p.musicClockTick = 0
				p.sampleMusicClock()
			}
			// Advance is the timeline, so it is called once per rendered
			// period and its result is what the renderer positions against.
			// Stride carries the DAC-vs-source rate ratio into the render so
			// the read phase moves smoothly across the period rather than
			// being corrected in a step at each boundary.
			mono := p.musicSync.render(
				p.musicClock.Advance(periodSize), p.musicClock.Stride(), periodSize)
			if mono != nil {
				if !p.musicMuted.Load() {
					p.musicStereo = toStereoInto(p.musicStereo, mono)
					music = p.musicStereo
				}
			}
			// Periodic scheduled-music telemetry (~5s at 42.7ms periods). This
			// plane had none, so a click or drift was invisible: buffer depth
			// answers starvation, interp/late/drift answer the rendering.
			p.musicSyncLogTick++
			if p.musicSyncLogTick >= 117 {
				p.musicSyncLogTick = 0
				logMusic = true
			}
		} else if p.music.ready(primePeriods) {
			p.musicSyncLogTick = 0
			p.musicClockTick = 0
			music = p.music.take()
		} else if p.music.playing {
			p.report(p.music.drained(), "music")
		} else {
			p.musicSyncLogTick = 0
			p.musicClockTick = 0
		}
		if elapsed := time.Since(renderStart); elapsed > musicRenderMax {
			musicRenderMax = elapsed
		}

		// The ring's level must be measured BEFORE mixing: Mix sums into the
		// voice buffer in place, so afterwards there is no voice-only signal
		// left to measure.
		var level float64
		if voice != nil && p.levelTap != nil {
			level = periodRMS(voice)
		}

		out := p.mixer.Mix(voice, music, p.duckTarget.Load())
		if out == nil {
			out = silencePeriod
		}
		chainStart := time.Now()
		out = p.applyOutputChain(out)
		if elapsed := time.Since(chainStart); elapsed > outputChainMax {
			outputChainMax = elapsed
		}

		// VOICE only, deliberately. The meter
		// ring visualises the RESPONSE; feeding it the mix made the ring
		// throb along to ducked music before the response had started, which
		// reads as the device doing something it is not.
		if p.levelTap != nil {
			p.levelTap(level)
		}
		pumpStart := time.Now()
		if err := p.sink.Pump(out); err != nil {
			log.Printf("silenceLoop: pump error: %v", err)
			return
		}
		if elapsed := time.Since(pumpStart); elapsed > pumpMax {
			pumpMax = elapsed
		}
		if logMusic {
			st := p.musicSync.stats()
			log.Printf("[music] sched buffered=%dms underruns=%d holds=%d "+
				"late=%d startErr=%dus drift=%dus(max %dus) rate=%.1f resync=%d "+
				"renderMax=%s chainMax=%s pumpMax=%s",
				st.BufferedMs, st.Underruns, st.Holds, st.LateSamples,
				st.StartErrorUs, st.LastErrorUs, st.MaxDriftUs,
				p.musicClock.Rate(), p.musicClock.Resyncs(),
				musicRenderMax.Round(time.Microsecond), outputChainMax.Round(time.Microsecond), pumpMax.Round(time.Microsecond))
			musicRenderMax, outputChainMax, pumpMax = 0, 0, 0
		}
	}
}

// report logs and forwards a completed stream's stats. A nil st is an
// underrun, which is counted inside the stream and only logged here.
func (p *PcmSpeaker) report(st *StreamStats, plane string) {
	if st == nil {
		log.Printf("[speaker] UNDERRUN: %s channel drained mid-stream — injecting silence", plane)
		return
	}
	log.Printf("[speaker] %s stream complete — returning to silence "+
		"(periods=%d underruns=%d minDepth=%d primeWait=%dms recvSpan=%dms maxGap=%dms)",
		plane, st.Periods, st.Underruns, st.MinDepth,
		st.PrimeWaitMs, st.RecvSpanMs, st.MaxGapMs)
	// Only the voice plane reports upstream: the controller attaches these
	// to the turn that produced them (device.last_turn_id), and a music
	// stream ending would overwrite a turn's delivery figures with a song's.
	if plane != "voice" || st.Periods == 0 {
		return
	}
	p.statsMu.Lock()
	cb := p.statsCb
	p.statsMu.Unlock()
	if cb != nil {
		go cb(*st)
	}
}

// toStereo converts a mono S16 wire period into the stereo frames the ALSA
// device requires. The stereo config is an I2S/codec-path constraint, not a
// wire one — shipping two identical channels would double bandwidth for
// nothing on links that are already marginal.
// toStereoInto duplicates L=R into a caller-owned buffer. The scheduled music
// path renders every 42.7ms on the ALSA writer, so allocating a period here
// was 8KB of garbage per period on the one goroutine with a hard deadline.
func toStereoInto(dst, data []byte) []byte {
	n := len(data) / 2
	if cap(dst) < n*4 {
		dst = make([]byte, n*4)
	}
	period := dst[:n*4]
	for i := 0; i < n; i++ {
		lo, hi := data[i*2], data[i*2+1]
		period[i*4+0], period[i*4+1] = lo, hi // L
		period[i*4+2], period[i*4+3] = lo, hi // R
	}
	return period
}

func toStereo(data []byte) []byte {
	n := len(data) / 2
	period := make([]byte, n*4)
	for i := 0; i < n; i++ {
		lo, hi := data[i*2], data[i*2+1]
		period[i*4+0], period[i*4+1] = lo, hi // L
		period[i*4+2], period[i*4+3] = lo, hi // R
	}
	return period
}

// PumpPeriod queues one period of VOICE audio (TTS, announcements). Called by
// the WS client for each incoming 0x02 frame. Blocks until the ALSA loop has
// consumed a slot (rate-limiting to playback speed), or returns an error if
// that loop has died — preventing an infinite block on a dead consumer.
func (p *PcmSpeaker) PumpPeriod(data []byte) error {
	_, err := p.voice.pump(toStereo(data), len(data))
	return err
}

// PumpMusic queues one period of MUSIC audio (0x04). Identical handling to
// the voice plane — it is a full stream with its own prime gate, buffer and
// discard semantics, not a lesser one — but kept separate so a voice turn
// can duck it rather than stopping it.
func (p *PcmSpeaker) PumpMusic(data []byte) error {
	_, err := p.music.pump(toStereo(data), len(data))
	return err
}

// SetDuck sets the gain applied to music while it plays under voice, in dB of
// attenuation (0 = no ducking). The change is ramped by the mixer rather than
// applied at once, because a gain step at a period boundary is a click — and
// it would land on exactly the moment the user started speaking.
func (p *PcmSpeaker) SetDuck(db float64) {
	p.duckTarget.Store(DuckGain(db))
}

// SetMusicMuted changes only the music plane. MA's player mute must never
// call the privacy-mute path: that would stop the microphone, change LEDs,
// and persist a privacy state for what was merely a music command.
func (p *PcmSpeaker) SetMusicMuted(muted bool) { p.musicMuted.Store(muted) }

// IsStreaming reports whether a VOICE stream is currently mid-flight.
//
// Added for on-device wake word scoring: while the speaker is playing, the
// controller lowers its wake threshold to bargeInThreshold, because echo at the
// mic is ~25dB louder than the person and speech-over-TTS scores are depressed.
// The device has to know when it is playing in order to mirror that, or it
// disagrees with the controller on every barge-in.
//
// Music deliberately does NOT count here. It is a quieter, continuous bed
// rather than a response the user is talking over, and the controller applies
// the same rule on its side (wake-over-music scores against bargeInThreshold
// only when barge-in is enabled). Reporting music as "streaming" would drop
// the device's bar for as long as a song plays.
func (p *PcmSpeaker) IsStreaming() bool { return p.voice.isActive() }

// IsPlayingMusic reports whether a music stream is mid-flight.
func (p *PcmSpeaker) IsPlayingMusic() bool { return p.music.isActive() }

// EndStream marks the in-flight voice stream complete (0x03). Always arrives
// after every 0x02 period of that stream has been handed to PumpPeriod —
// frames are processed sequentially on the read loop — so by the time the
// ALSA loop drains the buffer the flag is already set.
func (p *PcmSpeaker) EndStream() { p.voice.endStream() }

// EndMusicStream marks the in-flight music stream complete (0x05).
func (p *PcmSpeaker) EndMusicStream() { p.music.endStream() }

// Flush cuts a playing VOICE stream immediately (barge-in). Two parts:
//  1. Drain the buffer — kills up to ~5.5s already queued on-device.
//  2. Arm discarding (if a stream is mid-flight) — subsequent periods of
//     this stream are dropped until its EOS arrives. Necessary because the
//     controller writes the whole response into the WebSocket ahead of
//     playback: at barge time the rest of the stream is already in TCP
//     buffers and would refill the channel right after the drain (the
//     pre-2026-07-08 version drained only, and playback resumed after a
//     ~1.3s skip). The controller sends the EOS on the cancel path too, so
//     the discard always terminates.
//
// Up to PeriodCount ALSA periods (~170ms) already handed to the hardware
// still play — cutting those needs a stream restart, which costs more in
// click/pop than it saves.
func (p *PcmSpeaker) Flush() { p.voice.flush() }

// FlushMusic stops music the same way. This is now reserved for the user
// GENUINELY stopping or pausing playback: a voice turn ducks instead, which
// is the whole point of holding the two planes apart. Flushing here would
// throw away the buffered audio that makes ducking instant, and on a
// non-seekable stream that audio cannot be recovered.
func (p *PcmSpeaker) FlushMusic() { p.music.flush() }

// MusicSyncStart begins a timestamp-paced stream and clears any previous
// scheduled data. It does not touch the legacy music plane.
func (p *PcmSpeaker) MusicSyncStart(generation uint32) bool {
	started := p.musicSync.start(generation)
	if started {
		// A stream's timestamps are relative to the device clock, so the
		// timeline is re-anchored to it rather than continuing from wherever
		// the previous stream's period count had reached.
		p.musicClock.Reset()
		p.sampleMusicClock()
	}
	return started
}

func (p *PcmSpeaker) MusicSyncPCM(generation, sequence uint32, targetUs int64, pcm []byte) bool {
	return p.musicSync.push(generation, sequence, targetUs, pcm)
}

func (p *PcmSpeaker) MusicSyncClear(generation uint32) bool {
	return p.musicSync.clear(generation)
}

func (p *PcmSpeaker) MusicSyncEnd(generation uint32) bool {
	return p.musicSync.end(generation)
}

// sampleMusicClock reads ALSA's status snapshot outside the renderer's lock.
// It only refines the measured DAC rate — the timeline itself is counted, not
// derived from hw_ptr — so a failed read is ignored: it costs a little rate
// accuracy, which is a slow drift, never a discontinuity.
func (p *PcmSpeaker) sampleMusicClock() {
	b, err := os.ReadFile(statusPath(cardNr, deviceNr))
	if err != nil {
		return
	}
	status, err := sendspin.ParsePcmStatus(b)
	if err == nil {
		p.musicClock.Observe(status)
	}
}

// MusicSyncStats returns timestamp-rendering diagnostics without exposing the
// scheduled queue or allowing callers to mutate its state.
func (p *PcmSpeaker) MusicSyncStats() (underruns, holds, lateSamples uint64, startErrorUs, lastErrorUs, maxDriftUs, bufferedMs int64) {
	st := p.musicSync.stats()
	return st.Underruns, st.Holds, st.LateSamples,
		st.StartErrorUs, st.LastErrorUs, st.MaxDriftUs, st.BufferedMs
}

// Close stops the helper-backed renderer and releases its OpenSL player.
func (p *PcmSpeaker) Close() {
	close(p.stopCh)
	if p.sink != nil {
		p.sink.Close()
	}
	log.Println("PcmSpeaker closed")
}

// periodRMS computes the RMS level of a stereo S16LE period, normalized to
// 0..1 of int16 full-scale. Left channel only, every 4th frame — the wire
// is mono duplicated L=R and the LED meter needs ~2 significant digits at
// ~23Hz, so 512 of 2048 frames is plenty at a quarter of the cost. Runs on
// the ALSA pump goroutine: no allocation, integer accumulate.
func periodRMS(period []byte) float64 {
	if len(period) < 4 {
		return 0
	}
	var sum uint64
	n := 0
	// Stereo frame = 4 bytes (L16+R16); step 4 frames = 16 bytes.
	for i := 0; i+1 < len(period); i += 16 {
		s := int64(int16(uint16(period[i]) | uint16(period[i+1])<<8))
		sum += uint64(s * s)
		n++
	}
	if n == 0 {
		return 0
	}
	return math.Sqrt(float64(sum)/float64(n)) / 32768.0
}
