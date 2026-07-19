# Changelog

All notable changes to mcp-dispatch. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the git tag is the source of
truth for versions.

## [Unreleased]

## [0.8.0] - 2026-07-18

### Added
- `install.py` — one-command setup. Syncs dependencies, registers the MCP server
  (`claude mcp add`), and wires the SessionStart/Stop hooks that arm the
  wake-watcher and keep the cross-host git daemon running. Idempotent, with a
  `--dry-run` preview; backs up `~/.claude/settings.json` before touching it.
  `make install` runs it.
- `dispatch-gitsync-arm` is now wired on **Stop** as well as SessionStart, so the
  presence-gated git daemon self-heals after the host goes quiet and comes back —
  no manual relaunch after an idle period.

### Changed
- Quick Start leads with `python3 install.py`; the manual MCP + hook wiring is
  kept as a fold-out for hand setup. Added a "cross-host agents can't hear each
  other?" troubleshooting note pointing at `dispatch-gitsync status`.

## [0.7.1] - 2026-07-11

### Fixed
- TUI selection highlight now spans the full roster row (an interior ANSI reset
  from the pre-styled glyph had been breaking the `selStyle` background after the
  leading cell).

### Added
- `bin/dispatch-tui` launcher shim — builds `tui/` on first run and execs it, so
  the TUI starts like the other `bin/` tools.

## [0.7.0] - 2026-07-10

### Added
- **dispatch-tui** (`tui/`) — the repo's first Go component: an IRC-style Bubble
  Tea client for the relay. Groups the roster by project, keeps a persistent
  transcript across polls, reads both the local inboxes and the git bus, folds old
  sessions into a collapsible group, and can send/ack as a console nick. Read-only
  by default; sends guarded, ids validated, mouse optional.
- `dispatch-tail` now reads the git bus lanes — a full cross-host feed.
- Startup catch-up push so a restart recovers a remote lane frozen by a push
  outage (found via a real two-machine deployment).
- CI: a Go job (gofmt/vet/staticcheck/build/`test -race`) for `tui/`.

### Fixed
- Latch the first-run ledger so a quiet-start bridge can't silently drop
  cross-host messages.

### Changed
- Shared plumbing extracted to `dispatch_common.py`; both `bin/` scripts and the
  arm hooks dedup onto it.

## [0.6.0] - 2026-07-10

### Added
- Persistent Monitor wake watch: `dispatch-wait --follow` under the Monitor tool —
  one watch per session replaces the per-message re-arm loop.
- Transport first-run "bridge from now" guard (no backlog dump on enable) plus a
  single-machine loopback smoke test.

### Fixed
- Hooks share `dispatch_common`; fixes the `gitsync-arm` `[dispatch].auto_arm`
  drift.
- Repo-local git identity so transport push/rebase work on bare CI runners.

## [0.5.0] - 2026-06-24

### Added
- **Git-backed cross-host transport.** A bidirectional replicator daemon
  (`dispatch-gitsync`) bridges `DISPATCH_DIR` ↔ a shared git repo; remote messages
  materialize as normal inbox files, so they wake a parked session through the same
  path a local one does. Adds `GitBus` push/drain, `GitBridge`, the `dispatch_fs`
  extraction, `init`/`status` verbs, a presence-gated single-instance daemon, an
  auto-start hook, a `remote` roster in `who()`, and `via: "remote"` on `peek`.
- Language-independent wire contract for the git transport (`docs/git-transport.md`).

## [0.4.1] - 2026-06-10

### Changed
- Presence-gate `dispatch-wait` and default `--max-lifetime 0` — no heartbeat
  churn; the watch exits when its session's presence drops.

## [0.4.0] - 2026-06-10

### Added
- Hands-free auto-arm for `dispatch-wait` — parked sessions self-arm via the
  SessionStart/Stop hook.

## [0.3.1] - 2026-06-09

### Changed
- Default TTL raised 2h → 7 days so messages survive a parked/idle session instead
  of expiring unread.

## [0.3.0] - 2026-06-09

### Added
- `dispatch-wait` — wake a parked session on incoming direct messages, on a shared
  notify policy with the desktop notifier.
- `dispatch-tail` — live IRC-style view of relay traffic.
- `dispatch-status` — read-only relay inspector.
- Opt-in desktop notifier for parked/idle sessions; `group_mode` for sharing one
  relay across trusting accounts; `$PWD`-derived launcher identity; Stop-hook peek.
- `SECURITY.md` and Dependabot config.

[Unreleased]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/justinstimatze/mcp-dispatch/releases/tag/v0.3.0
