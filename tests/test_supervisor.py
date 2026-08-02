"""The lifecycle supervisor: start an agent when mail is waiting and it is offline.

Three things get the most coverage here, because they are the three ways this
feature could be actively harmful rather than merely broken:

  1. The allowlist really is the only source of what runs (nothing from a message
     reaches argv, cwd or env).
  2. The rate limits really do bound spawns under a message flood.
  3. The trigger agrees with ``server._inherit_orphan_inbox`` — waking a nick
     whose mail nobody would inherit is wasted work, and *not* waking one whose
     mail would be inherited is the bug the whole feature exists to fix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import supervisor  # noqa: E402

SUPERVISE = REPO_ROOT / "bin" / "dispatch-supervise"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _plant(dispatch_dir: Path, agent: str, *, mid: str, state="pending", content="hi", **extra):
    """Write a message straight into an inbox dir, as a sender would have."""
    inbox = dispatch_dir / agent
    inbox.mkdir(parents=True, exist_ok=True)
    msg = {
        "id": mid,
        "from": "bob",
        "to": agent,
        "content": content,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "state": state,
        **extra,
    }
    (inbox / f"{int(time.time() * 1000)}-bob-{mid}.json").write_text(json.dumps(msg))


def _hold_presence(dispatch_dir: Path, agent_id: str) -> subprocess.Popen:
    """A live session: a process holding the exclusive flock on a presence file.

    The flock, not the pid field, is what every liveness check in this repo reads,
    so a fake that only writes the JSON would be a fake the code sees through.
    """
    presence = dispatch_dir / ".presence"
    presence.mkdir(parents=True, exist_ok=True)
    pf = presence / f"{agent_id}.json"
    pf.write_text(json.dumps({"agent_id": agent_id, "pid": 0, "channels": []}))
    code = (
        "import fcntl,sys,time\n"
        f"fh=open({str(pf)!r},'a+')\n"
        "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
        "sys.stdout.write('locked\\n'); sys.stdout.flush()\n"
        "time.sleep(300)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    proc.stdout.readline()  # wait until the lock is actually held
    return proc


def _spec(nick="proj", command=None, cwd="", env=()):
    return supervisor.AgentSpec(
        nick=nick,
        command=tuple(command or ["/bin/true"]),
        cwd=cwd,
        env=tuple(env),
    )


def _cfg(**kw):
    c = supervisor.SupervisorConfig(enabled=True)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ---------------------------------------------------------------------------
# config: the allowlist has to fail loudly, not quietly
# ---------------------------------------------------------------------------


def test_absent_section_is_disabled():
    cfg = supervisor.load({})
    assert cfg.enabled is False
    assert cfg.agents == {}


def test_the_default_log_dir_is_expanded():
    """A `~` that survives into a Path becomes a directory literally named '~'
    in whatever the daemon's cwd happens to be."""
    assert "~" not in str(supervisor.SupervisorConfig().log_dir)
    assert "~" not in str(supervisor.load({"supervisor": {"enabled": True}}).log_dir)


def test_enabled_without_agents_is_not_a_wildcard():
    """The dangerous misreading of `enabled = true` is "supervise everything"."""
    cfg = supervisor.load({"supervisor": {"enabled": True}})
    assert cfg.enabled is True
    assert cfg.agents == {}


def test_command_as_a_string_is_rejected_by_name():
    """A shell string is the shape that invites injection. Say so, don't split it."""
    with pytest.raises(supervisor.ConfigError) as e:
        supervisor.load({"supervisor": {"agents": {"proj": {"command": "/bin/sh -c 'echo hi'"}}}})
    assert "LIST" in str(e.value)
    assert "never runs a shell" in str(e.value)


def test_relative_command_is_rejected():
    with pytest.raises(supervisor.ConfigError) as e:
        supervisor.load({"supervisor": {"agents": {"proj": {"command": ["claude"]}}}})
    assert "ABSOLUTE" in str(e.value)


def test_a_daemon_key_indented_into_an_agent_block_is_an_error():
    """Found by writing the first real config: `log_dir` under the agent table
    parsed fine and did nothing. A silently ignored key in an allowlist means
    believing a constraint is in force that isn't."""
    with pytest.raises(supervisor.ConfigError) as e:
        supervisor.load(
            {"supervisor": {"agents": {"proj": {"command": ["/bin/true"], "log_dir": "/tmp/x"}}}}
        )
    assert "unknown key(s): log_dir" in str(e.value)
    assert "belong under [supervisor]" in str(e.value)


