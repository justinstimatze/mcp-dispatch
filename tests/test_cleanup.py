"""Housekeeping fixes: empty-inbox GC, throttled .tmp sweep, no-unlink release."""

from __future__ import annotations


def test_reap_empty_inboxes_removes_empty_keeps_nonempty(server):
    empty = server.DISPATCH_DIR / "ghost"
    empty.mkdir(exist_ok=True)
    server._send("alpha", "beta", "hi")  # beta inbox now has a message
    server._reap_empty_inboxes()
    assert not empty.exists()  # empty dir reaped
    assert (server.DISPATCH_DIR / "beta").exists()  # non-empty kept


def test_reap_empty_inboxes_skips_roster_mode(server_factory, tmp_path):
    cfg = tmp_path / "roster.toml"
    cfg.write_text('agents = ["alice", "bob"]\n')
    srv = server_factory("alice", config_path=cfg)
    # Roster pre-creates inboxes; the GC must not touch them.
    assert srv._reap_empty_inboxes() == 0
    assert (srv.DISPATCH_DIR / "bob").exists()


def test_release_id_does_not_unlink_presence(server):
    pf = server.DISPATCH_DIR / ".presence" / f"{server.AGENT_ID}.json"
    assert pf.exists()
    server._release_id(server.AGENT_ID)
    # The file lingers (reclaimed by the next owner / reaped at startup); not unlinked.
    assert pf.exists()


def test_tmp_sweep_is_throttled(server, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_sweep_stale_tmp", lambda *a, **k: calls.append(1))
    server._last_tmp_sweep = 0.0
    server._cleanup_expired("alpha")  # elapsed > 60s since 0 → sweeps
    assert len(calls) == 1
    server._cleanup_expired("alpha")  # immediately again → throttled
    assert len(calls) == 1
