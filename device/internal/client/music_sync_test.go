package client

import "testing"

type musicSyncReceiverFake struct {
	start, pcm, clear, end int
}

func (f *musicSyncReceiverFake) MusicSyncStart(uint32) bool { f.start++; return true }
func (f *musicSyncReceiverFake) MusicSyncPCM(uint32, uint32, int64, []byte) bool {
	f.pcm++
	return true
}
func (f *musicSyncReceiverFake) MusicSyncClear(uint32) bool { f.clear++; return true }
func (f *musicSyncReceiverFake) MusicSyncEnd(uint32) bool   { f.end++; return true }

func TestMusicSyncControlAndPCMCodec(t *testing.T) {
	for _, kind := range []byte{musicSyncStart, musicSyncClear, musicSyncEnd} {
		raw, err := encodeMusicSyncControl(kind, 7)
		if err != nil || len(raw) != 5 || raw[0] != kind {
			t.Fatalf("control kind %x: raw=%v err=%v", kind, raw, err)
		}
	}
	original := musicSyncPCMFrame{Generation: 7, Sequence: 9, TargetUs: -123, PCM: []byte{1, 0, 2, 0}}
	raw, err := encodeMusicSyncPCM(original)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := decodeMusicSyncPCM(raw)
	if err != nil || decoded.Generation != original.Generation || decoded.Sequence != original.Sequence || decoded.TargetUs != original.TargetUs || string(decoded.PCM) != string(original.PCM) {
		t.Fatalf("decoded=%+v err=%v", decoded, err)
	}
	raw[17] = 99
	if decoded.PCM[0] != 1 {
		t.Fatal("decoder did not own PCM payload")
	}
}

func TestMusicSyncStateRejectsStaleFrames(t *testing.T) {
	var state musicSyncStreamState
	if state.clear(1) || state.end(1) {
		t.Fatal("inactive stream accepted lifecycle operation")
	}
	if !state.start(1) || state.start(1) || state.start(0) {
		t.Fatal("invalid stream generation accepted")
	}
	if !state.accept(musicSyncPCMFrame{Generation: 1, Sequence: 2, PCM: []byte{0, 0}}) {
		t.Fatal("first PCM frame rejected")
	}
	if state.accept(musicSyncPCMFrame{Generation: 1, Sequence: 2, PCM: []byte{0, 0}}) || state.accept(musicSyncPCMFrame{Generation: 1, Sequence: 1, PCM: []byte{0, 0}}) {
		t.Fatal("duplicate or regressed sequence accepted")
	}
	if state.accept(musicSyncPCMFrame{Generation: 2, Sequence: 3, PCM: []byte{0, 0}}) {
		t.Fatal("wrong generation accepted")
	}
	if state.clear(2) || state.end(2) {
		t.Fatal("wrong generation accepted lifecycle operation")
	}
	if !state.clear(1) || !state.accept(musicSyncPCMFrame{Generation: 1, Sequence: 0, PCM: []byte{0, 0}}) || !state.end(1) || state.accept(musicSyncPCMFrame{Generation: 1, Sequence: 1, PCM: []byte{0, 0}}) {
		t.Fatal("stream lifecycle failed")
	}
}

func TestMusicSyncCodecRejectsBoundaries(t *testing.T) {
	if _, err := encodeMusicSyncControl(musicSyncStart, 0); err == nil {
		t.Fatal("zero generation accepted")
	}
	if _, err := encodeMusicSyncPCM(musicSyncPCMFrame{Generation: 1, PCM: []byte{0}}); err == nil {
		t.Fatal("odd PCM accepted")
	}
	if _, err := decodeMusicSyncPCM([]byte{musicSyncPCM}); err == nil {
		t.Fatal("truncated PCM accepted")
	}
}

func TestDispatchMusicSyncFrameRoutesLifecycleAndPCM(t *testing.T) {
	receiver := &musicSyncReceiverFake{}
	start, _ := encodeMusicSyncControl(musicSyncStart, 1)
	clear, _ := encodeMusicSyncControl(musicSyncClear, 1)
	end, _ := encodeMusicSyncControl(musicSyncEnd, 1)
	pcm, _ := encodeMusicSyncPCM(musicSyncPCMFrame{Generation: 1, Sequence: 1, TargetUs: 10, PCM: []byte{0, 0}})
	for _, frame := range [][]byte{start, pcm, clear, end} {
		if !dispatchMusicSyncFrame(frame, receiver) {
			t.Fatalf("frame was not dispatched: %x", frame)
		}
	}
	if receiver.start != 1 || receiver.pcm != 1 || receiver.clear != 1 || receiver.end != 1 {
		t.Fatalf("receiver calls = %+v", receiver)
	}
	if dispatchMusicSyncFrame([]byte{musicSyncPCM, 0}, receiver) {
		t.Fatal("malformed frame dispatched")
	}
}
