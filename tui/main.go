// dispatch-tui — a read-only "passive IRC client" for the mcp-dispatch relay.
//
// The relay is the IRC server: live sessions are nicks, '#name' targets are
// channels, and the feed is the message stream. When cross-host comms are
// configured it also reads the git bus, so remote agents (nicks from other
// hosts) and their traffic show up too, marked «remote». It touches nothing —
// no ack, no delete — it only watches.
//
//	dispatch-tui                 # auto-resolve the relay + git bus from config
//	dispatch-tui --dir PATH      # point at a specific relay dir
//	dispatch-tui --no-git        # local inboxes only
//	dispatch-tui --interval 0.5  # poll faster (default 1s)
//	dispatch-tui --version
package main

import (
	"flag"
	"fmt"
	"os"
	"runtime/debug"
	"strconv"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// version is overridden at release via -ldflags "-X main.version=...". See
// buildVersion() for the fallback chain (the git tag is the source of truth).
var version = "dev"

func buildVersion() string {
	if version != "dev" {
		return version
	}
	bi, ok := debug.ReadBuildInfo()
	if !ok {
		return version
	}
	if bi.Main.Version != "" && bi.Main.Version != "(devel)" {
		return bi.Main.Version
	}
	var rev, dirty string
	for _, s := range bi.Settings {
		switch s.Key {
		case "vcs.revision":
			if len(s.Value) >= 7 {
				rev = s.Value[:7]
			} else {
				rev = s.Value
			}
		case "vcs.modified":
			if s.Value == "true" {
				dirty = "-dirty"
			}
		}
	}
	if rev != "" {
		return rev + dirty
	}
	return version
}

func main() {
	dir := flag.String("dir", "", "relay dir (default: $MCP_DISPATCH_DIR or config or ~/.config/mcp-dispatch/messages)")
	gitRepo := flag.String("git-repo", "", "git-bus clone dir (default: config [git].repo_dir)")
	noGit := flag.Bool("no-git", false, "local inboxes only — don't read the cross-host git bus")
	interval := flag.Float64("interval", 1.0, "poll seconds")
	dump := flag.Bool("dump", false, "render one frame to stdout and exit (no TTY; for scripts/screenshots)")
	showVersion := flag.Bool("version", false, "print version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Println("dispatch-tui", buildVersion())
		return
	}

	cfg := loadConfig()
	relay := RelayDir(cfg)
	if *dir != "" {
		relay = expandUser(*dir)
	}
	if fi, err := os.Stat(relay); err != nil || !fi.IsDir() {
		fmt.Fprintf(os.Stderr, "no relay at %s\n", relay)
		fmt.Fprintln(os.Stderr, "→ no dispatch-enabled session has started, or pass --dir.")
		os.Exit(1)
	}

	repo := GitRepoDir(cfg)
	if *gitRepo != "" {
		repo = expandUser(*gitRepo)
	}
	readGit := !*noGit && repo != ""

	poll := time.Duration(*interval * float64(time.Second))
	if poll < 100*time.Millisecond {
		poll = 100 * time.Millisecond
	}

	m := newModel(relay, repo, readGit, poll, buildVersion())

	if *dump {
		// One-shot render: drive the Model directly (no tea loop, no TTY) so it
		// works in a pipe. Size to $COLUMNS×$LINES or a sane default.
		var mi tea.Model = m
		mi, _ = mi.Update(tea.WindowSizeMsg{Width: termWidth(), Height: termHeight()})
		mi, _ = mi.Update(snapshotMsg(Load(relay, repo, readGit)))
		fmt.Println(mi.View())
		return
	}

	p := tea.NewProgram(m, tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "dispatch-tui:", err)
		os.Exit(1)
	}
}

func termWidth() int  { return envInt("COLUMNS", 100) }
func termHeight() int { return envInt("LINES", 24) }

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}
