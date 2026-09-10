"""Side-effect-free filesystem primitives for the mcp-dispatch local bus.

These are the byte-level details of the ``DISPATCH_DIR`` contract — id validation,
the durable atomic write, the inbox filename scheme, TTL parsing, and presence /
channel-subscriber resolution. They live here, apart from ``server.py``, so a
*second* process (the git replicator daemon, ``git_bridge.py``) can reuse the
exact same logic without importing ``server.py`` — whose module load claims an
agent id and starts background threads.

Nothing in this module touches global state or has import-time side effects, so
it is safe to import from anywhere. ``server.py`` delegates to these so there is
one source of truth for the on-disk format; drift here would silently corrupt
cross-host delivery.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Agent ids and targets become path segments under DISPATCH_DIR, so they must
# never contain separators or traversal sequences. Constrain to a safe charset.
# \Z (not $) anchors the absolute end — $ would also match before a trailing newline.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")

# A dynamic-mode id is `<nick>-<pid>`. The nick is the durable half.
PID_SUFFIX_RE = re.compile(r"^(?P<nick>.+)-\d+$")


def validate_id(value: str, kind: str = "agent id") -> str:
    """Ensure an id is a single safe path segment. Raises ValueError otherwise."""
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ValueError(
            f"Invalid {kind} {value!r}: must match {ID_RE.pattern} "
            "(lowercase alphanumeric, '_' or '-', 1-64 chars, no path separators)."
        )
    return value


def atomic_write(path: Path, data: dict) -> None:
    """Write JSON durably and atomically: write tmp, fsync file, rename, fsync dir.

    fsync on the file makes its bytes durable before the rename (no renamed-but-
    empty file on crash); fsync on the parent directory makes the rename itself
    durable (otherwise a crash can lose the new directory entry, dropping the
    message entirely).
    """
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def parse_timestamp(ts: str) -> float:
    """Parse ISO 8601 timestamp to epoch seconds."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def message_filename(from_id: str) -> str:
    """The inbox filename scheme: ``<ms-timestamp>-<from>-<uuid8>.json``.

    The uuid suffix prevents two same-millisecond sends from the same sender from
    colliding on one filename (which would silently drop a message).
    """
    ts = str(int(time.time() * 1000))
    return f"{ts}-{from_id}-{uuid.uuid4().hex[:8]}.json"


def is_expired(msg: dict) -> bool:
    """True if ``msg``'s TTL has elapsed. ``must_read`` never expires."""
    ttl = msg.get("ttl")
    if not ttl or ttl <= 0 or msg.get("must_read", False):
        return False
    sent_at = parse_timestamp(msg.get("timestamp", ""))
    if sent_at <= 0:
        return False
    return time.time() > sent_at + ttl


def iter_pending(inbox: Path) -> Iterator[dict]:
    """Every message in ``inbox`` still waiting to be read, oldest first.

    "Waiting" means pending *and* unexpired. Four places had written this loop
    and one of them left out the expiry test, so `dispatch-status` reported mail
    that every other consumer had already written off — including a five-minute
    routing probe it still called unread a day and a half later. A count a human
    acts on has to mean the same thing as the count the supervisor acts on.

    Unreadable and malformed files are skipped rather than raising: an inbox is
    written concurrently by other processes, and a half-written file is a
    momentary state, not a reason to fail the whole scan.
    """
    if not inbox.is_dir():
        return
    for f in sorted(inbox.glob("*.json")):
        try:
            msg = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("state", "pending") != "pending" or is_expired(msg):
            continue
        yield msg


def count_pending(inbox: Path) -> int:
    """How many messages in ``inbox`` are actually waiting. See `iter_pending`."""
    return sum(1 for _ in iter_pending(inbox))


def presence_is_live(pf: Path) -> bool:
    """True iff a live process holds the exclusive flock on this presence file.

    The lock — not the pid field — is the source of truth: it's uid-agnostic
    (works across accounts in group_mode, unlike os.kill) and immune to pid
    reuse, because the kernel drops it when the owner dies, crashes, or the host
    reboots. We probe with a non-blocking exclusive lock: if we can take it,
    nobody's home; if it blocks, a live process holds it.
    """
    try:
        fh = open(pf)
    except OSError:
        return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def live_presence_files(dispatch_dir: Path) -> list[Path]:
    """Presence files whose owner is currently live."""
    return [
        pf for pf in sorted((dispatch_dir / ".presence").glob("*.json")) if presence_is_live(pf)
    ]


