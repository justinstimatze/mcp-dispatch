# Changelog

All notable changes to mcp-dispatch. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the git tag is the source of
truth for versions.

## [Unreleased]

### Upgrading an existing relay

Nothing to migrate — the new state is created on demand — but three things
change under a running install:

- **Restart your sessions.** The `task` and `digest` tools are new, and an MCP
  server's tool list is read at session start. This bites harder than a missing
  tool: a server started before the durable-identity change keeps resolving
  `dispatch(target="<nick>")` the old way, writing to the nick's drop box even
  when that teammate has a live session — so mail waits for the *next* session
  of an agent that is running right now. Observed in the wild while building the
  digest. `dispatch-status` and the bin/ scripts read current code on every run,
  so they will disagree with a stale server; the server is the stale one.
- **Two directories appear** in the relay: `.agents/` (the durable nick
  registry) and `.tasks/`. Both are created on demand and are owner-only.
- **`dispatch(target="<nick>")` resolves now.** Sending to `publicai` used to
  write literally to `{relay}/publicai/`, which no `publicai-<pid>` session ever
  read; it now reaches that teammate's live sessions, or waits in the nick's
  inbox for the next one. Addressing a concrete session id is unchanged. This is
  the fix, but it *is* a behaviour change to an existing call.

### Added
- **The away-digest — `digest()` and `bin/dispatch-digest`.** A session that
  starts after a gap knew only what was in its inbox; everything else that moved
  while it was gone sat on disk unread because nothing assembled it. The
  supervisor sharpened that: an agent it starts has no human to brief it. The
  digest reports unread mail by sender, task transitions since the last session
  ended, open tasks addressed to this nick, and who was around.

  The window starts at `previous_seen`, which `_register_agent` now preserves
  from `last_seen` before overwriting it — and only on the offline→online edge,
  since a live sibling session means the teammate never left and advancing the
  mark would collapse the window and hide what the sibling hasn't handled.
  `_release_id` already stamped `last_seen` before dropping the presence lock, so
  that value is genuinely the last moment the nick was present. After an unclean
  exit there is no such mark, the window is too wide, and the digest reports it
  as approximate rather than hiding it: a report that silently under-reports is
  worse than none, because you stop looking.

  Reading does not consume. No cursor advances and nothing is deleted, so the
  digest is a pure function of relay and window — asking twice gives the same
  answer, and a session that dies mid-read loses nothing. A read cursor would be
  at-most-once delivery for exactly the content you cannot afford to drop.

  One gap is stated rather than papered over: channel posts fan out to live
  subscribers only, so a post made while a nick was offline left no local record
  — absent, not unread. The git lanes retain it; the digest does not read them
  yet.
- **Lifecycle supervisor (`bin/dispatch-supervise`).** Durable nicks made an
  offline teammate addressable; nothing made one answer. A DM to `publicai` sat
  in its inbox until a human happened to open that project. The supervisor
  watches for mail waiting on a nick with no live session and starts that nick's
  runtime — `service install` puts it under systemd, `check` validates the
  allowlist, `status` reports who is configured, live and waiting, `--dry-run`
  shows what it would start.

  The trigger is not a new rule: it is exactly the mail a successor session
  *would inherit* (the nick's own inbox plus any dead session inbox of that
  nick), so the supervisor fires precisely when there is something to find, and
  a successful start clears the trigger by itself. A test asserts that agreement
  differentially against the real inheritance path rather than restating its
  rules, because a copy of a rule is a copy that can drift.

  An inbound message causing a process to run is remote-triggered execution, and
  over the git bus those messages come from other machines. So what runs comes
  only from an operator-written allowlist: a nick with no
  `[supervisor.agents.<nick>]` block is never started whatever it is sent (there
  is no wildcard, and `enabled = true` alone supervises nothing), `command` is
  argv rather than a shell string, `command[0]` must be absolute so a service's
  minimal `PATH` cannot decide which binary a bare name means, and nothing from
  a message reaches argv, cwd or env — the supervisor reads messages only to
  count them. A test plants a message full of shell metacharacters and asserts
  none of it appears in the child's argv or environment.

  Rate limits bound the damage independently of any of that being correct: a
  per-nick cooldown, a starts-per-hour ceiling, a concurrency cap, and a breaker
  that parks a nick after repeated failures. The ceiling exists for a specific
  loop — a runtime that starts, dies without reading its mail, and would
  otherwise be restarted forever, each start "succeeding" and so never tripping
  the failure counter. Off unless `[supervisor] enabled = true`.

  Two config interactions the first review caught: under `inherit_inbox = false`
  the trigger narrows to the nick's own inbox, because a successor adopts
  nothing and starting an agent for mail it cannot see would leave the mail in
  place and repeat the start until the ceiling pinned it; and a stray
  `MCP_DISPATCH_AGENT_ID` in the launching shell is dropped from the child's
  environment, since inheriting it would pin every started agent to the id of
  whatever session launched the supervisor. A second supervisor now **waits**
  for the host lock rather than exiting — this daemon only ever runs supervised,
  and `Restart=always` restarts on a clean exit too, so exiting would have
  latched the unit `failed` after ten restarts.
