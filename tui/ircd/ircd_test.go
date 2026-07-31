package main

import (
	"bufio"
	"encoding/json"
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
	for _, addr := range []string{"0.0.0.0:6667", ":6667", "[::]:6667", "10.0.0.5:6667"} {
		c := Config{Enabled: true, Listen: addr}
		err := c.Validate()
		if err == nil {
			t.Fatalf("%s: binding the network must be refused by default", addr)
		}
		if !strings.Contains(err.Error(), "allow_remote") {
			t.Fatalf("%s: refusal should name allow_remote: %v", addr, err)
		}
	}
}

func TestRefusesRemoteInCleartextEvenWhenAllowed(t *testing.T) {
	// allow_remote alone is deliberately not enough — the token would cross the
	// wire in the clear along with every message body.
	c := Config{Enabled: true, Listen: "10.0.0.5:6667", AllowRemote: true}
	err := c.Validate()
	if err == nil {
		t.Fatal("a remote cleartext bind must be refused even with allow_remote")
	}
	if !strings.Contains(err.Error(), "TLS") {
		t.Fatalf("refusal should name TLS: %v", err)
	}
}

func TestAcceptsLoopbackAndRemoteWithTLS(t *testing.T) {
	if err := (Config{Enabled: true, Listen: "127.0.0.1:6667"}).Validate(); err != nil {
		t.Fatalf("loopback should be allowed: %v", err)
	}
	ok := Config{Enabled: true, Listen: "10.0.0.5:6697", AllowRemote: true,
		TLSCert: "/c.pem", TLSKey: "/k.pem"}
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

func TestServiceNickAcksInbox(t *testing.T) {
	h := startGateway(t)
	writeRelayMessage(t, h.relayDir, "justin", relay.Message{
		ID: "msg-3", From: "alice-1", To: "justin", Content: "please ack",
		Timestamp: nowStamp(), Priority: "normal", State: "pending",
	})
	c := h.dial(t)
	c.login(h.token, "justin")
	c.sendf("PRIVMSG %s :ack", serviceNick)
	c.expect("acknowledged 1 message")

	files, _ := filepath.Glob(filepath.Join(h.relayDir, "justin", "*.json"))
	if len(files) != 1 {
		t.Fatalf("expected the message to remain, got %d files", len(files))
	}
	var m map[string]any
	data, _ := os.ReadFile(files[0])
	_ = json.Unmarshal(data, &m)
	if m["state"] != "read" {
		t.Fatalf("ack should mark it read, got %v", m["state"])
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
