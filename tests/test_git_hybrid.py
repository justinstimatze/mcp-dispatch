"""Server-side hybrid-comms surface: peek provenance + who()'s remote roster.

These exercise the consumer side of the git transport — the bits server.py shows
to agents (a `via: "remote"` tag on cross-host messages, and a `remote` list in
who()) — using the reloadable `server` fixture from conftest.
"""

from __future__ import annotations

import json


def _remote_msg(frm: str, to: str, content: str) -> dict:
    return {
        "id": f"msg-{frm}1",
        "from": frm,
        "to": to,
        "timestamp": "2026-06-24T00:00:00Z",
        "priority": "normal",
        "content": content,
        "payload": None,
        "thread_id": None,
        "reply_to": None,
        "ttl": None,
        "must_read": False,
        "state": "pending",
        "_via": "git",  # as materialized by GitBridge
    }


def test_peek_surfaces_remote_via(server):
    inbox = server.DISPATCH_DIR / "alpha"
    inbox.mkdir(parents=True, exist_ok=True)
    server._atomic_write(inbox / "1-bob-aaaaaaaa.json", _remote_msg("bob", "alpha", "from afar"))

    out = server.peek_tool()
    assert out["count"] == 1
    msg = out["messages"][0]
    assert msg["via"] == "remote"
    assert msg["content"] == "from afar"
    # Internal underscore fields never leak to the wire.
    assert not any(k.startswith("_") for k in msg)


def test_who_includes_remote_roster(server):
    remote = server.DISPATCH_DIR / ".remote"
    remote.mkdir(parents=True, exist_ok=True)
    (remote / "carol.json").write_text(
        json.dumps({"agent_id": "carol", "via": "git", "last_seen": "2026-06-24T00:00:00Z"})
    )

    out = server.who_tool()
    assert out["remote_count"] == 1
    assert out["remote"][0]["agent_id"] == "carol"
    # carol is not double-counted as a live local agent.
    assert "carol" not in {a.get("agent_id") for a in out["agents"]}


def test_live_local_shadows_remote_entry(server):
    # alpha is live-local (the fixture holds its presence flock); a stale remote
    # roster entry for alpha must be hidden so who() shows one truth per id.
    remote = server.DISPATCH_DIR / ".remote"
    remote.mkdir(parents=True, exist_ok=True)
    (remote / "alpha.json").write_text(
        json.dumps({"agent_id": "alpha", "via": "git", "last_seen": "x"})
    )

    out = server.who_tool()
    assert "remote" not in out  # only entry was shadowed → list empty → omitted
    assert "alpha" in {a.get("agent_id") for a in out["agents"]}
