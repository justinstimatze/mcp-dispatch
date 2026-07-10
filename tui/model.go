// model.go — the Bubble Tea model: an IRC-style client for the dispatch relay.
//
// Two ideas make it read like a chat client over what is really an ephemeral
// message queue:
//
//   - Transcript, not inbox. The relay deletes a message when its recipient
//     acks it (and when it expires), so a snapshot keeps losing the
//     conversation. We ACCUMULATE every message seen across polls into a
//     transcript that persists after the on-disk copy is gone.
//   - Project, not pid. Ids are <project>-<pid> and pids churn every session
//     restart, so we group the roster by project — the persistent "nick" —
//     with old/offline projects tucked into a collapsible group.
package main

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

const (
	rosterWidth   = 26
	maxTranscript = 5000 // cap the accumulated log; evict oldest beyond this
	wrapIndent    = 2    // fixed hanging indent for wrapped feed lines
)

type tickMsg time.Time
type snapshotMsg Snapshot

type targetKind int

const (
	targetAll        targetKind = iota
	targetAgent                 // a project (grouped across its pids)
	targetChannel               // a "#name" channel
	targetPastHeader            // the collapsible "N past sessions" divider
)

type target struct {
	kind   targetKind
	value  string // project id or "#channel"; empty for all/header
	label  string
	live   bool
	remote bool
	count  int // transcript messages involving this project (offline count for header)
}

type model struct {
	relay, repo string
	readGit     bool
	interval    time.Duration
	version     string
	nick        string // console identity for send/ack

	snap       Snapshot       // latest raw snapshot (current presence + on-disk msgs)
	transcript []Message      // accumulated, deduped, chronological
	seen       map[string]int // msg id -> index in transcript
	targets    []target
	selected   int
	rosterTop  int             // first visible roster row
	offline    map[string]bool // offline project set (for the past-group feed filter)
	pastOpen   bool            // is the past-sessions group expanded
	nLive      int             // live project count (header)
	nProjects  int             // total project count (header)

	vp        viewport.Model
	ready     bool
	follow    bool
	composing bool
	input     textinput.Model
	status    string
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
		seen:    map[string]int{},
		offline: map[string]bool{},
		targets: []target{{kind: targetAll, label: "all traffic"}},
	}
}

func (m model) Init() tea.Cmd { return tea.Batch(m.load(), m.tick()) }

func (m model) load() tea.Cmd {
	return func() tea.Msg { return snapshotMsg(Load(m.relay, m.repo, m.readGit)) }
}

func (m model) tick() tea.Cmd {
	return tea.Tick(m.interval, func(t time.Time) tea.Msg { return tickMsg(t) })
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
		m.accumulate(m.snap.Messages)
		m.rebuildTargets()
		m.refreshFeed()
		return m, nil

	case tea.MouseMsg:
		// The scroll wheel only ever moves the feed (right pane), wherever the
		// cursor is — the roster is keyboard-navigated.
		switch msg.Button {
		case tea.MouseButtonWheelUp:
			m.vp.LineUp(3)
			m.follow = m.vp.AtBottom()
		case tea.MouseButtonWheelDown:
			m.vp.LineDown(3)
			m.follow = m.vp.AtBottom()
		}
		return m, nil

	case tea.KeyMsg:
		if m.composing {
			return m.handleCompose(msg)
		}
		return m.handleKey(msg)
	}
	return m, nil
}

