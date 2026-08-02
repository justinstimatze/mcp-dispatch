"""The away-digest: what changed while a nick had no session.

The parts worth pinning are the ones where a wrong answer is *invisible*: the
window boundary (too narrow silently hides things), the read-is-not-consume
property (a cursor would drop content on a crash), and the channel gap (an empty
section reads as "nothing happened" when it means "no record exists").
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import digest

T0 = "2026-08-01T00:00:00Z"
T1 = "2026-08-01T12:00:00Z"  # inside the window
T2 = "2026-08-02T00:00:00Z"
NOW = "2026-08-03T00:00:00Z"


def _plant(dd: Path, agent: str, *, mid: str, sender="bob", state="pending", **extra):
    inbox = dd / agent
    inbox.mkdir(parents=True, exist_ok=True)
    msg = {
        "id": mid,
        "from": sender,
        "to": agent,
        "content": "hi",
        "timestamp": T1,
        "state": state,
        **extra,
    }
    (inbox / f"{mid}.json").write_text(json.dumps(msg))


def _task(dd: Path, tid: str, **fields):
    d = dd / ".tasks"
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": tid,
        "title": f"title {tid}",
        "created_by": "bob",
        "created_at": T1,
        "state": "open",
        "claimed_by": None,
        "claimed_at": None,
        "done_at": None,
        "target": None,
        **fields,
    }
    (d / f"{tid}.json").write_text(json.dumps(rec))


def _agent_rec(dd: Path, nick: str, **fields):
    d = dd / ".agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{nick}.json").write_text(json.dumps({"nick": nick, **fields}))


@pytest.fixture
def relay(tmp_path):
    dd = tmp_path / "relay"
    dd.mkdir()
    return dd


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------


def test_previous_seen_is_the_window_start(relay):
    _agent_rec(relay, "proj", first_seen=T0, previous_seen=T2, last_seen=NOW)
    since, exact = digest.watermark(relay, "proj")
    assert (since, exact) == (T2, True)


def test_a_nick_with_no_previous_seen_reports_an_inexact_window(relay):
    """A first-ever session, or a record written before this feature existed.
    first_seen is the widest honest guess — and a caller that treated a guess as
    a measurement would over-claim, so say which it is."""
    _agent_rec(relay, "proj", first_seen=T0, last_seen=NOW)
    since, exact = digest.watermark(relay, "proj")
    assert (since, exact) == (T0, False)
    assert "approximate" in digest.render(digest.build(relay, "proj", now=NOW))


def test_an_unknown_nick_does_not_invent_a_window(relay):
    assert digest.watermark(relay, "ghost") == ("", False)


def test_the_window_is_half_open(relay):
    """Exactly-at-since is the previous session's last moment — already seen."""
    _agent_rec(relay, "proj", previous_seen=T1)
    _task(relay, "task-edge", created_at=T1)  # == since, excluded
    _task(relay, "task-in", created_at=T2)  # inside
    d = digest.build(relay, "proj", now=NOW)
    assert [t.task_id for t in d.tasks] == ["task-in"]


# ---------------------------------------------------------------------------
# mail
# ---------------------------------------------------------------------------


