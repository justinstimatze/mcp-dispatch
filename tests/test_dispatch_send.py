"""bin/dispatch-send — a headless CLI for posting one message, for callers that
aren't MCP clients and shouldn't hand-roll the on-disk filesystem contract.

Runs the real script as a subprocess against a synthetic relay, exercising the
exact code path (dispatch_fs.send_message) that server.py's dispatch() MCP
tool also calls — so these tests are really asserting the CLI wires that
shared function up correctly, not re-testing send_message's own routing
(covered by the server.py-facing tests already).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEND = REPO_ROOT / "bin" / "dispatch-send"


def _run(relay: Path, *args: str, config: Path | None = None) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env.update(
        MCP_DISPATCH_DIR=str(relay),
        MCP_DISPATCH_CONFIG=str(config or relay.parent / "no-such-config.toml"),
    )
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, str(SEND), *args], env=env, capture_output=True, text=True, timeout=30
    )


def _only_message(inbox: Path) -> dict:
    files = list(inbox.glob("*.json"))
    assert len(files) == 1, f"expected exactly one message in {inbox}, found {files}"
    return json.loads(files[0].read_text())


def test_posts_a_message_into_the_targets_inbox(tmp_path):
    relay = tmp_path / "relay"
    relay.mkdir()
    out = _run(relay, "--from", "watcher-1", "--to", "alice", "hello")
    assert out.returncode == 0, out.stderr

    result = json.loads(out.stdout)
    assert result["sent"] is True
    assert result["queued_to"] == ["alice"]

    msg = _only_message(relay / "alice")
    assert msg["from"] == "watcher-1"
    assert msg["content"] == "hello"
    assert msg["state"] == "pending"


def test_defaults_to_broadcast_when_no_target_given(tmp_path):
    relay = tmp_path / "relay"
    (relay / "bob").mkdir(parents=True)  # a known dynamic-mode inbox to broadcast to
    out = _run(relay, "--from", "watcher-1", "hi everyone")
    assert out.returncode == 0, out.stderr
    # No live sessions in this synthetic relay, so the broadcast pool is empty —
    # the point here is only that omitting --to doesn't error, it defaults to "all".
    assert json.loads(out.stdout)["to"] == "all"


def test_payload_round_trips_as_json(tmp_path):
    relay = tmp_path / "relay"
    relay.mkdir()
    out = _run(
        relay,
        "--from",
        "watcher-1",
        "--to",
        "alice",
        "--payload",
        '{"issue": "ENG-9"}',
        "event landed",
    )
    assert out.returncode == 0, out.stderr
    msg = _only_message(relay / "alice")
    assert msg["payload"] == {"issue": "ENG-9"}


def test_thread_id_reply_to_priority_and_must_read_all_land(tmp_path):
    relay = tmp_path / "relay"
    relay.mkdir()
    out = _run(
        relay,
        "--from",
        "watcher-1",
        "--to",
        "alice",
        "--priority",
        "urgent",
        "--thread-id",
        "t-1",
        "--reply-to",
        "msg-abc",
        "--must-read",
        "urgent thing",
    )
    assert out.returncode == 0, out.stderr
    msg = _only_message(relay / "alice")
    assert msg["priority"] == "urgent"
    assert msg["thread_id"] == "t-1"
    assert msg["reply_to"] == "msg-abc"
    assert msg["must_read"] is True


def test_invalid_payload_json_is_rejected_before_writing_anything(tmp_path):
    relay = tmp_path / "relay"
    relay.mkdir()
    out = _run(relay, "--from", "watcher-1", "--to", "alice", "--payload", "not json", "x")
    assert out.returncode != 0
    assert "not valid JSON" in out.stderr
    assert not list((relay / "alice").glob("*.json"))


def test_invalid_from_id_is_rejected(tmp_path):
    relay = tmp_path / "relay"
    relay.mkdir()
    out = _run(relay, "--from", "BAD ID", "--to", "alice", "x")
    assert out.returncode != 0
    assert "BAD ID" in out.stderr


def test_oversized_message_is_rejected(tmp_path):
    relay = tmp_path / "relay"
    cfg = tmp_path / "small.toml"
    cfg.write_text("max_message_bytes = 100\n")
    relay.mkdir()
    out = _run(relay, "--from", "watcher-1", "--to", "alice", "x" * 500, config=cfg)
    assert out.returncode != 0
    assert "too large" in out.stderr
    assert not list((relay / "alice").glob("*.json"))


def test_missing_relay_is_reported_not_silently_created(tmp_path):
    relay = tmp_path / "never-started"
    out = _run(relay, "--from", "watcher-1", "--to", "alice", "x")
    assert out.returncode != 0
    assert "no relay" in out.stderr
    assert not relay.exists()


def test_nick_with_no_live_session_gets_its_own_inbox(tmp_path):
    """The exact case pennon's watcher needs: post to an offline nick and have
    it wait there, inherited by that nick's next session — not error out."""
    relay = tmp_path / "relay"
    relay.mkdir()
    out = _run(relay, "--from", "watcher-1", "--to", "publicai", "queued for later")
    assert out.returncode == 0, out.stderr
    msg = _only_message(relay / "publicai")
    assert msg["content"] == "queued for later"
