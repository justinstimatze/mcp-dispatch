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

import dispatch_common  # noqa: E402
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


@pytest.fixture
def no_desktop(monkeypatch):
    """Capture notifications instead of firing them.

    Without this a local `pytest` run pops real desktop toasts — the repo's own
    config sets notify_command, and the sweep calls it for real.
    """
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        supervisor.dispatch_common,
        "notify",
        lambda summary, body="", cfg=None: bool(sent.append((summary, body))) or True,
    )
    return sent


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


def test_inherit_inbox_false_narrows_the_trigger_to_the_nicks_own_inbox(tmp_path):
    """With `inherit_inbox = false` the server adopts nothing, so waking an agent
    for a dead session's mail would start it for mail it cannot see — and since
    the mail then stays put, the trigger never clears and the start repeats until
    the hourly ceiling pins it. The nick's own inbox still counts: a new session
    is handed that directly, not by inheritance.
    """
    _plant(tmp_path, "proj-111", mid="dead")
    _plant(tmp_path, "proj", mid="own")
    assert supervisor.waiting_mail(tmp_path, "proj", inherit=True) == 2
    assert supervisor.waiting_mail(tmp_path, "proj", inherit=False) == 1


def test_the_server_flag_is_read_with_the_servers_own_precedence():
    """server._load_config merges [dispatch] OVER the top level — the opposite of
    dispatch_common.flat. For a key whose job is predicting the server, matching
    the server wins."""
    assert supervisor.load({"inherit_inbox": False}).inherit_inbox is False
    assert supervisor.load({"dispatch": {"inherit_inbox": False}}).inherit_inbox is False
    # [dispatch] wins over a conflicting top-level key, as the server does.
    cfg = supervisor.load({"inherit_inbox": True, "dispatch": {"inherit_inbox": False}})
    assert cfg.inherit_inbox is False
    assert supervisor.load({}).inherit_inbox is True


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


def test_a_skip_that_never_looked_does_not_claim_zero(tmp_path):
    """`already live; 0 waiting` reads as a measurement. The pass short-circuits
    before scanning when the nick is live, so it is not one."""
    dd = tmp_path / "relay"
    dd.mkdir()
    holder = _hold_presence(dd, "proj-111")
    try:
        cfg = _cfg(agents={"proj": _spec()}, log_dir=tmp_path / "logs")
        sup = supervisor.Supervisor(dd, cfg, log=lambda _: None)
        (decision,) = sup.tick()
        assert decision.counted is False
        assert "mail not checked" in decision.line()
        assert "0 waiting" not in decision.line()
    finally:
        holder.kill()


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
    # An inherited agent id would pin every started agent to the id of whatever
    # session launched the supervisor, colliding with that live session.
    assert "MCP_DISPATCH_AGENT_ID" not in recorded["env"]
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


def test_check_warns_about_a_nick_no_session_has_ever_used(tmp_path):
    """Almost always a typo in the section header — but say so without claiming
    the entry is inert. It is not: the trigger is the inbox, and dispatching to
    an unknown name creates that inbox, so a correctly-spelled new nick fires the
    first time anyone writes to it. A warning that over-claims here sends an
    operator hunting for a bug in a working config."""
    relay = tmp_path / "relay"
    relay.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        '[supervisor]\nenabled = true\n\n[supervisor.agents.typoo]\ncommand = ["/bin/true"]\n'
    )
    config.chmod(0o600)
    r = _run(["check"], config, relay)
    assert r.returncode == 0
    assert "no session has ever used the nick 'typoo'" in r.stdout
    assert "check the spelling" in r.stdout
    assert "the trigger is the inbox, not the registry" in r.stdout


def test_a_never_seen_nick_still_has_a_trigger(tmp_path):
    """The claim the warning above now makes, tested rather than left in prose.

    A nick with no registry entry and no session history still counts its mail,
    because the trigger reads inboxes and a sender creates the inbox on demand.
    That is the pilot case, and the reason the old warning was wrong."""
    _plant(tmp_path, "typoo", mid="m1")
    assert not (tmp_path / ".agents").exists()
    assert supervisor.waiting_mail(tmp_path, "typoo") == 1


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


# ---------------------------------------------------------------------------
# the runtime the supervisor starts
#
# `dispatch-agent-claude` is the other half of the security story: the supervisor
# guarantees nothing from a message reaches argv, and this script is what that
# argv turns into. It is also where a quiet CLI-shaped mistake cost a failed
# start during the first pilot — see the separator test.
# ---------------------------------------------------------------------------

RUNTIME = REPO_ROOT / "bin" / "dispatch-agent-claude"


