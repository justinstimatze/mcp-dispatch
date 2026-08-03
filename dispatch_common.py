"""Shared, side-effect-free plumbing for the dispatch hooks *and* bin/ scripts.

Config resolution (top-level-over-``[dispatch]``-table), the dispatch/state dir
lookups, the md5 lock-key, and the flock probe/acquire primitives were each
copy-pasted across ``hooks/dispatch-arm.py``, ``hooks/dispatch-gitsync-arm.py``,
``bin/dispatch-wait`` and ``bin/dispatch-gitsync`` — four near-copies that had
already drifted once (gitsync-arm silently ignored ``[dispatch].auto_arm``). This
module is the single source all four import, so they can't diverge again. It
lives at the repo root beside the other shared modules (``notify_policy``,
``dispatch_fs``) the bin scripts already import.

Stdlib plus ``dispatch_fs``, which is itself stdlib-only. Identity resolution
needs the nick rule, and the alternative — every caller re-deriving it — is the
drift this module exists to end: ``bin/dispatch-wait`` had its own regex copy
that no longer matched the launcher's.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex

# Runs only the operator's own notify_command, as an argv list with shell=False.
import subprocess  # nosec B404
import time
from pathlib import Path

import dispatch_fs


def truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> dict:
    """Raw parse of the config TOML (empty dict if absent/unreadable)."""
    cfg = os.environ.get("MCP_DISPATCH_CONFIG") or os.path.expanduser(
        "~/.config/mcp-dispatch/config.toml"
    )
    if not os.path.exists(cfg):
        return {}
    try:
        import tomllib

        with open(cfg, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def flat(cfg: dict, key: str):
    """A top-level key wins over the same key in a ``[dispatch]`` table — the repo
    convention shared by dispatch-wait / dispatch-peek."""
    val = cfg.get(key)
    if val is not None:
        return val
    sub = cfg.get("dispatch")
    return sub.get(key) if isinstance(sub, dict) else None


def dispatch_dir(cfg: dict) -> Path:
    raw = (
        os.environ.get("MCP_DISPATCH_DIR")
        or os.environ.get("DISPATCH_DIR")
        or flat(cfg, "dispatch_dir")
        or "~/.config/mcp-dispatch/messages"
    )
    return Path(os.path.expanduser(str(raw)))


def state_dir() -> Path:
    raw = os.environ.get("MCP_DISPATCH_STATE_DIR") or "~/.cache/mcp-dispatch"
    return Path(os.path.expanduser(raw))


def auto_arm_disabled(cfg: dict) -> bool:
    """True if auto-arm is opted out — via ``MCP_DISPATCH_NO_AUTO_ARM`` or
    ``auto_arm = false`` at either the top level OR under ``[dispatch]`` (both
    hooks now honor both, which fixes the historical gitsync-arm drift)."""
    if truthy(os.environ.get("MCP_DISPATCH_NO_AUTO_ARM")):
        return True
    return flat(cfg, "auto_arm") is False


def md5_key(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:8]


def notify(summary: str, body: str = "", cfg: dict | None = None) -> bool:
    """Fire the operator's ``notify_command``, if they set one. False if not.

    Argv list, never a shell, and ``--`` stops a body beginning with a hyphen
    from being read as options. Best-effort by design: a desktop that isn't
    there must never be able to fail a caller doing real work.

    (``server.py`` keeps its own call — it notifies per message from a background
    thread against a constant resolved at import, and re-reading config on that
    path would buy nothing.)
    """
    conf = load_config() if cfg is None else cfg
    cmd = str(flat(conf, "notify_command") or "").strip()
    if not cmd:
        return False
    try:
        subprocess.run(  # nosec B603 - argv from local trusted config, shell=False
            [*shlex.split(cmd), "--", summary, body],
            timeout=5,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return True


def process_chain(pid: int | None = None, depth: int = 8) -> list[int]:
    """A process's ancestry, nearest parent first.

    Two sessions opened in the same project directory derive the same nick and
    are indistinguishable by name, by directory, and by anything else in the
    presence record — but they hang off different `claude` processes, and a hook
    running inside one of them hangs off the same one. That shared ancestor is
    the only thing on the box that says which session a hook belongs to.

    Returns [] where /proc is absent or unreadable, so every caller has to treat
    ancestry as a tie-breaker it may not get rather than a fact it can rely on.
    """
    out: list[int] = []
    cur = os.getpid() if pid is None else pid
    for _ in range(depth):
        try:
            stat = Path(f"/proc/{cur}/stat").read_text()
        except OSError:
            break
        # comm sits in parens and may contain spaces and parens of its own, so
        # split after the LAST ')' — the field-counting version reads a `tmux:
        # server` process's ancestry as garbage.
        fields = stat.rpartition(")")[2].split()
        if len(fields) < 2:
            break
        try:
            cur = int(fields[1])  # ppid
        except ValueError:
            break
        if cur <= 1:
            break
        out.append(cur)
    return out


def pick_by_ancestry(candidates: dict[str, list[int]], mine: list[int] | None = None) -> str | None:
    """Of several equally-named sessions, the one sharing our nearest ancestor.

    Unrelated sessions still meet at the terminal, the desktop shell and init,
    so proximity is what carries the answer: the sibling under our own `claude`
    intersects our chain far sooner than a stranger under someone else's. Ties
    and empty chains return None — declining to guess, because arming the wrong
    session is worse than arming none.
    """
    ours = process_chain() if mine is None else mine
    if not ours:
        return None
    rank = {pid: i for i, pid in enumerate(ours)}
    scored: list[tuple[int, str]] = []
    for aid, chain in candidates.items():
        near = [rank[p] for p in chain or [] if p in rank]
        if near:
            scored.append((min(near), aid))
    if not scored:
        return None
    scored.sort()
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None  # equidistant: no evidence, so no answer
    return scored[0][1]


def resolve_agent_id(dispatch_dir: Path, cwd: str) -> str | None:
    """Which live session belongs to the process asking.

    ``MCP_DISPATCH_AGENT_ID`` when set; otherwise the live presence records whose
    id starts with the nick this directory produces, narrowed by ancestry when
    more than one matches. None means genuinely undecidable — a caller that arms,
    peeks or waits on a guess would attach itself to a stranger's session.

    Both the arm hook and the waiter used to carry their own copy of this, and
    the copies were where the bugs were: one re-derived the nick with a regex
    that drifted from the launcher's, and both dropped every candidate the
    moment a project had two windows open.
    """
    explicit = (os.environ.get("MCP_DISPATCH_AGENT_ID") or "").strip().lower()
    if explicit:
        return explicit

    prefix = dispatch_fs.nick_for_dir(cwd)
    if not prefix:
        return None
    matches: dict[str, list[int]] = {}
    for pf in (dispatch_dir / ".presence").glob("*.json"):
        try:
            data = json.loads(pf.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        aid = str(data.get("agent_id", ""))
        if not (aid.startswith(f"{prefix}-") and flock_held(pf)):
            continue
        # Walked now rather than read from a stored field. The record's pid is
        # the server process, and the held flock is what proves it is still that
        # process — so the ancestry is current, needs nothing written at startup,
        # and answers for sessions that were already running when this shipped.
        try:
            matches[aid] = process_chain(int(data.get("pid")))
        except (TypeError, ValueError):
            matches[aid] = []
    if len(matches) == 1:
        return next(iter(matches))
    # Two windows on one project is ordinary and it used to disarm both: the
    # prefix matched twice, this gave up, and neither session could ever be told
    # it was deaf. They hang off different `claude` processes, and so does the
    # hook or waiter asking — that is the tie-break.
    return pick_by_ancestry(matches)


def arm_lock(agent_id: str, state: Path | None = None) -> Path:
    """The lock a live ``dispatch-wait --follow`` holds while a session is armed.

    ``state`` overrides the local cache directory, for a reader asking about a
    session that is not its own: the lock lives under the *watcher's* HOME, so
    resolving it against ours would answer about a file that was never going to
    exist there. Sessions record their own state directory in the presence file.
    """
    return (state or state_dir()) / f"wait-{md5_key(agent_id)}.lock"


def armed(agent_id: str, state: Path | None = None) -> bool | None:
    """Whether a watch is holding this session's arm lock — the difference
    between a session that is running and one that will hear you.

    None means unprobeable, not unarmed. Another uid's cache directory is not
    ours to read, and calling a session deaf on the strength of a permission
    error would be a confident wrong answer where "can't tell" is the true one.
    """
    lock = arm_lock(agent_id, state)
    try:
        lock.open().close()
    except FileNotFoundError:
        return False  # no watch has ever armed this id
    except OSError:
        return None  # someone else's cache, or otherwise not ours to read
    return flock_held(lock)


def armed_for(rec: dict, presence_file: Path) -> bool | None:
    """``armed()`` for a presence record — the reader's side of the same fact.

    A session with no watch still receives mail; nothing wakes it to read it, so
    it answers whenever its operator next types. That is a different state from
    being offline and it deserves a different word.

    Sessions publish their own state directory. Ones started before that field
    existed get our directory instead, but only when the presence file is ours to
    begin with: another uid's session resolved against our cache finds no lock
    and would be reported deaf, which is the one wrong answer worth going out of
    the way to avoid.
    """
    aid = str(rec.get("agent_id") or "")
    if not aid:
        return None
    raw = rec.get("state_dir")
    if raw:
        return armed(aid, Path(str(raw)))
    try:
        if presence_file.stat().st_uid != os.getuid():
            return None
    except OSError:
        return None
    return armed(aid)


def flock_held(path: Path) -> bool:
    """True if some live process holds an exclusive flock on ``path``. We probe by
    trying to take it: success (we got it) means nobody holds it — release and
    report not-held. Opened read-only, so it never *creates* the file: a missing
    file (or a stale leftover nobody holds) reads as not-held. uid-agnostic and
    pid-reuse-immune; the kernel frees the lock the instant the holder dies."""
    try:
        fh = open(path)
    except OSError:
        return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def acquire_flock(path: Path, *, attempts: int = 1, backoff: float = 0.05):
    """Take an exclusive flock on ``path`` and RETURN the held handle (kept open so
    the lock lives for the caller's lifetime; the kernel releases it on exit/death).
    Returns ``None`` if the lock is already held elsewhere or the dir is unwritable.

    Single-instance guard for the waiter and the git daemon. Unlike ``flock_held``
    (a probe that immediately releases), this holds. ``attempts>1`` retries with a
    short ``backoff`` between tries — the arm hook probes this same lock to test
    liveness, and that momentary hold can collide with a just-starting holder, so
    one retry keeps a probe from masquerading as a rival. Opened ``a+`` so it does
    create the lock file (that's the point — the file's existence anchors the lock)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")  # noqa: SIM115 - held for the caller's process lifetime
    except OSError:
        return None
    for attempt in range(max(1, attempts)):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if attempt < attempts - 1:
                time.sleep(backoff)
    fh.close()
    return None
