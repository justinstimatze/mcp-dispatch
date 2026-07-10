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
	channelStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("170"))
	countStyle     = lipgloss.NewStyle().Foreground(lipgloss.Color("245"))
	unreadTagStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("214"))
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
	line := head + content
	// Truncate on display width so styled runs aren't cut mid-escape.
	if width > 0 && lipgloss.Width(line) > width {
		budget := width - lipgloss.Width(head) - 1
		if budget < 1 {
			budget = 1
		}
		line = head + truncate(content, budget) + "…"
	}
	return line
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
	meta := countStyle.Render(fmt.Sprintf(" %s · %d msgs · %d agents",
		src, len(m.snap.Messages), len(m.snap.Agents)))
	return lipgloss.NewStyle().Width(m.width).Render(title + meta)
}

func (m model) rosterView() string {
	var b strings.Builder
	agentByID := map[string]Agent{}
	for _, a := range m.snap.Agents {
		agentByID[a.ID] = a
	}
	for i, t := range m.targets {
		line := m.rosterLine(t, agentByID)
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
	filter := m.currentTarget().label
	keys := "tab/↑↓ filter · pgup/pgdn scroll · f follow · g/G top/bottom · q quit"
	return footerStyle.Width(m.width).Render(
		fmt.Sprintf(" [%s] %s · %s", filter, pos, keys))
}
