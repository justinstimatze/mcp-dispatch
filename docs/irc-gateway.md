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

For clients that only speak TCP, add a listener — which **must** be TLS. Name
the `socket` explicitly at the same time: the socket default applies only while
`listen` is unset, so adding TCP alone takes away the transport this document
otherwise tells you to prefer.

```toml
[irc]
listen   = "127.0.0.1:6697"
tls_cert = "~/.config/mcp-dispatch/irc-cert.pem"
tls_key  = "~/.config/mcp-dispatch/irc-key.pem"
socket   = "~/.config/mcp-dispatch/irc.sock"   # else TCP is the ONLY transport
```

```bash
# irssi — the fingerprint goes in COLON-SEPARATED form, exactly as printed
/connect -tls -tls_pinned_cert FE:08:C7:…:11 127.0.0.1 6697 <token>
```

`--init-tls` and every startup print the SHA-256 fingerprint of the **leaf** —
the certificate the gateway actually serves. No public CA can vouch for "the IRC
gateway on my laptop", so a client trusts it one of two ways: by pinning that
fingerprint, or by trusting the private CA that signed it (see
[GUI clients](#gui-clients)). Either beats turning verification off. Pinning is
the stronger of the two — it notices a swapped certificate, which a CA would
not.

Every client spells this differently, and **the format is not portable** — the
two most common want opposite things, and getting it wrong fails as a mismatch,
not as a parse error, so the message accuses the certificate rather than your
punctuation:

| client | option | format |
|---|---|---|
| irssi | `-tls_pinned_cert` (on `/connect`) | colon-separated, as printed: `FE:08:…:11` |
| WeeChat | `irc.server.<name>.tls_fingerprint` | bare hex, no colons, 64 chars: `fe08…11` |
| Halloy | `root_cert_path` — no pinning; see [GUI clients](#gui-clients) | n/a |

Lowercasing and stripping the colons for irssi produces `Pinned certificate
mismatch` against a fingerprint that is byte-for-byte correct.

The token goes in the server-password field, or as the SASL PLAIN password if
your client prefers that (`sasl` is advertised; only PLAIN is supported).

### Turn on `server-time`

Worth doing before anything else, because JOIN replays history:

```
# WeeChat — on by default in recent versions, but confirm
/set irc.server_default.capabilities "server-time,message-tags,sasl"
```

**Then check that your client actually honours it**, because acknowledging the
capability and rendering it are two different things. irssi 1.4.5 negotiates
`server-time` and then timestamps replayed messages with the moment they
arrived anyway — every line of a JOIN replay reads as "just now", which is the
precise failure the capability exists to prevent. The gateway is not the
variable here: on the wire each message carries `@time=` with the instant it
crossed the relay, which you can confirm without a client at all —

```bash
# after JOIN, every replayed PRIVMSG should carry a tag with an OLD timestamp
… | grep -o '^@time=[^;]*' | sort -u
```

— so if the times on screen are all identical and all recent, the tag was
delivered and discarded, and the fix belongs in the client.

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

### GUI clients

Terminal clients pin a fingerprint; most graphical ones don't offer pinning at
all, and the usual workaround — a checkbox that accepts any certificate — throws
away the protection instead of configuring it. There is a better path: `--init-tls`
writes a small **CA** alongside the certificate it signs, and a client that lets
you nominate a root certificate can do full chain and hostname verification
against exactly that one root: yours.

This is why there are three files, and why it matters which one you hand a
client:

| file | what it is | who wants it |
|---|---|---|
| `irc-cert.pem` | leaf **+ CA**, in that order | `tls_cert` — the gateway serves it |
| `irc-key.pem` | the leaf's private key, `0600` | `tls_key` |
| `irc-cert-ca.pem` | the CA alone | `root_cert_path` and equivalents |

**Point a root-certificate setting at the CA, never at the served certificate.**
A leaf is not a valid trust anchor. A single self-signed certificate could only
act as its own anchor by carrying `CA:TRUE` — and a certificate with `CA:TRUE`
is not a legal end-entity certificate, so a strict client rejects the handshake
outright rather than falling back.

Be careful how you check this: **OpenSSL tolerates the contradiction and rustls
does not.** `openssl s_client` will report `Verify return code: 0 (ok)` against
a CA-as-leaf that Halloy refuses with `CaUsedAsEndEntity`. A green result from
`s_client` is therefore not evidence that a Rust client will connect — for that
class of client, the only verifier that counts is the client.

[Halloy](https://halloy.chat) is the one to reach for. It ships as a plain
tarball, so nothing is snap- or flatpak-confined and it reads your real config
paths:

The archive is laid out as a `~/.local` prefix (`bin/halloy` plus a `.desktop`
entry and icons under `share/`), so extracting it there installs the binary and
registers the launcher in one step:

```bash
v=2026.8   # check the releases page; the asset name carries the version
curl -fsSL "https://github.com/squidowl/halloy/releases/download/$v/halloy-$v-x86_64-linux.tar.gz" \
  | tar -xzf - -C ~/.local
```

**Take the tarball, not a snap or flatpak.** A confined build cannot read
`~/.config` at all: the snap `home` interface only covers *non-hidden* paths
under `$HOME`, and the Halloy snap does not plug `home` in the first place. So
it can reach neither the certificate nor the token, and its
`password-manager-service` plug ships unconnected, so the keyring is out too.
Working around that means keeping a copy of the certificate inside the snap's
own directory — which goes stale the day you reissue the certificate and then
fails as a verification error that says nothing about the copy. Unconfined means
one path and no copies.

```toml
# ~/.config/halloy/config.toml
[servers.dispatch]
server   = "localhost"           # must match a SAN on the certificate
port     = 6697
use_tls  = true
root_cert_path = "/home/you/.config/mcp-dispatch/irc-cert-ca.pem"   # the CA, not the leaf
nickname = "you"
channels = ["&dispatch"]

[servers.dispatch.sasl.plain]
username                      = "you"
password_file                 = "/home/you/.config/mcp-dispatch/irc-token"
password_file_first_line_only = true
```

Both paths are absolute on purpose. Every `~/…` elsewhere on this page is an
*mcp-dispatch* config key, and those expand — Halloy's do not. It resolves a
relative path against its own config directory, so a literal `~/…` becomes
`~/.config/halloy/~/…` and the file is simply not found.

Note `password_file`. The token is read/write on every conversation on this
host, so the goal is to never have a second copy of it — and the gateway already
keeps the authoritative one at `0600`. Pointing the client straight at that file
beats both a literal `password` in the config and `password_keyring`: nothing is
duplicated, nothing to re-sync when you rotate the token, and no dependency on a
keyring daemon being present and unlocked.

`password_file_first_line_only` is not optional. `--init-token` writes a
trailing newline, and without this the newline is sent as part of the password
and authentication fails — with a wrong-token error, for a token that is right.

`server` has to match a name the certificate covers — `localhost`, this host's
name, or an address in `--tls-hosts`. Pointing it at `127.0.0.1` when the SAN
says `localhost` fails verification, correctly.

To check the two halves independently before blaming the client:

```bash
# does the served chain verify against the CA, for this hostname?
openssl s_client -connect localhost:6697 -CAfile ~/.config/mcp-dispatch/irc-cert-ca.pem \
  -verify_return_error -verify_hostname localhost </dev/null | grep 'Verify return code'
```

A `0 (ok)` there means TLS is fine and anything still broken is auth.

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
| `socket` | `~/.config/mcp-dispatch/irc.sock`, **but only while `listen` is unset** | unix transport, `0600`, uid-checked |
| `listen` | *(unset)* | optional TCP `host:port` — setting it drops the socket default |
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

The gateway is only useful if it is up when you open your client, and
"remember to start it in a spare terminal" is not a plan:

```bash
bin/dispatch-ircd service install      # systemd user unit, enabled + started
bin/dispatch-ircd service status       # installed? active? enabled?
bin/dispatch-ircd service show         # print the unit without writing it
bin/dispatch-ircd service install --dry-run
bin/dispatch-ircd service uninstall
journalctl --user -u dispatch-ircd -f  # watch it work
```

Install validates the config and the token *before* writing anything — a unit
that starts and then refuses is a crash loop, not an error message.

The unit bakes **absolute** paths and systemd keeps running the process it
started, so re-run `service install` after moving the repo, rebuilding the
binary, or changing `[irc]`. It is idempotent, which makes it the upgrade path
too.

**On a headless host, enable lingering once.** A `systemd --user` manager
doesn't start at boot without it, and many distros tear it down when your last
session ends — so the gateway would run only while you happen to be logged in,
which presents exactly like it being broken:

```bash
sudo loginctl enable-linger $USER
```

Without systemd, the gateway is a plain foreground process that exits cleanly on
SIGINT/SIGTERM — put it under whatever supervisor you do have.

It polls; it does not hold presence and does not gate on it. Running with no
live agents is fine — you see an empty roster and any traffic that arrives.
