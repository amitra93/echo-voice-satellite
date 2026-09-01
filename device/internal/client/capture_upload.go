package client

import (
	"context"
	"crypto/md5"
	"encoding/binary"
	"encoding/json"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/wakeword/capture"
)

const (
	frameTypeCaptureBegin  = byte(0x10)
	frameTypeCapturePCM    = byte(0x11)
	frameTypeCaptureEnd    = byte(0x12)
	captureProtocolVersion = byte(1)
	captureFrameBytes      = 2560
	captureWriteWait       = 250 * time.Millisecond
)

func encodeCaptureBegin(metadata capture.Metadata) ([]byte, error) {
	payload, err := json.Marshal(metadata)
	if err != nil {
		return nil, err
	}
	frame := make([]byte, 4+len(payload))
	frame[0], frame[1] = frameTypeCaptureBegin, captureProtocolVersion
	binary.BigEndian.PutUint16(frame[2:4], uint16(len(payload)))
	copy(frame[4:], payload)
	return frame, nil
}

func encodeCapturePCM(index uint16, pcm []byte) []byte {
	frame := make([]byte, 3+len(pcm))
	frame[0] = frameTypeCapturePCM
	binary.BigEndian.PutUint16(frame[1:3], index)
	copy(frame[3:], pcm)
	return frame
}

func encodeCaptureEnd(chunks uint16, pcm []byte) []byte {
	return encodeCaptureEndDigest(chunks, uint32(len(pcm)), md5.Sum(pcm))
}

func encodeCaptureEndDigest(chunks uint16, bytes uint32, sum [md5.Size]byte) []byte {
	frame := make([]byte, 23)
	frame[0] = frameTypeCaptureEnd
	binary.BigEndian.PutUint16(frame[1:3], chunks)
	binary.BigEndian.PutUint32(frame[3:7], bytes)
	copy(frame[7:], sum[:])
	return frame
}

func (d *DataClient) captureBlocked() bool {
	d.wakeMu.Lock()
	defer d.wakeMu.Unlock()
	return d.wakeGranted
}

func (d *DataClient) writeCaptureFrame(conn *websocket.Conn, frame []byte) error {
	d.connMu.Lock()
	defer d.connMu.Unlock()
	if d.conn != conn {
		return context.Canceled
	}
	conn.SetWriteDeadline(time.Now().Add(captureWriteWait))
	if err := conn.WriteMessage(websocket.BinaryMessage, frame); err != nil {
		conn.Close()
		return err
	}
	return nil
}

func (d *DataClient) runCaptureUploader(ctx context.Context, done <-chan struct{}, conn *websocket.Conn) {
	if d.captureManager == nil {
		return
	}
	defer d.captureManager.RetryInFlight()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-done:
			return
		case <-d.captureManager.Notify():
		case <-ticker.C:
		}
		if d.captureBlocked() {
			continue
		}
		item := d.captureManager.NextReady()
		if item == nil {
			continue
		}
		begin, err := encodeCaptureBegin(item.Metadata)
		if err != nil || !d.captureManager.Current(item) || d.writeCaptureFrame(conn, begin) != nil {
			d.captureManager.Retry(item.Metadata.CaptureID)
			continue
		}
		failed := false
		chunkCount, current := d.captureManager.ChunkCount(item)
		if !current || chunkCount > int(^uint16(0)) {
			d.captureManager.Retry(item.Metadata.CaptureID)
			continue
		}
		digest := md5.New()
		bytes := uint32(0)
		for index := 0; index < chunkCount; index++ {
			pcm, current := d.captureManager.Chunk(item, index)
			if !current {
				failed = true
				break
			}
			for d.captureBlocked() {
				select {
				case <-ctx.Done():
					d.captureManager.Retry(item.Metadata.CaptureID)
					return
				case <-done:
					d.captureManager.Retry(item.Metadata.CaptureID)
					return
				case <-time.After(20 * time.Millisecond):
				}
			}
			if d.writeCaptureFrame(conn, encodeCapturePCM(uint16(index), pcm)) != nil {
				failed = true
				break
			}
			digest.Write(pcm)
			bytes += uint32(len(pcm))
		}
		var sum [md5.Size]byte
		copy(sum[:], digest.Sum(nil))
		if failed || !d.captureManager.Current(item) || d.writeCaptureFrame(conn, encodeCaptureEndDigest(uint16(chunkCount), bytes, sum)) != nil {
			d.captureManager.Retry(item.Metadata.CaptureID)
			continue
		}
	}
}
