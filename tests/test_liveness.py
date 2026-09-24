"""flock-based liveness — the replacement for the pid heuristic.

A live owner holds an exclusive flock on its presence file. That signal is
uid-agnostic (works across accounts in group_mode) and immune to pid reuse,
because the kernel releases the lock when the owner dies / the host reboots.
These tests simulate peers by holding (or not holding) a real flock.
"""

from __future__ import annotations

import fcntl
import json
import os


def _make_presence(server, agent_id, channels=()):
    (server.DISPATCH_DIR / agent_id).mkdir(exist_ok=True)
    pf = server.DISPATCH_DIR / ".presence" / f"{agent_id}.json"
    pf.write_text(json.dumps({"agent_id": agent_id, "channels": list(channels)}))
    return pf


def _lock(pf):
    fh = open(pf, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def test_presence_is_live_tracks_the_lock(server):
    pf = _make_presence(server, "beta")
    fh = _lock(pf)
    try:
        assert server._presence_is_live(pf) is True
    finally:
        fh.close()
    assert server._presence_is_live(pf) is False  # lock released → dead


def test_self_presence_is_live(server):
    # The loaded server holds its own presence lock via _PRESENCE_HANDLE.
    pf = server.DISPATCH_DIR / ".presence" / f"{server.AGENT_ID}.json"
    assert server._presence_is_live(pf) is True


def test_missing_presence_is_dead(server):
    assert server._presence_is_live(server.DISPATCH_DIR / ".presence" / "nope.json") is False


def test_reap_removes_dead_keeps_live(server):
    dead = _make_presence(server, "ghost")
    live = _make_presence(server, "beta")
    fh = _lock(live)
    try:
        removed = server._reap_dead_presence()
        assert not dead.exists()  # reaped
        assert live.exists()  # kept (locked)
        assert removed >= 1
    finally:
        fh.close()


def test_who_filters_dead_without_unlinking(server):
    ghost = _make_presence(server, "ghost")  # no lock → dead
    ids = [a["agent_id"] for a in server.who_tool()["agents"]]
    assert "ghost" not in ids
    assert server.AGENT_ID in ids  # self is live
    assert ghost.exists()  # who() filters but does not delete (no claim race)


def test_broadcast_targets_only_live_agents(server):
    live = _make_presence(server, "beta")
    fh = _lock(live)
    _make_presence(server, "ghost")  # dead
    try:
        sent = server._send("alpha", "all", "hello everyone")
        assert "beta" in sent["queued_to"]
        assert "ghost" not in sent["queued_to"]
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# Launch directory
# ---------------------------------------------------------------------------


def test_presence_records_the_launch_directory(server_factory):
    """Two sessions can share a name honestly; the directory tells them apart.

    A session started in ~/Documents is correctly called `documents` — that is
    where it was launched. It just doesn't say which project it is working on,
    and every session launched from that folder answers to the same name.
    """
    s = server_factory("documents-111", extra_env={"MCP_DISPATCH_CWD": "/home/x/Documents"})
    pf = s.DISPATCH_DIR / ".presence" / "documents-111.json"
    assert json.loads(pf.read_text())["cwd"] == "/home/x/Documents"


def test_the_launch_directory_beats_the_process_directory(server_factory):
    """The launcher execs `uv run --directory <repo>`, so os.getcwd() inside the
    server is this repo for every agent on the box — the same string each time,
    which distinguishes nothing. The env var is stamped before that exec."""
    import os

    s = server_factory("proj-111", extra_env={"MCP_DISPATCH_CWD": "/home/x/Documents/stope"})
    assert s._session_cwd() == "/home/x/Documents/stope"
    assert s._session_cwd() != os.getcwd()


def test_a_server_started_by_hand_falls_back_to_its_own_directory(server_factory):
    import os

    s = server_factory("proj-111")  # no MCP_DISPATCH_CWD, i.e. not via the launcher
    assert s._session_cwd() == os.getcwd()


def test_who_shows_where_each_live_session_was_launched(server_factory):
    s = server_factory("documents-111", extra_env={"MCP_DISPATCH_CWD": "/home/x/Documents"})
    me = [a for a in s.who_tool()["agents"] if a["agent_id"] == "documents-111"]
    assert me, "self must be live in who()"
    assert me[0]["cwd"] == "/home/x/Documents"


# ---------------------------------------------------------------------------
# Live is not the same as listening. A session holding its presence lock with no
# message watch armed collects mail nobody wakes it to read — and it looks
# healthy from every other angle who() reports.
# ---------------------------------------------------------------------------


def test_who_says_whether_each_session_is_listening(server_factory, tmp_path):
    import dispatch_common as common

    state = tmp_path / "armstate"
    s = server_factory("alpha", extra_env={"MCP_DISPATCH_STATE_DIR": str(state)})
    me = [a for a in s.who_tool()["agents"] if a["agent_id"] == "alpha"][0]
    assert me["armed"] is False, "nothing has armed a watch for this session yet"

    lock = common.arm_lock("alpha", state)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = common.acquire_flock(lock)
    try:
        me = [a for a in s.who_tool()["agents"] if a["agent_id"] == "alpha"][0]
        assert me["armed"] is True
    finally:
        if fh is not None:
            fh.close()


def test_a_session_publishes_where_its_arm_lock_lives(server_factory, tmp_path):
    """Without this field a reader probes its own cache and calls every session
    on another account deaf."""
    state = tmp_path / "armstate"
    s = server_factory("alpha", extra_env={"MCP_DISPATCH_STATE_DIR": str(state)})
    rec = json.loads((s.DISPATCH_DIR / ".presence" / "alpha.json").read_text())
    assert rec["state_dir"] == str(state)


def test_who_names_the_sessions_that_will_not_hear_you(server):
    """The point of the field: a sender can tell a live-and-deaf target from a
    live one before reading silence as an answer."""
    pf = _make_presence(server, "parked")
    fh = _lock(pf)
    try:
        out = server.who_tool()
        assert "parked" in out["unarmed"]
        assert "durable" in out["unarmed_note"], "say the mail is still safe"
    finally:
        fh.close()


def test_an_all_armed_relay_says_nothing_about_it(server_factory, tmp_path):
    import dispatch_common as common

    state = tmp_path / "armstate"
    s = server_factory("alpha", extra_env={"MCP_DISPATCH_STATE_DIR": str(state)})
    lock = common.arm_lock("alpha", state)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = common.acquire_flock(lock)
    try:
        out = s.who_tool()
        assert "unarmed" not in out, "no warning when there is nothing to warn about"
    finally:
        if fh is not None:
            fh.close()


def test_who_names_the_pid_for_what_it_is(server):
    """A bare `pid` read as the session's and got checked against /proc, which
    called a live peer dead: it is the server subprocess's. The presence file
    keeps `pid` for its own readers; who() hands it out as `server_pid`."""
    me = next(a for a in server.who_tool()["agents"] if a["agent_id"] == server.AGENT_ID)
    assert "pid" not in me
    assert me["server_pid"] == os.getpid()
    pf = server.DISPATCH_DIR / ".presence" / f"{server.AGENT_ID}.json"
    assert json.loads(pf.read_text())["pid"] == os.getpid()


def test_who_live_scope_leaves_out_the_rosters(server_factory):
    """The full answer carries every nick ever seen; scope='live' is the cheap
    call for "who is here right now"."""
    gone = server_factory("mars-1")
    gone._release_id(gone.AGENT_ID)
    me = server_factory("venus-1")
    remote = me.DISPATCH_DIR / ".remote"
    remote.mkdir(exist_ok=True)
    (remote / "jupiter-9.json").write_text(json.dumps({"agent_id": "jupiter-9"}))

    full = me.who_tool()
    assert full["known_count"] >= 1 and full["remote_count"] == 1

    live = me.who_tool(scope="live")
    assert [a["agent_id"] for a in live["agents"]] == ["venus-1"]
    assert not {"known", "remote", "native"} & live.keys()


def test_who_rejects_an_unknown_scope(server):
    import pytest

    with pytest.raises(ValueError):
        server.who_tool(scope="everything")


def test_a_session_just_woken_by_its_watch_is_handling_not_deaf(server, tmp_path):
    """A one-shot watch exits on the message it wakes for, so for the length of
    the reply the arm lock is free. who() must not tell senders that session
    won't answer."""
    from dispatch_common import wake_record

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    pf = server.DISPATCH_DIR / ".presence" / f"{server.AGENT_ID}.json"
    data = json.loads(pf.read_text())
    data["state_dir"] = str(state)
    server._PRESENCE_DATA.update(data)
    server._write_presence()

    me = lambda: next(a for a in server.who_tool()["agents"] if a["agent_id"] == server.AGENT_ID)  # noqa: E731
    assert me()["armed"] is False and server.AGENT_ID in server.who_tool()["unarmed"]

    wake_record(server.AGENT_ID, state).write_text('{"reported": ["m1"]}')
    out = server.who_tool()
    assert me()["handling"] is True
    assert server.AGENT_ID not in out.get("unarmed", [])
