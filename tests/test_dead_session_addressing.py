"""Mail addressed to a session that has already exited.

Two agents hit this the same evening and reported it independently. A sender
picks a full session id off who()'s `remote` list, the session exited an hour
earlier, and the message is written into its spool — a directory nobody will
open again. `queued_to` looks normal, the sent receipt says `pending` forever,
and the only recovery anyone found was noticing by hand and resending.

Three things had to be true at once, and each gets its own section here: the
sender was steered onto a dead id by an undifferentiated list; delivery wrote
to the corpse instead of resolving the nick behind it; and inheritance — the
safety net that exists for exactly this — was disabled for those spools because
the git bridge had reclassified this host's own dead sessions as another host's.
"""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path

import dispatch_fs
import notify_policy


def _hold_presence(dd, agent_id, channels=()):
    """Make `agent_id` read as live. Caller keeps the handle or it dies."""
    pdir = dd / ".presence"
    pdir.mkdir(parents=True, exist_ok=True)
    pf = pdir / f"{agent_id}.json"
    pf.write_text(json.dumps({"agent_id": agent_id, "pid": 1, "channels": list(channels)}))
    fh = open(pf, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def _remote(dd, agent_id, last_seen="2026-08-05T21:11:13Z"):
    """Publish a roster entry the way dispatch-gitsync would."""
    rd = dd / ".remote"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"{agent_id}.json").write_text(
        json.dumps({"agent_id": agent_id, "via": "git", "last_seen": last_seen})
    )


def _files(dd, agent_id):
    return sorted((dd / agent_id).glob("*.json")) if (dd / agent_id).is_dir() else []


# ---------------------------------------------------------------------------
# Delivery: a dead session id resolves to the nick behind it
# ---------------------------------------------------------------------------


def test_a_dm_to_a_dead_session_id_reaches_the_live_one(server_factory):
    """The reported case. winze-3932373 had exited ~100 minutes earlier and
    winze-348403 was running; four replies went to the corpse anyway."""
    dd = server_factory.dispatch_dir
    dead = server_factory("winze-3932373")
    dead._release_id("winze-3932373")
    s = server_factory("alpha")
    fh = _hold_presence(dd, "winze-348403")
    try:
        result = s._send("alpha", "winze-3932373", "reply to your bug report")
        assert result["queued_to"] == ["winze-348403"], (
            "addressing a window that has closed must reach the one that is open"
        )
        assert _files(dd, "winze-348403"), "the live session should have the message"
        assert not _files(dd, "winze-3932373"), "nothing may be written to the corpse"
    finally:
        fh.close()


def test_a_dm_to_a_dead_session_id_waits_under_the_nick_when_nothing_is_live(server_factory):
    dd = server_factory.dispatch_dir
    dead = server_factory("winze-3932373")
    dead._release_id("winze-3932373")
    s = server_factory("alpha")

    result = s._send("alpha", "winze-3932373", "for whenever you're back")
    assert result["queued_to"] == ["winze"], "with nothing live it belongs to the nick"
    assert _files(dd, "winze")
    assert not _files(dd, "winze-3932373")


def test_the_dead_session_does_not_get_an_inbox_resurrected_for_it(server_factory):
    """_validate_target used to mkdir the named target before resolution ran,
    leaving an empty spool that reads like a real mailbox to anyone listing."""
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    fh = _hold_presence(dd, "winze-348403")
    try:
        s._send("alpha", "winze-3932373", "hello")
        assert not (dd / "winze-3932373").exists()
    finally:
        fh.close()


def test_a_live_session_id_is_still_addressed_exactly(server_factory):
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    a = _hold_presence(dd, "publicai-222")
    b = _hold_presence(dd, "publicai-333")
    try:
        result = s._send("alpha", "publicai-222", "this exact window")
        assert result["queued_to"] == ["publicai-222"], (
            "naming a live window must not fan out to its siblings"
        )
    finally:
        a.close()
        b.close()


def test_another_hosts_session_id_is_never_degraded_to_a_local_nick(server_factory):
    """The reason resolution can't just strip the suffix unconditionally: every
    session launched from a projects folder is `documents-<pid>` on every host,
    so a cross-host id must go to its own inbox for the bridge to pick up."""
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    _remote(dd, "documents-999")
    fh = _hold_presence(dd, "documents-3099918")
    try:
        result = s._send("alpha", "documents-999", "for the other machine")
        assert result["queued_to"] == ["documents-999"], (
            "a remote id must not be handed to a same-named local session"
        )
        assert not _files(dd, "documents-3099918")
    finally:
        fh.close()


def test_an_unsuffixed_unknown_name_is_untouched(server_factory):
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    assert s._send("alpha", "never-seen", "hello?")["queued_to"] == ["never-seen"]
    assert _files(dd, "never-seen")


# ---------------------------------------------------------------------------
# Wake: the stored `to` matches the reading session's own id, not the typed nick
# ---------------------------------------------------------------------------


