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
