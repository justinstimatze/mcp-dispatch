// session.go — one IRC client connection, from the first byte to QUIT.
//
// The mapping the TUI already implies, made literal: agents are nicks (stable
// project ids, not per-session pids), '#name' relay channels are IRC channels,
// and '&dispatch' is a local firehose of everything crossing the relay. What
// IRC has no room for — ack, delivery state, priority — lives behind a service
// nick you /msg, the way IRC has always handled what the protocol left out.
//
// Nothing is served before authentication: pre-registration a connection may
// only negotiate capabilities, present a token, and set a nick.
package main

import (
	"bufio"
	"encoding/base64"
	"fmt"
	"log"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/justinstimatze/mcp-dispatch/tui/relay"
)

const (
	serverName  = "dispatch"
	firehose    = "&dispatch"
	serviceNick = "dispatch"

	// maxLine caps an inbound line. RFC 1459 says 512; we allow more so a long
	// paste survives, but a bounded read is the point — an unbounded one is a
	// memory exhaustion primitive for anything that can open a socket.
	maxLine = 8192

	// maxOut is the per-client outbound queue. A client that cannot keep up is
	// disconnected rather than allowed to stall the poller that serves everyone.
	maxOut = 512

	// ircChunk caps an outbound message body so line + prefix stay under the
	// 512-byte wire limit with room to spare.
	ircChunk = 400
)

type session struct {
	hub   *hub
	cfg   Config
	token []byte
	lim   *limiter

	conn net.Conn
	key  string
	out  chan string
	done chan struct{}

	mu         sync.Mutex
	nick       string
	user       string
	authed     bool
	registered bool
	joined     map[string]bool
	saslActive bool
	capHeld    bool

	closeOnce sync.Once
}

func newSession(h *hub, cfg Config, token []byte, lim *limiter, conn net.Conn) *session {
	return &session{
		hub: h, cfg: cfg, token: token, lim: lim, conn: conn,
		key:    remoteKey(conn.RemoteAddr()),
		out:    make(chan string, maxOut),
		done:   make(chan struct{}),
		joined: map[string]bool{},
	}
}

// ---------------------------------------------------------------------------
// Plumbing
// ---------------------------------------------------------------------------

func (s *session) close() {
	s.closeOnce.Do(func() {
		close(s.done)
		_ = s.conn.Close()
	})
}

// send formats and queues a protocol line.
func (s *session) send(format string, args ...any) {
	s.sendRaw(fmt.Sprintf(format, args...))
}

// sendRaw queues an already-rendered line. A full queue means the client is not
// draining, which we treat as fatal for that connection: the alternative is
// blocking the hub that serves everyone else.
func (s *session) sendRaw(line string) {
	// Defensive: a stray CR/LF would let message content forge protocol lines.
	line = strings.NewReplacer("\r", " ", "\n", " ").Replace(line)
	select {
	case <-s.done:
	case s.out <- line:
	default:
		log.Printf("irc: %s: outbound queue full, dropping client", s.key)
		s.close()
	}
}

func (s *session) numeric(code, format string, args ...any) {
	nick := s.currentNick()
	if nick == "" {
		nick = "*"
	}
	s.send(":%s %s %s %s", serverName, code, nick, fmt.Sprintf(format, args...))
}

func (s *session) writeLoop() {
	for {
		select {
		case <-s.done:
			return
		case line := <-s.out:
			_ = s.conn.SetWriteDeadline(time.Now().Add(15 * time.Second))
			if _, err := s.conn.Write([]byte(line + "\r\n")); err != nil {
				s.close()
				return
			}
		}
	}
}

// drain gives the writer a moment to put queued lines on the wire before the
// caller closes the connection. Every refusal path ends in a close, and a
// client that is told nothing before the socket drops just sees EOF — which is
// indistinguishable from a crash, exactly when it matters most.
func (s *session) drain() {
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if len(s.out) == 0 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	time.Sleep(20 * time.Millisecond) // the last line, dequeued but mid-write
}

func (s *session) currentNick() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.nick
}

