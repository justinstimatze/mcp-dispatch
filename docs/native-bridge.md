# The native-protocol bridge (`dispatch-ucbridge`)

Claude Code ships its own inter-session messaging, independent of mcp-dispatch:
a fire-and-forget, newline-delimited JSON protocol over Unix sockets in
`/tmp/cc-socks/`, with sessions discovered via a `~/.claude/sessions/<pid>.json`
registry. Every Claude Code session speaks it with zero setup. The reference
description used to build this bridge is
[chrismo/claude-rig — inter-claude-protocol.md](https://github.com/chrismo/claude-rig/blob/main/docs/inter-claude-protocol.md).

`dispatch-ucbridge` bridges an explicit allowlist of dispatch nicks onto that
bus. The reason to do this is reach, same as the [IRC gateway](irc-gateway.md):
it lets *any* local Claude Code session address a dispatch nick by name — not
just sessions that have the `dispatch` MCP server wired in — without changing
how `dispatch()` is called on either side.

**Read the whole of [Threat model](#threat-model) before enabling this.** The
native protocol's own spec says sender attribution on that wire is composed
text, not a verified fact. This bridge is built around that fact, not in spite
of it: every message it delivers is provenance-tagged and never granted the
trust a normal local dispatch message gets.

## Quick start

```toml
# ~/.config/mcp-dispatch/config.toml
[bridge]
enabled = true
nicks = ["publicai"]     # allowlist — nothing else in your fleet is exposed
```

```bash
bin/dispatch-ucbridge --check       # validate config, print what would bridge
bin/dispatch-ucbridge --once        # one outbound pass and exit (smoke test)
bin/dispatch-ucbridge status        # configured nicks + visible native sessions
bin/dispatch-ucbridge               # run the daemon
```

Any Claude Code session on the host can now open a `SendMessage`/native-tool
call addressed to `publicai` and land a message in that nick's dispatch inbox
— and `dispatch(target="<some native session's name>", ...)` from any bridged
session reaches it back over the native socket if dispatch itself has no live
local delivery path to that name.

There is deliberately no wildcard. A nick with no entry in `nicks` gets no
socket, no registry entry, and no outbound delivery attempt — the same
allowlist-only posture as `[supervisor]`.

## How it works

**Outbound** (`dispatch(target=X)` → native socket): each tick, the bridge
scans every local inbox for messages whose recipient `X` has no live *local*
dispatch presence. If `X` matches a currently-live entry in the native session
registry, the message is wrapped as a `<cross-session-message from="dispatch:…"
from-name="…" from-mode="dispatch">` envelope (the same convention the spec
says a compliant sender uses) and written once to that session's socket.
Delivery there has no receipt — the protocol returns nothing on the sending
socket — so "sent" means only that the write succeeded, mirroring how
`git_bridge.py` treats a push to a frozen remote. Already-attempted message ids
are ledgered (`.native/ucbridge-outbound.json`) so a dead or slow peer isn't
retried every tick. Broadcasts (`to = "all"`) and channel posts (`to = "#…"`)
have no native equivalent and are never bridged, same as the git transport's
DM-only outbound scope.

**Inbound** (native socket → dispatch inbox): for each bridged nick, the
daemon opens one Unix socket (`/tmp/cc-socks/peer-dispatch-<nick>.sock`, `0600`,
directory `0700`) and registers one `~/.claude/sessions/dispatch-bridge-<nick>.json`
entry, so the harness's own roster-building logic finds it exactly like a real
session. A connecting peer's kernel-reported uid is checked against ours
(`SO_PEERCRED`, unforgeable, fail-closed if unavailable — the same check
`dispatch-ircd`'s gateway makes, see `tui/ircd/auth.go`) before a single byte
is read; this is defense in depth on top of the `0700`/`0600` filesystem
permissions, not the primary gate. Each valid NDJSON line is materialized into
that nick's dispatch inbox as an ordinary message — see
[Threat model](#threat-model) for exactly what "materialized" does and does not
mean for trust.

## Threat model

The Unix socket's `0700` directory and `0600` file permissions rule out
cross-account forgery: a different OS user on a shared box cannot open
`/tmp/cc-socks/*.sock` at all. That is a real boundary, but it is not the one
that matters here.

**The actual risk is another process running as *you*.** In this ecosystem
that is not a hypothetical: mcp-dispatch exists to run a fleet of agents on one
host, some of which are handling untrusted external content (a fetched
webpage, a PR comment, an issue body) and are therefore susceptible to prompt
injection. A compromised sibling session doesn't need elevated privilege to
abuse this bridge — it needs only whatever tool access it already has (a shell,
an MCP tool that opens a socket) to connect to a bridged nick's socket and
write a crafted envelope with a spoofed `from-name`. Per the spec, that wrapper
is composed client-side into `message.content`; there is nothing at the
transport layer asserting who really sent it, because the receiving harness
never surfaces the envelope's own `from` field to the model at all.

This is exactly the risk category `[supervisor]` already treats as first-class
elsewhere in this repo — see `supervisor.py`'s argv/env allowlisting and
`bin/dispatch-agent-claude`'s tool-stripped default — just applied to an inbound
message instead of a process launch. So this bridge answers it the same way
`git_bridge.py` answers a remote host's messages: **tag provenance, never
trust identity.**

Concretely, everything that arrives over this bridge:

- Is attributed to the fixed placeholder `from: "native-bridge"` — **never**
  to whatever the envelope or wrapper claims. The claimed identity is kept,
  read-only, under `payload.claimed_from` for a human to weigh; nothing
  downstream (task claiming, `who()`, any trust decision) consumes it.
- Is exposed to `peek()`/piggyback delivery with `via: "native-bridge"`, so a
  reading agent knows at a glance this one crossed an untrusted boundary — the
  same signal `via: "remote"` gives for git-transport messages.
- Can never carry `must_read` from the wire (the native protocol has no such
  field, and nothing here synthesizes one) — but it *can* be marked
  `must_read` by a malicious sender's content trying to trigger the escalation
  path anyway. By default, `notify_policy.should_notify` refuses to let a
  `_via: "native-bridge"` message force a wake purely by claiming
  `must_read=true`: it still notifies under `notify_on = "all"` / `"direct"` /
  `"important"` exactly like any other message, it just can't pierce a policy
  that would otherwise stay silent. Set `[bridge] trust_wake = true` only if
  you have a reason to trust everything that can reach this socket as much as
  you trust dispatch's own local relay.
- Is never routed to task creation or claiming. Bridged content is a message
  like any other; nothing about arriving over this path grants it the ability
  to act on the task board.

What is **not** at additional risk: other users' messages (still `0600`/`0700`
owner-only), cross-host traffic (gated separately by the git bus's own repo
ACLs), and any host running a single session with no adversarial input in its
loop — for that case this whole bridge is optional and the risk it's built
around doesn't apply.

## What this does not do (yet)

- No `who()` integration. Reachable-but-unbridged native sessions don't
  currently show up as a `native` key the way cross-host agents show up under
  `remote` — `list_native_sessions()` in `bridge_native.py` has everything
  needed for that; it just isn't wired into `who()` yet.
- No control-message handling (`rename`, delivery receipts) — cosmetic to
  dispatch's own model, and skipped until something needs them.
- No systemd service installer or `SessionStart` arm hook, unlike
  `dispatch-gitsync`/`dispatch-ircd`. Run it under whatever supervises your
  other long-lived processes; `bin/dispatch-ucbridge` exits cleanly on
  SIGINT/SIGTERM like any well-behaved daemon.
