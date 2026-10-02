"""What the sender learns when a message expires before anyone reads it.

Expiry used to unlink the file, and `_get_sent_receipts` builds receipts by
reading those same files — so the receipt vanished with the message. A sender
who checked before the deadline saw `state: pending`, and afterwards saw
nothing, which is exactly what an acked message looks like. The natural reading
of a missing receipt is "it got handled". A message nobody ever read now leaves
a tombstone that says so.
"""

from __future__ import annotations

import json

import dispatch_fs


def _jump(server, monkeypatch, seconds):
    """Move the clock forward. `time.gmtime()` is left alone, so `expired_at`
    still stamps real wall-clock — which is what the tombstone's own TTL is
    measured against."""
    real = server.time.time
    monkeypatch.setattr(server.time, "time", lambda: real() + seconds)


def _files(server, agent):
    return sorted((server.DISPATCH_DIR / agent).glob("*.json"))


def test_a_message_that_expires_unread_leaves_a_receipt(server, monkeypatch):
    server._send("alpha", "beta", "never read", ttl=60)
    _jump(server, monkeypatch, 61)
    assert server._cleanup_expired("beta") == 1

    receipts = server._get_sent_receipts("alpha")
    assert [r["state"] for r in receipts] == ["expired"]
    assert receipts[0]["expired_at"]
    assert receipts[0]["preview"] == "never read"


def test_the_recipient_is_not_shown_a_message_they_never_got(server, monkeypatch):
    """The tombstone is bookkeeping for the sender. Offering it to the reader
    would present something they cannot act on — the body is already gone."""
    server._send("alpha", "beta", "never read", ttl=60)
    _jump(server, monkeypatch, 61)
    server._cleanup_expired("beta")

    assert server._read_inbox("beta") == []
    assert server._read_inbox("beta", state_filter="pending") == []
    assert dispatch_fs.count_pending(server.DISPATCH_DIR / "beta") == 0


def test_a_message_read_before_it_expired_is_deleted_outright(server, monkeypatch):
    """No tombstone: the receipt said `read`, and that was true. The sender
    already learned everything expiry could tell them."""
    server._send("alpha", "beta", "seen in time", ttl=60)
    server._mark_read(server._read_inbox("beta", state_filter="pending"))
    _jump(server, monkeypatch, 61)
    assert server._cleanup_expired("beta") == 1

    assert _files(server, "beta") == []
    assert server._get_sent_receipts("alpha") == []


def test_the_tombstone_keeps_a_preview_not_the_body(server, monkeypatch):
    server._send("alpha", "beta", "x" * 5000, ttl=60)
    _jump(server, monkeypatch, 61)
    server._cleanup_expired("beta")

    body = json.loads(_files(server, "beta")[0].read_text())
    assert len(body["content"]) == server.TOMBSTONE_PREVIEW
    assert body["state"] == "expired"
    assert body["id"] and body["from"] == "alpha" and body["to"] == "beta"


def test_the_tombstone_is_dropped_once_it_has_had_its_own_week(server, monkeypatch):
    server._send("alpha", "beta", "never read", ttl=60)
    _jump(server, monkeypatch, 61)
    server._cleanup_expired("beta")
    assert len(_files(server, "beta")) == 1

    _jump(server, monkeypatch, server.TOMBSTONE_TTL + 120)
    server._cleanup_expired("beta")
    assert _files(server, "beta") == []


def test_sweeping_twice_neither_double_counts_nor_restamps(server, monkeypatch):
    server._send("alpha", "beta", "never read", ttl=60)
    _jump(server, monkeypatch, 61)
    assert server._cleanup_expired("beta") == 1
    first = json.loads(_files(server, "beta")[0].read_text())["expired_at"]
    assert server._cleanup_expired("beta") == 0
    assert json.loads(_files(server, "beta")[0].read_text())["expired_at"] == first


def test_must_read_never_becomes_a_tombstone(server, monkeypatch):
    """It never expires, so there is nothing to record — the message is still
    sitting there waiting to be read."""
    server._send("alpha", "beta", "important", ttl=60, must_read=True)
    _jump(server, monkeypatch, server.TOMBSTONE_TTL * 2)
    assert server._cleanup_expired("beta") == 0
    assert len(server._read_inbox("beta", state_filter="pending")) == 1


def test_peek_names_the_expired_messages_rather_than_leaving_them_in_a_list(server, monkeypatch):
    server._send("alpha", "beta", "never read", ttl=60)
    _jump(server, monkeypatch, 61)
    server._cleanup_expired("beta")

    out = server.peek_tool()
    assert out["expired_unread"] == [r["id"] for r in out["sent_receipts"]]
    assert "never read" in out["expired_unread_note"]