def live_agents(dispatch_dir: Path) -> list[str]:
    """Agent ids with a live presence record (validated to be path-safe)."""
    out: list[str] = []
    for pf in live_presence_files(dispatch_dir):
        try:
            aid = json.loads(pf.read_text()).get("agent_id") or pf.stem
        except (OSError, json.JSONDecodeError):
            aid = pf.stem
        if ID_RE.match(str(aid)):
            out.append(str(aid))
    return out


def durable_nick(agent_id: str) -> str:
    """The stable identity behind a session id: ``publicai-1767991`` → ``publicai``.

    An id with no pid suffix (a roster id, or an explicit MCP_DISPATCH_AGENT_ID)
    is already durable and passes through unchanged.
    """
    m = PID_SUFFIX_RE.match(agent_id)
    return m.group("nick") if m else agent_id


def nick_for_dir(cwd: str) -> str:
    """The nick a session launched in `cwd` will claim: ``~/code/webapp`` → ``webapp``.

    The rule lives in bin/dispatch-launcher, which computes it in shell before it
    can import anything. This is the Python half of that pair — kept identical so
    the supervisor can answer "what would a session started here actually be
    called?" without launching one. tests/test_launcher.py runs the real launcher
    and asserts the two agree; change one and change the other.

    Returns "" when the directory name has no usable characters, which is the
    launcher's `base="agent"` case — the caller decides what to do about it
    rather than being handed a plausible-looking wrong answer.
    """
    base = re.sub(r"^-*", "", re.sub(r"[^a-z0-9-]", "", os.path.basename(cwd.rstrip("/")).lower()))
    return base[:50]


def live_nicks(dispatch_dir: Path) -> set[str]:
    """Durable nicks with at least one live session right now."""
    return {durable_nick(aid) for aid in live_agents(dispatch_dir)}


def resolve_recipients(dispatch_dir: Path, target: str) -> list[str]:
    """Map a DM target to the inbox ids it should actually be written to.

    Cases, in order:

      - the target is itself a live session id → deliver to it, unchanged;
      - the target is a *nick* with live sessions → deliver to all of them,
        because addressing `publicai` means addressing that teammate, and
        picking one of its sessions arbitrarily is how a message reaches the
        window nobody is watching. The caller reports exactly where it went;
      - the target is another host's session id → deliver to it unchanged. Its
        inbox here is the git bridge's pickup point, not a local mailbox, and
        stripping the suffix would hand the message to a same-named session on
        *this* host instead — `documents-<pid>` is what every session launched
        from a projects folder is called on every machine;
      - the target is a *dead* session id of a local nick → resolve the nick
        behind it. Addressing one specific window only means something while
        that window exists; once it has exited, writing to its inbox is how a
        reply reaches a corpse — the sender picked the id off a `who()` list
        minutes stale, and nobody finds out until they go looking on disk;
      - nothing live → deliver to the nick's own inbox and leave it there. It
        is not lost: the next session of that nick inherits it on startup
        (see server._inherit_orphan_inbox). This is what makes an offline
        teammate addressable at all.

    Shared by server.py's dispatch() tool and bin/dispatch-send so a message
    from either path resolves identically — one routing decision, not two that
    can drift.
    """
    if target in live_agents(dispatch_dir):
        return [target]
    live = sorted(aid for aid in live_agents(dispatch_dir) if durable_nick(aid) == target)
    if live:
        return live
    nick = durable_nick(target)
    if nick == target:
        return [target]  # no pid suffix — an ordinary name, possibly never seen
    # The roster is only a safe "somewhere else" signal because git_bridge tells
    # this host's own corpses apart from other machines' sessions by the .agents
    # registry rather than by presence, which gets reaped. If that ever regresses
    # this branch starts stranding local mail again, which is where it began.
    if (dispatch_dir / ".remote" / f"{target}.json").exists():
        return [target]
    live = sorted(aid for aid in live_agents(dispatch_dir) if durable_nick(aid) == nick)
    return live if live else [nick]


def local_session_ids(dispatch_dir: Path) -> set[str]:
    """Every session id the ``.agents`` registry records as claimed on this host.

    The answer to "did this id ever run *here*?", asked long after it exited. The
    registry is never reaped, which is what makes it usable for the question;
    presence is not, so a dead session loses its presence file at the next startup
    and stops being recognisable as ours. See server._local_session_ids for what
    goes wrong when the caller has to fall back on presence.

    ``last_session_id`` counts too. It predates ``local_sessions`` and is written
    by the same claim, so records already on disk answer for their most recent
    session without waiting for that nick to start again.
    """
    out: set[str] = set()
    reg = dispatch_dir / ".agents"
    if not reg.is_dir():
        return out
    for f in reg.glob("*.json"):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        recorded = list(rec.get("local_sessions") or [])
        recorded.append(rec.get("last_session_id"))
        out.update(a for a in recorded if isinstance(a, str) and ID_RE.match(a))
    return out


