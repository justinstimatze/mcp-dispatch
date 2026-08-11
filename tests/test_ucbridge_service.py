"""Harness-independent daemon: the systemd unit renderer + the presence-gate
opt-out, for the native-protocol bridge. Mirrors test_gitsync_service.py's
structure and coverage; see that file's docstring for the shared background.

Two differences from the git bridge's unit worth calling out because they're
the whole reason this isn't a copy-paste of gitsync_service.py:

  * PrivateTmp is hard-coded "no" — /tmp/cc-socks/ must stay the REAL host /tmp,
    since every unsandboxed Claude Code session expects to find sockets there.
  * No network at all: RestrictAddressFamilies drops to AF_UNIX only, and
    ProtectSystem can go to "strict" instead of gitsync's "full".
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
import systemd_user  # noqa: E402
import ucbridge_service as svc  # noqa: E402

UCBRIDGE = REPO_ROOT / "bin" / "dispatch-ucbridge"


def _ucbridge_module():
    """Load the extensionless bin script as a module (same idiom as test_gitsync_service)."""
    loader = SourceFileLoader("dispatch_ucbridge", str(UCBRIDGE))
    spec = importlib.util.spec_from_loader("dispatch_ucbridge", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _unit(**over) -> str:
    kw = {
        "python": "/usr/bin/python3",
        "daemon": Path("/opt/mcp-dispatch/bin/dispatch-ucbridge"),
        "repo_root": Path("/opt/mcp-dispatch"),
        "config_path": Path("/home/a/.config/mcp-dispatch/config.toml"),
        "dispatch_dir": Path("/home/a/.config/mcp-dispatch/messages"),
        "socket_dir": Path("/tmp/cc-socks"),
        "sessions_dir": Path("/home/a/.claude/sessions"),
        "state_dir": Path("/home/a/.cache/mcp-dispatch"),
    }
    kw.update(over)
    return svc.render_unit(**kw)


# ── unit rendering ───────────────────────────────────────────────────────────


def test_unit_runs_the_daemon_ungated():
    text = _unit()
    assert "--no-presence-gate" in text
    assert "Restart=always" in text
    assert "WantedBy=default.target" in text


def test_config_path_is_baked_in():
    assert 'Environment="MCP_DISPATCH_CONFIG=/home/a/.config/mcp-dispatch/config.toml"' in _unit()


def test_env_values_with_spaces_survive():
    text = _unit(config_path=Path("/home/a/my dispatch/config.toml"))
    assert 'Environment="MCP_DISPATCH_CONFIG=/home/a/my dispatch/config.toml"' in text


def test_extra_env_is_emitted_and_validated():
    text = _unit(env={"SOME_KEY": "value"})
    assert 'Environment="SOME_KEY=value"' in text
    assert svc.validate_env(["A=1", "B_2=x=y"]) == {"A": "1", "B_2": "x=y"}
    for bad in ["novalue", "2BAD=x", "has space=x", "A\nB=x"]:
        with pytest.raises(svc.ServiceError):
            svc.validate_env([bad])


def test_percent_in_a_path_is_escaped():
    text = _unit(dispatch_dir=Path("/srv/100%mine/messages"))
    assert "/srv/100%%mine/messages" in text
    assert "/srv/100%mine/messages" not in text


def test_control_characters_are_refused():
    with pytest.raises(svc.ServiceError):
        _unit(repo_root=Path("/tmp/x\nExecStartPre=/bin/rm -rf /"))


def test_private_tmp_is_always_no():
    """The whole reason this module exists rather than reusing gitsync_service's
    dynamic PrivateTmp computation: /tmp/cc-socks/ must be the REAL host /tmp so
    every unsandboxed Claude Code session can still find the bridge's sockets —
    unlike gitsync, this is never conditional on where dispatch_dir happens to
    live."""
    assert "PrivateTmp=no" in _unit()
    assert "PrivateTmp=no" in _unit(dispatch_dir=Path("/home/a/somewhere/messages"))


def test_address_family_is_unix_only():
    """This daemon never touches the network, unlike gitsync (which needs
    AF_INET/6 for the git remote) — so the unit can be tighter here."""
    assert "RestrictAddressFamilies=AF_UNIX" in _unit()
    assert "AF_INET" not in _unit()


def test_protect_system_is_strict():
    """No unpredictable ssh-agent/known_hosts paths to preserve (unlike
    gitsync's ProtectSystem=full), so this can lock down further."""
    assert "ProtectSystem=strict" in _unit()


def test_umask_follows_group_mode():
    assert "UMask=0007" in _unit(group_mode=True)
    assert "UMask=0077" in _unit(group_mode=False)


def test_writable_paths_cover_every_tree_the_daemon_writes():
    text = _unit()
    for p in (
        "/home/a/.config/mcp-dispatch/messages",
        "/tmp/cc-socks",
        "/home/a/.claude/sessions",
        "/home/a/.cache/mcp-dispatch",
    ):
        assert f"ReadWritePaths=-{p}" in text


def test_no_capability_implying_directives():
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
    assert '"/opt/my dispatch/bin/dispatch-ucbridge"' in _unit(
        daemon=Path("/opt/my dispatch/bin/dispatch-ucbridge")
    )


# ── presence gate (process lifetime) ─────────────────────────────────────────


def _bus(tmp_path: Path) -> tuple[Path, Path]:
    dispatch_dir = tmp_path / "messages"
    (dispatch_dir / "alice").mkdir(parents=True)
    cfg = tmp_path / "config.toml"
    # socket_dir MUST be overridden: the default (/tmp/cc-socks) is a
    # host-global, security-sensitive path (see bridge_native.py's own
    # docstring), not scoped by dispatch_dir or by the HOME override _launch
    # sets below. A test that let bridge.start() bind the real default would
    # leave a live peer-dispatch-alice.sock on the actual machine running the
    # suite — worse, proc.kill() (SIGKILL) in every test's cleanup bypasses
    # the finally: bridge.stop() that would normally remove it, so the file
    # leaks permanently and could collide with a genuine dispatch-ucbridge
    # instance later, exactly the cross-relay collision
    # NativeInboundListener.start() now refuses to let happen.
    socket_dir = tmp_path / "cc-socks"
    cfg.write_text(
        f'dispatch_dir = "{dispatch_dir}"\n\n'
        '[bridge]\nenabled = true\nnicks = ["alice"]\n'
        f'socket_dir = "{socket_dir}"\n'
    )
    return dispatch_dir, cfg


def _launch(cfg: Path, state: Path, *args, grace="0.5", ready_poll="0.3"):
    return subprocess.Popen(
        [sys.executable, str(UCBRIDGE), "--interval", "0.2", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={
            "MCP_DISPATCH_CONFIG": str(cfg),
            "MCP_DISPATCH_STATE_DIR": str(state),
            "MCP_DISPATCH_UCBRIDGE_GRACE": grace,
            "MCP_DISPATCH_UCBRIDGE_READY_POLL": ready_poll,
            "PYTHONUNBUFFERED": "1",
            "PATH": "/usr/bin:/bin",
            "HOME": str(cfg.parent),
        },
    )


def test_gated_daemon_still_exits_without_presence(tmp_path):
    _, cfg = _bus(tmp_path)
    proc = _launch(cfg, tmp_path / "state")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert "startup grace" in out


def test_ungated_daemon_survives_the_grace(tmp_path):
    _, cfg = _bus(tmp_path)
    proc = _launch(cfg, tmp_path / "state", "--no-presence-gate")
    try:
        time.sleep(2.0)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def test_ungated_waits_for_the_lock_instead_of_exiting(tmp_path):
    dispatch_dir, cfg = _bus(tmp_path)
    state = tmp_path / "state"
    lock = state / f"ucbridge-{common.md5_key(str(dispatch_dir))}.lock"
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


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda t: t.replace("enabled = true", "enabled = false"), "enabled is not true"),
        (lambda t: t.replace('nicks = ["alice"]', "nicks = []"), "nicks is empty"),
        # Regression: an invalid nick used to reach NativeBridge's constructor
        # unguarded, raising ValueError uncaught and crash-looping the unit
        # instead of waiting for the same config fix every other problem here
        # gets.
        (lambda t: t.replace('nicks = ["alice"]', 'nicks = ["Not Valid!"]'), "Invalid"),
    ],
    ids=["bridge-disabled", "no-nicks", "invalid-nick"],
)
def test_supervised_daemon_waits_instead_of_exiting(tmp_path, mutate, expected):
    _, cfg = _bus(tmp_path)
    cfg.write_text(mutate(cfg.read_text()))
    proc = _launch(cfg, tmp_path / "state", "--no-presence-gate")
    try:
        time.sleep(2.0)
        assert proc.poll() is None  # waiting, not dead
    finally:
        proc.kill()
        out, _ = proc.communicate(timeout=10)
    assert expected in out
    assert "waiting — config is re-read every pass" in out


