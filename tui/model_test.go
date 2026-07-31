package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/muesli/termenv"
)

func write(t *testing.T, path string, v any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(v)
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

func writeInbox(t *testing.T, relay, owner, fname string, m map[string]any) {
	t.Helper()
	write(t, filepath.Join(relay, owner, fname), m)
}

func laneLine(mid, frm, to, content string) string {
	env := map[string]any{
		"type": "message", "from": frm, "to": to, "chan": nil, "key": nil,
		"id": "env-" + mid, "ts": "2026-07-10T18:00:00Z", "seq": 0, "version": 1,
		"body": map[string]any{
			"id": mid, "from": frm, "to": to, "timestamp": "2026-07-10T18:00:00Z",
			"priority": "normal", "content": content, "state": "pending",
		},
	}
	b, _ := json.Marshal(env)
	return string(b)
}

func TestMessageMatches(t *testing.T) {
	m := Message{From: "carol", To: "#eng"}
	if !matches(Message{From: "carol", To: "dave"}, target{kind: targetAgent, value: "carol"}) {
		t.Fatal("from match")
	}
	if !matches(m, target{kind: targetChannel, value: "#eng"}) {
		t.Fatal("channel match")
	}
	if !matches(m, target{kind: targetAll}) {
		t.Fatal("all matches everything")
	}
	if matches(m, target{kind: targetAgent, value: "nobody"}) {
		t.Fatal("non-participant should not match")
	}
}

func TestFormatMessageRemoteMarker(t *testing.T) {
	remote := formatMessage(Message{From: "c", To: "d", Content: "hi", Via: "git"}, 0)
	local := formatMessage(Message{From: "c", To: "d", Content: "hi"}, 0)
	if !strings.Contains(remote, "«remote»") {
		t.Fatal("remote message should carry the marker")
	}
	if strings.Contains(local, "«remote»") {
		t.Fatal("local message must not")
	}
}

func TestModelRendersAndFilters(t *testing.T) {
	m := newModel("/relay", "", false, time.Second, "test", "console-1")
	var mi tea.Model = m
	mi, _ = mi.Update(tea.WindowSizeMsg{Width: 100, Height: 20})
	snap := Snapshot{
		Relay: "/relay",
		Messages: []Message{
			{ID: "1", From: "alice", To: "bob", Content: "hello bob", Timestamp: "2026-07-10T18:00:00Z"},
			{ID: "2", From: "carol", To: "dave", Content: "remote hi", Via: "git", Timestamp: "2026-07-10T18:00:01Z"},
		},
		Agents: []Agent{{ID: "alice", Live: true}, {ID: "carol", Remote: true}},
	}
	mi, _ = mi.Update(snapshotMsg(snap))
	view := mi.View()
	for _, want := range []string{"dispatch-tui", "alice", "carol", "hello bob", "«remote»"} {
		if !strings.Contains(view, want) {
			t.Fatalf("view missing %q", want)
		}
	}

	// tab moves the filter to the first agent (alice); the feed should then drop
	// carol→dave (alice is not a participant) but keep alice→bob.
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyTab})
	view = mi.View()
	if !strings.Contains(view, "hello bob") || strings.Contains(view, "remote hi") {
		t.Fatalf("agent filter didn't apply:\n%s", view)
	}
}

func TestModelGroupsPidsAndCollapsesOffline(t *testing.T) {
	relay := t.TempDir()
	var mi tea.Model = newModel(relay, "", false, time.Second, "test", "console-1")
	mi, _ = mi.Update(tea.WindowSizeMsg{Width: 90, Height: 20})
	snap := Snapshot{
		// one live pid of publicai; the traffic is from OTHER publicai pids +
		// an offline project — grouping must land it all under "publicai".
		Agents: []Agent{{ID: "publicai-1664385", Live: true}},
		Messages: []Message{
			{ID: "1", From: "publicai-1767991", To: "documents-9", Content: "hi", Timestamp: "2026-07-10T18:00:00Z"},
			{ID: "2", From: "publicai-3580621", To: "publicai-1767991", Content: "yo", Timestamp: "2026-07-10T18:00:01Z"},
			{ID: "3", From: "ghost-42", To: "documents-9", Content: "old", Timestamp: "2026-07-10T18:00:02Z"},
		},
	}
	mi, _ = mi.Update(snapshotMsg(snap))
	m := mi.(model)
	// publicai is live (grouped) and carries the traffic even though the live pid
	// itself sent nothing; ghost/documents are offline → behind the past group.
	var pub target
	for _, tg := range m.targets {
		if tg.value == "publicai" {
			pub = tg
		}
	}
	if !pub.live || pub.count == 0 {
		t.Fatalf("publicai should be live with traffic: %+v", pub)
	}
	hasPast := false
	for _, tg := range m.targets {
		if tg.kind == targetPastHeader {
			hasPast = true
			if tg.count < 2 { // ghost + documents
				t.Fatalf("expected offline projects in the past group, got %d", tg.count)
			}
		}
		if tg.kind == targetAgent && (tg.value == "ghost" || tg.value == "documents") {
			t.Fatalf("offline project %q should be collapsed, not top-level", tg.value)
		}
	}
	if !hasPast {
		t.Fatal("expected a collapsible past-sessions group")
	}
	// selecting live publicai shows its cross-pid traffic (was empty pre-grouping)
	for i, tg := range m.targets {
		if tg.value == "publicai" {
			m.selected = i
		}
	}
	m.refreshFeed()
	if got := m.vp.View(); !strings.Contains(got, "hi") || !strings.Contains(got, "yo") {
		t.Fatalf("live publicai filter should show its pids' traffic:\n%s", got)
	}
}