func (s *session) isRegistered() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.registered
}

func (s *session) hasJoined(ch string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.joined[strings.ToLower(ch)]
}

// ---------------------------------------------------------------------------
// The connection lifecycle
// ---------------------------------------------------------------------------

func (s *session) serve() {
	defer func() {
		s.hub.remove(s)
		if n := s.currentNick(); n != "" {
			s.hub.releaseNick(n)
		}
		s.close()
	}()

	// The writer starts first so the refusals below are actually delivered —
	// a queued ERROR is worthless if nothing is draining the queue yet.
	go s.writeLoop()

	// Gate 1: kernel peer credentials, before a byte is read.
	if err := peerCheck(s.conn); err != nil {
		log.Printf("irc: %s: refused — %v", s.key, err)
		s.send("ERROR :peer credential check failed")
		s.drain()
		return
	}
	// Gate 3 (checked first on each connect): is this source locked out?
	if banned, rem := s.lim.banned(s.key); banned {
		log.Printf("irc: %s: refused — banned for another %s", s.key, rem.Truncate(time.Second))
		s.send("ERROR :too many failed authentications, try again in %s", rem.Truncate(time.Second))
		s.drain()
		return
	}

	sc := bufio.NewScanner(s.conn)
	sc.Buffer(make([]byte, 0, 4096), maxLine)

	// Unauthenticated connections get a short leash; authenticated ones get the
	// idle timeout, refreshed by any traffic (clients PING on their own).
	deadline := s.cfg.authTimeout()
	for {
		_ = s.conn.SetReadDeadline(time.Now().Add(deadline))
		if !sc.Scan() {
			return
		}
		line := strings.TrimRight(sc.Text(), "\r")
		if line == "" {
			continue
		}
		if !s.handle(line) {
			s.drain() // deliver the refusal / goodbye before hanging up
			return
		}
		s.mu.Lock()
		authed := s.authed
		s.mu.Unlock()
		if authed {
			deadline = s.cfg.idleTimeout()
		}
	}
}

// parse splits an IRC line into command and params, honouring the trailing
// ":rest of line" parameter.
func parse(line string) (cmd string, params []string) {
	if strings.HasPrefix(line, ":") { // ignore any client-sent prefix
		if i := strings.IndexByte(line, ' '); i >= 0 {
			line = line[i+1:]
		} else {
			return "", nil
		}
	}
	var trailing string
	hasTrailing := false
	if i := strings.Index(line, " :"); i >= 0 {
		trailing, hasTrailing = line[i+2:], true
		line = line[:i]
	}
	fields := strings.Fields(line)
	if len(fields) == 0 {
		if hasTrailing {
			return "", []string{trailing}
		}
		return "", nil
	}
	cmd = strings.ToUpper(fields[0])
	params = fields[1:]
	if hasTrailing {
		params = append(params, trailing)
	}
	return cmd, params
}

// handle processes one line. Returning false closes the connection.
func (s *session) handle(line string) bool {
	cmd, params := parse(line)
	if cmd == "" {
		return true
	}

	s.mu.Lock()
	authed, registered := s.authed, s.registered
	s.mu.Unlock()

	switch cmd {
	case "QUIT":
		s.send("ERROR :goodbye")
		return false
	case "PING":
		arg := serverName
		if len(params) > 0 {
			arg = params[0]
		}
		s.send(":%s PONG %s :%s", serverName, serverName, arg)
		return true
	case "PONG":
		return true
	case "CAP":
		return s.handleCap(params)
	case "PASS":
		return s.handlePass(params)
	case "AUTHENTICATE":
		return s.handleAuthenticate(params)
	case "NICK":
		return s.handleNick(params)
	case "USER":
		return s.handleUser(params)
	}

	if !authed {
		// Nothing else is served before the token. Say so without hinting at
		// which half of the handshake is missing.
		s.numeric("451", ":You have not registered")
		return true
	}
	if !registered {
		s.numeric("451", ":You have not registered")
		return true
	}

	switch cmd {
	case "JOIN":
		s.handleJoin(params)
	case "PART":
		s.handlePart(params)
	case "PRIVMSG":
		s.handlePrivmsg(params, false)
	case "NOTICE":
		s.handlePrivmsg(params, true)
	case "NAMES":
		if len(params) > 0 {
			s.sendNames(params[0])
		}
	case "WHO":
		s.handleWho(params)
	case "WHOIS":
		s.handleWhois(params)
	case "LIST":
		s.handleList()
	case "MODE":
		s.handleMode(params)
	case "TOPIC":
		if len(params) > 0 {
			s.sendTopic(params[0])
		}
	case "AWAY", "USERHOST", "ISON":
		// Accepted and ignored — clients send these unprompted.
	default:
		s.numeric("421", "%s :Unknown command", cmd)
	}
	return true
}

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

