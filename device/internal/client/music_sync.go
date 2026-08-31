package client

import (
	"encoding/binary"
	"errors"
)

const (
	musicSyncStart     byte = 0x06
	musicSyncPCM       byte = 0x07
	musicSyncClear     byte = 0x08
	musicSyncEnd       byte = 0x09
	musicSyncPCMHeader      = 1 + 4 + 4 + 8
)

type musicSyncPCMFrame struct {
	Generation uint32
	Sequence   uint32
	TargetUs   int64
	PCM        []byte
}

func validMusicSyncGeneration(generation uint32) bool { return generation != 0 }

func encodeMusicSyncControl(kind byte, generation uint32) ([]byte, error) {
	if kind != musicSyncStart && kind != musicSyncClear && kind != musicSyncEnd {
		return nil, errors.New("invalid music-sync control type")
	}
	if !validMusicSyncGeneration(generation) {
		return nil, errors.New("invalid music-sync generation")
	}
	frame := make([]byte, 5)
	frame[0] = kind
	binary.BigEndian.PutUint32(frame[1:], generation)
	return frame, nil
}

func encodeMusicSyncPCM(frame musicSyncPCMFrame) ([]byte, error) {
	if !validMusicSyncGeneration(frame.Generation) || len(frame.PCM) == 0 || len(frame.PCM)%2 != 0 {
		return nil, errors.New("invalid music-sync PCM frame")
	}
	raw := make([]byte, musicSyncPCMHeader+len(frame.PCM))
	raw[0] = musicSyncPCM
	binary.BigEndian.PutUint32(raw[1:5], frame.Generation)
	binary.BigEndian.PutUint32(raw[5:9], frame.Sequence)
	binary.BigEndian.PutUint64(raw[9:17], uint64(frame.TargetUs))
	copy(raw[musicSyncPCMHeader:], frame.PCM)
	return raw, nil
}

func decodeMusicSyncPCM(raw []byte) (musicSyncPCMFrame, error) {
	if len(raw) <= musicSyncPCMHeader || raw[0] != musicSyncPCM || len(raw[musicSyncPCMHeader:])%2 != 0 {
		return musicSyncPCMFrame{}, errors.New("invalid music-sync PCM frame")
	}
	frame := musicSyncPCMFrame{
		Generation: binary.BigEndian.Uint32(raw[1:5]),
		Sequence:   binary.BigEndian.Uint32(raw[5:9]),
		TargetUs:   int64(binary.BigEndian.Uint64(raw[9:17])),
		PCM:        append([]byte(nil), raw[musicSyncPCMHeader:]...),
	}
	if !validMusicSyncGeneration(frame.Generation) {
		return musicSyncPCMFrame{}, errors.New("invalid music-sync generation")
	}
	return frame, nil
}

func decodeMusicSyncControl(raw []byte) (byte, uint32, error) {
	if len(raw) != 5 || (raw[0] != musicSyncStart && raw[0] != musicSyncClear && raw[0] != musicSyncEnd) {
		return 0, 0, errors.New("invalid music-sync control frame")
	}
	generation := binary.BigEndian.Uint32(raw[1:])
	if !validMusicSyncGeneration(generation) {
		return 0, 0, errors.New("invalid music-sync generation")
	}
	return raw[0], generation, nil
}

type musicSyncStreamState struct {
	generation   uint32
	active       bool
	hasSequence  bool
	lastSequence uint32
}

func (s *musicSyncStreamState) start(generation uint32) bool {
	if !validMusicSyncGeneration(generation) || (s.generation != 0 && generation <= s.generation) {
		return false
	}
	s.generation, s.active, s.hasSequence = generation, true, false
	return true
}

func (s *musicSyncStreamState) clear(generation uint32) bool {
	if !s.active || generation != s.generation {
		return false
	}
	s.hasSequence = false
	return true
}

func (s *musicSyncStreamState) accept(frame musicSyncPCMFrame) bool {
	if !s.active || frame.Generation != s.generation || (s.hasSequence && frame.Sequence <= s.lastSequence) {
		return false
	}
	s.lastSequence, s.hasSequence = frame.Sequence, true
	return true
}

func (s *musicSyncStreamState) end(generation uint32) bool {
	if !s.active || generation != s.generation {
		return false
	}
	s.active, s.hasSequence = false, false
	return true
}
