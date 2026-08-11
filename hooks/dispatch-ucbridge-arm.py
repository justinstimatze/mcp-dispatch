#!/usr/bin/env python3
"""SessionStart hook: start the native-protocol bridge daemon, hands-free.

Mirrors hooks/dispatch-gitsync-arm.py exactly, one bridge later. The daemon
doesn't wake anyone — it just runs — so this hook can spawn it directly,
detached, the moment a session starts. It holds a host-level lock (one per
DISPATCH_DIR, see bin/dispatch-ucbridge's _host_lock_path), so spawning on
every SessionStart is safe: a second spawn finds the lock held and exits
immediately — including when the daemon is already running as a systemd
service, which makes this hook a harmless no-op there. By default it is also
presence-gated (exits when the host goes quiet) so it can't orphan, though
`[bridge] presence_gate = false` opts out of that.

This hook exists only inside Claude Code. A host running any other harness
never fires it and gets no daemon at all — those want
`dispatch-ucbridge service install` instead.

Gated on [bridge].enabled — does nothing unless the bridge is configured (set
`nicks = [...]` and `enabled = true`; see docs/native-bridge.md). Opt out
entirely with MCP_DISPATCH_NO_AUTO_ARM=1 or `auto_arm = false`.

Wire into ~/.claude/settings.json under SessionStart:

  { "type": "command", "command": "/abs/path/to/hooks/dispatch-ucbridge-arm.py" }
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

    raw_bridge = cfg.get("bridge")
    bridge = raw_bridge if isinstance(raw_bridge, dict) else {}
    if not (bridge.get("enabled") or os.environ.get("MCP_DISPATCH_BRIDGE_ENABLED")):
        return 0  # native bridge not configured — nothing to start
    if not bridge.get("nicks"):
        return 0  # enabled with an empty allowlist bridges nothing — don't spawn for it

    dispatch_dir = common.dispatch_dir(cfg)
    if not dispatch_dir.is_dir():
        return 0
    lock = common.state_dir() / f"ucbridge-{common.md5_key(str(dispatch_dir))}.lock"
    if common.flock_held(lock):
        return 0  # a daemon is already bridging this host

    daemon = Path(__file__).resolve().parent.parent / "bin" / "dispatch-ucbridge"
    log_dir = common.state_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "ucbridge.log", "a")  # noqa: SIM115 - handed to the child
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
        print(f"[ucbridge] could not start daemon: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
