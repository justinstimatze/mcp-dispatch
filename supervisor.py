"""Start an agent's runtime when mail is waiting and nobody is home.

Durable identity made an offline teammate *addressable*: a DM to `publicai` waits
in `{relay}/publicai/` and the nick's next session inherits it. But nothing ever
started that next session, so "durable" meant "waits until a human happens to open
a window". This module closes that gap — the piece the README calls out by name:

    This is *identity*, not *lifecycle*: nothing here starts an agent.

The trigger rule is deliberately not invented here. It is exactly
``server._inherit_orphan_inbox``'s source set, read-only: the nick's own inbox
plus any dead session inbox of that nick, same uid, pending and unexpired. So the
supervisor wakes a nick precisely when there is mail its next session *would
inherit*, and the two can't drift into "started for nothing" or "mail nobody will
ever see". A successful start empties those inboxes (the new session claims the
files by rename), which edge-triggers the next decision for free.

Security — the whole reason this file is careful
------------------------------------------------
An inbound message causing a process to run is remote-triggered execution, and
messages arrive from other agents on this host *and*, over the git bus, from
other machines. So the sender never influences *what* runs:

* **Allowlist only.** A nick with no ``[supervisor.agents.<nick>]`` block is
  never started, whatever it is sent. There is no wildcard and no default entry.
* **The command is operator-written argv.** A list, never a shell string, spawned
  with ``shell=False``. Nothing from a message reaches argv, cwd, or env — the
  supervisor reads messages only to count them, and a count cannot carry a
  payload.
* **Absolute program path required**, so a service's minimal ``PATH`` cannot
  decide which binary "claude" means.
* **Rate limits are the backstop, not the liveness check.** A cooldown, a
  consecutive-failure breaker, a starts-per-hour ceiling and a concurrency cap
  all apply regardless of how much mail arrives, so a message flood — or a
  runtime that starts, crashes, and leaves its mail unread — costs a bounded
  number of spawns rather than an unbounded one.

Off unless ``[supervisor] enabled = true``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess  # nosec B404 - spawns the operator's allowlisted argv, never a shell
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import dispatch_fs

# The window the starts-per-hour ceiling is measured over. Fixed rather than
# configurable: it exists to bound a runaway, and a knob that can be widened to
# a day is a knob that will be.
RATE_WINDOW_SECONDS = 3600.0

# Annotated Any so the int-typed knobs below can take their defaults from the same
# dict the validator reads — one source per default, rather than a literal here and
# a literal there that drift apart.
DEFAULTS: dict[str, Any] = {
    "interval": 5.0,
    "cooldown": 60.0,
    "start_timeout": 120.0,
    "max_failures": 5,
    "max_starts_per_hour": 12,
    "max_concurrent_starts": 2,
}

# Cap on a per-nick spawn log before it is rotated to `<nick>.log.1`. A runtime
# that crash-loops writes its whole traceback every time, and an unbounded log on
# a daemon that runs for months is a disk-full waiting to happen.
LOG_MAX_BYTES = 1 << 20

# Expanded here, once. A `~` that survives into a Path is a directory literally
# named "~" created in whatever the daemon's cwd happens to be.
DEFAULT_LOG_DIR = "~/.cache/mcp-dispatch/supervisor"


class ConfigError(ValueError):
    """The [supervisor] config is unusable. Names the key and the fix."""


@dataclass(frozen=True)
class AgentSpec:
    """One allowlist entry: the nick, and exactly how to start it."""

    nick: str
    command: tuple[str, ...]
    cwd: str = ""
    env: tuple[tuple[str, str], ...] = ()

    def env_map(self) -> dict[str, str]:
        return dict(self.env)


@dataclass
class SupervisorConfig:
    enabled: bool = False
    interval: float = DEFAULTS["interval"]
    cooldown: float = DEFAULTS["cooldown"]
    start_timeout: float = DEFAULTS["start_timeout"]
    max_failures: int = DEFAULTS["max_failures"]
    max_starts_per_hour: int = DEFAULTS["max_starts_per_hour"]
    max_concurrent_starts: int = DEFAULTS["max_concurrent_starts"]
    log_dir: Path = field(default_factory=lambda: Path(os.path.expanduser(DEFAULT_LOG_DIR)))
    agents: dict[str, AgentSpec] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _num(section: dict, key: str, cast) -> float | int:
    raw = section.get(key, DEFAULTS[key])
    try:
        val = cast(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"[supervisor] {key} must be a number, got {raw!r}") from None
    if val <= 0:
        raise ConfigError(f"[supervisor] {key} must be > 0, got {val!r}")
    return val


def _spec(nick: str, raw: object) -> AgentSpec:
    """Validate one ``[supervisor.agents.<nick>]`` block into an AgentSpec.

    Strict on purpose: this is the allowlist, so a malformed entry must fail
    loudly at load rather than silently become an entry that never fires (or,
    worse, one that fires something unintended).
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"[supervisor.agents.{nick}] must be a table, got {type(raw).__name__}")
    try:
        dispatch_fs.validate_id(nick, "supervisor nick")
    except ValueError as e:
        raise ConfigError(str(e)) from None

    # Unknown keys are an error, not a shrug. A misspelled `cdw` or a daemon-level
    # key indented into an agent block would otherwise be silently ignored, and
    # the operator would believe a constraint is in force that isn't.
    unknown = sorted(set(raw) - {"command", "cwd", "env"})
    if unknown:
        raise ConfigError(
            f"[supervisor.agents.{nick}] has unknown key(s): {', '.join(unknown)}. "
            "An agent block takes only command, cwd and env — daemon-wide settings "
            "like interval or log_dir belong under [supervisor]."
        )

    command = raw.get("command")
    if isinstance(command, str):
        raise ConfigError(
            f"[supervisor.agents.{nick}] command must be a LIST of arguments, not a string. "
            f'Write command = ["/usr/bin/foo", "--flag"], not command = "{command}". '
            "A string would need a shell to split it, and this never runs a shell."
        )
    if not isinstance(command, list) or not command:
        raise ConfigError(f"[supervisor.agents.{nick}] needs a non-empty command = [...] list")
    if not all(isinstance(a, str) for a in command):
        raise ConfigError(f"[supervisor.agents.{nick}] every command element must be a string")
    if not os.path.isabs(command[0]):
        raise ConfigError(
            f"[supervisor.agents.{nick}] command[0] must be an ABSOLUTE path, got {command[0]!r}. "
            "A user service starts with a minimal PATH, so a bare name would resolve "
            "differently for the daemon than it does in your shell."
        )

    cwd = raw.get("cwd", "")
    if not isinstance(cwd, str):
        raise ConfigError(f"[supervisor.agents.{nick}] cwd must be a string path")

    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise ConfigError(f'[supervisor.agents.{nick}] env must be a table of KEY = "value"')
    env: list[tuple[str, str]] = []
    for k, v in env_raw.items():
        if not isinstance(v, str):
            raise ConfigError(f"[supervisor.agents.{nick}] env.{k} must be a string")
        env.append((k, v))

    return AgentSpec(
        nick=nick,
        command=tuple(command),
        cwd=os.path.expanduser(cwd) if cwd else "",
        env=tuple(env),
    )


