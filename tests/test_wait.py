"""bin/dispatch-wait lifetime behaviour: presence-gated exit + standalone cap.

The default (no time cap) ties the waiter's life to its agent's presence flock,
so it exits when the session ends instead of orphaning. Exercised as a subprocess
with the scratch dir redirected via MCP_DISPATCH_STATE_DIR and the standalone
fallback shrunk via MCP_DISPATCH_STANDALONE_FALLBACK so nothing waits 30 minutes.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WAIT = REPO_ROOT / "bin" / "dispatch-wait"


def _env(dispatch_dir, state_dir, agent="alice", **extra):
    return {
        "MCP_DISPATCH_AGENT_ID": agent,
        "MCP_DISPATCH_DIR": str(dispatch_dir),
        "MCP_DISPATCH_STATE_DIR": str(state_dir),
        "MCP_DISPATCH_CONFIG": str(dispatch_dir.parent / "no-such-config.toml"),
        "PATH": "/usr/bin:/bin",
        **extra,
    }


def _launch(dispatch_dir, state_dir, *args, **extra):
    return subprocess.Popen(
        [sys.executable, str(WAIT), "--interval", "0.2", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_env(dispatch_dir, state_dir, **extra),
    )


def _dirs(tmp_path, agent="alice"):
    dispatch_dir = tmp_path / "messages"
    (dispatch_dir / agent).mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return dispatch_dir, state_dir


def _hold_presence(dispatch_dir, agent="alice"):
    pres = dispatch_dir / ".presence"
    pres.mkdir(parents=True, exist_ok=True)
    pf = pres / f"{agent}.json"
    pf.write_text(json.dumps({"agent_id": agent, "channels": []}))
    fh = open(pf, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # simulate the live server
    return fh


def test_presence_drop_exits(tmp_path):
    """A gated waiter blocks while presence is held, then exits when it drops."""
    dispatch_dir, state_dir = _dirs(tmp_path)
    holder = _hold_presence(dispatch_dir)
    proc = _launch(dispatch_dir, state_dir)
    try:
        time.sleep(0.6)
        assert proc.poll() is None  # live presence → still blocking
        holder.close()  # session "ends" → presence flock released
        out, _ = proc.communicate(timeout=5)
        assert proc.returncode == 0
        assert "presence dropped" in out
    finally:
        if proc.poll() is None:
            proc.kill()
        if not holder.closed:
            holder.close()


def test_standalone_falls_back_to_cap(tmp_path):
    """No presence to gate on → default 0 must not block forever; the fallback
    cap makes it exit cleanly."""
    dispatch_dir, state_dir = _dirs(tmp_path)
    proc = _launch(dispatch_dir, state_dir, MCP_DISPATCH_STANDALONE_FALLBACK="0.5")
    out, _ = proc.communicate(timeout=5)
    assert proc.returncode == 0
    assert "within" in out  # hit the fallback cap rather than blocking forever


def test_explicit_cap_honoured_when_gated(tmp_path):
    """An explicit --max-lifetime still applies on top of presence gating."""
    dispatch_dir, state_dir = _dirs(tmp_path)
    holder = _hold_presence(dispatch_dir)
    proc = _launch(dispatch_dir, state_dir, "--max-lifetime", "0.5")
    try:
        out, _ = proc.communicate(timeout=5)
        assert proc.returncode == 0
        assert "within" in out  # timed out despite live presence
    finally:
        holder.close()
