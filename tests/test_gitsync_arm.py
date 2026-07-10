"""hooks/dispatch-gitsync-arm.py — the git-daemon auto-start hook.

Run as a subprocess (it's a hook, not importable). We only exercise the *opt-out*
paths, which return before the daemon spawn — so the tests never leave a detached
daemon running. The hook opens `gitsync.log` immediately before Popen, so
"gitsync.log was not created" is a clean proxy for "did not reach the spawn".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARM = REPO_ROOT / "hooks" / "dispatch-gitsync-arm.py"


def _run(*, dispatch_dir, state_dir, config_path, extra_env=None):
    env = {
        "MCP_DISPATCH_DIR": str(dispatch_dir),
        "MCP_DISPATCH_STATE_DIR": str(state_dir),
        "MCP_DISPATCH_CONFIG": str(config_path),
        "PATH": "/usr/bin:/bin",
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, str(ARM)],
        input=json.dumps({"hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        env=env,
    )


def _setup(tmp_path):
    dispatch_dir = tmp_path / "messages"
    dispatch_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return dispatch_dir, state_dir


def _spawned(state_dir) -> bool:
    return (state_dir / "gitsync.log").exists()


def test_git_disabled_does_not_spawn(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text("")  # no [git] section → cross-host not configured
    proc = _run(dispatch_dir=dispatch_dir, state_dir=state_dir, config_path=cfg)
    assert proc.returncode == 0
    assert not _spawned(state_dir)


def test_dispatch_table_auto_arm_false_opts_out(tmp_path):
    """The drift fix, end to end: [git].enabled=true but auto_arm=false under a
    [dispatch] table must opt out. The old raw-config read missed this and would
    have spawned the daemon."""
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[dispatch]\nauto_arm = false\n[git]\nenabled = true\n")
    proc = _run(dispatch_dir=dispatch_dir, state_dir=state_dir, config_path=cfg)
    assert proc.returncode == 0
    assert not _spawned(state_dir)  # opted out → never reached the spawn


def test_no_auto_arm_env_opts_out(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[git]\nenabled = true\n")
    proc = _run(
        dispatch_dir=dispatch_dir,
        state_dir=state_dir,
        config_path=cfg,
        extra_env={"MCP_DISPATCH_NO_AUTO_ARM": "1"},
    )
    assert proc.returncode == 0
    assert not _spawned(state_dir)