def load(cfg: dict) -> SupervisorConfig:
    """Build a SupervisorConfig from a parsed config dict. Raises ConfigError."""
    section = cfg.get("supervisor")
    if not isinstance(section, dict):
        return SupervisorConfig()

    agents_raw = section.get("agents", {})
    if not isinstance(agents_raw, dict):
        raise ConfigError("[supervisor.agents] must be a table keyed by nick")
    agents = {nick: _spec(nick, raw) for nick, raw in agents_raw.items()}

    log_dir = section.get("log_dir") or DEFAULT_LOG_DIR
    if not isinstance(log_dir, str):
        raise ConfigError("[supervisor] log_dir must be a string path")

    return SupervisorConfig(
        enabled=bool(section.get("enabled", False)),
        interval=float(_num(section, "interval", float)),
        cooldown=float(_num(section, "cooldown", float)),
        start_timeout=float(_num(section, "start_timeout", float)),
        max_failures=int(_num(section, "max_failures", int)),
        max_starts_per_hour=int(_num(section, "max_starts_per_hour", int)),
        max_concurrent_starts=int(_num(section, "max_concurrent_starts", int)),
        log_dir=Path(os.path.expanduser(log_dir)),
        agents=agents,
    )


def check_spec(spec: AgentSpec) -> list[str]:
    """Problems that would make this entry fail at spawn time. Empty == fine.

    Separate from ``_spec`` because these are facts about the *filesystem right
    now*, not about the config text: a binary can be deleted after a valid config
    is written, and the daemon must not refuse to start every other agent over it.
    """
    problems = []
    prog = Path(spec.command[0])
    if not prog.exists():
        problems.append(f"command[0] does not exist: {prog}")
    elif not os.access(prog, os.X_OK):
        problems.append(f"command[0] is not executable: {prog}")
    if spec.cwd and not Path(spec.cwd).is_dir():
        problems.append(f"cwd is not a directory: {spec.cwd}")
    return problems


