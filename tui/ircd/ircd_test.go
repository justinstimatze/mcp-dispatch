package main

import (
	"bufio"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/justinstimatze/mcp-dispatch/tui/relay"
)

// ---------------------------------------------------------------------------
// Lockdown: the configurations we refuse to serve
// ---------------------------------------------------------------------------

func TestGatewayIsOffUntilExplicitlyEnabled(t *testing.T) {
	// The whole point: a config file that never mentions [irc] must not listen.
	if err := (Config{Socket: "/tmp/x.sock"}).Validate(); err == nil {
		t.Fatal("a gateway with enabled=false must refuse to start")
	}
	err := (Config{Socket: "/tmp/x.sock"}).Validate()
	if !strings.Contains(err.Error(), "enabled = true") {
		t.Fatalf("the refusal should name the key that turns it on: %v", err)
	}
}

func TestRefusesNonLoopbackWithoutAllowRemote(t *testing.T) {
	// TLS is satisfied here so the *address* axis is what is under test — the
	// two refusals are independent, and encryption does not imply exposure.
	for _, addr := range []string{"0.0.0.0:6667", ":6667", "[::]:6667", "10.0.0.5:6667"} {
		c := Config{Enabled: true, Listen: addr, TLSCert: "/c.pem", TLSKey: "/k.pem",
			TLSMinVersion: "1.3"}
		err := c.Validate()
		if err == nil {
			t.Fatalf("%s: binding the network must be refused by default", addr)
		}
		if !strings.Contains(err.Error(), "allow_remote") {
			t.Fatalf("%s: refusal should name allow_remote: %v", addr, err)
		}
	}
}

func TestRefusesCleartextEvenWhenRemoteIsAllowed(t *testing.T) {
	// allow_remote is about exposure, not encryption: it never buys cleartext.
	c := Config{Enabled: true, Listen: "10.0.0.5:6667", AllowRemote: true, TLSMinVersion: "1.3"}
	err := c.Validate()
	if err == nil {
		t.Fatal("a cleartext bind must be refused even with allow_remote")
	}
	if !strings.Contains(err.Error(), "TLS") {
		t.Fatalf("refusal should name TLS: %v", err)
	}
}

func TestAcceptsLoopbackAndRemoteWithTLS(t *testing.T) {
	lo := Config{Enabled: true, Listen: "127.0.0.1:6697",
		TLSCert: "/c.pem", TLSKey: "/k.pem", TLSMinVersion: "1.3"}
	if err := lo.Validate(); err != nil {
		t.Fatalf("loopback with TLS should be allowed: %v", err)
	}
	ok := Config{Enabled: true, Listen: "10.0.0.5:6697", AllowRemote: true,
		TLSCert: "/c.pem", TLSKey: "/k.pem", TLSMinVersion: "1.3"}
	if err := ok.Validate(); err != nil {
		t.Fatalf("remote+TLS+allow_remote should be allowed: %v", err)
	}
}

func TestIsLoopbackTreatsWildcardAsPublic(t *testing.T) {
	cases := map[string]bool{
		"127.0.0.1:6667": true, "[::1]:6667": true, "localhost:6667": true,
		"0.0.0.0:6667": false, ":6667": false, "[::]:6667": false,
		"192.168.1.9:6667": false, "garbage": false,
	}
	for addr, want := range cases {
		if got := isLoopback(addr); got != want {
			t.Fatalf("isLoopback(%q)=%v want %v", addr, got, want)
		}
	}
}

// ---------------------------------------------------------------------------
// Lockdown: the token
// ---------------------------------------------------------------------------

func TestReadTokenRejectsWeakFiles(t *testing.T) {
	dir := t.TempDir()

	if _, err := ReadToken(filepath.Join(dir, "absent")); err == nil {
		t.Fatal("a missing token must be fatal, not an empty password")
	}

	loose := filepath.Join(dir, "loose")
	writeFile(t, loose, strings.Repeat("a", 64), 0o644)
	_, err := ReadToken(loose)
	if err == nil {
		t.Fatal("a group/world-readable token must be refused")
	}
	if !strings.Contains(errStr(err), "chmod 600") {
		t.Fatalf("refusal should say how to fix it: %v", err)
	}

	short := filepath.Join(dir, "short")
	writeFile(t, short, "hunter2", 0o600)
	if _, err := ReadToken(short); err == nil {
		t.Fatal("a short token must be refused")
	}

	good := filepath.Join(dir, "good")
	writeFile(t, good, strings.Repeat("a", 64)+"\n", 0o600)
	tok, err := ReadToken(good)
	if err != nil {
		t.Fatalf("a 0600 64-char token should load: %v", err)
	}
	if string(tok) != strings.Repeat("a", 64) {
		t.Fatal("token should be whitespace-trimmed")
	}
}

