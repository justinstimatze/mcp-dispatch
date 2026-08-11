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
    notify_policy.py. Ordinary priority claims are NOT gated the same way —
    every dispatch sender can already self-report ``priority="urgent"`` with
    no validation, so mapping the native envelope's own ``"now"``/``"next"``
    into that same pre-existing, already-untrusted channel (see
    ``native_to_local_msg``) isn't a new trust concession.
  * Bridged nicks are an explicit allowlist (``[bridge] nicks = [...]``), the
    same "nothing without a config line" posture as ``[supervisor]``. There is
    no wildcard that exposes every dispatch agent to the native bus — and this
    is enforced on BOTH directions: inbound listeners only ever exist for a
    bridged nick, and the outbound scan (``NativeBridge._local_messages``)
    only ever reads a bridged nick's own inbox, never anyone else's.

Outbound delivery mirrors GitBridge's ``_publish_one``: a message is only
handed to the native transport once dispatch itself has given up trying to
reach the recipient locally (no live local presence) and a live native session
answers to that name. Delivery there is fire-and-forget with no receipt, so
"sent" only means the write succeeded — never that anything read it.
"""

from __future__ import annotations

import html
import json
import os
import socket
import struct
import sys
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

    EVERY field interpolated here is escaped — not just ``from``/``content``.
    ``thread_id`` and ``priority`` are just as attacker-controlled as
    ``content`` (``server.py``'s ``dispatch()`` accepts both as free-text
    strings with no enum/charset validation), so leaving either unescaped
    would reopen exactly the hole escaping ``content`` closes: a crafted
    ``thread_id`` containing its own literal
    ``</cross-session-message><cross-session-message from="…">`` can forge a
    second, differently-attributed block just as effectively as a crafted
    ``content`` can. That would defeat the one thing this wrapper is supposed
    to preserve truthfully: which *dispatch-validated* identity
    (``msg["from"]``, set server-side by ``server.py``'s ``_send``, never
    attacker-chosen) actually sent this. Escaping keeps every piece of
    untrusted content inertly inside its own element instead of letting it
    edit the markup around it.
    """
    frm = html.escape(str(msg.get("from") or "?"), quote=True)
    meta = []
    if msg.get("thread_id"):
        meta.append(f"thread={html.escape(str(msg['thread_id']), quote=True)}")
    if msg.get("priority") and msg["priority"] != "normal":
        meta.append(f"priority={html.escape(str(msg['priority']), quote=True)}")
    tail = f" [{', '.join(meta)}]" if meta else ""
    body = html.escape(str(msg.get("content", "")), quote=True)
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
    """Validate one NDJSON ``type: "user"`` line against the spec's stated
    gates. None means silently drop, exactly as the spec says a real receiver
    would. A ``type: "control"`` line is a different, valid thing — see
    ``parse_control_line`` — so this returning None doesn't imply the line was
    garbage, only that it isn't a deliverable message."""
    if not raw or len(raw) > MAX_LINE_BYTES:
        return None
    try:
        env = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(env, dict) or env.get("type") != "user":
        return None
    message = env.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return None
    return env


def parse_control_line(raw: bytes) -> dict[str, Any] | None:
    """Validate one NDJSON ``type: "control"`` line (``rename``,
    ``peer_message_status`` delivery receipts, per the spec). Recognized but
    never acted on:

    - ``rename`` would change how this bridge's registered identity resolves.
      Honoring a request to rename FROM the wire would let any peer that can
      reach the socket hijack a bridged nick's addressing — the identity is
      the operator's ``[bridge] nicks`` config, not something a peer gets to
      negotiate.
    - ``peer_message_status`` (delivery receipts) has no consumer here:
      ``send_native`` is intentionally fire-and-forget (see its docstring),
      and correlating a receipt back to a specific outbound send would need a
      sent-message log this bridge doesn't keep. The spec's own reference
      implementation is in the same position — receipts exist on the wire but
      nothing acts on hold/denial notifications there either.

    So a caller sees these ONLY for observability (``NativeInboundListener
    .control_seen``), distinguishing "recognized but intentionally inert"
    from "not valid JSON at all" — which ``parse_inbound_line`` alone can't."""
    if not raw or len(raw) > MAX_LINE_BYTES:
        return None
    try:
        env = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(env, dict) or env.get("type") != "control":
        return None
    return env


