"""Git-backed transport for mcp-dispatch (v1).

A second transport backend: a git repo as an async, durable, cross-machine,
audited message bus. See docs/git-transport.md for the full design rationale.

Core principle — per-author append-only lanes + scan-on-read:

- Each agent only ever *appends* to one file it owns:
  - DMs:      lanes/<agent_id>.jsonl
  - channels: channels/<chan>/<agent_id>.jsonl
  Because no two agents write the same file, every push is a fast-forward /
  trivially clean merge. No locks, no union-merge dependency.
- Sending = append one JSON line + commit (+ push if a remote is configured).
- Receiving = fetch; read lane files; yield lines past a *reader-local* cursor;
  filter `to == me` or `chan in my subscriptions`. The cursor (a per-lane
  consumed-line count) lives outside the repo, so read-state is private and
  generates zero history.

This module is transport-only and deliberately standalone (it does not import
server.py): it can be unit-tested against a local bare repo over a file://
remote, and later wired behind the same dispatch/peek/ack tool surface.

Scope of v1: publish (DM + channel), receive (event-stream tail), collect
(last-write-wins state snapshot), channels, local cursor. Encryption and
presence are designed seams (body_codec hook below, refs/presence/ in the doc)
but ship default-off / unimplemented in v1 — see docs/git-transport.md.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# Wire schema version. Bump on any incompatible header change.
WIRE_VERSION = 1

# Agent ids / channel names become path segments under the repo, so they must
# be safe single segments (mirrors server.py:_validate_id). No dots, slashes,
# or leading dashes; keeps lane filenames and sparse-checkout globs predictable.
_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$")


def _validate_id(value: str, kind: str = "id") -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f"Invalid {kind}: {value!r} (must match {_ID_RE.pattern})")
    return value


def _now_iso() -> str:
    """UTC timestamp, second resolution, matching server.py's format."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Envelope — the shared wire format (11-field header, all cleartext but body)
# ---------------------------------------------------------------------------


@dataclass
class Envelope:
    """The interop seam: one wire format for messages and for LWW state.

    Field order matches docs/git-transport.md:
        { type, from, to|chan, key, id, ts, seq, ttl, version, sig, body }

    `from_` / `to` are spelled with a trailing/plain name in Python (``from`` is
    a keyword); the on-disk JSON uses ``from`` and ``to``.
    """

    type: str  # record discriminator: message | atom | ack | presence | ...
    from_: str  # author (== lane owner == commit author)
    body: Any  # opaque payload (encryptable). dict for cleartext v1.
    to: str | None = None  # DM recipient id (mutually exclusive with chan)
    chan: str | None = None  # channel name, without leading '#'
    key: str | None = None  # LWW partition key for state consumers; None = event
    id: str = ""  # stable unique id (dedup across re-delivery)
    ts: str = ""  # ISO-8601 UTC send time
    seq: int = 0  # per-lane monotonic sequence (causal order, skew-proof)
    ttl: int | None = None  # seconds; None/0 = never expire
    version: int = WIRE_VERSION
    sig: str | None = None  # signature over the record (reserved in v1)

    def to_json(self) -> str:
        """Serialize to a single JSONL line (no embedded newlines)."""
        d = {
            "type": self.type,
            "from": self.from_,
            "to": self.to,
            "chan": self.chan,
            "key": self.key,
            "id": self.id,
            "ts": self.ts,
            "seq": self.seq,
            "ttl": self.ttl,
            "version": self.version,
            "sig": self.sig,
            "body": self.body,
        }
        return json.dumps(d, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> Envelope:
        d = json.loads(line)
        return cls(
            type=d["type"],
            from_=d["from"],
            body=d.get("body"),
            to=d.get("to"),
            chan=d.get("chan"),
            key=d.get("key"),
            id=d.get("id", ""),
            ts=d.get("ts", ""),
            seq=int(d.get("seq", 0)),
            ttl=d.get("ttl"),
            version=int(d.get("version", WIRE_VERSION)),
            sig=d.get("sig"),
        )


class BodyCodec(Protocol):
    """Encryption seam (default off in v1).

    When set on a GitBus, ``encrypt`` is applied to the body before append and
    ``decrypt`` after read. v1 ships no codec (cleartext); a codec keeps each
    line an independent opaque blob so per-line encryption coexists with the
    append-only lane model. See docs/git-transport.md (worry a).
    """

    def encrypt(self, body: Any, *, to: str | None, chan: str | None) -> Any: ...
    def decrypt(self, body: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Cursor — reader-local read-state, never written to the bus
# ---------------------------------------------------------------------------


@dataclass
class Cursor:
    """Per-lane consumed-line counts, persisted outside the repo.

    Lanes are append-only, so line N is immutable once written; a consumed
    count is a sufficient, compact cursor. (Lane-rewriting compaction — opt-in,
    off by default — would invalidate counts; such a consumer resets its
    cursor. v1 default never compacts, so counts stay valid forever.)
    """

    path: Path
    consumed: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Cursor:
        try:
            data = json.loads(path.read_text())
            return cls(path=path, consumed=dict(data.get("consumed", {})))
        except (OSError, json.JSONDecodeError):
            return cls(path=path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"consumed": self.consumed}, indent=2))
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# GitBus — the transport
# ---------------------------------------------------------------------------