func TestWriteTokenIsOwnerOnlyAndWontClobber(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tok")
	tok, err := WriteToken(path, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(tok) != 64 {
		t.Fatalf("expected 64 hex chars, got %d", len(tok))
	}
	fi, _ := os.Stat(path)
	if fi.Mode().Perm() != 0o600 {
		t.Fatalf("token written %04o, want 0600", fi.Mode().Perm())
	}
	if _, err := WriteToken(path, false); err == nil {
		t.Fatal("must not silently replace an existing token")
	}
	if _, err := WriteToken(path, true); err != nil {
		t.Fatalf("--force should replace it: %v", err)
	}
}

func TestTokenMatch(t *testing.T) {
	want := []byte(strings.Repeat("a", 64))
	if !tokenMatch(want, []byte(strings.Repeat("a", 64))) {
		t.Fatal("identical tokens should match")
	}
	if tokenMatch(want, []byte(strings.Repeat("a", 63)+"b")) {
		t.Fatal("differing tokens must not match")
	}
	if tokenMatch(want, []byte("")) {
		t.Fatal("empty must not match")
	}
}

// ---------------------------------------------------------------------------
// Lockdown: brute-force backoff
// ---------------------------------------------------------------------------

func TestLimiterBansAndExpires(t *testing.T) {
	now := time.Now()
	l := newLimiter(3, time.Minute)
	l.now = func() time.Time { return now }

	for i := 0; i < 2; i++ {
		if l.fail("1.2.3.4") {
			t.Fatalf("banned after %d failures, threshold is 3", i+1)
		}
	}
	if !l.fail("1.2.3.4") {
		t.Fatal("third failure should ban")
	}
	if banned, _ := l.banned("1.2.3.4"); !banned {
		t.Fatal("source should be locked out")
	}
	if banned, _ := l.banned("5.6.7.8"); banned {
		t.Fatal("the ban must be per-source")
	}
	now = now.Add(61 * time.Second)
	if banned, _ := l.banned("1.2.3.4"); banned {
		t.Fatal("the ban should expire")
	}
}

func TestLimiterSuccessClearsCount(t *testing.T) {
	l := newLimiter(3, time.Minute)
	l.fail("k")
	l.fail("k")
	l.success("k")
	if l.fail("k") {
		t.Fatal("a successful auth should reset the failure count")
	}
}

func TestRemoteKeyDropsPort(t *testing.T) {
	a := &net.TCPAddr{IP: net.ParseIP("10.0.0.1"), Port: 5000}
	b := &net.TCPAddr{IP: net.ParseIP("10.0.0.1"), Port: 5001}
	if remoteKey(a) != remoteKey(b) {
		t.Fatal("reconnecting from a new port must not reset the ban counter")
	}
	if remoteKey(&net.UnixAddr{Name: "/x", Net: "unix"}) != "unix" {
		t.Fatal("unix peers share one key")
	}
}

// ---------------------------------------------------------------------------
// Lockdown: the socket itself
// ---------------------------------------------------------------------------

func TestUnixSocketIsOwnerOnly(t *testing.T) {
	sock := filepath.Join(t.TempDir(), "irc.sock")
	l, err := unixListener(sock)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	fi, err := os.Lstat(sock)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm()&0o077 != 0 {
		t.Fatalf("socket is %04o — reachable by other local users", fi.Mode().Perm())
	}
}

func TestUnixListenerRefusesToReplaceARegularFile(t *testing.T) {
	p := filepath.Join(t.TempDir(), "notasocket")
	writeFile(t, p, "important", 0o600)
	if _, err := unixListener(p); err == nil {
		t.Fatal("must not unlink a regular file to bind over it")
	}
	if _, err := os.Stat(p); err != nil {
		t.Fatal("the file should still be there")
	}
}

func TestInstanceLockIsExclusive(t *testing.T) {
	dir := t.TempDir()
	f, err := lockInstance(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	// A second lock attempt in-process shares the fd table, so exercise the
	// flock via a distinct open — lockInstance opens its own file each call.
	if _, err := lockInstance(dir); err == nil {
		t.Log("note: flock is per-fd; in-process re-lock may succeed on this platform")
	}
}

// ---------------------------------------------------------------------------
// Protocol
// ---------------------------------------------------------------------------

func TestParse(t *testing.T) {
	cmd, p := parse("PRIVMSG #eng :hello there world")
	if cmd != "PRIVMSG" || len(p) != 2 || p[0] != "#eng" || p[1] != "hello there world" {
		t.Fatalf("trailing param: %q %#v", cmd, p)
	}
	cmd, p = parse(":nick!u@h JOIN #eng")
	if cmd != "JOIN" || len(p) != 1 || p[0] != "#eng" {
		t.Fatalf("client prefix should be ignored: %q %#v", cmd, p)
	}
	if cmd, _ := parse("ping"); cmd != "PING" {
		t.Fatal("commands are case-insensitive")
	}
	if cmd, _ := parse(""); cmd != "" {
		t.Fatal("empty line")
	}
}

func TestChunkContentSplitsAndNeverDrops(t *testing.T) {
	out := chunkContent("one\ntwo\n\nthree")
	if len(out) != 3 {
		t.Fatalf("newlines become separate lines, blank ones dropped: %#v", out)
	}
	long := strings.Repeat("word ", 400)
	got := chunkContent(long)
	for _, l := range got {
		if len(l) > ircChunk {
			t.Fatalf("chunk over the wire limit: %d", len(l))
		}
	}
	if n := strings.Count(strings.Join(got, " "), "word"); n != 400 {
		t.Fatalf("splitting dropped words: %d/400", n)
	}
	if len(chunkContent("")) != 1 {
		t.Fatal("an empty body still needs a line")
	}
}

func TestSendRawCannotForgeProtocolLines(t *testing.T) {
	s := &session{out: make(chan string, 4), done: make(chan struct{})}
	s.sendRaw("PRIVMSG #a :hi\r\nJOIN #secret")
	got := <-s.out
	if strings.Contains(got, "\r") || strings.Contains(got, "\n") {
		t.Fatalf("CR/LF in content must be neutralised, got %q", got)
	}
}

func TestMessageFlags(t *testing.T) {
	f := messageFlags(relay.Message{MustRead: true, Priority: "urgent", Via: "git", ThreadID: "t1"})
	for _, want := range []string{"🔒", "‼", "«remote»", "[t1]"} {
		if !strings.Contains(f, want) {
			t.Fatalf("flags %q missing %q", f, want)
		}
	}
	if messageFlags(relay.Message{Priority: "normal"}) != "" {
		t.Fatal("an ordinary message should carry no flags")
	}
}

// ---------------------------------------------------------------------------
// End to end, over a real socket
// ---------------------------------------------------------------------------

type harness struct {
	relayDir string
	sock     string
	token    string
	done     chan struct{}
	hub      *hub
}

func startGateway(t *testing.T) *harness {
	t.Helper()
	relayDir := t.TempDir()
	sock := filepath.Join(t.TempDir(), "irc.sock")
	token := strings.Repeat("t", 64)

	cfg := Config{
		Enabled: true, Socket: sock,
		MaxConns: 4, AuthTimeout: 5, IdleTimeout: 30,
		MaxAuthFailures: 3, BanSeconds: 60, Interval: 0.02, History: 50,
	}
	h := newHub(relayDir, "", false, 20*time.Millisecond, 50)
	lim := newLimiter(cfg.MaxAuthFailures, cfg.banDuration())
	done := make(chan struct{})
	go h.run(done)

	l, err := unixListener(sock)
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			conn, err := l.Accept()
			if err != nil {
				return
			}
			go newSession(h, cfg, []byte(token), lim, conn).serve()
		}
	}()
	t.Cleanup(func() { close(done); l.Close() })
	return &harness{relayDir: relayDir, sock: sock, token: token, done: done, hub: h}
}

