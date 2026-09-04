package client

import (
	"context"
	"crypto/md5"
	"encoding/binary"
	"encoding/json"
	"runtime"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/wakeword/capture"
)

var captureYield = runtime.Gosched

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

// notifyChan returns mgr's Notify channel, or nil if mgr is nil. A nil
// channel case in a select never becomes ready, which is exactly the right
// behaviour for "this manager was never configured" — some tests construct a
// DataClient directly and only ever set captureManager.
func notifyChan(mgr *capture.Manager) <-chan struct{} {
	if mgr == nil {
		return nil
	}
	return mgr.Notify()
}

func (d *DataClient) runCaptureUploader(ctx context.Context, done <-chan struct{}, conn *websocket.Conn) {
	if d.captureManager == nil && d.stopCaptureManager == nil {
		return
	}
	defer func() {
		if d.captureManager != nil {
			d.captureManager.RetryInFlight()
		}
		if d.stopCaptureManager != nil {
			d.stopCaptureManager.RetryInFlight()
		}
	}()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-done:
			return
		case <-notifyChan(d.captureManager):
		case <-notifyChan(d.stopCaptureManager):
		case <-ticker.C:
		}
		if d.captureBlocked() {
			continue
		}
		// Exactly one capture may be in flight on the wire at a time: the
		// controller's receiver (em_capture_upload.Receiver) is a single
		// BEGIN/PCM.../END state machine per connection, so interleaving
		// frames from a wake capture and a stop capture would corrupt both.
		// Wake is simply tried first; captures are debounced to roughly one
		// every few seconds per manager, so there is no real fairness
		// concern in practice.
		if d.captureManager != nil && d.uploadNextCapture(ctx, done, conn, d.captureManager) {
			continue
		}
		if d.stopCaptureManager != nil {
			d.uploadNextCapture(ctx, done, conn, d.stopCaptureManager)
		}
	}
}

// uploadNextCapture drains and uploads at most one ready capture from mgr.
// Returns whether an item was found at all — a capture that had to be
// retried still counts, so the caller does not fall through to the other
// manager and start a second capture's BEGIN mid-upload.
func (d *DataClient) uploadNextCapture(ctx context.Context, done <-chan struct{}, conn *websocket.Conn, mgr *capture.Manager) bool {
	item := mgr.NextReady()
	if item == nil {
		return false
	}
	begin, err := encodeCaptureBegin(item.Metadata)
	if err != nil || !mgr.Current(item) || d.writeCaptureFrame(conn, begin) != nil {
		mgr.Retry(item.Metadata.CaptureID)
		return true
	}
	failed := false
	chunkCount, current := mgr.ChunkCount(item)
	if !current || chunkCount > int(^uint16(0)) {
		mgr.Retry(item.Metadata.CaptureID)
		return true
	}
	digest := md5.New()
	bytes := uint32(0)
	for index := 0; index < chunkCount; index++ {
		pcm, current := mgr.Chunk(item, index)
		if !current {
			failed = true
			break
		}
		for d.captureBlocked() {
			select {
			case <-ctx.Done():
				mgr.Retry(item.Metadata.CaptureID)
				return true
			case <-done:
				mgr.Retry(item.Metadata.CaptureID)
				return true
			case <-time.After(20 * time.Millisecond):
			}
		}
		if d.writeCaptureFrame(conn, encodeCapturePCM(uint16(index), pcm)) != nil {
			failed = true
			break
		}
		digest.Write(pcm)
		bytes += uint32(len(pcm))
		captureYield()
	}
	var sum [md5.Size]byte
	copy(sum[:], digest.Sum(nil))
	if failed || !mgr.Current(item) || d.writeCaptureFrame(conn, encodeCaptureEndDigest(uint16(chunkCount), bytes, sum)) != nil {
		mgr.Retry(item.Metadata.CaptureID)
	}
	return true
}
