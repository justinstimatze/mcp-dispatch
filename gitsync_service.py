"""Run the cross-host git bridge as a systemd **user** service.

Until now the only thing that started ``bin/dispatch-gitsync`` was
``hooks/dispatch-gitsync-arm.py``, a *Claude Code* SessionStart/Stop hook. Agents
on any other harness (openclaw, Hermes, a plain script, the TUI, a human) never
fired it, so no daemon ran, nothing pushed, nothing fetched — and the only way to
exchange a message was to ``git pull``/``git push`` by hand. This module makes the
daemon harness-independent: one user-level unit, started at login, restarted on
failure, entirely unaware of which agent runtime is talking through the relay.

The rendered unit is deliberately conservative about sandboxing. Over-hardening a
process that has to run ``git`` (which in turn runs ``ssh``, reads credentials,
and talks to the network) breaks it in ways that look like the very "talking to a
wall" symptom this is here to fix, so each directive below is one we can justify:

* ``ProtectSystem=full`` (not ``strict``) — /usr, /boot, /efi and /etc read-only,
  while $HOME and /run stay writable. ``strict`` would also freeze ``/run/user``,
  cutting the ssh-agent socket, and ``~/.ssh/known_hosts``.
* ``PrivateTmp`` **only** when neither the relay nor the clone lives under /tmp or
  /var/tmp — the private namespace would otherwise hide the very directory the
  daemon exists to mirror. ``dispatch_dir = "/var/tmp/mcp-dispatch"`` is the
  documented group-mode layout, so this is a real case, not a hypothetical.
* ``UMask`` follows ``group_mode`` exactly as ``server.py`` does, so materialized
  messages land 0660 in a shared relay and 0600 otherwise — a sandbox that quietly
  widened permissions would be worse than none.

Everything interpolated into the unit is validated and escaped (`_esc`): a unit
file is a config format with its own metacharacter (``%``) and its own quoting
rules, and the values here come from a user config file, so they get treated as
untrusted input rather than pasted in raw.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 - only ever runs `systemctl` by fixed name
from pathlib import Path

UNIT_NAME = "mcp-dispatch-gitsync.service"

# A systemd env var name. Deliberately stricter than POSIX allows: we are writing
# into a config file, and a key with a newline or '=' in it would forge a directive.
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ServiceError(RuntimeError):
    """Install/uninstall could not proceed (bad input, or no systemd here)."""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _esc(value: str) -> str:
    """Make ``value`` safe to interpolate into a unit-file directive.

    Two hazards, both silent: a newline (or any control char) ends the directive
    and starts a forged one, and ``%`` introduces a systemd *specifier* (``%h``
    expands to the home dir), so an unescaped ``%`` in a path silently rewrites it.
    """
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ServiceError(f"control character in unit value: {value!r}")
    return value.replace("%", "%%")


def _quote_arg(value: str) -> str:
    """Quote one ExecStart argument. systemd applies shell-ish quoting to the
    command line, so a path with a space would otherwise split into two args."""
    return '"' + _esc(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _under_tmp(path: Path) -> bool:
    """True if ``path`` lives in a directory ``PrivateTmp=`` would namespace away."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser()
    return any(
        resolved == parent or parent in resolved.parents
        for parent in (Path("/tmp"), Path("/var/tmp"))  # nosec B108 - path check, not a write
    )


