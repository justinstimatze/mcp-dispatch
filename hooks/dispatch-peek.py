#!/usr/bin/env python3
"""Stop hook: surface unread mcp-dispatch messages to the model.

A fallback for sessions that act as pure consumers (they never call dispatch
tools, so piggyback delivery never fires). Reads the agent's inbox straight off
the filesystem — no need to talk to the running server — and prints a concise
summary to stdout, which Claude Code surfaces back into the conversation.

Read-only: it never marks messages read or deletes them. Acknowledge via the
ack() tool. Rate-limited to every Nth Stop event to avoid nagging.

Identity resolution (first that works):
  1. $MCP_DISPATCH_AGENT_ID — the recommended setup (export it in your shell so
     the server, this hook, and Claude Code all agree on the id).
  2. The single live agent whose id starts with "<project>-" (the launcher's
     "<dir>-<pid>" scheme). Skipped if zero or several match — too ambiguous.

Wire into ~/.claude/settings.json:

  { "hooks": { "Stop": [ { "hooks": [
      { "type": "command", "command": "/abs/path/to/hooks/dispatch-peek.py" }
  ] } ] } }
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

EVERY_N = 5  # surface on every 5th Stop event


def _dispatch_dir() -> Path:
    raw = os.environ.get("MCP_DISPATCH_DIR") or os.environ.get("DISPATCH_DIR")
    if not raw:
        raw = "~/.config/mcp-dispatch/messages"
    return Path(os.path.expanduser(raw))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _resolve_agent_id(dispatch_dir: Path, cwd: str) -> str | None:
    explicit = (os.environ.get("MCP_DISPATCH_AGENT_ID") or "").strip().lower()
    if explicit:
        return explicit

    prefix = re.sub(r"^-*", "", re.sub(r"[^a-z0-9-]", "", os.path.basename(cwd).lower()))
    if not prefix:
        return None
    presence = dispatch_dir / ".presence"
    matches = []
    for pf in presence.glob("*.json"):
        try:
            data = json.loads(pf.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        aid = data.get("agent_id", "")
        if aid.startswith(f"{prefix}-") and _pid_alive(data.get("pid", -1)):
            matches.append(aid)
    return matches[0] if len(matches) == 1 else None


def _rate_limited(agent_id: str) -> bool:
    """Return True if this invocation should be skipped (not the Nth)."""
    state_dir = Path(os.path.expanduser("~/.cache/mcp-dispatch"))
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False  # can't track — don't suppress
    key = hashlib.md5(agent_id.encode(), usedforsecurity=False).hexdigest()[:8]
    counter = state_dir / f"stop-{key}.txt"
    try:
        count = int(counter.read_text()) + 1
    except (OSError, ValueError):
        count = 1
    try:
        counter.write_text(str(count))
    except OSError:
        pass
    return count % EVERY_N != 0


def _expired(msg: dict) -> bool:
    ttl = msg.get("ttl")
    if not ttl or ttl <= 0 or msg.get("must_read"):
        return False
    try:
        t = time.strptime(msg.get("timestamp", ""), "%Y-%m-%dT%H:%M:%SZ")
        sent = calendar.timegm(t)  # struct_time is UTC
    except (ValueError, TypeError):
        return False
    return time.time() > sent + ttl


def _pending(dispatch_dir: Path, agent_id: str) -> list[dict]:
    inbox = dispatch_dir / agent_id
    out = []
    for f in sorted(inbox.glob("*.json")):
        try:
            msg = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if msg.get("state", "pending") == "pending" and not _expired(msg):
            out.append(msg)
    return out


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    cwd = payload.get("cwd") or os.getcwd()

    dispatch_dir = _dispatch_dir()
    if not dispatch_dir.is_dir():
        return 0

    agent_id = _resolve_agent_id(dispatch_dir, cwd)
    if not agent_id:
        return 0
    if _rate_limited(agent_id):
        return 0

    pending = _pending(dispatch_dir, agent_id)
    if not pending:
        return 0

    lines = [f"[mcp-dispatch] {len(pending)} unread message(s) for {agent_id}:"]
    for m in pending[:10]:
        pri = "!" if m.get("priority") == "urgent" else "-"
        preview = (m.get("content", "") or "")[:100].replace("\n", " ")
        lines.append(f"  {pri} from {m.get('from', '?')}: {preview}  (id {m.get('id', '?')})")
    lines.append("Use peek() to read in full and ack() when handled.")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
