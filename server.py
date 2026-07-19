#!/usr/bin/env python3
"""MCP Dispatch — Local inter-agent messaging for Claude Code instances.

Each Claude Code window runs its own dispatch MCP server (stdio transport).
They share a filesystem directory as a message relay.

Agent identity:
  Set MCP_DISPATCH_AGENT_ID=alpha in the shell before launching Claude Code
  to pin a stable identity.
  Without it, IDs are auto-claimed from the configured pool in startup order.

Configuration:
  Config file: ~/.config/mcp-dispatch/config.toml
  Override path: MCP_DISPATCH_CONFIG=/path/to/config.toml

  See _DEFAULT_CONFIG for all available settings.

Tools:
  dispatch(message, target, ...)  — send to one agent or all
  peek(...)                       — read inbox + delivery receipts for sent messages
  ack(message_ids)                — acknowledge and delete processed messages
  who()                           — list connected agents
"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import shlex
import signal
import stat

# subprocess only runs the opt-in, local-config notify_command (no shell).
import subprocess  # nosec B404
import sys
import threading
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import dispatch_fs
from notify_policy import should_notify

# tomllib is stdlib in 3.11+
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

# Optional: filesystem watcher for real-time stderr alerts
try:
    from watchdog.events import FileCreatedEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "agents": [],  # empty = dynamic registration (any name accepted)
    "dispatch_dir": "~/.config/mcp-dispatch/messages",
    "max_message_bytes": 65536,
    "default_ttl": 604800,  # seconds; 1-week ambient default (0 = no expiry; must_read overrides)
    "instructions": "",  # empty = use built-in template
    # Owner-only by default. Set true to share one relay across mutually-trusting
    # accounts in a common group: the relay dir must be group-owned + setgid, and
    # files/dirs become group-readable/writable (0660 / 2770) instead of 0600/0700.
    "group_mode": False,
    # Optional command run when a message arrives, so a PARKED session (model
    # idle, taking no turns) still surfaces incoming mail. The server process is
    # alive the whole session, so a background poll can fire this even while the
    # model sleeps. Empty = off. No Python deps — it just shells out, e.g.
    # "notify-send" on GNOME. The summary and body are appended as two args.
    "notify_command": "",
    # Which messages notify. must_read always pierces (except "none"):
    #   "none"      — never
    #   "direct"    — messages addressed to this agent (to == my id)
    #   "important" — urgent priority (this default also fires on must_read)
    #   "all"       — everything
    "notify_on": "important",
}


def _load_config() -> dict:
    """Load configuration from TOML file with env var overrides.

    Resolution order (highest priority first):
    1. Environment variables
    2. Config file
    3. Built-in defaults
    """
    config = dict(_DEFAULT_CONFIG)

    # Find config file
    config_path = os.environ.get(
        "MCP_DISPATCH_CONFIG",
        os.path.expanduser("~/.config/mcp-dispatch/config.toml"),
    )

    if os.path.exists(config_path):
        try:
            with open(config_path, "rb") as f:
                file_config = tomllib.load(f)
            # Flatten: support both top-level keys and [dispatch] section
            if "dispatch" in file_config and isinstance(file_config["dispatch"], dict):
                file_config = {**file_config, **file_config.pop("dispatch")}
            for key in _DEFAULT_CONFIG:
                if key in file_config:
                    config[key] = file_config[key]
            print(f"[dispatch] Config loaded: {config_path}", file=sys.stderr)
        except Exception as e:
            print(f"[dispatch] Config error ({config_path}): {e}", file=sys.stderr)
    else:
        print("[dispatch] No config file found, using defaults", file=sys.stderr)

    # Env var overrides
    if os.environ.get("MCP_DISPATCH_DIR"):
        config["dispatch_dir"] = os.environ["MCP_DISPATCH_DIR"]
    elif os.environ.get("DISPATCH_DIR"):
        config["dispatch_dir"] = os.environ["DISPATCH_DIR"]  # legacy fallback

    # Expand ~ in dispatch_dir
    config["dispatch_dir"] = os.path.expanduser(str(config["dispatch_dir"]))

    return config


CONFIG = _load_config()
AGENT_IDS: list[str] = CONFIG["agents"]
DISPATCH_DIR = Path(CONFIG["dispatch_dir"])
MAX_MESSAGE_BYTES = int(CONFIG["max_message_bytes"])
DEFAULT_TTL = int(CONFIG["default_ttl"])
DYNAMIC_MODE = len(AGENT_IDS) == 0  # no roster = accept any agent name
NOTIFY_COMMAND = str(CONFIG["notify_command"]).strip()
NOTIFY_ON = str(CONFIG["notify_on"]).strip().lower()
NOTIFY_POLL_SECONDS = 4
GROUP_MODE = bool(CONFIG["group_mode"])
# Directory mode: setgid + group-rwx when sharing, else owner-only. The setgid
# bit makes inboxes created by any participant inherit the relay's group.
DIR_MODE = 0o2770 if GROUP_MODE else 0o700

# Set the umask before any file is created so message files land group-readable
# (0660) in shared mode or owner-only (0600) otherwise, whatever the inherited umask.
os.umask(0o007 if GROUP_MODE else 0o077)


# ---------------------------------------------------------------------------
# Agent ID management
# ---------------------------------------------------------------------------


# Agent ids and targets become path segments under DISPATCH_DIR, so they must
# never contain separators or traversal sequences. Constrain to a safe charset.
# \Z (not $) anchors the absolute end — $ would also match before a trailing newline.
# The id contract and these fs primitives live in dispatch_fs so the git
# replicator daemon (git_bridge.py) reuses the exact same logic without importing
# this module (whose load claims an id and starts threads). One source of truth
# for the on-disk format — drift would silently corrupt cross-host delivery.
_ID_RE = dispatch_fs.ID_RE
_validate_id = dispatch_fs.validate_id


def _enforce_dir_mode(path: Path) -> None:
    """chmod a dir to DIR_MODE. In shared mode the relay may be owned by another
    participant (already set up correctly), so tolerate not being the owner — but
    only if it is actually usable and group-shared, else fail loud."""
    try:
        os.chmod(path, DIR_MODE)
    except PermissionError:
        if not GROUP_MODE:
            raise
        # We don't own it; that's fine only if it's genuinely set up for sharing.
        st = path.stat()
        usable = os.access(path, os.R_OK | os.W_OK | os.X_OK)
        shared = bool(st.st_mode & stat.S_ISGID) and (st.st_mode & 0o070) == 0o070
        if not (usable and shared):
            raise RuntimeError(
                f"group_mode relay {path} is owned by another user and not "
                f"correctly shared (need setgid + group rwx, and you must be in "
                f"its group). Current mode {oct(stat.S_IMODE(st.st_mode))}."
            ) from None


def _setup_dirs() -> None:
    DISPATCH_DIR.mkdir(parents=True, exist_ok=True)
    presence = DISPATCH_DIR / ".presence"
    presence.mkdir(exist_ok=True)
    # Explicit chmod in case the dirs predate this server's umask.
    _enforce_dir_mode(DISPATCH_DIR)
    _enforce_dir_mode(presence)
    for aid in AGENT_IDS:
        (DISPATCH_DIR / aid).mkdir(exist_ok=True)


_last_tmp_sweep = 0.0


def _sweep_stale_tmp(max_age_s: int = 60) -> None:
    """Unlink orphaned *.tmp files left by writers that crashed mid-rename."""
    cutoff = time.time() - max_age_s
    for tmp in DISPATCH_DIR.glob("*/*.tmp"):
        try:
            if tmp.stat().st_mtime < cutoff:
                tmp.unlink()
        except OSError:
            pass


def _maybe_sweep_stale_tmp() -> None:
    """Run the .tmp sweep at most once a minute, off the back of inbox reads, so a
    peer that crashes mid-write during a session is cleaned up before next start."""
    global _last_tmp_sweep
    now = time.time()
    if now - _last_tmp_sweep > 60:
        _last_tmp_sweep = now
        _sweep_stale_tmp()


# Held for the process lifetime so the flock on the presence file stays
# acquired. Closing or GC'ing this handle would release the lock.
_PRESENCE_HANDLE = None
# Live in-memory copy of this agent's presence record (incl. channel subs),
# written through the locked handle on every change.
_PRESENCE_DATA: dict = {}


def _initial_channels() -> list[str]:
    """Channels to auto-subscribe on startup, from MCP_DISPATCH_CHANNELS.

    Comma- or space-separated; a leading '#' is optional. Invalid ids are
    skipped with a warning rather than aborting startup. Lets a session rejoin
    standing rooms (e.g. an ops channel) on every restart without a manual
    subscribe() each time — the durable complement to ephemeral, presence-based
    subscriptions.
    """
    out: list[str] = []
    for tok in os.environ.get("MCP_DISPATCH_CHANNELS", "").replace(",", " ").split():
        name = tok.lstrip("#")
        if _ID_RE.match(name):
            if name not in out:
                out.append(name)
        else:
            print(
                f"[dispatch] ignoring invalid channel {tok!r} in MCP_DISPATCH_CHANNELS",
                file=sys.stderr,
            )
    return sorted(out)


def _write_presence() -> None:
    """Persist _PRESENCE_DATA through the held, locked handle.

    The handle keeps the flock that gates identity, so we can't atomically
    replace the file (rename would orphan the lock on a new inode). Serialize
    before truncating to keep the truncate→write empty-file window as small as
    possible for concurrent readers.
    """
    if _PRESENCE_HANDLE is None:
        return
    payload = json.dumps(_PRESENCE_DATA)
    _PRESENCE_HANDLE.seek(0)
    _PRESENCE_HANDLE.truncate()
    _PRESENCE_HANDLE.write(payload)
    _PRESENCE_HANDLE.flush()
    os.fsync(_PRESENCE_HANDLE.fileno())


def _try_lock_presence(pf: Path, agent_id: str) -> bool:
    """Atomically claim a presence file via an exclusive, non-blocking flock.

    Returns True and records the handle on success; False if another live
    process holds the identity. The lock — not a pid heuristic — is the source
    of truth, so a crashed process's lock is released by the kernel for free.
    """
    global _PRESENCE_HANDLE, _PRESENCE_DATA
    fh = open(pf, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _PRESENCE_HANDLE = fh
    _PRESENCE_DATA = {
        "agent_id": agent_id,
        "pid": os.getpid(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels": _initial_channels(),
    }
    _write_presence()
    return True


def _claim_id() -> str:
    """Claim an agent ID.

    If MCP_DISPATCH_AGENT_ID is set, use that directly.
    Otherwise auto-claim the first available slot from the configured pool.
    In dynamic mode, the env var is required.
    """
    presence_dir = DISPATCH_DIR / ".presence"

    # Explicit identity via env var
    explicit = os.environ.get("MCP_DISPATCH_AGENT_ID", "").strip().lower()

    if explicit:
        _validate_id(explicit)
        if AGENT_IDS and explicit not in AGENT_IDS:
            raise ValueError(
                f"Agent ID '{explicit}' is not in the configured roster. "
                f"Valid IDs: {', '.join(AGENT_IDS)}"
            )
        # In dynamic mode, create inbox dir on demand
        (DISPATCH_DIR / explicit).mkdir(exist_ok=True)

        if not _try_lock_presence(presence_dir / f"{explicit}.json", explicit):
            raise RuntimeError(
                f"Agent ID '{explicit}' is already held by a live process. "
                "Stop that instance or choose a different MCP_DISPATCH_AGENT_ID."
            )
        return explicit

    if DYNAMIC_MODE:
        raise RuntimeError(
            "No agent ID specified. In dynamic mode (no agents roster), "
            "set MCP_DISPATCH_AGENT_ID in your environment."
        )

    # Auto-claim: first slot whose presence lock we can acquire.
    for aid in AGENT_IDS:
        if _try_lock_presence(presence_dir / f"{aid}.json", aid):
            return aid

    raise RuntimeError(
        f"All {len(AGENT_IDS)} agent slots are claimed by live processes. "
        "Stop an existing instance first."
    )


def _release_id(agent_id: str) -> None:
    global _PRESENCE_HANDLE
    # Just drop the lock; don't unlink. A lingering presence file with no lock is
    # already "dead" to who()/channels, the next claimer of this id reclaims it
    # (truncate + rewrite), and _reap_dead_presence GCs it at startup. Unlinking
    # here would race a process concurrently reclaiming the same id.
    if _PRESENCE_HANDLE is not None:
        try:
            _PRESENCE_HANDLE.close()  # releases the flock
        except OSError:
            pass
        _PRESENCE_HANDLE = None


_presence_is_live = dispatch_fs.presence_is_live


def _live_presence_files() -> list[Path]:
    """Presence files whose owner is currently live."""
    return dispatch_fs.live_presence_files(DISPATCH_DIR)


def _live_agents() -> list[str]:
    """Agent ids with a live presence record (validated to be path-safe)."""
    return dispatch_fs.live_agents(DISPATCH_DIR)


def _reap_dead_presence() -> int:
    """Unlink presence files with no live owner. Returns count removed.

    Holds the lock across the unlink so a process reclaiming the same id can't
    recreate-and-lock the file underneath us mid-delete. Run at startup; who()
    and channel reads merely *filter* by liveness rather than delete, which
    avoids racing a concurrent claimant.
    """
    removed = 0
    for pf in sorted((DISPATCH_DIR / ".presence").glob("*.json")):
        try:
            fh = open(pf)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # live owner — leave it
            try:
                pf.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
    return removed


def _reap_empty_inboxes() -> int:
    """Remove completely-empty inbox dirs (recreated on demand by senders).

    A dead dynamic-mode agent (<project>-<pid>) never returns, so its empty inbox
    is pure clutter; a *live* peer's empty inbox is harmless to drop because the
    next sender re-creates it. Skipped in roster mode, where inboxes are
    pre-created on purpose.
    """
    if AGENT_IDS:
        return 0
    removed = 0
    for d in DISPATCH_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            if not any(d.iterdir()):  # truly empty — no messages, no .tmp
                d.rmdir()
                removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Message I/O
# ---------------------------------------------------------------------------


_atomic_write = dispatch_fs.atomic_write
_parse_timestamp = dispatch_fs.parse_timestamp


def _is_expired(msg: dict) -> bool:
    """Check if a message has expired based on TTL."""
    ttl = msg.get("ttl")
    if not ttl or ttl <= 0:
        return False
    if msg.get("must_read", False):
        return False
    sent_at = _parse_timestamp(msg.get("timestamp", ""))
    if sent_at <= 0:
        return False
    return time.time() > sent_at + ttl


def _cleanup_expired(agent_id: str) -> int:
    """Remove expired messages from an agent's inbox. Returns count removed."""
    _maybe_sweep_stale_tmp()
    inbox = DISPATCH_DIR / agent_id
    removed = 0
    for f in inbox.glob("*.json"):
        try:
            msg = json.loads(f.read_text())
            if _is_expired(msg):
                f.unlink()
                removed += 1
        except (json.JSONDecodeError, OSError):
            pass
    return removed


