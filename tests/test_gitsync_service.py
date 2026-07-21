"""Harness-independent daemon: the systemd unit renderer + the presence-gate opt-out.

Background: the git bridge was only ever started by a Claude Code SessionStart
hook, and even when started by hand it self-terminated after a 60s grace unless an
mcp-dispatch MCP *server* was holding a presence flock. Agents on another harness
(openclaw, Hermes, a plain script) therefore had no running daemon and had to run
`git pull`/`git push` by hand to exchange anything.

Two halves, tested separately: `render_unit` is pure text so it's asserted
directly, and the gate is a process lifetime so it's driven as a subprocess with
the grace shrunk via MCP_DISPATCH_GITSYNC_GRACE.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import dispatch_common as common  # noqa: E402
import gitsync_service as svc  # noqa: E402

GITSYNC = REPO_ROOT / "bin" / "dispatch-gitsync"


def _gitsync_module():
    """Load the extensionless bin script as a module (same idiom as test_tail)."""
    loader = SourceFileLoader("dispatch_gitsync", str(GITSYNC))
    spec = importlib.util.spec_from_loader("dispatch_gitsync", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _unit(**over) -> str:
    kw = {
        "python": "/usr/bin/python3",
        "daemon": Path("/opt/mcp-dispatch/bin/dispatch-gitsync"),
        "repo_root": Path("/opt/mcp-dispatch"),
        "config_path": Path("/home/a/.config/mcp-dispatch/config.toml"),
        "dispatch_dir": Path("/home/a/.config/mcp-dispatch/messages"),
        "repo_dir": Path("/home/a/.config/mcp-dispatch/bus"),
        "state_dir": Path("/home/a/.cache/mcp-dispatch"),
    }
    kw.update(over)
    return svc.render_unit(**kw)


# ── unit rendering ───────────────────────────────────────────────────────────


def test_unit_runs_the_daemon_ungated():
    """The whole point: a supervised daemon must not use the presence-gated
    lifetime, or systemd would restart a process that exits every 60s."""
    text = _unit()
    assert "--no-presence-gate" in text
    assert "Restart=always" in text
    assert "WantedBy=default.target" in text


def test_config_path_is_baked_in():
    """A user service inherits almost nothing from the login shell, so an ambient
    MCP_DISPATCH_CONFIG would silently resolve a different relay than the CLI did."""
    assert 'Environment="MCP_DISPATCH_CONFIG=/home/a/.config/mcp-dispatch/config.toml"' in _unit()


def test_env_values_with_spaces_survive():
    """systemd unquotes an Environment= line into a *list* of assignments split on
    whitespace, so an unquoted path with a space truncates to its first word and
    the daemon silently resolves a different relay than the CLI just did."""
    text = _unit(config_path=Path("/home/a/my dispatch/config.toml"))
    assert 'Environment="MCP_DISPATCH_CONFIG=/home/a/my dispatch/config.toml"' in text


def test_extra_env_is_emitted_and_validated():
    text = _unit(env={"SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh"})
    assert 'Environment="SSH_AUTH_SOCK=/run/user/1000/keyring/ssh"' in text
    assert svc.validate_env(["A=1", "B_2=x=y"]) == {"A": "1", "B_2": "x=y"}
    for bad in ["novalue", "2BAD=x", "has space=x", "A\nB=x"]:
        with pytest.raises(svc.ServiceError):
            svc.validate_env([bad])


def test_percent_in_a_path_is_escaped():
    """'%' introduces a systemd *specifier* — an unescaped one silently rewrites
    the path (%h -> home dir), pointing the daemon at the wrong relay."""
    text = _unit(dispatch_dir=Path("/srv/100%mine/messages"))
    assert "/srv/100%%mine/messages" in text
    assert "/srv/100%mine/messages" not in text


def test_control_characters_are_refused():
    """A newline in a value would end the directive and start a forged one."""
    with pytest.raises(svc.ServiceError):
        _unit(repo_root=Path("/tmp/x\nExecStartPre=/bin/rm -rf /"))


def test_private_tmp_off_when_the_relay_lives_under_tmp():
    """PrivateTmp would namespace away /var/tmp — hiding the exact directory the
    daemon exists to mirror. group_mode's documented layout puts it there."""
    assert "PrivateTmp=no" in _unit(dispatch_dir=Path("/var/tmp/mcp-dispatch"))
    assert "PrivateTmp=yes" in _unit()  # ...but stays on for a normal $HOME relay