def channel_subscribers(dispatch_dir: Path, channel: str) -> list[str]:
    """Live agents currently subscribed to a channel, by presence record."""
    subs: list[str] = []
    for pf in live_presence_files(dispatch_dir):
        try:
            data = json.loads(pf.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if channel in data.get("channels", []):
            aid = data.get("agent_id")
            # The agent_id becomes a path segment downstream. A presence file is
            # group-writable in group_mode, so don't trust it blindly.
            if aid and ID_RE.match(str(aid)):
                subs.append(aid)
    return subs


def send_message(
    dispatch_dir: Path,
    from_id: str,
    to: str,
    content: str,
    *,
    priority: str = "normal",
    thread_id: str | None = None,
    reply_to: str | None = None,
    payload: dict | None = None,
    ttl: int | None = None,
    must_read: bool = False,
    dynamic_mode: bool,
    agent_ids: list[str],
    max_message_bytes: int,
    default_ttl: int,
) -> dict:
    """Write a message to ``to``'s inbox (or fan it out for ``'all'``/``'#chan'``).

    The single implementation behind both server.py's ``dispatch()`` MCP tool
    and ``bin/dispatch-send``, so a write from either path produces byte-
    identical inbox files — the reason this lives here rather than being
    hand-rolled a second time by an external caller reimplementing the wire
    format from outside.
    """
    if ttl is not None and ttl < 0:
        raise ValueError(f"ttl must be >= 0 (got {ttl}); use 0 or omit for no expiry.")
    effective_ttl = default_ttl if ttl is None else ttl
    msg = {
        "id": f"msg-{uuid.uuid4().hex[:8]}",
        "from": from_id,
        "to": to,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "priority": priority,
        "content": content,
        "payload": payload,
        "thread_id": thread_id,
        "reply_to": reply_to,
        "ttl": effective_ttl if effective_ttl and effective_ttl > 0 else None,
        "must_read": must_read,
        "state": "pending",
    }

    # Enforce size limit against the bytes actually written (indent=2, matching
    # atomic_write) plus headroom for the read_at/state fields added on read.
    msg_bytes = len(json.dumps(msg, indent=2).encode("utf-8")) + 64
    if msg_bytes > max_message_bytes:
        raise ValueError(
            f"Message too large ({msg_bytes} bytes). Maximum: {max_message_bytes} bytes."
        )

    def _validate_target(target: str) -> None:
        if not dynamic_mode:
            if target not in agent_ids:
                valid = ", ".join(agent_ids) + ", #channel, all"
                raise ValueError(f"Unknown agent '{target}'. Valid targets: {valid}")
        else:
            # In dynamic mode any name is accepted, but it becomes a path
            # segment, so it must still be a safe single segment.
            validate_id(target, "target")
        # No mkdir here. _deliver_one creates whatever inbox resolution actually
        # chose, and creating the *named* one first resurrects the directory of a
        # dead session we are about to route away from — an empty spool that reads
        # like a real mailbox to anyone listing the relay.

    def _deliver_one(target: str, resolved_to: str | None = None) -> None:
        # resolved_to overrides the stored `to` for this copy only. Needed for the
        # nick-resolution path: notify_policy's "direct" check is exact-string
        # equality against the *reading* session's own id, so a copy still
        # carrying the typed nick never matches `<nick>-<pid>` and never wakes a
        # direct-policy watch, even though it landed in the right inbox — see
        # docs/feedback-2026-08-08-nick-addressed-dm-never-wakes-a-direct-watch.md.
        # "all" and "#channel" deliveries keep the original `to`; should_notify's
        # channel/broadcast branches key off that literal, not off exact-id match.
        out = dict(msg)
        if resolved_to is not None:
            out["to"] = resolved_to
        (dispatch_dir / target).mkdir(exist_ok=True)
        atomic_write(dispatch_dir / target / message_filename(from_id), out)

    if to == "all":
        # Broadcast: live agents in dynamic mode (a dead <project>-<pid> id never
        # returns, so writing to its inbox is pure waste). In roster mode keep the
        # full roster — an offline roster agent keeps its id and collects mail.
        pool = agent_ids if agent_ids else live_agents(dispatch_dir)
        delivered = [aid for aid in pool if aid != from_id]
        for target in delivered:
            _deliver_one(target)
    elif to.startswith("#"):
        # Channel: only current subscribers, except the sender.
        channel = validate_id(to[1:], "channel")
        delivered = [aid for aid in channel_subscribers(dispatch_dir, channel) if aid != from_id]
        for target in delivered:
            _deliver_one(target)
    else:
        _validate_target(to)
        # A nick is not an inbox: `publicai` names a teammate whose live sessions
        # are `publicai-<pid>`. Resolve it, so addressing the teammate reaches the
        # session actually running — and, when none is, waits in the nick's inbox
        # for the next one to inherit instead of rotting in a dead pid's.
        delivered = resolve_recipients(dispatch_dir, to)
        for target in delivered:
            _deliver_one(target, target)

    result: dict = dict(msg)
    # `queued_to`, not `delivered_to`: this is the set of inboxes written, i.e.
    # addressing — not receipt. Whether a recipient ever *reads* it shows up later
    # as the message's state flipping pending → read, which peek() surfaces to the
    # sender as sent_receipts. Conflating the two is how a channel post can look
    # landed while nobody has seen it.
    result["queued_to"] = delivered
    return result


# ---------------------------------------------------------------------------
# Translation seam: local message dict <-> git_transport.Envelope
# ---------------------------------------------------------------------------
#
# The whole local message dict travels as the git Envelope `body` (opaque), so
# every field round-trips losslessly. Only the routing/partition headers are
# lifted out of the body onto the envelope.


def msg_to_publish_kwargs(msg: dict) -> dict[str, Any]:
    """GitBus.publish kwargs for a local message, minus the routing target.

    The caller (git_bridge) supplies exactly one of ``to=`` / ``chan=`` based on
    the message's local target; everything else (the opaque body, the LWW
    partition key, the ttl) is derived here so the mapping lives in one place.
    """
    return {
        "body": msg,
        "type": "message",
        "key": msg.get("thread_id"),
        "ttl": msg.get("ttl"),
    }


def envelope_to_msg(env: Any) -> dict:
    """Reconstruct a deliverable local inbox message from a received Envelope.

    ``env.body`` *is* the original local message dict. Reset it to a freshly
    delivered state and tag its origin so the outbound mirror never re-publishes
    a message that arrived over git (echo guard).
    """
    msg = dict(env.body)
    msg["state"] = "pending"
    msg.pop("read_at", None)
    msg["_via"] = "git"
    return msg


# ---------------------------------------------------------------------------
# Bridge provenance tags — shared by git_bridge.py and bridge_native.py
# ---------------------------------------------------------------------------

# `_via` values marking a message as having already crossed ONE bridge (a
# transport/trust boundary this relay doesn't fully vouch for). Every bridge's
# own outbound scan must skip ALL of these, not just its own: re-publishing a
# git-origin message over git is a pointless echo, but re-publishing a
# native-bridge-origin message over git is worse than pointless — git_bridge's
# materialization (envelope_to_msg, above) unconditionally overwrites `_via`
# to "git", which would silently launder an untrusted-provenance message into
# one that reads as an ordinary cross-host DM on every other host. One shared
# set is what keeps a THIRD bridge from repeating this by hand-copying a
# single-value check the way this repo's dispatch_common.py module exists to
# stop happening for config/identity plumbing.
BRIDGED_VIA_TAGS = frozenset({"git", "native-bridge"})


# ---------------------------------------------------------------------------
# Outbound-ledger persistence — shared by git_bridge.py and bridge_native.py
# ---------------------------------------------------------------------------
#
# "Already attempted this message id" bookkeeping has the identical shape in
# both bridges (load + TTL-prune on start, prune + durable save after every
# tick), and started drifting the moment there were two copies — this is the
# single source both now call.


def load_ledger(path: Path, ttl_seconds: float) -> dict[str, float]:
    """Load a bridge's ``{message_id: attempted_at}`` ledger, dropping entries
    older than ``ttl_seconds`` (the source inbox message has long since expired,
    so the entry recording "already attempted" is moot)."""
    try:
        raw: dict[str, Any] = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    cutoff = time.time() - ttl_seconds
    return {k: float(v) for k, v in raw.items() if float(v) >= cutoff}


def save_ledger(path: Path, ledger: dict[str, float], ttl_seconds: float) -> dict[str, float]:
    """Prune and durably save a ledger (plain write+rename — this is
    bookkeeping, not delivery state, so atomic_write's fsync isn't needed).
    Returns the pruned dict, which the caller should keep as its new in-memory
    copy so a later save doesn't resurrect what this one just dropped."""
    cutoff = time.time() - ttl_seconds
    pruned = {k: v for k, v in ledger.items() if v >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pruned))
    tmp.replace(path)
    return pruned
