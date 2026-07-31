// listen.go — binding, and the single-instance lock.
//
// Config.Validate has already refused the configurations we won't serve; this
// file is about creating the socket without opening a window between "exists"
// and "has safe permissions". The unix socket is bound inside a 0077 umask and
// then chmod'd explicitly, because the umask applied to a socket inode is not
// portable enough to rely on alone.
package main

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"log"
	"net"
	"os"
	"path/filepath"
	"syscall"
)

// tlsConfig builds the server's TLS settings: the key pair, the version floor,
// and — when a client CA is configured — mutual TLS.
//
// Mutual TLS is an *additional* gate here, never a replacement for the token.
// Letting a certificate stand in for the password (IRC's SASL EXTERNAL) is the
// conventional move, and it is a second authentication path with its own bugs;
// requiring both costs a client nothing it isn't already configured for.
func tlsConfig(c Config) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(c.TLSCert, c.TLSKey)
	if err != nil {
		return nil, fmt.Errorf("tls: %w", err)
	}
	minVer, err := tlsMinVersion(c.TLSMinVersion)
	if err != nil {
		return nil, err
	}
	tc := &tls.Config{
		Certificates: []tls.Certificate{cert},
		MinVersion:   minVer,
	}
	if c.TLSClientCA != "" {
		pem, err := os.ReadFile(c.TLSClientCA) //nolint:gosec // operator-configured path
		if err != nil {
			return nil, fmt.Errorf("tls_client_ca: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("tls_client_ca: %s contains no usable certificate", c.TLSClientCA)
		}
		tc.ClientCAs = pool
		tc.ClientAuth = tls.RequireAndVerifyClientCert
	}
	return tc, nil
}

// listeners builds every configured transport. The caller closes them.
func listeners(c Config) ([]net.Listener, error) {
	var out []net.Listener
	closeAll := func() {
		for _, l := range out {
			_ = l.Close()
		}
	}

	if c.Socket != "" {
		l, err := unixListener(c.Socket)
		if err != nil {
			closeAll()
			return nil, err
		}
		out = append(out, l)
		log.Printf("irc: listening on unix socket %s (0600, peer-uid checked)", c.Socket)
	}

	if c.Listen != "" {
		l, err := net.Listen("tcp", c.Listen)
		if err != nil {
			closeAll()
			return nil, err
		}
		scope := "loopback"
		if !isLoopback(c.Listen) {
			scope = "REMOTE"
		}
		tc, err := tlsConfig(c)
		if err != nil {
			_ = l.Close()
			closeAll()
			return nil, err
		}
		l = tls.NewListener(l, tc)
		mutual := ""
		if c.TLSClientCA != "" {
			mutual = ", mutual (client certs required)"
		}
		log.Printf("irc: listening on tcp %s (%s, TLS %s+%s)", c.Listen, scope,
			c.TLSMinVersion, mutual)
		if fp, err := fingerprintFile(c.TLSCert); err == nil {
			// Printed every start, not just on generation: a self-signed cert is
			// trusted by pinning, and a pin you can't re-check is not a pin.
			log.Printf("irc: certificate SHA-256 %s", fp)
		}
		out = append(out, l)
	}

	if len(out) == 0 {
		return nil, fmt.Errorf("no transports configured")
	}
	return out, nil
}

// unixListener creates the socket owner-only. A stale socket from a crashed
// process is replaced, but only after the lock has established that no live
// gateway owns it — see lockInstance.
func unixListener(path string) (net.Listener, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	if fi, err := os.Lstat(path); err == nil {
		if fi.Mode()&os.ModeSocket == 0 {
			return nil, fmt.Errorf("%s exists and is not a socket — refusing to replace it", path)
		}
		_ = os.Remove(path)
	}
	old := syscall.Umask(0o177) // rw for owner only
	l, err := net.Listen("unix", path)
	syscall.Umask(old)
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(path, 0o600); err != nil {
		_ = l.Close()
		return nil, fmt.Errorf("could not restrict %s to its owner: %w", path, err)
	}
	return l, nil
}

// lockInstance takes an exclusive flock so two gateways can't bind the same
// relay — the same mechanism the relay uses for agent presence, so a crashed
// gateway's lock is released by the kernel rather than needing cleanup.
func lockInstance(relayDir string) (*os.File, error) {
	path := filepath.Join(relayDir, ".ircd.lock")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600) //nolint:gosec // relay-owned path
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		return nil, fmt.Errorf("another dispatch-ircd is already serving %s", relayDir)
	}
	return f, nil
}
