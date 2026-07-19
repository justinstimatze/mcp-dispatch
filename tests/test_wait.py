"""bin/dispatch-wait lifetime behaviour: presence-gated exit + standalone cap.

The default (no time cap) ties the waiter's life to its agent's presence flock,
so it exits when the session ends instead of orphaning. Exercised as a subprocess
with the scratch dir redirected via MCP_DISPATCH_STATE_DIR and the standalone
fallback shrunk via MCP_DISPATCH_STANDALONE_FALLBACK so nothing waits 30 minutes.
"""

from __future__ import annotations

import fcntl
import json
import select
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WAIT = REPO_ROOT / "bin" / "dispatch-wait"


def _env(dispatch_dir, state_dir, agent="alice", **extra):
    return {
        "MCP_DISPATCH_AGENT_ID": agent,
        "MCP_DISPATCH_DIR": str(dispatch_dir),
        "MCP_DISPATCH_STATE_DIR": str(state_dir),
        "MCP_DISPATCH_CONFIG": str(dispatch_dir.parent / "no-such-config.toml"),
        "PATH": "/usr/bin:/bin",
        **extra,
    }


def _launch(dispatch_dir, state_dir, *args, **extra):
    return subprocess.Popen(
        [sys.executable, str(WAIT), "--interval", "0.2", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_env(dispatch_dir, state_dir, **extra),
    )


def _dirs(tmp_path, agent="alice"):
    dispatch_dir = tmp_path / "messages"
    (dispatch_dir / agent).mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return dispatch_dir, state_dir


def _hold_presence(dispatch_dir, agent="alice", channels=()):
    pres = dispatch_dir / ".presence"
    pres.mkdir(parents=True, exist_ok=True)
    pf = pres / f"{agent}.json"
    pf.write_text(json.dumps({"agent_id": agent, "channels": list(channels)}))
    fh = open(pf, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # simulate the live server
    return fh


def test_presence_drop_exits(tmp_path):
    """A gated waiter blocks while presence is held, then exits when it drops."""
    dispatch_dir, state_dir = _dirs(tmp_path)
    holder = _hold_presence(dispatch_dir)
    proc = _launch(dispatch_dir, state_dir)
    try:
        time.sleep(0.6)
        assert proc.poll() is None  # live presence → still blocking
        holder.close()  # session "ends" → presence flock released
        out, _ = proc.communicate(timeout=5)
        assert proc.returncode == 0
        assert "presence dropped" in out
    finally:
        if proc.poll() is None:
            proc.kill()
        if not holder.closed:
            holder.close()


def test_standalone_falls_back_to_cap(tmp_path):
    """No presence to gate on → default 0 must not block forever; the fallback
    cap makes it exit cleanly."""
    dispatch_dir, state_dir = _dirs(tmp_path)
    proc = _launch(dispatch_dir, state_dir, MCP_DISPATCH_STANDALONE_FALLBACK="0.5")
    out, _ = proc.communicate(timeout=5)
    assert proc.returncode == 0
    assert "within" in out  # hit the fallback cap rather than blocking forever


def test_explicit_cap_honoured_when_gated(tmp_path):
    """An explicit --max-lifetime still applies on top of presence gating."""
    dispatch_dir, state_dir = _dirs(tmp_path)
    holder = _hold_presence(dispatch_dir)
    proc = _launch(dispatch_dir, state_dir, "--max-lifetime", "0.5")
    try:
        out, _ = proc.communicate(timeout=5)
        assert proc.returncode == 0
        assert "within" in out  # timed out despite live presence
    finally:
        holder.close()


# ── --follow stream mode (the Monitor-driven watch) ──────────────────────────


def _write_msg(dispatch_dir, agent, *, to, content, mid, **extra):
    inbox = dispatch_dir / agent
    inbox.mkdir(parents=True, exist_ok=True)
    msg = {
        "id": mid,
        "from": "bob",
        "to": to,
        "content": content,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "state": "pending",
        **extra,
    }
    # Millisecond-prefixed filename → chronological glob order, matching the server.
    (inbox / f"{int(time.time() * 1000)}-{mid}.json").write_text(json.dumps(msg))


def _readline(proc, timeout):
    """Read one line from proc.stdout, or '' if nothing lands within timeout."""
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    return proc.stdout.readline() if ready else ""


def test_follow_streams_each_message_without_rearming(tmp_path):
    """--follow emits one line per new qualifying message and keeps running — the
    property that lets a single Monitor registration cover the whole session."""
    dispatch_dir, state_dir = _dirs(tmp_path)
    holder = _hold_presence(dispatch_dir)
    proc = _launch(
        dispatch_dir, state_dir, "--follow", agent="alice", MCP_DISPATCH_NOTIFY_ON="direct"
    )
    try:
        _write_msg(dispatch_dir, "alice", to="alice", content="first ping", mid="m1")
        l1 = _readline(proc, 4)
        assert "m1" in l1 and "first ping" in l1
        assert proc.poll() is None  # still running — did NOT exit on the hit

        _write_msg(dispatch_dir, "alice", to="alice", content="second ping", mid="m2")
        l2 = _readline(proc, 4)
        assert "m2" in l2 and "second ping" in l2

        holder.close()  # session ends → presence drops → watch exits
        rest, _ = proc.communicate(timeout=5)
        assert proc.returncode == 0
        assert "watch exiting" in rest
        assert "m1" not in rest  # dedup: an already-emitted id is not repeated
    finally:
        if proc.poll() is None:
            proc.kill()
        if not holder.closed:
            holder.close()


def test_follow_wakes_on_a_subscribed_channel_post(tmp_path):
    """The regression this whole path exists for: a channel post is fanned out to
    the subscriber's inbox as a normal file, but its `to` is '#eng', not the agent
    id — so under notify_on="direct" the waiter used to silently drop it. The
    sender saw it queued and stopped chasing; the message was never seen. The
    subscription in the presence record is what makes it count as addressed."""
    dispatch_dir, state_dir = _dirs(tmp_path)
    holder = _hold_presence(dispatch_dir, channels=["eng"])
    proc = _launch(
        dispatch_dir, state_dir, "--follow", agent="alice", MCP_DISPATCH_NOTIFY_ON="direct"
    )
    try:
        _write_msg(dispatch_dir, "alice", to="#eng", content="room ping", mid="c1")
        line = _readline(proc, 4)
        assert "c1" in line and "room ping" in line
    finally:
        proc.kill()
        holder.close()


def test_follow_ignores_a_channel_i_did_not_join(tmp_path):
    """Symmetric guard: a stray file for a room this agent never joined must not
    wake it, or 'direct' degenerates into 'all'."""
    dispatch_dir, state_dir = _dirs(tmp_path)
    holder = _hold_presence(dispatch_dir, channels=["ops"])
    proc = _launch(
        dispatch_dir, state_dir, "--follow", agent="alice", MCP_DISPATCH_NOTIFY_ON="direct"
    )
    try:
        _write_msg(dispatch_dir, "alice", to="#eng", content="not my room", mid="c2")
        assert _readline(proc, 1.5) == ""  # stayed silent
        # ...and the watch is still healthy, not just dead.
        _write_msg(dispatch_dir, "alice", to="alice", content="dm though", mid="c3")
        assert "c3" in _readline(proc, 4)
    finally:
        proc.kill()
        holder.close()


def test_follow_marks_remote_provenance(tmp_path):
    """A git-materialized message (_via='git') is flagged «remote» in the stream."""
    dispatch_dir, state_dir = _dirs(tmp_path)
    holder = _hold_presence(dispatch_dir)
    proc = _launch(
        dispatch_dir, state_dir, "--follow", agent="alice", MCP_DISPATCH_NOTIFY_ON="direct"
    )
    try:
        _write_msg(dispatch_dir, "alice", to="alice", content="cross-host hi", mid="r1", _via="git")
        line = _readline(proc, 4)
        assert "r1" in line and "«remote»" in line
    finally:
        proc.kill()
        holder.close()
