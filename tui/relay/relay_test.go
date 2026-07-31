package relay

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
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
	if got := ExpandUser("~/x"); got != filepath.Join(home, "x") {
		t.Fatalf("ExpandUser: %s", got)
	}
	if got := ExpandUser("/abs/p"); got != "/abs/p" {
		t.Fatalf("abs path mangled: %s", got)
	}
}

func TestRelayDirPrecedence(t *testing.T) {
	t.Setenv("MCP_DISPATCH_DIR", "/from/env")
	if got := RelayDir(Config{DispatchDir: "/from/cfg"}); got != "/from/env" {
		t.Fatalf("env should win: %s", got)
	}
	os.Unsetenv("MCP_DISPATCH_DIR")
	t.Setenv("DISPATCH_DIR", "")
	cfg := Config{}
	cfg.Dispatch.DispatchDir = "/from/table"
	if got := RelayDir(cfg); got != "/from/table" {
		t.Fatalf("table fallback: %s", got)
	}
	top := Config{DispatchDir: "/top"}
	top.Dispatch.DispatchDir = "/table"
	if got := RelayDir(top); got != "/top" {
		t.Fatalf("top-level should win over table: %s", got)
	}
}

func TestGitRepoDirMissing(t *testing.T) {
	os.Unsetenv("MCP_DISPATCH_GIT_REPO")
	if got := GitRepoDir(Config{}); got != "" {
		t.Fatalf("unconfigured git → empty, got %s", got)
	}
	if got := GitRepoDir(func() Config { c := Config{}; c.Git.RepoDir = "/no/such/dir"; return c }()); got != "" {
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
	inbox := []Message{{ID: "a", Timestamp: "2026-07-10T18:00:02Z", SortMS: 2, Via: "git"}}
	git := []Message{
		{ID: "a", Timestamp: "2026-07-10T18:00:02Z", SortMS: 2, Via: "git"}, // dup of inbox
		{ID: "b", Timestamp: "2026-07-10T18:00:01Z", SortMS: 1, Via: "git"}, // lane-only, earlier
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
		if got := Project(in); got != want {
			t.Fatalf("Project(%q)=%q want %q", in, got, want)
		}
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
		if !ValidID(a.ID) {
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

func TestSendResolvesANickToItsLiveSessions(t *testing.T) {
	relay := t.TempDir()
	snap := Snapshot{Agents: []Agent{
		{ID: "publicai-222", Live: true},
		{ID: "publicai-333", Live: true},
		{ID: "publicai-111", Live: false}, // a dead session must not receive
		{ID: "other-9", Live: true},
	}}
	n, err := Send(relay, "console-1", "publicai", "the nick, not the pid", snap, "normal")
	if err != nil || n != 2 {
		t.Fatalf("a nick should reach every live session: n=%d err=%v", n, err)
	}
	for _, who := range []string{"publicai-222", "publicai-333"} {
		if fs, _ := filepath.Glob(filepath.Join(relay, who, "*.json")); len(fs) != 1 {
			t.Fatalf("%s should have the message", who)
		}
	}
	for _, who := range []string{"publicai-111", "publicai", "other-9"} {
		if fs, _ := filepath.Glob(filepath.Join(relay, who, "*.json")); len(fs) != 0 {
			t.Fatalf("%s should have nothing", who)
		}
	}
}

func TestSendToAnOfflineNickWaitsUnderTheNick(t *testing.T) {
	relay := t.TempDir()
	// publicai has existed but nothing of it is live: the message waits in the
	// nick's own inbox for the next session to inherit, rather than landing in a
	// dead pid's directory that nobody will ever open again.
	snap := Snapshot{Agents: []Agent{{ID: "publicai-111", Live: false}}}
	n, err := Send(relay, "console-1", "publicai", "when you're back", snap, "normal")
	if err != nil || n != 1 {
		t.Fatalf("n=%d err=%v", n, err)
	}
	if fs, _ := filepath.Glob(filepath.Join(relay, "publicai", "*.json")); len(fs) != 1 {
		t.Fatal("mail for an offline nick belongs in the nick's inbox")
	}
	if fs, _ := filepath.Glob(filepath.Join(relay, "publicai-111", "*.json")); len(fs) != 0 {
		t.Fatal("a dead session's inbox must not be used")
	}
}

func TestSendToAConcreteSessionIsUnchanged(t *testing.T) {
	relay := t.TempDir()
	snap := Snapshot{Agents: []Agent{
		{ID: "publicai-222", Live: true},
		{ID: "publicai-333", Live: true},
	}}
	n, _ := Send(relay, "console-1", "publicai-222", "this exact window", snap, "normal")
	if n != 1 {
		t.Fatalf("addressing a session id must not fan out to its siblings: n=%d", n)
	}
	if fs, _ := filepath.Glob(filepath.Join(relay, "publicai-333", "*.json")); len(fs) != 0 {
		t.Fatal("the sibling should have nothing")
	}
}