def config_permission_warning(config_path: Path) -> str:
    """Warn if the config — now an execution allowlist — is writable by others.

    Before this feature the config only decided where messages went. It now
    decides what runs, so group/world write on it is a local privilege-escalation
    path: anyone who can edit it can name a command and then send mail to trigger it.
    """
    try:
        mode = config_path.stat().st_mode
    except OSError:
        return ""
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        return (
            f"{config_path} is writable by group/other (mode {oct(stat.S_IMODE(mode))}). "
            "It is now an execution allowlist — chmod 600 it."
        )
    return ""


# ---------------------------------------------------------------------------
# Reading the relay: what mail is waiting, and who is home
# ---------------------------------------------------------------------------


def inherit_sources(dispatch_dir: Path, nick: str) -> list[Path]:
    """Inboxes whose pending mail this nick's next session would adopt.

    Mirrors ``server._inherit_orphan_inbox``: the nick's own inbox, plus any
    ``<nick>-<pid>`` session inbox with no live presence, skipping directories
    owned by another uid (in group_mode those are another account's mail, which
    our agent could not inherit either).
    """
    out: list[Path] = []
    if not dispatch_dir.is_dir():
        return out
    for d in sorted(dispatch_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name != nick and dispatch_fs.durable_nick(d.name) != nick:
            continue
        if d.name != nick:
            pf = dispatch_dir / ".presence" / f"{d.name}.json"
            if pf.exists() and dispatch_fs.presence_is_live(pf):
                continue  # a live session of this nick owns that inbox
        try:
            if d.stat().st_uid != os.getuid():
                continue
        except OSError:
            continue
        out.append(d)
    return out


def waiting_mail(dispatch_dir: Path, nick: str) -> int:
    """Count of pending, unexpired messages waiting for ``nick``.

    Reads message JSON *only* to test state and TTL. No field of any message
    influences whether or what the supervisor spawns — see the module docstring.
    """
    total = 0
    for d in inherit_sources(dispatch_dir, nick):
        for f in sorted(d.glob("*.json")):
            try:
                msg = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("state", "pending") != "pending" or dispatch_fs.is_expired(msg):
                continue
            total += 1
    return total


def is_live(dispatch_dir: Path, nick: str) -> bool:
    """True if any live session's durable nick is ``nick``."""
    return nick in dispatch_fs.live_nicks(dispatch_dir)


# ---------------------------------------------------------------------------
# Per-nick runtime state and the start decision
# ---------------------------------------------------------------------------


@dataclass
class NickState:
    """What the supervisor remembers about one nick between passes."""

    starts: list[float] = field(default_factory=list)  # monotonic, for the rate window
    failures: int = 0  # consecutive failed starts
    proc: subprocess.Popen | None = None  # held only to reap and to report an early exit
    awaiting_since: float = 0.0  # monotonic; >0 means "started, waiting for presence"
    parked: str = ""  # non-empty == breaker tripped, with the reason

    def last_start(self) -> float:
        return self.starts[-1] if self.starts else 0.0

    def recent_starts(self, now: float) -> int:
        return sum(1 for t in self.starts if now - t < RATE_WINDOW_SECONDS)

    def prune(self, now: float) -> None:
        self.starts = [t for t in self.starts if now - t < RATE_WINDOW_SECONDS]


@dataclass(frozen=True)
class Decision:
    """Why the supervisor did or didn't start a nick this pass."""

    nick: str
    action: str  # "start" | "skip"
    reason: str
    mail: int = 0

    def line(self) -> str:
        verb = "START" if self.action == "start" else "skip "
        return f"  {verb} {self.nick}  ({self.reason}; {self.mail} waiting)"


def decide(
    spec: AgentSpec,
    state: NickState,
    *,
    mail: int,
    live: bool,
    now: float,
    cfg: SupervisorConfig,
    concurrent: int,
) -> Decision:
    """Pure: should ``spec.nick`` be started right now, and why not if not.

    Every gate that isn't "there is mail and nobody is home" exists to bound how
    often inbound traffic can cause a spawn. Ordered cheapest-and-most-final first
    so the reported reason is the *decisive* one rather than the last one checked.
    """
    if state.parked:
        return Decision(spec.nick, "skip", f"parked: {state.parked}", mail)
    if live:
        return Decision(spec.nick, "skip", "already live", mail)
    if state.awaiting_since:
        waited = now - state.awaiting_since
        return Decision(spec.nick, "skip", f"start in flight, {waited:.0f}s in", mail)
    if mail <= 0:
        return Decision(spec.nick, "skip", "no waiting mail", mail)
    since = now - state.last_start()
    if state.starts and since < cfg.cooldown:
        return Decision(spec.nick, "skip", f"cooldown, {cfg.cooldown - since:.0f}s left", mail)
    if state.recent_starts(now) >= cfg.max_starts_per_hour:
        return Decision(
            spec.nick,
            "skip",
            f"rate limit: {cfg.max_starts_per_hour} starts in the last hour",
            mail,
        )
    if concurrent >= cfg.max_concurrent_starts:
        return Decision(spec.nick, "skip", f"{concurrent} starts already in flight", mail)
    return Decision(spec.nick, "start", f"{mail} waiting, no live session", mail)


# ---------------------------------------------------------------------------
# Spawning
# ---------------------------------------------------------------------------


def _open_log(log_dir: Path, nick: str):
    """Append-mode handle for a nick's spawn log, rotated once past the cap."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{nick}.log"
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(log_dir / f"{nick}.log.1")
        return open(path, "a")  # noqa: SIM115 - handed to the child, closed by the caller
    except OSError:
        return subprocess.DEVNULL


def spawn(spec: AgentSpec, cfg: SupervisorConfig) -> subprocess.Popen:
    """Start one agent runtime. argv-only, no shell, config-derived environment.

    ``start_new_session`` detaches the child into its own session: it must
    outlive a supervisor restart (an agent killed because its babysitter was
    upgraded is worse than no supervisor) and must not take a Ctrl-C aimed at a
    foreground ``--once`` run.

    The parent environment is inherited because a real runtime needs HOME, PATH
    and the rest to work at all; ``spec.env`` overlays it. Nothing here comes
    from a message.
    """
    env = dict(os.environ)
    env.update(spec.env_map())
    # Informational marker so a runtime can tell it was woken by mail rather than
    # launched by a human. Never read back as an instruction.
    env["MCP_DISPATCH_SUPERVISED_NICK"] = spec.nick
    log = _open_log(cfg.log_dir, spec.nick)
    try:
        return subprocess.Popen(  # nosec B603 - operator-allowlisted argv, shell=False
            list(spec.command),
            cwd=spec.cwd or None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        if log is not subprocess.DEVNULL:
            log.close()  # the child holds its own dup; ours would leak per spawn


# ---------------------------------------------------------------------------
# The daemon's one pass
# ---------------------------------------------------------------------------


class Supervisor:
    """Holds per-nick state across passes. One ``tick()`` is one full sweep."""

    def __init__(
        self,
        dispatch_dir: Path,
        cfg: SupervisorConfig,
        *,
        dry_run: bool = False,
        log=print,
    ) -> None:
        self.dispatch_dir = dispatch_dir
        self.cfg = cfg
        self.dry_run = dry_run
        self.log = log
        self.states: dict[str, NickState] = {n: NickState() for n in cfg.agents}

    # -- outcome bookkeeping ------------------------------------------------

    def _succeeded(self, spec: AgentSpec, state: NickState, waited: float) -> None:
        state.awaiting_since = 0.0
        state.failures = 0
        self.log(f"[supervise] {spec.nick} is live after {waited:.0f}s — mail will be inherited.")

    def _failed(self, spec: AgentSpec, state: NickState, why: str) -> None:
        """Count a failed start and trip the breaker once they stack up.

        A parked nick is not retried until the daemon is restarted (which is also
        when a fixed config takes effect). Retrying a start that has failed
        `max_failures` times in a row has never once been the thing that fixed it,
        and the log fills with the same traceback until someone looks.
        """
        state.awaiting_since = 0.0
        state.failures += 1
        self.log(f"[supervise] {spec.nick} start failed ({state.failures}): {why}")
        if state.failures >= self.cfg.max_failures:
            state.parked = f"{state.failures} consecutive failed starts — {why}"
            self.log(
                f"[supervise] {spec.nick} PARKED after {state.failures} failures. "
                f"Fix it and restart the supervisor; see {self.cfg.log_dir / spec.nick}.log"
            )

    def _resolve_in_flight(self, spec: AgentSpec, state: NickState, live: bool, now: float) -> None:
        """Decide the fate of a start we are still waiting on."""
        rc = state.proc.poll() if state.proc is not None else None
        waited = now - state.awaiting_since
        if live:
            # Checked before the exit code on purpose: a launcher that execs a
            # daemon and returns has *succeeded* even though our child is gone.
            self._succeeded(spec, state, waited)
        elif rc is not None:
            self._failed(
                spec, state, f"exited rc={rc} after {waited:.0f}s without claiming presence"
            )
        elif waited > self.cfg.start_timeout:
            # Deliberately does not kill it. It is still running and may be doing
            # real work; it just isn't a dispatch session. Stop waiting, let the
            # rate limit bound any further starts, and say so.
            self._failed(
                spec, state, f"still running but no presence after {self.cfg.start_timeout:.0f}s"
            )
        if rc is not None:
            state.proc = None  # reaped

    # -- the sweep ----------------------------------------------------------

    def tick(self) -> list[Decision]:
        now = time.monotonic()
        decisions: list[Decision] = []

        for nick, spec in self.cfg.agents.items():
            state = self.states[nick]
            state.prune(now)
            live = is_live(self.dispatch_dir, nick)

            if state.awaiting_since:
                self._resolve_in_flight(spec, state, live, now)
            elif state.proc is not None and state.proc.poll() is not None:
                state.proc = None  # a finished session we were only holding to reap

            # Only scan the relay when the answer can matter. A live nick needs no
            # start, and a parked one will not get one.
            mail = 0
            if not live and not state.parked and not state.awaiting_since:
                mail = waiting_mail(self.dispatch_dir, nick)

            concurrent = sum(1 for s in self.states.values() if s.awaiting_since)
            decision = decide(
                spec,
                state,
                mail=mail,
                live=live,
                now=now,
                cfg=self.cfg,
                concurrent=concurrent,
            )
            decisions.append(decision)
            if decision.action == "start":
                self._start(spec, state, decision, now)

        return decisions

    def _start(self, spec: AgentSpec, state: NickState, decision: Decision, now: float) -> None:
        if self.dry_run:
            self.log(f"[supervise] would start {spec.nick}: {' '.join(spec.command)}")
            return
        problems = check_spec(spec)
        if problems:
            self._failed(spec, state, "; ".join(problems))
            return
        self.log(
            f"[supervise] starting {spec.nick} — {decision.mail} message(s) waiting, "
            f"no live session. exec: {' '.join(spec.command)}"
        )
        try:
            state.proc = spawn(spec, self.cfg)
        except OSError as e:
            self._failed(spec, state, f"spawn failed: {e}")
            return
        state.starts.append(now)
        state.awaiting_since = now