def test_mail_is_grouped_by_sender_with_flags(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    _plant(relay, "proj", mid="m1", sender="alice")
    _plant(relay, "proj", mid="m2", sender="alice", priority="urgent")
    _plant(relay, "proj", mid="m3", sender="carol", must_read=True)
    d = digest.build(relay, "proj", now=NOW)
    by = {m.sender: m for m in d.mail}
    assert by["alice"].count == 2
    assert by["alice"].urgent == 1
    assert by["carol"].must_read == 1
    assert d.mail_total == 3
    # Urgent first: the point of the ordering is that it survives truncation.
    assert d.mail[0].sender == "alice"


def test_mail_predating_the_window_still_counts(relay):
    """Unread is unread. A message older than the window is more overdue, not
    less — scoping mail to the window would hide exactly the worst backlog."""
    _agent_rec(relay, "proj", previous_seen=T2)
    _plant(relay, "proj", mid="old", timestamp=T0)
    assert digest.build(relay, "proj", now=NOW).mail_total == 1


def test_read_and_expired_mail_are_not_reported(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    _plant(relay, "proj", mid="seen", state="read")
    _plant(relay, "proj", mid="stale", ttl=1, timestamp="2020-01-01T00:00:00Z")
    assert digest.build(relay, "proj", now=NOW).mail == []


def test_mail_in_a_session_inbox_belongs_to_the_nick(relay):
    """After inheritance the mail lives in `proj-<pid>/`, not `proj/`."""
    _agent_rec(relay, "proj", previous_seen=T0)
    _plant(relay, "proj-4242", mid="m1")
    assert digest.build(relay, "proj", now=NOW).mail_total == 1


def test_another_nicks_mail_is_not_reported(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    _plant(relay, "other", mid="m1")
    _plant(relay, "other-1", mid="m2")
    assert digest.build(relay, "proj", now=NOW).mail == []


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


def test_each_transition_in_the_window_is_an_event(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    _task(relay, "task-1", created_at=T1, claimed_at=T2, claimed_by="alice", state="claimed")
    d = digest.build(relay, "proj", now=NOW)
    assert [(e.kind, e.who) for e in d.tasks] == [("created", "bob"), ("claimed", "alice")]


def test_transitions_outside_the_window_are_not_events(relay):
    _agent_rec(relay, "proj", previous_seen=T2)
    _task(relay, "task-old", created_at=T0, claimed_at=T1, claimed_by="alice")
    assert digest.build(relay, "proj", now=NOW).tasks == []


def test_a_task_aimed_at_me_is_marked(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    _task(relay, "task-mine", target="proj")
    _task(relay, "task-theirs", target="other")
    d = digest.build(relay, "proj", now=NOW)
    assert {e.task_id: e.mine for e in d.tasks} == {"task-mine": True, "task-theirs": False}
    assert [t.task_id for t in d.open_tasks_for_me] == ["task-mine"]


def test_a_task_aimed_at_my_channel_is_mine(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    _task(relay, "task-eng", target="#eng")
    d = digest.build(relay, "proj", now=NOW, channels={"eng"})
    assert [t.task_id for t in d.open_tasks_for_me] == ["task-eng"]
    # ...and not if I don't stand in that room.
    assert digest.build(relay, "proj", now=NOW, channels=set()).open_tasks_for_me == []


def test_an_untargeted_task_is_open_work_not_a_personal_ping(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    _task(relay, "task-loose", target=None)
    d = digest.build(relay, "proj", now=NOW)
    assert d.open_tasks_for_me == []
    assert [e.mine for e in d.tasks] == [False]


def test_a_done_task_is_not_open_work(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    _task(relay, "task-done", target="proj", state="done", done_at=T2, claimed_by="proj-1")
    d = digest.build(relay, "proj", now=NOW)
    assert d.open_tasks_for_me == []
    assert [e.kind for e in d.tasks] == ["created", "done"]


# ---------------------------------------------------------------------------
# teammates
# ---------------------------------------------------------------------------


def test_who_was_around_while_i_was_gone(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    _agent_rec(relay, "alice", last_seen=T1)  # active in the window
    _agent_rec(relay, "carol", last_seen="2026-07-01T00:00:00Z")  # long gone
    d = digest.build(relay, "proj", now=NOW)
    assert d.active_while_away == ["alice"]


def test_i_am_not_my_own_teammate(relay):
    _agent_rec(relay, "proj", previous_seen=T0, last_seen=T1)
    assert digest.build(relay, "proj", now=NOW).active_while_away == []


# ---------------------------------------------------------------------------
# the properties that matter
# ---------------------------------------------------------------------------


def test_reading_is_not_consuming(relay):
    """No cursor advances and nothing is deleted, so asking twice gives the same
    answer. A read cursor would be at-most-once delivery for exactly the content
    you cannot afford to drop — a session that crashed after generating a digest
    would lose it."""
    _agent_rec(relay, "proj", previous_seen=T0)
    _plant(relay, "proj", mid="m1")
    _task(relay, "task-1", created_at=T1)

    first = digest.render(digest.build(relay, "proj", now=NOW))
    second = digest.render(digest.build(relay, "proj", now=NOW))
    assert first == second
    assert (relay / "proj" / "m1.json").exists()
    rec = json.loads((relay / ".agents" / "proj.json").read_text())
    assert rec["previous_seen"] == T0  # untouched


def test_the_channel_gap_is_stated_not_implied(relay):
    """Channel posts fan out to live subscribers only, so one made while this nick
    was offline left no local trace — absent, not unread. A silent empty section
    would read as 'nothing happened' and be believed."""
    _agent_rec(relay, "proj", previous_seen=T0)
    out = digest.render(digest.build(relay, "proj", now=NOW))
    assert "live subscribers only" in out
    assert "does not read them yet" in out


def test_an_empty_digest_says_so(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    d = digest.build(relay, "proj", now=NOW)
    assert d.empty
    assert "Nothing waiting and nothing moved." in digest.render(d)


def test_a_missing_relay_does_not_raise(tmp_path):
    d = digest.build(tmp_path / "nope", "proj", now=NOW)
    assert d.empty


def test_unreadable_records_are_skipped_not_fatal(relay):
    _agent_rec(relay, "proj", previous_seen=T0)
    (relay / ".tasks").mkdir()
    (relay / ".tasks" / "broken.json").write_text("{not json")
    (relay / "proj").mkdir()
    (relay / "proj" / "broken.json").write_text("{also not json")
    d = digest.build(relay, "proj", now=NOW)
    assert d.tasks == [] and d.mail == []


# ---------------------------------------------------------------------------
# the watermark, against the real server
# ---------------------------------------------------------------------------


def test_the_server_preserves_the_watermark_across_a_restart(server_factory):
    """The value `_register_agent` is about to overwrite is the only record of
    when this nick was last present. Asserted against the real claim path."""
    dd = server_factory.dispatch_dir
    dd.mkdir(parents=True, exist_ok=True)

    first = server_factory("proj-111")
    rec_path = dd / ".agents" / "proj.json"
    ended_at = json.loads(rec_path.read_text())["last_seen"]
    first._release_id("proj-111")  # a clean exit stamps last_seen, drops the lock

    time.sleep(1.1)  # timestamps are whole seconds; make the two distinguishable
    server_factory("proj-222")
    rec = json.loads(rec_path.read_text())
    assert rec["previous_seen"] == ended_at
    assert rec["last_seen"] > ended_at
    since, exact = digest.watermark(dd, "proj")
    assert (since, exact) == (ended_at, True)


def test_a_sibling_session_does_not_advance_the_watermark(server_factory):
    """Two windows of one teammate. The nick never went away, so the second must
    not collapse the window to nothing and hide what the first hasn't handled."""
    dd = server_factory.dispatch_dir
    dd.mkdir(parents=True, exist_ok=True)
    server_factory("proj-111")  # holds presence for the rest of the test
    rec_path = dd / ".agents" / "proj.json"
    watermark_after_first = json.loads(rec_path.read_text()).get("previous_seen")

    time.sleep(1.1)
    server_factory("proj-222")
    assert json.loads(rec_path.read_text()).get("previous_seen") == watermark_after_first