def test_umask_follows_group_mode():
    """Materialized messages must inherit the relay's sharing model (server.py
    sets the same umask); a sandbox that widened permissions would be worse."""
    assert "UMask=0007" in _unit(group_mode=True)
    assert "UMask=0077" in _unit(group_mode=False)


def test_writable_paths_cover_every_tree_the_daemon_writes():
    text = _unit()
    for p in ("/home/a/.config/mcp-dispatch/messages", "/home/a/.config/mcp-dispatch/bus"):
        assert f"ReadWritePaths={p}" in text
    assert "ReadWritePaths=/home/a/.cache/mcp-dispatch" in text


def test_no_capability_implying_directives():
    """Regression, found by installing this for real: a *user* manager can't change
    the capability bounding set, so any directive that implies one kills the unit at
    spawn with 218/CAPABILITIES — before a line of Python runs. Each of these reads
    like a free hardening win and is in fact a hard failure."""
    text = _unit()
    for directive in (
        "ProtectClock",
        "ProtectControlGroups",
        "ProtectKernelTunables",
        "ProtectKernelModules",
        "PrivateDevices",
        "CapabilityBoundingSet",
        "AmbientCapabilities",
    ):
        assert f"\n{directive}=" not in text, f"{directive} implies a capability drop"


def test_exec_args_are_quoted():
    assert 'ExecStart="/usr/bin/python3"' in _unit(python="/usr/bin/python3")
    assert '"/opt/my dispatch/bin/dispatch-gitsync"' in _unit(
        daemon=Path("/opt/my dispatch/bin/dispatch-gitsync")
    )


# ── presence gate (process lifetime) ─────────────────────────────────────────


