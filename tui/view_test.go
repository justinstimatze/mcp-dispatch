package main

import (
	"testing"
	"time"
)

// withLocal pins time.Local for the duration of a test so local-time formatting
// is deterministic regardless of the machine's zone, then restores it.
func withLocal(t *testing.T, name string) {
	t.Helper()
	loc, err := time.LoadLocation(name)
	if err != nil {
		t.Skipf("zone %s unavailable: %v", name, err)
	}
	prev := time.Local
	time.Local = loc
	t.Cleanup(func() { time.Local = prev })
}

func TestClockLocal(t *testing.T) {
	t.Run("utc drops fractional seconds", func(t *testing.T) {
		withLocal(t, "UTC")
		if got := clockLocal("2026-07-18T15:08:35.419994Z"); got != "15:08:35" {
			t.Fatalf("got %q, want 15:08:35", got)
		}
		if got := clockLocal("2026-07-19T01:05:32Z"); got != "01:05:32" {
			t.Fatalf("got %q, want 01:05:32", got)
		}
	})

	t.Run("converts to local zone", func(t *testing.T) {
		withLocal(t, "America/New_York") // EDT = UTC-4 in July
		// 01:05:32 UTC on Jul 19 is 21:05:32 the previous evening in NY.
		if got := clockLocal("2026-07-19T01:05:32Z"); got != "21:05:32" {
			t.Fatalf("got %q, want 21:05:32", got)
		}
	})

	t.Run("fallbacks", func(t *testing.T) {
		if got := clockLocal(""); got != "--:--:--" {
			t.Fatalf("empty: got %q, want --:--:--", got)
		}
		// Not RFC3339 (no offset) → parse fails, fallback strips date + fractional.
		if got := clockLocal("2026-01-01T09:10:11.5"); got != "09:10:11" {
			t.Fatalf("no-offset: got %q, want 09:10:11", got)
		}
		// No 'T' at all → returned verbatim.
		if got := clockLocal("nonsense"); got != "nonsense" {
			t.Fatalf("nonsense: got %q, want nonsense", got)
		}
	})
}

func TestDateLocal(t *testing.T) {
	withLocal(t, "America/New_York")
	// 01:05 UTC Jul 19 → 21:05 EDT Jul 18, so the divider day is Jul 18.
	want := time.Date(2026, 7, 18, 21, 5, 32, 0, time.Local).Format("Mon Jan 2 2006")
	if got := dateLocal("2026-07-19T01:05:32Z"); got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
	if got := dateLocal(""); got != "" {
		t.Fatalf("empty: got %q, want empty", got)
	}
	if got := dateLocal("garbage"); got != "" {
		t.Fatalf("garbage: got %q, want empty", got)
	}
}
