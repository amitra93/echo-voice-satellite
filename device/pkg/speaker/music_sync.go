package speaker

// MusicSyncReceiver is implemented by speakers that can schedule timestamped
// music. It is optional so old speaker implementations and host fakes remain
// compatible with the legacy PumpMusic plane.
type MusicSyncReceiver interface {
	MusicSyncStart(generation uint32) bool
	MusicSyncPCM(generation, sequence uint32, targetUs int64, pcm []byte) bool
	MusicSyncClear(generation uint32) bool
	MusicSyncEnd(generation uint32) bool
}