func (s *session) handleCap(params []string) bool {
	if len(params) == 0 {
		return true
	}
	switch strings.ToUpper(params[0]) {
	case "LS":
		s.mu.Lock()
		s.capHeld = true
		s.mu.Unlock()
		s.send(":%s CAP * LS :sasl", serverName)
	case "REQ":
		want := ""
		if len(params) > 1 {
			want = strings.TrimSpace(params[len(params)-1])
		}
		if strings.Contains(want, "sasl") {
			s.send(":%s CAP * ACK :sasl", serverName)
		} else {
			s.send(":%s CAP * NAK :%s", serverName, want)
		}
	case "END":
		s.mu.Lock()
		s.capHeld = false
		s.mu.Unlock()
		s.tryRegister()
	case "LIST":
		s.send(":%s CAP * LIST :sasl", serverName)
	}
	return true
}

// handlePass takes the token the classic way: PASS <token>, or PASS user:token
// for clients that insist on a username half.
func (s *session) handlePass(params []string) bool {
	if len(params) == 0 {
		s.numeric("461", "PASS :Not enough parameters")
		return true
	}
	tok := params[0]
	if i := strings.IndexByte(tok, ':'); i >= 0 {
		tok = tok[i+1:]
	}
	return s.tryAuth([]byte(tok), "PASS")
}

func (s *session) handleAuthenticate(params []string) bool {
	if len(params) == 0 {
		s.numeric("461", "AUTHENTICATE :Not enough parameters")
		return true
	}
	arg := params[0]
	s.mu.Lock()
	active := s.saslActive
	s.mu.Unlock()

	if !active {
		if !strings.EqualFold(arg, "PLAIN") {
			s.numeric("904", ":Only PLAIN is supported")
			return true
		}
		s.mu.Lock()
		s.saslActive = true
		s.mu.Unlock()
		s.send("AUTHENTICATE +")
		return true
	}

	s.mu.Lock()
	s.saslActive = false
	s.mu.Unlock()
	if arg == "*" {
		s.numeric("906", ":SASL aborted")
		return true
	}
	raw, err := base64.StdEncoding.DecodeString(arg)
	if err != nil {
		s.numeric("904", ":SASL authentication failed")
		return true
	}
	// PLAIN is authzid\0authcid\0passwd; only the password half matters here.
	parts := strings.Split(string(raw), "\x00")
	if len(parts) != 3 {
		s.numeric("904", ":SASL authentication failed")
		return true
	}
	return s.tryAuth([]byte(parts[2]), "SASL")
}

// tryAuth is the single place a token is checked. Every failure path is
// identical from the client's side and every failure is counted.
func (s *session) tryAuth(got []byte, method string) bool {
	if banned, rem := s.lim.banned(s.key); banned {
		s.send("ERROR :too many failed authentications, try again in %s", rem.Truncate(time.Second))
		return false
	}
	if !tokenMatch(s.token, got) {
		bannedNow := s.lim.fail(s.key)
		log.Printf("irc: %s: %s authentication failed%s", s.key, method,
			map[bool]string{true: " — source banned", false: ""}[bannedNow])
		if method == "SASL" {
			s.numeric("904", ":SASL authentication failed")
		} else {
			s.numeric("464", ":Password incorrect")
		}
		// A wrong token ends the connection. Reconnecting is allowed, and
		// counted — that is what turns guessing into a ban.
		s.send("ERROR :authentication failed")
		return false
	}
	s.lim.success(s.key)
	s.mu.Lock()
	s.authed = true
	s.mu.Unlock()
	if method == "SASL" {
		s.numeric("900", "%s!user@%s %s :You are now logged in", s.loginNick(), serverName, s.loginNick())
		s.numeric("903", ":SASL authentication successful")
	}
	log.Printf("irc: %s: authenticated via %s", s.key, method)
	s.tryRegister()
	return true
}

