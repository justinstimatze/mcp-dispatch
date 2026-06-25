# Git-backed transport for mcp-dispatch (design)

**Status:** design / RFC. Not implemented. **Decided:** mcp-dispatch owns this
transport; ettle (and other coordination layers) consume it (see "Ownership").

## Goal & non-goals

A second transport backend behind the **same** agent-facing tool API
(`dispatch / peek / ack / subscribe / who`) and the **same** message
envelope schema, providing **async, durable, cross-machine, cross-user,
audited** messaging — using a private git repo as the bus and inheriting
git-host auth / TLS / hosting / audit for free.

- **Non-goal:** replacing the local single-host rail. The local rail keeps
  its distinctive machinery (flock presence, zero-token event-driven wake,
  sub-second delivery). None of that ports; this is a *different primitive*
  (an async durable mailbox), not a port of the rail.
- **Deferred:** the wake story (how a parked reader learns of new messages
  without polling). Git has no push-to-idle-process; baseline is interval
  `git fetch`. Treated separately — see "Wake (deferred)".

## Core principle: per-author append-only lanes + union merge

The one trick everything rests on, and it's conflict-free by construction:

- Each agent owns exactly one file it ever writes: `lanes/<agent_id>.jsonl`.
  **Sending = append one JSON line + commit + push.** Because an agent only
  appends to its own lane, two agents' pushes never edit the same file →
  always fast-forward / trivially clean merge.
- **Channels keep the same invariant** via a per-channel directory with one
  file per author: `channels/<chan>/<author>.jsonl`. Every file is still
  single-writer → pure fast-forward, **no union-merge dependency anywhere**. A
  channel is one sparse-checkout-able directory; reading it = read the files in
  `channels/<chan>/` and merge by `seq`/`ts`. The only cost is cross-author
  order *within* a channel — the same cross-lane-ordering problem we solve with
  an envelope `seq` (below), now scoped to channel participants.
- **Union merge is a fallback, not the foundation.** If a flat shared file is
  ever wanted (e.g. extreme small-file counts), a `.gitattributes`
  `*.jsonl merge=union` driver makes concurrent appends auto-union — but it has
  reorder/duplicate edges on odd histories (mitigated by stable `id` + reader
  dedup). The directory-per-author layout avoids needing it at all; prefer that.

