// config.go — the [irc] section, and the refusals that keep it locked down.
//
// The relay is owner-only local IPC (0700/0600, no listener). A gateway is the
// first thing in mcp-dispatch that accepts a connection, so the config layer
// treats every widening of exposure as something you must ask for *explicitly*
// and in writing. Nothing here has a convenient default: the gateway is off
// until enabled in the config file, speaks over a 0600 unix socket unless told
// otherwise, and refuses outright to bind a public address in the clear.
package main

import (
	"crypto/sha256"
	"crypto/subtle"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/BurntSushi/toml"
	"github.com/justinstimatze/mcp-dispatch/tui/relay"
)

// minTokenLen is the shortest shared secret we will accept. `--init-token`
// writes 32 random bytes as 64 hex chars; this floor exists so a hand-edited
// token file can't quietly reduce the gateway to a guessable password.
const minTokenLen = 32

// Config is the [irc] table. Zero values are the safe end of every axis: not
// enabled, no TCP, no remote, no TLS.
type Config struct {
	Enabled bool `toml:"enabled"`

	// Transports. Socket is the default and the only one that keeps the
	// gateway off the network entirely; Listen opts into TCP.
	Socket string `toml:"socket"`
	Listen string `toml:"listen"`

	// AllowRemote is the second key required to bind a non-loopback address.
	// TLS is the third — a non-loopback bind in cleartext is refused even
	// with this set, because the relay's traffic is a team's codebase talk.
	AllowRemote bool   `toml:"allow_remote"`
	TLSCert     string `toml:"tls_cert"`
	TLSKey      string `toml:"tls_key"`

	TokenFile string `toml:"token_file"`
	Nick      string `toml:"nick"`

	MaxConns        int     `toml:"max_conns"`
	AuthTimeout     int     `toml:"auth_timeout"`
	IdleTimeout     int     `toml:"idle_timeout"`
	MaxAuthFailures int     `toml:"max_auth_failures"`
	BanSeconds      int     `toml:"ban_seconds"`
	Interval        float64 `toml:"interval"`
	History         int     `toml:"history"`
	ReadGit         bool    `toml:"read_git"`
}

type fileConfig struct {
	IRC Config `toml:"irc"`
}

// DefaultSocket is the gateway's default transport: a unix socket next to the
// config, created 0600. It is unreachable from the network by construction —
// the "no network listener" property in the README survives the default build.
func DefaultSocket() string {
	return relay.ExpandUser("~/.config/mcp-dispatch/irc.sock")
}

func defaultTokenFile() string {
	return relay.ExpandUser("~/.config/mcp-dispatch/irc-token")
}

// LoadConfig reads the [irc] table from the same TOML the rest of the tools use
// and fills in defaults. It never enables anything: Enabled comes from the file
// alone.
func LoadConfig() Config {
	path := os.Getenv("MCP_DISPATCH_CONFIG")
	if path == "" {
		path = relay.ExpandUser("~/.config/mcp-dispatch/config.toml")
	}
	var fc fileConfig
	_, _ = toml.DecodeFile(path, &fc) // absent/unreadable → all-zero, i.e. disabled
	c := fc.IRC

	if c.Socket == "" && c.Listen == "" {
		c.Socket = DefaultSocket()
	}
	c.Socket = relay.ExpandUser(c.Socket)
	if c.TokenFile == "" {
		c.TokenFile = defaultTokenFile()
	}
	c.TokenFile = relay.ExpandUser(c.TokenFile)
	c.TLSCert = relay.ExpandUser(c.TLSCert)
	c.TLSKey = relay.ExpandUser(c.TLSKey)

	if c.MaxConns <= 0 {
		c.MaxConns = 8
	}
	if c.AuthTimeout <= 0 {
		c.AuthTimeout = 10
	}
	if c.IdleTimeout <= 0 {
		c.IdleTimeout = 300
	}
	if c.MaxAuthFailures <= 0 {
		c.MaxAuthFailures = 5
	}
	if c.BanSeconds <= 0 {
		c.BanSeconds = 300
	}
	if c.Interval <= 0 {
		c.Interval = 1.0
	}
	if c.History <= 0 {
		c.History = 50
	}
	return c
}

func (c Config) authTimeout() time.Duration { return time.Duration(c.AuthTimeout) * time.Second }
func (c Config) idleTimeout() time.Duration { return time.Duration(c.IdleTimeout) * time.Second }
func (c Config) banDuration() time.Duration { return time.Duration(c.BanSeconds) * time.Second }
func (c Config) pollInterval() time.Duration {
	d := time.Duration(c.Interval * float64(time.Second))
	if d < 100*time.Millisecond {
		d = 100 * time.Millisecond
	}
	return d
}