def _stub_claude(tmp_path: Path) -> tuple[Path, Path]:
    """A fake `claude` that records the argv it was handed, and exits.

    NUL-separated, not newline-separated: the prompt is a multi-line string, and
    a line-based recording would silently split it into several 'arguments' —
    a fake that lies about the shape of what it received.
    """
    argv_out = tmp_path / "argv.bin"
    stub = tmp_path / "claude-stub"
    stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\0" "$@" > {argv_out}\n')
    stub.chmod(0o755)
    return stub, argv_out


def _run_runtime(project: Path, tmp_path: Path, **env):
    stub, argv_out = _stub_claude(tmp_path)
    r = subprocess.run(
        [str(RUNTIME), str(project)],
        capture_output=True,
        text=True,
        env={**os.environ, "DISPATCH_AGENT_CLAUDE": str(stub), **env},
    )
    raw = argv_out.read_text() if argv_out.exists() else ""
    return r, [a for a in raw.split("\0") if a]


def test_the_prompt_is_separated_from_the_variadic_flags(tmp_path):
    """`--allowed-tools` and `--mcp-config` both take unlimited values, so a
    trailing prompt is swallowed as one more value unless `--` ends the list.

    This is not hypothetical: the first supervised start failed with
    ENAMETOOLONG because claude tried to open the entire prompt as a config file
    path. The failure was loud, but a start that dies before claiming presence
    only shows up as a counted failure — so pin the separator here."""
    project = tmp_path / "proj"
    project.mkdir()
    r, argv = _run_runtime(project, tmp_path)
    assert r.returncode == 0
    assert argv[-2] == "--", f"prompt not separated from variadic flags: {argv[-3:]}"
    assert argv[-1].startswith("You were started by the mcp-dispatch lifecycle supervisor")


def test_a_woken_agent_comes_up_stripped(tmp_path):
    """Only the dispatch server, only the dispatch tools.

    Waking a fully-loaded session would spend back the memory that running
    agents on demand was meant to save, and hand a remote-triggered process file
    and shell tools it was never meant to have."""
    project = tmp_path / "proj"
    project.mkdir()
    _, argv = _run_runtime(project, tmp_path)
    assert "--strict-mcp-config" in argv
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert list(cfg["mcpServers"]) == ["dispatch"]
    tools = argv[argv.index("--allowed-tools") + 1 : argv.index("--strict-mcp-config")]
    assert tools and all(t.startswith("mcp__dispatch__") for t in tools)


def test_a_wider_runtime_is_opt_in_per_nick(tmp_path):
    """Both widenings come from the allowlist's env table — operator-set, never
    message-set — and neither is the default."""
    project = tmp_path / "proj"
    project.mkdir()
    _, argv = _run_runtime(project, tmp_path, DISPATCH_AGENT_FULL_MCP="1")
    assert "--strict-mcp-config" not in argv
    assert "--mcp-config" not in argv

    _, argv = _run_runtime(project, tmp_path, DISPATCH_AGENT_TOOLS="Read Bash")
    assert argv[argv.index("--allowed-tools") + 1 : argv.index("--strict-mcp-config")] == [
        "Read",
        "Bash",
    ]


def test_the_runtime_refuses_a_project_that_is_not_there(tmp_path):
    """A typo'd path must fail before starting anything, not start claude in
    whatever directory the daemon happened to be in — the cwd decides the nick."""
    r, argv = _run_runtime(tmp_path / "nope", tmp_path)
    assert r.returncode == 66
    assert argv == []

    stub, _ = _stub_claude(tmp_path)
    r = subprocess.run(
        [str(RUNTIME)],
        capture_output=True,
        text=True,
        env={**os.environ, "DISPATCH_AGENT_CLAUDE": str(stub)},
    )
    assert r.returncode == 64


def test_a_woken_agent_waits_for_its_tools(tmp_path):
    """A session that starts before its dispatch server registers finds a
    connected server exposing no tools, reports that it cannot work, and exits —
    burning a start and a slot in the failure breaker for nothing. Observed on
    the first real wake. Nobody is waiting on a supervised start, so it can
    afford to be patient in a way an interactive session cannot."""
    project = tmp_path / "proj"
    project.mkdir()
    stub = tmp_path / "claude-stub"
    env_out = tmp_path / "env.txt"
    stub.write_text(f"#!/usr/bin/env bash\nenv > {env_out}\n")
    stub.chmod(0o755)
    subprocess.run(
        [str(RUNTIME), str(project)],
        capture_output=True,
        env={**os.environ, "DISPATCH_AGENT_CLAUDE": str(stub)},
    )
    seen = dict(line.split("=", 1) for line in env_out.read_text().splitlines() if "=" in line)
    assert int(seen["MCP_TIMEOUT"]) >= 60000
    assert int(seen["MCP_CONNECT_TIMEOUT_MS"]) >= 60000


