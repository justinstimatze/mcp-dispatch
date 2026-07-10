// model.go — the Bubble Tea model: a read-only "passive IRC client" for the
// dispatch relay. A roster/channel sidebar picks a filter; the feed viewport
// scrolls the (local + cross-host) message stream, following new arrivals.
package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/textinput"
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
	nick        string // console identity for send/ack

	snap      Snapshot
	targets   []target
	selected  int // index into targets
	rosterTop int // first visible roster row (scrolls to keep selection in view)
	vp        viewport.Model
	ready     bool
	follow    bool // stick to the bottom as new messages arrive
	composing bool // the compose bar is open
	input     textinput.Model
	status    string // transient feedback (send/ack result); cleared on next action
	statusErr bool
	width     int
	height    int
}

func newModel(relay, repo string, readGit bool, interval time.Duration, version, nick string) model {
	ti := textinput.New()
	ti.Placeholder = "message… (enter to send · esc to cancel)"
	ti.CharLimit = 4000
	ti.Prompt = ""
	return model{
		relay: relay, repo: repo, readGit: readGit, interval: interval,
		version: version, nick: nick, follow: true, input: ti,
		targets: []target{{kind: targetAll, label: "all traffic"}},
	}
}

func (m model) Init() tea.Cmd {
	return tea.Batch(m.load(), m.tick())
}

func (m model) rosterVisibleRows() int {
	if m.vp.Height < 1 {
		return 1
	}
	return m.vp.Height
}

// ensureVisible scrolls the roster window so the selected row stays on screen.
func (m *model) ensureVisible() {
	h := m.rosterVisibleRows()
	if m.selected < m.rosterTop {
		m.rosterTop = m.selected
	}
	if m.selected >= m.rosterTop+h {
		m.rosterTop = m.selected - h + 1
	}
	if m.rosterTop < 0 || len(m.targets) <= h {
		m.rosterTop = 0
	}
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
		if m.composing {
			return m.handleCompose(msg)
		}
		return m.handleKey(msg)
	}
	return m, nil
}

func (m model) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "q", "ctrl+c":
		return m, tea.Quit
	case "i", "enter", "/":
		// Open the compose bar to send to the current target.
		m.composing = true
		m.status = ""
		m.input.Focus()
		return m, textinput.Blink
	case "a":
		n, err := AckInbox(m.relay, m.nick)
		if err != nil {
			m.status, m.statusErr = err.Error(), true
		} else {
			m.status, m.statusErr = fmt.Sprintf("acked %d in %s", n, m.nick), false
		}
		return m, m.load()
	case "esc":
		if m.selected != 0 {
			m.selected = 0
			(&m).ensureVisible()
			m.refreshFeed()
		}
		return m, nil
	case "tab", "down", "j":
		if len(m.targets) > 0 {
			m.selected = (m.selected + 1) % len(m.targets)
			(&m).ensureVisible()
			m.refreshFeed()
		}
		return m, nil
	case "shift+tab", "up", "k":
		if len(m.targets) > 0 {
			m.selected = (m.selected - 1 + len(m.targets)) % len(m.targets)
			(&m).ensureVisible()
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

// handleCompose routes keys while the compose bar is open: enter sends to the
// current target as the console nick, esc cancels, everything else edits.
func (m model) handleCompose(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.Type {
	case tea.KeyEsc:
		m.composing = false
		m.input.Blur()
		m.input.Reset()
		return m, nil
	case tea.KeyEnter:
		text := strings.TrimSpace(m.input.Value())
		m.composing = false
		m.input.Blur()
		m.input.Reset()
		if text == "" {
			return m, nil
		}
		target := sendTarget(m.currentTarget())
		n, err := Send(m.relay, m.nick, target, text, m.snap, "normal")
		if err != nil {
			m.status, m.statusErr = err.Error(), true
		} else {
			m.status, m.statusErr = fmt.Sprintf("✓ sent to %s → %d inbox(es)", target, n), false
			m.follow = true // jump to the bottom so the sent message is visible
		}
		return m, m.load()
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(msg)
	return m, cmd
}

func sendTarget(t target) string {
	if t.kind == targetAll {
		return "all"
	}
	return t.value // agent id or "#channel"
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
	m.ensureVisible()
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
	bottom := m.footerView()
	if m.composing {
		bottom = m.composeView()
	}
	return lipgloss.JoinVertical(lipgloss.Left,
		m.headerView(),
		lipgloss.JoinHorizontal(lipgloss.Top, m.rosterView(), m.vp.View()),
		bottom,
	)
}
