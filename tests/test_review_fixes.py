"""Regression tests for the adversarial-review fixes.

Covers: the \\Z id anchor, ttl=0 / negative-ttl semantics, untrusted presence
agent_id in channel fan-out, and group_mode's refusal to run against a relay
that isn't actually shared. (Liveness moved to flock — see test_liveness.py.)
"""

from __future__ import annotations

import fcntl
import json
import os

import pytest


def _hold_presence(server, agent_id, channels):
    """Create a presence file AND hold its flock, simulating a live owner."""
    (server.DISPATCH_DIR / agent_id).mkdir(exist_ok=True)
    pf = server.DISPATCH_DIR / ".presence" / f"{agent_id}.json"
    pf.write_text(json.dumps({"agent_id": agent_id, "channels": list(channels)}))
    fh = open(pf, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh  # caller keeps it open to hold the lock


# --- id anchor: \Z, not $ (no trailing-newline bypass) ---------------------


def test_id_regex_rejects_trailing_newline(server):
    with pytest.raises(ValueError):
        server._send("alpha", "beta\n", "x")


# --- ttl semantics ---------------------------------------------------------


def test_ttl_zero_means_no_expiry(server):
    m = server._send("alpha", "beta", "x", ttl=0)
    assert m["ttl"] is None
    # An ancient timestamp with no ttl never expires.
    assert server._is_expired({**m, "timestamp": "2000-01-01T00:00:00Z"}) is False


def test_ttl_negative_rejected(server):
    with pytest.raises(ValueError):
        server._send("alpha", "beta", "x", ttl=-5)


def test_ttl_none_uses_default(server):
    m = server._send("alpha", "beta", "x")  # built-in default_ttl = 7200
    assert m["ttl"] == 7200


# --- channel fan-out ignores untrusted presence agent_id -------------------


def test_channel_skips_invalid_presence_agent_id(server):
    # Hold the lock so the record is *live* — the id validation, not liveness,
    # must be what excludes the traversal payload.
    fh = _hold_presence(server, "evil", ["x"])
    try:
        (server.DISPATCH_DIR / ".presence" / "evil.json").write_text(
            json.dumps({"agent_id": "../../tmp/pwn", "channels": ["x"]})
        )
        assert "../../tmp/pwn" not in server._channel_subscribers("x")
    finally:
        fh.close()


# --- group_mode refuses an unusable / non-shared relay ---------------------


def test_group_mode_rejects_unshared_relay(server_factory, tmp_path, monkeypatch):
    cfg = tmp_path / "g.toml"
    cfg.write_text("group_mode = true\n")
    srv = server_factory("alpha", config_path=cfg)
    # Strip the setgid/group bits so the relay no longer looks shared...
    os.chmod(srv.DISPATCH_DIR, 0o700)
    # ...then simulate not owning it (chmod denied). Must fail loud, not swallow.
    monkeypatch.setattr(srv.os, "chmod", lambda *_a, **_k: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(RuntimeError):
        srv._setup_dirs()