def test_nick_must_be_a_safe_path_segment():
    with pytest.raises(supervisor.ConfigError):
        supervisor.load({"supervisor": {"agents": {"../etc": {"command": ["/bin/true"]}}}})


def test_a_valid_entry_round_trips():
    cfg = supervisor.load(
        {
            "supervisor": {
                "enabled": True,
                "cooldown": 5,
                "agents": {
                    "proj": {
                        "command": ["/bin/echo", "hello"],
                        "cwd": "/tmp",  # nosec B108 - a path in a test fixture
                        "env": {"FOO": "bar"},
                    }
                },
            }
        }
    )
    spec = cfg.agents["proj"]
    assert spec.command == ("/bin/echo", "hello")
    assert spec.env_map() == {"FOO": "bar"}
    assert cfg.cooldown == 5


def test_nonsense_numbers_are_rejected():
    with pytest.raises(supervisor.ConfigError):
        supervisor.load({"supervisor": {"cooldown": -1, "agents": {}}})


def test_world_writable_config_is_called_out(tmp_path):
    """The config now decides what executes, so who can write it is a real question."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[supervisor]\n")
    cfg.chmod(0o666)
    assert "execution allowlist" in supervisor.config_permission_warning(cfg)
    cfg.chmod(0o600)
    assert supervisor.config_permission_warning(cfg) == ""


# ---------------------------------------------------------------------------
# what counts as waiting mail
# ---------------------------------------------------------------------------


def test_mail_in_the_nick_inbox_counts(tmp_path):
    _plant(tmp_path, "proj", mid="m1")
    assert supervisor.waiting_mail(tmp_path, "proj") == 1


def test_mail_in_a_dead_session_inbox_counts(tmp_path):
    """The nick is offline and its last session died holding unread mail. That is
    exactly the case a human would call 'publicai never answered me'."""
    _plant(tmp_path, "proj-111", mid="m1")
    assert supervisor.waiting_mail(tmp_path, "proj") == 1


def test_a_live_sessions_inbox_does_not_count(tmp_path):
    holder = _hold_presence(tmp_path, "proj-111")
    try:
        _plant(tmp_path, "proj-111", mid="m1")
        assert supervisor.is_live(tmp_path, "proj") is True
        assert supervisor.waiting_mail(tmp_path, "proj") == 0
    finally:
        holder.kill()


def test_read_and_expired_mail_do_not_count(tmp_path):
    _plant(tmp_path, "proj", mid="seen", state="read")
    _plant(tmp_path, "proj", mid="stale", ttl=1, timestamp="2020-01-01T00:00:00Z")
    assert supervisor.waiting_mail(tmp_path, "proj") == 0


def test_must_read_pierces_ttl(tmp_path):
    _plant(
        tmp_path,
        "proj",
        mid="urgent",
        ttl=1,
        timestamp="2020-01-01T00:00:00Z",
        must_read=True,
    )
    assert supervisor.waiting_mail(tmp_path, "proj") == 1


def test_another_nicks_mail_is_not_mine(tmp_path):
    _plant(tmp_path, "other", mid="m1")
    _plant(tmp_path, "other-999", mid="m2")
    assert supervisor.waiting_mail(tmp_path, "proj") == 0


def test_trigger_agrees_with_the_servers_inheritance(server_factory):
    """The load-bearing invariant: the supervisor wakes a nick exactly when the
    nick's next session would inherit something.

    Asserted differentially against the real ``_inherit_orphan_inbox`` rather than
    by re-reading its rules, because a copy of a rule is a copy that can drift.
    """
    dd = server_factory.dispatch_dir
    dd.mkdir(parents=True, exist_ok=True)
    _plant(dd, "proj-111", mid="m1")  # dead session's unread mail
    _plant(dd, "proj", mid="m2")  # the nick's own drop box
    _plant(dd, "proj-222", mid="m3", state="read")  # read: not inherited
    _plant(dd, "unrelated-1", mid="m4")  # another project entirely

    predicted = supervisor.waiting_mail(dd, "proj")
    assert predicted == 2

    # A real successor session, inheriting the way it does in production — at
    # import, inside _claim_id, not via a hand-called helper.
    server_factory("proj-333")
    inherited = list((dd / "proj-333").glob("*.json"))
    assert len(inherited) == predicted
    assert {json.loads(f.read_text())["id"] for f in inherited} == {"m1", "m2"}
    # ...and having inherited, there is nothing left to wake for.
    assert supervisor.waiting_mail(dd, "proj") == 0


# ---------------------------------------------------------------------------
# the decision, in isolation
# ---------------------------------------------------------------------------


def _decide(state, *, mail=1, live=False, now=1000.0, concurrent=0, **cfg_kw):
    return supervisor.decide(
        _spec(),
        state,
        mail=mail,
        live=live,
        now=now,
        cfg=_cfg(**cfg_kw),
        concurrent=concurrent,
    )


def test_starts_when_mail_waits_and_nobody_is_home():
    d = _decide(supervisor.NickState())
    assert d.action == "start"


def test_never_starts_a_live_nick():
    assert _decide(supervisor.NickState(), live=True).action == "skip"


def test_no_mail_no_start():
    assert _decide(supervisor.NickState(), mail=0).action == "skip"


def test_cooldown_blocks_a_rapid_restart():
    state = supervisor.NickState(starts=[990.0])
    d = _decide(state, now=1000.0, cooldown=60)
    assert d.action == "skip"
    assert "cooldown" in d.reason
    # ...and lets go once it elapses.
    assert _decide(state, now=1100.0, cooldown=60).action == "start"


def test_hourly_ceiling_bounds_a_message_flood():
    """Mail keeps arriving and the runtime keeps not staying up. The ceiling is
    what turns an unbounded spawn loop into a bounded one."""
    state = supervisor.NickState(starts=[500.0 + i for i in range(12)])
    d = _decide(state, now=1000.0, cooldown=1, max_starts_per_hour=12)
    assert d.action == "skip"
    assert "rate limit" in d.reason


def test_starts_outside_the_window_do_not_count():
    state = supervisor.NickState(starts=[1.0 + i for i in range(12)])
    state.prune(now=1000.0 + supervisor.RATE_WINDOW_SECONDS)
    assert state.starts == []


def test_concurrency_cap_is_respected():
    d = _decide(supervisor.NickState(), concurrent=2, max_concurrent_starts=2)
    assert d.action == "skip"
    assert "in flight" in d.reason


def test_a_start_in_flight_is_not_repeated():
    state = supervisor.NickState(awaiting_since=990.0)
    d = _decide(state, now=1000.0)
    assert d.action == "skip"
    assert "in flight" in d.reason


def test_a_parked_nick_stays_parked():
    state = supervisor.NickState(parked="5 consecutive failed starts")
    assert _decide(state).action == "skip"


# ---------------------------------------------------------------------------
# spawning for real
# ---------------------------------------------------------------------------


def _runtime_script(tmp_path: Path, dispatch_dir: Path, agent_id: str) -> Path:
    """A stand-in agent runtime: claims presence the way server.py does, then sits."""
    script = tmp_path / "fake-runtime.py"
    script.write_text(
        "import fcntl, json, os, sys, time\n"
        f"dd = {str(dispatch_dir)!r}\n"
        f"aid = {agent_id!r}\n"
        "p = os.path.join(dd, '.presence')\n"
        "os.makedirs(p, exist_ok=True)\n"
        "pf = os.path.join(p, aid + '.json')\n"
        "open(pf, 'w').write(json.dumps({'agent_id': aid, 'pid': os.getpid()}))\n"
        "fh = open(pf, 'a+')\n"
        "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
        "time.sleep(300)\n"
    )
    return script


def test_a_started_runtime_is_confirmed_by_its_presence(tmp_path):
    dd = tmp_path / "relay"
    dd.mkdir()
    _plant(dd, "proj", mid="m1")
    script = _runtime_script(tmp_path, dd, "proj-4242")

    cfg = _cfg(
        agents={"proj": _spec(command=[sys.executable, str(script)])},
        log_dir=tmp_path / "logs",
        start_timeout=30,
    )
    lines: list[str] = []
    sup = supervisor.Supervisor(dd, cfg, log=lines.append)

    assert [d.action for d in sup.tick()] == ["start"]
    proc = sup.states["proj"].proc
    assert proc is not None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and sup.states["proj"].awaiting_since:
            time.sleep(0.1)
            sup.tick()
        assert sup.states["proj"].awaiting_since == 0.0
        assert sup.states["proj"].failures == 0
        assert any("is live after" in line for line in lines)
        # It is live now, so a further pass must not start a second one.
        assert [d.action for d in sup.tick()] == ["skip"]
    finally:
        proc.kill()


def test_a_runtime_that_exits_without_presence_is_a_failure(tmp_path):
    dd = tmp_path / "relay"
    dd.mkdir()
    _plant(dd, "proj", mid="m1")

    cfg = _cfg(
        agents={"proj": _spec(command=["/bin/false"])},
        log_dir=tmp_path / "logs",
        cooldown=0.01,
        max_failures=2,
    )
    lines: list[str] = []
    sup = supervisor.Supervisor(dd, cfg, log=lines.append)

    for _ in range(20):
        sup.tick()
        if sup.states["proj"].parked:
            break
        time.sleep(0.05)

    assert sup.states["proj"].parked, "a repeatedly-failing start must trip the breaker"
    assert any("rc=1" in line for line in lines)
    assert any("PARKED" in line for line in lines)
    # Parked means parked: no further spawn, however much mail arrives.
    _plant(dd, "proj", mid="m2")
    assert [d.action for d in sup.tick()] == ["skip"]


def test_a_missing_binary_fails_without_a_traceback(tmp_path):
    dd = tmp_path / "relay"
    dd.mkdir()
    _plant(dd, "proj", mid="m1")
    cfg = _cfg(
        agents={"proj": _spec(command=["/nonexistent/agent-runtime"])},
        log_dir=tmp_path / "logs",
    )
    lines: list[str] = []
    sup = supervisor.Supervisor(dd, cfg, log=lines.append)
    sup.tick()
    assert sup.states["proj"].failures == 1
    assert any("does not exist" in line for line in lines)


def test_dry_run_starts_nothing(tmp_path):
    dd = tmp_path / "relay"
    dd.mkdir()
    _plant(dd, "proj", mid="m1")
    marker = tmp_path / "ran"
    cfg = _cfg(
        agents={"proj": _spec(command=["/usr/bin/touch", str(marker)])},
        log_dir=tmp_path / "logs",
    )
    sup = supervisor.Supervisor(dd, cfg, dry_run=True, log=lambda _: None)
    assert [d.action for d in sup.tick()] == ["start"]
    assert not marker.exists()
    assert sup.states["proj"].proc is None


def test_no_part_of_a_message_reaches_the_child(tmp_path):
    """The security property, tested rather than asserted in a docstring.

    A message carrying every shape of hostile content lands in the inbox; the
    child records its own argv and environment. Neither may contain any of it.
    """
    dd = tmp_path / "relay"
    dd.mkdir()
    payload = "; touch /tmp/pwned; $(id); `id`; --inject=1"  # nosec B108 - a string, not a path
    _plant(dd, "proj", mid="evil", content=payload, **{"from": payload})

    dump = tmp_path / "dump.json"
    script = tmp_path / "recorder.py"
    script.write_text(
        "import json, os, sys\n"
        f"open({str(dump)!r}, 'w').write(json.dumps("
        "{'argv': sys.argv, 'env': dict(os.environ), 'cwd': os.getcwd()}))\n"
    )
    workdir = tmp_path / "work"
    workdir.mkdir()

    cfg = _cfg(
        agents={
            "proj": _spec(
                command=[sys.executable, str(script), "--configured-flag"],
                cwd=str(workdir),
                env=(("CONFIGURED", "yes"),),
            )
        },
        log_dir=tmp_path / "logs",
    )
    sup = supervisor.Supervisor(dd, cfg, log=lambda _: None)
    sup.tick()
    proc = sup.states["proj"].proc
    assert proc is not None
    proc.wait(timeout=30)

    recorded = json.loads(dump.read_text())
    assert recorded["argv"] == [str(script), "--configured-flag"]
    assert recorded["cwd"] == os.path.realpath(workdir)
    assert recorded["env"]["CONFIGURED"] == "yes"
    assert recorded["env"]["MCP_DISPATCH_SUPERVISED_NICK"] == "proj"
    for key, value in recorded["env"].items():
        assert payload not in value, f"message content leaked into env {key}"
    assert not Path("/tmp/pwned").exists()  # nosec B108 - asserting absence


def test_child_survives_the_supervisor(tmp_path):
    """An agent must not die because its babysitter was restarted."""
    dd = tmp_path / "relay"
    dd.mkdir()
    _plant(dd, "proj", mid="m1")
    script = _runtime_script(tmp_path, dd, "proj-4242")
    cfg = _cfg(
        agents={"proj": _spec(command=[sys.executable, str(script)])},
        log_dir=tmp_path / "logs",
    )
    sup = supervisor.Supervisor(dd, cfg, log=lambda _: None)
    sup.tick()
    proc = sup.states["proj"].proc
    assert proc is not None
    try:
        # Its own session, so a signal to the supervisor's process group misses it.
        assert os.getsid(proc.pid) == proc.pid
    finally:
        proc.kill()


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def _run(args, config: Path, relay: Path):
    env = dict(os.environ)
    env["MCP_DISPATCH_CONFIG"] = str(config)
    env["MCP_DISPATCH_DIR"] = str(relay)
    return subprocess.run(
        [sys.executable, str(SUPERVISE), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_check_refuses_a_command_that_is_not_there(tmp_path):
    relay = tmp_path / "relay"
    relay.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "[supervisor]\nenabled = true\n\n"
        '[supervisor.agents.proj]\ncommand = ["/nonexistent/runtime"]\n'
    )
    config.chmod(0o600)
    r = _run(["check"], config, relay)
    assert r.returncode == 1
    assert "does not exist" in r.stdout


def test_check_passes_a_good_config(tmp_path):
    relay = tmp_path / "relay"
    (relay / ".agents").mkdir(parents=True)
    (relay / ".agents" / "proj.json").write_text(json.dumps({"nick": "proj"}))
    config = tmp_path / "config.toml"
    config.write_text(
        '[supervisor]\nenabled = true\n\n[supervisor.agents.proj]\ncommand = ["/bin/true"]\n'
    )
    config.chmod(0o600)
    r = _run(["check"], config, relay)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK." in r.stdout


def test_check_warns_about_a_nick_the_relay_has_never_seen(tmp_path):
    """Almost always a typo in the section header — a valid entry that can't fire."""
    relay = tmp_path / "relay"
    relay.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        '[supervisor]\nenabled = true\n\n[supervisor.agents.typoo]\ncommand = ["/bin/true"]\n'
    )
    config.chmod(0o600)
    r = _run(["check"], config, relay)
    assert r.returncode == 0
    assert "no nick 'typoo' in the registry" in r.stdout