def test_supervised_daemon_starts_when_the_config_is_fixed(tmp_path):
    _, cfg = _bus(tmp_path)
    good = cfg.read_text()
    cfg.write_text(good.replace("enabled = true", "enabled = false"))
    proc = _launch(cfg, tmp_path / "state", "--no-presence-gate", ready_poll="0.3")
    try:
        time.sleep(1.0)
        assert proc.poll() is None
        cfg.write_text(good)
        time.sleep(2.0)
        assert proc.poll() is None
    finally:
        proc.kill()
        out, _ = proc.communicate(timeout=10)
    assert "ready — starting the bridge" in out
    assert "bridging" in out


def test_config_can_disable_the_gate(tmp_path):
    _, cfg = _bus(tmp_path)
    cfg.write_text(cfg.read_text() + "presence_gate = false\n")
    proc = _launch(cfg, tmp_path / "state")
    try:
        time.sleep(2.0)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def test_cli_flag_overrides_config(tmp_path):
    _, cfg = _bus(tmp_path)
    cfg.write_text(cfg.read_text() + "presence_gate = false\n")
    proc = _launch(cfg, tmp_path / "state", "--presence-gate")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert "startup grace" in out


# ── allow_outbound wiring ────────────────────────────────────────────────────


def test_build_defaults_allow_outbound_off(tmp_path):
    uc = _ucbridge_module()
    bridge = uc._build({"nicks": ["alice"]}, tmp_path / "messages")
    assert bridge.allow_outbound is False


