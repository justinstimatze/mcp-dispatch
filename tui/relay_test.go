package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
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
	m := newModel("/relay", "", false, time.Second, "test")
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
