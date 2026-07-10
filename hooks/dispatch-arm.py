#!/usr/bin/env python3
"""SessionStart + Stop hook: keep a message watch armed, hands-free.

The problem this closes: a *parked* Claude Code session (model idle) can only be
woken by the harness. A hook cannot start that wake source itself — only the
model can, by calling a tool. So this hook does the next best thing: it *nudges
the model* to start a persistent watch whenever none is armed, removing the human
from the loop.

The watch is `dispatch-wait --follow` run under the **Monitor** tool: Monitor
streams each stdout line as a wake event into the parked session, so ONE
registration covers the whole session and there is nothing to re-arm after each
message. (This replaced the older run_in_background waiter, which woke on task
*exit* and so had to be relaunched after every single message — the source of the
"not always armed" flakiness. Empirically confirmed: a Monitor event re-invokes an
idle session the same way a background-task exit does.)

  - SessionStart: if the relay+agent resolve and nothing is armed, inject an
    instruction telling the model to start the Monitor watch. Resolution is
    retried briefly to ride out the race with the server claiming presence.
  - Stop: same check, but *block* the stop (the model must not park unarmed) so
    the model arms before going idle. Self-terminating — once the watch holds the
    lock the next Stop is silent — and capped so a failing launch can never wedge
    the session; past the cap it warns loudly (desktop + text) instead of silently.

"Armed" is detected by probing the per-agent flock that a live `dispatch-wait
--follow` holds (hooks/.. and bin/dispatch-wait agree on the path). flock is
uid-agnostic and pid-reuse-immune, and the kernel frees it the instant the watch
dies — so a crashed watch is detected as unarmed and re-nudged.

Opt out with MCP_DISPATCH_NO_AUTO_ARM=1 or `auto_arm = false` in the config.

Identity + dispatch-dir resolution mirrors hooks/dispatch-peek.py exactly.

Wire into ~/.claude/settings.json under BOTH SessionStart and Stop:

  { "type": "command", "command": "/abs/path/to/hooks/dispatch-arm.py" }
"""

from __future__ import annotations

import json
import os
import re
import shlex

# subprocess only runs the opt-in, local-config notify_command as an argv list
# (no shell), mirroring server.py's notify path.
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dispatch_common as common  # noqa: E402

MAX_BLOCKS = 2  # consecutive Stop blocks before degrading, so a bad launch can't wedge
# On SessionStart the MCP server may not have claimed presence yet, so id
# resolution can momentarily fail. Retry *briefly* to catch that race — but kept
# short: the launcher exports MCP_DISPATCH_AGENT_ID only into the server's own
# process tree, not the hook's, so the hook always resolves by prefix-discovery
# and a non-dispatch session would otherwise pay this whole budget for nothing at
# every SessionStart. The Stop hook is the reliable arm point (presence always
# exists by then), so this start nudge only needs a cheap best-effort probe.
START_RESOLVE_TRIES = 3
START_RESOLVE_SLEEP = 0.2


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
        if aid.startswith(f"{prefix}-") and common.flock_held(pf):
            matches.append(aid)
    return matches[0] if len(matches) == 1 else None


def _is_armed(agent_id: str) -> bool:
    """True if a live ``dispatch-wait --follow`` holds this agent's arm lock."""
    return common.flock_held(common.state_dir() / f"wait-{common.md5_key(agent_id)}.lock")


def _block_count(agent_id: str, *, reset: bool = False, bump: bool = False) -> int:
    f = common.state_dir() / f"armblock-{common.md5_key(agent_id)}.txt"
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


def _git_liveness_note(cfg: dict, dispatch_dir: Path) -> str:
    """When cross-host git comms are enabled, tell the model whether the bridge
    daemon is actually pulling — an armed watch alone won't surface remote
    messages if the gitsync daemon isn't running."""
    raw = cfg.get("git")
    git = raw if isinstance(raw, dict) else {}
    if not (git.get("enabled") or os.environ.get("MCP_DISPATCH_GIT_ENABLED")):
        return ""
    lock = common.state_dir() / f"gitsync-{common.md5_key(str(dispatch_dir))}.lock"
    if common.flock_held(lock):  # daemon holds this flock while running
        return (
            "\n\nCross-host git bridge: LIVE — messages from other hosts arrive in "
            "this same inbox, so this one watch covers both local and remote."
        )
    return (
        "\n\n⚠ Cross-host git bridge is ENABLED but its daemon is NOT running — "
        "remote messages won't arrive until it is. Start it as a detached shell "
        f"task:\n\n    {Path(_waiter_path()).parent / 'dispatch-gitsync'}\n\n"
        "or check `dispatch-gitsync status`."
    )


def _instruction(agent_id: str, git_note: str = "") -> str:
    return (
        f"[mcp-dispatch] No message watch is armed for '{agent_id}'. While parked "
        "you won't see new messages until one is. Start a PERSISTENT watch NOW with "
        "the Monitor tool (load it via ToolSearch first if it isn't already "
        "available) — one registration streams an event per incoming message for "
        "the whole session, so there is NOTHING to re-arm after each one:\n\n"
        f'    Monitor(command="{_waiter_path()} --follow", '
        f'description="dispatch inbox for {agent_id}", persistent=true, '
        "timeout_ms=3600000)\n\n"
        "It costs zero tokens while idle and wakes you the instant a qualifying "
        "message (local OR cross-host) lands. This hook goes quiet once the watch "
        "holds its lock." + git_note
    )


def _desktop_warn(cfg: dict, agent_id: str) -> None:
    """Best-effort loud alert when auto-arm keeps failing, so a wedged launch is
    never silent. Uses the same notify_command the server/waiter use."""
    cmd = cfg.get("notify_command")
    if not cmd:
        return
    msg = (
        f"mcp-dispatch: message watch NOT armed for {agent_id} after {MAX_BLOCKS} "
        "tries — incoming messages may be missed until it starts."
    )
    try:
        # argv list, no shell — same shape as server.py's notify_command call.
        subprocess.run(  # nosec B603
            [*shlex.split(str(cmd)), "--", msg],
            timeout=3,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    event = payload.get("hook_event_name") or ""
    cwd = payload.get("cwd") or os.getcwd()

    cfg = common.load_config()
    if common.auto_arm_disabled(cfg):
        return 0

    dispatch_dir = common.dispatch_dir(cfg)
    if not dispatch_dir.is_dir():
        return 0
    agent_id = _resolve_agent_id(dispatch_dir, cwd)
    # Close the SessionStart presence race: the server may not have registered
    # presence at the instant this hook fires, so retry briefly before giving up.
    if not agent_id and event == "SessionStart":
        for _ in range(START_RESOLVE_TRIES):
            time.sleep(START_RESOLVE_SLEEP)
            agent_id = _resolve_agent_id(dispatch_dir, cwd)
            if agent_id:
                break
    if not agent_id:
        return 0

    if _is_armed(agent_id):
        _block_count(agent_id, reset=True)  # armed → clear the wedge guard
        return 0

    git_note = _git_liveness_note(cfg, dispatch_dir)

    # Unarmed. SessionStart just injects; Stop must block so we don't park unarmed.
    if event == "Stop":
        if _block_count(agent_id) >= MAX_BLOCKS:
            # Launch keeps failing — don't wedge, but don't go silent either: warn
            # loudly (desktop + text) so a persistently-unarmed session is visible.
            _desktop_warn(cfg, agent_id)
            print(_instruction(agent_id, git_note))
            return 0
        _block_count(agent_id, bump=True)
        print(json.dumps({"decision": "block", "reason": _instruction(agent_id, git_note)}))
        return 0

    print(_instruction(agent_id, git_note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