// isLoopback reports whether a "host:port" binds only to the local machine.
// An empty or wildcard host ("", "0.0.0.0", "::", "*") is emphatically NOT
// loopback — that is the case most likely to be typed by accident, so it must
// fail the check rather than fall through it.
func isLoopback(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return false
	}
	switch host {
	case "", "*", "0.0.0.0", "::", "[::]":
		return false
	case "localhost":
		return true
	}
	ip := net.ParseIP(strings.Trim(host, "[]"))
	return ip != nil && ip.IsLoopback()
}

// Validate is the gate every start passes through. Each refusal names the
// specific key that would have to change, so nobody has to guess their way to a
// weaker configuration by trial and error — and so nobody arrives at one by
// accident.
func (c Config) Validate() error {
	if !c.Enabled {
		return fmt.Errorf("the IRC gateway is disabled\n" +
			"→ it exposes the relay to anything that can reach its socket, so it stays off\n" +
			"  until you turn it on in the config file:\n\n" +
			"    [irc]\n    enabled = true\n\n" +
			"  There is deliberately no flag for this — see docs/irc-gateway.md.")
	}
	if c.Socket == "" && c.Listen == "" {
		return fmt.Errorf("[irc] has neither socket nor listen set — nothing to bind")
	}
	if c.Listen != "" {
		if _, _, err := net.SplitHostPort(c.Listen); err != nil {
			return fmt.Errorf("[irc] listen = %q is not host:port: %w", c.Listen, err)
		}
		hasTLS := c.TLSCert != "" && c.TLSKey != ""
		if !isLoopback(c.Listen) {
			if !c.AllowRemote {
				return fmt.Errorf("[irc] listen = %q is not a loopback address\n"+
					"→ refusing to serve the relay to the network. Bind 127.0.0.1 instead,\n"+
					"  or set [irc] allow_remote = true AND configure tls_cert/tls_key.", c.Listen)
			}
			if !hasTLS {
				return fmt.Errorf("[irc] listen = %q is remote and has no TLS\n"+
					"→ refusing: message bodies are cleartext on the wire and the auth token\n"+
					"  would be too. Set tls_cert and tls_key. allow_remote alone is not enough.", c.Listen)
			}
		}
		if (c.TLSCert == "") != (c.TLSKey == "") {
			return fmt.Errorf("[irc] tls_cert and tls_key must be set together")
		}
	}
	return nil
}

// ReadToken loads the shared secret and refuses anything that would make it a
// weak one: missing, group/world-readable, too short, or not a regular file.
// The permission check matters most — the token is equivalent to read/write
// access to every agent conversation on the host.
func ReadToken(path string) ([]byte, error) {
	fi, err := os.Lstat(path)
	if err != nil {
		return nil, fmt.Errorf("no auth token at %s\n"+
			"→ generate one:  dispatch-ircd --init-token", path)
	}
	if !fi.Mode().IsRegular() {
		return nil, fmt.Errorf("token %s is not a regular file", path)
	}
	if fi.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("token %s is mode %04o — readable beyond its owner\n"+
			"→ chmod 600 %s", path, fi.Mode().Perm(), path)
	}
	raw, err := os.ReadFile(path) //nolint:gosec // operator-configured path
	if err != nil {
		return nil, err
	}
	tok := strings.TrimSpace(string(raw))
	if len(tok) < minTokenLen {
		return nil, fmt.Errorf("token %s is %d chars — minimum is %d\n"+
			"→ regenerate:  dispatch-ircd --init-token --force", path, len(tok), minTokenLen)
	}
	return []byte(tok), nil
}

// tokenMatch compares in constant time, over digests so the comparison can't
// leak the token's length either.
func tokenMatch(want, got []byte) bool {
	w := sha256.Sum256(want)
	g := sha256.Sum256(got)
	return subtle.ConstantTimeCompare(w[:], g[:]) == 1
}

// WriteToken generates a fresh 64-hex-char secret, written 0600 into a
// 0700 parent. Refuses to clobber an existing token without --force so a
// re-run can't silently lock out every client that already has one.
func WriteToken(path string, force bool) (string, error) {
	if _, err := os.Stat(path); err == nil && !force {
		return "", fmt.Errorf("%s already exists (use --force to replace it)", path)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", err
	}
	tok, err := randomHex(32)
	if err != nil {
		return "", err
	}
	// O_EXCL under --force too: unlink first, then create, so we never write
	// through a symlink someone dropped in place.
	_ = os.Remove(path)
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return "", err
	}
	defer f.Close()
	if _, err := f.WriteString(tok + "\n"); err != nil {
		return "", err
	}
	return tok, nil
}