def _read_inbox(
    agent_id: str, *, state_filter: str | None = None, thread_id: str | None = None
) -> list[dict]:
    """Read messages from inbox with optional filtering. Non-destructive."""
    inbox = DISPATCH_DIR / agent_id
    messages: list[dict] = []
    for f in sorted(inbox.glob("*.json")):
        try:
            msg = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if state_filter and msg.get("state", "pending") != state_filter:
            continue
        if thread_id and msg.get("thread_id") != thread_id:
            continue

        msg["_file"] = str(f)  # internal: track file path for state updates
        messages.append(msg)

    return messages


def _mark_read(messages: list[dict]) -> None:
    """Transition messages from pending → read. Atomic write in place."""
    for msg in messages:
        if msg.get("state", "pending") != "pending":
            continue
        filepath = msg.pop("_file", None)
        if not filepath:
            continue
        msg["state"] = "read"
        msg["read_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _atomic_write(Path(filepath), {k: v for k, v in msg.items() if k != "_file"})


def _send(
    from_id: str,
    to: str,
    content: str,
    priority: str = "normal",
    thread_id: str | None = None,
    reply_to: str | None = None,
    payload: dict | None = None,
    ttl: int | None = None,
    must_read: bool = False,
) -> dict:
    """Write a message to the target's inbox. Fan-out for 'all'."""
    # ttl: None → default; 0 → explicitly never expire; >0 → seconds. Reject <0.
    if ttl is not None and ttl < 0:
        raise ValueError(f"ttl must be >= 0 (got {ttl}); use 0 or omit for no expiry.")
    effective_ttl = DEFAULT_TTL if ttl is None else ttl
    msg = {
        "id": f"msg-{uuid.uuid4().hex[:8]}",
        "from": from_id,
        "to": to,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "priority": priority,
        "content": content,
        "payload": payload,
        "thread_id": thread_id,
        "reply_to": reply_to,
        "ttl": effective_ttl if effective_ttl and effective_ttl > 0 else None,
        "must_read": must_read,
        "state": "pending",
    }

    # Enforce size limit against the bytes actually written (indent=2, matching
    # _atomic_write) plus headroom for the read_at/state fields added on read.
    msg_bytes = len(json.dumps(msg, indent=2).encode("utf-8")) + 64
    if msg_bytes > MAX_MESSAGE_BYTES:
        raise ValueError(
            f"Message too large ({msg_bytes} bytes). Maximum: {MAX_MESSAGE_BYTES} bytes."
        )

    def _filename() -> str:
        return dispatch_fs.message_filename(from_id)

    def _validate_target(target: str) -> None:
        if not DYNAMIC_MODE:
            if target not in AGENT_IDS:
                valid = ", ".join(AGENT_IDS) + ", #channel, all"
                raise ValueError(f"Unknown agent '{target}'. Valid targets: {valid}")
        else:
            # In dynamic mode any name is accepted, but it becomes a path
            # segment, so it must still be a safe single segment.
            _validate_id(target, "target")
        (DISPATCH_DIR / target).mkdir(exist_ok=True)

    def _deliver_one(target: str) -> None:
        (DISPATCH_DIR / target).mkdir(exist_ok=True)
        _atomic_write(DISPATCH_DIR / target / _filename(), dict(msg))

    if to == "all":
        # Broadcast: live agents in dynamic mode (a dead <project>-<pid> id never
        # returns, so writing to its inbox is pure waste). In roster mode keep the
        # full roster — an offline roster agent keeps its id and collects mail.
        pool = AGENT_IDS if AGENT_IDS else _live_agents()
        delivered = [aid for aid in pool if aid != from_id]
        for target in delivered:
            _deliver_one(target)
    elif to.startswith("#"):
        # Channel: only current subscribers, except the sender.
        channel = _validate_id(to[1:], "channel")
        delivered = [aid for aid in _channel_subscribers(channel) if aid != from_id]
        for target in delivered:
            _deliver_one(target)
    else:
        _validate_target(to)
        _deliver_one(to)
        delivered = [to]

    result: dict = dict(msg)
    result["delivered_to"] = delivered
    return result


def _discover_agents() -> list[str]:
    """List all known agents. From roster if configured, else from inbox dirs."""
    if AGENT_IDS:
        return list(AGENT_IDS)
    # Dynamic mode: find all directories that aren't .presence
    return [
        d.name for d in sorted(DISPATCH_DIR.iterdir()) if d.is_dir() and not d.name.startswith(".")
    ]


# ---------------------------------------------------------------------------
# Channels (presence-derived, ephemeral)
# ---------------------------------------------------------------------------


def _channel_subscribers(channel: str) -> list[str]:
    """Live agents currently subscribed to a channel, by presence record."""
    return dispatch_fs.channel_subscribers(DISPATCH_DIR, channel)


def _set_subscription(channel: str, subscribed: bool) -> list[str]:
    """Add/remove a channel from this agent's presence record. Returns the new set."""
    _validate_id(channel, "channel")
    channels = set(_PRESENCE_DATA.get("channels", []))
    if subscribed:
        channels.add(channel)
    else:
        channels.discard(channel)
    _PRESENCE_DATA["channels"] = sorted(channels)
    _write_presence()
    return _PRESENCE_DATA["channels"]


# ---------------------------------------------------------------------------
# Piggyback delivery (non-destructive)
# ---------------------------------------------------------------------------


def _get_sent_receipts(agent_id: str) -> list[dict]:
    """Check delivery state of messages sent by this agent across all inboxes."""
    receipts = []
    for agent in _discover_agents():
        if agent == agent_id:
            continue
        inbox = DISPATCH_DIR / agent
        if not inbox.is_dir():
            continue
        for f in inbox.glob("*.json"):
            try:
                msg = json.loads(f.read_text())
                if msg.get("from") == agent_id:
                    receipts.append(
                        {
                            "id": msg["id"],
                            "to": agent,
                            "state": msg.get("state", "pending"),
                            "read_at": msg.get("read_at"),
                            "sent_at": msg.get("timestamp"),
                            "preview": msg.get("content", "")[:60],
                        }
                    )
            except (json.JSONDecodeError, OSError):
                continue
    return receipts


def _public_msg(m: dict) -> dict:
    """Strip internal (_-prefixed) fields for the wire, but surface provenance: a
    message materialized from the git transport carries an internal `_via` tag —
    expose it as `via: "remote"` so an agent knows this one crossed machines
    (durable delivery, so not necessarily instant)."""
    clean = {k: v for k, v in m.items() if not k.startswith("_")}
    if m.get("_via") == "git":
        clean["via"] = "remote"
    return clean


def _with_pending(result: dict) -> dict:
    """Attach NEW (pending) messages to a tool response, marking them read."""
    _cleanup_expired(AGENT_ID)
    messages = _read_inbox(AGENT_ID, state_filter="pending")
    if messages:
        # Strip internal _file before exposing, but keep for _mark_read
        _mark_read(messages)
        # Clean internal fields for response
        clean = [_public_msg(m) for m in messages]
        result["_dispatches"] = clean
        result["_dispatch_count"] = len(clean)
    return result


# ---------------------------------------------------------------------------
# Human-facing notifications (work even while the model is parked/idle)
# ---------------------------------------------------------------------------


def _should_notify(msg: dict) -> bool:
    # Delegates to the shared predicate (notify_policy.py) so the OS-notification
    # poll here and the bin/dispatch-wait model-wake long-poll apply identical rules.
    return should_notify(msg, NOTIFY_ON, AGENT_ID)


def _notify(msg: dict) -> None:
    """Run NOTIFY_COMMAND with (summary, body). Best-effort; never raises."""
    sender = msg.get("from", "?")
    pri = msg.get("priority", "normal")
    summary = f"dispatch: {pri} message from {sender}"
    body = (msg.get("content", "") or "").replace("\n", " ")[:200]
    try:
        # argv list (no shell); command is local trusted config. "--" stops the
        # message-derived summary/body from being parsed as options (e.g. a body
        # starting with "-"). Most notifiers (notify-send) honor the separator.
        subprocess.run(  # nosec B603
            [*shlex.split(NOTIFY_COMMAND), "--", summary, body],
            timeout=5,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def _start_notifier(agent_id: str) -> None:
    """Fire NOTIFY_COMMAND when new messages arrive — even with the model idle.

    The MCP server process outlives any single turn, so a background poll of the
    inbox is the one delivery path that reaches a parked session. Opt-in: empty
    NOTIFY_COMMAND disables it. Stdlib only (threading + subprocess), no deps.
    """
    if not NOTIFY_COMMAND:
        return
    inbox = DISPATCH_DIR / agent_id

    def _loop() -> None:
        try:
            seen = {p.name for p in inbox.glob("*.json")}
        except OSError:
            seen = set()
        while True:
            time.sleep(NOTIFY_POLL_SECONDS)
            try:
                current = {p.name for p in inbox.glob("*.json")}
            except OSError:
                continue
            for name in sorted(current - seen):
                try:
                    msg = json.loads((inbox / name).read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if _should_notify(msg):
                    _notify(msg)
            seen = current

    threading.Thread(target=_loop, name="dispatch-notifier", daemon=True).start()


# ---------------------------------------------------------------------------
# Filesystem watcher (stderr alerts for the human operator)
# ---------------------------------------------------------------------------


def _start_watcher(agent_id: str) -> None:
    """Watch inbox for new files and print alerts to stderr."""
    if not HAS_WATCHDOG:
        print("[dispatch] watchdog not installed — no real-time alerts", file=sys.stderr)
        return

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            if not isinstance(event, FileCreatedEvent):
                return
            if not event.src_path.endswith(".json"):
                return
            try:
                msg = json.loads(Path(event.src_path).read_text())
                sender = msg.get("from", "?")
                preview = msg.get("content", "")[:80]
                pri = msg.get("priority", "normal")
                marker = "!!!" if pri == "urgent" else ">>>"
                print(
                    f"\n[dispatch {marker}] Message from {sender}: {preview}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception:
                pass

    observer = Observer()
    observer.schedule(_Handler(), str(DISPATCH_DIR / agent_id), recursive=False)
    observer.daemon = True
    observer.start()


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

_setup_dirs()
_sweep_stale_tmp()
_reap_dead_presence()  # GC presence files whose owner is gone (e.g. after reboot)
_reap_empty_inboxes()  # GC empty inbox dirs left by exited dynamic-mode agents
AGENT_ID = _claim_id()
print(f"[dispatch] I am {AGENT_ID} (PID {os.getpid()})", file=sys.stderr)

atexit.register(lambda: _release_id(AGENT_ID))


def _on_sigterm(*_: object) -> None:
    _release_id(AGENT_ID)
    sys.exit(0)


signal.signal(signal.SIGTERM, _on_sigterm)

_start_watcher(AGENT_ID)
_start_notifier(AGENT_ID)

# Build instructions from template. The default below is the load-bearing
# "when to reach for this" contract — override it via the `instructions` config
# key for environment-specific guidance (e.g. escalation to a deliberation tool).
_default_instructions = (
    "You are agent '{agent_id}' on MCP Dispatch, a messaging rail shared with "
    "other agent sessions running concurrently on this host. "
    "WHEN to use it: when your work would ripple into another session's work — a "
    "shared-interface change, a decision that affects a dependent project, or a "
    "learning a sibling session needs — dispatch a short FYI to the relevant agent "
    "or channel. Most exchanges are 1-3 messages. "
    "Tools: dispatch(message, target) sends to an agent id, a '#channel', or 'all'; "
    "peek() reads new messages and shows receipts for what you sent; "
    "ack(ids) clears messages you're done with; who() lists who's online; "
    "subscribe('#name')/unsubscribe('#name') manage channel membership. "
    "Incoming messages also ride along on every tool response (piggyback delivery) — "
    "address them before resuming your current task. "
    "If a git transport is configured, the SAME targets also reach agents on OTHER "
    "hosts transparently (who() shows them under 'remote'; their delivery is durable "
    "but not instant, and such messages arrive tagged via='remote'). "
    "Available targets: {agent_list}."
)
_instructions_template = CONFIG["instructions"] or _default_instructions
_agent_list = ", ".join(AGENT_IDS) if AGENT_IDS else "(dynamic — any name)"
_instructions = _instructions_template.format(
    agent_id=AGENT_ID,
    agent_list=_agent_list,
)

mcp = FastMCP("dispatch", instructions=_instructions)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="dispatch",
    description=(
        "Send a message to a target: one agent (its id), a channel ('#name', "
        "delivered to current subscribers), or 'all'. "
        "Use priority='urgent' for time-sensitive messages. "
        "Optional: thread_id groups messages into conversations, "
        "reply_to references a specific message, "
        "payload carries structured data (dict), "
        "ttl sets expiry in seconds, "
        "must_read=true prevents auto-expiry. "
        "Returns confirmation (incl. delivered_to) plus any pending messages for you."
    ),
)
def dispatch_tool(
    message: str,
    target: str = "all",
    priority: str = "normal",
    thread_id: str | None = None,
    reply_to: str | None = None,
    payload: dict | None = None,
    ttl: int | None = None,
    must_read: bool = False,
) -> dict:
    """Send a message to other agents."""
    sent = _send(
        AGENT_ID,
        target,
        message,
        priority,
        thread_id=thread_id,
        reply_to=reply_to,
        payload=payload,
        ttl=ttl,
        must_read=must_read,
    )
    return _with_pending(
        {
            "sent": True,
            "id": sent["id"],
            "from": AGENT_ID,
            "to": target,
            "delivered_to": sent.get("delivered_to", []),
            "priority": priority,
            "thread_id": sent.get("thread_id"),
        }
    )


@mcp.tool(
    name="peek",
    description=(
        "Read incoming messages without deleting them. "
        "By default returns only NEW (unread) messages. "
        "Set include_read=true to see ALL unacknowledged messages. "
        "Filter by thread_id to see a specific conversation. "
        "Use ack() to acknowledge messages when you're done with them. "
        "Also returns delivery receipts for your recently sent messages."
    ),
)
def peek_tool(
    thread_id: str | None = None,
    include_read: bool = False,
) -> dict:
    """Non-destructive read of inbox messages plus sent message receipts."""
    _cleanup_expired(AGENT_ID)

    if include_read:
        messages = _read_inbox(AGENT_ID, thread_id=thread_id)
    else:
        messages = _read_inbox(AGENT_ID, state_filter="pending", thread_id=thread_id)

    # Mark pending → read
    _mark_read(messages)

    # Clean internal fields (and surface cross-host provenance via _public_msg)
    clean = [_public_msg(m) for m in messages]

    # Delivery receipts for sent messages
    receipts = _get_sent_receipts(AGENT_ID)

    result = {
        "agent_id": AGENT_ID,
        "messages": clean,
        "count": len(clean),
    }
    if receipts:
        result["sent_receipts"] = receipts
    return result


@mcp.tool(
    name="ack",
    description=(
        "Acknowledge and delete messages by their IDs. "
        "This is the only way to permanently remove messages from your inbox "
        "(besides TTL expiry). Pass a list of message IDs to acknowledge."
    ),
)
def ack_tool(
    message_ids: list[str],
) -> dict:
    """Acknowledge and delete messages."""
    inbox = DISPATCH_DIR / AGENT_ID
    acked = 0
    not_found = []

    for msg_id in message_ids:
        found = False
        for f in inbox.glob("*.json"):
            try:
                msg = json.loads(f.read_text())
                if msg.get("id") == msg_id:
                    f.unlink()
                    acked += 1
                    found = True
                    break
            except (json.JSONDecodeError, OSError):
                continue
        if not found:
            not_found.append(msg_id)

    result = {
        "agent_id": AGENT_ID,
        "acked": acked,
        "not_found": not_found if not_found else None,
    }
    return _with_pending(result)


@mcp.tool(
    name="who",
    description=(
        "List agents: those live on this host, plus any reachable cross-host via "
        "the git transport (the 'remote' list — durable delivery, so they may be "
        "offline right now). dispatch(target=id) reaches either the same way."
    ),
)
def who_tool() -> dict:
    """List connected agents. Liveness is the presence flock, not a pid check.

    This only *filters* by liveness; it never unlinks (that would race a process
    reclaiming the same id). Dead presence files are reaped at startup instead.

    Cross-host agents come from DISPATCH_DIR/.remote/, a roster the dispatch-gitsync
    daemon maintains from git lane activity (no heartbeat); who() stays git-agnostic
    and just reads it. A live-local agent shadows any remote entry of the same id.
    """
    agents: list[dict] = []
    for pf in _live_presence_files():
        try:
            agents.append(json.loads(pf.read_text()))
        except (json.JSONDecodeError, OSError):
            pass

    local_ids = {a.get("agent_id") for a in agents}
    remote: list[dict] = []
    remote_dir = DISPATCH_DIR / ".remote"
    if remote_dir.is_dir():
        for rf in sorted(remote_dir.glob("*.json")):
            try:
                data = json.loads(rf.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("agent_id") in local_ids:
                continue  # a live local session wins over a git roster entry
            remote.append(data)

    result = {
        "self": AGENT_ID,
        "agents": agents,
        "count": len(agents),
    }
    if remote:
        result["remote"] = remote
        result["remote_count"] = len(remote)
    return result


@mcp.tool(
    name="subscribe",
    description=(
        "Subscribe to a channel so dispatch(target='#name') reaches you. "
        "Pass the channel name with or without the leading '#'. "
        "Subscriptions are ephemeral — they vanish when this session exits."
    ),
)
def subscribe_tool(channel: str) -> dict:
    """Join a channel."""
    name = channel[1:] if channel.startswith("#") else channel
    channels = _set_subscription(name, True)
    return _with_pending({"agent_id": AGENT_ID, "subscribed": name, "channels": channels})


@mcp.tool(
    name="unsubscribe",
    description=(
        "Leave a channel you previously subscribed to. "
        "Pass the channel name with or without the leading '#'."
    ),
)
def unsubscribe_tool(channel: str) -> dict:
    """Leave a channel."""
    name = channel[1:] if channel.startswith("#") else channel
    channels = _set_subscription(name, False)
    return _with_pending({"agent_id": AGENT_ID, "unsubscribed": name, "channels": channels})


if __name__ == "__main__":
    mcp.run()
