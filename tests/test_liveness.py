"""flock-based liveness — the replacement for the pid heuristic.

A live owner holds an exclusive flock on its presence file. That signal is
uid-agnostic (works across accounts in group_mode) and immune to pid reuse,
because the kernel releases the lock when the owner dies / the host reboots.
These tests simulate peers by holding (or not holding) a real flock.
"""

from __future__ import annotations

import fcntl
import json


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