- **IRC gateway polish.** Three IRCv3 capabilities, implemented rather than
  merely advertised: `server-time` (each message shows the time it crossed the
  relay — without it a JOIN replay is fifty messages that all look like they
  arrived just now, which is worse than no history), `message-tags` (carries
  `msgid`), and `sasl`. `CAP REQ` is now honoured **atomically** — a set naming
  anything unsupported is NAK'd whole instead of half-ACK'd, which previously
  left a client formatting for a capability the gateway would never send.
  `CAP LS 302` is understood.
- `/msg dispatch inbox` lists what is actually addressed to you, with each
  message's id and read state, and `ack` now takes those ids: `ack <id> [<id>…]`
  acknowledges exactly those and reports any it could not find. `MOTD`,
  `VERSION`, `TIME` and `LUSERS` answer properly.
- A server-side keepalive PINGs an idle authenticated client at half the idle
  timeout (floored at 15s). Without it the read deadline was a guillotine: a
  healthy but quiet client was disconnected at `idle_timeout`, which on a relay
  that can be silent for hours is most of them. A client that stops answering
  still hits the deadline, so dead connections are still reaped.
- The connection log records the transport (`unix`, `TLS1.3`, `TLS1.3+clientcert`).
- `dispatch-ircd service install|show|status|uninstall` writes an enabled,
  restart-on-failure systemd **user** unit, matching what `dispatch-gitsync`
  already offers. It validates the config and token before writing anything (a
  unit that starts and then refuses is a crash loop, not an error message),
  escapes `%` so a path can't be read as a systemd specifier, refuses control
  characters that would forge a directive, quotes the exec path, and emits no
  directive implying a capability-bounding-set change — a *user* manager can't
  perform one, and the unit would die at spawn with `218/CAPABILITIES`. Ordering
  on `network-online.target` only when a TCP listener is configured; `PrivateTmp`
  dropped for the documented `/var/tmp` group-mode relay layout.
- **`bin/dispatch-agent-claude` — the runtime the supervisor actually starts.**
  The supervisor shipped with an allowlist whose example pointed at a script
  nobody had written, which made the feature complete in the sense that mattered
  least. This is that script: a headless Claude Code session for one project,
  which reads its mail, replies, and exits.

  It comes up **stripped** — `--strict-mcp-config` with dispatch as the only
  server, and an allowed-tools list holding only the dispatch tools. A woken
  agent therefore has no file or shell tools, and is told to say so rather than
  improvise if a message asks for real work. `DISPATCH_AGENT_TOOLS`,
  `DISPATCH_AGENT_MODEL`, `DISPATCH_AGENT_MAX_TURNS` and `DISPATCH_AGENT_FULL_MCP`
  widen it per nick from the allowlist's `env` table — operator-set, never
  message-set, and none of them the default.

  What stripping saves is worth stating accurately, because the first version of
  this note overstated it. Measured on a 15 GiB box with seven sessions up: a
  session's MCP servers are 80–96 MiB PSS, the `claude` process itself is
  410–720 MiB. Dropping eight of nine servers is real and it is the smaller
  share — the memory case for the supervisor is *not running the session at
  all*, which holds whether the woken one is stripped or not. Stripping buys a
  cheaper and faster transient, and a remote-triggered process that never holds
  file or shell tools. Only the first of those is a percentage.

  The prompt is a constant in the script. A woken agent learns what it was sent
  by asking the relay, which is the same path a human-started session takes.