def validate_env(pairs: list[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` strings for ``Environment=`` lines, rejecting anything
    that could forge a directive. Values are escaped at render time, not here."""
    out: dict[str, str] = {}
    for raw in pairs:
        key, sep, value = raw.partition("=")
        if not sep or not ENV_KEY_RE.match(key):
            raise ServiceError(f"--env expects KEY=VALUE with a plain KEY, got {raw!r}")
        out[key] = value
    return out


def render_unit(
    *,
    python: str,
    daemon: Path,
    repo_root: Path,
    config_path: Path,
    dispatch_dir: Path,
    repo_dir: Path,
    state_dir: Path,
    group_mode: bool = False,
    interval: float | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Render the systemd user unit as text. Pure — writes nothing, so it is the
    seam the tests drive."""
    exec_args = [python, str(daemon), "--no-presence-gate"]
    if interval is not None:
        exec_args += ["--interval", repr(float(interval))]
    exec_start = " ".join(_quote_arg(a) for a in exec_args)

    # MCP_DISPATCH_CONFIG is baked in unconditionally: a user service inherits
    # almost nothing from the login shell, so relying on the ambient value would
    # make the service resolve a *different* config than the CLI did at install.
    # PYTHONUNBUFFERED so `journalctl -f` shows progress live rather than in 8KB
    # bursts — a bridge you can't watch is one you can't diagnose.
    environment = {
        "MCP_DISPATCH_CONFIG": str(config_path),
        "PYTHONUNBUFFERED": "1",
        **(env or {}),
    }
    # Each assignment is quoted as a whole. systemd unquotes an Environment= line
    # into a *list* of assignments split on whitespace, so an unquoted value with a
    # space (a config at "~/my dispatch/config.toml") silently truncates to the
    # first word and appends junk — the daemon then resolves a different relay than
    # the CLI just did. Same class of bug as the ExecStart quoting below.
    env_lines = "\n".join(f"Environment={_quote_arg(f'{k}={v}')}" for k, v in environment.items())

    # Only the three trees the daemon actually writes. Listed even under
    # ProtectSystem=full (which leaves $HOME writable) so tightening to `strict`
    # later is a one-word change rather than a rediscovery of these paths.
    rw = "\n".join(
        f"ReadWritePaths={_esc(str(p))}" for p in dict.fromkeys([dispatch_dir, repo_dir, state_dir])
    )
    private_tmp = "yes" if not (_under_tmp(dispatch_dir) or _under_tmp(repo_dir)) else "no"
    umask = "0007" if group_mode else "0077"

    return f"""\
# Generated by `dispatch-gitsync service install`. Re-run that to regenerate;
# local edits are overwritten. See gitsync_service.py for why each knob is set.
[Unit]
Description=mcp-dispatch cross-host git bridge
Documentation=https://github.com/justinstimatze/mcp-dispatch
After=network-online.target
Wants=network-online.target
# A crash loop must not hammer the git remote: give up after 10 restarts in 5min.
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory={_esc(str(repo_root))}
ExecStart={exec_start}
{env_lines}
Restart=always
RestartSec=5
# Materialized messages inherit the relay's sharing model, exactly as server.py.
UMask={umask}

# -- sandboxing (see module docstring: git/ssh need net, $HOME and /run) -------
# Everything here is seccomp- or namespace-based. Deliberately NO ProtectClock,
# ProtectControlGroups, ProtectKernelTunables or ProtectKernelModules: each implies
# a CapabilityBoundingSet change, which a *user* manager cannot perform — the unit
# then dies at spawn with 218/CAPABILITIES before running a line of Python. They
# also guard operations an unprivileged process could never do in the first place.
NoNewPrivileges=yes
ProtectSystem=full
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
# AF_UNIX for the ssh-agent socket, AF_INET/6 for the git remote. Nothing else.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
PrivateTmp={private_tmp}
{rw}

[Install]
WantedBy=default.target
"""


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(base)) / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT_NAME


def systemctl_available() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 B607 - fixed binary, fixed literal args
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def install(unit_text: str, *, enable: bool = True, dry_run: bool = False) -> list[str]:
    """Write the unit and (by default) enable + (re)start it. Idempotent: running
    it again is exactly how you *upgrade* an existing install — the unit is
    rewritten from current config and the daemon restarted onto it."""
    path = unit_path()
    if not systemctl_available():
        # Checked before the dry-run branch too: a dry run exists to tell you what
        # WILL happen, and reporting a plan that can't run says the opposite.
        raise ServiceError(
            "no systemd user session here. Run the daemon under whatever supervisor "
            "you do have (launchd, supervisord, tmux) with:  "
            "bin/dispatch-gitsync --no-presence-gate"
        )
    steps = [f"write {path}"]
    if dry_run:
        return steps + (
            ["systemctl --user daemon-reload", f"enable --now {UNIT_NAME}"] if enable else []
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text)
    path.chmod(0o600)  # may carry an auth token in Environment=
    _systemctl("daemon-reload", check=True)
    steps.append("systemctl --user daemon-reload")
    if enable:
        _systemctl("enable", UNIT_NAME, check=True)
        # Clear a latched start limit FIRST. Once a unit trips StartLimitBurst it
        # stays `failed` and every restart returns "Start request repeated too
        # quickly" — so re-installing, which is both the upgrade path and the
        # obvious thing to try when the service is crash-looping, would fail
        # exactly when it's needed most. No-op on a healthy unit.
        _systemctl("reset-failed", UNIT_NAME)
        # restart, not start: an upgrade must land the running process on the new unit.
        _systemctl("restart", UNIT_NAME, check=True)
        steps += [f"systemctl --user enable {UNIT_NAME}", f"systemctl --user restart {UNIT_NAME}"]
    return steps


def uninstall(*, dry_run: bool = False) -> list[str]:
    path = unit_path()
    steps = [f"systemctl --user disable --now {UNIT_NAME}", f"remove {path}"]
    if dry_run:
        return steps
    if systemctl_available():
        _systemctl("disable", "--now", UNIT_NAME)
    path.unlink(missing_ok=True)
    if systemctl_available():
        _systemctl("daemon-reload")
    return steps


def status_lines() -> list[str]:
    """Short human-readable service status for `dispatch-gitsync status`."""
    path = unit_path()
    if not path.exists():
        return ["service unit:     (not installed — `dispatch-gitsync service install`)"]
    if not systemctl_available():
        return [f"service unit:     {path} (no systemd session to query)"]
    active = _systemctl("is-active", UNIT_NAME).stdout.strip() or "unknown"
    enabled = _systemctl("is-enabled", UNIT_NAME).stdout.strip() or "unknown"
    return [f"service unit:     {path}", f"service state:    {active} / {enabled} at login"]
