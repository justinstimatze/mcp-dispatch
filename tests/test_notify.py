"""Notifier tests — the opt-in human-facing alert for parked/idle sessions.

We test the pure decision (_should_notify) and that _notify shells out to the
configured command with a sensible summary/body. We never fire a real
notification: _notify is called directly with subprocess.run monkeypatched, and
the background poll thread only wakes after several seconds (long after these
fast tests finish), with nothing landing in the agent's own inbox meanwhile.
"""

from __future__ import annotations


def _cfg(tmp_path, body):
    cfg = tmp_path / "notify.toml"
    cfg.write_text(body)
    return cfg


def test_important_only_notifies_urgent_or_must_read(server_factory, tmp_path):
    srv = server_factory(
        "alpha",
        config_path=_cfg(tmp_path, 'notify_command = "notify-send"\nnotify_on = "important"\n'),
    )
    assert srv._should_notify({"priority": "urgent"})
    assert srv._should_notify({"must_read": True})
    assert not srv._should_notify({"priority": "normal"})
    assert not srv._should_notify({})


def test_all_notifies_everything(server_factory, tmp_path):
    srv = server_factory(
        "alpha", config_path=_cfg(tmp_path, 'notify_command = "notify-send"\nnotify_on = "all"\n')
    )
    assert srv._should_notify({"priority": "normal"})
    assert srv._should_notify({"priority": "urgent"})


def test_none_notifies_nothing(server_factory, tmp_path):
    srv = server_factory(
        "alpha", config_path=_cfg(tmp_path, 'notify_command = "notify-send"\nnotify_on = "none"\n')
    )
    assert not srv._should_notify({"priority": "urgent"})
    assert not srv._should_notify({"must_read": True})


def test_notify_shells_out_with_summary_and_body(server_factory, tmp_path, monkeypatch):
    srv = server_factory(
        "alpha", config_path=_cfg(tmp_path, 'notify_command = "notify-send -a dispatch"\n')
    )
    captured = []
    monkeypatch.setattr(srv.subprocess, "run", lambda argv, **kw: captured.append(argv))
    srv._notify({"from": "bob", "content": "interface change", "priority": "urgent"})
    assert captured, "subprocess.run was not called"
    argv = captured[0]
    assert argv[:3] == ["notify-send", "-a", "dispatch"]  # config args preserved
    joined = " ".join(argv)
    assert "bob" in joined and "interface change" in joined


def test_notifier_disabled_by_default(server):
    # Default config has no notify_command → notifier is a no-op.
    assert server.NOTIFY_COMMAND == ""
    server._start_notifier(server.AGENT_ID)  # must not raise or spawn anything
