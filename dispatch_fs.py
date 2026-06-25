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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Agent ids and targets become path segments under DISPATCH_DIR, so they must
# never contain separators or traversal sequences. Constrain to a safe charset.
# \Z (not $) anchors the absolute end — $ would also match before a trailing newline.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")


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
        pf
        for pf in sorted((dispatch_dir / ".presence").glob("*.json"))
        if presence_is_live(pf)
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
