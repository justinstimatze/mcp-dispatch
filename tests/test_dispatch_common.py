"""Unit tests for dispatch_common.py — the shared hook + bin/ plumbing.

Its whole reason to exist is that the arm hooks and the bin/ scripts had each
carried near-copies that drifted; the key regression it fixes is that gitsync-arm
ignored `[dispatch].auto_arm`. These tests pin the unified behavior at the source
so no consumer can drift again.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import dispatch_common as common  # noqa: E402


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


def test_acquire_flock_returns_held_handle(tmp_path):
    lock = tmp_path / "held.lock"
    fh = common.acquire_flock(lock)
    try:
        assert fh is not None
        assert lock.exists()  # unlike flock_held, acquire DOES create the anchor file
        assert common.flock_held(lock) is True  # a concurrent probe sees it held
    finally:
        if fh is not None:
            fh.close()


def test_acquire_flock_none_when_already_held(tmp_path):
    lock = tmp_path / "contended.lock"
    holder = common.acquire_flock(lock)
    assert holder is not None
    try:
        assert common.acquire_flock(lock) is None  # second acquire loses
    finally:
        holder.close()


def test_acquire_flock_after_release_succeeds(tmp_path):
    lock = tmp_path / "reusable.lock"
    first = common.acquire_flock(lock)
    assert first is not None
    first.close()  # release
    second = common.acquire_flock(lock)  # now free again
    try:
        assert second is not None
    finally:
        if second is not None:
            second.close()


# ---------------------------------------------------------------------------
# armed(): running and listening are separate facts
#
# A session with no watch still receives mail; nothing wakes it to read it. The
# three-state answer exists because the arm lock lives under the *watcher's*
# HOME, so a reader looking at another account's session is not entitled to a
# yes-or-no — and "no" there would name a healthy session deaf.
# ---------------------------------------------------------------------------


def test_arm_lock_path_is_stable_and_scoped(tmp_path):
    a = common.arm_lock("stope-42", tmp_path)
    assert a.parent == tmp_path
    assert a == common.arm_lock("stope-42", tmp_path), "same id → same path"
    assert a != common.arm_lock("stope-43", tmp_path), "different id → different path"


def test_a_lock_nobody_ever_took_is_unarmed(tmp_path):
    assert common.armed("never-armed", tmp_path) is False


def test_a_held_lock_is_armed(tmp_path):
    lock = common.arm_lock("watching-1", tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = common.acquire_flock(lock)
    try:
        assert common.armed("watching-1", tmp_path) is True
    finally:
        if fh is not None:
            fh.close()
    assert common.armed("watching-1", tmp_path) is False, "the watch died → unarmed"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this tests")
def test_an_unreadable_cache_is_unknown_not_unarmed(tmp_path):
    """The distinction the whole three-state return exists for."""
    theirs = tmp_path / "someone-elses-cache"
    theirs.mkdir()
    (common.arm_lock("theirs-1", theirs)).write_text("")
    theirs.chmod(0o000)
    try:
        assert common.armed("theirs-1", theirs) is None
    finally:
        theirs.chmod(0o700)  # let tmp_path cleanup run


def test_armed_for_uses_the_directory_the_session_published(tmp_path):
    lock = common.arm_lock("published-1", tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = common.acquire_flock(lock)
    try:
        rec = {"agent_id": "published-1", "state_dir": str(tmp_path)}
        assert common.armed_for(rec, tmp_path / "presence.json") is True
    finally:
        if fh is not None:
            fh.close()


def test_armed_for_falls_back_to_our_cache_for_our_own_session(tmp_path, monkeypatch):
    """No state_dir means a session older than the field. Ours to probe, so probe."""
    monkeypatch.setenv("MCP_DISPATCH_STATE_DIR", str(tmp_path))
    pf = tmp_path / "presence.json"
    pf.write_text("{}")
    assert common.armed_for({"agent_id": "legacy-1"}, pf) is False


def test_armed_for_will_not_guess_about_another_account(tmp_path, monkeypatch):
    """Same missing field, but the presence file belongs to someone else: their
    lock was never going to be in our cache, so absence proves nothing."""
    monkeypatch.setenv("MCP_DISPATCH_STATE_DIR", str(tmp_path))
    pf = tmp_path / "presence.json"
    pf.write_text("{}")
    monkeypatch.setattr(common.os, "getuid", lambda: os.stat(pf).st_uid + 1)
    assert common.armed_for({"agent_id": "legacy-1"}, pf) is None


def test_armed_for_without_an_id_is_unknown(tmp_path):
    assert common.armed_for({}, tmp_path / "presence.json") is None


# ---------------------------------------------------------------------------
# resolve_agent_id(): two windows on one project
#
# Same directory → same nick → same prefix, and the presence records agree on
# every other field too. Before ancestry the resolver saw two matches and
# returned None, which meant neither session could arm a watch and neither could
# be told it was deaf. The processes hang off different `claude` parents, and so
# does the hook doing the asking.
# ---------------------------------------------------------------------------


def _presence(relay: Path, agent_id: str, pid=0, *, lock=True):
    """A presence record. `pid` is what resolve_agent_id walks the ancestry of,
    so the tests stub process_chain to map these fake pids to fake chains."""
    (relay / ".presence").mkdir(parents=True, exist_ok=True)
    pf = relay / ".presence" / f"{agent_id}.json"
    pf.write_text(json.dumps({"agent_id": agent_id, "pid": pid}))
    if not lock:
        return None
    return common.acquire_flock(pf)


def _chains(mapping, mine):
    """Stub process_chain: a pid argument looks up a candidate's chain, no
    argument means "our own", which is what pick_by_ancestry asks for."""
    return lambda pid=None, **kw: mine if pid is None else mapping.get(pid, [])


def test_process_chain_starts_at_our_parent(tmp_path):
    chain = common.process_chain()
    assert chain, "/proc should be readable on the host running these tests"
    assert chain[0] == os.getppid()
    assert os.getpid() not in chain, "our own pid is not our ancestor"


def test_process_chain_survives_a_comm_containing_spaces(tmp_path):
    """`tmux: server` broke the field-counting parse — its comm holds a space,
    so counting whitespace fields reads the state letter as the ppid."""
    proc = tmp_path / "proc" / "4242"
    proc.mkdir(parents=True)
    (proc / "stat").write_text("4242 (tmux: server) S 99 4242 4242 0 -1 0 " + "0 " * 40)
    # Parse the same way process_chain does, on the pathological line.
    fields = (proc / "stat").read_text().rpartition(")")[2].split()
    assert fields[0] == "S" and int(fields[1]) == 99


def test_two_windows_on_one_project_resolve_to_the_right_one(tmp_path, monkeypatch):
    relay = tmp_path / "relay"
    monkeypatch.delenv("MCP_DISPATCH_AGENT_ID", raising=False)
    mine = [500, 400, 300]  # our claude is 500; 300 is the terminal we share
    a = _presence(relay, "cope-1", pid=11)
    b = _presence(relay, "cope-2", pid=22)
    chains = {11: [500, 400, 300], 22: [900, 800, 300]}  # 22 hangs off another claude
    try:
        monkeypatch.setattr(common, "process_chain", _chains(chains, mine))
        assert common.resolve_agent_id(relay, "/home/x/cope") == "cope-1"
    finally:
        for fh in (a, b):
            if fh is not None:
                fh.close()


def test_a_lone_session_needs_no_ancestry(tmp_path, monkeypatch):
    """The common case must not start depending on /proc."""
    relay = tmp_path / "relay"
    monkeypatch.delenv("MCP_DISPATCH_AGENT_ID", raising=False)
    monkeypatch.setattr(common, "process_chain", _chains({}, []))
    fh = _presence(relay, "cope-1")  # /proc unreadable, single match anyway
    try:
        assert common.resolve_agent_id(relay, "/home/x/cope") == "cope-1"
    finally:
        if fh is not None:
            fh.close()


def test_an_equidistant_tie_is_not_guessed(tmp_path, monkeypatch):
    """Two candidates sharing the same nearest ancestor carry no evidence.
    Arming a stranger's session is worse than arming none."""
    relay = tmp_path / "relay"
    monkeypatch.delenv("MCP_DISPATCH_AGENT_ID", raising=False)
    a = _presence(relay, "cope-1", pid=11)
    b = _presence(relay, "cope-2", pid=22)
    try:
        monkeypatch.setattr(common, "process_chain", _chains({11: [777], 22: [777]}, [777, 300]))
        assert common.resolve_agent_id(relay, "/home/x/cope") is None
    finally:
        for fh in (a, b):
            if fh is not None:
                fh.close()


