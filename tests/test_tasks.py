"""Tasks: work items that survive being read.

A message says something and is deleted on ack; a task is something to *do* and
has to outlive the reading. The only genuinely hard part is claiming — two
agents that both read "open" and both write "claimed" have silently duplicated
the work — so the claim is an O_EXCL create and most of these tests are about
that one property.
"""

from __future__ import annotations

import json
import os

import pytest


def _tasks(dd):
    return sorted((dd / ".tasks").glob("task-*.json"))


def test_create_makes_an_open_task(server):
    rec = server._create_task("alpha", "fix the flake", "it's the clock", None)
    assert rec["state"] == "open"
    assert rec["title"] == "fix the flake"
    assert rec["detail"] == "it's the clock"
    assert rec["created_by"] == "alpha"
    assert rec["claimed_by"] is None
    assert rec["id"].startswith("task-")
    assert _tasks(server.DISPATCH_DIR), "the task must be on disk, not just in the reply"


def test_create_rejects_an_empty_or_huge_title(server):
    with pytest.raises(ValueError):
        server._create_task("alpha", "   ", "", None)
    with pytest.raises(ValueError):
        server._create_task("alpha", "x" * 201, "", None)


def test_claim_is_exclusive(server):
    rec = server._create_task("alpha", "deploy", "", None)
    claimed = server._claim_task(rec["id"], "alpha")
    assert claimed["state"] == "claimed"
    assert claimed["claimed_by"] == "alpha"

    with pytest.raises(ValueError, match="already claimed by 'alpha'"):
        server._claim_task(rec["id"], "bob")


def test_reclaiming_your_own_task_is_idempotent(server):
    rec = server._create_task("alpha", "deploy", "", None)
    server._claim_task(rec["id"], "alpha")
    again = server._claim_task(rec["id"], "alpha")
    assert again["claimed_by"] == "alpha", "a retry must not look like a conflict"


def test_the_claim_marker_is_what_decides(server):
    """The record is bookkeeping; the O_EXCL marker is the claim.

    Rewriting the record to look unclaimed must not hand the task to someone
    else — otherwise a crash mid-write, or a stale reader, reopens the race.
    """
    rec = server._create_task("alpha", "deploy", "", None)
    server._claim_task(rec["id"], "alpha")

    path = server._task_path(rec["id"])
    doctored = json.loads(path.read_text())
    doctored["state"] = "open"
    doctored["claimed_by"] = None
    path.write_text(json.dumps(doctored))

    with pytest.raises(ValueError, match="already claimed"):
        server._claim_task(rec["id"], "bob")


def test_concurrent_claims_produce_exactly_one_winner(server):
    """Simulate the real race: many claimants, one task, no coordination."""
    rec = server._create_task("alpha", "the contended one", "", None)
    winners = []
    for who in [f"agent-{i}" for i in range(20)]:
        try:
            winners.append(server._claim_task(rec["id"], who)["claimed_by"])
        except ValueError:
            pass
    assert len(winners) == 1, f"exactly one agent may win, got {winners}"


def test_done_requires_being_the_holder(server):
    rec = server._create_task("alpha", "deploy", "", None)
    server._claim_task(rec["id"], "alpha")
    with pytest.raises(ValueError, match="claimed by 'alpha', not you"):
        server._complete_task(rec["id"], "bob")

    done = server._complete_task(rec["id"], "alpha")
    assert done["state"] == "done"
    assert done["done_at"]


def test_an_unclaimed_task_can_be_completed_directly(server):
    rec = server._create_task("alpha", "quick one", "", None)
    done = server._complete_task(rec["id"], "alpha")
    assert done["state"] == "done"
    assert done["claimed_by"] == "alpha", "whoever finishes it owns it"


def test_a_done_task_cannot_be_claimed(server):
    rec = server._create_task("alpha", "over", "", None)
    server._complete_task(rec["id"], "alpha")
    with pytest.raises(ValueError, match="already done"):
        server._claim_task(rec["id"], "bob")


def test_list_filters_by_state(server):
    a = server._create_task("alpha", "one", "", None)
    b = server._create_task("alpha", "two", "", None)
    server._create_task("alpha", "three", "", None)
    server._claim_task(a["id"], "alpha")
    server._complete_task(b["id"], "alpha")

    assert len(server._list_tasks()) == 3
    assert [t["title"] for t in server._list_tasks("open")] == ["three"]
    assert [t["title"] for t in server._list_tasks("claimed")] == ["one"]
    assert [t["title"] for t in server._list_tasks("done")] == ["two"]


def test_unknown_task_is_an_error_not_a_blank(server):
    with pytest.raises(ValueError, match="No such task"):
        server._read_task("task-deadbeef")


def test_task_id_cannot_escape_the_relay(server):
    for bad in ["../../etc/passwd", "a/b", "..", "foo bar"]:
        with pytest.raises(ValueError):
            server._task_path(bad)


def test_task_files_are_owner_only(server):
    rec = server._create_task("alpha", "private work", "", None)
    mode = os.stat(server._task_path(rec["id"])).st_mode & 0o777
    assert mode & 0o077 == 0, f"task file is {oct(mode)} — readable beyond its owner"


# ---------------------------------------------------------------------------
# The tool surface
# ---------------------------------------------------------------------------


def test_tool_create_announces_over_the_normal_message_path(server):
    result = server.task_tool("create", title="ship it", target="bob")
    assert result["task"]["state"] == "open"
    assert result["announced_to"] == ["bob"]

    # The announcement is an ordinary message, so it wakes a parked session and
    # crosses hosts through the paths that already exist.
    msgs = list((server.DISPATCH_DIR / "bob").glob("*.json"))
    assert len(msgs) == 1
    msg = json.loads(msgs[0].read_text())
    assert msg["payload"]["type"] == "task.created"
    assert msg["payload"]["task"]["id"] == result["task"]["id"]
    assert result["task"]["id"] in msg["content"]


def test_tool_create_without_a_target_announces_nothing(server):
    result = server.task_tool("create", title="just for me")
    assert result["announced_to"] == []


def test_tool_claim_done_and_list(server):
    created = server.task_tool("create", title="round trip")["task"]
    assert server.task_tool("claim", task_id=created["id"])["task"]["state"] == "claimed"
    assert server.task_tool("done", task_id=created["id"])["task"]["state"] == "done"
    listed = server.task_tool("list", state="done")
    assert listed["count"] == 1
    assert listed["tasks"][0]["id"] == created["id"]


def test_tool_rejects_unknown_action_and_state(server):
    with pytest.raises(ValueError, match="Unknown action"):
        server.task_tool("frobnicate")
    with pytest.raises(ValueError, match="Unknown state"):
        server.task_tool("list", state="pending")
