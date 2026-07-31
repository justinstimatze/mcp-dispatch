"""Durable agent identity: a nick outlives the session that introduced it.

Presence answers "who is live right now" and evaporates on exit. That is the
right answer for fan-out and the wrong one for identity: with ``<project>-<pid>``
ids every restart was a new stranger, an agent that wasn't running couldn't be
discovered, and a DM addressed to the project landed in a directory nobody would
ever open.

The registry (``.agents/<nick>.json``) is the durable half, and these tests pin
the three properties that follow from it: a nick is discoverable while offline,
a DM to a nick reaches its live sessions, and a DM sent while it is offline
waits for the next one.
"""

from __future__ import annotations

import fcntl
import json
import time


def _read_registry(dd, nick):
    return json.loads((dd / ".agents" / f"{nick}.json").read_text())


def _hold_presence(dd, agent_id, channels=None):
    """Make `agent_id` read as live, the way a second session would.

    Returns the open handle — the caller must keep a reference, since dropping
    it releases the flock and the agent goes offline.
    """
    pdir = dd / ".presence"
    pdir.mkdir(parents=True, exist_ok=True)
    pf = pdir / f"{agent_id}.json"
    pf.write_text(json.dumps({"agent_id": agent_id, "pid": 1, "channels": channels or []}))
    fh = open(pf, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


# ---------------------------------------------------------------------------
# The nick
# ---------------------------------------------------------------------------


def test_durable_nick_strips_the_pid(server):
    assert server._durable_nick("publicai-1767991") == "publicai"
    assert server._durable_nick("agent-service-879152") == "agent-service"
    # already durable — a roster id or an explicit MCP_DISPATCH_AGENT_ID
    assert server._durable_nick("alice") == "alice"
    assert server._durable_nick("alpha") == "alpha"


def test_claim_registers_the_nick(server_factory):
    s = server_factory("publicai-111")
    rec = _read_registry(server_factory.dispatch_dir, "publicai")
    assert rec["nick"] == "publicai"
    assert rec["last_session_id"] == "publicai-111"
    assert rec["sessions"] == 1
    assert rec["first_seen"] and rec["last_seen"]
    assert s.AGENT_ID == "publicai-111"


def test_restart_keeps_first_seen_and_counts_sessions(server_factory):
    server_factory("publicai-111")
    first = _read_registry(server_factory.dispatch_dir, "publicai")["first_seen"]
    server_factory("publicai-222")
    rec = _read_registry(server_factory.dispatch_dir, "publicai")

    assert rec["first_seen"] == first, "the nick's history must survive a restart"
    assert rec["sessions"] == 2
    assert rec["last_session_id"] == "publicai-222", "the newest session is the one to resolve to"


def test_standing_channels_are_recorded_durably(server):
    server._set_subscription("ops", True)
    rec = _read_registry(server.DISPATCH_DIR, "alpha")
    assert rec["channels"] == ["ops"]
    server._set_subscription("ops", False)
    assert _read_registry(server.DISPATCH_DIR, "alpha")["channels"] == []


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_who_reports_offline_nicks_as_known(server_factory):
    # A previous session of `publicai` ran and exited: its presence flock is gone,
    # but the identity should not be. (Loading a module doesn't end its session —
    # the flock is held by the live handle, so the exit has to be explicit.)
    prev = server_factory("publicai-111")
    prev._release_id("publicai-111")
    s = server_factory("alpha")

    result = s.who_tool()
    known = {r["nick"] for r in result.get("known", [])}
    assert "publicai" in known, "an offline teammate must still be discoverable"
    assert result["known_count"] >= 1
    live = {a["agent_id"] for a in result["agents"]}
    assert "publicai-111" not in live, "the dead session is not live"


def test_who_does_not_list_a_live_nick_as_known(server_factory):
    dd = server_factory.dispatch_dir
    server_factory("publicai-111")
    s = server_factory("alpha")
    fh = _hold_presence(dd, "publicai-222")
    try:
        result = s.who_tool()
        known = {r["nick"] for r in result.get("known", [])}
        assert "publicai" not in known, "a nick with a live session is not 'known-offline'"
    finally:
        fh.close()


def test_registered_nick_inbox_survives_reaping(server_factory):
    dd = server_factory.dispatch_dir
    server_factory("publicai-111")
    (dd / "publicai").mkdir(exist_ok=True)

    s = server_factory("alpha")
    s._reap_empty_inboxes()
    assert (dd / "publicai").is_dir(), "a registered nick's drop box must not be GC'd"


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def test_dm_to_a_nick_reaches_its_live_session(server_factory):
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    fh = _hold_presence(dd, "publicai-222")
    try:
        result = s._send("alpha", "publicai", "the nick, not the pid")
        assert result["queued_to"] == ["publicai-222"], (
            "addressing the teammate must reach the session actually running"
        )
        assert list((dd / "publicai-222").glob("*.json")), "message should be in the live inbox"
    finally:
        fh.close()


def test_dm_to_a_nick_reaches_every_live_session(server_factory):
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    a = _hold_presence(dd, "publicai-222")
    b = _hold_presence(dd, "publicai-333")
    try:
        result = s._send("alpha", "publicai", "both windows")
        assert sorted(result["queued_to"]) == ["publicai-222", "publicai-333"], (
            "picking one session arbitrarily is how a message reaches the window "
            "nobody is watching"
        )
    finally:
        a.close()
        b.close()


def test_dm_to_an_offline_nick_waits_in_its_inbox(server_factory):
    dd = server_factory.dispatch_dir
    prev = server_factory("publicai-111")  # publicai has existed...
    prev._release_id("publicai-111")  # ...and is now gone
    s = server_factory("alpha")

    result = s._send("alpha", "publicai", "read this when you're back")
    assert result["queued_to"] == ["publicai"], "with nothing live, mail waits under the nick"
    assert list((dd / "publicai").glob("*.json"))


def test_next_session_inherits_mail_sent_while_offline(server_factory):
    prev = server_factory("publicai-111")
    prev._release_id("publicai-111")
    sender = server_factory("alpha")
    sender._send("alpha", "publicai", "read this when you're back")

    # publicai starts a new session; the mail addressed to the nick is its own.
    s = server_factory("publicai-999")
    got = s._read_inbox("publicai-999")
    assert [m["content"] for m in got] == ["read this when you're back"], (
        "a message sent to an offline teammate must survive to its next session"
    )
    assert got[0]["_inherited_from"] == "publicai"


def test_direct_session_id_still_addressable(server_factory):
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    fh = _hold_presence(dd, "publicai-222")
    try:
        # Addressing the concrete session id is unchanged — no resolution applies.
        result = s._send("alpha", "publicai-222", "this exact window")
        assert result["queued_to"] == ["publicai-222"]
    finally:
        fh.close()


def test_unknown_target_still_creates_a_waiting_inbox(server_factory):
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    result = s._send("alpha", "never-seen", "hello?")
    assert result["queued_to"] == ["never-seen"]
    assert list((dd / "never-seen").glob("*.json"))


def test_resolution_does_not_escape_the_relay(server_factory):
    s = server_factory("alpha")
    for bad in ["../evil", "a/b", "..", "foo bar"]:
        try:
            s._send("alpha", bad, "nope")
        except ValueError:
            continue
        raise AssertionError(f"target {bad!r} should have been rejected")


# ---------------------------------------------------------------------------
# The registry is a convenience, never a correctness dependency
# ---------------------------------------------------------------------------


def test_corrupt_registry_record_is_ignored(server_factory):
    dd = server_factory.dispatch_dir
    server_factory("publicai-111")
    (dd / ".agents" / "publicai.json").write_text("{not json")

    s = server_factory("alpha")
    assert s._known_agents() == [] or all(r.get("nick") != "publicai" for r in s._known_agents())
    # and the relay still works
    assert s._send("alpha", "bob", "still fine")["queued_to"] == ["bob"]


def test_release_stamps_last_seen(server_factory):
    s = server_factory("publicai-111")
    before = _read_registry(s.DISPATCH_DIR, "publicai")["last_seen"]
    time.sleep(1.05)  # timestamps are second-resolution
    s._release_id("publicai-111")
    after = _read_registry(s.DISPATCH_DIR, "publicai")["last_seen"]
    assert after > before, "the departure time is the only record of when a nick went quiet"
