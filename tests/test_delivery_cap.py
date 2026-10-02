"""A long message reaches the recipient's model as its head.

Every delivered message stays in the recipient's context for the rest of its
session, so the cost of a long body is paid on every later turn whether or not
the recipient needed past its first paragraph. The stored file stays whole —
bridges, the TUI and IRC read the file — and only what `peek` and the piggyback
hand the model is cut.
"""

from __future__ import annotations

import json
import re


def _long(n_lines=80):
    return "\n".join(f"line {i}: " + "x" * 40 for i in range(n_lines))


def test_long_content_is_delivered_as_its_head(server):
    body = _long()
    server._send("beta", "alpha", body)
    [m] = server.peek_tool()["messages"]
    assert m["truncated_from"] == len(body)
    assert len(m["content"]) <= server.DELIVER_MAX_CHARS + 80  # the marker
    head, marker, tail = re.split(r"\n(\[… .*? …\])\n", m["content"])
    assert f'peek(message_ids=["{m["id"]}"])' in marker
    # Both halves are whole lines.
    assert head.startswith("line 0: ") and head.endswith("x" * 40)
    assert tail.startswith("line ") and tail.endswith("line 79: " + "x" * 40)


def test_a_closing_caveat_survives_the_cut(server):
    """The case the cap was reviewed against: a long handoff whose last line is
    the one that stops the recipient from acting on the rest."""
    server._send("beta", "alpha", _long() + "\nDon't push yet.")
    [m] = server.peek_tool()["messages"]
    assert m["content"].endswith("Don't push yet.")


def test_must_read_and_urgent_are_never_cut(server):
    body = _long()
    server._send("beta", "alpha", body, must_read=True)
    server._send("beta", "alpha", body, "urgent")
    assert all(m["content"] == body for m in server.peek_tool()["messages"])
    assert "truncated_on_delivery" not in server.dispatch_tool(body, "beta", must_read=True)


def test_the_stored_file_keeps_the_whole_message(server):
    body = _long()
    server._send("beta", "alpha", body)
    server.peek_tool()
    [path] = (server.DISPATCH_DIR / "alpha").glob("*.json")
    assert json.loads(path.read_text())["content"] == body


def test_message_ids_returns_the_full_text_even_once_read(server):
    body = _long()
    sent = server._send("beta", "alpha", body)
    server.peek_tool()  # marks it read
    [m] = server.peek_tool(message_ids=[sent["id"]])["messages"]
    assert m["content"] == body
    assert "truncated_from" not in m


def test_short_content_is_untouched(server):
    server._send("beta", "alpha", "short")
    [m] = server.peek_tool()["messages"]
    assert m["content"] == "short"
    assert "truncated_from" not in m


def test_the_sender_is_told_its_message_was_cut(server):
    out = server.dispatch_tool(_long(), "beta")
    assert "truncated_on_delivery" in out
    assert "truncated_on_delivery" not in server.dispatch_tool("short", "beta")


def test_zero_turns_the_cap_off(server_factory, tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("deliver_max_chars = 0\n")
    srv = server_factory("alpha", config_path=cfg)
    body = _long()
    srv._send("beta", "alpha", body)
    [m] = srv.peek_tool()["messages"]
    assert m["content"] == body
    assert "truncated_on_delivery" not in srv.dispatch_tool(body, "beta")


def test_the_style_note_survives_a_custom_instructions_template(server_factory, tmp_path):
    """A host's `instructions` replaces the template wholesale, which is the
    exact case on the machine this was written for."""
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('instructions = "custom text"\n')
    srv = server_factory("alpha", config_path=cfg)
    assert srv._instructions.startswith("custom text")
    assert "Message style" in srv._instructions


def test_an_id_peek_cannot_return_is_named(server):
    """An acked message is gone, and an empty list must not look like success."""
    sent = server._send("beta", "alpha", "x")
    server.ack_tool([sent["id"]])
    out = server.peek_tool(message_ids=[sent["id"], "nope"])
    assert out["count"] == 0
    assert out["not_found"] == sorted([sent["id"], "nope"])


def test_just_over_the_cap_is_left_whole(server):
    """The marker costs more than cutting a few characters saves."""
    body = "y" * (server.DELIVER_MAX_CHARS + 3)
    server._send("beta", "alpha", body)
    [m] = server.peek_tool()["messages"]
    assert m["content"] == body


def test_neither_half_breaks_a_word(server):
    """A replay against real sends found tails opening on "ne etc.)" — a
    fragment of a word — whenever no newline fell near the cut."""
    words = " ".join(f"word{i}" for i in range(1200))
    out = server._clip(words, 1000)
    head, tail = re.split(r"\n\[… .*? …\]\n", out)
    assert head.split()[-1] in words.split()
    assert tail.split()[0] in words.split()


def test_a_tiny_cap_still_cuts(server):
    assert server._clip("z" * 80, 3) == "zzz …"
