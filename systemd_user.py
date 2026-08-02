"""Generic systemd **user** unit plumbing, shared by every dispatch daemon.

``gitsync_service.py`` grew this machinery first — escaping values into a unit
file, writing it 0600, reload/enable/restart, reading its state back. The
lifecycle supervisor needs all of it and differs only in the unit's *name* and
*body*, so the two would otherwise be near-copies of ~140 lines of code whose
whole job is to be careful with untrusted input. That is the last place a repo
wants a second implementation.

So the parts that don't vary live here, parameterized by unit name, and each
service module keeps only its own ``UNIT_NAME`` and ``render_unit``. Escaping in
particular is *not* per-service: a unit file is a config format with its own
metacharacter (``%``) and its own quoting rules, and every value we interpolate
comes from a user config file.

Stdlib only, no import-time side effects.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404 - only ever runs `systemctl` by fixed name
from pathlib import Path

# A systemd env var name. Deliberately stricter than POSIX allows: we are writing
# into a config file, and a key with a newline or '=' in it would forge a directive.
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ServiceError(RuntimeError):
    """Install/uninstall could not proceed (bad input, or no systemd here)."""


# ---------------------------------------------------------------------------
# Escaping — the security-relevant half
# ---------------------------------------------------------------------------


def esc(value: str) -> str:
    """Make ``value`` safe to interpolate into a unit-file directive.

    Two hazards, both silent: a newline (or any control char) ends the directive
    and starts a forged one, and ``%`` introduces a systemd *specifier* (``%h``
    expands to the home dir), so an unescaped ``%`` in a path silently rewrites it.
    """
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ServiceError(f"control character in unit value: {value!r}")
    return value.replace("%", "%%")


def quote_arg(value: str) -> str:
    """Quote one ExecStart argument. systemd applies shell-ish quoting to the
    command line, so a path with a space would otherwise split into two args."""
    return '"' + esc(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def under_tmp(path: Path) -> bool:
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


def env_lines(environment: dict[str, str]) -> str:
    """Render ``Environment=`` directives, one per assignment.

    Each assignment is quoted as a whole. systemd unquotes an ``Environment=``
    line into a *list* of assignments split on whitespace, so an unquoted value
    with a space (a config at ``~/my dispatch/config.toml``) silently truncates
    to the first word and appends junk — the daemon then resolves a different
    relay than the CLI just did.
    """
    return "\n".join(f"Environment={quote_arg(f'{k}={v}')}" for k, v in environment.items())


def read_write_paths(paths: list[Path]) -> str:
    """Render ``ReadWritePaths=`` for the trees a daemon writes, de-duplicated.

    ``-`` makes a missing path non-fatal: daemons here deliberately support
    starting *before* the relay exists (they wait for one), and without the
    prefix some systemd versions fail namespace setup and crash-loop the unit
    into its start limit before Python ever runs.
    """
    return "\n".join(f"ReadWritePaths=-{esc(str(p))}" for p in dict.fromkeys(paths))


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(base)) / "systemd" / "user"


def unit_path(unit_name: str) -> Path:
    return unit_dir() / unit_name


def systemctl_available() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()


def systemctl(
    *args: str, check: bool = False, timeout: float = 30.0
) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 B607 - fixed binary, fixed literal args
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def install(
    unit_name: str,
    unit_text: str,
    *,
    enable: bool = True,
    dry_run: bool = False,
    no_systemd_hint: str = "",
) -> list[str]:
    """Write the unit and (by default) enable + (re)start it. Idempotent: running
    it again is exactly how you *upgrade* an existing install — the unit is
    rewritten from current config and the daemon restarted onto it."""
    path = unit_path(unit_name)
    if not systemctl_available():
        # Checked before the dry-run branch too: a dry run exists to tell you what
        # WILL happen, and reporting a plan that can't run says the opposite.
        tail = f":  {no_systemd_hint}" if no_systemd_hint else "."
        raise ServiceError(
            "no systemd user session here. Run the daemon under whatever supervisor "
            f"you do have (launchd, supervisord, tmux){tail}"
        )
    steps = [f"write {path}"]
    if dry_run:
        return steps + (
            ["systemctl --user daemon-reload", f"enable --now {unit_name}"] if enable else []
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    # Narrow the mode BEFORE any content lands. `--env` can carry an auth token, and
    # writing first at the ambient umask (0644 in a 0755 dir) then chmod'ing leaves a
    # window where any local process can read it.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)  # existing unit from an earlier, laxer install
    path.write_text(unit_text)
    systemctl("daemon-reload", check=True)
    steps.append("systemctl --user daemon-reload")
    if enable:
        systemctl("enable", unit_name, check=True)
        # Clear a latched start limit FIRST. Once a unit trips StartLimitBurst it
        # stays `failed` and every restart returns "Start request repeated too
        # quickly" — so re-installing, which is both the upgrade path and the
        # obvious thing to try when the service is crash-looping, would fail
        # exactly when it's needed most. No-op on a healthy unit.
        systemctl("reset-failed", unit_name)
        # restart, not start: an upgrade must land the running process on the new unit.
        systemctl("restart", unit_name, check=True)
        steps += [f"systemctl --user enable {unit_name}", f"systemctl --user restart {unit_name}"]
    return steps


def uninstall(unit_name: str, *, dry_run: bool = False) -> list[str]:
    path = unit_path(unit_name)
    steps = [f"systemctl --user disable --now {unit_name}", f"remove {path}"]
    if dry_run:
        return steps
    if systemctl_available():
        systemctl("disable", "--now", unit_name)
    path.unlink(missing_ok=True)
    if systemctl_available():
        systemctl("daemon-reload")
    return steps


def status_lines(unit_name: str, install_hint: str) -> list[str]:
    """Short human-readable service status for a `… status` subcommand."""
    path = unit_path(unit_name)
    if not path.exists():
        return [f"service unit:     (not installed — `{install_hint}`)"]
    if not systemctl_available():
        return [f"service unit:     {path} (no systemd session to query)"]
    # `status` is the command the README sends you to when comms are broken, so a
    # wedged user manager must not take the relay report down with it.
    try:
        active = systemctl("is-active", unit_name, timeout=5).stdout.strip() or "unknown"
        enabled = systemctl("is-enabled", unit_name, timeout=5).stdout.strip() or "unknown"
    except (subprocess.TimeoutExpired, OSError):
        return [f"service unit:     {path}", "service state:    (systemctl did not respond)"]
    return [f"service unit:     {path}", f"service state:    {active} / {enabled} at login"]
