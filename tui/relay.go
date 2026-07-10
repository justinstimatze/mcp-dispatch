// relay.go — the read-only view of the dispatch relay, with no TUI dependency.
//
// Everything here is pure disk-reading so it can be unit-tested without a
// terminal: resolve the relay + git-bus paths (env → config → default, matching
// the Python tools), scan local inboxes and the git lanes into a merged
// deduped message list, and read presence/roster to know who is reachable.
package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/BurntSushi/toml"
)

// Message is one dispatch message, from a local inbox file or a git bus lane.
type Message struct {
	ID        string `json:"id"`
	From      string `json:"from"`
	To        string `json:"to"` // recipient id, "all", or "#channel"
	Timestamp string `json:"timestamp"`
	Priority  string `json:"priority"`
	Content   string `json:"content"`
	ThreadID  string `json:"thread_id"`
	MustRead  bool   `json:"must_read"`
	State     string `json:"state"`
	Via       string `json:"_via"` // "git" when it arrived over the bus
	sortMS    int64  // chronological order key (not serialized)
}

// Remote reports whether the message arrived from another host over git.
func (m Message) Remote() bool { return m.Via == "git" }

// Channel returns the channel name (without '#') if this is a channel post.
func (m Message) Channel() string {
	if strings.HasPrefix(m.To, "#") {
		return m.To[1:]
	}
	return ""
}

// Agent is a participant on the relay: a live-local session, or a cross-host
// agent known only through the git roster.
type Agent struct {
	ID       string
	Live     bool // holds its presence flock right now (local session)
	Remote   bool // known via the .remote git roster (reachable, maybe offline)
	Channels []string
	Unread   int
	PID      int
}

// Snapshot is one read of the whole relay: the merged feed plus the roster.
type Snapshot struct {
	Relay    string
	RepoDir  string
	Messages []Message
	Agents   []Agent
}

// ---------------------------------------------------------------------------
// Path resolution (env → config → default), mirroring the Python tools.
// ---------------------------------------------------------------------------

type fileConfig struct {
	DispatchDir string `toml:"dispatch_dir"`
	Dispatch    struct {
		DispatchDir string `toml:"dispatch_dir"`
	} `toml:"dispatch"`
	Git struct {
		RepoDir string `toml:"repo_dir"`
	} `toml:"git"`
}

func expandUser(p string) string {
	if p == "~" || strings.HasPrefix(p, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, strings.TrimPrefix(p, "~"))
		}
	}
	return p
}

func loadConfig() fileConfig {
	path := os.Getenv("MCP_DISPATCH_CONFIG")
	if path == "" {
		path = expandUser("~/.config/mcp-dispatch/config.toml")
	}
	var cfg fileConfig
	if _, err := toml.DecodeFile(path, &cfg); err != nil {
		return fileConfig{} // absent/unreadable → empty, callers fall back
	}
	return cfg
}

// RelayDir resolves the dispatch dir: env override, then config (top-level key
// winning over the [dispatch] table), then the default.
func RelayDir(cfg fileConfig) string {
	if v := os.Getenv("MCP_DISPATCH_DIR"); v != "" {
		return expandUser(v)
	}
	if v := os.Getenv("DISPATCH_DIR"); v != "" {
		return expandUser(v)
	}
	if cfg.DispatchDir != "" {
		return expandUser(cfg.DispatchDir)
	}
	if cfg.Dispatch.DispatchDir != "" {
		return expandUser(cfg.Dispatch.DispatchDir)
	}
	return expandUser("~/.config/mcp-dispatch/messages")
}

// GitRepoDir resolves the git-bus clone if cross-host comms are configured,
// else "". Read-only: the TUI only scans lane files, never fetches.
func GitRepoDir(cfg fileConfig) string {
	repo := os.Getenv("MCP_DISPATCH_GIT_REPO")
	if repo == "" {
		repo = cfg.Git.RepoDir
	}
	if repo == "" {
		return ""
	}
	repo = expandUser(repo)
	if fi, err := os.Stat(repo); err != nil || !fi.IsDir() {
		return ""
	}
	return repo
}

