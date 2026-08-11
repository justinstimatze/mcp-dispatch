"""bridge_native — a one-way-trust bridge to Claude Code's own built-in
inter-session protocol (Unix sockets under ``/tmp/cc-socks/``), as documented in
https://github.com/chrismo/claude-rig/blob/main/docs/inter-claude-protocol.md.

mcp-dispatch already reaches agents that run the ``dispatch`` MCP server. Every
Claude Code session ALSO speaks a second, built-in protocol with no setup at
all — newline-delimited JSON over a per-session Unix socket, discovered via a
``~/.claude/sessions/<pid>.json`` registry. This module bridges the two, so a
bridged dispatch nick can be reached by ANY local Claude Code session, not just
ones wired into the dispatch relay.

The spec is explicit that attribution on that wire is NOT a transport-verified
fact: the ``<cross-session-message from=… from-name=…>`` wrapper the *sending
tool* composes is plain text inside ``message.content``, and the receiving
model never sees the envelope's own ``from`` field at all. Concretely, that
means any local process that can open a socket in ``/tmp/cc-socks/`` — which
includes a *sibling* Claude Code session that has been prompt-injected via
content it was asked to process — can claim to be anyone. That is not a remote
hypothetical for this codebase: a fleet of differently-trusted agents sharing
one host and one relay is the exact scenario mcp-dispatch exists for (see the
supervisor's argv/env allowlisting in supervisor.py for the same threat applied
to process launch instead of message content).

So this bridge treats everything it receives as untrusted, symmetrically with
how git_bridge.py treats a remote host: tagged provenance, never a trusted
identity.

  * Inbound messages are ALWAYS attributed to the fixed ``NATIVE_FROM_ID``
    placeholder, never to whatever the envelope or wrapper claims — see
    ``native_to_local_msg``. The sender's self-reported identity is kept,
    read-only, under ``payload.claimed_from`` for a human to weigh.
  * Inbound messages never carry ``must_read`` — the native protocol has no
    such concept, and nothing here synthesizes one.
  * ``notify_policy.should_notify`` refuses to let a message tagged
    ``_via: "native-bridge"`` force a wake via the must_read override unless
    the operator has explicitly set ``[bridge] trust_wake = true``. See
    notify_policy.py.
  * Bridged nicks are an explicit allowlist (``[bridge] nicks = [...]``), the
    same "nothing without a config line" posture as ``[supervisor]``. There is
    no wildcard that exposes every dispatch agent to the native bus.

Outbound delivery mirrors GitBridge's ``_publish_one``: a message is only
handed to the native transport once dispatch itself has given up trying to
reach the recipient locally (no live local presence) and a live native session
answers to that name. Delivery there is fire-and-forget with no receipt, so
"sent" only means the write succeeded — never that anything read it.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import dispatch_fs
from dispatch_fs import ID_RE

# The placeholder `from` for every message this bridge delivers into a local
# inbox. Fixed and never derived from the wire, so nothing downstream (task
# claiming, who(), a trust decision in someone's own reasoning) can be misled
# into treating bridged content as a real dispatch identity.
NATIVE_FROM_ID = "native-bridge"

# This exact path is the wire contract, not a scratch location we chose: it's
# where Claude Code's own harness creates its inter-session sockets, per the
# spec. Nothing here writes secrets or predictable-named files an attacker
# could pre-create to race — the directory is created 0700 and each socket
# 0600 in NativeInboundListener.start().
DEFAULT_SOCKET_DIR = Path("/tmp/cc-socks")  # nosec B108
DEFAULT_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

PROTO_VERSION = 1
CONNECT_TIMEOUT = 5.0  # matches the spec's stated sender-side connection timeout
PROBE_TIMEOUT = 0.25  # matches the spec's stated roster-probe connect attempt
MAX_LINE_BYTES = 1_048_576  # the spec's 1 MiB newline-delimited-JSON line limit

# Outbound-ledger entries older than this are pruned, mirroring git_bridge's
# LEDGER_TTL_SECONDS: once the source inbox message has expired we can never
# re-see it, so the entry recording "already attempted" is moot.
LEDGER_TTL_SECONDS = 14 * 24 * 3600


# ---------------------------------------------------------------------------
# Roster: discovering live native sessions
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not owned by us
    except OSError:
        return False
    return True


def _probe_socket(path: Path, timeout: float = PROBE_TIMEOUT) -> bool:
    """Best-effort liveness probe: connect, send nothing, close. Per the spec's
    own roster-building rule — presence in the registry doesn't mean the socket
    still answers."""
    if not path.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def list_native_sessions(
    sessions_dir: Path = DEFAULT_SESSIONS_DIR,
    *,
    probe: bool = True,
    timeout: float = PROBE_TIMEOUT,
) -> list[dict[str, Any]]:
    """Every registry entry with a live pid, optionally probed for a live socket.

    A dead-pid entry is skipped rather than deleted — garbage-collecting a file
    this module doesn't own is the harness's job, not ours (mirrors
    dispatch_fs.presence_is_live's read-only stance on liveness).
    """
    sessions_dir = Path(sessions_dir)
    out: list[dict[str, Any]] = []
    if not sessions_dir.is_dir():
        return out
    for f in sorted(sessions_dir.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        sock_path = rec.get("messagingSocketPath")
        pid = rec.get("pid")
        if not sock_path or not isinstance(pid, int):
            continue
        if not _pid_alive(pid):
            continue
        rec = dict(rec)
        rec["_registry_file"] = str(f)
        rec["live"] = _probe_socket(Path(str(sock_path)), timeout) if probe else None
        out.append(rec)
    return out


def find_live_session(
    name: str,
    sessions: list[dict[str, Any]] | None = None,
    *,
    sessions_dir: Path = DEFAULT_SESSIONS_DIR,
) -> dict[str, Any] | None:
    """The one live session named ``name``, or None if there is zero or more than
    one — an ambiguous name is a reason to decline, not to guess (same rule as
    dispatch_common.pick_by_ancestry)."""
    pool = list_native_sessions(sessions_dir) if sessions is None else sessions
    matches = [s for s in pool if s.get("name") == name and s.get("live")]
    if len(matches) != 1:
        return None
    return matches[0]


# ---------------------------------------------------------------------------
# Outbound: dispatch message -> native envelope
# ---------------------------------------------------------------------------


def _wrap_content(msg: dict[str, Any]) -> str:
    """Render a dispatch message the way a native peer expects to read one.

    Per the spec, attribution is composed by the SENDING TOOL into
    ``message.content`` — the receiving model never sees a verified envelope
    field. This mirrors that convention on the way out, and is exactly why the
    inbound half of this module (``native_to_local_msg``) never trusts the same
    convention coming back: it's a display format, not proof of anything.
    """
    frm = str(msg.get("from") or "?")
    meta = []
    if msg.get("thread_id"):
        meta.append(f"thread={msg['thread_id']}")
    if msg.get("priority") and msg["priority"] != "normal":
        meta.append(f"priority={msg['priority']}")
    tail = f" [{', '.join(meta)}]" if meta else ""
    body = str(msg.get("content", ""))
    return (
        f'<cross-session-message from="dispatch:{frm}" from-name="{frm}" '
        f'from-mode="dispatch">{body}</cross-session-message>{tail}'
    )


def build_envelope(msg: dict[str, Any], *, from_path: str) -> dict[str, Any]:
    """The outbound native JSON envelope for one dispatch message."""
    return {
        "msgV": PROTO_VERSION,
        "msg_id": str(uuid.uuid4()),
        "type": "user",
        "message": {"role": "user", "content": _wrap_content(msg)},
        "priority": "next",
        "from": from_path,
    }


def send_native(
    socket_path: Path, envelope: dict[str, Any], *, timeout: float = CONNECT_TIMEOUT
) -> bool:
    """Fire-and-forget delivery to a native session's socket.

    True means only that the write succeeded — the protocol returns nothing on
    the sending socket (per spec), so there is no way to confirm anything read
    it. Never raises: a dead or slow peer is exactly as unremarkable here as a
    frozen git remote is to GitBridge._publish_one.
    """
    line = (json.dumps(envelope) + "\n").encode()
    if len(line) > MAX_LINE_BYTES:
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(socket_path))
        s.sendall(line)
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Inbound: native envelope -> dispatch message
# ---------------------------------------------------------------------------


def parse_inbound_line(raw: bytes) -> dict[str, Any] | None:
    """Validate one NDJSON line against the spec's stated gates. None means
    silently drop, exactly as the spec says a real receiver would."""
    if not raw or len(raw) > MAX_LINE_BYTES:
        return None
    try:
        env = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(env, dict) or env.get("type") != "user":
        return None  # control messages (rename, receipts) aren't handled here
    message = env.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return None
    return env


def native_to_local_msg(env: dict[str, Any], *, to: str) -> dict[str, Any]:
    """A validated inbound native envelope, as a dispatch inbox message.

    ``from`` is ALWAYS ``NATIVE_FROM_ID`` — never the envelope's own claimed
    sender, which is untrusted content, not a verified identity (see the module
    docstring). That claim is preserved read-only for a human under
    ``payload.claimed_from``, not consumed by anything that makes a trust
    decision. ``must_read`` is always False: the native protocol has no such
    concept, so nothing here synthesizes escalation on its behalf.
    """
    content = env["message"]["content"]
    return {
        "id": f"msg-{uuid.uuid4().hex[:8]}",
        "from": NATIVE_FROM_ID,
        "to": to,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "priority": "normal",
        "content": content,
        "payload": {"claimed_from": env.get("from"), "native_msg_id": env.get("msg_id")},
        "thread_id": None,
        "reply_to": None,
        "ttl": None,
        "must_read": False,
        "state": "pending",
        "_via": "native-bridge",
    }


# ---------------------------------------------------------------------------
# Inbound listener: one Unix socket + one session-registry file per bridged nick
# ---------------------------------------------------------------------------


def _peer_is_us(conn: socket.socket) -> bool:
    """Kernel-verified: does the connecting process share our uid?

    The socket directory is already 0700 and the socket itself 0600, so this is
    defense in depth rather than the primary gate — but it's the same
    unforgeable check dispatch-ircd's Go gateway makes (tui/ircd/auth.go), and
    the same fail-closed policy: credentials unavailable means refused, not
    admitted.
    """
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
    except (OSError, AttributeError):
        return False
    return uid == os.getuid()


class NativeInboundListener:
    """Owns one bridged nick's registry file and Unix socket.

    Accepts native connections, validates each NDJSON line, and materializes
    valid ones into that nick's dispatch inbox tagged ``_via: "native-bridge"``.
    """

    def __init__(
        self,
        nick: str,
        *,
        dispatch_dir: Path,
        socket_dir: Path = DEFAULT_SOCKET_DIR,
        sessions_dir: Path = DEFAULT_SESSIONS_DIR,
    ) -> None:
        ID_RE.match(nick) or (_ for _ in ()).throw(ValueError(f"invalid nick {nick!r}"))
        self.nick = nick
        self.dispatch_dir = Path(dispatch_dir)
        self.socket_dir = Path(socket_dir)
        self.sessions_dir = Path(sessions_dir)
        self.socket_path = self.socket_dir / f"peer-dispatch-{nick}.sock"
        self.registry_path = self.sessions_dir / f"dispatch-bridge-{nick}.json"
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.delivered = 0

    def start(self) -> None:
        self.socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.socket_dir, 0o700)
        except OSError:
            pass
        if self.socket_path.exists():
            self.socket_path.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        s.listen(8)
        s.settimeout(0.5)  # lets the accept loop notice _stop promptly
        self._sock = s
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        dispatch_fs.atomic_write(
            self.registry_path,
            {
                "pid": os.getpid(),
                "name": self.nick,
                "kind": "bridge",
                "status": "idle",
                "messagingSocketPath": str(self.socket_path),
            },
        )
        self._thread = threading.Thread(
            target=self._accept_loop, args=(s,), name=f"ucbridge-{self.nick}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        for p in (self.socket_path, self.registry_path):
            try:
                p.unlink()
            except OSError:
                pass

    def _accept_loop(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle, args=(conn,), name=f"ucbridge-{self.nick}-conn", daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            if not _peer_is_us(conn):
                return
            conn.settimeout(30.0)
            buf = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._deliver(line)
                if len(buf) > MAX_LINE_BYTES:
                    break  # unterminated oversized line: drop the connection
        except OSError:
            pass
        finally:
            conn.close()

    def _deliver(self, raw: bytes) -> None:
        env = parse_inbound_line(raw)
        if env is None:
            return
        msg = native_to_local_msg(env, to=self.nick)
        inbox = self.dispatch_dir / self.nick
        inbox.mkdir(parents=True, exist_ok=True)
        dispatch_fs.atomic_write(inbox / dispatch_fs.message_filename(NATIVE_FROM_ID), msg)
        self.delivered += 1


# ---------------------------------------------------------------------------
# NativeBridge: orchestrates the allowlisted nicks' listeners + outbound scan
# ---------------------------------------------------------------------------


class NativeBridge:
    """Bridges an explicit allowlist of dispatch nicks to the native protocol.

    No wildcard: a nick not in ``nicks`` is never exposed on the native bus and
    never receives outbound native delivery, the same "nothing without a config
    line" posture as ``[supervisor]``.
    """

    def __init__(
        self,
        dispatch_dir: Path,
        nicks: list[str],
        *,
        sessions_dir: Path = DEFAULT_SESSIONS_DIR,
        socket_dir: Path = DEFAULT_SOCKET_DIR,
        state_dir: Path | None = None,
    ) -> None:
        for n in nicks:
            ID_RE.match(n) or (_ for _ in ()).throw(ValueError(f"invalid nick {n!r}"))
        self.dispatch_dir = Path(dispatch_dir)
        self.nicks = sorted(set(nicks))
        self.sessions_dir = Path(sessions_dir)
        self.socket_dir = Path(socket_dir)
        self._state = Path(state_dir) if state_dir else (self.dispatch_dir / ".native")
        self._ledger_path = self._state / "ucbridge-outbound.json"
        self._ledger = self._load_ledger()
        self._listeners: dict[str, NativeInboundListener] = {}

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        for nick in self.nicks:
            listener = NativeInboundListener(
                nick,
                dispatch_dir=self.dispatch_dir,
                socket_dir=self.socket_dir,
                sessions_dir=self.sessions_dir,
            )
            listener.start()
            self._listeners[nick] = listener

    def stop(self) -> None:
        for listener in self._listeners.values():
            listener.stop()
        self._listeners.clear()

    def run(self, interval: float) -> None:  # pragma: no cover - thin loop
        self.start()
        try:
            while True:
                self.tick()
                time.sleep(interval)
        finally:
            self.stop()

    # -- outbound: local inbox -> native socket -------------------------------

    def tick(self) -> int:
        """One outbound pass: deliver newly-seen messages addressed to a name
        that resolves on the native bus but not on the local dispatch bus.
        Returns how many were sent."""
        sessions = list_native_sessions(self.sessions_dir)
        live_local = set(dispatch_fs.live_agents(self.dispatch_dir))
        sent = 0
        touched = False
        for msg in self._local_messages():
            mid = msg.get("id")
            if not mid or mid in self._ledger:
                continue
            touched = True
            if self._publish_one(msg, sessions=sessions, live_local=live_local):
                sent += 1
            self._ledger[mid] = time.time()
        if touched:
            self._save_ledger()
        return sent

    def _local_messages(self):
        for inbox in self._inbox_dirs():
            for f in sorted(inbox.glob("*.json")):
                try:
                    msg = json.loads(f.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if msg.get("_via") in ("git", "native-bridge"):
                    continue  # never echo something that arrived over a bridge
                if msg.get("state") == "expired":
                    continue
                yield msg

    def _publish_one(
        self, msg: dict[str, Any], *, sessions: list[dict[str, Any]], live_local: set[str]
    ) -> bool:
        to = msg.get("to")
        if not to or to == "all" or to.startswith("#"):
            return False  # broadcast/channels have no native equivalent
        if not ID_RE.match(to):
            return False
        if to in live_local:
            return False  # the local bus already delivered it
        session = find_live_session(to, sessions)
        if session is None:
            return False
        envelope = build_envelope(msg, from_path=str(self.socket_dir / "dispatch-ucbridge.sock"))
        return send_native(Path(str(session["messagingSocketPath"])), envelope)

    def _inbox_dirs(self) -> list[Path]:
        try:
            entries = sorted(self.dispatch_dir.iterdir())
        except OSError:
            return []
        return [d for d in entries if d.is_dir() and not d.name.startswith(".")]

    # -- ledger (identical shape to git_bridge's) ------------------------------

    def _load_ledger(self) -> dict[str, float]:
        try:
            raw: dict[str, Any] = json.loads(self._ledger_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        cutoff = time.time() - LEDGER_TTL_SECONDS
        return {k: float(v) for k, v in raw.items() if float(v) >= cutoff}

    def _save_ledger(self) -> None:
        cutoff = time.time() - LEDGER_TTL_SECONDS
        pruned = {k: v for k, v in self._ledger.items() if v >= cutoff}
        self._ledger = pruned
        self._state.mkdir(parents=True, exist_ok=True)
        tmp = self._ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(pruned))
        tmp.replace(self._ledger_path)
