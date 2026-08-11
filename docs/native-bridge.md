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

### Running it hands-free

Two options, mirroring `dispatch-gitsync` exactly:

- **Claude Code:** wire `hooks/dispatch-ucbridge-arm.py` into `SessionStart`
  (`install.py` does this for you, unconditionally — it's a no-op unless
  `[bridge].enabled` is set). It spawns the daemon detached on session start,
  gated on a host-level lock so a redundant spawn exits immediately.
- **Any other harness** (openclaw, Hermes, a script, cron): `bin/dispatch-ucbridge
  service install` runs it as a systemd **user** service — started at login,
  restarted on failure, independent of any agent runtime. `service uninstall` /
  `service show` are the other two actions.

By default the daemon is **presence-gated**: it exits once no dispatch agent is
live on the host, so a hook-spawned daemon can't orphan. `service install`
passes `--no-presence-gate` for you; the equivalent by hand is
`--no-presence-gate` or `[bridge] presence_gate = false`.

Any Claude Code session on the host can now open a `SendMessage`/native-tool
call addressed to `publicai` and land a message in that nick's dispatch inbox
— and `dispatch(target="publicai", ...)` reaches `publicai` back over the
native socket too, on the occasions dispatch itself has no live local delivery
path to it (its only live session right now is the native one, not a dispatch
session).

There is deliberately no wildcard, in **either** direction. A nick with no
entry in `nicks` gets no socket, no registry entry, and is never a candidate
for outbound delivery, whatever it is sent — the same allowlist-only posture
as `[supervisor]`. Bridging `publicai` does not make `carol`'s pending mail
reachable by anyone who registers a native session named `carol`, even though
both live in the same `dispatch_dir`; the outbound scan only ever reads a
bridged nick's own inbox.

## How it works

**Outbound** (`dispatch(target=X)` → native socket, `X` a bridged nick): each
tick, the bridge scans only the bridged nicks' own inboxes for messages with
no live *local* dispatch presence for their recipient. If the recipient (which
by construction of the scan is always one of `nicks`) matches a currently-live
entry in the native session registry, the message is wrapped as a
`<cross-session-message from="dispatch:…" from-name="…" from-mode="dispatch">`
envelope (the same convention the spec says a compliant sender uses, with
`from`/content escaped so the message's own text can't forge a second,
differently-attributed block) and written once to that session's socket.
Delivery there has no receipt — the protocol returns nothing on the sending
socket — so "sent" means only that the write succeeded, mirroring how
`git_bridge.py` treats a push to a frozen remote. Already-attempted message ids
are ledgered (`.native/ucbridge-outbound.json`) so a dead or slow peer isn't
retried every tick, and — mirroring `git_bridge.py`'s identical first-run
guard — messages already pending when the bridge is first enabled are seeded
into that ledger unsent, so turning it on means "bridge from now on," not
"dump the backlog." Broadcasts (`to = "all"`) and channel posts (`to = "#…"`)
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
permissions, not the primary gate. Each valid NDJSON `type: "user"` line is
materialized into that nick's dispatch inbox as an ordinary message — see
[Threat model](#threat-model) for exactly what "materialized" does and does not
mean for trust. A `type: "control"` line (`rename`, `peer_message_status`
delivery receipts) is recognized and counted (`NativeInboundListener
.control_seen`, surfaced in `status`) but never acted on — see
[Control messages](#control-messages-rename--delivery-receipts) below.

**who() visibility:** every tick, the daemon also live-probes the native
session registry and writes what it finds to `DISPATCH_DIR/.native/` — read-only
from `server.py`'s side, exactly like `git_bridge.py`'s `.remote/` roster keeps
`who()` git-agnostic. `who()` then shows a `native` key: every OTHER live
native session on the host (bridged nicks' own listeners are excluded — they're
already visible as ordinary dispatch agents). This is independent of outbound
send traffic and independent of whether that other session is itself bridged;
it's informational reachability, the native-bus equivalent of `remote`.

## Control messages (`rename` / delivery receipts)

The spec defines two `type: "control"` messages: `rename` (change the
receiving session's registered name) and `peer_message_status` (delivery
receipts — held, denied, expired, delivered). Neither is acted on, deliberately:

- **`rename`** would let whatever peer can reach the socket change how this
  bridge's registered identity resolves. The bridge's identity is the
  operator's `[bridge] nicks` config, not something a connecting peer gets to
  negotiate — honoring a rename request from the wire would hand that control
  to an untrusted sender.
- **`peer_message_status`** (receipts) has no consumer here: `send_native` is
  intentionally fire-and-forget (see its docstring), and this bridge doesn't
  keep a sent-message log to correlate a receipt against. The reference
  implementation this bridge is built from is in the same position — the spec
  itself notes receipts exist on the wire but nothing there acts on
  hold/denial notifications either.

Both are still recognized (not silently indistinguishable from malformed
input) and counted for observability.

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
- DOES carry a self-reported `priority`: the native envelope's own
  `"now"`/`"next"` maps to dispatch's `"urgent"`/`"normal"`. This is not a
  parallel escalation path to `must_read` — every dispatch sender could
  already claim `priority="urgent"` for free, with no validation, so this is
  the same pre-existing, already-untrusted signal, not a new one. Without the
  mapping, a bridged nick could never be woken by native-bus traffic at all
  under the default `notify_on = "important"`, defeating the point of bridging
  it; `must_read` remains the one channel actually gated behind `trust_wake`.
- Is never routed to task creation or claiming. Bridged content is a message
  like any other; nothing about arriving over this path grants it the ability
  to act on the task board.
- On the way OUT, `from`/`content` are HTML-escaped before being spliced into
  the `<cross-session-message>` wrapper, so a dispatch message's own content
  can't close that element early and forge a second one claiming a different
  `from-name` to the native peer reading it.

What is **not** at additional risk: other users' messages (still `0600`/`0700`
owner-only), cross-host traffic (gated separately by the git bus's own repo
ACLs), and any host running a single session with no adversarial input in its
loop — for that case this whole bridge is optional and the risk it's built
around doesn't apply.

## What this deliberately does not do

- **Act on `rename` or delivery receipts.** See
  [Control messages](#control-messages-rename--delivery-receipts) above —
  both are recognized, neither is safe or useful to honor here.
- **Correlate outbound sends to receipts.** `send_native` stays fire-and-forget
  by design; adding a sent-message log purely to watch receipts nobody acts on
  would be complexity with no payoff — same call the spec's own reference
  implementation makes.
- **Show `remote`-style staleness for native sessions.** Unlike a git-lane
  entry (durable — an agent can be hours offline and still listed, flagged
  `stale`), a dead native session has no history to remain reachable through:
  `.native/` entries are dropped the tick they stop probing live, not marked
  stale.
