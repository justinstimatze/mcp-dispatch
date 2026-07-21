#!/usr/bin/env python3
"""SessionStart hook: start the cross-host git-sync daemon, hands-free.

Unlike dispatch-wait (which must be launched by the *model* because only the model
can register a wake-capable background task), the git-sync daemon doesn't wake
anyone — it just runs. So this hook can spawn it directly, detached, the moment a
session starts. The daemon holds a host-level lock (one per DISPATCH_DIR), so
spawning on every SessionStart is safe: a second spawn finds the lock held and
exits immediately — including when the daemon is already running as a systemd
service, which makes this hook a harmless no-op there. By default it is also
presence-gated (exits when the host goes quiet) so it can't orphan, though
`[git] presence_gate = false` opts out of that.

This hook exists only inside Claude Code. A host running any other harness
(openclaw, Hermes, a script, cron) never fires it and gets no daemon at all —
those want `dispatch-gitsync service install` instead.

Gated on [git].enabled — does nothing unless cross-host comms are configured (run
`dispatch-gitsync init <repo>` once to set that up). Opt out entirely with
MCP_DISPATCH_NO_AUTO_ARM=1 or `auto_arm = false`.

Wire into ~/.claude/settings.json under SessionStart:

  { "type": "command", "command": "/abs/path/to/hooks/dispatch-gitsync-arm.py" }
"""

from __future__ import annotations

import json
import os

# subprocess only launches our own daemon by fixed path (no shell, no untrusted
# args); detached so it outlives this hook.
import subprocess  # nosec B404
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dispatch_common as common  # noqa: E402


def main() -> int:
    # Drain stdin (hook payload) so the harness sees a clean read; we don't need it.
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        pass

    cfg = common.load_config()
    if common.auto_arm_disabled(cfg):
        return 0

    raw_git = cfg.get("git")
    git = raw_git if isinstance(raw_git, dict) else {}
    if not (git.get("enabled") or os.environ.get("MCP_DISPATCH_GIT_ENABLED")):
        return 0  # cross-host comms not configured — nothing to start

    dispatch_dir = common.dispatch_dir(cfg)
    if not dispatch_dir.is_dir():
        return 0
    lock = common.state_dir() / f"gitsync-{common.md5_key(str(dispatch_dir))}.lock"
    if common.flock_held(lock):
        return 0  # a daemon is already mirroring this host

    daemon = Path(__file__).resolve().parent.parent / "bin" / "dispatch-gitsync"
    log_dir = common.state_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "gitsync.log", "a")  # noqa: SIM115 - handed to the child
    except OSError:
        log = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        subprocess.Popen(  # nosec B603
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
