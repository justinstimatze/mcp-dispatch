"""Channel subscription + fan-out tests.

Subscriptions live in the per-agent presence record, so a "subscriber" in these
tests is an agent whose presence file lists the channel. Because each loaded
server instance owns exactly one presence handle, we drive multi-agent
scenarios by writing sibling presence records directly and keeping their pids
alive (this process).
"""

from __future__ import annotations

import json
import os


def _add_subscriber(server, agent_id, channels):
    """Create a live sibling presence record subscribed to channels."""
    (server.DISPATCH_DIR / agent_id).mkdir(exist_ok=True)
    pf = server.DISPATCH_DIR / ".presence" / f"{agent_id}.json"
    pf.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "pid": os.getpid(),  # alive: this test process
                "started": "2026-06-05T00:00:00Z",
                "channels": list(channels),
            }
        )
    )


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


def test_channel_send_reaches_only_subscribers(server):
    # alpha (the loaded server) sends; beta subscribes, carol does not.
    _add_subscriber(server, "beta", ["gemot"])
    _add_subscriber(server, "carol", [])
    sent = server._send("alpha", "#gemot", "channel hello")
    assert sent["delivered_to"] == ["beta"]
    assert any(m["content"] == "channel hello" for m in server._read_inbox("beta"))
    assert server._read_inbox("carol") == []


def test_channel_send_excludes_sender(server):
    # alpha is itself subscribed; it must not receive its own channel message.
    server._set_subscription("gemot", True)
    _add_subscriber(server, "beta", ["gemot"])
    sent = server._send(server.AGENT_ID, "#gemot", "to the channel")
    assert server.AGENT_ID not in sent["delivered_to"]
    assert "beta" in sent["delivered_to"]


def test_channel_send_with_no_subscribers_delivers_nothing(server):
    sent = server._send("alpha", "#empty", "into the void")
    assert sent["delivered_to"] == []


def test_dead_subscriber_is_skipped(server):
    # A presence record with a dead pid must not receive channel messages.
    pf = server.DISPATCH_DIR / ".presence" / "ghost.json"
    (server.DISPATCH_DIR / "ghost").mkdir(exist_ok=True)
    pf.write_text(
        json.dumps({"agent_id": "ghost", "pid": 2**31 - 1, "started": "x", "channels": ["gemot"]})
    )
    sent = server._send("alpha", "#gemot", "anyone there?")
    assert "ghost" not in sent["delivered_to"]


def test_channel_name_validated(server):
    import pytest

    with pytest.raises(ValueError):
        server._send("alpha", "#../evil", "traversal via channel")


def test_subscribe_tool_roundtrip(server):
    res = server.subscribe_tool("#gemot")
    assert res["subscribed"] == "gemot"
    assert "gemot" in res["channels"]
    res2 = server.unsubscribe_tool("gemot")
    assert res2["channels"] == []
