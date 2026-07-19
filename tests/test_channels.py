"""Channel subscription + fan-out tests.

A "subscriber" is an agent whose presence record lists the channel AND whose
owner is live. Liveness is the presence flock, so we simulate sibling agents by
writing a presence file and actually holding an exclusive flock on it (a record
with no held lock reads as dead, exactly like a crashed peer).
"""

from __future__ import annotations

import fcntl
import json

import pytest


@pytest.fixture
def add_live():
    """Factory: create a sibling presence record and hold its flock (live owner).

    Held handles are closed at teardown so the locks release.
    """
    held = []

    def _add(server, agent_id, channels):
        (server.DISPATCH_DIR / agent_id).mkdir(exist_ok=True)
        pf = server.DISPATCH_DIR / ".presence" / f"{agent_id}.json"
        pf.write_text(json.dumps({"agent_id": agent_id, "channels": list(channels)}))
        fh = open(pf, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(fh)

    yield _add
    for fh in held:
        try:
            fh.close()
        except OSError:
            pass


def test_subscribe_updates_presence(server):
    channels = server._set_subscription("gemot", True)
    assert channels == ["gemot"]
    pf = server.DISPATCH_DIR / ".presence" / f"{server.AGENT_ID}.json"
    assert json.loads(pf.read_text())["channels"] == ["gemot"]


def test_subscribe_is_idempotent_and_sorted(server):
    server._set_subscription("beta", True)
    server._set_subscription("alpha-chan", True)
    assert server._set_subscription("beta", True) == ["alpha-chan", "beta"]


def test_unsubscribe_removes(server):
    server._set_subscription("gemot", True)
    assert server._set_subscription("gemot", False) == []


def test_channel_send_reaches_only_subscribers(server, add_live):
    # alpha (the loaded server) sends; beta subscribes, carol does not.
    add_live(server, "beta", ["gemot"])
    add_live(server, "carol", [])
    sent = server._send("alpha", "#gemot", "channel hello")
    assert sent["queued_to"] == ["beta"]
    assert any(m["content"] == "channel hello" for m in server._read_inbox("beta"))
    assert server._read_inbox("carol") == []


def test_channel_send_excludes_sender(server, add_live):
    # alpha is itself subscribed; it must not receive its own channel message.
    server._set_subscription("gemot", True)
    add_live(server, "beta", ["gemot"])
    sent = server._send(server.AGENT_ID, "#gemot", "to the channel")
    assert server.AGENT_ID not in sent["queued_to"]
    assert "beta" in sent["queued_to"]


def test_channel_send_with_no_subscribers_delivers_nothing(server):
    sent = server._send("alpha", "#empty", "into the void")
    assert sent["queued_to"] == []


def test_dead_subscriber_is_skipped(server):
    # A presence record whose owner holds no flock (crashed/exited) is dead and
    # must not receive channel messages — regardless of the pid field.
    pf = server.DISPATCH_DIR / ".presence" / "ghost.json"
    (server.DISPATCH_DIR / "ghost").mkdir(exist_ok=True)
    pf.write_text(json.dumps({"agent_id": "ghost", "channels": ["gemot"]}))
    sent = server._send("alpha", "#gemot", "anyone there?")
    assert "ghost" not in sent["queued_to"]


def test_channel_name_validated(server):
    with pytest.raises(ValueError):
        server._send("alpha", "#../evil", "traversal via channel")


def test_subscribe_tool_roundtrip(server):
    res = server.subscribe_tool("#gemot")
    assert res["subscribed"] == "gemot"
    assert "gemot" in res["channels"]
    res2 = server.unsubscribe_tool("gemot")
    assert res2["channels"] == []


# --- MCP_DISPATCH_CHANNELS: auto-subscribe standing rooms on startup ----------


def test_initial_channels_from_env(server_factory):
    s = server_factory(extra_env={"MCP_DISPATCH_CHANNELS": "agentops"})
    assert s._PRESENCE_DATA["channels"] == ["agentops"]
    pf = s.DISPATCH_DIR / ".presence" / f"{s.AGENT_ID}.json"
    assert json.loads(pf.read_text())["channels"] == ["agentops"]


def test_initial_channels_parsing(server_factory):
    # Comma- or space-separated, leading '#' optional, deduped and sorted.
    s = server_factory(extra_env={"MCP_DISPATCH_CHANNELS": "#ops, ops  agentops"})
    assert s._PRESENCE_DATA["channels"] == ["agentops", "ops"]


def test_initial_channels_skips_invalid(server_factory):
    # Structurally-invalid ids are dropped (not fatal); valid ones survive so
    # startup proceeds. (Case alone is not invalid — see the normalize test.)
    s = server_factory(extra_env={"MCP_DISPATCH_CHANNELS": "bad/x, ok2, ..trav"})
    assert s._PRESENCE_DATA["channels"] == ["ok2"]


def test_initial_channels_lowercases(server_factory):
    # Names are lowercased (like MCP_DISPATCH_AGENT_ID), so `#Ops` joins `#ops`
    # rather than being dropped; a de-cased dup collapses.
    s = server_factory(extra_env={"MCP_DISPATCH_CHANNELS": "#Ops, ops, AgentOps"})
    assert s._PRESENCE_DATA["channels"] == ["agentops", "ops"]


def test_initial_channels_absent_is_empty(server_factory):
    assert server_factory()._PRESENCE_DATA["channels"] == []
