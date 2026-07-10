"""Unit tests for hooks/_dispatch_common.py — the shared hook plumbing.

Its whole reason to exist is that the two arm hooks had drifted; the key
regression it fixes is that gitsync-arm ignored `[dispatch].auto_arm`. These
tests pin the unified behavior at the source so neither hook can drift again.
"""

from __future__ import annotations

import fcntl
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

import _dispatch_common as common  # noqa: E402


def test_flat_top_level_wins_over_dispatch_table():
    cfg = {"notify_on": "all", "dispatch": {"notify_on": "direct"}}
    assert common.flat(cfg, "notify_on") == "all"


def test_flat_falls_back_to_dispatch_table():
    cfg = {"dispatch": {"dispatch_dir": "/x"}}
    assert common.flat(cfg, "dispatch_dir") == "/x"


def test_flat_missing_key_is_none():
    assert common.flat({"dispatch": {}}, "nope") is None


def test_auto_arm_disabled_top_level():
    assert common.auto_arm_disabled({"auto_arm": False}) is True


def test_auto_arm_disabled_in_dispatch_table():
    # THE drift fix: gitsync-arm used to read raw and miss this nested opt-out.
    assert common.auto_arm_disabled({"dispatch": {"auto_arm": False}}) is True


def test_auto_arm_enabled_by_default():
    assert common.auto_arm_disabled({}) is False
    assert common.auto_arm_disabled({"auto_arm": True}) is False


def test_auto_arm_env_optout(monkeypatch):
    monkeypatch.setenv("MCP_DISPATCH_NO_AUTO_ARM", "1")
    assert common.auto_arm_disabled({}) is True


def test_dispatch_dir_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_DISPATCH_DIR", str(tmp_path))
    assert common.dispatch_dir({"dispatch_dir": "/ignored"}) == tmp_path


def test_dispatch_dir_from_config(monkeypatch):
    monkeypatch.delenv("MCP_DISPATCH_DIR", raising=False)
    monkeypatch.delenv("DISPATCH_DIR", raising=False)
    assert common.dispatch_dir({"dispatch": {"dispatch_dir": "/relay"}}) == Path("/relay")


def test_flock_held_false_when_missing(tmp_path):
    assert common.flock_held(tmp_path / "nope.lock") is False


def test_flock_held_false_for_unheld_leftover(tmp_path):
    # A stale leftover file nobody holds must read as not-held (the 100s of stale
    # wait-*.lock files must never be mistaken for a live waiter).
    leftover = tmp_path / "wait.lock"
    leftover.write_text("")
    assert common.flock_held(leftover) is False


def test_flock_held_true_when_locked(tmp_path):
    lock = tmp_path / "held.lock"
    lock.write_text("")
    holder = open(lock, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert common.flock_held(lock) is True
    finally:
        holder.close()


def test_flock_held_read_only_probe_does_not_create(tmp_path):
    # The probe must not create the file (old _is_armed opened a+ and left one).
    missing = tmp_path / "ghost.lock"
    common.flock_held(missing)
    assert not missing.exists()