class GitBus:
    """A git repo as an inter-agent message bus.

    repo_dir   working clone the agent reads/writes (its lane lives here).
    agent_id   this agent's identity == its lane filename == commit author.
    remote     optional git remote name to push/fetch (e.g. "origin"). If None,
               the bus is local-only (still useful for tests and single-host).
    state_dir  where the reader-local cursor lives (defaults to repo_dir/.git).
    body_codec optional encryption seam (default None = cleartext).
    """

    def __init__(
        self,
        repo_dir: str | Path,
        agent_id: str,
        *,
        remote: str | None = None,
        state_dir: str | Path | None = None,
        body_codec: BodyCodec | None = None,
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.agent_id = _validate_id(agent_id, "agent_id")
        self.remote = remote
        self.body_codec = body_codec
        state = Path(state_dir) if state_dir else (self.repo_dir / ".git" / "mcp-dispatch")
        self._cursor = Cursor.load(state / f"cursor-{self.agent_id}.json")
        self._subs: set[str] = set()
        # Configure a LOCAL commit identity so commits AND `pull --rebase` succeed
        # even where no global git identity is set (e.g. CI runners). Without it a
        # rebase aborts mid-flight, detaching HEAD — which then poisons the push
        # refspec. Best-effort: a no-op/ignored error if repo_dir isn't a repo yet.
        self._git("config", "user.name", self.agent_id, check=False)
        self._git("config", "user.email", f"{self.agent_id}@mcp-dispatch", check=False)

    # -- git plumbing -------------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> str:
        """Run a git command in the repo, returning stdout."""
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    # -- lane paths ---------------------------------------------------------

    def _dm_lane(self, agent_id: str) -> Path:
        return self.repo_dir / "lanes" / f"{agent_id}.jsonl"

    def _chan_lane(self, chan: str, agent_id: str) -> Path:
        return self.repo_dir / "channels" / chan / f"{agent_id}.jsonl"

    def _my_lane(self, chan: str | None) -> Path:
        return self._chan_lane(chan, self.agent_id) if chan else self._dm_lane(self.agent_id)

    # -- sequence -----------------------------------------------------------

    def _next_seq(self, lane: Path) -> int:
        """Next per-lane seq = number of records already in my lane.

        Derived from the lane itself (not stored separately) so it can never
        drift from on-disk reality.
        """
        if not lane.exists():
            return 0
        with lane.open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    # -- subscriptions ------------------------------------------------------

    def subscribe(self, chan: str) -> None:
        self._subs.add(_validate_id(chan, "channel"))

    def unsubscribe(self, chan: str) -> None:
        self._subs.discard(chan)

    # -- publish ------------------------------------------------------------

    def publish(
        self,
        body: Any,
        *,
        to: str | None = None,
        chan: str | None = None,
        type: str = "message",
        key: str | None = None,
        ttl: int | None = None,
        push: bool = True,
    ) -> Envelope:
        """Append one record to my own lane, commit, and (optionally) push.

        Exactly one of ``to`` (DM) or ``chan`` (channel) must be set.
        """
        if (to is None) == (chan is None):
            raise ValueError("publish requires exactly one of `to` or `chan`")
        if to is not None:
            _validate_id(to, "recipient")
        if chan is not None:
            _validate_id(chan, "channel")
        if key is not None:
            # key is freeform partition identity, not a path segment, but keep
            # it sane: no newlines (would corrupt the JSONL line).
            if "\n" in key or "\r" in key:
                raise ValueError("key must not contain newlines")

        lane = self._my_lane(chan)
        out_body = body
        if self.body_codec is not None:
            out_body = self.body_codec.encrypt(body, to=to, chan=chan)

        env = Envelope(
            type=type,
            from_=self.agent_id,
            to=to,
            chan=chan,
            key=key,
            body=out_body,
            id=f"rec-{uuid.uuid4().hex[:12]}",
            ts=_now_iso(),
            seq=self._next_seq(lane),
            ttl=ttl,
        )

        lane.parent.mkdir(parents=True, exist_ok=True)
        with lane.open("a", encoding="utf-8") as f:
            f.write(env.to_json() + "\n")

        rel = lane.relative_to(self.repo_dir).as_posix()
        self._git("add", "--", rel)
        self._git(
            "-c",
            f"user.name={self.agent_id}",
            "-c",
            f"user.email={self.agent_id}@mcp-dispatch",
            "commit",
            "-q",
            "-m",
            f"{type} {env.id} from {self.agent_id}" + (f" to {to}" if to else f" in #{chan}"),
        )
        if push:
            self.push()
        return env

    def push(self) -> None:
        """Pull-rebase then push HEAD to the remote, retrying the race.

        Lanes are per-author single-writer so the rebase never *conflicts*, but a
        sibling's concurrent push can still leave ours non-fast-forward; absorb
        that by re-pulling and retrying a bounded number of times. (A single-shot
        push loses this race — the bug leat's CI caught and fixed with the same
        loop.) No-op when the bus is local-only.
        """
        if not self.remote:
            return
        branch = self._branch()
        last_err = ""
        for _ in range(6):
            self._git("pull", "--rebase", "-q", self.remote, branch, check=False)
            proc = subprocess.run(
                ["git", "push", "-q", self.remote, f"HEAD:{branch}"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                return
            last_err = proc.stderr.strip()
        raise RuntimeError(f"push to {self.remote}/{branch} failed after retries: {last_err}")

    def _branch(self) -> str:
        b = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        # "HEAD" means a detached state (e.g. a rebase mid-flight); never push to
        # the literal ref "HEAD" — fall back to the default branch name.
        return b if b and b != "HEAD" else "main"

    # -- fetch --------------------------------------------------------------

    def fetch(self) -> None:
        """Pull remote updates (fast-forward; lanes never conflict)."""
        if not self.remote:
            return
        self._git("fetch", "-q", self.remote, check=False)
        self._git("merge", "-q", "--ff-only", f"{self.remote}/{self._branch()}", check=False)

    # -- receive (event-stream tail) ---------------------------------------

    def _all_lane_files(self) -> list[Path]:
        files: list[Path] = []
        lanes_dir = self.repo_dir / "lanes"
        if lanes_dir.is_dir():
            files.extend(sorted(lanes_dir.glob("*.jsonl")))
        chan_root = self.repo_dir / "channels"
        if chan_root.is_dir():
            files.extend(sorted(chan_root.glob("*/*.jsonl")))
        return files

    def _read_new_lines(self, lane: Path) -> list[Envelope]:
        """Lines in `lane` past the reader cursor; advances the cursor."""
        rel = lane.relative_to(self.repo_dir).as_posix()
        start = self._cursor.consumed.get(rel, 0)
        out: list[Envelope] = []
        consumed = start
        with lane.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < start:
                    continue
                consumed = i + 1
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Envelope.from_json(line))
                except (json.JSONDecodeError, KeyError):
                    # A malformed line shouldn't wedge the whole lane; skip it
                    # but still advance past it so we don't re-read it forever.
                    continue
        self._cursor.consumed[rel] = consumed
        return out

    def receive(self, *, fetch: bool = True) -> list[Envelope]:
        """Return new records addressed to me since the last receive.

        Advances and persists the cursor. Filters to DMs where `to == me` and
        channel posts in my subscriptions. Excludes my own records.
        """
        if fetch:
            self.fetch()
        results: list[Envelope] = []
        for lane in self._all_lane_files():
            for env in self._read_new_lines(lane):
                if env.from_ == self.agent_id:
                    continue
                if env.chan is not None:
                    if env.chan in self._subs:
                        results.append(self._decode(env))
                elif env.to == self.agent_id:
                    results.append(self._decode(env))
        self._cursor.save()
        # Stable order across lanes: by (ts, from, seq). Within a lane seq is
        # authoritative; across lanes we have no global clock (see doc).
        results.sort(key=lambda e: (e.ts, e.from_, e.seq))
        return results

    def drain(self, *, fetch: bool = True) -> list[Envelope]:
        """All new records across every lane past the reader cursor.

        Like ``receive()`` but WITHOUT the ``to == me`` self-filter: the
        replicator daemon (git_bridge.py) wants every new record so it can route
        each one by ``to``/``chan`` into the right local inbox itself. Still skips
        this reader's own lane (we never re-ingest what we authored) and
        advances+persists the cursor. Guarding against re-ingesting records this
        host *published on behalf of others* relies on the daemon's msg-id dedup
        against the recipient inbox, not on lane exclusion here.
        """
        if fetch:
            self.fetch()
        results: list[Envelope] = []
        for lane in self._all_lane_files():
            for env in self._read_new_lines(lane):
                if env.from_ == self.agent_id:
                    continue
                results.append(self._decode(env))
        self._cursor.save()
        # Same cross-lane ordering as receive(): no global clock, so (ts, from, seq).
        results.sort(key=lambda e: (e.ts, e.from_, e.seq))
        return results

    def _decode(self, env: Envelope) -> Envelope:
        if self.body_codec is not None:
            env.body = self.body_codec.decrypt(env.body)
        return env

    # -- collect (state snapshot) ------------------------------------------

    def collect(self, *, type_filter: str | None = None, fetch: bool = True) -> list[Envelope]:
        """Latest record per (author, key) — the LWW state-snapshot view.

        Reads full lanes (not the cursor tail), folds state. For event-stream
        records `key` is None, so the partition is per-author. State consumers
        (ettle's atoms) set `key` to the slot identity, giving per-slot LWW.
        Does not touch or advance the receive cursor.
        """
        if fetch:
            self.fetch()
        latest: dict[tuple[str, str | None], Envelope] = {}
        for lane in self._all_lane_files():
            with lane.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        env = Envelope.from_json(line)
                    except (json.JSONDecodeError, KeyError):
                        continue
                    if type_filter is not None and env.type != type_filter:
                        continue
                    pk = (env.from_, env.key)
                    cur = latest.get(pk)
                    if cur is None or (env.seq, env.ts) > (cur.seq, cur.ts):
                        latest[pk] = env
        return [self._decode(e) for e in latest.values()]
