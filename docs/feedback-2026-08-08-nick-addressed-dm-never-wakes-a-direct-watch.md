# Feedback: a nick-addressed DM is delivered correctly but never wakes a `direct`-policy watch

Filed 2026-08-08 by a `firecrawl` session, from a live incident on this host.

## Symptom

`aipotluckorg-215638` dispatched to `firecrawl-750492` and, after 30 minutes
with the message still unread, concluded the addressee was unreachable and
reassigned the work to a third agent. The receiving session's presence had
been live the entire time, `dispatch-wait --follow` was armed under Monitor,
and `notify_on = "direct"` was in effect. `peek()` showed the message the
moment someone thought to call it. Nothing ever woke the session to call it
sooner.

## Why delivery is fine but the wake is not

`_send` (`server.py:886-977`) stores the message's `to` field as whatever
string the *sender* passed in, unresolved:

```python
msg = {..., "to": to, ...}   # server.py:905
```

Delivery itself goes through `_resolve_recipients` (`server.py:582-622`),
which maps a nick to whatever session id is actually live right now and
writes the file into that directory. The resolved id is used only as a
filesystem path — it is never written back into the message body. So
`dispatch(target="firecrawl")` lands correctly in `firecrawl-750492`'s inbox,
and the file sitting there still says `"to": "firecrawl"`.

That mismatch is invisible to `peek()`, which reads the inbox directory
directly (`_read_inbox(AGENT_ID, ...)`) and never checks `to`. It is not
invisible to `notify_policy.should_notify` (`notify_policy.py:52-58`), which
the `"direct"` policy resolves by exact string equality:

```python
if agent_id and to == agent_id:   # "firecrawl" == "firecrawl-750492" → False
```

Both delivery paths that exist to wake a parked session call this same
predicate with the *current* session's literal `<nick>-<pid>` id:
`dispatch-wait --follow`'s `_qualifying` (`bin/dispatch-wait:112-126`, the
thing Monitor streams) and the server's own desktop-notifier thread
(`server.py:1243-1248`). Neither fires. The watch is armed, the message is
delivered, and the policy silently declines to count it as addressed to
anyone.

## Suggested fixes, roughly in order of leverage

1. **Stamp the resolved id, not the typed one.** `_send`'s `_deliver_one`
   (`server.py:942-944`) already loops per resolved target; for the
   single-recipient DM case, writing `{**msg, "to": target}` instead of the
   shared `msg` dict closes the gap at the source and needs no change to
   `should_notify` or either watcher.
2. **Failing that, widen the "direct" check** in `notify_policy.should_notify`
   to compare against the durable nick as well as the literal id —
   `to in (agent_id, dispatch_fs.durable_nick(agent_id))`. Cheaper, but every
   other consumer of `to` (receipts, digests, any future feature that prints
   who a message was addressed to) keeps seeing the pre-resolution string,
   so this only patches the wake path, not the mismatch itself.
3. **Either way, this contradicts the sender-side guidance already on file.**
   `feedback-2026-08-05-must-read-stranded-in-a-dead-inbox.md` recommends
   addressing the nick over the session id, for durability — correct for that
   failure mode. This incident is the opposite pull: nick-addressing is
   exactly what makes a `direct`-policy watch go silent on delivery. Both are
   true at once today, which means there is no single correct way to tell a
   sender to address a message. Fix #1 removes the contradiction outright;
   short of that, the `dispatch` tool description should say so explicitly
   rather than leave the two docs disagreeing.

## Not the cause, ruled out before landing here

- The receiving session's Monitor watch held its arm lock throughout — this
  was not an unarmed-session gap (`hooks/dispatch-arm.py`'s `MAX_BLOCKS`
  degrade path).
- `_inherit_orphan_inbox` was not involved — the session never restarted, and
  the file was never orphaned; it sat `pending` in the correct live inbox the
  whole time.
- `should_notify`'s channel branch is unaffected — this was a DM, not a
  channel post.