def test_the_operators_env_still_wins_over_the_patience_defaults(tmp_path):
    """These are defaults, not policy. A nick that needs a different budget sets
    it in the allowlist's env table and that value must survive."""
    project = tmp_path / "proj"
    project.mkdir()
    stub = tmp_path / "claude-stub"
    env_out = tmp_path / "env.txt"
    stub.write_text(f"#!/usr/bin/env bash\nenv > {env_out}\n")
    stub.chmod(0o755)
    subprocess.run(
        [str(RUNTIME), str(project)],
        capture_output=True,
        env={**os.environ, "DISPATCH_AGENT_CLAUDE": str(stub), "MCP_TIMEOUT": "5000"},
    )
    seen = dict(line.split("=", 1) for line in env_out.read_text().splitlines() if "=" in line)
    assert seen["MCP_TIMEOUT"] == "5000"


# ---------------------------------------------------------------------------
# where an entry starts vs where its nick actually lives
# ---------------------------------------------------------------------------


def _register(dd, nick, cwd):
    """A registry record as server.py writes one when a session claims the nick."""
    agents = dd / ".agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{nick}.json").write_text(
        json.dumps({"nick": nick, "sessions": 1, "last_cwd": cwd, "last_session_id": f"{nick}-1"})
    )


def test_launch_dir_reads_the_project_out_of_argv(tmp_path):
    proj = tmp_path / "stope"
    proj.mkdir()
    spec = _spec(nick="stope", command=["/bin/true", str(proj)])
    assert supervisor.launch_dir(spec) == str(proj)


def test_an_explicit_cwd_wins_over_a_positional(tmp_path):
    proj = tmp_path / "stope"
    proj.mkdir()
    spec = _spec(nick="stope", command=["/bin/true", str(proj)], cwd="/elsewhere")
    assert supervisor.launch_dir(spec) == "/elsewhere"


def test_two_directory_arguments_are_not_guessed_between(tmp_path):
    """A confident warning about the wrong path is worse than none."""
    a, b = tmp_path / "one", tmp_path / "two"
    a.mkdir()
    b.mkdir()
    spec = _spec(nick="stope", command=["/bin/true", str(a), str(b)])
    assert supervisor.launch_dir(spec) == ""


def test_a_mismatch_between_config_and_registry_is_named(tmp_path):
    """The incident this exists for: an entry aimed one directory too high.

    A session started in the parent registers under the parent's name, so the
    nick the supervisor waits for never goes live and the mail is never
    inherited — while the start itself looks fine.
    """
    dd = tmp_path / "relay"
    dd.mkdir()
    parent = tmp_path / "Documents"
    (parent / "stope").mkdir(parents=True)
    _register(dd, "stope", str(parent / "stope"))

    spec = _spec(nick="stope", command=["/bin/true", str(parent)])
    hint = supervisor.misconfig_hint(dd, spec)
    assert "registers as 'documents'" in hint
    assert "not 'stope'" in hint
    assert str(parent / "stope") in hint, "say where the nick does live, not just that it doesn't"


def test_a_matching_directory_says_nothing(tmp_path):
    dd = tmp_path / "relay"
    dd.mkdir()
    proj = tmp_path / "stope"
    proj.mkdir()
    _register(dd, "stope", str(proj))
    assert (
        supervisor.misconfig_hint(dd, _spec(nick="stope", command=["/bin/true", str(proj)])) == ""
    )


def test_a_nick_never_claimed_anywhere_is_not_second_guessed(tmp_path):
    """No recorded directory means no evidence, and no evidence means no claim —
    a new project allowlisted before it is ever opened must not be warned about."""
    dd = tmp_path / "relay"
    dd.mkdir()
    proj = tmp_path / "brandnew"
    proj.mkdir()
    assert (
        supervisor.misconfig_hint(dd, _spec(nick="brandnew", command=["/bin/true", str(proj)]))
        == ""
    )


def test_the_park_says_why_when_the_directory_is_the_reason(tmp_path):
    """`no presence after 120s` is the symptom. The cause goes next to it."""
    dd = tmp_path / "relay"
    dd.mkdir()
    parent = tmp_path / "Documents"
    (parent / "stope").mkdir(parents=True)
    _register(dd, "stope", str(parent / "stope"))
    _plant(dd, "stope", mid="m1")

    cfg = _cfg(
        agents={"stope": _spec(nick="stope", command=["/bin/false", str(parent)])},
        log_dir=tmp_path / "logs",
        cooldown=0.01,
        max_failures=2,
    )
    lines: list[str] = []
    sup = supervisor.Supervisor(dd, cfg, log=lines.append)
    for _ in range(20):
        sup.tick()
        if sup.states["stope"].parked:
            break
        time.sleep(0.05)

    assert sup.states["stope"].parked
    assert any("PARKED" in line for line in lines)
    assert any("registers as 'documents'" in line for line in lines), (
        "a park with a knowable cause must name it"
    )