### Fixed
- **`dispatch-supervise check` claimed a never-seen nick could not fire.** The
  warning read "it will only ever fire once some session has used that id",
  which is false: the trigger reads inboxes, and dispatching to an unknown name
  creates that inbox on demand, so a correctly-spelled new nick starts the first
  time anyone writes to it. Found during the first pilot, where `check` warned
  about the entry and `--dry-run` started it thirty seconds later. The advice was
  right (it is usually a typo) and the mechanism was wrong, which is the worse
  half to get wrong — it sends an operator hunting for a bug in a working config.
- **The IRC gateway ignored the cross-host git bus unless `read_git` was set
  explicitly**, though every doc gave its default as `true`. It was a plain Go
  bool, so an omitted key took the zero value and silently disabled the remote
  feed: no error, no `«remote»` messages, and `--check` reporting the bus as
  `(none)` as though none were configured. It is now a pointer read through
  `ReadGitEnabled()`, so absent means true and a zero `Config` cannot
  reintroduce the bug, and `--check` prints a configured-but-unread bus as such
  rather than hiding it behind `(none)`. Found by pointing a real IRC client at
  a relay with a live git bridge and seeing no remote traffic at all.
- **The pinning instructions named an irssi option that does not exist.** It is
  `-tls_pinned_cert`, not `tls_cert_fp` — and the fingerprint must be in the
  colon-separated form, since bare hex fails as `Pinned certificate mismatch`,
  which accuses the certificate rather than the punctuation. WeeChat wants the
  opposite (bare hex, `tls_fingerprint`; it renamed `ssl_*` to `tls_*` in 4.0),
  so the docs now carry a per-client format table.
- **`server-time` is documented per client.** The gateway sends `@time=` with
  the instant each message crossed the relay — verified on the wire — but
  clients disagree about rendering it. Halloy honours it, so a JOIN replay shows
  each message's real time; irssi 1.4.5 negotiates the capability and then
  stamps replayed lines with their arrival time anyway, which is the exact
  failure the capability prevents. Both are named, along with the one-liner that
  proves which side is at fault.
