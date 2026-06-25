"""Tests for the local-bus <-> git replicator (git_bridge.GitBridge).

Two hosts, no network: a bare 'server' repo + two working clones (one per host),
each paired with its own DISPATCH_DIR and a GitBridge. We simulate the local file
bus by writing inbox files exactly as server._send does, tick the bridges, and
assert messages cross from one host's DISPATCH_DIR to the other's.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import dispatch_fs
from git_bridge import GitBridge


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _make_msg(frm: str, to: str, content: str, **extra) -> dict:
    """A local message dict shaped exactly like server._send builds."""
    return {
        "id": f"msg-{uuid.uuid4().hex[:8]}",
        "from": frm,
        "to": to,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "priority": "normal",
        "content": content,
        "payload": None,
        "thread_id": extra.get("thread_id"),
        "reply_to": None,
        "ttl": extra.get("ttl"),
        "must_read": False,
        "state": "pending",
    }


def _local_dm(dispatch_dir: Path, frm: str, to: str, content: str) -> dict:
    """Mimic server._send delivering a DM into the recipient's local inbox."""
    msg = _make_msg(frm, to, content)
    inbox = dispatch_dir / to
    inbox.mkdir(parents=True, exist_ok=True)
    dispatch_fs.atomic_write(inbox / dispatch_fs.message_filename(frm), msg)
    return msg


def _local_channel(dispatch_dir: Path, frm: str, chan: str, content: str, subs) -> dict:
    """Mimic server._send fanning a channel post into each subscriber's inbox."""
    msg = _make_msg(frm, f"#{chan}", content)
    for sub in subs:
        if sub == frm:
            continue  # senders don't get their own channel post (server behaviour)
        inbox = dispatch_dir / sub
        inbox.mkdir(parents=True, exist_ok=True)
        dispatch_fs.atomic_write(inbox / dispatch_fs.message_filename(frm), dict(msg))
    return msg


def _live_presence(holds: list, dispatch_dir: Path, agent_id: str, channels=None) -> None:
    """Create a presence file and hold its flock so presence_is_live() == True.

    The open handle is appended to `holds` so the caller keeps it alive for the
    test's duration (closing it would release the lock = 'agent went offline').
    """
    pdir = dispatch_dir / ".presence"
    pdir.mkdir(parents=True, exist_ok=True)
    pf = pdir / f"{agent_id}.json"
    pf.write_text(json.dumps({"agent_id": agent_id, "channels": channels or []}))
    fh = open(pf, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    holds.append(fh)


def _inbox_files(dispatch_dir: Path, agent_id: str) -> list[dict]:
    inbox = dispatch_dir / agent_id
    if not inbox.is_dir():
        return []
    out = []
    for f in sorted(inbox.glob("*.json")):
        out.append(json.loads(f.read_text()))
    return out


@pytest.fixture
def hosts(tmp_path: Path):
    """A bare repo + two host clones, each with its own DISPATCH_DIR + GitBridge."""
    bare = tmp_path / "bus.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))

    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "README").write_text("mcp-dispatch git bus\n")
    _git(seed, "-c", "user.name=s", "-c", "user.email=s@x", "add", "README")
    _git(seed, "-c", "user.name=s", "-c", "user.email=s@x", "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", "origin", "main")

    repo_a = tmp_path / "repoA"
    repo_b = tmp_path / "repoB"
    _git(tmp_path, "clone", "-q", str(bare), str(repo_a))
    _git(tmp_path, "clone", "-q", str(bare), str(repo_b))

    dir_a = tmp_path / "dirA"
    dir_b = tmp_path / "dirB"
    dir_a.mkdir()
    dir_b.mkdir()

    return SimpleNamespace(
        dir_a=dir_a,
        dir_b=dir_b,
        repo_a=repo_a,
        repo_b=repo_b,
        a=GitBridge(dir_a, repo_a, remote="origin"),
        b=GitBridge(dir_b, repo_b, remote="origin"),
        holds=[],  # keep presence flocks alive
    )


def test_dm_crosses_hosts(hosts):
    _local_dm(hosts.dir_a, "alice", "bob", "hello bob")
    hosts.a.tick()  # outbound: publish to git (bob not live-local on A)
    hosts.b.tick()  # inbound: materialize into dirB/bob

    got = _inbox_files(hosts.dir_b, "bob")
    assert len(got) == 1
    assert got[0]["content"] == "hello bob"
    assert got[0]["from"] == "alice"
    assert got[0]["state"] == "pending"
    assert got[0]["_via"] == "git"


def test_no_echo_on_origin_host(hosts):
    _local_dm(hosts.dir_a, "alice", "bob", "hi")
    hosts.a.tick()
    hosts.a.tick()  # re-fetches our own published record
    # dirA/bob still holds exactly the one original local file — no echo.
    assert len(_inbox_files(hosts.dir_a, "bob")) == 1


