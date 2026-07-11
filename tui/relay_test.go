package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
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

func TestExpandUser(t *testing.T) {
	home, _ := os.UserHomeDir()
	if got := expandUser("~/x"); got != filepath.Join(home, "x") {
		t.Fatalf("expandUser: %s", got)
	}
	if got := expandUser("/abs/p"); got != "/abs/p" {
		t.Fatalf("abs path mangled: %s", got)
	}
}

func TestRelayDirPrecedence(t *testing.T) {
	t.Setenv("MCP_DISPATCH_DIR", "/from/env")
	if got := RelayDir(fileConfig{DispatchDir: "/from/cfg"}); got != "/from/env" {
		t.Fatalf("env should win: %s", got)
	}
	os.Unsetenv("MCP_DISPATCH_DIR")
	t.Setenv("DISPATCH_DIR", "")
	cfg := fileConfig{}
	cfg.Dispatch.DispatchDir = "/from/table"
	if got := RelayDir(cfg); got != "/from/table" {
		t.Fatalf("table fallback: %s", got)
	}
	top := fileConfig{DispatchDir: "/top"}
	top.Dispatch.DispatchDir = "/table"
	if got := RelayDir(top); got != "/top" {
		t.Fatalf("top-level should win over table: %s", got)
	}
}

func TestGitRepoDirMissing(t *testing.T) {
	os.Unsetenv("MCP_DISPATCH_GIT_REPO")
	if got := GitRepoDir(fileConfig{}); got != "" {
		t.Fatalf("unconfigured git → empty, got %s", got)
	}
	if got := GitRepoDir(func() fileConfig { c := fileConfig{}; c.Git.RepoDir = "/no/such/dir"; return c }()); got != "" {
		t.Fatalf("nonexistent repo → empty, got %s", got)
	}
}

func TestFlockHeld(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "p.json")
	os.WriteFile(p, []byte("{}"), 0o644)

	if flockHeld(p) {
		t.Fatal("nobody holds it → false")
	}
	if flockHeld(filepath.Join(dir, "ghost.json")) {
		t.Fatal("missing file → false")
	}
	f, _ := os.Open(p)
	defer f.Close()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	if !flockHeld(p) {
		t.Fatal("held lock → true")
	}
}

func TestScanGitSkipsNonMessage(t *testing.T) {
	repo := t.TempDir()
	os.MkdirAll(filepath.Join(repo, "lanes"), 0o755)
	atom := `{"type":"atom","from":"ettle","id":"e","ts":"t","body":{"s":1}}`
	lines := atom + "\n" + laneLine("m1", "carol", "dave", "hi") + "\n"
	os.WriteFile(filepath.Join(repo, "lanes", "carol.jsonl"), []byte(lines), 0o644)

	got := scanGit(repo)
	if len(got) != 1 || got[0].ID != "m1" {
		t.Fatalf("only the message record should surface: %+v", got)
	}
	if !got[0].Remote() {
		t.Fatal("git record must be tagged remote")
	}
}

func TestMergeDedupAndOrder(t *testing.T) {
	inbox := []Message{{ID: "a", Timestamp: "2026-07-10T18:00:02Z", sortMS: 2, Via: "git"}}
	git := []Message{
		{ID: "a", Timestamp: "2026-07-10T18:00:02Z", sortMS: 2, Via: "git"}, // dup of inbox
		{ID: "b", Timestamp: "2026-07-10T18:00:01Z", sortMS: 1, Via: "git"}, // lane-only, earlier
	}
	got := mergeMessages(inbox, git)
	if len(got) != 2 {
		t.Fatalf("dedup failed: %d", len(got))
	}
	if got[0].ID != "b" || got[1].ID != "a" {
		t.Fatalf("chronological order wrong: %s,%s", got[0].ID, got[1].ID)
	}
}

