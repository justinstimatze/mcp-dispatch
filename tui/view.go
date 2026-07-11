// view.go — lipgloss styling and the header/roster/footer/message renderers.
package main

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

var (
	dimStyle       = lipgloss.NewStyle().Foreground(lipgloss.Color("240"))
	fromStyle      = lipgloss.NewStyle().Foreground(lipgloss.Color("39"))  // cyan-blue
	toStyle        = lipgloss.NewStyle().Foreground(lipgloss.Color("170")) // magenta
	urgentStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("196")).Bold(true)
	highStyle      = lipgloss.NewStyle().Foreground(lipgloss.Color("214"))
	remoteStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("111")) // soft indigo
	liveStyle      = lipgloss.NewStyle().Foreground(lipgloss.Color("42"))  // green
	headerStyle    = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("231")).Background(lipgloss.Color("24")).Padding(0, 1)
	selStyle       = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("231")).Background(lipgloss.Color("32"))
	rosterBox      = lipgloss.NewStyle().Width(rosterWidth).BorderStyle(lipgloss.NormalBorder()).BorderRight(true).BorderForeground(lipgloss.Color("238"))
	footerStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("245"))
	footerKeyStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("244"))
	channelStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("170"))
	countStyle     = lipgloss.NewStyle().Foreground(lipgloss.Color("245"))
	nickStyle      = lipgloss.NewStyle().Foreground(lipgloss.Color("42")).Bold(true)
	okStatusStyle  = lipgloss.NewStyle().Foreground(lipgloss.Color("42")).Bold(true)
	errStatusStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("203")).Bold(true)
	composeBar     = lipgloss.NewStyle().Background(lipgloss.Color("236")).Foreground(lipgloss.Color("231"))
	composePrompt  = lipgloss.NewStyle().Background(lipgloss.Color("30")).Foreground(lipgloss.Color("231")).Bold(true).Padding(0, 1)
)

func hhmmss(ts string) string {
	if i := strings.IndexByte(ts, 'T'); i >= 0 && strings.HasSuffix(ts, "Z") {
		return ts[i+1 : len(ts)-1]
	}
	if ts == "" {
		return "--:--:--"
	}
	return ts
}

// formatMessage renders one message as a header line plus word-wrapped content.
func formatMessage(m Message, width int) string {
	arrow := toStyle.Render("→")
	if m.Priority == "urgent" {
		arrow = urgentStyle.Render("→")
	}
	flags := ""
	if m.MustRead {
		flags += " 🔒"
	}
	switch m.Priority {
	case "urgent":
		flags += " " + urgentStyle.Render("‼")
	case "high":
		flags += " " + highStyle.Render("!")
	}
	via := ""
	if m.Remote() {
		via = " " + remoteStyle.Render("«remote»")
	}
	content := strings.ReplaceAll(m.Content, "\n", " ⏎ ")
	head := fmt.Sprintf("%s  %s %s %s%s%s  ",
		dimStyle.Render(hhmmss(m.Timestamp)),
		fromStyle.Render(m.From), arrow, toStyle.Render(m.To), flags, via)
	if width <= 0 {
		return head + content
	}
	// Word-wrap the content, hanging continuation lines at a FIXED small indent
	// (not the header width — that varies with name/time length and made the left
	// edge jagged from message to message). Continuation lines get near-full width.
	headW := lipgloss.Width(head)
	lines := wrapHanging(content, width-headW, width-wrapIndent)
	indent := strings.Repeat(" ", wrapIndent)
	var b strings.Builder
	b.WriteString(head + lines[0])
	for _, l := range lines[1:] {
		b.WriteByte('\n')
		b.WriteString(indent + l)
	}
	return b.String()
}

