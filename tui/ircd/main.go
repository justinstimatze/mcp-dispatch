// dispatch-ircd — an IRC gateway to the mcp-dispatch relay.
//
// Point any IRC client at it and the relay reads like a small network: agents
// are nicks, relay channels are channels, '&dispatch' is the firehose. That is
// a distribution move, not a UX one — it buys every desktop and mobile IRC
// client, and a bouncer in front buys scrollback and push, without this repo
// shipping a single line of UI for them.
//
// It is off unless the config says otherwise, speaks over a 0600 unix socket by
// default, checks the peer's uid with the kernel, requires a token on every
// transport, requires TLS on every TCP listener (loopback included), and will
// not bind a public address unless asked twice. See docs/irc-gateway.md.
//
//	dispatch-ircd --init-token   # generate the shared secret, once
//	dispatch-ircd --init-tls     # ...and a certificate, if you want TCP
//	dispatch-ircd --check        # validate config and exit
//	dispatch-ircd                # run (needs [irc] enabled = true)
//	dispatch-ircd service install   # ...or run it under systemd
package main

import (
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"runtime/debug"
	"strings"
	"sync"
	"syscall"

	"github.com/justinstimatze/mcp-dispatch/tui/relay"
)

var version = "dev"

func buildVersion() string {
	if version != "dev" {
		return version
	}
	if bi, ok := debug.ReadBuildInfo(); ok {
		for _, s := range bi.Settings {
			if s.Key == "vcs.revision" && len(s.Value) >= 7 {
				return s.Value[:7]
			}
		}
	}
	return version
}

func main() {
	var (
		dir         = flag.String("dir", "", "relay dir (default: config / $MCP_DISPATCH_DIR)")
		gitRepo     = flag.String("git-repo", "", "git-bus clone dir (default: config [git].repo_dir)")
		noGit       = flag.Bool("no-git", false, "local inboxes only — don't read the cross-host git bus")
		initToken   = flag.Bool("init-token", false, "generate the auth token and exit")
		initTLS     = flag.Bool("init-tls", false, "generate a self-signed TLS certificate and exit")
		tlsHosts    = flag.String("tls-hosts", "", "with --init-tls: extra hostnames/IPs for the certificate (comma separated)")
		force       = flag.Bool("force", false, "with --init-token/--init-tls, replace what exists")
		check       = flag.Bool("check", false, "validate configuration and exit")
		dryRun      = flag.Bool("dry-run", false, "with `service`, print what would be done")
		showVersion = flag.Bool("version", false, "print version and exit")
	)
	flag.Parse()

	log.SetFlags(log.LstdFlags)
	log.SetPrefix("")

	if *showVersion {
		fmt.Println("dispatch-ircd", buildVersion())
		return
	}

	// Anything this process creates is owner-only.
	syscall.Umask(0o077)

	cfg := LoadConfig()

	if *initToken {
		tok, err := WriteToken(cfg.TokenFile, *force)
		if err != nil {
			fatal("%v", err)
		}
		fmt.Printf("wrote %s (0600)\n\n", cfg.TokenFile)
		fmt.Printf("  %s\n\n", tok)
		fmt.Println("Give this to your IRC client as the server password, or as the SASL")
		fmt.Println("PLAIN password. Anyone holding it can read and send every message on")
		fmt.Println("this relay — treat it like an SSH key, and don't put it in a shared")
		fmt.Println("config file or a bouncer you don't control.")
		return
	}

	if *initTLS {
		certPath, keyPath := cfg.TLSCert, cfg.TLSKey
		if certPath == "" {
			certPath = relay.ExpandUser("~/.config/mcp-dispatch/irc-cert.pem")
		}
		if keyPath == "" {
			keyPath = relay.ExpandUser("~/.config/mcp-dispatch/irc-key.pem")
		}
		fp, err := WriteSelfSignedCert(certPath, keyPath, strings.Split(*tlsHosts, ","), *force)
		if err != nil {
			fatal("%v", err)
		}
		fmt.Printf("wrote %s\n      %s (0600)\n\n", certPath, keyPath)
		fmt.Printf("  SHA-256  %s\n\n", fp)
		fmt.Println("Point the config at them, then pin that fingerprint in your client —")
		fmt.Println("a self-signed certificate is trusted by pinning, not by a CA:")
		fmt.Printf("\n    [irc]\n    listen = \"127.0.0.1:6697\"\n    tls_cert = \"%s\"\n    tls_key = \"%s\"\n",
			certPath, keyPath)
		return
	}

	if flag.Arg(0) == "service" {
		runService(flag.Args()[1:], cfg, *dir, *dryRun)
		return
	}

	if err := cfg.Validate(); err != nil {
		fatal("%v", err)
	}
	token, err := ReadToken(cfg.TokenFile)
	if err != nil {
		fatal("%v", err)
	}

	relayCfg := relay.LoadConfig()
	relayDir := relay.RelayDir(relayCfg)
	if *dir != "" {
		relayDir = relay.ExpandUser(*dir)
	}
	if fi, err := os.Stat(relayDir); err != nil || !fi.IsDir() {
		fatal("no relay at %s\n→ no dispatch-enabled session has started, or pass --dir.", relayDir)
	}

	repo := relay.GitRepoDir(relayCfg)
	if *gitRepo != "" {
		repo = relay.ExpandUser(*gitRepo)
	}
	readGit := !*noGit && repo != "" && cfg.ReadGitEnabled()

	if *check {
		fmt.Println("configuration OK")
		fmt.Printf("  relay        %s\n", relayDir)
		fmt.Printf("  git bus      %s\n", orNone(repoLabel(repo, readGit)))
		fmt.Printf("  unix socket  %s\n", orNone(cfg.Socket))
		fmt.Printf("  tcp listen   %s\n", orNone(tcpLabel(cfg)))
		if cfg.TLSCert != "" {
			fp, err := fingerprintFile(cfg.TLSCert)
			if err != nil {
				fatal("tls_cert: %v", err)
			}
			fmt.Printf("  tls cert     %s\n", cfg.TLSCert)
			fmt.Printf("  fingerprint  %s\n", fp)
		}
		fmt.Printf("  token        %s (%d bytes)\n", cfg.TokenFile, len(token))
		fmt.Printf("  max conns    %d\n", cfg.MaxConns)
		return
	}

	lock, err := lockInstance(relayDir)
	if err != nil {
		fatal("%v", err)
	}
	defer lock.Close()

	ls, err := listeners(cfg)
	if err != nil {
		fatal("%v", err)
	}

	h := newHub(relayDir, repo, readGit, cfg.pollInterval(), cfg.History)
	lim := newLimiter(cfg.MaxAuthFailures, cfg.banDuration())

	done := make(chan struct{})
	var closeOnce sync.Once
	shutdown := func() {
		closeOnce.Do(func() {
			close(done)
			for _, l := range ls {
				_ = l.Close()
			}
			if cfg.Socket != "" {
				_ = os.Remove(cfg.Socket)
			}
		})
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sig
		log.Println("irc: shutting down")
		shutdown()
	}()

	go h.run(done)

	log.Printf("irc: dispatch-ircd %s serving %s", buildVersion(), relayDir)

	var wg sync.WaitGroup
	sem := make(chan struct{}, cfg.MaxConns)
	for _, l := range ls {
		wg.Add(1)
		go func(l net.Listener) {
			defer wg.Done()
			for {
				conn, err := l.Accept()
				if err != nil {
					select {
					case <-done:
						return
					default:
						log.Printf("irc: accept: %v", err)
						return
					}
				}
				select {
				case sem <- struct{}{}:
				default:
					// At the connection cap. Say so and hang up rather than
					// queueing — an unbounded accept queue is a resource sink.
					log.Printf("irc: %s: refused — connection limit (%d) reached",
						remoteKey(conn.RemoteAddr()), cfg.MaxConns)
					_, _ = conn.Write([]byte("ERROR :server full\r\n"))
					_ = conn.Close()
					continue
				}
				go func() {
					defer func() { <-sem }()
					newSession(h, cfg, token, lim, conn).serve()
				}()
			}
		}(l)
	}
	wg.Wait()
	shutdown()
}

