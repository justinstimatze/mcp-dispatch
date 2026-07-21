# Changelog

All notable changes to mcp-dispatch. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the git tag is the source of
truth for versions.

## [Unreleased]

## [0.11.0] - 2026-07-21

### Added
- The cross-host git bridge can run **independently of any agent harness**.
  `dispatch-gitsync service install` (or `python3 install.py --service`) writes an
  enabled, restart-on-failure systemd **user** unit; re-running it regenerates the
  unit from current config and restarts onto it, so it doubles as the upgrade
  path. `service show` prints the unit, `--dry-run` writes nothing, `uninstall`
  removes it, and `dispatch-gitsync status` reports its state. Interpolated values
  are escaped (`%` is a systemd specifier) and control characters refused, so a
  path or `--env` value can't forge a directive. Sandboxing is deliberately
  conservative — `PrivateTmp` is omitted when the relay lives under `/var/tmp`
  (the documented group-mode layout), `UMask` follows `group_mode`, and no
  directive that implies a capability drop is emitted — a *user* manager can't
  change the capability bounding set, so those kill the unit at spawn with
  `218/CAPABILITIES` before a line of Python runs.
- `dispatch-gitsync init <repo> --service` — the whole setup for a host that isn't
  running Claude Code, in one idempotent command: clone/seed the bus, write the
  `[git]` config, then install and start the service. Re-running it upgrades in
  place. (`--dry-run` covers the service half only — the clone and the `[git]`
  config are written either way.)
- `--no-presence-gate` / `[git] presence_gate = false` — run until stopped rather
  than exiting when no agent is live. Ungated, the daemon also waits for a relay
  that doesn't exist yet (a service can start at login before any agent has), waits
  to take over the host lock instead of exiting into a restart loop, and backs off
  to 60s after repeated sync failures instead of hammering a broken remote.

### Changed
- A supervised daemon (`--no-presence-gate`) now **waits** instead of exiting when
  it isn't ready to bridge — `[git].enabled` false, no clone configured, the clone
  missing, or the relay not created yet. Each of those exited immediately, which
  under the unit's `Restart=always` is a crash loop that latches the service
  `failed` after ten tries; recovering then meant noticing a dead unit and running
  `systemctl` by hand. Config is re-read every pass, so enabling the bridge or
  restoring a deleted clone starts it with no further intervention. A gated or
  `--once` run still reports and exits exactly as before.
- The daemon no longer `git fetch`es on every pass when the bus is quiet. A fetch
  costs ~170ms of CPU against a real remote while the entire local scan costs
  ~12ms, so a supervised 24/7 daemon spent essentially all of its CPU asking a
  silent remote whether anything had happened — measured at 6.45% of a core on the
  installed service, now 1.25%. The inbound cadence now decays
  toward `[git] max_fetch_interval` (default 30s) while nothing moves and snaps
  back to `interval` on any traffic in either direction. Outbound is untouched, so
  sends are as fast as before; only noticing the first message after a lull can be
  delayed. Set `max_fetch_interval = 0` for the old behaviour.
- The remote roster is only rewritten when it changes, instead of one write+rename
  per known remote agent per pass forever.

### Fixed
- Agents on a harness other than Claude Code had no running bridge at all. The only
  thing that started the daemon was `hooks/dispatch-gitsync-arm.py`, a *Claude
  Code* SessionStart/Stop hook — so openclaw, Hermes, the TUI, scripts and cron got
  nothing pushed or fetched and had to run `git pull`/`git push` by hand around
  every message. Even started by hand it self-terminated after the 60s grace,
  because presence is only ever claimed by the mcp-dispatch MCP server. Reported by
  Steven Wu.

## [0.10.0] - 2026-07-19

### Fixed
- A post to a channel you subscribe to now wakes a parked session under
  `notify_on = "direct"`. Fan-out already put a durable copy in each subscriber's
  inbox, but the wake predicate matched only `to == my id` — and a channel
  message's `to` is `#room`, so every subscriber's `dispatch-wait --follow` watch
  silently dropped it. The sender saw it queued and stopped chasing; the message
  was never read. Subscribing is the opt-in, so a subscribed room now counts as
  addressed; broadcast (`all`) deliberately still does not.
- Unread mail no longer dies with the session that was addressed. Dynamic-mode
  ids are `<project>-<pid>`, so a restart is a new identity with an empty inbox
  and the predecessor's `pending` messages rotted in a directory nobody would
  open again. A successor now adopts them at startup (tagged `inherited_from`),
  guarded to same project, dead presence lock, and same account. Opt out with
  `inherit_inbox = false`; no effect in roster mode.

### Changed
- **Breaking:** `dispatch()` returns `queued_to` instead of `delivered_to`. The
  old name conflated addressing with receipt — it only ever meant "written to
  these inboxes." Whether anyone read it is `sent_receipts` in `peek()`, where a
  message flips `pending` → `read`.

## [0.9.0] - 2026-07-18

### Added
- `MCP_DISPATCH_CHANNELS` — auto-subscribe standing rooms on startup (#13, by
  @fiorastudio). Comma/space-separated, leading `#` optional, deduped, sorted;
  names are lowercased (matching `MCP_DISPATCH_AGENT_ID`) so `#Ops` joins `#ops`;
  structurally-invalid ids are skipped with a warning rather than aborting.
  Durable complement to the ephemeral, presence-based `subscribe()`.

### Fixed
- TUI renders message times in the viewer's local zone at whole-second precision
  with per-day dividers (#12, by @fiorastudio), so the time-only column no longer
  reads hours-off, ragged with stray microseconds, or out of order across the
  UTC-midnight boundary.

## [0.8.1] - 2026-07-18

### Changed
- `LICENSE` now lists both copyright holders — Sophia Labs (retained, as MIT
  requires) and Justin Stimatze — reflecting the fork's mixed authorship.

### Removed
- The `SOPHIA_AGENT_ID` environment variable, a backward-compat alias for
  `MCP_DISPATCH_AGENT_ID` inherited from the upstream. Set `MCP_DISPATCH_AGENT_ID`
  instead.

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

[Unreleased]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/justinstimatze/mcp-dispatch/releases/tag/v0.3.0