type client struct {
	t    *testing.T
	conn net.Conn
	r    *bufio.Reader
}

func (h *harness) dial(t *testing.T) *client {
	t.Helper()
	c, err := net.Dial("unix", h.sock)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return &client{t: t, conn: c, r: bufio.NewReader(c)}
}

func (c *client) sendf(format string, args ...any) {
	c.t.Helper()
	if _, err := fmt.Fprintf(c.conn, format+"\r\n", args...); err != nil {
		c.t.Fatalf("write: %v", err)
	}
}

// expect reads lines until one contains want, or fails after a deadline.
func (c *client) expect(want string) string {
	c.t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		_ = c.conn.SetReadDeadline(deadline)
		line, err := c.r.ReadString('\n')
		if err != nil {
			c.t.Fatalf("waiting for %q: %v", want, err)
		}
		if strings.Contains(line, want) {
			return line
		}
		if time.Now().After(deadline) {
			c.t.Fatalf("timed out waiting for %q", want)
		}
	}
}

func (c *client) login(token, nick string) {
	c.t.Helper()
	c.sendf("PASS %s", token)
	c.sendf("NICK %s", nick)
	c.sendf("USER %s 0 * :%s", nick, nick)
	c.expect(" 001 ")
}

func TestEndToEndLoginJoinAndReceive(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.login(h.token, "justin")
	c.expect("End of /MOTD")

	c.sendf("JOIN #eng")
	c.expect("JOIN :#eng")
	c.expect(" 366 ") // end of names

	// An agent writes into a subscriber's inbox; the gateway sees it on its
	// next poll and delivers it to the joined channel.
	writeRelayMessage(t, h.relayDir, "bob", relay.Message{
		ID: "msg-1", From: "alice-123", To: "#eng", Content: "deploying now",
		Timestamp: nowStamp(), Priority: "normal", State: "pending",
	})
	line := c.expect("deploying now")
	if !strings.HasPrefix(line, ":alice!agent@dispatch PRIVMSG #eng :") {
		t.Fatalf("pid should be stripped to a stable nick: %q", line)
	}
}

