# mcp-dispatch

Local inter-agent messaging for AI coding agents via [MCP](https://modelcontextprotocol.io/).

Multiple Claude Code sessions (or any MCP-compatible agents) running on the same machine can send messages to each other through a shared filesystem relay. No server process, no ports, no network — just directories and JSON files with atomic writes.

## Features

- **Non-destructive messaging** — Messages persist until explicitly acknowledged. No more lost messages from crashes or compaction.
- **Channels** — `subscribe('#name')` and `dispatch(target='#name')` fan a message out to current subscribers. Ephemeral — subscriptions vanish when a session exits.
- **Threading** — Group messages into conversations with `thread_id` and `reply_to`.
- **Structured payloads** — Attach machine-readable data alongside human-readable messages.
- **TTL & must_read** — Time-sensitive messages auto-expire. Critical messages survive until acknowledged.
- **Delivery receipts** — `peek()` shows read/unread state of messages you've sent.
- **`$PWD`-derived identity** — `bin/dispatch-launcher` gives each session a `<project>-<pid>` id with no per-window config.
- **Live tail** — `bin/dispatch-tail` streams every message across the relay to a terminal, IRC-style, so you can watch sessions talk in real time.
- **Wake on arrival** — `bin/dispatch-wait` blocks until a message matching the notify policy lands, then exits to wake a parked model — an event-driven replacement for polling with `/loop`.
- **Config-driven** — TOML config for agent rosters, directories, and limits. Or go dynamic with no roster.
- **Zero infrastructure** — Filesystem relay survives process crashes. No daemon to manage.
- **Local-only & per-user** — `0700`/`0600` perms, validated ids, no network. See [Security](#security).

## Quick Start

### 1. Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/sophia-labs/mcp-dispatch.git
cd mcp-dispatch
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

A session started in `~/Documents/gemot` then becomes agent `gemot-<pid>`. An
explicit `MCP_DISPATCH_AGENT_ID` always wins.

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

The response includes `delivered_to` — the list of agents the message actually
reached (empty for a channel with no current subscribers).

### channels

```python
subscribe("#deploys")              # join (with or without the leading '#')
dispatch("staging is up", "#deploys")   # fan out to current subscribers
unsubscribe("#deploys")            # leave
```

Channels are presence-derived and ephemeral: a subscription lives only as long
as the session, and a channel send reaches whoever is subscribed *right now*.

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
(addressed to this agent), `important` (urgent priority), `all`, or `none`;
`must_read` always pierces except under `none`. Meanwhile `must_read` guarantees
the message itself waits until that session next takes a turn and acks it. See
`config.example.toml`.

### Waking a parked session (`dispatch-wait`)

A desktop notification alerts *you*, but the model still won't act until its
next turn. To wake the **model** on arrival without burning turns on a timer,
launch `bin/dispatch-wait` as a background task. It blocks until a message that
matches `notify_on` lands in this agent's inbox, prints a summary, and exits —
and Claude Code re-invokes the model when a backgrounded task exits. Handle the
message, then launch `dispatch-wait` again to re-arm.

A waiter holds a per-agent lock for its lifetime, so launching a second
`dispatch-wait` for the same agent exits immediately rather than double-arming.

```bash
dispatch-wait                  # block until a notify_on-qualifying message lands
dispatch-wait --notify-on direct   # wake only on messages addressed to me
dispatch-wait --interval 1     # poll seconds (default 2.0)
dispatch-wait --max-lifetime 600   # add a wall-clock cap (default 0 = none)
```

By default there's no time cap: the waiter exits the instant its agent's presence
flock drops (the session's server died), so a backgrounded waiter can't outlive
its session and orphan — independent of whether the harness reaps background
tasks on close. `--max-lifetime` adds a wall-clock cap on top; standalone use
with no live server to gate on falls back to a finite cap automatically.

This replaces `/loop` for staying responsive while idle: `/loop` fires a whole
model turn every interval whether or not anything arrived; `dispatch-wait`
spends **zero** model tokens while blocking and wakes only on a real qualifying
arrival. It reads the same `notify_on` policy the desktop notifier uses (one
source of truth, `notify_policy.py`), so the two paths never disagree. It is
level-triggered — a message already unread at launch exits it immediately, so
re-arming after handling one leaves no window to miss the next. Resolves
identity from `$MCP_DISPATCH_AGENT_ID` (set it, for a dozen sessions especially).

#### Arming it hands-free (`hooks/dispatch-arm.py`)

Launching and re-launching `dispatch-wait` by hand is the manual step. Only the
model can start the wake task (the harness wakes on a *model-launched*
`run_in_background` task, not on an arbitrary process), but a hook can make the
model do it. `hooks/dispatch-arm.py`, wired into **SessionStart and Stop**,
checks whether a waiter holds the arm lock; if none does, it tells the model to
launch one. On SessionStart it injects the instruction; on Stop it *blocks*
(capped, so a failing launch can't wedge the session) so the model never parks
unarmed. Once a waiter is armed the hook is silent. Net: the session arms itself
at startup and re-arms after every wake, with no human in the loop.

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
```

Each message prints once as a one-liner — `time  from → to  content` — with
flags for `must_read` (🔒), urgent (‼) and high (!) priority, the thread id, and
any structured `payload` type. A broadcast shows as a single line (its `to`
reads `all` or `#channel`), not one per recipient. It's read-only — it never
acks or deletes — and finds the relay from your config like the server does
(override with `MCP_DISPATCH_DIR`).

For a point-in-time snapshot instead of a stream — who's live, their channel
subscriptions, and unread counts — run `bin/dispatch-status`.

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

The daemon is **presence-gated** — it exits once no agent is live on the host, so
it can't orphan. For hands-free operation, wire `hooks/dispatch-gitsync-arm.py`
into `SessionStart` (alongside the dispatch-wait arm hook) and it starts the daemon
automatically whenever `[git].enabled`:

```json
{ "type": "command", "command": "/abs/path/to/hooks/dispatch-gitsync-arm.py" }
```

`mirror = "remote-only"` (default) only bridges messages with no live-local
recipient — same-host chatter stays local and private; `mirror = "all"` makes a
full audited cross-host replica.

The git **wire format** (an 11-field JSONL envelope + lane layout) is the
cross-language contract — see [`docs/git-transport.md`](docs/git-transport.md).
The canonical Go implementation is [`leat`](https://github.com/justinstimatze/leat).
Cross-host **channel** membership currently relies on a local subscriber to carry
a post onto git (shared presence across hosts is a later step); **DMs** bridge
unconditionally.

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