// ---------------------------------------------------------------------------
// Presence — a live owner holds an exclusive flock on its presence file.
// ---------------------------------------------------------------------------

// flockHeld reports whether some live process holds an exclusive flock on path.
// We probe by trying to take it non-blocking: success means nobody holds it
// (release and report not-held); EWOULDBLOCK means a live holder. Opened
// read-only so it never creates the file. Matches the Python probe exactly.
func flockHeld(path string) bool {
	f, err := os.Open(path) //nolint:gosec // path is a relay presence file we enumerate
	if err != nil {
		return false
	}
	defer f.Close()
	fd := int(f.Fd())
	if err := syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return true // couldn't take it → a live process holds it
	}
	_ = syscall.Flock(fd, syscall.LOCK_UN)
	return false
}

// ---------------------------------------------------------------------------
// Scanning
// ---------------------------------------------------------------------------

// isoToMS parses an ISO-8601 "YYYY-MM-DDTHH:MM:SSZ" timestamp to epoch millis.
func isoToMS(ts string) int64 {
	t, err := time.Parse("2006-01-02T15:04:05Z", ts)
	if err != nil {
		return 0
	}
	return t.UnixMilli()
}

// filenameMS reads the leading millisecond epoch from an inbox filename
// (e.g. "1780000000000-bob-hash.json"), falling back to the message timestamp.
func filenameMS(name string, m Message) int64 {
	if i := strings.IndexByte(name, '-'); i > 0 {
		if ms, err := strconv.ParseInt(name[:i], 10, 64); err == nil {
			return ms
		}
	}
	return isoToMS(m.Timestamp)
}

func isDotName(name string) bool { return strings.HasPrefix(name, ".") }

// scanInbox reads every message file across the relay's inbox dirs.
func scanInbox(relay string) []Message {
	var out []Message
	entries, err := os.ReadDir(relay)
	if err != nil {
		return out
	}
	for _, e := range entries {
		if !e.IsDir() || isDotName(e.Name()) {
			continue
		}
		inbox := filepath.Join(relay, e.Name())
		files, err := os.ReadDir(inbox)
		if err != nil {
			continue
		}
		for _, f := range files {
			if f.IsDir() || !strings.HasSuffix(f.Name(), ".json") {
				continue
			}
			data, err := os.ReadFile(filepath.Join(inbox, f.Name())) //nolint:gosec // enumerated relay file
			if err != nil {
				continue
			}
			var m Message
			if json.Unmarshal(data, &m) != nil {
				continue
			}
			m.sortMS = filenameMS(f.Name(), m)
			out = append(out, m)
		}
	}
	return out
}

// laneEnvelope is the subset of the git wire envelope the TUI reads.
type laneEnvelope struct {
	Type string          `json:"type"`
	Body json.RawMessage `json:"body"`
}

// scanGit reads every message record on the git bus (all hosts' lanes). The
// envelope body IS the original message dict; non-message records (atoms, acks,
// presence) are skipped. Tags each Via="git" so the UI marks it remote.
func scanGit(repo string) []Message {
	if repo == "" {
		return nil
	}
	var laneFiles []string
	if lanes, err := filepath.Glob(filepath.Join(repo, "lanes", "*.jsonl")); err == nil {
		laneFiles = append(laneFiles, lanes...)
	}
	if chans, err := filepath.Glob(filepath.Join(repo, "channels", "*", "*.jsonl")); err == nil {
		laneFiles = append(laneFiles, chans...)
	}
	var out []Message
	for _, lane := range laneFiles {
		data, err := os.ReadFile(lane) //nolint:gosec // enumerated bus lane file
		if err != nil {
			continue
		}
		for _, line := range strings.Split(string(data), "\n") {
			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}
			var env laneEnvelope
			if json.Unmarshal([]byte(line), &env) != nil || env.Type != "message" {
				continue
			}
			var m Message
			if json.Unmarshal(env.Body, &m) != nil || m.ID == "" {
				continue
			}
			m.Via = "git"
			m.sortMS = isoToMS(m.Timestamp)
			out = append(out, m)
		}
	}
	return out
}

