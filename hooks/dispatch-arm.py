#!/usr/bin/env python3
"""SessionStart + Stop hook: keep an event-driven waiter armed, hands-free.

The problem this closes: a *parked* Claude Code session (model idle) can only be
woken by the harness, and the harness only wakes on a `run_in_background` task
exiting (see bin/dispatch-wait). A hook cannot launch that task in a wake-capable
way — only the model can, via the Bash tool. So this hook does the next best
thing: it *nudges the model* to (re)launch dispatch-wait whenever no waiter is
armed, removing the human from the loop.

  - SessionStart: if the relay+agent resolve and nothing is armed, inject an
    instruction telling the model to launch dispatch-wait as a background task.
  - Stop: same check, but *block* the stop (the model must not park unarmed) so
    the model re-arms before going idle. Self-terminating — once a waiter holds
    the lock the next Stop is silent — and capped so a failing launch can never
    wedge the session into an endless block loop.

"Armed" is detected by probing the per-agent flock that a live dispatch-wait
holds (hooks/.. and bin/dispatch-wait agree on the path). flock is uid-agnostic
and pid-reuse-immune, and the kernel frees it the instant the waiter dies.

Opt out with MCP_DISPATCH_NO_AUTO_ARM=1 or `auto_arm = false` in the config.

Identity + dispatch-dir resolution mirrors hooks/dispatch-peek.py exactly.

Wire into ~/.claude/settings.json under BOTH SessionStart and Stop:

  { "type": "command", "command": "/abs/path/to/hooks/dispatch-arm.py" }
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
from pathlib import Path

MAX_BLOCKS = 2  # consecutive Stop blocks before degrading, so a bad launch can't wedge


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on"}


def _config() -> dict:
    cfg = os.environ.get("MCP_DISPATCH_CONFIG") or os.path.expanduser(
        "~/.config/mcp-dispatch/config.toml"
    )
    if not os.path.exists(cfg):
        return {}
    try:
        import tomllib

        with open(cfg, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}
    # Match dispatch-wait / dispatch-peek: top-level keys win, a [dispatch] table
    # is only a fallback for the same keys.
    sub = data.get("dispatch")
    merged = dict(sub) if isinstance(sub, dict) else {}
    for k, v in data.items():
        if k != "dispatch":
            merged[k] = v
    return merged


def _dispatch_dir(cfg: dict) -> Path:
    raw = (
        os.environ.get("MCP_DISPATCH_DIR")
        or os.environ.get("DISPATCH_DIR")
        or cfg.get("dispatch_dir")
        or "~/.config/mcp-dispatch/messages"
    )
    return Path(os.path.expanduser(str(raw)))


def _state_dir() -> Path:
    raw = os.environ.get("MCP_DISPATCH_STATE_DIR") or "~/.cache/mcp-dispatch"
    return Path(os.path.expanduser(raw))


def _key(agent_id: str) -> str:
    return hashlib.md5(agent_id.encode(), usedforsecurity=False).hexdigest()[:8]


def _presence_is_live(pf: Path) -> bool:
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
        if aid.startswith(f"{prefix}-") and _presence_is_live(pf):
            matches.append(aid)
    return matches[0] if len(matches) == 1 else None


def _is_armed(agent_id: str) -> bool:
    """True if a live dispatch-wait holds the per-agent lock. We probe by trying
    to take it: success (we got it) means nobody's armed — release and report."""
    lock = _state_dir() / f"wait-{_key(agent_id)}.lock"
    try:
        fh = open(lock, "a+")
    except OSError:
        # Can't even open the lock file — assume unarmed so we still nudge.
        return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def _block_count(agent_id: str, *, reset: bool = False, bump: bool = False) -> int:
    f = _state_dir() / f"armblock-{_key(agent_id)}.txt"
    if reset:
        try:
            f.unlink()
        except OSError:
            pass
        return 0
    try:
        n = int(f.read_text())
    except (OSError, ValueError):
        n = 0
    if bump:
        n += 1
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(str(n))
        except OSError:
            pass
    return n


def _waiter_path() -> str:
    return str(Path(__file__).resolve().parent.parent / "bin" / "dispatch-wait")


def _instruction(agent_id: str) -> str:
    return (
        f"[mcp-dispatch] No event-driven waiter is armed for '{agent_id}'. While "
        "parked you won't see new messages until one is. Launch the waiter NOW as "
        "a background shell task (Bash tool, run_in_background: true), then carry "
        f"on:\n\n    {_waiter_path()}\n\n"
        "It blocks with zero tokens until a qualifying message lands, exits to "
        "wake you, and this hook re-arms automatically after you handle each one."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    event = payload.get("hook_event_name") or ""
    cwd = payload.get("cwd") or os.getcwd()

    if _truthy(os.environ.get("MCP_DISPATCH_NO_AUTO_ARM")):
        return 0
    cfg = _config()
    if cfg.get("auto_arm") is False:
        return 0

    dispatch_dir = _dispatch_dir(cfg)
    if not dispatch_dir.is_dir():
        return 0
    agent_id = _resolve_agent_id(dispatch_dir, cwd)
    if not agent_id:
        return 0

    if _is_armed(agent_id):
        _block_count(agent_id, reset=True)  # armed → clear the wedge guard
        return 0

    # Unarmed. SessionStart just injects; Stop must block so we don't park unarmed.
    if event == "Stop":
        if _block_count(agent_id) >= MAX_BLOCKS:
            # Launch keeps failing — degrade to a soft note rather than wedge.
            print(_instruction(agent_id))
            return 0
        _block_count(agent_id, bump=True)
        print(json.dumps({"decision": "block", "reason": _instruction(agent_id)}))
        return 0

    print(_instruction(agent_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
