package bluetooth

import (
	"os"
	"testing"
	"time"
)

func testAdvert(addr string, data ...byte) Advert {
	return Advert{Addr: addr, Rssi: -60, Data: data}
}

func TestBatchKeyIncludesAddressAndPayload(t *testing.T) {
	base := testAdvert("AA:BB:CC:DD:EE:FF", 1, 2)
	if batchKey(base) == batchKey(testAdvert("11:22:33:44:55:66", 1, 2)) {
		t.Fatal("different addresses produced the same batch key")
	}
	if batchKey(base) == batchKey(testAdvert(base.Addr, 2, 1)) {
		t.Fatal("different payloads produced the same batch key")
	}
	if batchKey(base) != batchKey(testAdvert(base.Addr, 1, 2)) {
		t.Fatal("identical adverts produced different batch keys")
	}
}

func TestIngestCoalescesDuplicatesAndFlushesLatestAdvert(t *testing.T) {
	var batches [][]Advert
	s := NewScanner(func(batch []Advert) { batches = append(batches, batch) })
	first := testAdvert("AA", 1, 2)
	latest := first
	latest.Rssi = -30
	second := testAdvert("BB", 3)

	s.ingest([]Advert{first, latest, second})
	stats := s.Stats()
	if stats.AdvertsSeen != 3 || stats.UniqueAddrs != 2 {
		t.Fatalf("stats after ingest = %#v, want seen=3 unique=2", stats)
	}
	if len(batches) != 0 {
		t.Fatal("ingest flushed before the batch threshold")
	}

	s.flush()
	if len(batches) != 1 || len(batches[0]) != 2 {
		t.Fatalf("batches = %#v, want one batch with two adverts", batches)
	}
	foundLatest := false
	for _, advert := range batches[0] {
		if advert.Addr == latest.Addr && advert.Rssi == latest.Rssi {
			foundLatest = true
		}
	}
	if !foundLatest {
		t.Fatalf("coalesced batch did not retain latest RSSI: %#v", batches[0])
	}
	if got := s.Stats().AdvertsSent; got != 2 {
		t.Fatalf("AdvertsSent = %d, want 2", got)
	}
	s.flush()
	if len(batches) != 1 {
		t.Fatal("empty flush invoked the callback")
	}
}

func TestIngestFlushesAtDistinctAdvertCountLimit(t *testing.T) {
	flushes := 0
	s := NewScanner(func(batch []Advert) {
		flushes++
		if len(batch) != flushCount {
			t.Errorf("threshold batch len = %d, want %d", len(batch), flushCount)
		}
	})
	adverts := make([]Advert, flushCount)
	for i := range adverts {
		adverts[i] = testAdvert(string(rune('A'+i)), byte(i))
	}
	s.ingest(adverts)
	if flushes != 1 {
		t.Fatalf("threshold ingest flushes = %d, want 1", flushes)
	}
	if got := s.Stats().AdvertsSent; got != flushCount {
		t.Fatalf("AdvertsSent = %d, want %d", got, flushCount)
	}
}

func TestStatsExpiresOldUniqueAddresses(t *testing.T) {
	s := NewScanner(nil)
	s.uniqueMu.Lock()
	s.unique["old"] = time.Now().Add(-uniqueWindow - time.Second)
	s.unique["fresh"] = time.Now()
	s.uniqueMu.Unlock()
	s.scanning.Store(true)
	s.advertsSeen.Store(4)
	s.advertsSent.Store(2)
	s.hciErrors.Store(1)
	s.restarts.Store(3)
	s.bdAddrMu.Lock()
	s.bdAddr = "00:11:22:33:44:55"
	s.bdAddrMu.Unlock()

	stats := s.Stats()
	if stats.UniqueAddrs != 1 || !stats.Scanning || stats.AdvertsSeen != 4 || stats.AdvertsSent != 2 || stats.HciErrors != 1 || stats.Restarts != 3 || stats.BdAddr == "" {
		t.Fatalf("Stats() = %#v", stats)
	}
}

func TestEnvIntDefault(t *testing.T) {
	key := "EM_TEST_BLE_INT"
	old, had := os.LookupEnv(key)
	t.Cleanup(func() {
		if had {
			_ = os.Setenv(key, old)
		} else {
			_ = os.Unsetenv(key)
		}
	})

	_ = os.Unsetenv(key)
	if got := envIntDefault(key, 320); got != 320 {
		t.Fatalf("unset env = %d, want 320", got)
	}
	_ = os.Setenv(key, "640")
	if got := envIntDefault(key, 320); got != 640 {
		t.Fatalf("valid env = %d, want 640", got)
	}
	_ = os.Setenv(key, "0")
	if got := envIntDefault(key, 320); got != 320 {
		t.Fatalf("zero env = %d, want default 320", got)
	}
	_ = os.Setenv(key, "bad")
	if got := envIntDefault(key, 320); got != 320 {
		t.Fatalf("invalid env = %d, want default 320", got)
	}
}

func TestSetEnabledIsIdempotentAndStopsFailedSession(t *testing.T) {
	s := NewScanner(nil)
	s.SetEnabled(false)
	s.SetEnabled(true)
	s.SetEnabled(true)
	s.SetEnabled(false)
	s.SetEnabled(false)
	if s.Stats().Scanning {
		t.Fatal("scanner remained active after disable")
	}
}