def test_a_nick_addressed_dm_stamps_the_resolved_id(server_factory):
    """A firecrawl session reported this from a live incident: a message
    addressed to the nick delivered into the right inbox but was never seen as
    addressed to anyone, because notify_policy's "direct" check is exact-string
    equality and the stored file still said `to: "firecrawl"` while the reading
    session's id was `firecrawl-750492`."""
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    fh = _hold_presence(dd, "firecrawl-750492")
    try:
        result = s._send("alpha", "firecrawl", "reply to your bug report")
        assert result["queued_to"] == ["firecrawl-750492"]
        stored = json.loads(_files(dd, "firecrawl-750492")[0].read_text())
        assert stored["to"] == "firecrawl-750492", (
            "the copy in the live session's own inbox must name that session, "
            "or its own direct-policy watch never fires on it"
        )
        assert notify_policy.should_notify(stored, "direct", "firecrawl-750492")
    finally:
        fh.close()


def test_a_dm_that_waits_under_the_nick_stamps_the_nick(server_factory):
    """With nothing live, the resolved target *is* the nick — there is no
    session id yet to stamp instead."""
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    s._send("alpha", "winze", "for whenever you're back")
    stored = json.loads(_files(dd, "winze")[0].read_text())
    assert stored["to"] == "winze"


def test_broadcast_and_channel_deliveries_keep_the_original_to(server_factory):
    """ "all" and "#channel" are not nick resolution — should_notify's channel
    branch keys off that literal prefix, so stamping would break it instead
    of fixing anything."""
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    fh = _hold_presence(dd, "beta-1", channels=["eng"])
    try:
        s._send("alpha", "all", "hi everyone")
        before = {f.name for f in _files(dd, "beta-1")}
        stored = json.loads(next(iter(_files(dd, "beta-1"))).read_text())
        assert stored["to"] == "all"

        s._send("alpha", "#eng", "channel post")
        new_file = next(f for f in _files(dd, "beta-1") if f.name not in before)
        stored = json.loads(new_file.read_text())
        assert stored["to"] == "#eng"
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# Ownership: the registry outlives presence, which gets reaped
# ---------------------------------------------------------------------------


def test_the_registry_records_the_session_id_that_claimed_the_nick(server_factory):
    dd = server_factory.dispatch_dir
    server_factory("winze-3932373")
    rec = json.loads((dd / ".agents" / "winze.json").read_text())
    assert rec["local_sessions"] == ["winze-3932373"]


def test_a_predecessor_stays_recorded_while_its_spool_exists(server_factory):
    dd = server_factory.dispatch_dir
    prev = server_factory("winze-3932373")
    prev._release_id("winze-3932373")
    # Mail in it, because that is the only state where the record earns its keep —
    # an empty spool is reaped at the next startup anyway (_reap_empty_inboxes).
    (dd / "winze-3932373").mkdir(exist_ok=True)
    (dd / "winze-3932373" / "1-x-a.json").write_text(
        json.dumps({"id": "a", "from": "x", "to": "winze-3932373", "state": "read"})
    )

    server_factory("winze-348403")
    rec = json.loads((dd / ".agents" / "winze.json").read_text())
    assert rec["local_sessions"] == ["winze-348403", "winze-3932373"], (
        "the list has to outlive the spool it describes, newest first"
    )


def test_a_predecessor_drops_out_once_its_spool_is_gone(server_factory):
    dd = server_factory.dispatch_dir
    prev = server_factory("winze-3932373")
    prev._release_id("winze-3932373")
    for f in (dd / "winze-3932373").glob("*"):
        f.unlink()
    (dd / "winze-3932373").rmdir()

    server_factory("winze-348403")
    rec = json.loads((dd / ".agents" / "winze.json").read_text())
    assert rec["local_sessions"] == ["winze-348403"], (
        "with nothing left to misattribute the entry is just growth"
    )


def test_records_written_before_local_sessions_existed_still_answer(server_factory):
    """`last_session_id` predates the list and is written by the same claim, so
    an existing registry answers for its most recent session without waiting for
    that nick to start again."""
    dd = server_factory.dispatch_dir
    reg = dd / ".agents"
    reg.mkdir(parents=True, exist_ok=True)
    (reg / "publicai.json").write_text(
        json.dumps({"nick": "publicai", "last_session_id": "publicai-1767991"})
    )
    assert "publicai-1767991" in dispatch_fs.local_session_ids(dd)


def test_local_session_ids_is_empty_without_a_registry(tmp_path):
    assert dispatch_fs.local_session_ids(tmp_path / "nothing-here") == set()


# ---------------------------------------------------------------------------
# The roster: this host's corpses are not other hosts' agents
# ---------------------------------------------------------------------------


def _bridge(dd, repo):
    import git_bridge

    b = git_bridge.GitBridge.__new__(git_bridge.GitBridge)
    b.dispatch_dir = dd
    b.repo_dir = repo
    return b


