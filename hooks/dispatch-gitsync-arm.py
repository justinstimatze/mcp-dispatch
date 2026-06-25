#!/usr/bin/env python3
"""SessionStart hook: start the cross-host git-sync daemon, hands-free.

Unlike dispatch-wait (which must be launched by the *model* because only the model
can register a wake-capable background task), the git-sync daemon doesn't wake
anyone — it just runs. So this hook can spawn it directly, detached, the moment a
session starts. The daemon holds a host-level lock (one per DISPATCH_DIR) and is
presence-gated (exits when the host goes quiet), so spawning on every SessionStart
is safe: a second spawn finds the lock held and exits immediately.

Gated on [git].enabled — does nothing unless cross-host comms are configured (run
`dispatch-gitsync init <repo>` once to set that up). Opt out entirely with
MCP_DISPATCH_NO_AUTO_ARM=1 or `auto_arm = false`.

Wire into ~/.claude/settings.json under SessionStart:

  { "type": "command", "command": "/abs/path/to/hooks/dispatch-gitsync-arm.py" }
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


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
            return tomllib.load(f)
    except Exception:
        return {}


def _dispatch_dir(cfg: dict) -> Path:
    sub = cfg.get("dispatch") if isinstance(cfg.get("dispatch"), dict) else {}
    raw = (
        os.environ.get("MCP_DISPATCH_DIR")
        or os.environ.get("DISPATCH_DIR")
        or cfg.get("dispatch_dir")
        or sub.get("dispatch_dir")
        or "~/.config/mcp-dispatch/messages"
    )
    return Path(os.path.expanduser(str(raw)))


def _state_dir() -> Path:
    raw = os.environ.get("MCP_DISPATCH_STATE_DIR") or "~/.cache/mcp-dispatch"
    return Path(os.path.expanduser(raw))


def _host_lock_held(dispatch_dir: Path) -> bool:
    key = hashlib.md5(str(dispatch_dir).encode(), usedforsecurity=False).hexdigest()[:8]
    pf = _state_dir() / f"gitsync-{key}.lock"
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


def main() -> int:
    # Drain stdin (hook payload) so the harness sees a clean read; we don't need it.
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        pass

    if _truthy(os.environ.get("MCP_DISPATCH_NO_AUTO_ARM")):
        return 0
    cfg = _config()
    if cfg.get("auto_arm") is False:
        return 0

    git = cfg.get("git") if isinstance(cfg.get("git"), dict) else {}
    if not (git.get("enabled") or os.environ.get("MCP_DISPATCH_GIT_ENABLED")):
        return 0  # cross-host comms not configured — nothing to start

    dispatch_dir = _dispatch_dir(cfg)
    if not dispatch_dir.is_dir():
        return 0
    if _host_lock_held(dispatch_dir):
        return 0  # a daemon is already mirroring this host

    daemon = Path(__file__).resolve().parent.parent / "bin" / "dispatch-gitsync"
    log_dir = _state_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "gitsync.log", "a")  # noqa: SIM115 - handed to the child
    except OSError:
        log = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        subprocess.Popen(
            [sys.executable, str(daemon)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach: survives this session's process group
            env=os.environ.copy(),
        )
    except OSError as e:
        print(f"[gitsync] could not start daemon: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
