"""bin/dispatch-tail git-bus reading — the pure scan/merge/format helpers.

dispatch-tail is a script (no .py suffix), loaded here via importlib so we can
unit-test its helpers directly. The behavior under test: with [git] configured it
reads the bus lanes too, deduping a materialized inbox copy against its lane
record and marking bus-origin messages «remote», while ignoring non-message
(atom/ack/presence) records that share the lanes.
"""

from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_tail():
    # dispatch-tail has no .py suffix, so give importlib an explicit source loader.
    loader = SourceFileLoader("dispatch_tail", str(REPO_ROOT / "bin" / "dispatch-tail"))
    spec = importlib.util.spec_from_loader("dispatch_tail", loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


tail = _load_tail()


def _lane(repo: Path, owner: str, *records: dict) -> None:
    (repo / "lanes").mkdir(parents=True, exist_ok=True)
    lines = "\n".join(json.dumps(r) for r in records) + "\n"
    (repo / "lanes" / f"{owner}.jsonl").write_text(lines)


def _msg_env(mid: str, frm: str, to: str, content: str, **extra) -> dict:
    body = {
        "id": mid,
        "from": frm,
        "to": to,
        "timestamp": "2026-07-10T18:00:00Z",
        "priority": "normal",
        "content": content,
        "state": "pending",
        **extra,
    }
    return {
        "type": "message",
        "from": frm,
        "to": to,
        "chan": None,
        "key": None,
        "id": f"env-{mid}",
        "ts": "2026-07-10T18:00:00Z",
        "seq": 0,
        "ttl": None,
        "version": 1,
        "sig": None,
        "body": body,
    }


def _inbox(relay: Path, owner: str, mid: str, frm: str, **extra) -> None:
    (relay / owner).mkdir(parents=True, exist_ok=True)
    msg = {
        "id": mid,
        "from": frm,
        "to": owner,
        "timestamp": "2026-07-10T18:00:00Z",
        "priority": "normal",
        "content": "x",
        "payload": None,
        "thread_id": None,
        "reply_to": None,
        "ttl": None,
        "must_read": False,
        "state": "pending",
        **extra,
    }
    (relay / owner / f"1780000000000-{frm}-aaaa.json").write_text(json.dumps(msg))


def test_scan_git_reads_message_and_tags_via(tmp_path):
    repo = tmp_path / "bus"
    _lane(repo, "carol", _msg_env("m1", "carol", "dave", "cross-host"))
    got = tail._scan_git(repo)
    assert [m["id"] for m in got] == ["m1"]
    assert got[0]["_via"] == "git"  # so the feed marks it «remote»


def test_scan_git_skips_non_message_records(tmp_path):
    repo = tmp_path / "bus"
    atom = {
        "type": "atom",
        "from": "ettle",
        "to": None,
        "chan": "room",
        "key": "p",
        "id": "e",
        "ts": "t",
        "seq": 1,
        "ttl": None,
        "version": 1,
        "sig": None,
        "body": {"state": 1},
    }
    _lane(repo, "ettle", atom, _msg_env("m2", "x", "y", "real message"))
    ids = [m["id"] for m in tail._scan_git(repo)]
    assert ids == ["m2"]  # the atom is not chat and must not appear


def test_scan_all_dedups_materialized_copy(tmp_path):
    relay, repo = tmp_path / "relay", tmp_path / "bus"
    # Same logical message present BOTH as a materialized inbox file and its lane.
    _inbox(relay, "dave", "m3", "carol", _via="git")
    _lane(repo, "carol", _msg_env("m3", "carol", "dave", "hi"))
    msgs = tail._scan_all(relay, repo)
    assert [m["id"] for m in msgs] == ["m3"]  # shown once, not twice


def test_scan_all_surfaces_lane_only_remote(tmp_path):
    relay, repo = tmp_path / "relay", tmp_path / "bus"
    relay.mkdir()
    # A DM between two OTHER hosts: only on the bus, never materialized locally.
    _lane(repo, "carol", _msg_env("m4", "carol", "dave", "not for us"))
    msgs = tail._scan_all(relay, repo)
    assert len(msgs) == 1 and msgs[0]["id"] == "m4" and msgs[0]["_via"] == "git"


def test_scan_all_without_git_is_inbox_only(tmp_path):
    relay = tmp_path / "relay"
    _inbox(relay, "alice", "m5", "bob")
    msgs = tail._scan_all(relay, None)  # repo_dir=None → local-only, unchanged
    assert [m["id"] for m in msgs] == ["m5"]


def test_format_marks_remote(tmp_path):
    remote = tail._format({"from": "carol", "to": "dave", "content": "hi", "_via": "git"}, False)
    local = tail._format({"from": "carol", "to": "dave", "content": "hi"}, False)
    assert "«remote»" in remote
    assert "«remote»" not in local