def _bus(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A minimal working setup: a relay dir, a local-only git clone, a config."""
    dispatch_dir = tmp_path / "messages"
    (dispatch_dir / "alice").mkdir(parents=True)
    repo_dir = tmp_path / "bus"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'dispatch_dir = "{dispatch_dir}"\n\n[git]\nenabled = true\nrepo_dir = "{repo_dir}"\n'
    )
    return dispatch_dir, repo_dir, cfg


def _launch(cfg: Path, state: Path, *args, grace="0.5"):
    return subprocess.Popen(
        [sys.executable, str(GITSYNC), "--interval", "0.2", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={
            "MCP_DISPATCH_CONFIG": str(cfg),
            "MCP_DISPATCH_STATE_DIR": str(state),
            "MCP_DISPATCH_GITSYNC_GRACE": grace,
            "PYTHONUNBUFFERED": "1",  # these tests kill the process; buffered output is lost
            "PATH": "/usr/bin:/bin",
            "HOME": str(cfg.parent),
        },
    )


def test_gated_daemon_still_exits_without_presence(tmp_path):
    """The default must not change: a hook-spawned daemon that nobody claims
    presence for still self-terminates, so it can't orphan."""
    _, _, cfg = _bus(tmp_path)
    proc = _launch(cfg, tmp_path / "state")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert "startup grace" in out


def test_ungated_daemon_survives_the_grace(tmp_path):
    """The fix for the openclaw/Hermes case: no presence holder anywhere, and the
    daemon keeps mirroring instead of quietly dying after a minute."""
    _, _, cfg = _bus(tmp_path)
    proc = _launch(cfg, tmp_path / "state", "--no-presence-gate")
    try:
        time.sleep(2.0)  # 4x the grace
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def test_ungated_waits_for_the_lock_instead_of_exiting(tmp_path):
    """A service and a hook-spawned daemon coexist on one host. The loser of the
    lock race must WAIT — exiting would land systemd in a restart loop, and would
    leave nothing mirroring once the hook daemon's session ended."""
    dispatch_dir, _, cfg = _bus(tmp_path)
    state = tmp_path / "state"
    lock = state / f"gitsync-{common.md5_key(str(dispatch_dir))}.lock"
    holder = common.acquire_flock(lock)
    assert holder is not None
    proc = _launch(cfg, state, "--no-presence-gate")
    try:
        time.sleep(2.0)
        assert proc.poll() is None  # waiting, not dead
    finally:
        proc.kill()
        out, _ = proc.communicate(timeout=10)
        holder.close()
    assert "waiting to take over" in out


def test_ungated_waits_for_a_relay_that_does_not_exist_yet(tmp_path):
    """A service starts at login, possibly before any agent has created the relay.
    Exiting there would need a manual restart to ever recover."""
    repo_dir = tmp_path / "bus"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    cfg = tmp_path / "config.toml"
    missing = tmp_path / "not-yet"
    cfg.write_text(
        f'dispatch_dir = "{missing}"\n\n[git]\nenabled = true\nrepo_dir = "{repo_dir}"\n'
    )
    proc = _launch(cfg, tmp_path / "state", "--no-presence-gate")
    try:
        time.sleep(1.0)
        assert proc.poll() is None
        missing.mkdir()
        time.sleep(1.5)
        assert proc.poll() is None
    finally:
        proc.kill()
        out, _ = proc.communicate(timeout=10)
    assert "waiting for a relay" in out
    assert "relay appeared" in out


def test_config_can_disable_the_gate(tmp_path):
    """Same opt-out without editing anyone's launcher: `presence_gate = false`."""
    dispatch_dir, repo_dir, cfg = _bus(tmp_path)
    cfg.write_text(cfg.read_text() + "presence_gate = false\n")
    proc = _launch(cfg, tmp_path / "state")
    try:
        time.sleep(2.0)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def test_cli_flag_overrides_config(tmp_path):
    """--presence-gate forces the safe default back on even when config drops it."""
    _, _, cfg = _bus(tmp_path)
    cfg.write_text(cfg.read_text() + "presence_gate = false\n")
    proc = _launch(cfg, tmp_path / "state", "--presence-gate")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert "startup grace" in out


# ── failure backoff ──────────────────────────────────────────────────────────


def test_backoff_escalates_then_caps():
    gs = _gitsync_module()
    assert gs._backoff_delay(0, 2.0) == 2.0  # healthy: plain interval
    assert gs._backoff_delay(1, 2.0) == 4.0
    assert gs._backoff_delay(2, 2.0) == 8.0
    assert gs._backoff_delay(99, 2.0) == gs.MAX_BACKOFF_SECONDS


def test_backoff_survives_a_very_long_outage():
    """The bug this guards: `failures` counts bad ticks for the process's whole
    life, so an unclamped 2**failures overflows float past ~1024 — about 17h
    against an expired token — and takes the daemon down with an OverflowError."""
    gs = _gitsync_module()
    for failures in (1_024, 10_000, 10**6):
        assert gs._backoff_delay(failures, 2.0) == gs.MAX_BACKOFF_SECONDS


def test_bad_grace_env_does_not_break_the_cli():
    """Parsing this at import means a typo would traceback `status` and `init`,
    which never even use the grace."""
    gs = _gitsync_module()
    assert gs._float_env("MCP_DISPATCH_GITSYNC_GRACE", 60.0) in (60.0, 0.5)
    import os as _os

    _os.environ["MCP_DISPATCH_GITSYNC_GRACE"] = "60 seconds"
    try:
        assert gs._float_env("MCP_DISPATCH_GITSYNC_GRACE", 60.0) == 60.0
    finally:
        _os.environ.pop("MCP_DISPATCH_GITSYNC_GRACE", None)


# ── the `service` subcommand ─────────────────────────────────────────────────


def _service(cfg: Path, *args, home: Path):
    return subprocess.run(
        [sys.executable, str(GITSYNC), "service", *args],
        capture_output=True,
        text=True,
        env={"MCP_DISPATCH_CONFIG": str(cfg), "HOME": str(home), "PATH": "/usr/bin:/bin"},
    )


def test_service_show_renders_without_touching_the_system(tmp_path):
    _, _, cfg = _bus(tmp_path)
    r = _service(cfg, "show", home=tmp_path)
    assert r.returncode == 0
    assert "--no-presence-gate" in r.stdout
    assert not (tmp_path / ".config" / "systemd").exists()


def test_service_refuses_when_the_bridge_is_disabled(tmp_path):
    """A unit wrapping a disabled bridge would exit immediately and be restarted
    forever. Better to fail at install than ship a flapping service."""
    dispatch_dir, repo_dir, cfg = _bus(tmp_path)
    cfg.write_text(cfg.read_text().replace("enabled = true", "enabled = false"))
    r = _service(cfg, "show", home=tmp_path)
    assert r.returncode == 2
    assert "enabled is false" in r.stdout


def test_service_refuses_without_a_clone(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[git]\nenabled = true\n")
    r = _service(cfg, "show", home=tmp_path)
    assert r.returncode == 2
    assert "init" in r.stdout


@pytest.mark.skipif(
    not svc.systemctl_available(), reason="no systemd user session (install refuses, by design)"
)
def test_service_dry_run_writes_nothing(tmp_path):
    _, _, cfg = _bus(tmp_path)
    r = _service(cfg, "install", "--dry-run", home=tmp_path)
    assert r.returncode == 0
    assert "would write" in r.stdout
    assert not (tmp_path / ".config" / "systemd").exists()


def test_init_service_is_one_command_and_rerunnable(tmp_path):
    """The whole setup for a host that isn't running Claude Code. Both halves are
    idempotent, so re-running it is also the upgrade path — the failure this
    guards is `init` not seeing the [git] config it just wrote."""
    bus = tmp_path / "bus"
    bus.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(bus)], check=True)
    (tmp_path / "messages").mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'dispatch_dir = "{tmp_path / "messages"}"\n')

    def once():
        return subprocess.run(
            # --dry-run so this exercises the plumbing without touching the real
            # user manager of whatever machine the suite runs on.
            [sys.executable, str(GITSYNC), "init", str(bus), "--service", "--dry-run"],
            capture_output=True,
            text=True,
            env={"MCP_DISPATCH_CONFIG": str(cfg), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )

    first = once()
    assert "using existing clone" in first.stdout
    assert "installing the systemd user service" in first.stdout
    # The config `init` wrote must be visible to the `service` half in the SAME run.
    assert "no git clone configured" not in first.stdout
    assert "[git]" in cfg.read_text()

    second = once()  # idempotent: no clobbered config, no duplicate [git] block
    assert cfg.read_text().count("[git]") == 1
    assert "already has a [git] section" in second.stdout


def test_dry_run_refuses_without_systemd(tmp_path, monkeypatch):
    """A dry run exists to say what WILL happen. Reporting a plan that can't run
    (macOS, a container with no user manager) says the opposite."""
    monkeypatch.setattr(svc, "systemctl_available", lambda: False)
    with pytest.raises(svc.ServiceError):
        svc.install("[Service]\n", dry_run=True)


def test_config_untouched_by_service_commands(tmp_path):
    """`service` reads config; it must never rewrite it (`init` is what writes)."""
    _, _, cfg = _bus(tmp_path)
    before = cfg.read_text()
    _service(cfg, "show", home=tmp_path)
    _service(cfg, "install", "--dry-run", home=tmp_path)
    assert cfg.read_text() == before