- **Shutdown dropped live connections instead of closing them.** It closed the
  listeners and let the process exit, so established connections died with the
  process — no IRC `ERROR` line, and on TLS no `close_notify`, which a strict
  client reports as a truncated stream rather than a disconnect ("peer closed
  connection without sending TLS close_notify"). Now that the gateway runs under
  systemd and is restarted on every upgrade, that made routine restarts look
  like faults. It now sends `ERROR :Server shutting down`, drains, and closes
  each connection — the `drain()` helper the refusal paths already used for
  exactly this reason.
- **`--init-tls` produced a certificate that strict TLS clients refuse.** It
  wrote one self-signed certificate marked `CA:TRUE` so it could serve as its
  own trust anchor — but a certificate with `CA:TRUE` is not a legal
  *end-entity* certificate, and rustls enforces that where OpenSSL does not.
  Every Rust IRC client, Halloy included, failed the handshake with
  `CaUsedAsEndEntity`; `openssl s_client` against the same listener reported
  `Verify return code: 0 (ok)`, so checking with OpenSSL confirmed a
  configuration no such client could use. It now writes a real chain: a small CA
  that signs a leaf which is not a CA. The CA's key is generated, used once, and
  never written, so no signing key is left at rest.

  `tls_cert` now holds leaf-then-CA and is served as a chain; a new
  `irc-cert-ca.pem` holds the CA alone, which is what `root_cert_path` and
  equivalents want. **Point a root-certificate setting at the CA, never at the
  served certificate.** The printed fingerprint is the leaf's, unchanged in
  meaning for pinning clients. Existing certificates must be regenerated with
  `--init-tls --force`; pinning clients will need the new fingerprint.
- **`--check` explains an unbound unix socket instead of printing `(none)`.**
  The socket default applies only while `listen` is unset, so adding a TCP
  listener silently takes the socket away — from a reader the same document has
  just told, twice, to prefer the socket and to run a bouncer over it. The
  config table documented the default flatly, with no mention of the condition.
  Both now say so, and the quick start sets `socket` alongside `listen`.
- **GUI client guidance is corrected to `password_file`.** It recommended
  `password_keyring`; pointing the client at the gateway's own `0600` token file
  is strictly better — no second copy of a credential that is read/write on the
  whole relay, nothing to re-sync on rotation, no keyring daemon to depend on.
  Documents `password_file_first_line_only`, without which `--init-token`'s
  trailing newline is sent as part of the password and auth fails with a
  wrong-token error for a token that is right. Also warns off snap and flatpak
  builds: a confined client cannot read `~/.config` at all.

### Removed
- **`bin/dispatch-tail`.** Its own docstring described it as watching messages
  "scroll by like IRC", and `&dispatch` on the gateway is now literally that.
  It was also a third independent implementation of the on-disk contract —
  stdlib-only Python that shared no code with `git_transport.py` or
  `tui/relay/`, reimplementing config resolution, git-lane scanning,
  dedupe-by-msgid and `«remote»` marking. Three copies of a wire format is how
  the copies drift. Use `dispatch-tui` for a no-listener view, the gateway's
  `&dispatch` for a scrolling one, and `bin/dispatch-status` for a snapshot.

### Changed
- **Internal: three rules that had two homes now have one.** `durable_nick` and
  the TTL-expiry predicate moved from `server.py` into `dispatch_fs` (which
  exists precisely so a second process can share the on-disk contract without
  importing a module that claims an agent id at import), and the systemd unit
  machinery — escaping, the 0600 write, reload/enable/restart, status — moved
  from `gitsync_service.py` into `systemd_user.py`. The supervisor needs all
  four; copying them would have meant a second implementation of code whose only
  job is being careful with untrusted input. No behaviour change.
- **A bare `/msg dispatch ack` no longer acknowledges your entire inbox.** It
  prints usage instead. Acking is destructive — the MCP server deletes an acked
  message — and that is not a sensible default for a command you might type to
  clear one notification. Say `ack all` when you mean all.

### Security
- **TLS is required on every TCP listener the IRC gateway binds — loopback
  included.** There is no cleartext TCP mode at any address any more. Cleartext
  on `lo` is not private: anything on the host that can capture the loopback
  interface reads the auth token, which is equivalent to read/write on every
  conversation on the relay. The unix socket remains the supported unencrypted
  transport (kernel-mediated, `0600`, uid-checked) and remains the default, so
  needing this is the exception.
- `dispatch-ircd --init-tls` generates a self-signed ECDSA P-256 certificate
  covering localhost, the loopback addresses and this host's name (plus
  `--tls-hosts`), writes the key `0600`, and prints the SHA-256 fingerprint —
  also printed on every start, since a self-signed certificate is trusted by
  pinning and a pin you can't re-check is not a pin.
- `tls_min_version` defaults to **1.3**, with `1.2` available for a client too
  old to speak it. Nothing below 1.2 is reachable through the knob, and an
  invalid value fails validation rather than falling back silently.
- `tls_client_ca` adds mutual TLS (`RequireAndVerifyClientCert`). Deliberately
  an *additional* gate: unlike the conventional IRC CertFP/SASL EXTERNAL move, a
  certificate never replaces the token — that would be a second authentication
  path with its own bugs, and requiring both costs a configured client nothing.
  An unusable CA file fails at startup instead of silently disabling mTLS.
- Encryption and exposure are now independent axes. `allow_remote` is still
  required for a non-loopback bind and buys exposure only, never cleartext; TLS
  does not make a public bind automatic.