// accumulate merges a snapshot's messages into the transcript, deduped by id, so
// a message keeps showing after the relay deletes its on-disk copy on ack. An
// existing id is updated in place (e.g. its state changed); new ids are appended
// and the transcript re-sorted chronologically and capped.
func (m *model) accumulate(msgs []Message) {
	added := false
	for _, nm := range msgs {
		if i, ok := m.seen[nm.ID]; ok {
			m.transcript[i] = nm
			continue
		}
		m.seen[nm.ID] = len(m.transcript)
		m.transcript = append(m.transcript, nm)
		added = true
	}
	if !added {
		return
	}
	sort.SliceStable(m.transcript, func(i, j int) bool {
		if m.transcript[i].sortMS != m.transcript[j].sortMS {
			return m.transcript[i].sortMS < m.transcript[j].sortMS
		}
		return m.transcript[i].ID < m.transcript[j].ID
	})
	if len(m.transcript) > maxTranscript {
		m.transcript = m.transcript[len(m.transcript)-maxTranscript:]
	}
	m.seen = make(map[string]int, len(m.transcript))
	for i, mm := range m.transcript {
		m.seen[mm.ID] = i
	}
}

// rebuildTargets recomputes the sidebar from current presence + the transcript:
// live/remote projects up top (busiest first), channels, then the collapsible
// past-sessions group of offline projects. Selection is kept stable by value.
func (m *model) rebuildTargets() {
	prev, prevKind := "", targetAll
	if m.selected >= 0 && m.selected < len(m.targets) {
		prev, prevKind = m.targets[m.selected].value, m.targets[m.selected].kind
	}

	liveP, remoteP := map[string]bool{}, map[string]bool{}
	for _, a := range m.snap.Agents {
		p := project(a.ID)
		if a.Live {
			liveP[p] = true
		} else if a.Remote {
			remoteP[p] = true
		}
	}

	count := map[string]int{}
	var projOrder []string
	chans := map[string]bool{}
	for _, msg := range m.transcript {
		for _, id := range [2]string{msg.From, msg.To} {
			if strings.HasPrefix(id, "#") {
				chans[strings.TrimPrefix(id, "#")] = true
				continue
			}
			if id == "" || id == "all" {
				continue
			}
			p := project(id)
			if !validID(p) {
				continue
			}
			if _, ok := count[p]; !ok {
				projOrder = append(projOrder, p)
			}
			count[p]++
		}
	}
	for p := range liveP {
		if _, ok := count[p]; !ok {
			count[p] = 0
			projOrder = append(projOrder, p)
		}
	}
	for p := range remoteP {
		if _, ok := count[p]; !ok {
			count[p] = 0
			projOrder = append(projOrder, p)
		}
	}

	var active, offline []string
	m.offline = map[string]bool{}
	for _, p := range projOrder {
		if liveP[p] || remoteP[p] {
			active = append(active, p)
		} else {
			offline = append(offline, p)
			m.offline[p] = true
		}
	}
	byCount := func(s []string) {
		sort.SliceStable(s, func(i, j int) bool {
			if count[s[i]] != count[s[j]] {
				return count[s[i]] > count[s[j]]
			}
			return s[i] < s[j]
		})
	}
	byCount(active)
	byCount(offline)
	var chanList []string
	for c := range chans {
		chanList = append(chanList, c)
	}
	sort.Strings(chanList)

	targets := []target{{kind: targetAll, label: "all traffic"}}
	for _, p := range active {
		targets = append(targets, target{
			kind: targetAgent, value: p, label: p,
			live: liveP[p], remote: remoteP[p], count: count[p],
		})
	}
	for _, c := range chanList {
		targets = append(targets, target{kind: targetChannel, value: "#" + c, label: "#" + c})
	}
	if len(offline) > 0 {
		targets = append(targets, target{kind: targetPastHeader, count: len(offline)})
		if m.pastOpen {
			for _, p := range offline {
				targets = append(targets, target{kind: targetAgent, value: p, label: p, count: count[p]})
			}
		}
	}
	m.targets = targets

	m.nProjects = len(active) + len(offline)
	m.nLive = 0
	for _, p := range active {
		if liveP[p] {
			m.nLive++
		}
	}

	m.selected = 0
	for i, t := range targets {
		if prev != "" && t.value == prev {
			m.selected = i
			break
		}
		if prev == "" && prevKind == targetPastHeader && t.kind == targetPastHeader {
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

func (msg Message) matches(t target) bool {
	switch t.kind {
	case targetAll:
		return true
	case targetAgent:
		return project(msg.From) == t.value || project(msg.To) == t.value
	case targetChannel:
		return msg.To == t.value
	}
	return false
}

func (m *model) refreshFeed() {
	if !m.ready {
		return
	}
	t := m.currentTarget()
	match := func(msg Message) bool {
		if t.kind == targetPastHeader { // the group header shows all offline chatter
			return m.offline[project(msg.From)] || m.offline[project(msg.To)]
		}
		return msg.matches(t)
	}
	var b strings.Builder
	shown := 0
	for _, msg := range m.transcript {
		if !match(msg) {
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

func (m model) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "q", "ctrl+c":
		return m, tea.Quit
	case "enter", "i", "/":
		if m.currentTarget().kind == targetPastHeader { // toggle the group
			m.pastOpen = !m.pastOpen
			m.rebuildTargets()
			m.refreshFeed()
			return m, nil
		}
		m.composing = true
		m.status = ""
		m.input.Focus()
		return m, textinput.Blink
	case "right", "l":
		if m.currentTarget().kind == targetPastHeader && !m.pastOpen {
			m.pastOpen = true
			m.rebuildTargets()
			m.refreshFeed()
		}
		return m, nil
	case "left", "h":
		if m.currentTarget().kind == targetPastHeader && m.pastOpen {
			m.pastOpen = false
			m.rebuildTargets()
			m.refreshFeed()
		}
		return m, nil
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
			m.ensureVisible()
			m.refreshFeed()
		}
		return m, nil
	case "tab", "down", "j":
		if len(m.targets) > 0 {
			m.selected = (m.selected + 1) % len(m.targets)
			m.ensureVisible()
			m.refreshFeed()
		}
		return m, nil
	case "shift+tab", "up", "k":
		if len(m.targets) > 0 {
			m.selected = (m.selected - 1 + len(m.targets)) % len(m.targets)
			m.ensureVisible()
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
		label, ids, meta := m.resolveSend(m.currentTarget())
		switch {
		case meta: // "all" / "#channel": Send does the fan-out
			n, err := Send(m.relay, m.nick, label, text, m.snap, "normal")
			m.setSendStatus(label, n, err)
		case len(ids) == 0:
			m.status, m.statusErr = fmt.Sprintf("no live session for %s to send to", label), true
		default: // a project: deliver to each of its live sessions
			total := 0
			var err error
			for _, id := range ids {
				n, e := Send(m.relay, m.nick, id, text, m.snap, "normal")
				total += n
				if e != nil {
					err = e
				}
			}
			m.setSendStatus(label, total, err)
		}
		m.follow = true
		return m, m.load()
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(msg)
	return m, cmd
}

func (m *model) setSendStatus(label string, n int, err error) {
	if err != nil {
		m.status, m.statusErr = err.Error(), true
		return
	}
	m.status, m.statusErr = fmt.Sprintf("✓ sent to %s → %d inbox(es)", label, n), false
}

// resolveSend maps the selected target to a delivery. all/#channel are handled
// by Send's own fan-out (meta=true). A project resolves to its live sessions'
// pids (empty if none are live — you can't reach an offline project).
func (m model) resolveSend(t target) (label string, ids []string, meta bool) {
	switch t.kind {
	case targetAll:
		return "all", nil, true
	case targetChannel:
		return t.value, nil, true
	case targetAgent:
		var pids []string
		for _, a := range m.snap.Agents {
			if a.Live && project(a.ID) == t.value {
				pids = append(pids, a.ID)
			}
		}
		return t.value, pids, false
	}
	return t.label, nil, false
}

// filterLabel is the human name of the current filter, for the footer/compose.
func (m model) filterLabel() string {
	t := m.currentTarget()
	switch t.kind {
	case targetAll:
		return "all traffic"
	case targetPastHeader:
		return "past sessions"
	default:
		return t.label
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