func TestTranscriptAccumulatesAcrossSnapshots(t *testing.T) {
	var mi tea.Model = newModel("/r", "", false, time.Second, "test", "c")
	mi, _ = mi.Update(tea.WindowSizeMsg{Width: 80, Height: 16})
	// snapshot 1 carries m1
	mi, _ = mi.Update(snapshotMsg(Snapshot{Messages: []Message{
		{ID: "m1", From: "alice-1", To: "bob-1", Content: "first", Timestamp: "2026-07-10T18:00:00Z", SortMS: 1},
	}}))
	// snapshot 2 no longer has m1 (its recipient acked → the file was deleted) but
	// brings m2. The transcript must KEEP m1 — that's the inbox→transcript shift.
	mi, _ = mi.Update(snapshotMsg(Snapshot{Messages: []Message{
		{ID: "m2", From: "alice-1", To: "bob-1", Content: "second", Timestamp: "2026-07-10T18:00:01Z", SortMS: 2},
	}}))
	m := mi.(model)
	if len(m.transcript) != 2 {
		t.Fatalf("transcript should retain the acked-away m1 plus m2, got %d", len(m.transcript))
	}
	view := m.vp.View()
	if !strings.Contains(view, "first") || !strings.Contains(view, "second") {
		t.Fatalf("a message deleted from the queue must persist in the transcript:\n%s", view)
	}
}

func TestFormatMessageWrapsNotTruncates(t *testing.T) {
	long := strings.TrimSpace(strings.Repeat("word ", 40)) // ~200 chars
	out := formatMessage(Message{From: "alice", To: "bob", Content: long}, 60)
	lines := strings.Split(out, "\n")
	if len(lines) < 3 {
		t.Fatalf("long content should wrap to several lines, got %d:\n%s", len(lines), out)
	}
	if strings.Contains(out, "…") {
		t.Fatal("wrapping must not truncate with an ellipsis")
	}
	for _, l := range lines {
		if lipgloss.Width(l) > 60 {
			t.Fatalf("wrapped line exceeds width 60 (w=%d): %q", lipgloss.Width(l), l)
		}
	}
	// every word survives (nothing dropped by the wrap)
	if got := strings.Count(out, "word"); got != 40 {
		t.Fatalf("expected all 40 words, got %d", got)
	}
}

func TestComposeAndSendThroughModel(t *testing.T) {
	relay := t.TempDir()
	var mi tea.Model = newModel(relay, "", false, time.Second, "test", "console-1")
	mi, _ = mi.Update(tea.WindowSizeMsg{Width: 80, Height: 12})
	mi, _ = mi.Update(snapshotMsg(Snapshot{Agents: []Agent{{ID: "bob", Live: true}}}))
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyTab})                             // select bob
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("i")})       // open compose
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("ship it")}) // type
	_, _ = mi.Update(tea.KeyMsg{Type: tea.KeyEnter})                            // send (writes to disk)

	files, _ := filepath.Glob(filepath.Join(relay, "bob", "*.json"))
	if len(files) != 1 {
		t.Fatalf("compose→send didn't reach bob: %d files", len(files))
	}
	data, _ := os.ReadFile(files[0])
	var msg Message
	json.Unmarshal(data, &msg)
	if msg.Content != "ship it" || msg.From != "console-1" || msg.To != "bob" {
		t.Fatalf("bad message from the compose flow: %+v", msg)
	}
}