def test_disabled_is_the_default_and_once_says_so(tmp_path):
    relay = tmp_path / "relay"
    relay.mkdir()
    config = tmp_path / "config.toml"
    config.write_text("[dispatch]\n")
    r = _run(["--once"], config, relay)
    assert r.returncode == 1
    assert "enabled is false" in r.stdout


def test_once_reports_its_decision_without_starting(tmp_path):
    relay = tmp_path / "relay"
    relay.mkdir()
    _plant(relay, "proj", mid="m1")
    marker = tmp_path / "ran"
    config = tmp_path / "config.toml"
    config.write_text(
        "[supervisor]\nenabled = true\n\n[supervisor.agents.proj]\n"
        f'command = ["/usr/bin/touch", "{marker}"]\n'
    )
    config.chmod(0o600)
    r = _run(["--dry-run"], config, relay)
    assert r.returncode == 0
    assert "START proj" in r.stdout
    assert not marker.exists()


def test_status_reports_waiting_mail(tmp_path):
    relay = tmp_path / "relay"
    relay.mkdir()
    _plant(relay, "proj", mid="m1")
    _plant(relay, "proj", mid="m2")
    config = tmp_path / "config.toml"
    config.write_text(
        '[supervisor]\nenabled = true\n\n[supervisor.agents.proj]\ncommand = ["/bin/true"]\n'
    )
    config.chmod(0o600)
    r = _run(["status"], config, relay)
    assert r.returncode == 0
    assert "offline, 2 waiting" in r.stdout