def test_a_reaped_local_session_is_not_published_as_remote(server_factory, tmp_path):
    """The root cause. _reap_dead_presence unlinks a dead session's presence file
    at the next startup; classifying by presence alone then made this host's own
    corpse look like another machine's agent."""
    dd = server_factory.dispatch_dir
    prev = server_factory("winze-3932373")
    prev._release_id("winze-3932373")
    s = server_factory("alpha")
    s._reap_dead_presence()
    assert not (dd / ".presence" / "winze-3932373.json").exists()

    repo = tmp_path / "repo"
    (repo / "lanes").mkdir(parents=True)
    (repo / "lanes" / "winze-3932373.jsonl").write_text("")
    (repo / "lanes" / "elsewhere-42.jsonl").write_text("")

    b = _bridge(dd, repo)
    b._write_remote_roster()
    assert not (dd / ".remote" / "winze-3932373.json").exists(), (
        "our own dead session is not another host's agent"
    )
    assert (dd / ".remote" / "elsewhere-42.json").exists(), "a genuine peer still publishes"


def test_a_misclassified_roster_entry_is_pruned_on_the_next_pass(server_factory, tmp_path):
    """The roster is self-pruning, so the entries already on disk clear
    themselves — the stranded mail becomes adoptable without a migration."""
    dd = server_factory.dispatch_dir
    prev = server_factory("winze-3932373")
    prev._release_id("winze-3932373")
    _remote(dd, "winze-3932373")

    repo = tmp_path / "repo"
    (repo / "lanes").mkdir(parents=True)
    (repo / "lanes" / "winze-3932373.jsonl").write_text("")

    _bridge(dd, repo)._write_remote_roster()
    assert not (dd / ".remote" / "winze-3932373.json").exists()


def test_mail_in_a_reaped_predecessors_spool_is_inherited(server_factory):
    """End to end: the message that was unreachable is reachable again."""
    dd = server_factory.dispatch_dir
    prev = server_factory("winze-3932373")
    prev._release_id("winze-3932373")
    sender = server_factory("alpha")
    sender._send("alpha", "winze-3932373", "the reply that went missing")
    # Land it in the corpse the way the old resolution did, then reap presence.
    assert _files(dd, "winze"), "resolution parks it under the nick now"
    (dd / "winze-3932373").mkdir(exist_ok=True)
    for f in _files(dd, "winze"):
        f.rename(dd / "winze-3932373" / f.name)
    sender._reap_dead_presence()

    s = server_factory("winze-348403")
    got = s._read_inbox("winze-348403")
    assert [m["content"] for m in got] == ["the reply that went missing"]
    assert got[0]["_inherited_from"] == "winze-3932373"


def test_a_real_remote_spool_is_still_off_limits(server_factory):
    """The guard the .remote check exists for, unchanged: a same-named session on
    another host is not a previous incarnation of this one."""
    dd = server_factory.dispatch_dir
    (dd / "documents-999").mkdir(parents=True)
    (dd / "documents-999" / "1-x-a.json").write_text(
        json.dumps({"id": "a", "from": "x", "to": "documents-999", "content": "theirs"})
    )
    _remote(dd, "documents-999")

    s = server_factory("documents-3099918")
    assert s._read_inbox("documents-3099918") == [], "another machine's mail stays theirs"
    assert _files(dd, "documents-999"), "and stays where the bridge left it"


# ---------------------------------------------------------------------------
# who(): what steered the sender wrong
# ---------------------------------------------------------------------------


def test_who_marks_a_cold_remote_entry_stale_and_names_its_nick(server_factory):
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    _remote(dd, "mcp-dispatch-2995699", last_seen="2026-08-03T12:00:00Z")

    entry = next(r for r in s.who_tool()["remote"] if r["agent_id"] == "mcp-dispatch-2995699")
    assert entry["stale"] is True
    assert entry["nick"] == "mcp-dispatch", "the name that actually resolves"
    assert entry["age"].endswith("d")
    assert "Address the `nick`" in s.who_tool()["remote_note"]


def test_who_leaves_a_warm_remote_entry_unflagged(server_factory):
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _remote(dd, "elsewhere-42", last_seen=fresh)

    out = s.who_tool()
    entry = next(r for r in out["remote"] if r["agent_id"] == "elsewhere-42")
    assert "stale" not in entry
    assert entry["age"] == "0m"
    assert "remote_note" not in out, "the note is for a list with something wrong in it"


def test_who_names_the_relay_it_is_talking_about(server_factory):
    """An agent that goes to check something on disk needs this path. Without it
    the only directory who() named was the per-session `state_dir`, which holds
    arm locks and none of the relay's state — so the check comes back empty and
    reads as "wrong relay" rather than as an answer."""
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    out = s.who_tool()
    assert out["relay"] == str(dd)
    assert (Path(out["relay"]) / ".presence").is_dir(), "and it is the one with the state under it"


def test_a_roster_entry_with_no_timestamp_is_not_guessed_at(server_factory):
    dd = server_factory.dispatch_dir
    s = server_factory("alpha")
    rd = dd / ".remote"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "elsewhere-42.json").write_text(
        json.dumps({"agent_id": "elsewhere-42", "via": "git", "last_seen": None})
    )

    entry = next(r for r in s.who_tool()["remote"] if r["agent_id"] == "elsewhere-42")
    assert "age" not in entry and "stale" not in entry
    assert entry["nick"] == "elsewhere", "the nick is derivable without a clock"