// runService handles `dispatch-ircd service <verb> [flags]`.
//
// Flags are re-parsed here rather than read off the global set: the stdlib flag
// package stops at the first positional, so `service install --dry-run` would
// otherwise leave dryRun false and do the real thing — a dry run that isn't one
// is worse than no dry run at all.
func runService(args []string, cfg Config, dirOverride string, dryRun bool) {
	verb := ""
	if len(args) > 0 {
		verb = args[0]
		args = args[1:]
	}
	fs := flag.NewFlagSet("service", flag.ExitOnError)
	sub := fs.Bool("dry-run", false, "print what would be done, change nothing")
	if err := fs.Parse(args); err != nil {
		fatal("%v", err)
	}
	dryRun = dryRun || *sub

	switch verb {
	case "status":
		serviceStatus()
		return
	case "uninstall":
		if err := serviceUninstall(dryRun); err != nil {
			fatal("%v", err)
		}
		return
	case "install", "show":
	default:
		fatal("unknown service verb %q — valid: install, show, status, uninstall", verb)
	}

	// A unit that starts and then refuses at Validate() is a crash loop, so
	// check the config before writing one.
	if err := cfg.Validate(); err != nil {
		fatal("%v", err)
	}
	if _, err := ReadToken(cfg.TokenFile); err != nil {
		fatal("%v", err)
	}

	relayDir := relay.RelayDir(relay.LoadConfig())
	if dirOverride != "" {
		relayDir = relay.ExpandUser(dirOverride)
	}
	// Absolute, and resolved now: systemd keeps running the process it started,
	// so the unit must name the binary rather than whatever is on $PATH later.
	exePath, err := os.Executable()
	if err != nil {
		fatal("cannot resolve my own path: %v", err)
	}
	if p, err := filepath.EvalSymlinks(exePath); err == nil {
		exePath = p
	}

	unit, err := renderUnit(cfg, relayDir, exePath, os.Getenv("MCP_DISPATCH_CONFIG"))
	if err != nil {
		fatal("%v", err)
	}
	if verb == "show" {
		fmt.Print(unit)
		return
	}
	if err := serviceInstall(unit, dryRun); err != nil {
		fatal("%v", err)
	}
	if !dryRun {
		fmt.Print(lingerHint)
	}
}

// repoLabel describes the git bus for --check. A configured-but-unread bus says
// so out loud: printing "(none)" for it reads as "you have no git bus", which
// sends you looking for the wrong problem when the cross-host feed goes quiet.
func repoLabel(repo string, readGit bool) string {
	if repo == "" {
		return ""
	}
	if !readGit {
		return repo + "  ← configured but NOT read (read_git = false, or --no-git)"
	}
	return repo
}

func tcpLabel(c Config) string {
	if c.Listen == "" {
		return ""
	}
	s := c.Listen + " (TLS " + c.TLSMinVersion + "+"
	if c.TLSClientCA != "" {
		s += ", mutual"
	}
	return s + ")"
}

func orNone(s string) string {
	if s == "" {
		return "(none)"
	}
	return s
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "%s: %s\n", filepath.Base(os.Args[0]), fmt.Sprintf(format, args...))
	os.Exit(1)
}
