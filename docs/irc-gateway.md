# The IRC gateway (`dispatch-ircd`)

Point any IRC client at the relay and it reads like a small network: agents are
nicks, relay channels are channels, `&dispatch` is a firehose of everything
crossing the bus.

The reason to do this is reach, not taste. IRC buys every desktop client
(irssi, WeeChat, Halloy, Textual), and — the part that actually matters — a
bouncer in front (soju, ZNC) buys scrollback and push notifications to a phone.
None of that is code this repo has to write, maintain, or design.

It also, for the first time, makes mcp-dispatch accept a connection. Read
[Security](#security) before enabling it. The short version: it is off unless
you turn it on in the config file, it speaks over a `0600` unix socket by
default, the kernel checks the peer's uid, a token is required on every
transport, TLS is required on every TCP listener it binds — loopback included —
and reaching a public address takes a second, separate key.

## Quick start

```bash
bin/dispatch-ircd --init-token     # generate the shared secret (0600), once
bin/dispatch-ircd --init-tls       # ...and a certificate, if you want TCP
```

Then turn it on — there is deliberately no flag that does this:

```toml
# ~/.config/mcp-dispatch/config.toml
[irc]
enabled = true
```

```bash
bin/dispatch-ircd --check          # validate config, print what would be served
bin/dispatch-ircd                  # run
```

Connect over the unix socket (no network involved at all):

```bash
# WeeChat, over the unix socket — no TLS involved, and none needed
/server add dispatch unix:///home/you/.config/mcp-dispatch/irc.sock
/set irc.server.dispatch.password <token>
/connect dispatch
```

For clients that only speak TCP, add a listener — which **must** be TLS:

```toml
[irc]
listen   = "127.0.0.1:6697"
tls_cert = "~/.config/mcp-dispatch/irc-cert.pem"
tls_key  = "~/.config/mcp-dispatch/irc-key.pem"
```

```bash
/connect -tls -tls_cert_fp <fingerprint> dispatch 127.0.0.1 6697 <token>
```

`--init-tls` and every startup print the certificate's SHA-256 fingerprint. A
self-signed certificate is trusted by **pinning**, not by a CA, so put that
fingerprint in your client (`tls_cert_fp` in irssi, `ssl_fingerprint` in
WeeChat) rather than turning verification off. Pinning is the stronger of the
two anyway — it notices a swapped certificate, which a public CA would not.

The token goes in the server-password field, or as the SASL PLAIN password if
your client prefers that (`sasl` is advertised; only PLAIN is supported).

### Turn on `server-time`

Worth doing before anything else, because JOIN replays history:

```
# WeeChat — on by default in recent versions, but confirm
/set irc.server_default.capabilities "server-time,message-tags,sasl"
```

The gateway advertises three IRCv3 capabilities and implements exactly those:

| cap | what it buys |
|---|---|
| `server-time` | each message shows the time it **crossed the relay**, not the time your client received it |
| `message-tags` | carries `msgid`, which is the id `/msg dispatch ack <id>` takes |
| `sasl` | PLAIN, if you prefer it to a server password |

Without `server-time`, joining a channel dumps fifty messages that all appear to
have arrived just now — you cannot tell a week-old decision from a live one,
which is worse than having no history. `CAP REQ` is honoured atomically: a
request naming anything outside that table is refused whole rather than
half-granted, so your client never formats for a capability we won't send.

## What you get

| IRC | relay |
|---|---|
| nick | an agent, by its **stable** id — `publicai`, not `publicai-1767991` |
| `#eng` | the relay channel `#eng` |
| `&dispatch` | every message crossing the relay, read-only |
| `PRIVMSG #eng :…` | `dispatch(target="#eng")` — fans out to live subscribers |
| `PRIVMSG alice :…` | a DM to the nick — see [durable identity](../README.md#durable-identity-nicks-that-outlive-a-session) |
| `NAMES` / `WHO` / `LIST` | the presence roster and known channels |
| `/msg dispatch …` | inbox, ack, who, tasks — see below |

Messages carry the same markers the TUI uses: `🔒` must_read, `‼` urgent, `!`
high, `«remote»` for anything that arrived over the git bus, `[thread-id]` when
threaded. A body with newlines becomes several PRIVMSGs, because IRC has no
multi-line message; long lines are split on word boundaries, never truncated.

### The service nick

IRC has no verb for "acknowledge this message", so the things the protocol
can't express live behind a `dispatch` nick you `/msg`:

```
/msg dispatch help                    what follows
/msg dispatch who                     the roster, live / remote, with channels
/msg dispatch inbox                   what is waiting for you, with message ids
/msg dispatch ack <id> [<id>…]        acknowledge those messages
/msg dispatch ack all                 acknowledge your whole inbox
/msg dispatch channels                list relay channels
/msg dispatch replay 100              re-send recent history into &dispatch
/msg dispatch urgent bob :look now    send at urgent priority
/msg dispatch tasks [state]           the task board — read-only here
```

`inbox` is the one to reach for first: it lists what is actually addressed to
you, with the id and read state of each, so `ack` can be aimed. A bare `ack`
with no argument does nothing but print usage — acknowledging is destructive
(the MCP server deletes an acked message), and that is not a sensible default
for a command you might type to clear a single notification. Say `ack all` when
you mean all.

`dispatch` is reserved as a nick; an agent literally named `dispatch` would be
shadowed by the service.

### What it is not

The gateway **does not claim presence**. It is an observer and a sender,
exactly like `dispatch-tui`: agents never see your IRC client as a live
session, `who()` will not list you, and a channel fan-out does not reach you
because you are not a subscriber — you see channel traffic because the gateway
reads *every* inbox on the relay, not because you are a member.

The practical consequence: you can read everything and send anything, but you
are not addressable as an agent. A DM to your nick still works — it lands in an
inbox directory named after your nick and the gateway delivers it — but nothing
on the relay knows you are there until you speak.

Sending follows the relay's [durable identity](../README.md#durable-identity-nicks-that-outlive-a-session)
rule, which the gateway does not reimplement: `/msg publicai` reaches every live
session of that teammate, or waits in the nick's inbox for its next one if none
is running. `/msg publicai-1767991` addresses that one window and never fans out.

Presence semantics stay owned by the MCP server alone. That is deliberate:
duplicating the flock protocol in a second implementation is exactly how two
processes end up disagreeing about who is live.

## Security

The relay's threat model is *other local users on a shared machine*: it is
owner-only IPC (`0700` directories, `0600` messages) with no listener. A gateway
changes that, so every widening of exposure past the default is its own
explicit key, and the defaults are all at the safe end.

**Enabling it is a config-file edit, not a flag.** `enabled = true` has no
command-line equivalent. A listener should not be able to appear because of a
typo in a shell history or a copy-pasted command.

**The default transport is a unix socket**, created inside a `0177` umask and
then explicitly `chmod 600`. There is no TCP port unless you configure one, so
the README's "no network listener" property survives the default build.

**The kernel checks the peer.** On the unix socket, `SO_PEERCRED` gives us the
connecting process's uid, recorded at `connect()` time and unforgeable by the
client. A uid that isn't ours is refused before a byte is read — this holds even
if the token has leaked. If the credential can't be read at all, the connection
is refused rather than served.

**A token is required on every transport, including the unix socket.** Peer
credentials stop other *users*; the token stops other *processes running as
you*. It must be at least 32 characters and mode `0600`; the gateway refuses to
start on a group- or world-readable token file and tells you how to fix it.
Comparison is constant-time over SHA-256 digests, so neither the value nor its
length leaks through timing.

**Wrong tokens are counted and then banned.** Five failures from one source
(dropping the port, so reconnecting doesn't reset the counter) lock it out for
five minutes; the counter keeps climbing while banned, so persistence extends
the lockout. A failed authentication also ends the connection. A loopback port
is not an offline guessing oracle.

**Nothing is served before authentication.** Pre-registration, a connection may
only negotiate capabilities, present a token, and set a nick. `JOIN`, `PRIVMSG`,
`NAMES`, `WHO` and `LIST` all answer `451` — no roster, no channel list, no
traffic, not even the shape of the relay.

**Every TCP listener is encrypted — loopback included.** There is no cleartext
TCP mode at any address. Loopback traffic is not private: anything on the host
that can capture `lo` reads the token, and the token is equivalent to read/write
on every conversation on the relay. TLS 1.3 is the default floor; `1.2` exists
for a client too old to speak it, and nothing below that is reachable through
the knob. The unix socket is the supported way to have an unencrypted
transport, which is exactly why it is the default.

**Exposure is a separate axis from encryption.** Binding a non-loopback address
additionally needs `allow_remote = true`. TLS does not make a public bind
automatic and `allow_remote` never buys cleartext — the two refusals are
independent. A wildcard bind (`0.0.0.0`, `::`, `:6697`) counts as public; that
is the case most likely to be typed by accident, so it fails the check rather
than sliding through it.

**Mutual TLS, if you want it.** Set `tls_client_ca` and clients must present a
certificate signed by that CA (`RequireAndVerifyClientCert`). It is an
*additional* gate: the token is still required. The conventional IRC move is to
let a certificate stand in for the password (SASL EXTERNAL / CertFP), and we
deliberately don't — that is a second authentication path with its own bugs,
and requiring both costs a client nothing it isn't already configured for. An
unreadable or unusable CA file fails at startup rather than silently leaving
mTLS off.

**Nicks are relay-safe.** A nick becomes a path segment on the relay, so it must
match `^[a-z0-9][a-z0-9_-]{0,63}$` — the same rule the Python server enforces.
`../../etc` is answered with `432`, not a directory.

**Content cannot forge protocol.** CR and LF are stripped from every outbound
line, so a message body can't inject an IRC command into a client's stream.

**Resources are bounded.** Inbound lines are capped (8 KiB), so are concurrent
connections (8), the per-client outbound queue (512 lines — a client that won't
drain is disconnected rather than allowed to stall the poller serving everyone),
and unauthenticated connections (10 seconds to authenticate, then dropped).

An idle *authenticated* connection is not dropped for being quiet: the gateway
PINGs it at half the idle timeout (floored at 15s), and the client's PONG is the
traffic that keeps it alive. A client that stops answering still hits the
deadline, which is how a dead connection gets reaped rather than held open.

**Logs carry metadata only** — connect, authenticate, register, disconnect, with
an address and a nick. Message content is never logged.

**One gateway per relay**, enforced by the same `flock` the relay uses for agent
presence, so a crashed gateway's lock is released by the kernel.

### What it does not protect against

- **A leaked token on a TCP listener.** Peer credentials only exist on the unix
  socket. TLS protects the token in transit but nothing protects it once copied;
  on TCP it is the only thing between a local process (or, with `allow_remote`,
  the network) and every conversation on the relay. Prefer the socket, and reach
  for `tls_client_ca` if you can't.
- **A bouncer you don't control.** A bouncer is a second holder of your token
  and a second copy of your scrollback, so it inherits the whole threat model.
  Connect it to the gateway over the unix socket if it runs on the same host, or
  over TLS with the fingerprint pinned if it doesn't — and do not point it at a
  hosted ZNC, which means handing that operator your agents' traffic in
  readable form. `soju` on the same machine, over the socket, is the shape that
  keeps the guarantees above intact.
- **Anything at rest.** Messages are cleartext on disk, as they always were.
- **A compromised account.** The gateway runs as you and reads what you can read.

## Configuration

Every key, with its default, lives in [`config.example.toml`](../config.example.toml)
under `[irc]`. The ones worth knowing:

| key | default | what it does |
|---|---|---|
| `enabled` | `false` | the master switch; no flag equivalent |
| `socket` | `~/.config/mcp-dispatch/irc.sock` | unix transport, `0600`, uid-checked |
| `listen` | *(unset)* | optional TCP `host:port` |
| `tls_cert` / `tls_key` | *(unset)* | **required for any TCP listener** |
| `tls_min_version` | `1.3` | version floor; `1.2` for an old client |
| `tls_client_ca` | *(unset)* | mutual TLS — an extra gate, not a token replacement |
| `allow_remote` | `false` | required for a non-loopback bind (exposure only) |
| `token_file` | `~/.config/mcp-dispatch/irc-token` | must be `0600`, ≥32 chars |
| `max_conns` | `8` | concurrent clients |
| `auth_timeout` | `10` | seconds to authenticate before disconnect |
| `max_auth_failures` / `ban_seconds` | `5` / `300` | brute-force backoff |
| `interval` | `1.0` | relay poll seconds |
| `history` | `50` | messages replayed into a channel on JOIN |
| `read_git` | `true` | also read the cross-host git lanes |

## Running it

The gateway is a foreground process that exits on SIGINT/SIGTERM. Put it under
whatever supervisor you already run — the same `systemd --user` pattern
`dispatch-gitsync service install` uses works here, and the same
[credential](../README.md#credentials) and
[lingering](../README.md#headless-hosts-enable-lingering) caveats apply if you
want it up without a login session.

It polls; it does not hold presence and does not gate on it. Running with no
live agents is fine — you see an empty roster and any traffic that arrives.
