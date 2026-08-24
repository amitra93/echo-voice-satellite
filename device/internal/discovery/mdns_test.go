package discovery

import (
	"context"
	"errors"
	"net"
	"reflect"
	"testing"
	"time"

	"github.com/grandcat/zeroconf"
)

func TestParseTLSPort(t *testing.T) {
	cases := []struct {
		name string
		txt  []string
		want int
	}{
		{"valid", []string{"foo=bar", "tls_port=8770"}, 8770},
		{"zero", []string{"tls_port=0"}, 0},
		{"negative", []string{"tls_port=-1"}, 0},
		{"too large", []string{"tls_port=65536"}, 0},
		{"malformed", []string{"tls_port=not-a-port"}, 0},
		{"absent", []string{"foo=bar"}, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := parseTLSPort(tc.txt); got != tc.want {
				t.Fatalf("parseTLSPort(%v) = %d, want %d", tc.txt, got, tc.want)
			}
		})
	}
}

func TestServerInfoFromEntryPrefersIPv4AndParsesTLS(t *testing.T) {
	entry := &zeroconf.ServiceEntry{
		HostName: "controller.local.",
		Port:     8767,
		Text:     []string{"tls_port=8770"},
		AddrIPv4: []net.IP{net.ParseIP("192.0.2.10")},
		AddrIPv6: []net.IP{net.ParseIP("2001:db8::10")},
	}
	var verified string

	info, ok := serverInfoFromEntry(entry, func(addr string) bool {
		verified = addr
		return true
	})

	if !ok {
		t.Fatal("serverInfoFromEntry rejected a valid IPv4 entry")
	}
	want := &ServerInfo{Host: "192.0.2.10", Port: 8767, Addr: "192.0.2.10:8767", TLSPort: 8770}
	if !reflect.DeepEqual(info, want) {
		t.Fatalf("info = %#v, want %#v", info, want)
	}
	if verified != "192.0.2.10:8767" {
		t.Fatalf("verification address = %q, want IPv4 address", verified)
	}
}

func TestServerInfoFromEntryUsesIPv6WhenIPv4Absent(t *testing.T) {
	entry := &zeroconf.ServiceEntry{
		Port:     8767,
		AddrIPv6: []net.IP{net.ParseIP("2001:db8::10")},
	}
	info, ok := serverInfoFromEntry(entry, func(string) bool { return true })

	if !ok || info.Host != "2001:db8::10" || info.Addr != "2001:db8::10:8767" {
		t.Fatalf("IPv6 info = %#v, accepted = %v", info, ok)
	}
}

func TestServerInfoFromEntryRejectsMissingAddressAndFailedVerification(t *testing.T) {
	if info, ok := serverInfoFromEntry(&zeroconf.ServiceEntry{Port: 8767}, func(string) bool { return true }); ok || info != nil {
		t.Fatalf("missing address returned info=%#v, ok=%v", info, ok)
	}
	entry := &zeroconf.ServiceEntry{Port: 8767, AddrIPv4: []net.IP{net.ParseIP("192.0.2.10")}}
	if info, ok := serverInfoFromEntry(entry, func(string) bool { return false }); ok || info != nil {
		t.Fatalf("failed verification returned info=%#v, ok=%v", info, ok)
	}
}

func TestFindServerRetriesWithCappedBackoff(t *testing.T) {
	oldBrowse, oldAfter := browseFunc, after
	t.Cleanup(func() { browseFunc, after = oldBrowse, oldAfter })

	var delays []time.Duration
	attempts := 0
	browseFunc = func(context.Context) (*ServerInfo, error) {
		attempts++
		if attempts < 6 {
			return nil, errors.New("not found")
		}
		return &ServerInfo{Addr: "192.0.2.10:8767"}, nil
	}
	closed := make(chan time.Time)
	close(closed)
	after = func(d time.Duration) <-chan time.Time {
		delays = append(delays, d)
		return closed
	}

	info, err := FindServer(context.Background())
	if err != nil || info == nil {
		t.Fatalf("FindServer() = %#v, %v; want success", info, err)
	}
	want := []time.Duration{5 * time.Second, 10 * time.Second, 20 * time.Second, 40 * time.Second, 60 * time.Second}
	if !reflect.DeepEqual(delays, want) {
		t.Fatalf("backoff delays = %v, want %v", delays, want)
	}
}

func TestFindServerStopsWhenContextIsCanceledDuringRetry(t *testing.T) {
	oldBrowse, oldAfter := browseFunc, after
	t.Cleanup(func() { browseFunc, after = oldBrowse, oldAfter })

	ctx, cancel := context.WithCancel(context.Background())
	browseFunc = func(context.Context) (*ServerInfo, error) {
		cancel()
		return nil, errors.New("not found")
	}

	_, err := FindServer(ctx)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("FindServer() error = %v, want context.Canceled", err)
	}
}

func TestFindServerReturnsImmediatelyForCanceledContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := FindServer(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("FindServer(canceled) error = %v, want context.Canceled", err)
	}
}

func TestFindServerOnceUsesBrowseSeam(t *testing.T) {
	oldBrowse := browseFunc
	t.Cleanup(func() { browseFunc = oldBrowse })
	want := &ServerInfo{Host: "192.0.2.10", Port: 8767, Addr: "192.0.2.10:8767"}
	browseFunc = func(got context.Context) (*ServerInfo, error) {
		if got == nil {
			t.Fatal("FindServerOnce passed a nil context")
		}
		return want, nil
	}

	got, err := FindServerOnce(context.Background())
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("FindServerOnce() = %#v, %v; want %#v", got, err, want)
	}
}