func (s *session) loginNick() string {
	if n := s.currentNick(); n != "" {
		return n
	}
	return "*"
}

func (s *session) handleNick(params []string) bool {
	if len(params) == 0 {
		s.numeric("431", ":No nickname given")
		return true
	}
	want := strings.ToLower(params[0])
	if !relay.ValidID(want) {
		s.numeric("432", "%s :Erroneous nickname (need [a-z0-9][a-z0-9_-]{0,63})", params[0])
		return true
	}
	if want == serviceNick {
		s.numeric("433", "%s :Nickname is reserved for the gateway service", params[0])
		return true
	}
	if !s.hub.claimNick(want) {
		s.numeric("433", "%s :Nickname is already in use", params[0])
		return true
	}
	s.mu.Lock()
	old, registered := s.nick, s.registered
	s.nick = want
	s.mu.Unlock()
	if old != "" && old != want {
		s.hub.releaseNick(old)
		if registered {
			s.send(":%s!user@%s NICK :%s", old, serverName, want)
		}
	}
	s.tryRegister()
	return true
}

func (s *session) handleUser(params []string) bool {
	if len(params) < 4 {
		s.numeric("461", "USER :Not enough parameters")
		return true
	}
	s.mu.Lock()
	s.user = params[0]
	s.mu.Unlock()
	s.tryRegister()
	return true
}

// tryRegister completes registration once the token, a nick and a USER line are
// all in hand and capability negotiation has finished.
func (s *session) tryRegister() {
	s.mu.Lock()
	if s.registered || !s.authed || s.nick == "" || s.user == "" || s.capHeld {
		s.mu.Unlock()
		return
	}
	s.registered = true
	nick := s.nick
	s.mu.Unlock()

	s.hub.add(s)
	log.Printf("irc: %s: registered as %s (%d client(s))", s.key, nick, s.hub.clientCount())

	s.numeric("001", ":Welcome to the mcp-dispatch relay, %s", nick)
	s.numeric("002", ":Your host is %s, running dispatch-ircd", serverName)
	s.numeric("003", ":This gateway is a view of a local filesystem relay")
	s.numeric("004", "%s dispatch-ircd o o", serverName)
	s.numeric("005", "CHANTYPES=#& NICKLEN=64 CASEMAPPING=ascii :are supported by this server")
	s.numeric("375", ":- %s message of the day -", serverName)
	for _, l := range []string{
		"mcp-dispatch IRC gateway.",
		"",
		fmt.Sprintf("  %-14s every message crossing the relay (read-only view)", firehose),
		"  #name          a relay channel — JOIN to watch, send to post",
		fmt.Sprintf("  /msg %-9s help, who, ack, replay — what IRC has no verb for", serviceNick),
		"",
		"You are an observer and a sender: this gateway does not claim presence,",
		"so agents do not see your client as a live session.",
	} {
		s.numeric("372", ":- %s", l)
	}
	s.numeric("376", ":End of /MOTD command")

	// Put everyone somewhere useful immediately.
	s.joinChannel(firehose)
}

// ---------------------------------------------------------------------------
// Channels
// ---------------------------------------------------------------------------

func (s *session) handleJoin(params []string) {
	if len(params) == 0 {
		s.numeric("461", "JOIN :Not enough parameters")
		return
	}
	for _, ch := range strings.Split(params[0], ",") {
		s.joinChannel(strings.TrimSpace(ch))
	}
}

