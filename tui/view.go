// view.go — lipgloss styling and the header/roster/footer/message renderers.
package main

import (
	"fmt"
	"strings"
	"time"

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

// parseTS parses an RFC3339 timestamp (with or without fractional seconds) and
// returns it in the viewer's local zone. ok is false when ts is empty or
// unparseable, so callers can fall back rather than print garbage.
func parseTS(ts string) (time.Time, bool) {
	if ts == "" {
		return time.Time{}, false
	}
	if t, err := time.Parse(time.RFC3339, ts); err == nil {
		return t.Local(), true
	}
	return time.Time{}, false
}

// clockLocal renders a message timestamp as local wall-clock time at whole-second
// precision (HH:MM:SS). Messages arrive in UTC (and from some senders carry
// microseconds); showing raw UTC made the column read hours-off and ragged. On an
// unparseable value it falls back to a best-effort raw time, dropping any date,
// trailing Z, and fractional part.
func clockLocal(ts string) string {
	if t, ok := parseTS(ts); ok {
		return t.Format("15:04:05")
	}
	if i := strings.IndexByte(ts, 'T'); i >= 0 {
		s := strings.TrimSuffix(ts[i+1:], "Z")
		if dot := strings.IndexByte(s, '.'); dot >= 0 {
			s = s[:dot]
		}
		return s
	}
	if ts == "" {
		return "--:--:--"
	}
	return ts
}

// dateLocal renders the local calendar day for a timestamp (used for day-change
// dividers), or "" when unparseable so the caller simply omits the divider.
func dateLocal(ts string) string {
	if t, ok := parseTS(ts); ok {
		return t.Format("Mon Jan 2 2006")
	}
	return ""
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
		dimStyle.Render(clockLocal(m.Timestamp)),
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
		var line string
		if i == m.selected {
			// Build the row unstyled so selStyle owns the whole width — inner
			// Render calls emit \x1b[0m resets that otherwise punch holes in
			// the selection background.
			line = selStyle.Render(padRight(m.rosterLine(m.targets[i], false), rosterWidth-1))
		} else {
			line = m.rosterLine(m.targets[i], true)
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
	if m.confirmBroadcast {
		n := 0
		for _, a := range m.snap.Agents {
			if a.Live && a.ID != m.nick {
				n++
			}
		}
		warn := errStatusStyle.Render(
			fmt.Sprintf(" ⚠ broadcast to %d live agents — enter to confirm, esc to cancel ", n))
		return composeBar.Width(m.width).Render(prompt + warn)
	}
	body := prompt + composeBar.Render(" "+m.input.View())
	return composeBar.Width(m.width).Render(body)
}

// rosterLine renders one sidebar row. When styled is false it returns plain
// text (no ANSI) so the caller can wrap a selected row in selStyle without inner
// resets breaking the highlight background.
func (m model) rosterLine(t target, styled bool) string {
	paint := func(s lipgloss.Style, txt string) string {
		if !styled {
			return txt
		}
		return s.Render(txt)
	}
	switch t.kind {
	case targetAll:
		return "▸ all traffic"
	case targetChannel:
		return paint(channelStyle, t.label)
	case targetPastHeader:
		caret := "▸"
		if m.pastOpen {
			caret = "▾"
		}
		return paint(dimStyle, fmt.Sprintf("%s %d past sessions", caret, t.count))
	case targetAgent:
		offline := !t.live && !t.remote
		glyphChar, glyphStyle := "◦", dimStyle
		if t.live {
			glyphChar, glyphStyle = "●", liveStyle
		} else if t.remote {
			glyphChar, glyphStyle = "◆", remoteStyle
		}
		glyph := paint(glyphStyle, glyphChar)
		badge := ""
		if t.count > 0 {
			badge = " " + paint(dimStyle, fmt.Sprintf("·%d", t.count))
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
