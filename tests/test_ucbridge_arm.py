"""hooks/dispatch-ucbridge-arm.py — the native-bridge auto-start hook.

Mirrors test_gitsync_arm.py exactly, one bridge later. Run as a subprocess (it's
a hook, not importable); only the *opt-out* paths are exercised, which return
before the daemon spawn — so these tests never leave a detached daemon running.
"gitsync.log"/"ucbridge.log" being written is a clean proxy for "reached the spawn".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARM = REPO_ROOT / "hooks" / "dispatch-ucbridge-arm.py"


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
    return (state_dir / "ucbridge.log").exists()


def test_bridge_disabled_does_not_spawn(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text("")  # no [bridge] section -> not configured
    proc = _run(dispatch_dir=dispatch_dir, state_dir=state_dir, config_path=cfg)
    assert proc.returncode == 0
    assert not _spawned(state_dir)


def test_enabled_with_no_nicks_does_not_spawn(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[bridge]\nenabled = true\n")  # empty allowlist bridges nothing
    proc = _run(dispatch_dir=dispatch_dir, state_dir=state_dir, config_path=cfg)
    assert proc.returncode == 0
    assert not _spawned(state_dir)


def test_dispatch_table_auto_arm_false_opts_out(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text('[dispatch]\nauto_arm = false\n[bridge]\nenabled = true\nnicks = ["alice"]\n')
    proc = _run(dispatch_dir=dispatch_dir, state_dir=state_dir, config_path=cfg)
    assert proc.returncode == 0
    assert not _spawned(state_dir)  # opted out -> never reached the spawn


def test_no_auto_arm_env_opts_out(tmp_path):
    dispatch_dir, state_dir = _setup(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text('[bridge]\nenabled = true\nnicks = ["alice"]\n')
    proc = _run(
        dispatch_dir=dispatch_dir,
        state_dir=state_dir,
        config_path=cfg,
        extra_env={"MCP_DISPATCH_NO_AUTO_ARM": "1"},
    )
    assert proc.returncode == 0
    assert not _spawned(state_dir)