def test_a_relay_with_nothing_expired_says_nothing(server):
    server._send("alpha", "beta", "fresh", ttl=600)
    out = server.peek_tool()
    assert "expired_unread" not in out
    assert [r["state"] for r in out["sent_receipts"]] == ["pending"]


def test_a_tombstone_is_never_published_over_the_git_bridge(tmp_path):
    """Local bookkeeping, not a message. The window is narrow — the daemon has to
    be down across a message's entire TTL — but on the way back up the tombstone
    is unledgered, and publishing it would hand another host a truncated body
    past the deadline its sender set."""
    import git_bridge

    relay = tmp_path / "messages"
    inbox = relay / "beta"
    inbox.mkdir(parents=True)
    (inbox / "1-alpha-msg-live.json").write_text(
        json.dumps({"id": "msg-live", "from": "alpha", "to": "beta", "state": "pending"})
    )
    (inbox / "2-alpha-msg-gone.json").write_text(
        json.dumps({"id": "msg-gone", "from": "alpha", "to": "beta", "state": "expired"})
    )

    bridge = git_bridge.GitBridge.__new__(git_bridge.GitBridge)
    bridge.dispatch_dir = relay
    assert [m["id"] for m in bridge._local_messages()] == ["msg-live"]


def test_peek_reports_a_receipt_once_per_state(server):
    """A read-but-unacked message keeps its receipt for up to a week. Returning it
    on every peek was 44% of dispatch's tool-result volume, 95% of it repeats."""
    server._send("alpha", "beta", "hello", ttl=600)
    first = server.peek_tool()
    assert [r["state"] for r in first["sent_receipts"]] == ["pending"]

    again = server.peek_tool()
    assert "sent_receipts" not in again
    assert again["receipts_unchanged"] == 1

    server._mark_read(server._read_inbox("beta", state_filter="pending"))
    changed = server.peek_tool()
    assert [r["state"] for r in changed["sent_receipts"]] == ["read"]


def test_all_receipts_returns_the_full_list(server):
    server._send("alpha", "beta", "hello", ttl=600)
    server.peek_tool()
    full = server.peek_tool(all_receipts=True)
    assert [r["state"] for r in full["sent_receipts"]] == ["pending"]
    assert "receipts_unchanged" not in full


def test_an_expiry_is_still_reported_after_the_pending_receipt_was_seen(server, monkeypatch):
    """The state change that matters most must not be swallowed by the dedupe."""
    server._send("alpha", "beta", "never read", ttl=60)
    server.peek_tool()
    _jump(server, monkeypatch, 61)
    server._cleanup_expired("beta")
    out = server.peek_tool()
    assert [r["state"] for r in out["sent_receipts"]] == ["expired"]
    assert out["expired_unread"] == [out["sent_receipts"][0]["id"]]


def test_a_delivered_message_leaves_out_its_default_fields(server):
    """Default-valued envelope fields were 9.4% of delivered message text, and
    state/read_at always said "read, just now". Non-defaults must survive."""
    server._send("beta", "alpha", "plain", ttl=None)
    server._send("beta", "alpha", "urgent", priority="urgent", must_read=True, thread_id="t1")
    by_text = {m["content"]: m for m in server.peek_tool()["messages"]}
    plain, urgent = by_text["plain"], by_text["urgent"]
    assert set(plain) == {"id", "from", "to", "timestamp", "content"}
    assert urgent["priority"] == "urgent" and urgent["must_read"] is True
    assert urgent["thread_id"] == "t1"


def test_each_recipient_of_a_fan_out_gets_its_own_receipt_history(server_factory):
    """One id lands in every recipient's inbox. Keyed by id alone, the copies
    overwrote each other and a second recipient's read was never reported."""
    beta = server_factory("beta")
    gamma = server_factory("gamma")
    me = server_factory("alpha")
    me._send("alpha", "all", "to everyone", ttl=600)
    assert sorted(r["to"] for r in me.peek_tool()["sent_receipts"]) == ["beta", "gamma"]

    beta._mark_read(beta._read_inbox("beta", state_filter="pending"))
    assert [(r["to"], r["state"]) for r in me.peek_tool()["sent_receipts"]] == [("beta", "read")]
    assert "sent_receipts" not in me.peek_tool()

    gamma._mark_read(gamma._read_inbox("gamma", state_filter="pending"))
    assert [(r["to"], r["state"]) for r in me.peek_tool()["sent_receipts"]] == [("gamma", "read")]
