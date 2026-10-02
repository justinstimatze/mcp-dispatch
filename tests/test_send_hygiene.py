"""What dispatch() does about a send beyond delivering it.

Each of these answers a cost the recipient pays and the sender never sees: a
message nobody can triage without reading, an identical copy of one already
waiting, a sender that is the loudest thing on the rail, a greeting or a pasted
file sitting in every recipient's context for the rest of its session.
"""

from __future__ import annotations

import json

import pytest


def _stored(server, agent):
    [path] = (server.DISPATCH_DIR / agent).glob("*.json")
    return json.loads(path.read_text())


def test_kind_and_about_are_stored_and_delivered(server):
    server._send("beta", "alpha", "x", kind="ask", about="server.py:1207")
    [m] = server.peek_tool()["messages"]
    assert (m["kind"], m["about"]) == ("ask", "server.py:1207")


def test_a_message_without_them_has_no_such_keys(server):
    """The git envelope carries the message dict verbatim, so an unset field
    must not appear as a null that a peer's decoder then has to tolerate."""
    server._send("beta", "alpha", "x")
    assert "kind" not in _stored(server, "alpha")
    assert "about" not in _stored(server, "alpha")


def test_an_unknown_kind_is_refused(server):
    with pytest.raises(ValueError, match="kind must be one of"):
        server._send("beta", "alpha", "x", kind="urgent-ish")


def test_about_is_a_pointer_not_a_summary(server):
    with pytest.raises(ValueError, match="about is a pointer"):
        server._send("beta", "alpha", "x", about="y" * 201)


def test_an_identical_unread_resend_is_dropped(server):
    first = server.dispatch_tool("same words", "beta")
    again = server.dispatch_tool("same words", "beta")
    assert again["sent"] is False
    assert again["duplicate_of"] == first["id"]
    assert len(list((server.DISPATCH_DIR / "beta").glob("*.json"))) == 1


def test_a_resend_after_it_was_read_goes_through(server_factory):
    """Somebody saw the first one, so a second may be a deliberate re-ping."""
    alpha = server_factory("alpha")
    first = alpha.dispatch_tool("same words", "beta")
    alpha._mark_read(alpha._read_inbox("beta"))
    again = alpha.dispatch_tool("same words", "beta")
    assert again["sent"] is True
    assert again["id"] != first["id"]


def test_a_different_envelope_is_not_a_duplicate(server):
    server.dispatch_tool("same words", "beta")
    assert server.dispatch_tool("same words", "beta", kind="ask")["sent"] is True
    assert server.dispatch_tool("same words", "gamma")["sent"] is True


def test_over_budget_warns_and_still_sends(server, monkeypatch):
    monkeypatch.setattr(server, "SEND_BUDGET", 2)
    notes = [server.dispatch_tool(f"m{i}", "beta") for i in range(3)]
    assert "over_budget" not in notes[1]
    assert notes[2]["sent"] is True
    assert notes[2]["over_budget"].startswith("3 sends")


def test_a_greeting_is_named_at_the_send_that_has_one(server):
    assert server.dispatch_tool("Hey! Quick one: is CI green?", "beta")["style"]
    assert "style" not in server.dispatch_tool("Is CI green on main?", "beta")


def test_a_long_pasted_block_is_named(server):
    block = "```\n" + "\n".join(f"line {i}" for i in range(30)) + "\n```"
    out = server.dispatch_tool("See:\n" + block, "beta")
    assert any("30-line block" in s for s in out["style"])
    short = "```\n" + "\n".join(f"line {i}" for i in range(5)) + "\n```"
    assert "style" not in server.dispatch_tool("See:\n" + short, "beta")


def test_changed_receipts_are_capped_newest_first(server, monkeypatch):
    monkeypatch.setattr(server, "_RECEIPTS_SHOWN", 3)
    for i in range(5):
        server._send("alpha", "beta", f"m{i}")
    fake = [
        {"id": f"r{i}", "to": "beta", "state": "pending", "sent_at": f"2026-10-01T00:00:0{i}Z"}
        for i in range(5)
    ]
    monkeypatch.setattr(server, "_get_sent_receipts", lambda _agent: list(fake))
    out = server.peek_tool()
    assert [r["id"] for r in out["sent_receipts"]] == ["r4", "r3", "r2"]
    assert out["receipts_older"] == 2
    full = server.peek_tool(all_receipts=True)
    assert len(full["sent_receipts"]) == 5
    assert "receipts_older" not in full


def test_every_tool_is_registered_to_its_own_function(server):
    """A helper inserted between a decorator and its function silently becomes
    the tool. Tests that call peek_tool() directly never notice."""
    for name, tool in server.mcp._tool_manager._tools.items():
        assert tool.fn.__name__ == f"{name}_tool", name


def test_upgrading_a_resend_to_must_read_goes_through(server):
    """The truncation notice tells senders to resend with must_read=true."""
    server.dispatch_tool("important", "beta")
    assert server.dispatch_tool("important", "beta", must_read=True)["sent"] is True


def test_an_acked_copy_counts_as_read(server, monkeypatch):
    first = server.dispatch_tool("same words", "beta")
    monkeypatch.setitem(server._SENT_KEYS, next(iter(server._SENT_KEYS)), (first["id"], 2))
    assert server.dispatch_tool("same words", "beta")["sent"] is True


def test_a_receipt_held_back_by_the_cap_surfaces_at_the_next_peek(server, monkeypatch):
    monkeypatch.setattr(server, "_RECEIPTS_SHOWN", 3)
    fake = [
        {"id": f"r{i}", "to": "beta", "state": "pending", "sent_at": f"2026-10-01T00:00:0{i}Z"}
        for i in range(5)
    ]
    monkeypatch.setattr(server, "_get_sent_receipts", lambda _agent: list(fake))
    assert [r["id"] for r in server.peek_tool()["sent_receipts"]] == ["r4", "r3", "r2"]
    again = server.peek_tool()
    assert [r["id"] for r in again["sent_receipts"]] == ["r1", "r0"]
    assert again["receipts_unchanged"] == 3
    assert "sent_receipts" not in server.peek_tool()