**Addressing is by content; delivery is by scan.** Recipients are named in
the line (`to`, or `chan`), not by file path. A reader does: `git fetch` →
diff lanes/channels changed since its local cursor → filter lines where
`to == me` or `chan ∈ my-subs`. This mirrors the current rail's
notify-poll-diffs-inbox pattern, made git-native. (Writing into a
recipient's inbox path would break one-writer-per-file and reintroduce
conflicts — so we don't.)

## Mapping: mcp-dispatch / IRC mechanics → git mechanics

| mcp-dispatch / IRC            | git mechanic |
|-------------------------------|--------------|
| agent identity (NICK)         | lane filename + commit author (+ optional signing key) |
| send DM (PRIVMSG user)        | append `{to, …}` to own `lanes/<me>.jsonl`; commit; push |
| channel post (PRIVMSG #chan)  | append to own `channels/<chan>/<me>.jsonl` (single-writer) |
| receive                       | fetch; diff since local cursor; filter `to==me` ∨ `chan∈subs` |
| ack **+** delivery receipt    | append `{type:"ack", ids:[…]}` to own lane (unified — see below) |
| read/unread state             | **reader-local cursor** (last-processed commit SHA), NOT in repo |
| threading (thread_id/reply_to)| body fields, grouped on read (no git mechanic needed) |
| presence / WHO                | `refs/presence/<id>` heartbeat ref, **off** the message branch |
| subscribe (JOIN)              | reader-side filter (+ optional `subs/<id>.json` for visibility) |
| topic                         | `channels/<chan>/.topic` file, last-writer-wins |
| scrollback                    | `git log` of `channels/<chan>/` — free, permanent (beats IRC) |
| priority / urgent             | content field; affects reader/notify policy only |
| TTL / expiry                  | opt-in rewrite of own lane dropping expired lines (no conflict) |
| log rotation (opt-in)         | squash / orphan old history, or archive branch (default: keep) |
| conflict-free concurrency     | per-author lanes + `merge=union` on shared files |
| auth / TLS / audit            | inherited from the git host |

### ack & receipts unify; read-state is local

You can't delete a line in append-only land without rewriting the file. So
**ack is itself an append**: a reader appends `{type:"ack", ids:[…]}` to its
own lane. The sender learns delivery by scanning for acks — meaning **ack and
delivery-receipt are the same mechanism**. Separately, "have I personally
seen this" is a **reader-local cursor** (a commit SHA stored outside the repo,
e.g. under the state dir) — never written to the bus, so it generates zero
history and stays private.

### Presence via refs, not files

Git has no liveness. A naive heartbeat *file* committed every N seconds would
explode history (directly worsening worry **b**). Instead, each agent keeps a
ref `refs/presence/<agent_id>` pointed at a tiny commit whose timestamp is the
heartbeat. **Updating a ref doesn't add to the message-branch history**;
`git for-each-ref refs/presence/` filtered by commit age = `who`. This keeps
the high-churn liveness channel entirely out of the message log — the same
separation flock-vs-inbox has today. Async de-emphasizes presence anyway
(what matters is delivered+acked, which the log already shows), so presence
can even be best-effort / optional.

## The three worries, addressed

### (a) Cleartext agent chat in git history → encrypt **per line**, app-layer

**Opt-in, default off.** Like the local rail (trust = host), the baseline
trust boundary is *repo access*; encryption is opt-in for cross-trust-boundary
use. When on, the fix is **per-line application-layer encryption**: the `body`
of each record is encrypted before append, so the repo and its entire history
store only ciphertext. Cleartext exists only in an authorized agent's working
memory, never on disk in git.

- DMs: encrypt to the **recipient's public key** (age / libsodium sealed box).
  Even a repo-reader without the key can't read a DM not addressed to them.
- Channels: encrypt to a **shared channel key** held by subscribers.
- **Why per-line and not whole-file (git-crypt):** git-crypt encrypts the
  whole file, which **breaks union merge** (you can't union two ciphertext
  blobs). Per-line keeps each line an independent opaque blob — encryption and
  conflict-freedom coexist. Whole-file encryption and a mergeable log are
  mutually exclusive; this is the non-obvious constraint.
- **One keypair, three jobs.** The per-agent keypair this needs is the *same*
  primitive `sig` (non-repudiation) and any future auth want — build it once,
  publish pubkeys as `keys/<agent_id>.pub` lanes (fits the per-author model).
  Encryption is not standalone scope; it's the convergent identity substrate.
- **Versioned algorithm field; PQ-ready.** Carry the cipher/KEM in a versioned
  envelope field so the scheme can migrate without a wire break. This bus
  stores ciphertext **forever** in git history, so harvest-now-decrypt-later is
  a real threat here (unlike ephemeral transports) — design toward a hybrid
  X25519 + ML-KEM sealed box (NIST ML-KEM finalized 2024) even if v1 ships
  classical-only.
- Routing fields (`to`, `chan`, `id`, `ts`, `ttl`) stay cleartext — see the
  metadata note below.

#### Metadata is cleartext, by necessity — scope it honestly

Routing fields must be readable without decryption (and the git commit
author + timestamp leak who-committed-when *regardless* of the envelope — you
cannot out-design the commit graph). So **this transport provides body
confidentiality + audit, NOT metadata privacy.** Who-talks-to-whom and timing
are visible to anyone with repo read access. This is accepted for v1. Cheap
mitigation: allow **pseudonymous agent_ids** (a DM to `agent-7f3a` leaks less
than to `acme-payroll-bot`). Full metadata privacy needs mixnets/PIR/oblivious
storage — categorically incompatible with a git substrate; if a user needs
that, it's the wrong substrate, and we say so rather than half-solving it.

### (b) Long history → slow checkouts/pulls → don't pull what you don't need

Git already has every lever; combine them:

- **Bare repo on the server, holds full history.** Agents never clone it whole.
- **Partial + shallow clone** for readers: `--filter=blob:none --depth=N` →
  fetch only recent objects, not the whole DAG.
- **Sparse checkout**: a reader checks out only the lanes/channels it cares
  about (`channels/general/` + DMs), not every lane — the per-channel
  *directory* layout is what makes "follow only these channels" cheap.
- **Presence on separate refs** (above) keeps the chattiest writes off the
  message branch entirely.
- Net: a reader's working copy and fetch cost scale with *its* recent
  traffic, not the global all-time history.

### (c) Log rotation / compaction → opt-in, off by default

Default is **keep everything** (the audit trail is a feature). Rotation is an
**opt-in** knob for users who treat the bus as transient, not as an
audit record:

- **Lane-local expiry:** an agent periodically rewrites its own lane dropping
  lines past a retention window + commits. Single-writer file → no conflict.
- **History compaction:** squash commits older than the window into one
  "archive" commit, or move old history to an `archive` branch / cold repo,
  then `gc`/repack. The active branch stays small; auditors keep the archive.
- Knob shape: `retention = forever (default) | <duration>`, plus
  `compact = off (default) | squash | archive`.

**Compaction must be consumer-aware (event-stream vs state-snapshot).** This is
the subtle one. For an **event-stream** consumer (our messages — each delivered
once, old = stale), time/count-based "drop older than window" compaction is
safe and desirable. For a **last-write-wins state** consumer (e.g. ettle's
atoms — the latest write of each key *is* current, even if old), naive
time-based compaction can drop a still-current value and break snapshot
reconstruction. So:
- Default `compact = off` is snapshot-safe trivially (nothing dropped).
- Offer a **key-aware compaction mode** ("keep latest line per `(author, key)`,
  drop superseded") for state consumers — that's *more* compactable, not less,
  but along a different axis than time.
- **Never destroy the per-author lane tail a snapshot consumer folds over.**
  Time-based tail-trimming is opt-in and event-stream-only.

## Consumer access patterns & the shared envelope header

Two consumer shapes ride this transport, and the layout must serve both:

- **Event stream** (our messages): each record delivered once; read the lane
  *tail* since a cursor; old records are stale. Git fits this natively.
- **State snapshot** (e.g. ettle's atoms): records are last-write-wins state;
  the consumer wants "every author's *latest*" (`Collect()`), folding state
  over the log. Served cheaply **iff** per-author lanes are preserved and the
  tail isn't destroyed (see compaction, above).

**Design rule:** keep per-author lanes whose tail reconstructs current state;
don't optimize purely for event-stream tail-reading + destructive compaction,
or state consumers can't adopt the transport without full replay.

### Shared envelope header (interop seam)

So a messages-over-git lane and a state-over-git lane are the *same wire
format*, the ratified header is (all cleartext except `body`):

```
{ type, from, to|chan, key, id, ts, seq, ttl, version, sig, body }
```

- `type` — **record discriminator** (`message` | `atom` | `ack` | `presence` |
  …). Load-bearing: it's what lets one lane carry mixed records and lets ettle
  be "just another body type." Without it, interop is impossible.
- `from / to|chan / id / ts` — routing + dedup, readable without decrypting.
- `key` — **LWW partition key** (cleartext, optional). `null` for event-stream
  records (our messages). For state consumers, the compaction unit: ettle sets
  it to an atom's slot identity so "keep latest per `(from, key)`" runs on
  *encrypted* bodies without the compactor holding a decrypt key. Added at
  ettle's pre-implementation request — `from` alone is too coarse (ettle dedups
  per-atom-slot, not per-author). Moot in v0 cleartext, future-proofs encryption.
- `seq` — per-lane monotonic sequence, for causal/delivery-order
  reconstruction without trusting wall-clock (the cross-lane-ordering fix).
- `ttl` — **in the header, not body**: compaction/expiry is a transport
  function that must run on ciphertext, so the compactor can't be required to
  hold decryption keys.
- `version` — schema version, forward-compat.
- `sig` — signature over the record (non-repudiation); reserved/unenforced
  acceptable in v1 (matches ettle's DirBus `Sig`). Same keypair as encryption.
- `body` — **opaque** payload (encryptable): our message
  `{content, priority, thread_id, reply_to, must_read}`, or ettle's `atoms[]`.

`priority / thread_id / reply_to / must_read` live **in `body`** — only the
recipient acts on them, and keeping them encrypted minimizes metadata leak.

**CloudEvents-aligned.** This maps near 1:1 onto the CNCF CloudEvents standard
(`source`≈from, `specversion`≈version, `time`≈ts, `type`=type, `data`≈body),
so adopting its field names buys broad ecosystem interop nearly free. Extends
ettle's proposed `{from, ts, version, sig, body}` with `type`, `to|chan`,
`key`, `id`, `seq`, `ttl` — the fields our routing, encryption, and compaction
need cleartext. Cheap to fix now, expensive to reconcile later.

**Consumer contract:** expose `Publish(envelope)` and
`Collect() -> latest envelope per author`. A transport offering those two
serves both the event-stream and state-snapshot consumers.

### Cross-lane ordering

Each lane is totally ordered (append-only). There is **no global cross-lane
order** — cross-lane ordering is commit timestamp (clock-skew-unreliable) or
needs logical clocks. State consumers (LWW atoms) barely care; **message**
consumers may care about delivery/causality, so the envelope should carry
enough (per-lane sequence + optional `reply_to`/logical clock) to reconstruct
causal order where it matters, rather than trusting wall-clock.

## Prior art

ettle's `internal/transport/dir.go` (`DirBus`): a working "per-participant
files in a shared folder = bus." Envelope `{Participant, Atoms[], V, EmittedAt,
Sig}`; identity = filename convention; honest-limits comment notes "anyone who
can write the folder can write any file" and `Sig` is reserved-but-unenforced
in v1. A real reference for the lane layout — git adds the auth/TLS/audit/
cross-machine that the shared-folder version can't provide across a trust
boundary.

## Wake (deferred)

Baseline: readers run an interval `git fetch` loop (cron, or a long-running
fetch-poll). Tighter polling hits git-host rate limits, so this is a
low-to-moderate-volume bus by nature. Push-style wake (webhook → local
notifier → re-arm a parked session) is possible but is the cross-machine
sibling of the same-host reachability/spawn problem — tracked in the memory
note `reachability-vs-capability-spawn-design`, to revisit together.

## Honest ceilings

Per-message commit+push is heavy; repo growth needs the (b)/(c) levers;
host rate limits cap poll frequency; very high channel fan-out (everyone
pulls everyone's lane) is awkward. This is a **durable / audited / cross-
boundary / moderate-volume async bus**, not a real-time firehose. Within that
envelope it's not just "still useful" — for cross-org/audited coordination
it's the *only* admissible option, since the local rail's filesystem+flock
security model can't cross a trust boundary.

## Ownership (decided)

**mcp-dispatch owns the transport; ettle is a coordination layer on top of it.**
Clean separation of concerns:

- **mcp-dispatch (transport):** move records — locally (the flock rail) or
  cross-machine (this git bus) — behind one tool API + the shared envelope.
  Independently useful with *zero* coordination machinery: "easy multi-machine
  agent chat" (e.g. between physical machines on one desk) is the same value
  the local rail already delivers on one host, extended across machines.
- **ettle (coordination layer):** atoms, state-folding (`Collect()`),
  distributed-mode orchestration — the machinery that makes ettle *ettle* —
  rides the transport as a consumer via `Publish/Collect`.

The earlier worry about an external consumer constraining the API / timeline
**does not apply here**: both are the same author's projects with a single
user, so "ettle depends on mcp-dispatch" is internal layering, not a vendor
obligation. The shared envelope is the seam between the two layers; the local
rail stays the flagship, the git bus is the second reference backend.
