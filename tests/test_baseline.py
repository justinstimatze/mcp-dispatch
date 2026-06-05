"""Baseline behavior tests — lock in current (correct) behavior before changes.

These exercise the pieces the audit calls out as nice and worth preserving:
non-destructive read, TTL expiry, threading, must_read override, piggyback.
"""

from __future__ import annotations


def test_send_and_read_roundtrip(server):
    server._send("alpha", "beta", "hello beta")
    inbox = server._read_inbox("beta")
    assert len(inbox) == 1
    assert inbox[0]["content"] == "hello beta"
    assert inbox[0]["from"] == "alpha"
    assert inbox[0]["state"] == "pending"


def test_read_is_non_destructive(server):
    server._send("alpha", "beta", "still here")
    server._read_inbox("beta")
    # File must still exist after a read.
    assert len(server._read_inbox("beta", state_filter=None)) == 1


def test_mark_read_transitions_state(server):
    server._send("alpha", "beta", "msg")
    msgs = server._read_inbox("beta", state_filter="pending")
    server._mark_read(msgs)
    assert server._read_inbox("beta", state_filter="pending") == []
    read = server._read_inbox("beta", state_filter="read")
    assert len(read) == 1
    assert read[0]["read_at"]


def test_thread_filter(server):
    server._send("alpha", "beta", "t1", thread_id="thread-1")
    server._send("alpha", "beta", "t2", thread_id="thread-2")
    only_one = server._read_inbox("beta", thread_id="thread-1")
    assert len(only_one) == 1
    assert only_one[0]["content"] == "t1"


def test_ttl_expiry(server, monkeypatch):
    server._send("alpha", "beta", "expires", ttl=60)
    # Jump 61s forward.
    real_time = server.time.time
    monkeypatch.setattr(server.time, "time", lambda: real_time() + 61)
    removed = server._cleanup_expired("beta")
    assert removed == 1
    assert server._read_inbox("beta") == []


def test_must_read_overrides_ttl(server, monkeypatch):
    server._send("alpha", "beta", "important", ttl=60, must_read=True)
    real_time = server.time.time
    monkeypatch.setattr(server.time, "time", lambda: real_time() + 3600)
    assert server._cleanup_expired("beta") == 0
    assert len(server._read_inbox("beta")) == 1


def test_broadcast_fans_out_excluding_sender(server_factory):
    srv = server_factory("alpha")
    # Create inboxes for beta and gamma by sending direct first.
    srv._send("alpha", "beta", "seed")
    srv._send("alpha", "gamma", "seed")
    srv._send("alpha", "all", "broadcast")
    assert any(m["content"] == "broadcast" for m in srv._read_inbox("beta"))
    assert any(m["content"] == "broadcast" for m in srv._read_inbox("gamma"))
    # Sender does not receive its own broadcast.
    assert all(m["content"] != "broadcast" for m in srv._read_inbox("alpha"))


def test_piggyback_attaches_pending(server_factory):
    beta = server_factory("beta")
    beta._send("alpha", "beta", "for beta")
    result = beta._with_pending({"ok": True})
    assert result["_dispatch_count"] == 1
    assert result["_dispatches"][0]["content"] == "for beta"
    # Piggyback marks them read so they don't re-deliver.
    assert beta._read_inbox("beta", state_filter="pending") == []


def test_ack_deletes(server_factory):
    beta = server_factory("beta")
    sent = beta._send("alpha", "beta", "delete me")
    result = beta.ack_tool([sent["id"]])
    assert result["acked"] == 1
    assert beta._read_inbox("beta") == []


def test_who_lists_self(server):
    result = server.who_tool()
    assert result["self"] == "alpha"
    assert any(a["agent_id"] == "alpha" for a in result["agents"])