func (s *session) joinChannel(ch string) {
	ch = strings.ToLower(ch)
	if ch != firehose {
		if !strings.HasPrefix(ch, "#") || !relay.ValidID(strings.TrimPrefix(ch, "#")) {
			s.numeric("403", "%s :No such channel (need #name, [a-z0-9][a-z0-9_-]{0,63})", ch)
			return
		}
	}
	s.mu.Lock()
	already := s.joined[ch]
	s.joined[ch] = true
	nick := s.nick
	s.mu.Unlock()
	if already {
		return
	}
	s.send(":%s!user@%s JOIN :%s", nick, serverName, ch)
	s.sendTopic(ch)
	s.sendNames(ch)
	s.replay(ch)
}

func (s *session) handlePart(params []string) {
	if len(params) == 0 {
		return
	}
	for _, ch := range strings.Split(params[0], ",") {
		ch = strings.ToLower(strings.TrimSpace(ch))
		s.mu.Lock()
		had := s.joined[ch]
		delete(s.joined, ch)
		nick := s.nick
		s.mu.Unlock()
		if had {
			s.send(":%s!user@%s PART %s", nick, serverName, ch)
		}
	}
}

func (s *session) sendTopic(ch string) {
	topic := fmt.Sprintf("relay channel %s", ch)
	if ch == firehose {
		topic = "every message crossing the relay — read-only, post to #channels or DMs instead"
	}
	s.numeric("332", "%s :%s", ch, topic)
}

func (s *session) sendNames(ch string) {
	var names []string
	if ch == firehose {
		names = s.hub.allNicks()
	} else {
		names = s.hub.membersOf(ch)
	}
	names = append(names, s.currentNick(), serviceNick)
	s.numeric("353", "= %s :%s", ch, strings.Join(names, " "))
	s.numeric("366", "%s :End of /NAMES list", ch)
}

// replay pushes recent matching history into a freshly joined channel, so a
// client that connects mid-conversation has context.
func (s *session) replay(ch string) {
	for _, m := range s.hub.recent(s.cfg.History) {
		for _, line := range s.linesFor(m, ch) {
			s.sendRaw(line)
		}
	}
}

// ---------------------------------------------------------------------------
// Delivery
// ---------------------------------------------------------------------------

// onRelayMessage is called by the hub for every newly seen relay message.
// Delivery is at most once per session: a channel message goes to its channel,
// and only traffic with nowhere else to land is mirrored into the firehose.
func (s *session) onRelayMessage(m relay.Message) {
	if !s.isRegistered() {
		return
	}
	nick := s.currentNick()
	if relay.Project(m.From) == nick {
		return // our own send, echoed back off the disk
	}

	if strings.HasPrefix(m.To, "#") && s.hasJoined(m.To) {
		for _, l := range s.linesFor(m, m.To) {
			s.sendRaw(l)
		}
		return
	}
	if m.To == nick || relay.Project(m.To) == nick {
		for _, l := range s.linesFor(m, nick) {
			s.sendRaw(l)
		}
		return
	}
	if s.hasJoined(firehose) {
		for _, l := range s.linesFor(m, firehose) {
			s.sendRaw(l)
		}
	}
}

// linesFor renders a relay message as the IRC lines it should produce in the
// given context, or nil if it doesn't belong there.
func (s *session) linesFor(m relay.Message, ctx string) []string {
	nick := s.currentNick()
	from := relay.Project(m.From)
	if from == "" {
		from = "unknown"
	}
	if from == nick {
		return nil
	}

	var target, prefix string
	switch {
	case ctx == firehose:
		target = firehose
		to := m.To
		if to == "" {
			to = "?"
		}
		prefix = "→" + to + " "
	case strings.HasPrefix(ctx, "#"):
		if m.To != ctx {
			return nil
		}
		target = ctx
	default: // a DM view
		if m.To != nick && relay.Project(m.To) != nick {
			return nil
		}
		target = nick
	}

	flags := messageFlags(m)
	var out []string
	for _, chunk := range chunkContent(m.Content) {
		out = append(out, fmt.Sprintf(":%s!agent@%s PRIVMSG %s :%s%s%s",
			from, serverName, target, prefix, flags, chunk))
		flags = "" // only the first line of a multi-line body carries the flags
	}
	return out
}