def test_reply_round_trips(hosts):
    _local_dm(hosts.dir_a, "alice", "bob", "ping")
    hosts.a.tick()
    hosts.b.tick()
    assert _inbox_files(hosts.dir_b, "bob")[0]["content"] == "ping"

    _local_dm(hosts.dir_b, "bob", "alice", "pong")
    hosts.b.tick()
    hosts.a.tick()
    alice_msgs = _inbox_files(hosts.dir_a, "alice")
    assert [m["content"] for m in alice_msgs] == ["pong"]
    assert alice_msgs[0]["_via"] == "git"


def test_channel_fans_out_to_remote_subscribers(hosts):
    # A local subscriber on host A carries the post onto git; carol/dave on host
    # B are the remote subscribers that should receive it.
    _live_presence(hosts.holds, hosts.dir_a, "frank", channels=["eng"])
    _live_presence(hosts.holds, hosts.dir_b, "carol", channels=["eng"])
    _live_presence(hosts.holds, hosts.dir_b, "dave", channels=["eng"])

    _local_channel(hosts.dir_a, "alice", "eng", "team update", subs=["frank"])
    hosts.a.tick()
    hosts.b.tick()

    for sub in ("carol", "dave"):
        got = _inbox_files(hosts.dir_b, sub)
        assert len(got) == 1, sub
        assert got[0]["content"] == "team update"
        assert got[0]["to"] == "#eng"
        assert got[0]["_via"] == "git"


def test_remote_only_keeps_live_local_dm_off_git(hosts):
    # bob is live-local on host A: under the default mirror=remote-only the local
    # bus already delivered, so nothing should reach git / host B.
    _live_presence(hosts.holds, hosts.dir_a, "bob")
    _local_dm(hosts.dir_a, "alice", "bob", "local only")
    hosts.a.tick()
    hosts.b.tick()

    assert len(_inbox_files(hosts.dir_a, "bob")) == 1  # original local copy intact
    assert _inbox_files(hosts.dir_b, "bob") == []  # never bridged
    assert not (hosts.repo_a / "lanes" / "alice.jsonl").exists()


def test_mirror_all_bridges_a_live_local_dm(tmp_path: Path, hosts):
    # Same as above but mirror="all" — a full replica bridges even live-local DMs.
    hosts.a.mirror = "all"
    _live_presence(hosts.holds, hosts.dir_a, "bob")
    _local_dm(hosts.dir_a, "alice", "bob", "replicate me")
    hosts.a.tick()
    hosts.b.tick()

    got = _inbox_files(hosts.dir_b, "bob")
    assert len(got) == 1
    assert got[0]["content"] == "replicate me"


def test_via_git_message_not_republished(hosts):
    # Deliver alice->bob to host B, then tick B again: bob's materialized (_via
    # git) file must not be re-published back onto git.
    _local_dm(hosts.dir_a, "alice", "bob", "once")
    hosts.a.tick()
    hosts.b.tick()
    assert len(_inbox_files(hosts.dir_b, "bob")) == 1

    hosts.b.tick()  # outbound scan sees the _via:git file
    hosts.a.tick()  # inbound on A — would see an echo if B republished
    assert not (hosts.repo_b / "lanes" / "bob.jsonl").exists()
    assert len(_inbox_files(hosts.dir_b, "bob")) == 1


def test_remote_roster_written(hosts):
    # After alice's lane reaches host B (where alice is not a local presence), B's
    # daemon records her in DISPATCH_DIR/.remote/ so who() can show her cross-host.
    _local_dm(hosts.dir_a, "alice", "bob", "hi")
    hosts.a.tick()
    hosts.b.tick()
    roster = hosts.dir_b / ".remote" / "alice.json"
    assert roster.exists()
    data = json.loads(roster.read_text())
    assert data["agent_id"] == "alice"
    assert data["via"] == "git"


def test_tick_guarded_swallows_errors(hosts):
    def boom() -> None:
        raise RuntimeError("git exploded")

    hosts.a._outbound = boom  # type: ignore[method-assign]
    assert hosts.a.tick_guarded() is False  # logged + retried next pass, not raised


def test_broadcast_not_bridged(hosts):
    # A broadcast file (to == "all") is host-local semantics; not bridged in v1.
    msg = _make_msg("alice", "all", "all hands")
    (hosts.dir_a / "bob").mkdir(parents=True)
    dispatch_fs.atomic_write(hosts.dir_a / "bob" / dispatch_fs.message_filename("alice"), msg)
    hosts.a.tick()
    hosts.b.tick()
    assert _inbox_files(hosts.dir_b, "bob") == []
    assert not (hosts.repo_a / "lanes" / "alice.jsonl").exists()
