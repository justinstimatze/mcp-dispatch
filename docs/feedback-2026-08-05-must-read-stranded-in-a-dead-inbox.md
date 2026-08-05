# Feedback: a `must_read` message can become permanently visible and permanently unreachable

Filed 2026-08-05 by an `ettle` session, from a live incident on this host.

## Symptom

Four consecutive `stope` sessions opened, saw the same line in their digest —
one `must_read` message from `ettle-3619745` at `2026-08-05T15:51:41Z` — and
found `peek()` empty, with and without `include_read=true`. `ack()` on the id
returned `not_found`. Two of them dispatched back asking for a resend of
something they could see the existence of but not the body of.

The message was real. It was sitting in `DISPATCH_DIR/stope-1472743/`, the
inbox of a session that had died two days before it was sent. Nothing in the
tool surface could reach it, and nothing would ever remove it.

## Why it is visible

`digest._collect_mail` walks `_nick_inboxes`, which is every directory whose
durable nick matches — `digest.py:101-113`, and the docstring is explicit that
this is deliberate: *"liveness does not matter: a live session's inbox is
exactly where this session's own unread mail is."* Reasonable for the live
case. It means a dead session's pending mail is counted for every future
session under that nick.

## Why it is unreachable

`peek_tool` reads `_read_inbox(AGENT_ID)` — `server.py:1445-1455`. `ack_tool`
globs `DISPATCH_DIR / AGENT_ID` — `server.py:1497-1518`. Both are scoped to the
calling session's own directory. So the digest counts across the nick and the
two tools that could act on it read one inbox. A message in a sibling's
directory is in the gap between them.

## Why it is permanent

`is_expired` returns `False` unconditionally when `must_read` is set —
`dispatch_fs.py:92-94`. `_reap_empty_inboxes` only removes directories that are
*truly* empty (`server.py:598-622`), and this one is not. So the TTL never
fires, the directory is never collected, and the entry stays in the nick's
digest for the life of the store.

The three behaviours are individually defensible. Together they produce an
item that is counted forever, readable never, and ackable by nobody.

## Why inheritance did not save it

`_inherit_orphan_inbox` (`server.py:625`) exists for exactly this case, and its
docstring describes exactly this failure — *"anything the previous session
never got to read stays `pending` in a directory nobody will ever open
again."* It did not fire, and the reason is a chain of two correct-looking
decisions:

1. `_write_remote_roster` (`git_bridge.py:333`) publishes into `DISPATCH_DIR/.remote/`
   every id that has a lane in the git bus and is not in `_local_ids()`.
2. `_local_ids()` (`git_bridge.py:324`) derives "belongs to THIS host" from the
   presence directory, describing a presence file as *"the durable 'this id had
   a session here' marker."*
3. `_reap_dead_presence` (`server.py:568`) unlinks the presence file of every
   session with no live owner, at startup.

So the marker is not durable. Once a local session dies and the next server
start reaps its presence file, `_local_ids()` no longer contains it, and if it
ever wrote to a lane, the next roster pass republishes it as remote.

4. `_inherit_orphan_inbox` then skips it:
   ```python
   if (DISPATCH_DIR / ".remote" / f"{d.name}.json").exists():
       continue  # another host's session, not a dead predecessor of mine
   ```
   (`server.py:668`)

A dead local predecessor is reclassified as a foreign host, and the guard meant
to stop cross-host siphoning blocks the same-host inheritance it was written
alongside.

The clean demonstration does not depend on knowing where any past session ran.
On this host right now: the session writing this document is certainly local,
has a lane in the bus, and is absent from `.remote` — and the only thing
excluding it is its own live presence file. When it exits and the next server
start reaps that file, it will be published as remote. Every id in `.remote`
here (80 of 80) has a lane in this host's own bus checkout, against 6 surviving
presence files.

A secondary point: a `.remote` record is `{"agent_id", "via": "git",
"last_seen"}` — no host field. Even if the reclassification is fixed, the guard
cannot presently test the thing its comment says it is testing.

## Suggested fixes, roughly in order of leverage

1. **Make the local marker actually durable.** `_local_ids()` wants a permanent
   "this id had a session here" record and reads one that gets reaped. An
   append-only local-ids file, or skipping the reap for ids that still hold an
   inbox or a lane, restores inheritance for every project on the git
   transport — which is where this bug lives.
2. **Put the origin host in the `.remote` record**, so the inheritance guard can
   compare hosts instead of inferring foreignness from roster membership.
3. **Make `must_read` bounded rather than immortal.** It is a defence against
   inattention, not against death. A long TTL floor, or dropping the exemption
   once the addressee has no presence file and no live session, keeps the
   intent and removes the permanence. Nothing that is unreachable should also
   be un-expirable.
4. **Let a session `ack` anything addressed to its nick**, not only to its own
   id — or, failing that, have the digest name the inbox an item is sitting in,
   so a human has something to act on when `peek` disagrees with it.

## Sender-side lesson (not a bug)

`dispatch(target="stope")` — the bare nick — would have been durable. Both
`_nick_inboxes` and `_inherit_orphan_inbox` already treat the nick's own
directory as a drop box for exactly this reason. Addressing a `<project>-<pid>`
session id with `must_read=true` is a bet that the session outlives the send,
and losing that bet is unrecoverable rather than merely late. Worth a line in
the `dispatch` tool description: prefer the nick for anything that must not be
lost.

## Workaround applied

Three stranded messages were removed by hand from `DISPATCH_DIR`, all sent by
the reporting session, all `must_read`, all in dead `stope-*` inboxes: one
`pending` (the visible ghost) and two `read` (inert to the digest but equally
un-expirable). The nick's digest recomputed clean afterwards. That is cleanup,
not a fix — the next `must_read` to a session that dies first reproduces it.
