"""Successor inbox inheritance: unread mail survives a session restart.

A dynamic-mode id is ``<project>-<pid>``, so every restart is a *new* identity
with an empty inbox. Anything the previous session never read stayed `pending`
in a directory nobody would open again — silently lost, while the sender's
receipt showed it queued. A successor now adopts it at startup.

The guards are the interesting part (never steal from a live peer, never cross
projects, never cross accounts), so they get as much coverage as the happy path.
"""

from __future__ import annotations

import fcntl
import json
import time


def _plant(dispatch_dir, agent, *, mid, state="pending", content="left behind"):
    """Write a message straight into an inbox dir, as a sender would have."""
    inbox = dispatch_dir / agent
    inbox.mkdir(parents=True, exist_ok=True)
    msg = {
        "id": mid,
        "from": "bob",
        "to": agent,
        "content": content,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "state": state,
    }
    (inbox / f"{int(time.time() * 1000)}-bob-{mid}.json").write_text(json.dumps(msg))


def test_successor_adopts_pending_mail(server_factory):
    dd = server_factory.dispatch_dir
    dd.mkdir(parents=True, exist_ok=True)
    _plant(dd, "proj-111", mid="msg-orphan")

    s = server_factory("proj-222")
    got = s._read_inbox("proj-222")
    assert [m["id"] for m in got] == ["msg-orphan"]
    # Provenance is surfaced, not silently laundered into fresh mail.
    assert got[0]["_inherited_from"] == "proj-111"
    assert s._public_msg(got[0])["inherited_from"] == "proj-111"
    assert not list((dd / "proj-111").glob("*.json"))  # moved, not copied


def test_already_read_mail_is_left_behind(server_factory):
    # Read mail is history, not a delivery failure; it also still backs the
    # sender's receipt, which scans every inbox dir including dead ones.
    dd = server_factory.dispatch_dir
    dd.mkdir(parents=True, exist_ok=True)
    _plant(dd, "proj-111", mid="msg-seen", state="read")

    s = server_factory("proj-222")
    assert s._read_inbox("proj-222") == []
    assert list((dd / "proj-111").glob("*.json"))


def test_live_peer_is_not_robbed(server_factory):
    """Same project prefix but the owner still holds its presence lock — that's a
    running sibling session, not a corpse. Taking its mail would be theft."""
    dd = server_factory.dispatch_dir
    (dd / ".presence").mkdir(parents=True, exist_ok=True)
    _plant(dd, "proj-111", mid="msg-live")
    pf = dd / ".presence" / "proj-111.json"
    pf.write_text(json.dumps({"agent_id": "proj-111", "channels": []}))
    fh = open(pf, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        s = server_factory("proj-222")
        assert s._read_inbox("proj-222") == []
        assert list((dd / "proj-111").glob("*.json"))
    finally:
        fh.close()


def test_other_projects_are_not_inherited(server_factory):
    dd = server_factory.dispatch_dir
    dd.mkdir(parents=True, exist_ok=True)
    _plant(dd, "other-111", mid="msg-theirs")
    # A same-prefix id with no numeric pid suffix isn't a predecessor either.
    _plant(dd, "proj-shared", mid="msg-notapid")

    s = server_factory("proj-222")
    assert s._read_inbox("proj-222") == []


def test_roster_mode_never_inherits(server_factory, tmp_path):
    """A roster id keeps its identity across restarts, so its inbox isn't orphaned
    — it's waiting for the same agent to come back."""
    cfg = tmp_path / "roster.toml"
    cfg.write_text('agents = ["proj-111", "proj-222"]\n')
    dd = server_factory.dispatch_dir
    dd.mkdir(parents=True, exist_ok=True)
    _plant(dd, "proj-111", mid="msg-roster")

    s = server_factory("proj-222", config_path=cfg)
    assert s._read_inbox("proj-222") == []


def test_inherit_can_be_disabled(server_factory, tmp_path):
    cfg = tmp_path / "off.toml"
    cfg.write_text("inherit_inbox = false\n")
    dd = server_factory.dispatch_dir
    dd.mkdir(parents=True, exist_ok=True)
    _plant(dd, "proj-111", mid="msg-optout")

    s = server_factory("proj-222", config_path=cfg)
    assert s._read_inbox("proj-222") == []
    assert list((dd / "proj-111").glob("*.json"))