def test_build_honors_allow_outbound_true(tmp_path):
    uc = _ucbridge_module()
    bridge = uc._build({"nicks": ["alice"], "allow_outbound": True}, tmp_path / "messages")
    assert bridge.allow_outbound is True


# ── failure backoff ──────────────────────────────────────────────────────────


def test_backoff_escalates_then_caps():
    uc = _ucbridge_module()
    assert uc._backoff_delay(0, 2.0) == 2.0
    assert uc._backoff_delay(1, 2.0) == 4.0
    assert uc._backoff_delay(2, 2.0) == 8.0
    assert uc._backoff_delay(99, 2.0) == uc.MAX_BACKOFF_SECONDS


def test_backoff_survives_a_very_long_outage():
    uc = _ucbridge_module()
    for failures in (1_024, 10_000, 10**6):
        assert uc._backoff_delay(failures, 2.0) == uc.MAX_BACKOFF_SECONDS


# ── the `service` subcommand ─────────────────────────────────────────────────


def _service(cfg: Path, *args, home: Path):
    return subprocess.run(
        [sys.executable, str(UCBRIDGE), "service", *args],
        capture_output=True,
        text=True,
        env={"MCP_DISPATCH_CONFIG": str(cfg), "HOME": str(home), "PATH": "/usr/bin:/bin"},
    )


def test_service_show_renders_without_touching_the_system(tmp_path):
    _, cfg = _bus(tmp_path)
    r = _service(cfg, "show", home=tmp_path)
    assert r.returncode == 0
    assert "--no-presence-gate" in r.stdout
    assert not (tmp_path / ".config" / "systemd").exists()


def test_service_refuses_when_the_bridge_is_disabled(tmp_path):
    _, cfg = _bus(tmp_path)
    cfg.write_text(cfg.read_text().replace("enabled = true", "enabled = false"))
    r = _service(cfg, "show", home=tmp_path)
    assert r.returncode == 2
    assert "enabled is false" in r.stdout


def test_service_refuses_without_nicks(tmp_path):
    _, cfg = _bus(tmp_path)
    cfg.write_text(cfg.read_text().replace('nicks = ["alice"]', "nicks = []"))
    r = _service(cfg, "show", home=tmp_path)
    assert r.returncode == 2
    assert "nicks is empty" in r.stdout


@pytest.mark.skipif(
    not svc.systemctl_available(), reason="no systemd user session (install refuses, by design)"
)
def test_service_dry_run_writes_nothing(tmp_path):
    _, cfg = _bus(tmp_path)
    r = _service(cfg, "install", "--dry-run", home=tmp_path)
    assert r.returncode == 0
    assert "would write" in r.stdout
    assert not (tmp_path / ".config" / "systemd").exists()


def test_dry_run_refuses_without_systemd(monkeypatch):
    monkeypatch.setattr(systemd_user, "systemctl_available", lambda: False)
    with pytest.raises(svc.ServiceError):
        svc.install("[Service]\n", dry_run=True)


def test_config_untouched_by_service_commands(tmp_path):
    _, cfg = _bus(tmp_path)
    before = cfg.read_text()
    _service(cfg, "show", home=tmp_path)
    _service(cfg, "install", "--dry-run", home=tmp_path)
    assert cfg.read_text() == before