// wrapHanging greedily word-wraps s, using firstW for the first line and restW
// for every line after it (so a wide header on line 1 and a modest indent on the
// rest can coexist without overflowing the terminal). A single word longer than
// the line width is left to overflow (the viewport clips it) rather than split
// mid-word.
func wrapHanging(s string, firstW, restW int) []string {
	if firstW < 1 {
		firstW = 1
	}
	if restW < 1 {
		restW = 1
	}
	words := strings.Fields(s)
	if len(words) == 0 {
		return []string{""}
	}
	var lines []string
	cur := words[0]
	w := firstW
	for _, word := range words[1:] {
		if lipgloss.Width(cur)+1+lipgloss.Width(word) <= w {
			cur += " " + word
		} else {
			lines = append(lines, cur)
			cur = word
			w = restW
		}
	}
	return append(lines, cur)
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

func (m model) headerView() string {
	src := m.relay
	if m.snap.RepoDir != "" {
		src += "  " + remoteStyle.Render("+git")
	}
	title := headerStyle.Render("dispatch-tui")
	meta := countStyle.Render(fmt.Sprintf(" %s · %d msgs · %d live / %d agents",
		src, len(m.transcript), m.nLive, m.nProjects))
	return lipgloss.NewStyle().Width(m.width).Render(title + meta)
}

func (m model) rosterView() string {
	end := m.rosterTop + m.rosterVisibleRows()
	if end > len(m.targets) {
		end = len(m.targets)
	}
	var b strings.Builder
	for i := m.rosterTop; i < end; i++ {
		line := m.rosterLine(m.targets[i])
		if i == m.selected {
			line = selStyle.Render(padRight(line, rosterWidth-1))
		}
		b.WriteString(line)
		b.WriteByte('\n')
	}
	body := b.String()
	// Pad the roster to the feed height so the right border runs full length.
	for lipgloss.Height(body) < m.vp.Height {
		body += "\n"
	}
	return rosterBox.Height(m.vp.Height).Render(body)
}

func (m model) composeView() string {
	prompt := composePrompt.Render(m.filterLabel() + " ▸")
	body := prompt + composeBar.Render(" "+m.input.View())
	return composeBar.Width(m.width).Render(body)
}

func (m model) rosterLine(t target) string {
	switch t.kind {
	case targetAll:
		return "▸ all traffic"
	case targetChannel:
		return channelStyle.Render(t.label)
	case targetPastHeader:
		caret := "▸"
		if m.pastOpen {
			caret = "▾"
		}
		return dimStyle.Render(fmt.Sprintf("%s %d past sessions", caret, t.count))
	case targetAgent:
		offline := !t.live && !t.remote
		glyph := dimStyle.Render("◦")
		if t.live {
			glyph = liveStyle.Render("●")
		} else if t.remote {
			glyph = remoteStyle.Render("◆")
		}
		badge := ""
		if t.count > 0 {
			badge = " " + dimStyle.Render(fmt.Sprintf("·%d", t.count))
		}
		prefix := ""
		if offline { // indent members shown under the expanded past-sessions group
			prefix = "  "
		}
		name := truncate(t.label, rosterWidth-8)
		return fmt.Sprintf("%s%s %s%s", prefix, glyph, name, badge)
	}
	return t.label
}

func padRight(s string, n int) string {
	for lipgloss.Width(s) < n {
		s += " "
	}
	return s
}

func (m model) footerView() string {
	pos := "top"
	switch {
	case m.follow:
		pos = "following"
	case m.vp.AtBottom():
		pos = "bottom"
	default:
		pos = fmt.Sprintf("%3.0f%%", m.vp.ScrollPercent()*100)
	}
	scroll := ""
	if len(m.targets) > m.rosterVisibleRows() {
		scroll = fmt.Sprintf(" · roster %d-%d/%d",
			m.rosterTop+1, min(m.rosterTop+m.rosterVisibleRows(), len(m.targets)), len(m.targets))
	}
	status := ""
	if m.status != "" {
		st := okStatusStyle
		if m.statusErr {
			st = errStatusStyle
		}
		status = "  " + st.Render(m.status)
	}
	left := fmt.Sprintf(" %s · [%s] %s%s",
		nickStyle.Render("@"+m.nick), m.filterLabel(), pos, scroll)
	keys := footerKeyStyle.Render("i send · a ack · tab filter · pgup/pgdn scroll · f follow · q quit")
	return footerStyle.Width(m.width).Render(left + status + "  " + keys)
}
