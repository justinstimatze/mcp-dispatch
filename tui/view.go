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
	selStyle       = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("231")).Background(lipgloss.Color("238"))
	rosterBox      = lipgloss.NewStyle().Width(rosterWidth).BorderStyle(lipgloss.NormalBorder()).BorderRight(true).BorderForeground(lipgloss.Color("238"))
	footerStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("245"))
	footerKeyStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("244"))
	channelStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("170"))
	countStyle     = lipgloss.NewStyle().Foreground(lipgloss.Color("245"))
	unreadTagStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("214"))
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

// formatMessage renders one feed line, truncated to width.
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
	// Word-wrap the content and hang continuation lines under it (IRC-style)
	// instead of truncating. The continuation indent is capped so a long
	// from→to header (these ids are project-pid, easily 40+ cols) doesn't squeeze
	// the content into a thin ribbon: short ids align under the content, long
	// ids fall back to a modest indent that gives the content near-full width.
	headW := lipgloss.Width(head)
	indentW := headW
	if indentW > width/3 {
		indentW = min(width/3, 14)
	}
	lines := wrapHanging(content, width-headW, width-indentW)
	indent := strings.Repeat(" ", indentW)
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
	live := 0
	for _, a := range m.snap.Agents {
		if a.Live {
			live++
		}
	}
	title := headerStyle.Render("dispatch-tui")
	meta := countStyle.Render(fmt.Sprintf(" %s · %d msgs · %d live / %d seen",
		src, len(m.snap.Messages), live, len(m.snap.Agents)))
	return lipgloss.NewStyle().Width(m.width).Render(title + meta)
}

func (m model) rosterView() string {
	agentByID := map[string]Agent{}
	for _, a := range m.snap.Agents {
		agentByID[a.ID] = a
	}
	end := m.rosterTop + m.rosterVisibleRows()
	if end > len(m.targets) {
		end = len(m.targets)
	}
	var b strings.Builder
	for i := m.rosterTop; i < end; i++ {
		line := m.rosterLine(m.targets[i], agentByID)
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
	prompt := composePrompt.Render(sendTarget(m.currentTarget()) + " ▸")
	body := prompt + composeBar.Render(" "+m.input.View())
	return composeBar.Width(m.width).Render(body)
}

func (m model) rosterLine(t target, agents map[string]Agent) string {
	switch t.kind {
	case targetAll:
		return "▸ all traffic"
	case targetChannel:
		return channelStyle.Render(t.label)
	case targetAgent:
		a := agents[t.value]
		glyph := dimStyle.Render("◦")
		if a.Live {
			glyph = liveStyle.Render("●")
		} else if a.Remote {
			glyph = remoteStyle.Render("◆")
		}
		unread := ""
		if a.Unread > 0 {
			unread = " " + unreadTagStyle.Render(fmt.Sprintf("(%d)", a.Unread))
		}
		id := truncate(t.label, rosterWidth-6)
		return fmt.Sprintf("%s %s%s", glyph, id, unread)
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
		nickStyle.Render("@"+m.nick), m.currentTarget().label, pos, scroll)
	keys := footerKeyStyle.Render("i send · a ack · tab filter · pgup/pgdn scroll · f follow · q quit")
	return footerStyle.Width(m.width).Render(left + status + "  " + keys)
}
