"""Regression tests for the adversarial-review fixes.

Covers: the cross-account _pid_alive EPERM bug, the \\Z id anchor, ttl=0 /
negative-ttl semantics, untrusted presence agent_id in channel fan-out, and
group_mode's refusal to run against a relay that isn't actually shared.
"""

from __future__ import annotations

import json
import os

import pytest

# --- _pid_alive cross-account (EPERM means alive) --------------------------


def test_pid_alive_eperm_is_alive(server, monkeypatch):
    def fake_kill(_pid, _sig):
        raise PermissionError()  # EPERM: exists but owned by another user

    monkeypatch.setattr(server.os, "kill", fake_kill)
    assert server._pid_alive(424242) is True


def test_pid_alive_esrch_is_dead(server, monkeypatch):
    def fake_kill(_pid, _sig):
        raise ProcessLookupError()  # ESRCH: truly gone

    monkeypatch.setattr(server.os, "kill", fake_kill)
    assert server._pid_alive(424242) is False


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
    pf = server.DISPATCH_DIR / ".presence" / "evil.json"
    pf.write_text(json.dumps({"agent_id": "../../tmp/pwn", "pid": os.getpid(), "channels": ["x"]}))
    assert "../../tmp/pwn" not in server._channel_subscribers("x")


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