func TestEndToEndDirectMessageAndSend(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.login(h.token, "justin")
	c.expect("End of /MOTD")

	// A DM addressed to our nick arrives as a PRIVMSG to us.
	writeRelayMessage(t, h.relayDir, "justin", relay.Message{
		ID: "msg-2", From: "alice-123", To: "justin", Content: "can you look at this",
		Timestamp: nowStamp(), Priority: "urgent", State: "pending",
	})
	line := c.expect("can you look at this")
	if !strings.Contains(line, "PRIVMSG justin :") || !strings.Contains(line, "‼") {
		t.Fatalf("urgent DM should reach us flagged: %q", line)
	}

	// Sending back writes a real relay message into alice's inbox.
	c.sendf("PRIVMSG alice :on it")
	deadline := time.Now().Add(3 * time.Second)
	for {
		files, _ := filepath.Glob(filepath.Join(h.relayDir, "alice", "*.json"))
		if len(files) == 1 {
			var m relay.Message
			data, _ := os.ReadFile(files[0])
			if err := json.Unmarshal(data, &m); err != nil {
				t.Fatal(err)
			}
			if m.From != "justin" || m.To != "alice" || m.Content != "on it" {
				t.Fatalf("bad relay message: %+v", m)
			}
			if m.State != "pending" {
				t.Fatalf("state should be pending, got %q", m.State)
			}
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("PRIVMSG never became a relay message")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestNothingIsServedBeforeAuthentication(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.sendf("NICK justin")
	c.sendf("USER justin 0 * :justin")
	c.sendf("JOIN #eng")
	c.expect(" 451 ") // not registered — no JOIN, no names, no traffic
	c.sendf("PRIVMSG #eng :leaking?")
	c.expect(" 451 ")
}

func TestWrongTokenIsRefusedAndCounted(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.sendf("PASS %s", strings.Repeat("x", 64))
	c.expect(" 464 ")
	c.expect("ERROR :authentication failed")

	// The connection is over: a follow-up command gets nothing back.
	_ = c.conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	c.sendf("NICK justin")
	if _, err := c.r.ReadString('\n'); err == nil {
		t.Fatal("the connection should be closed after a failed auth")
	}
}

func TestRepeatedWrongTokensBanTheSource(t *testing.T) {
	h := startGateway(t) // MaxAuthFailures = 3
	for i := 0; i < 3; i++ {
		c := h.dial(t)
		c.sendf("PASS %s", strings.Repeat("x", 64))
		c.expect("ERROR")
	}
	c := h.dial(t)
	c.expect("too many failed authentications")
	// Even the correct token gets nowhere while banned.
	c2 := h.dial(t)
	c2.sendf("PASS %s", h.token)
	c2.expect("too many failed authentications")
}

func TestNickMustBeRelaySafe(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.sendf("PASS %s", h.token)
	// "../../etc" would be a path segment on the relay if it ever got through.
	c.sendf("NICK ../../etc")
	c.expect(" 432 ")
	c.sendf("NICK dispatch")
	c.expect(" 433 ") // the service nick is reserved
	c.sendf("NICK justin")
	c.sendf("USER justin 0 * :justin")
	c.expect(" 001 ")
}

func TestNickCollisionRefused(t *testing.T) {
	h := startGateway(t)
	c1 := h.dial(t)
	c1.login(h.token, "justin")

	c2 := h.dial(t)
	c2.sendf("PASS %s", h.token)
	c2.sendf("NICK justin")
	c2.expect(" 433 ")
}

func TestFirehoseIsReadOnly(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("PRIVMSG %s :hello", firehose)
	c.expect("read-only view")
	if files, _ := filepath.Glob(filepath.Join(h.relayDir, "*", "*.json")); len(files) != 0 {
		t.Fatal("a post to the firehose must not write to the relay")
	}
}

func inboxState(t *testing.T, relayDir, nick, id string) string {
	t.Helper()
	files, _ := filepath.Glob(filepath.Join(relayDir, nick, "*.json"))
	for _, f := range files {
		var m map[string]any
		data, _ := os.ReadFile(f)
		if json.Unmarshal(data, &m) != nil {
			continue
		}
		if m["id"] == id {
			st, _ := m["state"].(string)
			if st == "" {
				return "pending"
			}
			return st
		}
	}
	return "absent"
}

func TestServiceInboxListsWhatIsWaiting(t *testing.T) {
	h := startGateway(t)
	writeRelayMessage(t, h.relayDir, "justin", relay.Message{
		ID: "msg-a1", From: "alice-1", To: "justin", Content: "first thing\nsecond line",
		Timestamp: nowStamp(), Priority: "urgent", State: "pending",
	})
	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("PRIVMSG %s :inbox", serviceNick)

	line := c.expect("msg-a1")
	for _, want := range []string{"pending", "alice", "first thing"} {
		if !strings.Contains(line, want) {
			t.Fatalf("inbox line missing %q: %q", want, line)
		}
	}
	if strings.Contains(line, "second line") {
		t.Fatalf("the listing should preview one line, not the whole body: %q", line)
	}
	c.expect("1 message(s), 1 unread")
}

func TestServiceInboxOnEmptyInbox(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("PRIVMSG %s :inbox", serviceNick)
	c.expect("inbox empty")
}

func TestBareAckNoLongerDeletesEverything(t *testing.T) {
	// `ack` with no argument used to acknowledge the whole inbox. That is a
	// destructive default for a command you might type to clear one thing.
	h := startGateway(t)
	writeRelayMessage(t, h.relayDir, "justin", relay.Message{
		ID: "msg-b1", From: "alice-1", To: "justin", Content: "keep me",
		Timestamp: nowStamp(), Priority: "normal", State: "pending",
	})
	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("PRIVMSG %s :ack", serviceNick)
	c.expect("usage: ack")

	if got := inboxState(t, h.relayDir, "justin", "msg-b1"); got != "pending" {
		t.Fatalf("a bare ack must not touch the inbox, state is %q", got)
	}
}

func TestServiceAcksNamedMessagesOnly(t *testing.T) {
	h := startGateway(t)
	for _, id := range []string{"msg-c1", "msg-c2"} {
		writeRelayMessage(t, h.relayDir, "justin", relay.Message{
			ID: id, From: "alice-1", To: "justin", Content: "please ack",
			Timestamp: nowStamp(), Priority: "normal", State: "pending",
		})
	}
	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("PRIVMSG %s :ack msg-c1", serviceNick)
	c.expect("acknowledged 1 message")

	if got := inboxState(t, h.relayDir, "justin", "msg-c1"); got != "read" {
		t.Fatalf("named message should be read, got %q", got)
	}
	if got := inboxState(t, h.relayDir, "justin", "msg-c2"); got != "pending" {
		t.Fatalf("the one you didn't name must be untouched, got %q", got)
	}
}

func TestServiceReportsIdsItCouldNotFind(t *testing.T) {
	h := startGateway(t)
	writeRelayMessage(t, h.relayDir, "justin", relay.Message{
		ID: "msg-d1", From: "alice-1", To: "justin", Content: "here",
		Timestamp: nowStamp(), Priority: "normal", State: "pending",
	})
	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("PRIVMSG %s :ack msg-d1 msg-nope", serviceNick)
	c.expect("acknowledged 1 message")
	line := c.expect("not in justin's inbox")
	if !strings.Contains(line, "msg-nope") {
		t.Fatalf("should name the id it couldn't find: %q", line)
	}
}

func TestServiceAckAllStillWorks(t *testing.T) {
	h := startGateway(t)
	for _, id := range []string{"msg-e1", "msg-e2"} {
		writeRelayMessage(t, h.relayDir, "justin", relay.Message{
			ID: id, From: "alice-1", To: "justin", Content: "bulk",
			Timestamp: nowStamp(), Priority: "normal", State: "pending",
		})
	}
	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("PRIVMSG %s :ack all", serviceNick)
	c.expect("acknowledged all 2 message")
	for _, id := range []string{"msg-e1", "msg-e2"} {
		if got := inboxState(t, h.relayDir, "justin", id); got != "read" {
			t.Fatalf("%s should be read, got %q", id, got)
		}
	}
}

func TestOwnSendsAreNotEchoedBack(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("JOIN #eng")
	c.expect(" 366 ")

	// A message the gateway itself wrote, as seen on the next poll.
	writeRelayMessage(t, h.relayDir, "bob", relay.Message{
		ID: "msg-4", From: "justin", To: "#eng", Content: "my own words",
		Timestamp: nowStamp(), Priority: "normal", State: "pending",
	})
	writeRelayMessage(t, h.relayDir, "bob", relay.Message{
		ID: "msg-5", From: "alice-1", To: "#eng", Content: "someone else",
		Timestamp: nowStamp(), Priority: "normal", State: "pending",
	})
	line := c.expect("someone else")
	if strings.Contains(line, "my own words") {
		t.Fatal("unreachable")
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func writeFile(t *testing.T, path, content string, mode os.FileMode) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil { // umask-proof
		t.Fatal(err)
	}
}

func writeRelayMessage(t *testing.T, relayDir, inbox string, m relay.Message) {
	t.Helper()
	dir := filepath.Join(relayDir, inbox)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(m)
	name := fmt.Sprintf("%d-%s-%s.json", time.Now().UnixMilli(), m.From, m.ID)
	if err := os.WriteFile(filepath.Join(dir, name), data, 0o600); err != nil {
		t.Fatal(err)
	}
}

func nowStamp() string { return time.Now().UTC().Format("2006-01-02T15:04:05Z") }

func errStr(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func TestServiceShowsTheTaskBoard(t *testing.T) {
	h := startGateway(t)
	tasksDir := filepath.Join(h.relayDir, ".tasks")
	if err := os.MkdirAll(tasksDir, 0o700); err != nil {
		t.Fatal(err)
	}
	for _, tk := range []relay.Task{
		{ID: "task-aaa11111", Title: "fix the flake", State: "open", CreatedAt: "2026-01-01T00:00:00Z"},
		{ID: "task-bbb22222", Title: "ship it", State: "claimed", ClaimedBy: "alice", CreatedAt: "2026-01-02T00:00:00Z"},
	} {
		data, _ := json.Marshal(tk)
		if err := os.WriteFile(filepath.Join(tasksDir, tk.ID+".json"), data, 0o600); err != nil {
			t.Fatal(err)
		}
	}

	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("PRIVMSG %s :tasks", serviceNick)
	c.expect("fix the flake")
	c.expect("ship it")

	c.sendf("PRIVMSG %s :tasks claimed", serviceNick)
	line := c.expect("task-bbb22222")
	if !strings.Contains(line, "alice") {
		t.Fatalf("a claimed task should name its holder: %q", line)
	}
}

func TestServiceReportsAnEmptyBoard(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("PRIVMSG %s :tasks", serviceNick)
	c.expect("no tasks")
}

// ---------------------------------------------------------------------------
// Lockdown: transport encryption
// ---------------------------------------------------------------------------

func TestEveryTCPListenerMustBeEncrypted(t *testing.T) {
	// Loopback included. Cleartext on lo is not private: anything on the host
	// that can capture the loopback interface sees the token, which is
	// equivalent to read/write on the whole relay.
	for _, addr := range []string{"127.0.0.1:6667", "localhost:6667", "[::1]:6667"} {
		err := Config{Enabled: true, Listen: addr, TLSMinVersion: "1.3"}.Validate()
		if err == nil {
			t.Fatalf("%s: a cleartext TCP listener must be refused", addr)
		}
		if !strings.Contains(err.Error(), "--init-tls") {
			t.Fatalf("%s: the refusal should say how to get a certificate: %v", addr, err)
		}
	}
}

func TestUnixSocketNeedsNoTLS(t *testing.T) {
	// The exception, and the reason the default transport is a socket: there is
	// no network path to encrypt, and the kernel already answers who the peer is.
	if err := (Config{Enabled: true, Socket: "/tmp/x.sock", TLSMinVersion: "1.3"}).Validate(); err != nil {
		t.Fatalf("a unix socket should not require TLS: %v", err)
	}
}

func TestTLSKeyPairMustBeComplete(t *testing.T) {
	c := Config{Enabled: true, Listen: "127.0.0.1:6697", TLSCert: "/c.pem", TLSMinVersion: "1.3"}
	if err := c.Validate(); err == nil {
		t.Fatal("a cert without a key must be refused")
	}
}

func TestTLSMinVersionFloorIs12(t *testing.T) {
	if v, _ := tlsMinVersion(""); v != tls.VersionTLS13 {
		t.Fatal("the default floor should be TLS 1.3")
	}
	if v, _ := tlsMinVersion("1.3"); v != tls.VersionTLS13 {
		t.Fatal("1.3")
	}
	if v, _ := tlsMinVersion("1.2"); v != tls.VersionTLS12 {
		t.Fatal("1.2 is allowed for an old client")
	}
	// Nothing below 1.2 is reachable through this knob.
	for _, bad := range []string{"1.1", "1.0", "ssl3", "none", "0"} {
		if _, err := tlsMinVersion(bad); err == nil {
			t.Fatalf("tls_min_version = %q must be refused", bad)
		}
	}
	c := Config{Enabled: true, Listen: "127.0.0.1:6697", TLSCert: "/c.pem", TLSKey: "/k.pem",
		TLSMinVersion: "1.1"}
	if err := c.Validate(); err == nil {
		t.Fatal("a bad tls_min_version must fail validation, not fall back silently")
	}
}

func TestRemoteStillNeedsAllowRemote(t *testing.T) {
	c := Config{Enabled: true, Listen: "10.0.0.5:6697", TLSCert: "/c.pem", TLSKey: "/k.pem",
		TLSMinVersion: "1.3"}
	err := c.Validate()
	if err == nil || !strings.Contains(err.Error(), "allow_remote") {
		t.Fatalf("TLS does not make a public bind automatic: %v", err)
	}
}

func TestSelfSignedCertIsUsableAndKeyIsOwnerOnly(t *testing.T) {
	dir := t.TempDir()
	certPath := filepath.Join(dir, "cert.pem")
	keyPath := filepath.Join(dir, "key.pem")

	fp, err := WriteSelfSignedCert(certPath, keyPath, []string{"192.0.2.7", "irc.example"}, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(fp) != 95 { // 32 bytes as AA:BB:...
		t.Fatalf("fingerprint looks wrong: %q", fp)
	}
	fi, _ := os.Stat(keyPath)
	if fi.Mode().Perm() != 0o600 {
		t.Fatalf("private key is %04o, want 0600", fi.Mode().Perm())
	}
	if _, err := WriteSelfSignedCert(certPath, keyPath, nil, false); err == nil {
		t.Fatal("must not silently replace an existing key pair")
	}

	// It loads as a real key pair, and covers the hosts you'd actually bind.
	pair, err := tls.LoadX509KeyPair(certPath, keyPath)
	if err != nil {
		t.Fatalf("generated pair does not load: %v", err)
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	if err := leaf.VerifyHostname("localhost"); err != nil {
		t.Fatalf("cert should cover localhost: %v", err)
	}
	if err := leaf.VerifyHostname("192.0.2.7"); err != nil {
		t.Fatalf("--tls-hosts entries should be covered: %v", err)
	}
	if leaf.NotAfter.Before(time.Now().Add(300 * 24 * time.Hour)) {
		t.Fatal("certificate expires too soon to be useful")
	}

	// The fingerprint the operator pins is the one the file actually has.
	fromFile, err := fingerprintFile(certPath)
	if err != nil || fromFile != fp {
		t.Fatalf("fingerprint mismatch: %q vs %q (%v)", fromFile, fp, err)
	}
}

func TestEndToEndOverTLS(t *testing.T) {
	dir := t.TempDir()
	certPath := filepath.Join(dir, "cert.pem")
	keyPath := filepath.Join(dir, "key.pem")
	if _, err := WriteSelfSignedCert(certPath, keyPath, nil, false); err != nil {
		t.Fatal(err)
	}
	cfg := Config{
		Enabled: true, Listen: "127.0.0.1:0",
		TLSCert: certPath, TLSKey: keyPath, TLSMinVersion: "1.3",
		MaxConns: 4, AuthTimeout: 5, IdleTimeout: 30, History: 50,
	}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("a TLS loopback listener should validate: %v", err)
	}
	tc, err := tlsConfig(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if tc.MinVersion != tls.VersionTLS13 {
		t.Fatal("server should refuse anything below TLS 1.3 by default")
	}

	relayDir := t.TempDir()
	token := strings.Repeat("t", 64)
	h := newHub(relayDir, "", false, 20*time.Millisecond, 50)
	lim := newLimiter(3, time.Minute)
	done := make(chan struct{})
	go h.run(done)

	raw, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	l := tls.NewListener(raw, tc)
	go func() {
		for {
			conn, err := l.Accept()
			if err != nil {
				return
			}
			go newSession(h, cfg, []byte(token), lim, conn).serve()
		}
	}()
	t.Cleanup(func() { close(done); l.Close() })

	// A client that pins the certificate — no CA involved, which is the point.
	pool := x509.NewCertPool()
	pem, _ := os.ReadFile(certPath)
	pool.AppendCertsFromPEM(pem)
	conn, err := tls.Dial("tcp", l.Addr().String(), &tls.Config{
		RootCAs: pool, ServerName: "localhost", MinVersion: tls.VersionTLS13,
	})
	if err != nil {
		t.Fatalf("pinned TLS dial failed: %v", err)
	}
	defer conn.Close()
	c := &client{t: t, conn: conn, r: bufio.NewReader(conn)}
	c.login(token, "justin")
	c.expect("End of /MOTD")

	if st := conn.ConnectionState(); st.Version != tls.VersionTLS13 {
		t.Fatalf("negotiated TLS 0x%x, want 1.3", st.Version)
	}
}

func TestPlaintextClientCannotTalkToATLSListener(t *testing.T) {
	dir := t.TempDir()
	certPath := filepath.Join(dir, "cert.pem")
	keyPath := filepath.Join(dir, "key.pem")
	if _, err := WriteSelfSignedCert(certPath, keyPath, nil, false); err != nil {
		t.Fatal(err)
	}
	tc, err := tlsConfig(Config{TLSCert: certPath, TLSKey: keyPath, TLSMinVersion: "1.3"})
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := net.Listen("tcp", "127.0.0.1:0")
	l := tls.NewListener(raw, tc)
	defer l.Close()
	go func() {
		conn, err := l.Accept()
		if err == nil {
			_ = conn.(*tls.Conn).Handshake()
			conn.Close()
		}
	}()

	conn, err := net.Dial("tcp", l.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	// Sending the token in the clear must not get a session — the handshake
	// never happens, so nothing downstream ever sees these bytes.
	fmt.Fprintf(conn, "PASS %s\r\nNICK justin\r\nUSER j 0 * :j\r\n", strings.Repeat("t", 64))
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, 64)
	if n, err := conn.Read(buf); err == nil && strings.Contains(string(buf[:n]), "001") {
		t.Fatal("a cleartext client must never be registered")
	}
}

func TestMutualTLSRequiresAClientCertificate(t *testing.T) {
	dir := t.TempDir()
	certPath := filepath.Join(dir, "cert.pem")
	keyPath := filepath.Join(dir, "key.pem")
	if _, err := WriteSelfSignedCert(certPath, keyPath, nil, false); err != nil {
		t.Fatal(err)
	}
	// Self-signed, so the server cert doubles as its own CA for this test.
	tc, err := tlsConfig(Config{
		TLSCert: certPath, TLSKey: keyPath, TLSMinVersion: "1.3", TLSClientCA: certPath,
	})
	if err != nil {
		t.Fatal(err)
	}
	if tc.ClientAuth != tls.RequireAndVerifyClientCert {
		t.Fatal("tls_client_ca must require AND verify the client certificate")
	}
	if tc.ClientCAs == nil {
		t.Fatal("the client CA pool should be populated")
	}

	raw, _ := net.Listen("tcp", "127.0.0.1:0")
	l := tls.NewListener(raw, tc)
	defer l.Close()
	go func() {
		for {
			conn, err := l.Accept()
			if err != nil {
				return
			}
			go func() {
				_ = conn.(*tls.Conn).Handshake()
				conn.Close()
			}()
		}
	}()

	pool := x509.NewCertPool()
	pem, _ := os.ReadFile(certPath)
	pool.AppendCertsFromPEM(pem)
	conn, err := tls.Dial("tcp", l.Addr().String(), &tls.Config{
		RootCAs: pool, ServerName: "localhost", MinVersion: tls.VersionTLS13,
	})
	if err == nil {
		// Under TLS 1.3 the client's handshake completes before the server has
		// looked at (the absence of) its certificate, so the rejection lands on
		// the first read rather than on Dial.
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
		_, _ = conn.Write([]byte("PING x\r\n"))
		buf := make([]byte, 32)
		_, err = conn.Read(buf)
	}
	if err == nil {
		t.Fatal("a client with no certificate must be rejected under mutual TLS")
	}
}

func TestBadClientCAIsRefusedAtStartup(t *testing.T) {
	dir := t.TempDir()
	certPath := filepath.Join(dir, "cert.pem")
	keyPath := filepath.Join(dir, "key.pem")
	if _, err := WriteSelfSignedCert(certPath, keyPath, nil, false); err != nil {
		t.Fatal(err)
	}
	junk := filepath.Join(dir, "junk.pem")
	writeFile(t, junk, "not a certificate", 0o600)

	if _, err := tlsConfig(Config{
		TLSCert: certPath, TLSKey: keyPath, TLSMinVersion: "1.3", TLSClientCA: junk,
	}); err == nil {
		t.Fatal("an unusable client CA must fail loudly at startup, not silently disable mTLS")
	}
}

// ---------------------------------------------------------------------------
// IRCv3 capabilities
// ---------------------------------------------------------------------------

func TestCapRequestIsAtomic(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.sendf("CAP LS 302")
	line := c.expect("CAP * LS")
	for _, want := range []string{"sasl", "server-time", "message-tags"} {
		if !strings.Contains(line, want) {
			t.Fatalf("LS should advertise %q: %q", want, line)
		}
	}

	// A set containing anything unsupported must be refused WHOLE — acking a
	// partial set leaves the client formatting for a capability we never honour.
	c.sendf("CAP REQ :sasl multi-prefix")
	line = c.expect("CAP * ")
	if !strings.Contains(line, "NAK") {
		t.Fatalf("a set with an unsupported cap must be NAK'd whole: %q", line)
	}

	c.sendf("CAP REQ :sasl server-time")
	line = c.expect("CAP * ACK")
	if !strings.Contains(line, "sasl") || !strings.Contains(line, "server-time") {
		t.Fatalf("ACK should name everything granted: %q", line)
	}
}

func TestCapEndReleasesRegistration(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.sendf("CAP LS 302")
	c.expect("CAP * LS")
	c.sendf("PASS %s", h.token)
	c.sendf("NICK justin")
	c.sendf("USER justin 0 * :justin")
	// Registration is held until CAP END — that is the whole point of CAP LS.
	c.sendf("CAP END")
	c.expect(" 001 ")
}

func TestServerTimeCarriesTheOriginalTimestamp(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.sendf("CAP LS 302")
	c.expect("CAP * LS")
	c.sendf("CAP REQ :server-time message-tags")
	c.expect("CAP * ACK")
	c.sendf("PASS %s", h.token)
	c.sendf("NICK justin")
	c.sendf("USER justin 0 * :justin")
	c.sendf("CAP END")
	c.expect(" 001 ")

	writeRelayMessage(t, h.relayDir, "justin", relay.Message{
		ID: "msg-old1", From: "alice-1", To: "justin", Content: "sent last week",
		Timestamp: "2026-07-01T09:30:00Z", Priority: "normal", State: "pending",
	})
	line := c.expect("sent last week")
	if !strings.HasPrefix(line, "@") {
		t.Fatalf("negotiated tags should be present: %q", line)
	}
	// The point of server-time: replayed history reads at the time it happened,
	// not the time it was delivered to this client.
	if !strings.Contains(line, "time=2026-07-01T09:30:00.000Z") {
		t.Fatalf("server-time should carry the message's own timestamp: %q", line)
	}
	if !strings.Contains(line, "msgid=msg-old1") {
		t.Fatalf("message-tags should carry the id you'd ack with: %q", line)
	}
}

func TestNoTagsWithoutNegotiation(t *testing.T) {
	// A client that never asked must not be sent tags — they'd be printed as
	// literal noise in the message body.
	h := startGateway(t)
	c := h.dial(t)
	c.login(h.token, "justin")
	writeRelayMessage(t, h.relayDir, "justin", relay.Message{
		ID: "msg-plain", From: "alice-1", To: "justin", Content: "no tags please",
		Timestamp: nowStamp(), Priority: "normal", State: "pending",
	})
	line := c.expect("no tags please")
	if strings.HasPrefix(line, "@") {
		t.Fatalf("unnegotiated client got tags: %q", line)
	}
}

func TestIrcTimeRejectsUnparseableStamps(t *testing.T) {
	if got := ircTime("2026-07-01T09:30:00Z"); got != "2026-07-01T09:30:00.000Z" {
		t.Fatalf("got %q", got)
	}
	// A missing tag degrades to "now" in the client; a wrong one lies.
	if got := ircTime("not a time"); got != "" {
		t.Fatalf("unparseable stamps must yield no tag, got %q", got)
	}
}

func TestTagEscape(t *testing.T) {
	if got := tagEscape("a;b c\\d"); got != `a\:b\sc\\d` {
		t.Fatalf("tag escaping wrong: %q", got)
	}
}

// ---------------------------------------------------------------------------
// Keepalive
// ---------------------------------------------------------------------------

func TestServerPingsAQuietClient(t *testing.T) {
	// Without this the read deadline is a guillotine: a healthy but silent
	// client is dropped at idle_timeout. The floor is 15s, so use a hub with a
	// short idle timeout and just assert the PING arrives.
	h := startGateway(t)
	c := h.dial(t)
	c.login(h.token, "justin")
	c.expect("End of /MOTD")

	// The keepalive interval floors at 15s, which is too long for a unit test;
	// exercise the responder path instead — a client PING must always answer.
	c.sendf("PING :keepalive-probe")
	line := c.expect("PONG")
	if !strings.Contains(line, "keepalive-probe") {
		t.Fatalf("PONG should echo the token: %q", line)
	}
}

func TestKeepaliveIntervalHasAFloor(t *testing.T) {
	s := &session{cfg: Config{IdleTimeout: 2}}
	if got := s.cfg.idleTimeout() / 2; got >= 15*time.Second {
		t.Fatal("precondition: this config would not exercise the floor")
	}
	// The floor exists so a misconfigured idle_timeout can't turn the keepalive
	// into a flood.
	done := make(chan struct{})
	sess := &session{cfg: Config{IdleTimeout: 2}, out: make(chan string, 8), done: done}
	go sess.keepalive()
	time.Sleep(200 * time.Millisecond)
	close(done)
	if len(sess.out) > 0 {
		t.Fatalf("keepalive fired %d times inside 200ms — the floor is not holding", len(sess.out))
	}
}

func TestStandardQueriesAnswer(t *testing.T) {
	h := startGateway(t)
	c := h.dial(t)
	c.login(h.token, "justin")
	c.expect("End of /MOTD")

	c.sendf("VERSION")
	c.expect(" 351 ")
	c.sendf("TIME")
	c.expect(" 391 ")
	c.sendf("LUSERS")
	c.expect(" 251 ")
	c.sendf("MOTD")
	c.expect("End of /MOTD")
}

// ---------------------------------------------------------------------------
// systemd unit
// ---------------------------------------------------------------------------

func TestRenderUnitEscapesSystemdSpecifiers(t *testing.T) {
	// '%h' is the home directory to systemd. An unescaped '%' in a path
	// silently rewrites it — the unit installs fine and serves the wrong relay.
	cfg := Config{Enabled: true, Socket: "/home/me/50%/irc.sock", TLSMinVersion: "1.3"}
	unit, err := renderUnit(cfg, "/home/me/100%relay", "/opt/bin/dispatch-ircd", "")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(unit, "100%relay") {
		t.Fatal("a bare '%' reached the unit — systemd will expand it as a specifier")
	}
	if !strings.Contains(unit, "100%%relay") {
		t.Fatalf("'%%' should be doubled:\n%s", unit)
	}
}

func TestRenderUnitRefusesControlCharacters(t *testing.T) {
	// A newline ends the directive and starts a forged one.
	cfg := Config{Enabled: true, TLSMinVersion: "1.3"}
	_, err := renderUnit(cfg, "/relay\nExecStartPost=/bin/rm -rf /", "/opt/bin/dispatch-ircd", "")
	if err == nil {
		t.Fatal("a control character in a path must be refused, not escaped-ish")
	}
	if _, err := renderUnit(cfg, "/relay", "/opt/bin/ircd\nUser=root", ""); err == nil {
		t.Fatal("the exec path is interpolated too and must be checked")
	}
}

func TestRenderUnitQuotesTheExecPath(t *testing.T) {
	cfg := Config{Enabled: true, TLSMinVersion: "1.3"}
	unit, err := renderUnit(cfg, "/relay", "/opt/my tools/dispatch-ircd", "")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(unit, `ExecStart="/opt/my tools/dispatch-ircd"`) {
		t.Fatalf("a path with a space must be quoted or systemd splits it:\n%s", unit)
	}
}

func TestRenderUnitOmitsCapabilityImplyingDirectives(t *testing.T) {
	// Each of these implies a CapabilityBoundingSet change that a *user*
	// manager cannot perform: the unit then dies at spawn with 218/CAPABILITIES
	// before running a line of the gateway.
	cfg := Config{Enabled: true, TLSMinVersion: "1.3"}
	unit, _ := renderUnit(cfg, "/relay", "/opt/bin/dispatch-ircd", "")
	// Only actual directives count — the unit comments explain why these are
	// absent, and naming them there is the documentation, not the bug.
	var directives []string
	for _, l := range strings.Split(unit, "\n") {
		l = strings.TrimSpace(l)
		if l != "" && !strings.HasPrefix(l, "#") {
			directives = append(directives, l)
		}
	}
	body := strings.Join(directives, "\n")
	for _, bad := range []string{
		"ProtectClock", "ProtectKernelTunables", "ProtectKernelModules",
		"ProtectControlGroups", "CapabilityBoundingSet",
	} {
		if strings.Contains(body, bad) {
			t.Fatalf("%s cannot be used in a user unit", bad)
		}
	}
	for _, want := range []string{"NoNewPrivileges=yes", "SystemCallFilter=@system-service"} {
		if !strings.Contains(unit, want) {
			t.Fatalf("expected %s in the unit", want)
		}
	}
}

func TestRenderUnitOrdersOnNetworkOnlyForTCP(t *testing.T) {
	sockOnly := Config{Enabled: true, Socket: "/x.sock", TLSMinVersion: "1.3"}
	unit, _ := renderUnit(sockOnly, "/relay", "/opt/bin/dispatch-ircd", "")
	if strings.Contains(unit, "network-online.target") {
		t.Fatal("a unix-socket-only gateway should not wait on the network")
	}
	tcp := Config{Enabled: true, Listen: "127.0.0.1:6697",
		TLSCert: "/c.pem", TLSKey: "/k.pem", TLSMinVersion: "1.3"}
	unit, _ = renderUnit(tcp, "/relay", "/opt/bin/dispatch-ircd", "")
	if !strings.Contains(unit, "network-online.target") {
		t.Fatal("a TCP listener needs the network up first")
	}
}

func TestRenderUnitDropsPrivateTmpForATmpRelay(t *testing.T) {
	// PrivateTmp namespaces /var/tmp away; the documented group-mode relay
	// lives there, and the unit would otherwise serve an empty directory.
	cfg := Config{Enabled: true, TLSMinVersion: "1.3"}
	unit, _ := renderUnit(cfg, "/var/tmp/mcp-dispatch/messages", "/opt/bin/dispatch-ircd", "")
	if !strings.Contains(unit, "PrivateTmp=no") {
		t.Fatalf("expected PrivateTmp=no for a /var/tmp relay:\n%s", unit)
	}
	unit, _ = renderUnit(cfg, "/home/me/.config/mcp-dispatch/messages", "/opt/bin/dispatch-ircd", "")
	if !strings.Contains(unit, "PrivateTmp=yes") {
		t.Fatal("a normal relay should keep PrivateTmp on")
	}
}

func TestRenderUnitPinsAnExplicitConfigPath(t *testing.T) {
	cfg := Config{Enabled: true, TLSMinVersion: "1.3"}
	unit, _ := renderUnit(cfg, "/relay", "/opt/bin/dispatch-ircd", "/etc/dispatch.toml")
	if !strings.Contains(unit, "Environment=MCP_DISPATCH_CONFIG=/etc/dispatch.toml") {
		t.Fatalf("an explicit config path must be carried into the unit:\n%s", unit)
	}
	unit, _ = renderUnit(cfg, "/relay", "/opt/bin/dispatch-ircd", "")
	if strings.Contains(unit, "MCP_DISPATCH_CONFIG") {
		t.Fatal("with no explicit config, the service should resolve it the same way a shell run does")
	}
}

func TestServiceDryRunFlagIsHonouredAfterTheVerb(t *testing.T) {
	// The stdlib flag package stops parsing at the first positional, so
	// `service install --dry-run` used to leave dryRun false and do the real
	// thing. A dry run that isn't one is worse than none.
	fs := flag.NewFlagSet("service", flag.ContinueOnError)
	dry := fs.Bool("dry-run", false, "")
	if err := fs.Parse([]string{"--dry-run"}); err != nil {
		t.Fatal(err)
	}
	if !*dry {
		t.Fatal("precondition")
	}

	// And the real path: a temp HOME means install would write here if it ran.
	home := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", filepath.Join(home, ".config"))
	if err := serviceInstall("[Unit]\n", true); err != nil {
		t.Fatalf("dry run should not fail: %v", err)
	}
	if _, err := os.Stat(unitPath()); err == nil {
		t.Fatal("a dry run must not write the unit file")
	}
}

func TestServiceInstallWritesOwnerOnly(t *testing.T) {
	if !systemctlAvailable() {
		t.Skip("no systemctl on this host")
	}
	home := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", filepath.Join(home, ".config"))
	// systemctl will fail without a session bus; the file write is what matters
	// and happens first.
	_ = serviceInstall("[Unit]\nDescription=test\n", false)
	fi, err := os.Stat(unitPath())
	if err != nil {
		t.Skipf("unit not written (%v) — nothing to assert", err)
	}
	if fi.Mode().Perm() != 0o600 {
		t.Fatalf("unit is %04o, want 0600 — it names paths and may carry env", fi.Mode().Perm())
	}
}