def native_to_local_msg(env: dict[str, Any], *, to: str) -> dict[str, Any]:
    """A validated inbound native envelope, as a dispatch inbox message.

    ``from`` is ALWAYS ``NATIVE_FROM_ID`` — never the envelope's own claimed
    sender, which is untrusted content, not a verified identity (see the module
    docstring). That claim is preserved read-only for a human under
    ``payload.claimed_from``, not consumed by anything that makes a trust
    decision. ``must_read`` is always False: the native protocol has no such
    concept, so nothing here synthesizes escalation on its behalf, and
    notify_policy.py refuses to let it pierce via must_read regardless.

    ``priority`` DOES carry the envelope's own self-reported ``"now"`` (mapped
    to dispatch's ``"urgent"``) vs ``"next"`` (``"normal"``). This is not a new
    trust concession: every dispatch sender can already self-report
    ``priority="urgent"`` with no validation — notify_policy.py's "important"
    policy has always trusted a claimed priority the same way it distrusts a
    claimed must_read. Without this mapping every native-bridge message would
    be silently unable to wake anyone under the default ``notify_on =
    "important"``, regardless of ``[bridge] trust_wake`` (which only affects
    the separate must_read override, not this one) — undermining the whole
    point of exposing a nick on the native bus. must_read stays the one thing
    this bridge gates behind explicit operator opt-in.
    """
    content = env["message"]["content"]
    return {
        "id": f"msg-{uuid.uuid4().hex[:8]}",
        "from": NATIVE_FROM_ID,
        "to": to,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "priority": "urgent" if env.get("priority") == "now" else "normal",
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
        # Each connection gets its own thread (_accept_loop), so these two
        # counters are incremented concurrently — a bare `+= 1` is a
        # non-atomic read-modify-write that can lose an increment when two
        # connections land at once. Observability-only (nothing branches on
        # the exact count), but a wrong count is still a wrong count.
        self._counts_lock = threading.Lock()
        self.delivered = 0
        self.control_seen = 0

    def start(self) -> None:
        self.socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.socket_dir, 0o700)
        except OSError:
            pass
        if self.socket_path.exists():
            # peer-dispatch-<nick>.sock lives in a HOST-GLOBAL namespace
            # (/tmp/cc-socks/ by default), not scoped to this relay's own
            # DISPATCH_DIR — the host-level lock in bin/dispatch-ucbridge only
            # ever prevents two daemons for the SAME DISPATCH_DIR from racing
            # each other, so two independently-configured relays that happen
            # to both bridge a nick named e.g. "publicai" are not stopped by
            # it. Without this probe, whichever one starts second would
            # silently unlink and rebind the first's live socket, and
            # atomic_write below would overwrite its registry entry — routing
            # every future native message addressed to that name into the
            # SECOND relay's dispatch_dir instead of the first's, defeating
            # the provenance/allowlist guarantees this whole module exists
            # for. Refuse instead of stealing.
            if _probe_socket(self.socket_path):
                raise RuntimeError(
                    f"{self.socket_path} is already live — another process "
                    f"(a different dispatch-ucbridge instance?) already owns "
                    f"nick {self.nick!r} on the native bus. Bridged nick names "
                    "are a host-wide namespace: refusing to steal a live "
                    "socket out from under whoever already holds it."
                )
            self.socket_path.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Narrow the process umask around bind(), then still chmod explicitly
        # afterward — the same belt-and-suspenders dispatch-ircd's Go gateway
        # uses (tui/ircd/listen.go): the umask closes the window between
        # "socket exists" and "socket has safe permissions" (an ambient umask
        # under group_mode, e.g. this daemon's own 0007 systemd UMask, would
        # otherwise leave the socket group-writable for that window), and the
        # chmod is the actual guarantee — the umask's effect on a socket
        # inode isn't portable enough to rely on alone.
        old_umask = os.umask(0o177)
        try:
            s.bind(str(self.socket_path))
        finally:
            os.umask(old_umask)
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
        if env is not None:
            msg = native_to_local_msg(env, to=self.nick)
            inbox = self.dispatch_dir / self.nick
            inbox.mkdir(parents=True, exist_ok=True)
            dispatch_fs.atomic_write(inbox / dispatch_fs.message_filename(NATIVE_FROM_ID), msg)
            with self._counts_lock:
                self.delivered += 1
            return
        if parse_control_line(raw) is not None:
            # Recognized (rename / peer_message_status) but deliberately not
            # acted on — see parse_control_line's docstring. Counted so an
            # operator can tell "peers are talking to me and I'm ignoring
            # control frames" apart from "nothing is reaching this socket".
            with self._counts_lock:
                self.control_seen += 1


# ---------------------------------------------------------------------------
# NativeBridge: orchestrates the allowlisted nicks' listeners + outbound scan
# ---------------------------------------------------------------------------