func TestRosterLiveShadowsRemote(t *testing.T) {
	relay := t.TempDir()
	// alice is live-local AND has a remote roster entry → shown once, as live.
	write(t, filepath.Join(relay, ".presence", "alice.json"),
		map[string]any{"agent_id": "alice", "pid": 1, "channels": []string{"eng"}})
	write(t, filepath.Join(relay, ".remote", "alice.json"),
		map[string]any{"agent_id": "alice", "via": "git"})
	write(t, filepath.Join(relay, ".remote", "zed.json"),
		map[string]any{"agent_id": "zed", "via": "git"})
	// hold alice's presence lock so she reads as live
	f, _ := os.Open(filepath.Join(relay, ".presence", "alice.json"))
	defer f.Close()
	syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)

	ag := roster(relay, "")
	var alice, zed int
	for _, a := range ag {
		if a.ID == "alice" {
			alice++
			if !a.Live {
				t.Fatal("alice should be live")
			}
		}
		if a.ID == "zed" {
			zed++
			if !a.Remote {
				t.Fatal("zed should be remote")
			}
		}
	}
	if alice != 1 || zed != 1 {
		t.Fatalf("alice=%d zed=%d (want 1,1)", alice, zed)
	}
	// live agents sort before remote-only ones
	if ag[0].ID != "alice" {
		t.Fatalf("live should lead: %+v", ag)
	}
}