func messageFlags(m relay.Message) string {
	var b strings.Builder
	if m.MustRead {
		b.WriteString("🔒 ")
	}
	switch m.Priority {
	case "urgent":
		b.WriteString("‼ ")
	case "high":
		b.WriteString("! ")
	}
	if m.Remote() {
		b.WriteString("«remote» ")
	}
	if m.ThreadID != "" {
		b.WriteString("[" + m.ThreadID + "] ")
	}
	return b.String()
}

// chunkContent splits a body into wire-safe lines: newlines become separate
// PRIVMSGs (IRC has no multi-line message), and long runs are hard-split.
func chunkContent(content string) []string {
	var out []string
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimRight(line, "\r")
		if line == "" {
			continue
		}
		for len(line) > ircChunk {
			cut := ircChunk
			// prefer a space so words survive the split
			if i := strings.LastIndexByte(line[:cut], ' '); i > ircChunk/2 {
				cut = i
			}
			out = append(out, line[:cut])
			line = strings.TrimSpace(line[cut:])
		}
		if line != "" {
			out = append(out, line)
		}
	}
	if len(out) == 0 {
		out = []string{"(empty message)"}
	}
	return out
}

// ---------------------------------------------------------------------------
// Sending
// ---------------------------------------------------------------------------

func (s *session) handlePrivmsg(params []string, notice bool) {
	if len(params) < 2 {
		s.numeric("411", ":No recipient given")
		return
	}
	target, text := params[0], params[len(params)-1]
	if strings.TrimSpace(text) == "" {
		s.numeric("412", ":No text to send")
		return
	}
	if notice {
		return // notices are not relayed — they'd loop against other bots
	}

	switch {
	case strings.EqualFold(target, serviceNick):
		s.service(text)
	case strings.EqualFold(target, firehose):
		s.notice("%s is a read-only view. Post to a #channel or /msg a nick.", firehose)
	case strings.HasPrefix(target, "#"):
		s.relaySend(strings.ToLower(target), text, "normal")
	case strings.HasPrefix(target, "&"):
		s.numeric("403", "%s :No such channel", target)
	default:
		s.relaySend(strings.ToLower(target), text, "normal")
	}
}

func (s *session) relaySend(target, text, priority string) {
	nick := s.currentNick()
	n, err := s.hub.send(nick, target, text, priority)
	if err != nil {
		s.notice("send to %s failed: %v", target, err)
		return
	}
	if n == 0 {
		s.notice("queued to nobody — %s has no live subscribers right now", target)
	}
}

// notice speaks to the client as the service nick.
func (s *session) notice(format string, args ...any) {
	nick := s.currentNick()
	for _, chunk := range chunkContent(fmt.Sprintf(format, args...)) {
		s.send(":%s!service@%s NOTICE %s :%s", serviceNick, serverName, nick, chunk)
	}
}