### Added
- **Tasks** — a `task(action, ...)` tool for claimable work items (`create`,
  `claim`, `done`, `list`), stored under `{dispatch_dir}/.tasks/`. Claiming is
  an `O_EXCL` create rather than read-then-write, so exactly one agent wins a
  race and doctoring the record can't reopen it; re-claiming your own task is
  idempotent and completing one you don't hold is an error. Creating with a
  target dispatches an ordinary message carrying a `task.created` payload, so
  the announcement wakes parked sessions, crosses hosts on the git bus, and
  reaches the TUI and the IRC gateway through the paths that already exist. The
  gateway reads the board (`/msg dispatch tasks`) but does not write it.
- **Durable agent identity.** Every id now also has a *nick* — itself with the
  `-<pid>` suffix stripped — recorded in `{dispatch_dir}/.agents/<nick>.json`
  (first/last seen, session count, last session id, standing channels) and never
  reaped. `who()` gains a `known` list of teammates that exist but aren't live;
  `dispatch(target="publicai")` resolves to that teammate's live sessions rather
  than writing to a directory no session opens; and mail sent while a nick is
  offline waits in the nick's own inbox, which its next session inherits at
  startup. Addressing a concrete session id is unchanged. The rule lives in the
  shared `tui/relay` package as well as `server.py`, so the MCP server, the TUI
  and the IRC gateway all write the same files.
- **`bin/dispatch-ircd` — an IRC gateway to the relay.** Any IRC client now works
  against mcp-dispatch: agents are nicks (by their stable id, not the per-session
  pid), `#name` targets are channels, and `&dispatch` is a read-only firehose.
  Ack, priority and the rest of what IRC has no verb for live behind a `dispatch`
  service nick you `/msg`. A bouncer in front adds scrollback and phone push, so
  a mobile client costs this repo no UI code. Like the TUI it is an observer and
  a sender — it does not claim presence, so the relay's presence semantics stay
  owned by the MCP server alone.
- Lockdown, since this is the first component here that accepts a connection:
  off unless `[irc] enabled = true` in the config (no flag equivalent); a `0600`
  unix socket by default with no port at all; `SO_PEERCRED` uid check on that
  socket, enforced before a byte is read and even if the token leaks; a mandatory
  token on every transport (≥32 chars, `0600`, constant-time compare over
  digests); per-source failure counting with a temporary ban; nothing served
  before authentication (no roster, no channel list, not even the shape of the
  relay); relay-safe nick validation; CR/LF stripped from outbound lines so
  content can't forge protocol; bounded lines, connections, and queues; and a
  refusal to bind a non-loopback address without *both* `allow_remote` and TLS —
  with wildcard binds counted as public. See `docs/irc-gateway.md`.

### Changed
- The Go relay reader moved from `tui/relay.go` to a shared `tui/relay` package
  so the TUI and the gateway implement the on-disk contract exactly once. The UI
  keeps its old names through type aliases, so `Message` in the TUI *is*
  `relay.Message` and the two can never drift. No behaviour change.
- `bin/dispatch-tui` rebuilds on changes anywhere under `tui/`, not just its top
  level — the relay reader it depends on now lives in a subdirectory.

### Fixed
- `config.example.toml` put the top-level `instructions` key *after* the `[git]`
  table, so TOML nested it inside `[git]` and the server silently ignored it.
  Anyone who copied the example verbatim got the built-in affordance contract
  instead of their own. Top-level keys now precede every table.

## [0.11.1] - 2026-07-21

### Added
- `scripts/setup-cross-host.sh <bus-url>` — one command that sets a host up on the
  bus and checks the things people actually trip over: reachability of the bus
  (failing early with the real git error), whether the *service* can authenticate
  given that it inherits almost nothing from your shell, and whether systemd user
  lingering is enabled. It verifies the daemon is genuinely syncing rather than
  merely started, since auth failures appear only in the service's own log.

### Fixed
- Documented systemd **user lingering**. Without `loginctl enable-linger`, the
  service doesn't start at boot and is torn down with your last session on many
  distros — so on a headless host the bridge ran only while someone was logged in,
  which presents identically to the cross-host outage 0.11.0 set out to fix. The
  service could be installed, enabled and correct, and still not be running.

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

[Unreleased]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.11.1...HEAD
[0.11.1]: https://github.com/justinstimatze/mcp-dispatch/compare/v0.11.0...v0.11.1
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
