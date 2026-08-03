"""The server telling its own session that nothing is listening for it.

The Stop hook is the normal guard against parking unarmed, but it resolves
identity by discovery and exits silently when that comes back ambiguous — it
runs in every session, including ones with no relay at all, so it cannot be
loud. The server has no such problem: it *is* the session. These cover the
notice it attaches to a tool result the model was already getting.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

import pytest
from conftest import load_server

import dispatch_common as common


@pytest.fixture
def held_arm_lock():
    """Hold an agent's arm lock for the test, the way a live watch would."""
    handles = []

    def _hold(agent_id: str, state: Path) -> Path:
        lock = common.arm_lock(agent_id, state)
        lock.parent.mkdir(parents=True, exist_ok=True)
        fh = lock.open("w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handles.append(fh)
        return lock

    yield _hold
    for fh in handles:
        fh.close()


def _state(server) -> Path:
    return Path(os.environ["MCP_DISPATCH_STATE_DIR"])


def test_an_unarmed_session_is_told_on_its_next_tool_result(server):
    out = server.peek_tool()
    assert "_arm_required" in out
    assert server.AGENT_ID in out["_arm_required"]
    assert "Monitor(" in out["_arm_required"]


def test_an_armed_session_is_not_told(server, held_arm_lock):
    held_arm_lock(server.AGENT_ID, _state(server))
    assert "_arm_required" not in server.peek_tool()


def test_an_unreadable_arm_state_is_not_treated_as_deaf(server):
    """`None` is not `False`. A lock we cannot open says nothing either way, and
    claiming a healthy session is deaf is the worse error."""
    lock = common.arm_lock(server.AGENT_ID, _state(server))
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    lock.chmod(0o000)
    try:
        if common.armed(server.AGENT_ID, _state(server)) is not None:
            pytest.skip("running as a uid that can read mode-000 files")
        assert "_arm_required" not in server.peek_tool()
    finally:
        lock.chmod(0o600)


def test_the_notice_repeats_at_most_once_per_interval(server):
    """A harness with no Monitor tool can never carry the instruction out, and
    without the cap the notice would ride every tool result for the whole
    session."""
    assert "_arm_required" in server.peek_tool()
    assert "_arm_required" not in server.peek_tool()
    assert "_arm_required" not in server.who_tool()


def test_the_notice_returns_once_the_interval_lapses(server):
    assert "_arm_required" in server.peek_tool()
    stamp = _state(server) / f"armnudge-{common.md5_key(server.AGENT_ID)}.txt"
    old = time.time() - common.ARM_NUDGE_INTERVAL - 1
    os.utime(stamp, (old, old))
    assert "_arm_required" in server.peek_tool()


def test_opting_out_silences_it(server_factory):
    srv = server_factory("alpha", extra_env={"MCP_DISPATCH_NO_AUTO_ARM": "1"})
    try:
        assert "_arm_required" not in srv.peek_tool()
    finally:
        os.environ.pop("MCP_DISPATCH_NO_AUTO_ARM", None)


@pytest.mark.parametrize("tool", ["peek", "who", "digest", "dispatch", "task"])
def test_every_tool_seam_carries_it(server_factory, tool):
    """peek, who and digest never went through `_with_pending` — they read the
    inbox themselves or not at all — so the notice needs its own wiring at each."""
    srv = server_factory("alpha")
    call = {
        "peek": lambda: srv.peek_tool(),
        "who": lambda: srv.who_tool(),
        "digest": lambda: srv.digest_tool(),
        "dispatch": lambda: srv.dispatch_tool("hi", target="alpha"),
        "task": lambda: srv.task_tool("list"),
    }[tool]
    assert "_arm_required" in call()


def test_who_reports_its_own_unarmed_state_not_just_everyone_elses(server):
    """`who()` lists other sessions' `armed`; the caller's own is the one it
    cannot see by looking outward."""
    out = server.who_tool()
    mine = [a for a in out["agents"] if a["agent_id"] == server.AGENT_ID]
    assert mine and mine[0]["armed"] is False
    assert "_arm_required" in out


# --- the shared instruction text -------------------------------------------


def test_hook_and_server_say_the_same_thing():
    """Two callers describe this condition. A session acting on one wording and
    then seeing another has no way to tell they are one problem."""
    text = common.arm_instruction("alpha", {}, Path("/tmp/relay"))
    assert "alpha" in text
    assert str(common.waiter_path()) in text
    assert "--follow" in text


def test_the_instruction_warns_when_the_git_bridge_is_down(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DISPATCH_STATE_DIR", str(tmp_path / "state"))
    text = common.arm_instruction("alpha", {"git": {"enabled": True}}, tmp_path / "relay")
    assert "NOT running" in text


def test_the_instruction_is_quiet_about_a_bridge_nobody_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DISPATCH_STATE_DIR", str(tmp_path / "state"))
    text = common.arm_instruction("alpha", {}, tmp_path / "relay")
    assert "git bridge" not in text


def test_the_rate_limiter_stamps_when_it_says_yes(tmp_path):
    state = tmp_path / "state"
    assert common.arm_nudge_due("alpha", 600, state) is True
    assert common.arm_nudge_due("alpha", 600, state) is False
    assert (state / f"armnudge-{common.md5_key('alpha')}.txt").exists()


def test_the_rate_limiter_is_per_agent(tmp_path):
    state = tmp_path / "state"
    assert common.arm_nudge_due("alpha", 600, state) is True
    assert common.arm_nudge_due("beta", 600, state) is True


def test_an_unwritable_state_dir_keeps_asking(tmp_path):
    """Unable to remember having asked, we ask again: the notice is advisory text
    on a result the caller wanted anyway, and repeating beats going quiet about a
    session nothing can reach."""
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o500)
    try:
        if os.access(state, os.W_OK):
            pytest.skip("running as a uid that can write read-only dirs")
        assert common.arm_nudge_due("alpha", 600, state) is True
        assert common.arm_nudge_due("alpha", 600, state) is True
    finally:
        state.chmod(0o700)


def test_a_session_that_never_asks_is_never_nudged(tmp_path):
    """The seam is a tool result, so a session doing no dispatch work pays
    nothing — and equally, hears nothing. This is what the supervisor's sweep is
    for and why the nudge does not replace it."""
    srv = load_server(tmp_path / "messages", "alpha")
    assert not (
        Path(os.environ["MCP_DISPATCH_STATE_DIR"]) / f"armnudge-{common.md5_key(srv.AGENT_ID)}.txt"
    ).exists()
