"""Security / correctness tests for the four audited bugs.

Each test fails against the unhardened server and passes once the
corresponding fix lands:

  1. Path traversal in agent_id and target.
  2. World-readable files / dirs (missing umask + chmod).
  3. Presence "lock" is a printf, not a real lock (flock).
  4. Filename collision silently drops simultaneous same-sender messages.
"""

from __future__ import annotations

import fcntl
import stat

import pytest
from conftest import load_server

# --- Bug 1: path traversal -------------------------------------------------


@pytest.mark.parametrize(
    "bad_id", ["../evil", "../../tmp/pwn", "a/b", "foo bar", ".", "..", "a.b", "a@b"]
)
def test_malicious_agent_id_rejected(tmp_path, bad_id):
    # Either a validation error (bad chars) or a refusal to start is acceptable;
    # the security property is simply that the id is never accepted as-is.
    with pytest.raises((ValueError, RuntimeError)):
        load_server(tmp_path / "messages", agent_id=bad_id)


@pytest.mark.parametrize("bad_target", ["../evil", "../../tmp/pwn", "a/b", "foo bar"])
def test_malicious_target_rejected(server, bad_target):
    with pytest.raises(ValueError):
        server._send("alpha", bad_target, "payload")


def test_traversal_does_not_escape_dispatch_dir(server):
    before = set(server.DISPATCH_DIR.parent.iterdir())
    try:
        server._send("alpha", "../pwned", "escape")
    except ValueError:
        pass
    # Nothing new may appear outside the dispatch dir.
    assert set(server.DISPATCH_DIR.parent.iterdir()) == before


def test_valid_agent_ids_still_accepted(tmp_path):
    for good in ["alpha", "gemot-12345", "schorl_portfolio", "a", "x" * 64]:
        srv = load_server(tmp_path / "messages", agent_id=good)
        assert srv.AGENT_ID == good


# --- Bug 2: file permissions ----------------------------------------------


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_dispatch_dir_not_world_accessible(server):
    assert _mode(server.DISPATCH_DIR) == 0o700
    assert _mode(server.DISPATCH_DIR / ".presence") == 0o700


def test_message_file_not_world_readable(server):
    server._send("alpha", "beta", "secret")
    files = list((server.DISPATCH_DIR / "beta").glob("*.json"))
    assert files
    for f in files:
        assert _mode(f) & 0o077 == 0, f"{f} is group/other accessible: {oct(_mode(f))}"


# --- Bug 3: presence flock -------------------------------------------------


def test_live_agent_id_cannot_be_taken_over(tmp_path):
    # First claim establishes the dirs.
    load_server(tmp_path / "messages", agent_id="alpha")
    presence = tmp_path / "messages" / ".presence" / "locked.json"
    presence.parent.mkdir(parents=True, exist_ok=True)

    # Hold an exclusive lock on the presence file via an independent fd,
    # simulating another live process owning the identity.
    holder = open(presence, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(RuntimeError):
            load_server(tmp_path / "messages", agent_id="locked")
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


# --- Bug 4: filename collision --------------------------------------------


def test_simultaneous_same_sender_messages_both_survive(server, monkeypatch):
    # Freeze the millisecond clock so both sends share a timestamp prefix.
    monkeypatch.setattr(server.time, "time", lambda: 1_700_000_000.0)
    server._send("alpha", "beta", "first")
    server._send("alpha", "beta", "second")
    contents = {m["content"] for m in server._read_inbox("beta")}
    assert contents == {"first", "second"}
