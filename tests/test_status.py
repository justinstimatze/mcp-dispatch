"""bin/dispatch-status — the operator's view of the relay.

Runs the real script as a subprocess against a synthetic relay. The presence
flocks are held by *this* process, so the child sees them held the same way it
would see a live session's.

The thing worth pinning is the distinction the script exists to draw now: a
session can hold its presence lock, answer to its name, and still have no
message watch armed. Every other readout calls that healthy.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import dispatch_common as common  # noqa: E402

STATUS = REPO_ROOT / "bin" / "dispatch-status"


def _session(relay: Path, agent_id: str, state: Path, *, unread: int = 0):
    """A live session: a locked presence file plus `unread` pending messages."""
    (relay / ".presence").mkdir(parents=True, exist_ok=True)
    pf = relay / ".presence" / f"{agent_id}.json"
    pf.write_text(json.dumps({"agent_id": agent_id, "pid": os.getpid(), "state_dir": str(state)}))
    inbox = relay / agent_id
    inbox.mkdir(exist_ok=True)
    for i in range(unread):
        (inbox / f"msg-{i}.json").write_text(json.dumps({"id": f"msg-{i}", "state": "pending"}))
    fh = open(pf, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def _run(relay: Path, state: Path) -> str:
    env = dict(os.environ)
    env.update(
        MCP_DISPATCH_DIR=str(relay),
        MCP_DISPATCH_STATE_DIR=str(state),
        MCP_DISPATCH_CONFIG=str(relay.parent / "no-such-config.toml"),
    )
    out = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, str(STATUS)], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_a_live_session_with_no_watch_is_called_out(tmp_path):
    relay, state = tmp_path / "relay", tmp_path / "state"
    state.mkdir()
    listening = common.arm_lock("hears-1", state)
    held = common.acquire_flock(listening)
    deaf = _session(relay, "deaf-1", state, unread=3)
    ok = _session(relay, "hears-1", state)
    try:
        out = _run(relay, state)
    finally:
        for fh in (deaf, ok, held):
            if fh is not None:
                fh.close()

    assert "deaf-1" in out and "NOT LISTENING" in out
    assert "live but not listening" in out
    # The armed one is listed as live but carries no warning of its own.
    hears_line = [ln for ln in out.splitlines() if "hears-1" in ln][0]
    assert "NOT LISTENING" not in hears_line
    assert "unread:3" in out


def test_an_all_armed_relay_prints_no_warning(tmp_path):
    relay, state = tmp_path / "relay", tmp_path / "state"
    state.mkdir()
    held = common.acquire_flock(common.arm_lock("hears-1", state))
    fh = _session(relay, "hears-1", state)
    try:
        out = _run(relay, state)
    finally:
        for h in (fh, held):
            if h is not None:
                h.close()

    assert "hears-1" in out
    assert "NOT LISTENING" not in out
    assert "live but not listening" not in out


def test_a_session_older_than_the_field_still_reports(tmp_path):
    """Sessions started before state_dir existed keep running for days. Their
    presence records have no such key and must not take the readout down.

    (Whether such a session is *judged* deaf is decided by uid in
    dispatch_common.armed_for, which is unit-tested — a subprocess cannot forge
    a foreign owner without root.)"""
    relay, state = tmp_path / "relay", tmp_path / "state"
    state.mkdir()
    fh = _session(relay, "legacy-1", state, unread=2)
    pf = relay / ".presence" / "legacy-1.json"
    pf.write_text(json.dumps({"agent_id": "legacy-1", "pid": os.getpid()}))  # pre-state_dir
    try:
        out = _run(relay, state)  # asserts rc == 0
    finally:
        fh.close()

    assert "legacy-1" in out and "unread:2" in out


# ---------------------------------------------------------------------------
# The unread count has to mean what the supervisor and digest mean by it.
#
# This readout used to count raw `state == "pending"` and ignore TTL, so a
# five-minute routing probe was still being reported as unread a day and a half
# after it expired — while every other consumer had written it off.
# ---------------------------------------------------------------------------


def _msg(inbox: Path, name: str, **fields):
    inbox.mkdir(parents=True, exist_ok=True)
    base = {"id": name, "state": "pending", "timestamp": "2020-01-01T00:00:00Z", "ttl": None}
    (inbox / f"{name}.json").write_text(json.dumps({**base, **fields}))


def test_expired_mail_is_not_counted_as_unread(tmp_path):
    relay, state = tmp_path / "relay", tmp_path / "state"
    state.mkdir()
    inbox = relay / "probe-1"
    _msg(inbox, "stale", ttl=300)  # a 2020 message with a 5-minute TTL
    _msg(inbox, "fresh")  # no TTL → waits forever
    fh = _session(relay, "probe-1", state)
    try:
        out = _run(relay, state)
    finally:
        fh.close()

    line = [ln for ln in out.splitlines() if "probe-1" in ln][0]
    assert "unread:1" in line, f"only the unexpired message counts: {line}"


def test_an_expired_message_never_makes_an_inbox_look_orphaned(tmp_path):
    """Orphan reporting is driven by the same count, so an expired-only inbox
    must not appear as messages stranded by a departed owner."""
    relay, state = tmp_path / "relay", tmp_path / "state"
    state.mkdir()
    (relay / ".presence").mkdir(parents=True)
    _msg(relay / "ghost-9", "stale", ttl=60)
    assert "ghost-9" not in _run(relay, state)


def test_must_read_outlives_its_ttl(tmp_path):
    """must_read is the escape hatch from expiry; the readout has to honour it
    or the one class of message that must not be dropped goes uncounted."""
    relay, state = tmp_path / "relay", tmp_path / "state"
    state.mkdir()
    _msg(relay / "probe-1", "important", ttl=60, must_read=True)
    fh = _session(relay, "probe-1", state)
    try:
        out = _run(relay, state)
    finally:
        fh.close()
    assert "unread:1" in [ln for ln in out.splitlines() if "probe-1" in ln][0]
