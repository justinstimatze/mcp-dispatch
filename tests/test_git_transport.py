"""Tests for the git-backed transport (git_transport.GitBus).

Exercised against a real local git bus: a bare repo as the "server" and two
working clones as two agents on different machines. No network / GitHub needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_transport import WIRE_VERSION, Cursor, Envelope, GitBus


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def bus_pair(tmp_path: Path):
    """A bare 'server' repo + two clones acting as two agents."""
    bare = tmp_path / "bus.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))

    # Seed the bare repo with an initial commit so clones share a branch.
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "README").write_text("mcp-dispatch git bus\n")
    _git(seed, "-c", "user.name=seed", "-c", "user.email=seed@x", "add", "README")
    _git(seed, "-c", "user.name=seed", "-c", "user.email=seed@x", "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", "origin", "main")

    alice_dir = tmp_path / "alice"
    bob_dir = tmp_path / "bob"
    _git(tmp_path, "clone", "-q", str(bare), str(alice_dir))
    _git(tmp_path, "clone", "-q", str(bare), str(bob_dir))

    alice = GitBus(alice_dir, "alice", remote="origin")
    bob = GitBus(bob_dir, "bob", remote="origin")
    return alice, bob


def test_envelope_roundtrip():
    env = Envelope(type="message", from_="alice", to="bob", body={"content": "hi"}, key=None, seq=3)
    again = Envelope.from_json(env.to_json())
    assert again.from_ == "alice"
    assert again.to == "bob"
    assert again.body == {"content": "hi"}
    assert again.seq == 3
    assert again.version == WIRE_VERSION


def test_envelope_uses_git_field_names():
    env = Envelope(type="message", from_="alice", to="bob", body={})
    line = env.to_json()
    assert '"from":"alice"' in line
    assert '"to":"bob"' in line
    # 11-field header present, body included.
    import json

    keys = set(json.loads(line).keys())
    assert keys == {
        "type",
        "from",
        "to",
        "chan",
        "key",
        "id",
        "ts",
        "seq",
        "ttl",
        "version",
        "sig",
        "body",
    }


def test_dm_delivery(bus_pair):
    alice, bob = bus_pair
    alice.publish({"content": "hello bob"}, to="bob")
    received = bob.receive()
    assert len(received) == 1
    assert received[0].from_ == "alice"
    assert received[0].body == {"content": "hello bob"}


def test_cursor_advances_no_redelivery(bus_pair):
    alice, bob = bus_pair
    alice.publish({"content": "one"}, to="bob")
    assert len(bob.receive()) == 1
    # Second receive with nothing new returns empty (cursor advanced).
    assert bob.receive() == []
    alice.publish({"content": "two"}, to="bob")
    again = bob.receive()
    assert len(again) == 1
    assert again[0].body["content"] == "two"


def test_dm_not_addressed_to_me_is_filtered(bus_pair):
    alice, bob = bus_pair
    alice.publish({"content": "for carol"}, to="carol")
    # bob is not the recipient; nothing delivered to him.
    assert bob.receive() == []


def test_own_messages_excluded(bus_pair):
    alice, _bob = bus_pair
    alice.publish({"content": "to bob"}, to="bob")
    # Alice never receives her own lane records.
    assert alice.receive() == []


def test_channel_requires_subscription(bus_pair):
    alice, bob = bus_pair
    alice.subscribe("general")
    bob.subscribe("general")
    alice.publish({"content": "hi all"}, chan="general")
    assert len(bob.receive()) == 1

    # A non-subscriber gets nothing.
    bob.unsubscribe("general")
    alice.publish({"content": "still here"}, chan="general")
    assert bob.receive() == []


def test_seq_is_per_lane_monotonic(bus_pair):
    alice, bob = bus_pair
    alice.publish({"content": "a"}, to="bob")
    alice.publish({"content": "b"}, to="bob")
    alice.publish({"content": "c"}, to="bob")
    got = bob.receive()
    assert [e.seq for e in got] == [0, 1, 2]


def test_collect_lww_per_author(bus_pair):
    alice, bob = bus_pair
    # No key → partition is per-author; latest wins.
    alice.publish({"v": 1}, to="bob", type="atom")
    alice.publish({"v": 2}, to="bob", type="atom")
    snap = bob.collect(type_filter="atom")
    assert len(snap) == 1
    assert snap[0].body == {"v": 2}


def test_collect_lww_per_key(bus_pair):
    alice, bob = bus_pair
    # Distinct keys → distinct partitions, each keeps its latest.
    alice.publish({"v": "x1"}, to="bob", type="atom", key="slot-x")
    alice.publish({"v": "y1"}, to="bob", type="atom", key="slot-y")
    alice.publish({"v": "x2"}, to="bob", type="atom", key="slot-x")
    snap = {(e.from_, e.key): e.body for e in bob.collect(type_filter="atom")}
    assert snap[("alice", "slot-x")] == {"v": "x2"}
    assert snap[("alice", "slot-y")] == {"v": "y1"}


def test_concurrent_publishers_no_conflict(bus_pair, tmp_path):
    """Two agents pushing interleaved never conflict (distinct lane files)."""
    alice, bob = bus_pair
    alice.subscribe("general")
    bob.subscribe("general")
    alice.publish({"content": "a1"}, chan="general")
    bob.publish({"content": "b1"}, chan="general")
    alice.publish({"content": "a2"}, chan="general")
    bob.publish({"content": "b2"}, chan="general")

    # Each sees the other's two channel posts.
    alice_in = [e.body["content"] for e in alice.receive()]
    bob_in = [e.body["content"] for e in bob.receive()]
    assert sorted(alice_in) == ["b1", "b2"]
    assert sorted(bob_in) == ["a1", "a2"]


def test_exactly_one_of_to_or_chan(bus_pair):
    alice, _bob = bus_pair
    with pytest.raises(ValueError):
        alice.publish({"x": 1})  # neither
    with pytest.raises(ValueError):
        alice.publish({"x": 1}, to="bob", chan="general")  # both


def test_cursor_persists_across_instances(bus_pair, tmp_path):
    alice, bob = bus_pair
    alice.publish({"content": "persist me"}, to="bob")
    assert len(bob.receive()) == 1
    # A fresh GitBus over the same repo/state must not re-deliver.
    bob2 = GitBus(bob.repo_dir, "bob", remote="origin")
    assert bob2.receive() == []


def test_cursor_load_save(tmp_path: Path):
    p = tmp_path / "cur.json"
    c = Cursor.load(p)
    c.consumed["lanes/alice.jsonl"] = 5
    c.save()
    c2 = Cursor.load(p)
    assert c2.consumed["lanes/alice.jsonl"] == 5
