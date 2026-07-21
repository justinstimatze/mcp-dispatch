# mcp-dispatch

Local-first inter-agent messaging for AI coding agents via [MCP](https://modelcontextprotocol.io/), with an optional git transport for cross-host comms.

Multiple Claude Code sessions (or any MCP-compatible agents) running on the same machine send messages to each other through a shared filesystem relay — no server process, no ports, no network, just directories and JSON files with atomic writes. When you need to reach agents on **other machines**, an opt-in [git transport](#cross-host-comms-git-transport) bridges the same tool surface across hosts; until you enable it, everything stays purely local.

## Features

- **Non-destructive messaging** — Messages persist until explicitly acknowledged. No more lost messages from crashes or compaction.
- **Channels** — `subscribe('#name')` and `dispatch(target='#name')` fan a message out to current subscribers. Ephemeral — subscriptions vanish when a session exits.
- **Threading** — Group messages into conversations with `thread_id` and `reply_to`.
- **Structured payloads** — Attach machine-readable data alongside human-readable messages.
- **TTL & must_read** — Time-sensitive messages auto-expire. Critical messages survive until acknowledged.
- **Delivery receipts** — `peek()` shows read/unread state of messages you've sent.
- **`$PWD`-derived identity** — `bin/dispatch-launcher` gives each session a `<project>-<pid>` id with no per-window config.
- **Live tail & TUI** — `bin/dispatch-tail` streams every message across the relay (local + cross-host) to a terminal, IRC-style; `tui/dispatch-tui` is a full-screen [Bubble Tea](https://github.com/charmbracelet/bubbletea) client with a nick/channel sidebar for watching sessions talk in real time — and sending to them (`i`) or acking your own inbox (`a`) as a console nick.
- **Wake on arrival** — `bin/dispatch-wait --follow` run under the Monitor tool streams a wake event per incoming message into a parked model — one persistent watch per session, event-driven, zero idle tokens, replacing `/loop` polling.
- **Config-driven** — TOML config for agent rosters, directories, and limits. Or go dynamic with no roster.
- **Zero infrastructure** — Filesystem relay survives process crashes. No daemon to manage.
- **Local-first & per-user** — `0700`/`0600` perms, validated ids, no network by default. See [Security](#security).
- **Optional cross-host** — An opt-in [git transport](#cross-host-comms-git-transport) reaches agents on other machines through a shared git repo, transparently, without changing how agents call `dispatch()`.

## Quick Start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/justinstimatze/mcp-dispatch.git
cd mcp-dispatch
python3 install.py        # sync deps, register the server, wire the hooks
```

`install.py` does the whole setup in one shot — it syncs dependencies, registers
the MCP server with Claude Code (`claude mcp add`), and wires the SessionStart /
Stop hooks that arm the wake-watcher and keep the cross-host git daemon running.
It's **idempotent** (re-run any time; it only adds what's missing) and writes a
backup of `~/.claude/settings.json` before touching it. Preview without writing:

```bash
python3 install.py --dry-run
```

Then restart your Claude Code sessions so the new config loads, and jump to
[Send messages](#3-send-messages). The manual steps below are what the installer
automates — read them if you want to wire things by hand or understand what it
did.

<details>
<summary>Manual setup (what <code>install.py</code> automates)</summary>

### 1. Install

```bash
uv sync
```

For real-time stderr alerts when messages arrive (optional):

```bash
uv sync --extra watch
```

### 2. Configure Claude Code

Add to your `~/.claude.json`:

```json
{
  "mcpServers": {
    "dispatch": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-dispatch", "python", "server.py"],
      "env": {
        "MCP_DISPATCH_AGENT_ID": "alice"
      }
    }
  }
}
```

Each Claude Code window needs a unique `MCP_DISPATCH_AGENT_ID`. To avoid hand-
assigning one per window, point `command` at the launcher instead, which derives
a `<project>-<pid>` id from the working directory:

```json
{
  "mcpServers": {
    "dispatch": {
      "type": "stdio",
      "command": "/path/to/mcp-dispatch/bin/dispatch-launcher"
    }
  }
}
```

A session started in `~/code/webapp` then becomes agent `webapp-<pid>`. An
explicit `MCP_DISPATCH_AGENT_ID` always wins.

</details>

### 3. Send messages

From any Claude Code session:

```
Agent alice: dispatch("Hey bob, I pushed the fix", target="bob")
Agent bob:   peek()  →  sees alice's message
Agent bob:   ack(["msg-abc12345"])  →  message removed
```

## Tools

| Tool | Description |
|------|-------------|
| `dispatch(message, target, ...)` | Send to one agent (`id`), a channel (`#name`), or `all` |
| `peek(thread_id?, include_read?)` | Read messages and delivery receipts for sent messages |
| `ack(message_ids)` | Acknowledge and delete processed messages |
| `who()` | List connected agents and their channel subscriptions |
| `subscribe(channel)` / `unsubscribe(channel)` | Join / leave a channel |

### dispatch

```python
dispatch(
    message="Deployed to staging",
    target="all",           # an agent id, a "#channel", or "all"
    priority="normal",      # "normal" or "urgent"
    thread_id="deploy-123", # optional: group into conversation
    reply_to="msg-abc",     # optional: reference specific message
    payload={"commit": "abc123", "env": "staging"},  # optional: structured data
    ttl=3600,               # optional: expire after 1 hour
    must_read=True,         # optional: survive TTL, require explicit ack
)
```

The response includes `queued_to` — the inboxes the message was written to
(empty for a channel with no current subscribers). That is *addressing, not
receipt*: it says the message is durably waiting, not that anyone has looked at
it. To confirm it was read, check `sent_receipts` in your next `peek()` — a
recipient flips the message from `pending` to `read` when they read it.

### channels

```python
subscribe("#deploys")              # join (with or without the leading '#')
dispatch("staging is up", "#deploys")   # fan out to current subscribers
unsubscribe("#deploys")            # leave
```

Channels are presence-derived and ephemeral: a subscription lives only as long
as the session, and a channel send reaches whoever is subscribed *right now*.
Fan-out happens at send time, so each subscriber gets a durable copy in its own
inbox — and under `notify_on = "direct"` a post to a subscribed channel wakes a
parked session exactly like a DM does. (Set `MCP_DISPATCH_CHANNELS` to rejoin
standing rooms automatically on every restart.)

### peek

```python
peek()                          # new (unread) messages only
peek(include_read=True)         # all unacknowledged messages
peek(thread_id="deploy-123")    # filter by thread
```

### ack

```python
ack(message_ids=["msg-abc", "msg-def"])  # delete specific messages
```

## Configuration

Create `~/.config/mcp-dispatch/config.toml`:

```toml
# Agent roster (omit for dynamic registration — any name accepted)
agents = ["alice", "bob", "carol"]

# Message directory (default: ~/.config/mcp-dispatch/messages)
dispatch_dir = "~/.config/mcp-dispatch/messages"

# Maximum message size in bytes (default: 65536)
max_message_bytes = 65536

# Default TTL in seconds (0 = no expiry; must_read overrides). Default: 604800 (7 days)
# — long enough that messages survive a parked/idle session instead of expiring
# unread. Set a short ttl= explicitly on time-sensitive sends.
default_ttl = 604800

# Custom MCP instructions template (optional)
# Placeholders: {agent_id}, {agent_list}
# instructions = "You are {agent_id}. Available agents: {agent_list}."
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `MCP_DISPATCH_AGENT_ID` | Agent identity (required in dynamic mode) |
| `MCP_DISPATCH_CHANNELS` | Channels to auto-subscribe on startup (comma/space separated, `#` optional) so standing rooms survive restarts without a manual `subscribe()` |
| `MCP_DISPATCH_CONFIG` | Config file path (default: `~/.config/mcp-dispatch/config.toml`) |
| `MCP_DISPATCH_DIR` | Override dispatch directory from config |

A fuller annotated example lives in [`config.example.toml`](config.example.toml),
including an `instructions` template that wires a loose escalation seam to a
structured-deliberation tool via the generic `payload` field.

### Dynamic Mode

When no `agents` roster is configured, any validated agent name is accepted (see [Security](#security) for the id rules). Inbox directories are created on demand. This is more flexible but less safe (typos create phantom agents). It pairs naturally with the launcher's `<project>-<pid>` ids.

### Stop hook (optional)

Sessions that only *consume* messages never call a dispatch tool, so piggyback
delivery never fires for them. `hooks/dispatch-peek.py` is a Stop hook that reads
the inbox directly from the filesystem and surfaces unread messages back into the
conversation, rate-limited to every 5th turn. Wire it into `~/.claude/settings.json`:

```json
{ "hooks": { "Stop": [ { "hooks": [
  { "type": "command", "command": "/path/to/mcp-dispatch/hooks/dispatch-peek.py" }
] } ] } }
```

It resolves your agent id from `$MCP_DISPATCH_AGENT_ID`, falling back to the single
live `<project>-<pid>` agent for the session's directory. It is read-only —
acknowledge messages with `ack()`.

### Parked sessions (notifications)

Piggyback and the Stop hook both need the session to be *taking turns*. A parked
session — model idle while you work elsewhere — takes none, and a dormant model
can't wake itself. But the server process stays alive, so it can alert **you**.
Set `notify_command` (e.g. `notify-send` on GNOME) and the server shells out to
it when a message arrives, even with the model idle — no Python dependency, no
polling of the model. `notify_on` controls which messages alert: `direct`
(addressed to this agent — a DM, or a post to a channel it subscribes to),
`important` (urgent priority), `all`, or `none`;
`must_read` always pierces except under `none`. Meanwhile `must_read` guarantees
the message itself waits until that session next takes a turn and acks it. See
`config.example.toml`.

### Waking a parked session (`dispatch-wait`)

A desktop notification alerts *you*, but the model still won't act until its
next turn. To wake the **model** on arrival without burning turns on a timer,
run `bin/dispatch-wait --follow` under the **Monitor** tool. Monitor streams each
line the script prints into the parked session as a wake event, and
`dispatch-wait --follow` prints one line per qualifying message and keeps
running — so a **single registration covers the whole session**, waking the model
on every arrival (local or cross-host) with nothing to re-arm.

A watch holds a per-agent lock for its lifetime, so starting a second one for the
same agent exits immediately rather than double-arming.

```bash
# the wake path (started for you by the Monitor tool, see below)
dispatch-wait --follow             # stream one wake event per qualifying message

# one-shot / standalone forms (exit on first hit; relaunch to re-arm)
dispatch-wait                      # block until a notify_on-qualifying message lands
dispatch-wait --notify-on direct   # wake only on messages addressed to me
dispatch-wait --interval 1         # poll seconds (default 2.0)
dispatch-wait --max-lifetime 600   # add a wall-clock cap (default 0 = none)
```

By default there's no time cap: the watch exits the instant its agent's presence
flock drops (the session's server died), so it can't outlive its session and
orphan — independent of whether the harness reaps background tasks on close.
`--max-lifetime` adds a wall-clock cap on top; standalone use with no live server
to gate on falls back to a finite cap automatically.

This replaces `/loop` for staying responsive while idle: `/loop` fires a whole
model turn every interval whether or not anything arrived; the watch spends
**zero** model tokens while blocking and wakes only on a real qualifying arrival.
It reads the same `notify_on` policy the desktop notifier uses (one source of
truth, `notify_policy.py`), so the two paths never disagree. It is level-triggered
— a message already unread at launch is emitted immediately, so a watch started
mid-session never misses a backlog. Cross-host messages arrive as ordinary inbox
files (see the git transport below), so the same watch covers them and flags them
`«remote»`. Resolves identity from `$MCP_DISPATCH_AGENT_ID` (set it, for a dozen
sessions especially).

#### Arming it hands-free (`hooks/dispatch-arm.py`)

Starting the watch is the one manual step. Only the model can start a wake source
(the harness wakes on a *model-launched* Monitor event or background-task exit,
not on an arbitrary process), but a hook can make the model do it.
`hooks/dispatch-arm.py`, wired into **SessionStart and Stop**, checks whether a
watch holds the arm lock; if none does, it tells the model to start
`dispatch-wait --follow` under Monitor. On SessionStart it injects the instruction
(retrying identity resolution briefly to ride out the race with the server
claiming presence); on Stop it *blocks* (capped, so a failing launch can't wedge
the session — past the cap it warns loudly instead of going silent) so the model
never parks unarmed. Because the watch is persistent, this fires **once per
session**, not once per message — the old per-message re-arm loop (and its
flakiness) is gone. When cross-host git comms are enabled, the nudge also reports
whether the bridge daemon is actually running. Once a watch is armed the hook is
silent.

```json
{ "hooks": {
  "SessionStart": [ { "hooks": [
    { "type": "command", "command": "/path/to/mcp-dispatch/hooks/dispatch-arm.py" }
  ] } ],
  "Stop": [ { "hooks": [
    { "type": "command", "command": "/path/to/mcp-dispatch/hooks/dispatch-arm.py" }
  ] } ]
} }
```

Disable per environment with `auto_arm = false` in the config or
`MCP_DISPATCH_NO_AUTO_ARM=1` in the environment. It no-ops when no relay or agent
resolves, so wiring it globally is safe.

### Watching the relay

To watch messages flow by like an IRC channel, run the live tail in a spare
terminal:

```bash
bin/dispatch-tail              # follow new traffic from now on
bin/dispatch-tail --replay     # print what's already queued, then follow
bin/dispatch-tail --interval 0.5   # poll faster (default 1.0s)
bin/dispatch-tail --no-git     # local inboxes only, ignore the git bus
```

Each message prints once as a one-liner — `time  from → to  content` — with
flags for `must_read` (🔒), urgent (‼) and high (!) priority, the thread id, and
any structured `payload` type. A broadcast shows as a single line (its `to`
reads `all` or `#channel`), not one per recipient. When [cross-host
comms](#cross-host-comms-git-transport) are enabled it also reads the git bus
lanes, so the feed shows the whole cross-host bus — including traffic bound for
other hosts — with git-origin messages marked «remote». It's read-only — never
acks or deletes, and never fetches (the daemon owns that) — and finds the relay
from your config like the server does (override with `MCP_DISPATCH_DIR`).

For a point-in-time snapshot instead of a stream — who's live, their channel
subscriptions, and unread counts — run `bin/dispatch-status`.

#### dispatch-tui — an IRC client for the relay

For a full-screen view instead of a scrolling log, `tui/` is a
[Bubble Tea](https://github.com/charmbracelet/bubbletea) TUI that treats the
relay like an IRC server: live sessions are nicks, `#name` targets are channels,
and the feed is the message stream. Pick a nick or channel in the sidebar to
filter the feed; git-origin messages are marked «remote». It's the first Go
component in the repo.

```bash
bin/dispatch-tui                # builds on first run, then launches (needs Go 1.25+)
bin/dispatch-tui --nick alice   # send/ack as 'alice' (default: console-<pid>)
bin/dispatch-tui --no-git       # local inboxes only
bin/dispatch-tui --dump         # render one frame to stdout and exit (no TTY)
```

`bin/dispatch-tui` is a thin launcher that compiles the Go source in `tui/` on
first run (or when a source file changed) and execs it — so it starts like the
other `bin/` tools. To build the binary directly instead: `cd tui && make build`
(→ `./dispatch-tui`).

Keys: `tab`/`↑`/`↓` cycle the filter (the sidebar scrolls to keep the selection
in view) · `pgup`/`pgdn` scroll the feed · `f` toggle follow · `g`/`G`
top/bottom · **`i` compose a message to the selected nick/channel** (`enter`
sends, `esc` cancels) · **`a` ack your own inbox** · `q` quit. Sending writes the
same on-disk message a session would (byte-for-byte: fan-out for channels/`all`,
atomic write); a DM to a remote nick is bridged by the gitsync daemon like any
other. You send as your `--nick` (a lightweight console identity — it does not
hold presence, so agents don't see it as an always-on session).

## How It Works

- Each agent gets an inbox directory (`{dispatch_dir}/{agent_name}/`)
- Messages are JSON files written atomically (tmp + fsync + rename)
- Presence is held via an exclusive `flock` on a file in `{dispatch_dir}/.presence/`,
  so a crashed process's identity is freed by the kernel and two live processes
  can never claim the same agent id
- Channel subscriptions are stored in the presence record; a `#channel` send
  fans out to every live subscriber, so channels need no separate state and are
  cleaned up automatically when sessions exit
- Messages have states: `pending` → `read` → acknowledged (deleted)
- Piggyback delivery: pending messages are attached to every tool response
- TTL cleanup runs lazily on read operations
- Optional watchdog prints stderr alerts for the human operator

## Cross-host comms (git transport)

By default dispatch is local-host-only: a message to an agent on another machine
is written to a local inbox dir nobody reads. To reach agents on **other hosts**,
run the `dispatch-gitsync` daemon — it bridges this host's relay to a shared git
repo (per-author append-only JSONL lanes; durable, audited, conflict-free). The
message tool surface is untouched: agents keep calling `dispatch(target=...)` and
a message from another machine arrives as a normal inbox file, so it wakes a
parked session through the same path a local one does. `who()` also lists
cross-host agents under a `remote` key, and such messages arrive tagged
`via: "remote"`.

**Setup is one command per host** (`init` create-or-clones the repo, seeds it, and
writes the `[git]` config — no hand-editing TOML):

```bash
dispatch-gitsync init --create you/agent-bus   # gh-create a PRIVATE repo, then wire it
dispatch-gitsync init git@github.com:you/agent-bus.git   # join an existing bus
```

```bash
dispatch-gitsync            # run the daemon (one per host; holds a host lock)
dispatch-gitsync --once     # a single sync pass (smoke / cron)
dispatch-gitsync status     # running? repo, lane count, who's reachable
```

By default the daemon is **presence-gated** — it exits once no agent is live on
the host, so a hook-spawned daemon can't orphan. For hands-free operation inside
Claude Code, wire `hooks/dispatch-gitsync-arm.py` into `SessionStart` (alongside
the dispatch-wait arm hook) and it starts the daemon automatically whenever
`[git].enabled`:

```json
{ "type": "command", "command": "/abs/path/to/hooks/dispatch-gitsync-arm.py" }
```

#### Running it outside Claude Code (openclaw, Hermes, scripts, cron)

That hook is a *Claude Code* hook. On any other harness nothing starts the daemon,
so nothing is pushed or fetched and two agents appear to be **talking to a wall** —
the symptom is having to run `git pull`/`git push` by hand around every message.
Presence gating compounds it: presence is claimed by the mcp-dispatch **MCP
server**, so a hand-started daemon also self-terminates after ~60s if nothing else
on the host is holding a presence lock.

Put the daemon under your own init instead. **On a host that isn't running Claude
Code, this is the entire setup — one idempotent command, safe to re-run:**

```bash
dispatch-gitsync init git@github.com:you/agent-bus.git --service
```

It clones (or reuses) the bus, seeds it, writes the `[git]` config, then installs
and starts the service. Re-running it is also the upgrade path — an existing clone
is reused, an existing `[git]` block is left alone, and the unit is regenerated
from current config. Add `--dry-run` to see the plan first. If the bus is already
configured, just the second half:

```bash
dispatch-gitsync service install      # systemd user unit, enabled + started
python3 install.py --service          # ...or as part of the Claude Code installer
```

That writes `~/.config/systemd/user/mcp-dispatch-gitsync.service` running the
daemon with `--no-presence-gate`, restarted on failure and started at login.
Re-running it is also the **upgrade** path: the unit is regenerated from current
config and the running daemon restarted onto it.

```bash
dispatch-gitsync service show         # print the unit without installing anything
dispatch-gitsync service install --dry-run
dispatch-gitsync service uninstall
journalctl --user -u mcp-dispatch-gitsync -f    # watch it work
```

A user service inherits almost nothing from your login shell, so **git credentials
are the thing to check first**. Either use an HTTPS remote with a stored credential
helper, or pass the agent socket through at install time:

```bash
dispatch-gitsync service install --env SSH_AUTH_SOCK=$SSH_AUTH_SOCK
```

The service and the Claude Code hook coexist safely: both take the same host lock,
so only one daemon ever mirrors a relay. Without systemd, run the daemon under
whatever supervisor you do have — the only thing that matters is the flag:

```bash
dispatch-gitsync --no-presence-gate
```

or set `presence_gate = false` under `[git]` to make that the default for every
launcher on the host.

`mirror = "remote-only"` (default) only bridges messages with no live-local
recipient — same-host chatter stays local and private; `mirror = "all"` makes a
full audited cross-host replica.

**Testing cross-host with only one machine.** You don't need a second box to
verify the whole path. `scripts/loopback-smoke.py` simulates two hosts on one
machine — two throwaway DISPATCH_DIRs and two clones of the same bus repo — and
round-trips a DM between them over a **real** git remote, asserting both
directions land tagged `via: "remote"`:

```bash
scripts/loopback-smoke.py                    # against the default bus repo
scripts/loopback-smoke.py --repo you/agent-bus --keep
```

It never touches your live config or real relay (everything lives in a temp dir),
so it's safe to run repeatedly.

The git **wire format** (an 11-field JSONL envelope + lane layout) is the
cross-language contract — see [`docs/git-transport.md`](docs/git-transport.md).
The canonical Go implementation is [`leat`](https://github.com/justinstimatze/leat).
Cross-host **channel** membership currently relies on a local subscriber to carry
a post onto git (shared presence across hosts is a later step); **DMs** bridge
unconditionally.

### Cross-host agents can't hear each other?

Almost always the git daemon isn't running on one host — messages get written to
a local inbox nobody bridges, so both sides look like they're talking to a wall.
Check it:

```bash
bin/dispatch-gitsync status     # running? repo, lane count, who's reachable
bin/dispatch-gitsync --once     # force one sync pass and print what moved
```

The daemon is presence-gated (it exits when no agent is live on the host), so it
must be **restarted** each time the host goes quiet and comes back. That restart
is what the `dispatch-gitsync-arm.py` SessionStart/Stop hook automates — if you
wired the repo up before `install.py` existed, re-run `python3 install.py` to add
it, then start a fresh session. Without the hook you'd have to relaunch the daemon
by hand after every quiet period, which is the usual cause of this.

## Security

This is local-host-only IPC; the threat model is other local users on a shared machine.

- The dispatch directory and `.presence` are created `0700`; the server sets
  `umask 0o077` so message files are `0600` (owner-only). Other users on the box
  cannot read your inter-agent messages.
- Agent ids and targets must match `^[a-z0-9][a-z0-9_-]{0,63}$`. They become path
  segments, so anything with separators or traversal sequences (`../…`) is rejected
  rather than allowed to escape the dispatch directory.
- No network listener, no daemon, no encryption at rest (out of scope for the
  local-only threat model).
- The optional [git transport](#cross-host-comms-git-transport) doesn't open a
  listener either — it pushes/pulls a git remote you control. Its confidentiality
  is your repo's (use a **private** repo); message bodies are cleartext in the
  lanes today (a per-line encryption seam exists but ships off). Don't enable it
  against a remote you wouldn't trust with the message contents.

### Sharing a relay across accounts

By default each user has a separate, owner-only relay. To let several
mutually-trusting accounts (e.g. multiple accounts belonging to one person)
share a single relay, set `group_mode = true` and point `dispatch_dir` at a
shared, group-owned, setgid directory whose group every participant belongs to.
In that mode the relay and messages are group-readable/writable (`2770` / `0660`)
— so anyone in the group can read the group's traffic. Only enable it when the
accounts trust each other; the default stays owner-only. See `config.example.toml`.

## Message Format

```json
{
  "id": "msg-a1b2c3d4",
  "from": "alice",
  "to": "bob",
  "timestamp": "2026-02-17T20:30:00Z",
  "priority": "normal",
  "content": "Deployed to staging",
  "payload": {"commit": "abc123"},
  "thread_id": "deploy-123",
  "reply_to": null,
  "ttl": 3600,
  "must_read": false,
  "state": "pending"
}
```

## License

MIT — see [LICENSE](LICENSE).