def test_service_install_refuses_a_broken_allowlist(tmp_path):
    """A unit around an allowlist that can't resolve would look healthy and do
    nothing — the failure would first surface in a log nobody reads."""
    relay = tmp_path / "relay"
    relay.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        '[supervisor]\nenabled = true\n\n[supervisor.agents.proj]\ncommand = ["/nope/runtime"]\n'
    )
    config.chmod(0o600)
    r = _run(["service", "install", "--dry-run"], config, relay)
    assert r.returncode == 1
    assert "dispatch-supervise check" in r.stdout


def test_service_show_renders_a_unit_that_spares_the_children(tmp_path):
    relay = tmp_path / "relay"
    relay.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        '[supervisor]\nenabled = true\n\n[supervisor.agents.proj]\ncommand = ["/bin/true"]\n'
    )
    config.chmod(0o600)
    r = _run(["service", "show"], config, relay)
    assert r.returncode == 0, r.stdout + r.stderr
    # Restarting the supervisor must not kill the agents it started.
    assert "KillMode=process" in r.stdout
    assert f"MCP_DISPATCH_CONFIG={config}" in r.stdout
    # The sandbox stays light on purpose: the child inherits it.
    assert "SystemCallFilter" not in r.stdout
    assert "RestrictAddressFamilies" not in r.stdout
