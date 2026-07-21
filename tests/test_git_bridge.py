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


def _bus_and_clones(tmp_path: Path):
    """A seeded bare 'bus' repo + two working clones + two empty DISPATCH_DIRs."""
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
    return dir_a, dir_b, repo_a, repo_b


@pytest.fixture
def hosts(tmp_path: Path):
    """A bare repo + two host clones, each with its own DISPATCH_DIR + GitBridge."""
    dir_a, dir_b, repo_a, repo_b = _bus_and_clones(tmp_path)

    return SimpleNamespace(
        dir_a=dir_a,
        dir_b=dir_b,
        repo_a=repo_a,
        repo_b=repo_b,
        a=GitBridge(dir_a, repo_a, remote="origin"),
        b=GitBridge(dir_b, repo_b, remote="origin"),
        holds=[],  # keep presence flocks alive
    )


def test_first_run_does_not_dump_backlog(tmp_path):
    """Enabling the bridge on a relay that already has history must NOT push that
    backlog to git — only messages that arrive after enable bridge ('from now on')."""
    dir_a, dir_b, repo_a, repo_b = _bus_and_clones(tmp_path)

    # Backlog exists BEFORE the bridge is ever constructed (git just got enabled).
    _local_dm(dir_a, "alice", "bob", "ancient backlog")

    a = GitBridge(dir_a, repo_a, remote="origin")  # first run → seeds ledger, no push
    b = GitBridge(dir_b, repo_b, remote="origin")
    a.tick()
    b.tick()
    assert _inbox_files(dir_b, "bob") == []  # backlog stayed out of git

    # A message that lands AFTER enable bridges normally.
    _local_dm(dir_a, "alice", "bob", "fresh message")
    a.tick()
    b.tick()
    assert [m["content"] for m in _inbox_files(dir_b, "bob")] == ["fresh message"]


def test_empty_relay_first_run_latches_ledger(tmp_path):
    """A bridge first constructed on an EMPTY relay must still persist the ledger
    marker, so a SECOND bridge (a --once/cron pass, or a daemon restarted by a
    fresh SessionStart) isn't first_run again and misclassify a since-arrived
    message as backlog. Regression for a silent cross-host drop on a quiet-start
    relay: without the marker, bridge #2 re-seeds and never bridges the message."""
    dir_a, dir_b, repo_a, repo_b = _bus_and_clones(tmp_path)

    GitBridge(dir_a, repo_a, remote="origin")  # bridge #1: empty relay, no seed
    # A message arrives AFTER that first construction (not pre-existing backlog).
    _local_dm(dir_a, "alice", "bob", "arrived after start")

    a2 = GitBridge(dir_a, repo_a, remote="origin")  # bridge #2: must not re-seed
    b = GitBridge(dir_b, repo_b, remote="origin")
    a2.tick()
    b.tick()
    assert [m["content"] for m in _inbox_files(dir_b, "bob")] == ["arrived after start"]


def test_flush_pending_recovers_frozen_remote(tmp_path):
    """Steven's bug: pushes fail for a while (auth/network/identity), so the
    remote lane freezes while the local daemon keeps committing. On restart,
    flush_pending() must push the piled-up commits — WITHOUT any new traffic — so
    the remote catches up and the other host finally sees the missed messages."""
    dir_a, dir_b, repo_a, repo_b = _bus_and_clones(tmp_path)
    bare = str(tmp_path / "bus.git")

    a = GitBridge(dir_a, repo_a, remote="origin")
    _local_dm(dir_a, "alice", "bob", "before outage")
    a.tick()  # published + pushed → remote has it

    # Outage: point origin at nothing so the push fails; the daemon keeps going.
    _git(repo_a, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    _local_dm(dir_a, "alice", "bob", "during outage")
    a.tick_guarded()  # commits locally, push fails, swallowed — remote now frozen

    b = GitBridge(dir_b, repo_b, remote="origin")
    b.tick()
    assert [m["content"] for m in _inbox_files(dir_b, "bob")] == ["before outage"]  # frozen

    # Update + restart: push works again; a fresh daemon flushes on startup with
    # NO new message sent.
    _git(repo_a, "remote", "set-url", "origin", bare)
    a2 = GitBridge(dir_a, repo_a, remote="origin")
    assert a2.flush_pending() is True

    b.tick()
    assert [m["content"] for m in _inbox_files(dir_b, "bob")] == ["before outage", "during outage"]


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


# ── inbound fetch pacing ─────────────────────────────────────────────────────


def test_fetch_backs_off_on_a_silent_bus(tmp_path, monkeypatch):
    """`git fetch` is ~170ms of CPU against a real remote while the whole local
    scan is ~12ms, so a 24/7 daemon polling every 2s spends all its CPU asking a
    quiet remote whether anything happened. The cadence must decay while silent."""
    dd = tmp_path / "messages"
    dd.mkdir()
    repo = tmp_path / "bus"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    b = GitBridge(dd, repo, poll_interval=2.0, max_fetch_interval=30.0)

    fetches = []
    monkeypatch.setattr(b, "_inbound", lambda: (fetches.append(1), 0)[1])
    monkeypatch.setattr(b, "_outbound", lambda: 0)
    monkeypatch.setattr(b, "_write_remote_roster", lambda: None)

    b.tick()  # first tick always fetches (nothing known yet)
    assert len(fetches) == 1
    b.tick()  # ...then the backoff holds it off
    assert len(fetches) == 1
    assert b._fetch_every == 4.0  # 2 -> 4, doubling toward the ceiling


def test_backoff_ceiling_is_respected(tmp_path):
    dd = tmp_path / "messages"
    dd.mkdir()
    repo = tmp_path / "bus"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    b = GitBridge(dd, repo, poll_interval=2.0, max_fetch_interval=30.0)
    for _ in range(20):
        b._slow_down()
    assert b._fetch_every == 30.0


def test_traffic_snaps_the_cadence_back(tmp_path, monkeypatch):
    """A local send makes an inbound reply likely, so it must not have to wait out
    a 30s backoff — otherwise a conversation resuming after a lull stalls."""
    dd = tmp_path / "messages"
    dd.mkdir()
    repo = tmp_path / "bus"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    b = GitBridge(dd, repo, poll_interval=2.0, max_fetch_interval=30.0)
    monkeypatch.setattr(b, "_write_remote_roster", lambda: None)
    monkeypatch.setattr(b, "_inbound", lambda: 0)

    monkeypatch.setattr(b, "_outbound", lambda: 0)
    for _ in range(5):
        b.tick()
    assert b._fetch_every > 2.0  # decayed while silent

    monkeypatch.setattr(b, "_outbound", lambda: 1)  # someone sent something
    b.tick()
    assert b._fetch_every == 2.0
    assert b._next_fetch == 0.0  # and the next tick fetches immediately


def test_pacing_disabled_by_default(tmp_path, monkeypatch):
    """tick()'s contract is 'one outbound pass, one inbound pass'. --once and the
    rest of the suite depend on that, so pacing stays opt-in."""
    dd = tmp_path / "messages"
    dd.mkdir()
    repo = tmp_path / "bus"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    b = GitBridge(dd, repo)
    fetches = []
    monkeypatch.setattr(b, "_inbound", lambda: (fetches.append(1), 0)[1])
    monkeypatch.setattr(b, "_outbound", lambda: 0)
    monkeypatch.setattr(b, "_write_remote_roster", lambda: None)
    for _ in range(4):
        b.tick()
    assert len(fetches) == 4