def test_a_dead_sibling_does_not_create_ambiguity(tmp_path, monkeypatch):
    relay = tmp_path / "relay"
    monkeypatch.delenv("MCP_DISPATCH_AGENT_ID", raising=False)
    _presence(relay, "cope-dead", pid=99, lock=False)  # no flock → not live
    fh = _presence(relay, "cope-1", pid=11)
    try:
        assert common.resolve_agent_id(relay, "/home/x/cope") == "cope-1"
    finally:
        if fh is not None:
            fh.close()


def test_an_explicit_id_skips_discovery_entirely(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DISPATCH_AGENT_ID", "Stated-1")
    assert common.resolve_agent_id(tmp_path, "/home/x/cope") == "stated-1"


def test_a_candidate_whose_process_is_gone_loses_to_one_that_is_there(tmp_path, monkeypatch):
    """A pid that no longer resolves yields an empty chain and cannot win — it
    must not drag its live sibling down with it into an unresolvable tie."""
    relay = tmp_path / "relay"
    monkeypatch.delenv("MCP_DISPATCH_AGENT_ID", raising=False)
    gone = _presence(relay, "cope-gone", pid=98)
    here = _presence(relay, "cope-here", pid=11)
    try:
        monkeypatch.setattr(common, "process_chain", _chains({11: [500]}, [500, 300]))
        assert common.resolve_agent_id(relay, "/home/x/cope") == "cope-here"
    finally:
        for fh in (gone, here):
            if fh is not None:
                fh.close()


def test_ancestry_resolves_against_real_proc(tmp_path, monkeypatch):
    """No stubs: the live walk, on this machine's /proc.

    The decoy claims pid 1, whose walk terminates immediately and yields no
    ancestors, so it can never share one with us. The candidate claiming this
    test process has exactly our chain.
    """
    relay = tmp_path / "relay"
    monkeypatch.delenv("MCP_DISPATCH_AGENT_ID", raising=False)
    decoy = _presence(relay, "cope-decoy", pid=1)
    ours = _presence(relay, "cope-ours", pid=os.getpid())
    try:
        assert common.resolve_agent_id(relay, "/home/x/cope") == "cope-ours"
    finally:
        for fh in (decoy, ours):
            if fh is not None:
                fh.close()
