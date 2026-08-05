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