func TestMessageMatches(t *testing.T) {
	m := Message{From: "carol", To: "#eng"}
	if !(Message{From: "carol", To: "dave"}).matches(target{kind: targetAgent, value: "carol"}) {
		t.Fatal("from match")
	}
	if !m.matches(target{kind: targetChannel, value: "#eng"}) {
		t.Fatal("channel match")
	}
	if !m.matches(target{kind: targetAll}) {
		t.Fatal("all matches everything")
	}
	if m.matches(target{kind: targetAgent, value: "nobody"}) {
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

func TestProjectStripsPid(t *testing.T) {
	cases := map[string]string{
		"aipotluck-dualpath-2833067": "aipotluck-dualpath",
		"agent-service-879152":       "agent-service",
		"mcp-dispatch-1207946":       "mcp-dispatch",
		"publicai-1767991":           "publicai",
		"alice":                      "alice", // no pid suffix → unchanged
		"#eng":                       "#eng",  // channel passthrough
		"all":                        "all",
	}
	for in, want := range cases {
		if got := project(in); got != want {
			t.Fatalf("project(%q)=%q want %q", in, got, want)
		}
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
		{ID: "m1", From: "alice-1", To: "bob-1", Content: "first", Timestamp: "2026-07-10T18:00:00Z", sortMS: 1},
	}}))
	// snapshot 2 no longer has m1 (its recipient acked → the file was deleted) but
	// brings m2. The transcript must KEEP m1 — that's the inbox→transcript shift.
	mi, _ = mi.Update(snapshotMsg(Snapshot{Messages: []Message{
		{ID: "m2", From: "alice-1", To: "bob-1", Content: "second", Timestamp: "2026-07-10T18:00:01Z", sortMS: 2},
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
	// each with a pid suffix that project() strips.
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

func TestSendDM(t *testing.T) {
	relay := t.TempDir()
	snap := Snapshot{Agents: []Agent{{ID: "bob", Live: true}}}
	n, err := Send(relay, "console-1", "bob", "hi bob", snap, "normal")
	if err != nil || n != 1 {
		t.Fatalf("send DM: n=%d err=%v", n, err)
	}
	files, _ := filepath.Glob(filepath.Join(relay, "bob", "*.json"))
	if len(files) != 1 {
		t.Fatalf("expected 1 inbox file, got %d", len(files))
	}
	data, _ := os.ReadFile(files[0])
	var m Message
	json.Unmarshal(data, &m)
	if m.From != "console-1" || m.To != "bob" || m.Content != "hi bob" || m.State != "pending" {
		t.Fatalf("bad message: %+v", m)
	}
	if !strings.HasSuffix(files[0], ".json") || strings.Contains(filepath.Base(files[0]), ".tmp") {
		t.Fatalf("filename scheme wrong: %s", files[0])
	}
}

func TestSendChannelFansOutToLiveSubscribers(t *testing.T) {
	relay := t.TempDir()
	snap := Snapshot{Agents: []Agent{
		{ID: "carol", Live: true, Channels: []string{"eng"}},
		{ID: "dave", Live: true, Channels: []string{"eng"}},
		{ID: "erin", Live: true, Channels: []string{"ops"}},      // not subscribed
		{ID: "console-1", Live: true, Channels: []string{"eng"}}, // sender excluded
	}}
	n, err := Send(relay, "console-1", "#eng", "team update", snap, "normal")
	if err != nil || n != 2 {
		t.Fatalf("channel fan-out should hit 2 subscribers: n=%d err=%v", n, err)
	}
	for _, who := range []string{"carol", "dave"} {
		if fs, _ := filepath.Glob(filepath.Join(relay, who, "*.json")); len(fs) != 1 {
			t.Fatalf("%s should have 1 message", who)
		}
	}
	if fs, _ := filepath.Glob(filepath.Join(relay, "erin", "*.json")); len(fs) != 0 {
		t.Fatal("non-subscriber erin should get nothing")
	}
	if fs, _ := filepath.Glob(filepath.Join(relay, "console-1", "*.json")); len(fs) != 0 {
		t.Fatal("sender should not receive its own channel post")
	}
}

func TestSendRejectsBadTarget(t *testing.T) {
	if _, err := Send(t.TempDir(), "console-1", "../escape", "x", Snapshot{}, "normal"); err == nil {
		t.Fatal("path-traversal target must be rejected")
	}
}

func TestSendFanoutSkipsMaliciousAgentID(t *testing.T) {
	// A crafted presence file could carry a traversal in agent_id; Send's "all"
	// fan-out must not turn it into a path outside the relay.
	relay := t.TempDir()
	snap := Snapshot{Agents: []Agent{
		{ID: "bob", Live: true},
		{ID: "../../etc/evil", Live: true}, // hostile id
	}}
	n, err := Send(relay, "console-1", "all", "hi", snap, "normal")
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	if n != 1 {
		t.Fatalf("only the valid recipient should get the message, got %d", n)
	}
	if fs, _ := filepath.Glob(filepath.Join(relay, "bob", "*.json")); len(fs) != 1 {
		t.Fatal("valid recipient bob should have received it")
	}
	// nothing must have been written up and out of the relay
	if _, err := os.Stat(filepath.Join(filepath.Dir(relay), "etc")); err == nil {
		t.Fatal("traversal escaped the relay dir")
	}
}

func TestRosterSkipsInvalidAgentID(t *testing.T) {
	relay := t.TempDir()
	writeInbox(t, relay, ".presence", "evil.json", map[string]any{"agent_id": "../escape", "pid": 1})
	writeInbox(t, relay, ".presence", "ok.json", map[string]any{"agent_id": "alice", "pid": 2})
	// hold alice's lock so she reads live
	f, _ := os.Open(filepath.Join(relay, ".presence", "ok.json"))
	defer f.Close()
	syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)

	for _, a := range roster(relay, "") {
		if !validID(a.ID) {
			t.Fatalf("roster surfaced an unvalidated id: %q", a.ID)
		}
	}
}

func TestAckInboxMarksRead(t *testing.T) {
	relay := t.TempDir()
	writeInbox(t, relay, "console-1", "1780000000000-bob-a.json", map[string]any{
		"id": "m1", "from": "bob", "to": "console-1", "state": "pending", "content": "hi",
	})
	writeInbox(t, relay, "console-1", "1780000000001-bob-b.json", map[string]any{
		"id": "m2", "from": "bob", "to": "console-1", "state": "read", "content": "old",
	})
	n, err := AckInbox(relay, "console-1")
	if err != nil || n != 1 {
		t.Fatalf("only the pending one acks: n=%d err=%v", n, err)
	}
	data, _ := os.ReadFile(filepath.Join(relay, "console-1", "1780000000000-bob-a.json"))
	var m map[string]any
	json.Unmarshal(data, &m)
	if m["state"] != "read" || m["read_at"] == nil {
		t.Fatalf("message not marked read: %+v", m)
	}
}