// service implements the verbs IRC has no room for.
func (s *session) service(text string) {
	fields := strings.Fields(text)
	if len(fields) == 0 {
		return
	}
	switch strings.ToLower(fields[0]) {
	case "help":
		for _, l := range []string{
			"help                     this",
			"who                      the relay roster (live / remote)",
			"ack                      acknowledge everything in your own inbox",
			"replay [n]               re-send the last n messages into " + firehose,
			"urgent <target> <text>   send at urgent priority",
			"channels                 list relay channels",
			"tasks [state]            the task board (open / claimed / done)",
		} {
			s.notice("%s", l)
		}
	case "who":
		agents := s.hub.agents()
		if len(agents) == 0 {
			s.notice("nobody on the relay right now")
			return
		}
		for _, a := range agents {
			state := "offline"
			switch {
			case a.Live:
				state = "live"
			case a.Remote:
				state = "remote"
			}
			chans := ""
			if len(a.Channels) > 0 {
				chans = " #" + strings.Join(a.Channels, " #")
			}
			s.notice("%-28s %-7s%s", a.ID, state, chans)
		}
	case "ack":
		n, err := s.hub.ack(s.currentNick())
		if err != nil {
			s.notice("ack failed: %v", err)
			return
		}
		s.notice("acknowledged %d message(s) in %s's inbox", n, s.currentNick())
	case "channels":
		chans := s.hub.channels()
		if len(chans) == 0 {
			s.notice("no channels are in use")
			return
		}
		s.notice("#%s", strings.Join(chans, " #"))
	case "tasks":
		want := ""
		if len(fields) > 1 {
			want = strings.ToLower(fields[1])
		}
		var shown int
		for _, tk := range s.hub.tasks() {
			if want != "" && tk.State != want {
				continue
			}
			who := tk.ClaimedBy
			if who == "" {
				who = "-"
			}
			s.notice("%-14s %-8s %-16s %s", tk.ID, tk.State, who, tk.Title)
			shown++
		}
		if shown == 0 {
			s.notice("no tasks%s", map[bool]string{true: "", false: " in state " + want}[want == ""])
		}
	case "replay":
		n := s.cfg.History
		if len(fields) > 1 {
			if v, err := parseCount(fields[1]); err == nil {
				n = v
			}
		}
		for _, m := range s.hub.recent(n) {
			for _, l := range s.linesFor(m, firehose) {
				s.sendRaw(l)
			}
		}
	case "urgent":
		if len(fields) < 3 {
			s.notice("usage: urgent <target> <text>")
			return
		}
		target := strings.ToLower(fields[1])
		body := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(text), fields[0]))
		body = strings.TrimSpace(strings.TrimPrefix(body, fields[1]))
		s.relaySend(target, body, "urgent")
	default:
		s.notice("unknown command %q — try: help", fields[0])
	}
}

func parseCount(s string) (int, error) {
	var n int
	_, err := fmt.Sscanf(s, "%d", &n)
	if err != nil || n < 0 {
		return 0, fmt.Errorf("bad count")
	}
	if n > maxTranscript {
		n = maxTranscript
	}
	return n, nil
}

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

func (s *session) handleWho(params []string) {
	mask := firehose
	if len(params) > 0 {
		mask = params[0]
	}
	var names []string
	if strings.HasPrefix(mask, "#") {
		names = s.hub.membersOf(mask)
	} else {
		names = s.hub.allNicks()
	}
	for _, n := range names {
		s.numeric("352", "%s agent %s %s %s H :0 relay agent", mask, serverName, serverName, n)
	}
	s.numeric("315", "%s :End of /WHO list", mask)
}

func (s *session) handleWhois(params []string) {
	if len(params) == 0 {
		return
	}
	want := strings.ToLower(params[0])
	for _, a := range s.hub.agents() {
		if relay.Project(a.ID) != want {
			continue
		}
		state := "offline"
		switch {
		case a.Live:
			state = "live on this host"
		case a.Remote:
			state = "reachable over the git bus"
		}
		s.numeric("311", "%s agent %s * :%s (%s)", want, serverName, a.ID, state)
		if len(a.Channels) > 0 {
			s.numeric("319", "%s :#%s", want, strings.Join(a.Channels, " #"))
		}
	}
	s.numeric("318", "%s :End of /WHOIS list", want)
}

func (s *session) handleList() {
	s.numeric("321", "Channel :Users  Name")
	s.numeric("322", "%s %d :every message crossing the relay", firehose, len(s.hub.allNicks()))
	for _, c := range s.hub.channels() {
		ch := "#" + c
		s.numeric("322", "%s %d :relay channel", ch, len(s.hub.membersOf(ch)))
	}
	s.numeric("323", ":End of /LIST")
}

func (s *session) handleMode(params []string) {
	if len(params) == 0 {
		return
	}
	t := params[0]
	if strings.HasPrefix(t, "#") || strings.HasPrefix(t, "&") {
		if len(params) == 1 {
			s.numeric("324", "%s +n", t)
		}
		return
	}
	if len(params) == 1 {
		s.numeric("221", "+i")
	}
}