// mergeMessages dedups inbox + git by message id (a materialized inbox copy
// wins over its lane record) and returns them in chronological order.
func mergeMessages(inbox, git []Message) []Message {
	byID := make(map[string]Message, len(inbox)+len(git))
	for _, m := range inbox {
		byID[m.ID] = m
	}
	for _, m := range git {
		if _, seen := byID[m.ID]; !seen { // lane-only → cross-host traffic for other hosts
			byID[m.ID] = m
		}
	}
	out := make([]Message, 0, len(byID))
	for _, m := range byID {
		out = append(out, m)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].sortMS != out[j].sortMS {
			return out[i].sortMS < out[j].sortMS
		}
		return out[i].ID < out[j].ID
	})
	return out
}

type presenceFile struct {
	AgentID  string   `json:"agent_id"`
	PID      int      `json:"pid"`
	Channels []string `json:"channels"`
}

type remoteFile struct {
	AgentID string `json:"agent_id"`
}

// roster reads live-local presence and the cross-host git roster into a single
// agent list: live-local agents first (sorted), then remote-only agents. A
// remote agent that also has a live-local session is shown once, as live.
func roster(relay, repo string) []Agent {
	live := map[string]*Agent{}
	presence := filepath.Join(relay, ".presence")
	if files, err := filepath.Glob(filepath.Join(presence, "*.json")); err == nil {
		for _, pf := range files {
			data, err := os.ReadFile(pf) //nolint:gosec // enumerated presence file
			if err != nil {
				continue
			}
			var p presenceFile
			if json.Unmarshal(data, &p) != nil || p.AgentID == "" {
				continue
			}
			if !flockHeld(pf) {
				continue
			}
			live[p.AgentID] = &Agent{
				ID: p.AgentID, Live: true, Channels: p.Channels, PID: p.PID,
				Unread: unreadCount(filepath.Join(relay, p.AgentID)),
			}
		}
	}

	var remotes []Agent
	roster := filepath.Join(relay, ".remote")
	if files, err := filepath.Glob(filepath.Join(roster, "*.json")); err == nil {
		for _, rf := range files {
			data, err := os.ReadFile(rf) //nolint:gosec // enumerated roster file
			if err != nil {
				continue
			}
			var r remoteFile
			if json.Unmarshal(data, &r) != nil || r.AgentID == "" {
				continue
			}
			if _, isLive := live[r.AgentID]; isLive {
				continue // a live-local session shadows its own remote roster entry
			}
			remotes = append(remotes, Agent{ID: r.AgentID, Remote: true})
		}
	}

	out := make([]Agent, 0, len(live)+len(remotes))
	for _, a := range live {
		out = append(out, *a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	sort.Slice(remotes, func(i, j int) bool { return remotes[i].ID < remotes[j].ID })
	return append(out, remotes...)
}

func unreadCount(inbox string) int {
	files, err := filepath.Glob(filepath.Join(inbox, "*.json"))
	if err != nil {
		return 0
	}
	n := 0
	for _, f := range files {
		data, err := os.ReadFile(f) //nolint:gosec // enumerated inbox file
		if err != nil {
			continue
		}
		var m Message
		if json.Unmarshal(data, &m) == nil && (m.State == "" || m.State == "pending") {
			n++
		}
	}
	return n
}

// Load reads one full snapshot of the relay (feed + roster). readGit=false
// restricts it to local inboxes.
func Load(relay, repo string, readGit bool) Snapshot {
	if !readGit {
		repo = ""
	}
	msgs := mergeMessages(scanInbox(relay), scanGit(repo))
	return Snapshot{
		Relay:    relay,
		RepoDir:  repo,
		Messages: msgs,
		Agents:   roster(relay, repo),
	}
}