class NativeBridge:
    """Bridges an explicit allowlist of dispatch nicks to the native protocol.

    No wildcard: a nick not in ``nicks`` is never exposed on the native bus and
    never receives outbound native delivery, the same "nothing without a config
    line" posture as ``[supervisor]``. That allowlist is enforced TWICE on the
    outbound path, deliberately redundantly: ``_local_messages`` only ever
    scans a bridged nick's own inbox in the first place, and ``_publish_one``
    checks ``to in self.nicks`` again before ever building an envelope — so a
    future change to either one alone can't reopen the gap where an unrelated
    dispatch agent's private mail was reachable by anyone able to register a
    same-named entry in the native session roster.
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
        # Deliberately NOT inside `.native/` (see _write_native_roster): that
        # directory is glob-and-prune owned by the roster writer, which
        # unlinks any *.json file it doesn't recognize as a currently-live
        # session. The ledger used to live there and was deleted as "stale"
        # on the very next tick — silently breaking both the at-most-once
        # send guarantee and the first-run backlog seed across every daemon
        # restart. A sibling directory keeps the two concerns from colliding
        # regardless of what either one's file-naming convention does later.
        self._state = Path(state_dir) if state_dir else (self.dispatch_dir / ".native-state")
        self._ledger_path = self._state / "ucbridge-outbound.json"
        # "Bridge from now on": if no ledger exists yet (the bridge was just
        # enabled), messages already sitting in a bridged nick's inbox are
        # pre-existing backlog, NOT traffic to forward. Mirrors GitBridge's
        # identical first-run guard, and for the identical reason — without it,
        # turning this on for a nick with old pending mail dumps that backlog
        # onto whatever native session currently answers to that name.
        first_run = not self._ledger_path.exists()
        self._ledger = self._load_ledger()
        if first_run:
            self._seed_ledger_from_backlog()
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
                self.tick_guarded()
                time.sleep(interval)
        finally:
            self.stop()

    # -- outbound: local inbox -> native socket -------------------------------

    def tick(self) -> int:
        """One outbound pass: deliver newly-seen messages addressed to a
        bridged nick that resolves on the native bus but not on the local
        dispatch bus, and refresh who() 's view of native-bus visibility.
        Returns how many were sent.

        The native-session probe sweep (a live connect per registered session)
        runs every tick unconditionally now — not just when there's an
        un-ledgered candidate to send — because ``_write_native_roster`` needs
        current liveness regardless of outbound traffic: who()'s ``native`` key
        should reflect who's reachable right now, not lag until the next
        dispatch() happens to target one of them.

        A message whose native target isn't live YET is deliberately left
        un-ledgered (see ``_publish_one``'s three-way return) so it is
        reconsidered every tick until either it's sent or a native session by
        that name actually shows up — rather than being permanently written
        off the moment it's first seen, which would silently drop any message
        that arrives before its bridged nick's native session happens to
        start.
        """
        sessions = list_native_sessions(self.sessions_dir)
        self._write_native_roster(sessions)
        live_local = set(dispatch_fs.live_agents(self.dispatch_dir))
        sent = 0
        touched = False
        for msg in self._local_messages():
            mid = msg.get("id")
            if not mid or mid in self._ledger:
                continue
            result = self._publish_one(msg, sessions=sessions, live_local=live_local)
            if result is None:
                continue  # not yet deliverable — leave un-ledgered, retry next tick
            touched = True
            if result:
                sent += 1
            self._ledger[mid] = time.time()
        if touched:
            self._save_ledger()
        return sent

    def tick_guarded(self) -> bool:
        """tick() that never raises: mirrors GitBridge.tick_guarded — a
        transient I/O error must not take down every bridged nick's listener.
        Returns True on a clean pass, False if it swallowed an error."""
        try:
            self.tick()
            return True
        except Exception as e:  # noqa: BLE001 - daemon resilience is the whole point
            print(f"[ucbridge] sync pass failed (will retry): {e}", file=sys.stderr, flush=True)
            return False

    def _local_messages(self):
        """Yield each pending message in a BRIDGED nick's own inbox — never any
        other agent's. Scoping the scan itself (rather than reading every inbox
        on the relay and filtering by recipient afterward) means an unrelated
        dispatch agent's private mail is never even parsed by this process, on
        top of the explicit `to in self.nicks` check `_publish_one` makes
        again before ever building an envelope."""
        for nick in self.nicks:
            inbox = self.dispatch_dir / nick
            if not inbox.is_dir():
                continue
            for f in sorted(inbox.glob("*.json")):
                try:
                    msg = json.loads(f.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if msg.get("_via") in dispatch_fs.BRIDGED_VIA_TAGS:
                    continue  # never echo something that arrived over a bridge
                if msg.get("state") == "expired":
                    continue
                yield msg

    def _publish_one(
        self, msg: dict[str, Any], *, sessions: list[dict[str, Any]], live_local: set[str]
    ) -> bool | None:
        """Try to deliver one message. Three-way result, not a plain bool —
        the caller (tick()) uses this to decide whether the message is done
        with forever or must be looked at again next tick:

          True  — sent. Done.
          False — PERMANENTLY not applicable (broadcast/channel, not one of
                  our nicks, malformed `to`, or already delivered by the local
                  bus). None of these become true later, so the caller may
                  ledger it and never look again.
          None  — NOT YET deliverable, but might become so: no live native
                  session currently answers to this name, or the one that
                  does didn't accept the write this tick. Both are ordinary,
                  expected states for an ephemeral local process — the native
                  session simply hasn't started yet, or was momentarily slow
                  to accept — not a reason to give up. The caller must NOT
                  ledger these, or a message that arrives before its native
                  target happens to be running is silently dropped forever
                  the instant that target finally starts.
        """
        to = msg.get("to")
        if not isinstance(to, str) or not to or to == "all" or to.startswith("#"):
            return False  # broadcast/channels have no native equivalent
        if to not in self.nicks:
            return False  # redundant with _local_messages' scope — see class docstring
        if not ID_RE.match(to):
            return False
        if to in live_local:
            return False  # the local bus already delivered it
        session = find_live_session(to, sessions)
        if session is None:
            return None  # no live native peer yet — retry next tick
        envelope = build_envelope(msg, from_path=str(self.socket_dir / "dispatch-ucbridge.sock"))
        return send_native(Path(str(session["messagingSocketPath"])), envelope) or None

    # -- native-bus visibility for who() -----------------------------------------

    def _write_native_roster(self, sessions: list[dict[str, Any]]) -> None:
        """Materialize native-bus visibility into DISPATCH_DIR/.native/ so who()
        can show it — read-only from server.py's side, exactly like GitBridge's
        .remote/ roster keeps who() git-agnostic (that separation is deliberate;
        server.py doesn't import this module). Every OTHER live native session is
        listed, live-probed fresh this tick, self-pruning each pass.

        Two exclusions, both deliberate: our OWN inbound listeners register with
        ``kind: "bridge"`` (see NativeInboundListener.start) — those are bridged
        nicks reflected back at themselves, not new information, so who() would
        just be quoting its own `agents`/`known` entries back. And unlike
        GitBridge's `.remote/` (durable: an entry survives its agent going
        offline, flagged `stale` instead of removed — see git_bridge.py), a dead
        native session has no lane history to remain reachable through, so it's
        simply dropped rather than marked stale.

        A third exclusion, for consistency rather than trust: a name with more
        than one live match is ambiguous, and ``_publish_one``'s
        ``find_live_session`` already declines to guess in that case (same
        rule as ``dispatch_common.pick_by_ancestry``). Showing one of the two
        anyway here — "last one wins" — would have who() confidently display a
        session that an actual ``dispatch()`` to that name could never reach.
        """
        roster_dir = self.dispatch_dir / ".native"
        by_name: dict[str, list[dict[str, Any]]] = {}
        for s in sessions:
            name = s.get("name")
            live_other = s.get("live") and s.get("kind") != "bridge"
            if live_other and isinstance(name, str) and ID_RE.match(name):
                by_name.setdefault(name, []).append(s)
        current = {name: matches[0] for name, matches in by_name.items() if len(matches) == 1}
        roster_dir.mkdir(parents=True, exist_ok=True)
        existing = {p.stem: p for p in roster_dir.glob("*.json")}
        for name, sess in current.items():
            path = roster_dir / f"{name}.json"
            record = {"name": name, "via": "native", "kind": sess.get("kind")}
            # Only write on change, same reasoning as GitBridge._write_remote_roster:
            # in the steady state every entry is byte-identical, so an unconditional
            # write+rename here would cost one per known session per tick forever.
            try:
                if json.loads(path.read_text()) == record:
                    continue
            except (OSError, json.JSONDecodeError):
                pass
            dispatch_fs.atomic_write(path, record)
        for stale in set(existing) - set(current):
            try:
                existing[stale].unlink()
            except OSError:
                pass

    # -- ledger -----------------------------------------------------------------

    def _seed_ledger_from_backlog(self) -> None:
        """First-run guard: record every message already sitting in a bridged
        nick's inbox as already-handled WITHOUT publishing it. Idempotent-safe:
        only called when no ledger existed yet. Mirrors
        GitBridge._seed_ledger_from_backlog exactly, including always
        persisting even when nothing was seeded — the file's *existence* is
        what latches first_run to false for the next construction."""
        now = time.time()
        for msg in self._local_messages():
            mid = msg.get("id")
            if mid and mid not in self._ledger:
                self._ledger[mid] = now
        self._save_ledger()

    def _load_ledger(self) -> dict[str, float]:
        return dispatch_fs.load_ledger(self._ledger_path, LEDGER_TTL_SECONDS)

    def _save_ledger(self) -> None:
        self._ledger = dispatch_fs.save_ledger(self._ledger_path, self._ledger, LEDGER_TTL_SECONDS)
