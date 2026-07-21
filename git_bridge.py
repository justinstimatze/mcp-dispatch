"""GitBridge — a bidirectional replicator between the local bus and git.

mcp-dispatch is a local-only file bus: ``dispatch(target=T)`` drops a JSON file in
``DISPATCH_DIR/T/`` and T's server reads its own inbox. That's invisible across
machines. GitBridge proxies that local replica to a git repo (``git_transport``'s
per-author append-only lanes), so agents on other hosts reach each other with
*zero* change to how they use the tool — the daemon, not ``_send``, owns all git
knowledge.

The trick that keeps DX free: a record arriving over git is *materialized as a
normal inbox file*. The whole existing wake/notify path (watcher, notifier,
``dispatch-wait``) keys on "a ``.json`` appeared in my inbox", so a cross-host
message wakes a parked session through the identical path a local one does.

Each ``tick()`` runs two passes:

- **outbound** — scan local inboxes, publish not-yet-mirrored messages to the
  *sender's* git lane (one record per logical message), then one push.
- **inbound** — drain new git records and materialize each into the right local
  inbox(es), skipping anything already present (the dedup guard).

Two guards keep it from looping: outbound skips files tagged ``_via:"git"`` (they
arrived over git); inbound skips a record whose original ``msg-id`` is already in
the recipient inbox (on the sender's own host that's the local original, so the
daemon never re-delivers what it just published).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import dispatch_fs
from dispatch_fs import ID_RE
from git_transport import Envelope, GitBus

# Reader identity for the inbound drain. Leads with '_' (valid for GitBus, which
# never collides with a real lowercase agent id) and never authors a lane.
READER_ID = "_gitsync"

# Outbound-ledger entries older than this are pruned: once the source inbox file
# has expired (default ttl ~1 week) we can never re-see it, so the entry is moot.
LEDGER_TTL_SECONDS = 14 * 24 * 3600


class GitBridge:
    """Mirror one ``DISPATCH_DIR`` to/from a git ``repo_dir`` clone.

    mirror = "remote-only" (default): only bridge a DM whose recipient is not a
    live-local agent (the local bus already delivered live-local ones).
    mirror = "all": bridge every DM regardless — a full audited cross-host replica.
    Channels always bridge (inherently multi-host).
    """

    def __init__(
        self,
        dispatch_dir: str | Path,
        repo_dir: str | Path,
        *,
        remote: str | None = None,
        mirror: str = "remote-only",
        group_mode: bool = False,
        state_dir: str | Path | None = None,
        poll_interval: float = 2.0,
        max_fetch_interval: float = 0.0,
    ) -> None:
        # Inbound pacing. `git fetch` is by far the most expensive thing a tick does
        # — ~170ms of CPU against a real remote, versus ~12ms for the entire local
        # scan — so a daemon that runs 24/7 spends essentially all of its CPU asking
        # a quiet remote whether anything happened. max_fetch_interval > 0 lets the
        # fetch cadence decay toward that ceiling while the bus stays silent, and
        # snap back to poll_interval the instant there is traffic in EITHER
        # direction (a local send makes an inbound reply likely). 0 keeps the old
        # fetch-every-tick behaviour, which is what tick()'s contract implies and
        # what --once and the tests want.
        self._poll_interval = max(0.05, poll_interval)
        self._max_fetch_interval = max(0.0, max_fetch_interval)
        self._fetch_every = self._poll_interval
        self._next_fetch = 0.0  # monotonic deadline; 0 => fetch on the very first tick
        self.dispatch_dir = Path(dispatch_dir)
        self.repo_dir = Path(repo_dir)
        self.remote = remote
        if mirror not in ("remote-only", "all"):
            raise ValueError(f"mirror must be 'remote-only' or 'all', got {mirror!r}")
        self.mirror = mirror
        self.group_mode = group_mode
        if group_mode:
            # Match server.py so materialized files land group-readable (0660).
            os.umask(0o007)
        self._state = Path(state_dir) if state_dir else (self.repo_dir / ".git" / "mcp-dispatch")
        self._reader = GitBus(repo_dir, READER_ID, remote=remote, state_dir=self._state)
        self._writers: dict[str, GitBus] = {}
        self._ledger_path = self._state / "gitsync-outbound.json"
        # "Bridge from now on": if no ledger exists yet (git was just enabled), the
        # messages already sitting in local inboxes are pre-existing backlog, NOT
        # traffic to sync. Snapshot their ids as already-handled so the first tick
        # doesn't dump weeks of history to git. Only messages arriving *after* this
        # construction (daemon start) bridge. Captured at __init__ so a message that
        # lands between start and the first tick still counts as new.
        first_run = not self._ledger_path.exists()
        self._ledger = self._load_ledger()
        if first_run:
            self._seed_ledger_from_backlog()

    # -- public surface -----------------------------------------------------

    def tick(self) -> None:
        """Run one outbound pass, one inbound pass, then refresh the remote roster.

        A backed-off tick (see ``max_fetch_interval``) skips BOTH the inbound pass
        and the roster refresh, since the roster is derived from the lanes a fetch
        would have updated. Outbound always runs, so *sending* is never slowed by a
        quiet bus — only noticing the first message after a lull is.
        """
        published = self._outbound()
        if published:
            self._quicken()  # a send makes a reply likely — listen closely again
        if not self._fetch_due():
            return
        received = self._inbound()
        if received or published:
            self._quicken()
        else:
            self._slow_down()
        self._write_remote_roster()

    # -- inbound pacing -----------------------------------------------------

    def _fetch_due(self) -> bool:
        if not self._max_fetch_interval:
            return True  # pacing disabled: every tick fetches, as before
        return time.monotonic() >= self._next_fetch

    def _quicken(self) -> None:
        self._fetch_every = self._poll_interval
        self._next_fetch = 0.0

    def _slow_down(self) -> None:
        self._fetch_every = min(self._fetch_every * 2, self._max_fetch_interval)
        self._next_fetch = time.monotonic() + self._fetch_every

    def tick_guarded(self) -> bool:
        """tick() that never raises: a transient git/network error (laptop asleep,
        VPN dropped) logs and is retried next pass instead of killing the daemon.
        Returns True on a clean pass, False if it swallowed an error."""
        try:
            self.tick()
            return True
        except Exception as e:  # noqa: BLE001 - daemon resilience is the whole point
            print(f"[gitsync] sync pass failed (will retry): {e}", file=sys.stderr, flush=True)
            return False

    def flush_pending(self) -> bool:
        """Push lane records the remote is missing because a PRIOR run committed
        them locally but couldn't push (a network/auth/identity outage froze the
        remote while the daemon kept committing). Called once at daemon start so a
        RESTART ALONE recovers a frozen remote — without waiting for the next
        outbound message to trigger a push. No-op with no remote or an up-to-date
        remote; never raises (a still-broken remote just retries on the next
        push). This is the recovery path for the 'remote lane stuck at an old
        date while agents keep talking' symptom."""
        if not self.remote:
            return False
        try:
            self._reader.push()
            return True
        except Exception as e:  # noqa: BLE001 - startup best-effort; real pushes retry
            print(
                f"[gitsync] startup catch-up push failed (will retry on next send): {e}",
                file=sys.stderr,
                flush=True,
            )
            return False

    def run(self, interval: float) -> None:  # pragma: no cover - thin loop
        """Tick forever (resiliently), sleeping ``interval`` seconds between passes."""
        while True:
            self.tick_guarded()
            time.sleep(interval)

    # -- outbound: local inbox -> git lane ----------------------------------

    def _local_messages(self):
        """Yield each locally-originated (non-``_via:git``) inbox message dict,
        across every inbox. The single scan both ``_outbound`` and the first-run
        ``_seed_ledger_from_backlog`` share, so their skip logic can't drift."""
        for inbox in self._inbox_dirs():
            for f in sorted(inbox.glob("*.json")):
                try:
                    msg = json.loads(f.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if msg.get("_via") == "git":
                    continue  # arrived over git — never echo back
                yield msg

    def _outbound(self) -> int:
        """Publish new local messages to git. Returns how many were sent."""
        published = 0
        for msg in self._local_messages():
            mid = msg.get("id")
            if not mid or mid in self._ledger:
                continue
            # One routing decision per logical message; ledger it either way so
            # live-local / broadcast messages aren't re-examined every tick and a
            # fan-out's N inbox copies publish exactly once.
            if self._publish_one(msg):
                published += 1
            self._ledger[mid] = time.time()
        if published:
            self._save_ledger()
            if self.remote:
                self._reader.push()
        return published

    def _publish_one(self, msg: dict) -> bool:
        """Publish a local message to its sender's git lane. Returns True if sent."""
        to = msg.get("to")
        sender = msg.get("from")
        if not to or to == "all":
            return False  # broadcast == "every live agent on THIS host"; not bridged
        if not sender or not ID_RE.match(sender):
            return False
        kw = dispatch_fs.msg_to_publish_kwargs(msg)
        if to.startswith("#"):
            chan = to[1:]
            if not ID_RE.match(chan):
                return False
            self._writer(sender).publish(chan=chan, push=False, **kw)
            return True
        if not ID_RE.match(to):
            return False
        if self.mirror == "remote-only" and to in self._live_local():
            return False  # local bus already delivered it
        self._writer(sender).publish(to=to, push=False, **kw)
        return True

    # -- inbound: git lane -> local inbox -----------------------------------

    def _inbound(self) -> int:
        """Materialize new git records into local inboxes. Returns how many landed."""
        materialized = 0
        present: dict[str, set[str]] = {}  # recipient -> msg-ids already in its inbox

        def known(rcpt: str) -> set[str]:
            if rcpt not in present:
                present[rcpt] = self._inbox_ids(rcpt)
            return present[rcpt]

        for env in self._reader.drain(fetch=True):
            body = env.body
            if not isinstance(body, dict):
                continue  # only cleartext message bodies are deliverable in v1
            mid = body.get("id")
            for rcpt in self._recipients(env):
                if not ID_RE.match(rcpt):
                    continue
                if mid and mid in known(rcpt):
                    continue  # dedup: local original, or already materialized
                self._materialize(rcpt, env)
                materialized += 1
                if mid:
                    known(rcpt).add(mid)
        return materialized

    def _recipients(self, env: Envelope) -> list[str]:
        # Never deliver a record back to its own author (a channel post must not
        # echo to the sender if they happen to be a local subscriber).
        if env.chan:
            subs = dispatch_fs.channel_subscribers(self.dispatch_dir, env.chan)
            return [s for s in subs if s != env.from_]
        if env.to and env.to != env.from_:
            return [env.to]
        return []

    def _materialize(self, rcpt: str, env: Envelope) -> None:
        inbox = self.dispatch_dir / rcpt
        inbox.mkdir(parents=True, exist_ok=True)
        msg = dispatch_fs.envelope_to_msg(env)
        raw_from = str(msg.get("from", ""))
        sender = raw_from if ID_RE.match(raw_from) else "unknown"
        dispatch_fs.atomic_write(inbox / dispatch_fs.message_filename(sender), msg)

    # -- helpers ------------------------------------------------------------

    def _inbox_dirs(self) -> list[Path]:
        try:
            entries = sorted(self.dispatch_dir.iterdir())
        except OSError:
            return []
        return [d for d in entries if d.is_dir() and not d.name.startswith(".")]

    def _inbox_ids(self, rcpt: str) -> set[str]:
        inbox = self.dispatch_dir / rcpt
        ids: set[str] = set()
        if not inbox.is_dir():
            return ids
        for f in inbox.glob("*.json"):
            try:
                mid = json.loads(f.read_text()).get("id")
            except (json.JSONDecodeError, OSError):
                continue
            if mid:
                ids.add(mid)
        return ids

    def _live_local(self) -> set[str]:
        return set(dispatch_fs.live_agents(self.dispatch_dir))

    # -- remote roster (churn-free cross-host presence) ----------------------

    def _local_ids(self) -> set[str]:
        """Ids that belong to THIS host: anything with a presence file (live or
        not). A presence file is the durable 'this id had a session here' marker —
        unlike an inbox dir, which a remote recipient also gets from _send."""
        pres = self.dispatch_dir / ".presence"
        if not pres.is_dir():
            return set()
        return {p.stem for p in pres.glob("*.json") if ID_RE.match(p.stem)}

    def _write_remote_roster(self) -> None:
        """Materialize cross-host reachability into DISPATCH_DIR/.remote/ so who()
        can show remote agents — derived from lane *existence* (durable delivery
        means an agent with a lane is reachable even if offline now), NOT a
        heartbeat. The daemon, already the git-aware process, bridges presence the
        same way it bridges messages; who() stays git-agnostic."""
        lanes_dir = self.repo_dir / "lanes"
        roster_dir = self.dispatch_dir / ".remote"
        local = self._local_ids()
        current: dict[str, str | None] = {}
        if lanes_dir.is_dir():
            for lane in lanes_dir.glob("*.jsonl"):
                author = lane.stem
                if not ID_RE.match(author) or author in local:
                    continue
                current[author] = self._last_ts(lane)
        roster_dir.mkdir(parents=True, exist_ok=True)
        existing = {p.stem: p for p in roster_dir.glob("*.json")}
        for author, last_seen in current.items():
            path = roster_dir / f"{author}.json"
            record = {"agent_id": author, "via": "git", "last_seen": last_seen}
            # Only write on change. The roster is derived from lane *existence*, so
            # in the steady state every entry is byte-identical to what's already on
            # disk — rewriting them unconditionally meant one write+rename per known
            # remote agent per tick, forever, for no observable difference.
            try:
                if json.loads(path.read_text()) == record:
                    continue
            except (OSError, json.JSONDecodeError):
                pass
            dispatch_fs.atomic_write(path, record)
        for stale in set(existing) - set(current):  # self-pruning each pass
            try:
                existing[stale].unlink()
            except OSError:
                pass

    def _last_ts(self, lane: Path) -> str | None:
        """Send time of the last record in a lane (a cheap last-seen proxy)."""
        last = ""
        try:
            with lane.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last = line
        except OSError:
            return None
        if not last:
            return None
        try:
            return Envelope.from_json(last).ts
        except (json.JSONDecodeError, KeyError):
            return None

    def _writer(self, sender: str) -> GitBus:
        bus = self._writers.get(sender)
        if bus is None:
            bus = GitBus(self.repo_dir, sender, remote=self.remote, state_dir=self._state)
            self._writers[sender] = bus
        return bus

    # -- ledger -------------------------------------------------------------

    def _seed_ledger_from_backlog(self) -> None:
        """First-run guard: record every message currently in local inboxes as
        already-handled WITHOUT publishing it, so enabling the bridge on a busy
        relay means 'bridge from now on' rather than dumping the whole pre-existing
        backlog to git. Idempotent-safe: only called when no ledger existed yet.

        Always persists the ledger, even when nothing was seeded (an empty relay):
        the file's *existence* is what latches ``first_run`` to false. Without this,
        a bridge first constructed on a quiet relay never writes the marker, so the
        NEXT bridge (a ``--once``/cron pass, or a daemon restarted by a fresh
        SessionStart) is first_run again and would seed messages that arrived in the
        meantime as backlog — silently dropping them instead of bridging."""
        now = time.time()
        for msg in self._local_messages():
            mid = msg.get("id")
            if mid and mid not in self._ledger:
                self._ledger[mid] = now
        self._save_ledger()

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