func TestBroadcastRequiresDoubleConfirm(t *testing.T) {
	relay := t.TempDir()
	var mi tea.Model = newModel(relay, "", false, time.Second, "test", "console-1")
	mi, _ = mi.Update(tea.WindowSizeMsg{Width: 80, Height: 12})
	mi, _ = mi.Update(snapshotMsg(Snapshot{Agents: []Agent{{ID: "bob", Live: true}}}))
	// stay on "all traffic" (selected 0), compose, type, then enter → arms only
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("i")})
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("all hands")})
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyEnter})
	if fs, _ := filepath.Glob(filepath.Join(relay, "bob", "*.json")); len(fs) != 0 {
		t.Fatal("first enter on a broadcast must NOT send")
	}
	if !mi.(model).confirmBroadcast {
		t.Fatal("first enter should arm the broadcast confirm")
	}
	// second enter confirms and broadcasts (the send is a filesystem side effect)
	_, _ = mi.Update(tea.KeyMsg{Type: tea.KeyEnter})
	if fs, _ := filepath.Glob(filepath.Join(relay, "bob", "*.json")); len(fs) != 1 {
		t.Fatal("second enter should broadcast to the live agent")
	}
}

func TestBroadcastConfirmDisarmedByEdit(t *testing.T) {
	relay := t.TempDir()
	var mi tea.Model = newModel(relay, "", false, time.Second, "test", "console-1")
	mi, _ = mi.Update(tea.WindowSizeMsg{Width: 80, Height: 12})
	mi, _ = mi.Update(snapshotMsg(Snapshot{Agents: []Agent{{ID: "bob", Live: true}}}))
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("i")})
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("hi")})
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyEnter})                     // arm
	mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("!")}) // edit → disarm
	if mi.(model).confirmBroadcast {
		t.Fatal("editing after arming must disarm the confirm")
	}
	if fs, _ := filepath.Glob(filepath.Join(relay, "bob", "*.json")); len(fs) != 0 {
		t.Fatal("no send should have happened")
	}
}

func TestRosterScrollsToSelection(t *testing.T) {
	var mi tea.Model = newModel("/r", "", false, time.Second, "test", "c")
	mi, _ = mi.Update(tea.WindowSizeMsg{Width: 60, Height: 8}) // feed height 6 → 6 roster rows
	// distinct PROJECTS (each a different name so grouping doesn't collapse them),
	// each with a pid suffix that Project() strips.
	var agents []Agent
	for i := 0; i < 20; i++ {
		agents = append(agents, Agent{ID: fmt.Sprintf("proj%02dx-1", i), Live: true})
	}
	mi, _ = mi.Update(snapshotMsg(Snapshot{Agents: agents}))
	for i := 0; i < 20; i++ { // land the selection on the last project
		mi, _ = mi.Update(tea.KeyMsg{Type: tea.KeyTab})
	}
	view := mi.View()
	if !strings.Contains(view, "proj19x") {
		t.Fatalf("roster did not scroll to reveal the selection:\n%s", view)
	}
	if strings.Contains(view, "proj00x") {
		t.Fatal("top of a scrolled roster should be off-screen")
	}
}

// A live-agent row built for the selection path must be plain text (no ANSI):
// inner Render calls emit \x1b[0m resets that break the selStyle highlight
// background, so only the leading cell stayed highlighted (the reported bug).
// With a color profile forced, the rendered selection must be one contiguous
// highlight span — no interior reset before the trailing pad.
func TestSelectedRosterRowHighlightIsContiguous(t *testing.T) {
	restore := lipgloss.ColorProfile()
	lipgloss.SetColorProfile(termenv.TrueColor)
	defer lipgloss.SetColorProfile(restore)

	m := newModel("/r", "", false, time.Second, "test", "c")
	tg := target{kind: targetAgent, value: "alice", label: "alice", live: true}

	if strings.ContainsRune(m.rosterLine(tg, false), '\x1b') {
		t.Fatalf("selection row must be plain text, got ANSI: %q", m.rosterLine(tg, false))
	}
	if !strings.ContainsRune(m.rosterLine(tg, true), '\x1b') {
		t.Fatalf("unselected row should carry glyph styling, got plain: %q", m.rosterLine(tg, true))
	}
	// The selection path: selStyle over the plain, padded row → exactly one
	// reset, at the very end. An interior reset is the bug.
	sel := selStyle.Render(padRight(m.rosterLine(tg, false), rosterWidth-1))
	if n := strings.Count(sel, "\x1b[0m"); n != 1 {
		t.Fatalf("selected row should have one trailing reset, got %d: %q", n, sel)
	}
	if !strings.HasSuffix(sel, "\x1b[0m") {
		t.Fatalf("selection highlight must run to the row's end: %q", sel)
	}
}
