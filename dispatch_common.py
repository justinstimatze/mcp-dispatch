"""Shared, side-effect-free plumbing for the dispatch hooks *and* bin/ scripts.

Config resolution (top-level-over-``[dispatch]``-table), the dispatch/state dir
lookups, the md5 lock-key, and the flock probe/acquire primitives were each
copy-pasted across ``hooks/dispatch-arm.py``, ``hooks/dispatch-gitsync-arm.py``,
``bin/dispatch-wait`` and ``bin/dispatch-gitsync`` — four near-copies that had
already drifted once (gitsync-arm silently ignored ``[dispatch].auto_arm``). This
module is the single source all four import, so they can't diverge again. It
lives at the repo root beside the other shared modules (``notify_policy``,
``dispatch_fs``) the bin scripts already import. Stdlib only; safe to import from
a standalone hook.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
from pathlib import Path


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
