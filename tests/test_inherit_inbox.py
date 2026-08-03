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
import threading
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


def test_a_remote_session_is_not_a_dead_predecessor(server_factory):
    """Another host's session, however alike the names look.

    Container-ish directory names make this collision easy: every session
    launched from a projects folder is `documents-<pid>`, on every host. Without
    this guard the first local one to start would claim an unrelated project's
    mail from another machine, and the sender would never learn where it went.
    """
    dd = server_factory.dispatch_dir
    (dd / ".remote").mkdir(parents=True, exist_ok=True)
    _plant(dd, "documents-111", mid="msg-elsewhere")
    (dd / ".remote" / "documents-111.json").write_text(
        json.dumps({"agent_id": "documents-111", "via": "git", "last_seen": "2026-07-25T04:18:19Z"})
    )

    s = server_factory("documents-222")
    assert s._read_inbox("documents-222") == []
    assert list((dd / "documents-111").glob("*.json")), "left where it was, not laundered"


# --- the recurring sweep ----------------------------------------------------
#
# Inheritance used to run once, at claim time, but orphaning is continuous: a
# sibling that dies with unread mail *after* we started leaves it where nobody
# will look, and the thing that would have adopted it — a new session of the
# nick — may never be started. Measured before the fix: nine unread messages
# across five dead inboxes, oldest six days, three of them belonging to a nick
# that had two live sessions the whole time.


def _wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _sweepers():
    return sum(1 for t in threading.enumerate() if t.name == "dispatch-sweeper")


def test_mail_orphaned_after_startup_is_adopted_without_a_restart(server_factory, monkeypatch):
    dd = server_factory.dispatch_dir
    dd.mkdir(parents=True, exist_ok=True)
    s = server_factory("proj-222")
    assert s._read_inbox("proj-222") == []  # nothing to inherit at claim time

    _plant(dd, "proj-111", mid="msg-late")  # a sibling dies AFTER we started
    monkeypatch.setattr(s, "SWEEP_SECONDS", 0.02)
    s._start_sweeper("proj-222")

    assert _wait_for(lambda: bool(s._read_inbox("proj-222", state_filter="pending")))
    got = s._read_inbox("proj-222")
    assert [m["id"] for m in got] == ["msg-late"]
    assert got[0]["_inherited_from"] == "proj-111"


def test_the_sweep_still_refuses_a_live_peer(server_factory, monkeypatch):
    """The guards are the function's, not the caller's, so putting it on a loop
    must not loosen any of them."""
    dd = server_factory.dispatch_dir
    (dd / ".presence").mkdir(parents=True, exist_ok=True)
    s = server_factory("proj-222")
    _plant(dd, "proj-111", mid="msg-livepeer")
    pf = dd / ".presence" / "proj-111.json"
    pf.write_text(json.dumps({"agent_id": "proj-111", "channels": []}))
    fh = open(pf, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        monkeypatch.setattr(s, "SWEEP_SECONDS", 0.02)
        s._start_sweeper("proj-222")
        time.sleep(0.2)
        assert s._read_inbox("proj-222") == []
        assert list((dd / "proj-111").glob("*.json"))
    finally:
        fh.close()


def test_the_sweep_leaves_stale_presence_files_alone(server_factory, monkeypatch):
    """Reaping them here would be a corruption risk, not a tidy-up: claiming an
    identity opens the presence file and *then* flocks it, and an unlink between
    those two syscalls leaves the claimant writing through a handle whose path is
    gone — invisible to who(), to dispatch-status and to the supervisor, which
    would start a second session for the nick and race it for this inbox. A stale
    file misleads nobody who filters on the flock."""
    dd = server_factory.dispatch_dir
    (dd / ".presence").mkdir(parents=True, exist_ok=True)
    s = server_factory("proj-222")
    stale = dd / ".presence" / "proj-111.json"
    stale.write_text(json.dumps({"agent_id": "proj-111", "channels": []}))

    monkeypatch.setattr(s, "SWEEP_SECONDS", 0.02)
    s._start_sweeper("proj-222")
    time.sleep(0.2)
    assert stale.exists()


def test_no_sweeper_runs_in_roster_mode(server_factory, tmp_path):
    cfg = tmp_path / "roster-sweep.toml"
    cfg.write_text('agents = ["proj-111", "proj-222"]\n')
    s = server_factory("proj-222", config_path=cfg)
    before = _sweepers()
    s._start_sweeper("proj-222")
    assert _sweepers() == before


def test_no_sweeper_runs_when_inheritance_is_disabled(server_factory, tmp_path):
    cfg = tmp_path / "off-sweep.toml"
    cfg.write_text("inherit_inbox = false\n")
    s = server_factory("proj-222", config_path=cfg)
    before = _sweepers()
    s._start_sweeper("proj-222")
    assert _sweepers() == before