# ---------------------------------------------------------------------------
# The deaf session: somebody is home and cannot hear the door.
#
# No start rule covers it. A session holding its presence lock is never started
# for — correctly, since a second one would race the same inbox — so the nick
# looks handled while its mail goes unread. Nothing inside the session can fix
# it either: the hook that arms a watch loads at session start, so a window that
# was already open when the hooks were wired never runs it, and a parked session
# emits no event that would retry. The supervisor is the only process watching
# the relay that is not itself a session.
# ---------------------------------------------------------------------------


def _live_session(dd: Path, agent_id: str, state: Path, *, armed: bool):
    """A session holding its presence lock, optionally holding an arm lock too."""
    (dd / ".presence").mkdir(parents=True, exist_ok=True)
    pf = dd / ".presence" / f"{agent_id}.json"
    pf.write_text(json.dumps({"agent_id": agent_id, "pid": os.getpid(), "state_dir": str(state)}))
    handles = [dispatch_common.acquire_flock(pf)]
    if armed:
        state.mkdir(parents=True, exist_ok=True)
        handles.append(dispatch_common.acquire_flock(dispatch_common.arm_lock(agent_id, state)))
    return handles


def test_a_live_session_with_no_watch_and_waiting_mail_is_reported(tmp_path):
    dd = tmp_path / "relay"
    dd.mkdir()
    _plant(dd, "proj-42", mid="m1")
    handles = _live_session(dd, "proj-42", tmp_path / "state", armed=False)
    try:
        assert supervisor.deaf_sessions(dd) == [("proj-42", 1)]
    finally:
        for h in handles:
            if h is not None:
                h.close()


def test_a_listening_session_is_not_reported(tmp_path):
    dd = tmp_path / "relay"
    dd.mkdir()
    _plant(dd, "proj-42", mid="m1")
    handles = _live_session(dd, "proj-42", tmp_path / "state", armed=True)
    try:
        assert supervisor.deaf_sessions(dd) == []
    finally:
        for h in handles:
            if h is not None:
                h.close()


def test_a_deaf_session_with_an_empty_inbox_is_not_an_alert(tmp_path):
    """Latent, not current. Alerting here trains the operator to ignore alerts."""
    dd = tmp_path / "relay"
    dd.mkdir()
    handles = _live_session(dd, "proj-42", tmp_path / "state", armed=False)
    try:
        assert supervisor.deaf_sessions(dd) == []
    finally:
        for h in handles:
            if h is not None:
                h.close()


def test_expired_mail_does_not_make_a_session_deaf(tmp_path):
    dd = tmp_path / "relay"
    dd.mkdir()
    _plant(dd, "proj-42", mid="m1", ttl=60, timestamp="2020-01-01T00:00:00Z")
    handles = _live_session(dd, "proj-42", tmp_path / "state", armed=False)
    try:
        assert supervisor.deaf_sessions(dd) == []
    finally:
        for h in handles:
            if h is not None:
                h.close()


def test_the_sweep_says_it_once_and_again_only_if_it_recurs(tmp_path, no_desktop):
    """A standing condition on a five-second tick must not become a firehose,
    and a cleared-then-returned one must not be swallowed."""
    dd = tmp_path / "relay"
    dd.mkdir()
    _plant(dd, "proj-42", mid="m1")
    handles = _live_session(dd, "proj-42", tmp_path / "state", armed=False)
    lines: list[str] = []
    sup = supervisor.Supervisor(dd, _cfg(agents={}), log=lines.append)
    try:
        sup.tick()
        sup.tick()
        said = [ln for ln in lines if "no message watch" in ln]
        assert len(said) == 1, f"one alert for one condition, got {said}"
        assert "proj-42" in said[0] and "1 message" in said[0]

        for f in (dd / "proj-42").glob("*.json"):  # operator reads the mail
            f.unlink()
        sup.tick()
        _plant(dd, "proj-42", mid="m2")  # and a new message arrives
        sup.tick()
        assert len([ln for ln in lines if "no message watch" in ln]) == 2
        assert len(no_desktop) == 2, "the operator is told, not just the log"
    finally:
        for h in handles:
            if h is not None:
                h.close()


def test_the_sweep_covers_sessions_outside_the_allowlist(tmp_path, no_desktop):
    """An unreachable session is worth naming whether or not the operator ever
    chose to auto-start that nick."""
    dd = tmp_path / "relay"
    dd.mkdir()
    _plant(dd, "stranger-9", mid="m1")
    handles = _live_session(dd, "stranger-9", tmp_path / "state", armed=False)
    lines: list[str] = []
    sup = supervisor.Supervisor(dd, _cfg(agents={}), log=lines.append)
    try:
        sup.tick()
        assert any("stranger-9" in ln for ln in lines)
    finally:
        for h in handles:
            if h is not None:
                h.close()
