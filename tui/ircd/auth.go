// auth.go — who is allowed to talk to the gateway at all.
//
// Three independent gates, in the order a connection meets them:
//
//  1. Kernel peer credentials (unix socket only). The relay is per-user, so a
//     connection from another uid is refused before a byte is read — no token
//     leak, no config mistake, and no borrowed socket path gets another local
//     account into your agents' conversations.
//  2. A shared token, over PASS or SASL PLAIN, compared in constant time.
//     Mandatory on every transport including the unix socket: peer credentials
//     stop other users, the token stops other *processes* running as you.
//  3. Failure backoff. Wrong tokens are counted per remote and answered with a
//     temporary ban, so a loopback port is not an offline guessing oracle.
package main

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net"
	"os"
	"sync"
	"syscall"
	"time"
)

func randomHex(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// peerCheck verifies the kernel-reported uid of a unix-socket peer against our
// own. SO_PEERCRED is recorded at connect() time by the kernel and cannot be
// forged by the client, which makes this the strongest check we have — it holds
// even if the token has leaked.
//
// TCP connections have no such credential; they return nil here and rely on the
// token plus the bind-address refusals in Config.Validate.
func peerCheck(conn net.Conn) error {
	uc, ok := conn.(*net.UnixConn)
	if !ok {
		return nil
	}
	raw, err := uc.SyscallConn()
	if err != nil {
		return fmt.Errorf("peer credentials unavailable: %w", err)
	}
	var cred *syscall.Ucred
	var credErr error
	if err := raw.Control(func(fd uintptr) {
		cred, credErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	}); err != nil {
		return fmt.Errorf("peer credentials unavailable: %w", err)
	}
	if credErr != nil {
		return fmt.Errorf("peer credentials unavailable: %w", credErr)
	}
	// Fail closed: if we can't read a credential we don't serve the connection.
	if cred == nil {
		return fmt.Errorf("peer credentials unavailable")
	}
	if uid := uint32(os.Getuid()); cred.Uid != uid { //nolint:gosec // uid fits
		return fmt.Errorf("peer uid %d is not %d", cred.Uid, uid)
	}
	return nil
}

// limiter tracks failed authentications per remote and bans a source that keeps
// guessing. Keyed by IP for TCP and by a single constant for the unix socket
// (where peerCheck has already established it is us, so the counter is really a
// brake on a runaway local client).
type limiter struct {
	mu       sync.Mutex
	fails    map[string]int
	banUntil map[string]time.Time
	max      int
	ban      time.Duration
	now      func() time.Time // injectable for tests
}

func newLimiter(max int, ban time.Duration) *limiter {
	return &limiter{
		fails:    map[string]int{},
		banUntil: map[string]time.Time{},
		max:      max,
		ban:      ban,
		now:      time.Now,
	}
}

// remoteKey reduces an address to the unit we rate-limit. Port is dropped so
// reconnecting from a fresh ephemeral port doesn't reset the counter.
func remoteKey(addr net.Addr) string {
	if addr == nil {
		return "unknown"
	}
	if addr.Network() == "unix" {
		return "unix"
	}
	if host, _, err := net.SplitHostPort(addr.String()); err == nil {
		return host
	}
	return addr.String()
}

// banned reports whether a source is currently locked out, and for how long.
func (l *limiter) banned(key string) (bool, time.Duration) {
	l.mu.Lock()
	defer l.mu.Unlock()
	until, ok := l.banUntil[key]
	if !ok {
		return false, 0
	}
	if rem := until.Sub(l.now()); rem > 0 {
		return true, rem
	}
	delete(l.banUntil, key)
	delete(l.fails, key)
	return false, 0
}

// fail records a bad token. At the threshold the source is banned for the
// configured window; the counter keeps climbing while banned, so a client that
// reconnects and keeps guessing extends its own lockout.
func (l *limiter) fail(key string) (bannedNow bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.fails[key]++
	if l.fails[key] >= l.max {
		l.banUntil[key] = l.now().Add(l.ban)
		return true
	}
	return false
}

// success clears the record for a source that authenticated.
func (l *limiter) success(key string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.fails, key)
	delete(l.banUntil, key)
}
