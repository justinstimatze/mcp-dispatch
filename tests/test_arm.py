"""hooks/dispatch-arm.py — the hands-free re-arm gate.

Exercised as a subprocess (it's a hook, not an importable module), with the
agent id pinned via env so we don't need a live presence file, and the scratch
dir redirected via MCP_DISPATCH_STATE_DIR so the arm lock / wedge counter land
in tmp instead of ~/.cache.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARM = REPO_ROOT / "hooks" / "dispatch-arm.py"


def _run(event, *, dispatch_dir, state_dir, agent="alice", config_path=None):
    env = {
        "MCP_DISPATCH_AGENT_ID": agent,
        "MCP_DISPATCH_DIR": str(dispatch_dir),
        "MCP_DISPATCH_STATE_DIR": str(state_dir),
        "MCP_DISPATCH_CONFIG": str(config_path or dispatch_dir.parent / "no-such-config.toml"),
        "PATH": "/usr/bin:/bin",
    }
    proc = subprocess.run(
        [sys.executable, str(ARM)],
        input=json.dumps({"hook_event_name": event, "cwd": str(dispatch_dir.parent)}),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc


def _setup(tmp_path, agent="alice"):
    dispatch_dir = tmp_path / "messages"
    (dispatch_dir / agent).mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return dispatch_dir, state_dir


def _hold_arm_lock(state_dir, agent="alice"):
    key = hashlib.md5(agent.encode(), usedforsecurity=False).hexdigest()[:8]
    fh = open(state_dir / f"wait-{key}.lock", "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh  # caller holds it to simulate a live waiter


def test_stop_unarmed_blocks(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    proc = _run("Stop", dispatch_dir=dispatch_dir, state_dir=state_dir)
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    assert "dispatch-wait" in out["reason"]


def test_stop_armed_is_silent(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    fh = _hold_arm_lock(state_dir)
    try:
        proc = _run("Stop", dispatch_dir=dispatch_dir, state_dir=state_dir)
    finally:
        fh.close()
    assert proc.stdout.strip() == ""


def test_session_start_unarmed_injects_without_blocking(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    proc = _run("SessionStart", dispatch_dir=dispatch_dir, state_dir=state_dir)
    assert "dispatch-wait" in proc.stdout
    assert "decision" not in proc.stdout  # plain context, not a block


def test_stop_block_cap_degrades(tmp_path):
    """After MAX_BLOCKS consecutive unarmed Stops, stop emitting block decisions
    so a failing launch can never wedge the session."""
    dispatch_dir, state_dir = _setup(tmp_path)
    blocked = 0
    for _ in range(5):
        proc = _run("Stop", dispatch_dir=dispatch_dir, state_dir=state_dir)
        try:
            if json.loads(proc.stdout).get("decision") == "block":
                blocked += 1
        except json.JSONDecodeError:
            pass  # degraded plain-text note
    assert blocked == 2  # MAX_BLOCKS, then degrades


def test_armed_resets_wedge_counter(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    # Two unarmed Stops exhaust the block budget.
    _run("Stop", dispatch_dir=dispatch_dir, state_dir=state_dir)
    _run("Stop", dispatch_dir=dispatch_dir, state_dir=state_dir)
    # A waiter arms → an arm check resets the counter.
    fh = _hold_arm_lock(state_dir)
    try:
        _run("Stop", dispatch_dir=dispatch_dir, state_dir=state_dir)
    finally:
        fh.close()
    # Budget restored: the next unarmed Stop blocks again.
    proc = _run("Stop", dispatch_dir=dispatch_dir, state_dir=state_dir)
    assert json.loads(proc.stdout)["decision"] == "block"


def test_config_auto_arm_false_opts_out(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text("auto_arm = false\n")
    proc = _run("Stop", dispatch_dir=dispatch_dir, state_dir=state_dir, config_path=cfg)
    assert proc.stdout.strip() == ""


def test_top_level_auto_arm_overrides_section(tmp_path):
    """Repo convention: a top-level key wins over the same key in [dispatch]."""
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text("auto_arm = false\n[dispatch]\nauto_arm = true\n")
    proc = _run("Stop", dispatch_dir=dispatch_dir, state_dir=state_dir, config_path=cfg)
    assert proc.stdout.strip() == ""  # top-level false wins → opted out, silent


def test_session_start_nudges_monitor(tmp_path):
    """The nudge now points at the Monitor tool + --follow, not run_in_background."""
    dispatch_dir, state_dir = _setup(tmp_path)
    proc = _run("SessionStart", dispatch_dir=dispatch_dir, state_dir=state_dir)
    assert "Monitor(" in proc.stdout
    assert "--follow" in proc.stdout


def test_git_enabled_bridge_down_warns(tmp_path):
    """git enabled but no daemon lock held → the nudge flags the bridge as down."""
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[git]\nenabled = true\n")
    proc = _run("SessionStart", dispatch_dir=dispatch_dir, state_dir=state_dir, config_path=cfg)
    out = proc.stdout.lower()
    assert "bridge" in out and "not running" in out


def test_git_enabled_bridge_live_note(tmp_path):
    """git enabled and the gitsync daemon lock is held → the nudge says LIVE."""
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[git]\nenabled = true\n")
    key = hashlib.md5(str(dispatch_dir).encode(), usedforsecurity=False).hexdigest()[:8]
    lock = open(state_dir / f"gitsync-{key}.lock", "a+")  # noqa: SIM115
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # simulate a live daemon
    try:
        proc = _run("SessionStart", dispatch_dir=dispatch_dir, state_dir=state_dir, config_path=cfg)
    finally:
        lock.close()
    assert "live" in proc.stdout.lower()


def test_opt_out_env(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    env_extra = {"MCP_DISPATCH_NO_AUTO_ARM": "1"}
    proc = subprocess.run(
        [sys.executable, str(ARM)],
        input=json.dumps({"hook_event_name": "Stop", "cwd": str(dispatch_dir.parent)}),
        capture_output=True,
        text=True,
        env={
            "MCP_DISPATCH_AGENT_ID": "alice",
            "MCP_DISPATCH_DIR": str(dispatch_dir),
            "MCP_DISPATCH_STATE_DIR": str(state_dir),
            "MCP_DISPATCH_CONFIG": str(dispatch_dir.parent / "no-such-config.toml"),
            "PATH": "/usr/bin:/bin",
            **env_extra,
        },
    )
    assert proc.stdout.strip() == ""
