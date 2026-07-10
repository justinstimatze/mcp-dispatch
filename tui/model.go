// model.go — the Bubble Tea model: a read-only "passive IRC client" for the
// dispatch relay. A roster/channel sidebar picks a filter; the feed viewport
// scrolls the (local + cross-host) message stream, following new arrivals.
package main

import (
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

const rosterWidth = 26

type tickMsg time.Time
type snapshotMsg Snapshot

// targetKind is what a sidebar row filters the feed to.
type targetKind int

const (
	targetAll targetKind = iota
	targetAgent
	targetChannel
)

type target struct {
	kind  targetKind
	value string // agent id or channel ("#name"); empty for all
	label string
}

type model struct {
	relay, repo string
	readGit     bool
	interval    time.Duration
	version     string

	snap     Snapshot
	targets  []target
	selected int // index into targets
	vp       viewport.Model
	ready    bool
	follow   bool // stick to the bottom as new messages arrive
	width    int
	height   int
}

func newModel(relay, repo string, readGit bool, interval time.Duration, version string) model {
	return model{
		relay: relay, repo: repo, readGit: readGit, interval: interval,
		version: version, follow: true, targets: []target{{kind: targetAll, label: "all traffic"}},
	}
}

func (m model) Init() tea.Cmd {
	return tea.Batch(m.load(), m.tick())
}

func (m model) load() tea.Cmd {
	return func() tea.Msg { return snapshotMsg(Load(m.relay, m.repo, m.readGit)) }
}

func (m model) tick() tea.Cmd {
	return tea.Tick(m.interval, func(t time.Time) tea.Msg { return tickMsg(t) })
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		feedW := msg.Width - rosterWidth - 1
		feedH := msg.Height - 2 // header + footer
		if feedH < 1 {
			feedH = 1
		}
		if !m.ready {
			m.vp = viewport.New(feedW, feedH)
			m.ready = true
		} else {
			m.vp.Width, m.vp.Height = feedW, feedH
		}
		m.refreshFeed()
		return m, nil

	case tickMsg:
		return m, tea.Batch(m.load(), m.tick())

	case snapshotMsg:
		m.snap = Snapshot(msg)
		m.rebuildTargets()
		m.refreshFeed()
		return m, nil

	case tea.KeyMsg:
		return m.handleKey(msg)
	}
	return m, nil
}

func (m model) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "q", "ctrl+c":
		return m, tea.Quit
	case "esc":
		if m.selected != 0 {
			m.selected = 0
			m.refreshFeed()
		}
		return m, nil
	case "tab", "down", "j":
		if len(m.targets) > 0 {
			m.selected = (m.selected + 1) % len(m.targets)
			m.refreshFeed()
		}
		return m, nil
	case "shift+tab", "up", "k":
		if len(m.targets) > 0 {
			m.selected = (m.selected - 1 + len(m.targets)) % len(m.targets)
			m.refreshFeed()
		}
		return m, nil
	case "pgdown", "ctrl+f", " ":
		m.vp.ViewDown()
		m.follow = m.vp.AtBottom()
		return m, nil
	case "pgup", "ctrl+b", "b":
		m.vp.ViewUp()
		m.follow = m.vp.AtBottom()
		return m, nil
	case "g", "home":
		m.vp.GotoTop()
		m.follow = false
		return m, nil
	case "G", "end":
		m.vp.GotoBottom()
		m.follow = true
		return m, nil
	case "f":
		m.follow = !m.follow
		if m.follow {
			m.vp.GotoBottom()
		}
		return m, nil
	}
	return m, nil
}

// rebuildTargets refreshes the sidebar list from the current snapshot, keeping
// the current selection stable by value across refreshes.
func (m *model) rebuildTargets() {
	prev := ""
	if m.selected >= 0 && m.selected < len(m.targets) {
		prev = m.targets[m.selected].value
	}
	targets := []target{{kind: targetAll, label: "all traffic"}}
	for _, a := range m.snap.Agents {
		targets = append(targets, target{kind: targetAgent, value: a.ID, label: a.ID})
	}
	seen := map[string]bool{}
	for _, a := range m.snap.Agents {
		for _, c := range a.Channels {
			if !seen[c] {
				seen[c] = true
				targets = append(targets, target{kind: targetChannel, value: "#" + c, label: "#" + c})
			}
		}
	}
	for _, msg := range m.snap.Messages {
		if c := msg.Channel(); c != "" && !seen[c] {
			seen[c] = true
			targets = append(targets, target{kind: targetChannel, value: "#" + c, label: "#" + c})
		}
	}
	m.targets = targets
	m.selected = 0
	for i, t := range targets {
		if t.value == prev && prev != "" {
			m.selected = i
			break
		}
	}
}

func (m model) currentTarget() target {
	if m.selected >= 0 && m.selected < len(m.targets) {
		return m.targets[m.selected]
	}
	return target{kind: targetAll}
}

func (m Message) matches(t target) bool {
	switch t.kind {
	case targetAll:
		return true
	case targetAgent:
		return m.From == t.value || m.To == t.value
	case targetChannel:
		return m.To == t.value
	}
	return false
}

func (m *model) refreshFeed() {
	if !m.ready {
		return
	}
	t := m.currentTarget()
	var b strings.Builder
	shown := 0
	for _, msg := range m.snap.Messages {
		if !msg.matches(t) {
			continue
		}
		b.WriteString(formatMessage(msg, m.vp.Width))
		b.WriteByte('\n')
		shown++
	}
	if shown == 0 {
		b.WriteString(dimStyle.Render("  (no messages yet — waiting for traffic…)"))
	}
	m.vp.SetContent(b.String())
	if m.follow {
		m.vp.GotoBottom()
	}
}

func (m model) View() string {
	if !m.ready {
		return "starting dispatch-tui…"
	}
	return lipgloss.JoinVertical(lipgloss.Left,
		m.headerView(),
		lipgloss.JoinHorizontal(lipgloss.Top, m.rosterView(), m.vp.View()),
		m.footerView(),
	)
}
