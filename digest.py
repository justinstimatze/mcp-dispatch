"""What happened while I was away.

A session that starts after a gap — and, now that the lifecycle supervisor
exists, a session that starts because *mail arrived* with nobody watching —
begins knowing only what is in its inbox. Everything else that moved while it
was gone (tasks claimed, teammates active, work finished) is on disk and unread,
because nothing ever assembled it into an answer.

This module assembles it. Given a nick and a window, it reports what changed.

The window
----------
It starts at the nick's ``previous_seen``: the value of ``last_seen`` at the
moment the current session claimed its id. ``server._release_id`` stamps
``last_seen`` on the way out, before dropping the presence lock, so on a clean
exit that really is the last moment the nick was present.

On an *unclean* exit — SIGKILL, a crash, a reboot — atexit never ran, so
``last_seen`` is older than the true end of the session and the window is too
wide: the digest re-shows some things the previous session already saw. That is
the safe direction to be wrong in, and it is reported rather than hidden
(``window_exact``), because a digest that silently under-reports is worse than
no digest at all — you would stop looking.

Reading is not consuming. There is no cursor to advance and nothing is deleted,
so the digest is a pure function of (relay, nick, window): asking twice gives
the same answer, and a session that crashes mid-read loses nothing. A read
cursor would be at-most-once delivery for exactly the content you cannot afford
to drop.

The channel gap
---------------
Channel posts fan out to *live subscribers only* — that is what makes channels
free of separate state. The consequence is that a post made while this nick was
offline left no local trace at all: not unread, simply absent. So the digest
cannot report missed channel traffic from the local relay, and says so rather
than printing an empty section that reads like "nothing happened". The git
transport's append-only lanes do retain it, which is where that section will
come from — see ``channel_gap_note``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import dispatch_fs


@dataclass
class MailItem:
    sender: str
    count: int
    urgent: int = 0
    must_read: int = 0
    newest: str = ""


@dataclass
class TaskEvent:
    kind: str  # "created" | "claimed" | "done"
    task_id: str
    title: str
    who: str
    at: str
    target: str | None = None
    mine: bool = False


@dataclass
class Digest:
    nick: str
    since: str
    now: str
    window_exact: bool = True
    mail: list[MailItem] = field(default_factory=list)
    tasks: list[TaskEvent] = field(default_factory=list)
    open_tasks_for_me: list[TaskEvent] = field(default_factory=list)
    active_while_away: list[str] = field(default_factory=list)
    live_now: list[str] = field(default_factory=list)
    channel_gap: bool = True

    @property
    def mail_total(self) -> int:
        return sum(m.count for m in self.mail)

    @property
    def empty(self) -> bool:
        return not (self.mail or self.tasks or self.open_tasks_for_me or self.active_while_away)


def _in_window(ts: str, since: str, now: str) -> bool:
    """Half-open (since, now]. String compare is correct here: every timestamp in
    the relay is the same fixed-width UTC ``%Y-%m-%dT%H:%M:%SZ`` format, so
    lexicographic order is chronological order — and unlike parsing, an
    unreadable value simply sorts out rather than raising."""
    return bool(ts) and since < ts <= now


def _nick_inboxes(dispatch_dir: Path, nick: str) -> list[Path]:
    """Every inbox belonging to this nick — its own drop box and each session's.

    Unlike the supervisor's view, liveness does not matter: a live session's
    inbox is exactly where this session's own unread mail is.
    """
    out = []
    for d in sorted(dispatch_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name == nick or dispatch_fs.durable_nick(d.name) == nick:
            out.append(d)
    return out


def _collect_mail(dispatch_dir: Path, nick: str) -> list[MailItem]:
    """Pending mail grouped by sender.

    Deliberately window-independent. Unread mail is unread whenever it arrived,
    and a message that predates the window is *more* overdue, not less.
    """
    by_sender: dict[str, MailItem] = {}
    for d in _nick_inboxes(dispatch_dir, nick):
        for f in sorted(d.glob("*.json")):
            try:
                msg = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("state", "pending") != "pending" or dispatch_fs.is_expired(msg):
                continue
            sender = str(msg.get("from") or "?")
            item = by_sender.setdefault(sender, MailItem(sender=sender, count=0))
            item.count += 1
            if msg.get("priority") == "urgent":
                item.urgent += 1
            if msg.get("must_read"):
                item.must_read += 1
            ts = str(msg.get("timestamp") or "")
            if ts > item.newest:
                item.newest = ts
    return sorted(by_sender.values(), key=lambda m: (-m.urgent, -m.count, m.sender))


def _collect_tasks(
    dispatch_dir: Path, nick: str, since: str, now: str, channels: set[str]
) -> tuple[list[TaskEvent], list[TaskEvent]]:
    """(events in the window, open tasks addressed to me).

    A task's record carries all three of its timestamps, so one pass over the
    store yields every transition without needing an event log.
    """
    events: list[TaskEvent] = []
    open_mine: list[TaskEvent] = []
    tasks_dir = dispatch_dir / ".tasks"
    if not tasks_dir.is_dir():
        return events, open_mine

    for f in sorted(tasks_dir.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(rec, dict):
            continue
        tid = str(rec.get("id") or f.stem)
        title = str(rec.get("title") or "")
        target = rec.get("target")
        # "Mine" means addressed to me by nick or to a room I stand in. A task
        # with no target is addressed to whoever picks it up, so it is not mine
        # in particular — it shows up as open work, not as a personal ping.
        mine = bool(
            target
            and (
                target == nick
                or dispatch_fs.durable_nick(str(target)) == nick
                or (str(target).startswith("#") and str(target)[1:] in channels)
            )
        )

        for kind, at, who in (
            ("created", rec.get("created_at"), rec.get("created_by")),
            ("claimed", rec.get("claimed_at"), rec.get("claimed_by")),
            ("done", rec.get("done_at"), rec.get("claimed_by")),
        ):
            if _in_window(str(at or ""), since, now):
                events.append(TaskEvent(kind, tid, title, str(who or "?"), str(at), target, mine))

        if rec.get("state") == "open" and mine:
            open_mine.append(
                TaskEvent(
                    "open",
                    tid,
                    title,
                    str(rec.get("created_by") or "?"),
                    str(rec.get("created_at") or ""),
                    target,
                    True,
                )
            )

    events.sort(key=lambda e: e.at)
    return events, open_mine


def _collect_teammates(dispatch_dir: Path, nick: str, since: str, now: str) -> list[str]:
    """Nicks that were present during the window — the people you missed."""
    out: list[str] = []
    reg = dispatch_dir / ".agents"
    if not reg.is_dir():
        return out
    for f in sorted(reg.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(rec, dict):
            continue
        other = str(rec.get("nick") or f.stem)
        if other == nick:
            continue
        if _in_window(str(rec.get("last_seen") or ""), since, now):
            out.append(other)
    return out


def watermark(dispatch_dir: Path, nick: str) -> tuple[str, bool]:
    """(window start, whether it is exact) for ``nick``.

    Falls back to ``first_seen`` for a nick that has never had a second session,
    and reports inexact when there is nothing to anchor to — a caller that treats
    a guessed window as measured would over-claim.
    """
    rec_path = dispatch_dir / ".agents" / f"{nick}.json"
    try:
        rec = json.loads(rec_path.read_text())
    except (json.JSONDecodeError, OSError):
        return "", False
    if not isinstance(rec, dict):
        return "", False
    prev = rec.get("previous_seen")
    if prev:
        return str(prev), True
    # No previous_seen: either a first-ever session, or a record written before
    # this feature existed. first_seen is the widest honest window.
    return str(rec.get("first_seen") or ""), False


def build(
    dispatch_dir: Path,
    nick: str,
    *,
    now: str,
    since: str | None = None,
    channels: set[str] | None = None,
) -> Digest:
    """Assemble the digest for ``nick``. Reads only; advances no cursor."""
    exact = True
    if since is None:
        since, exact = watermark(dispatch_dir, nick)
    if not dispatch_dir.is_dir():
        return Digest(nick=nick, since=since, now=now, window_exact=exact)

    chans = channels or set()
    events, open_mine = _collect_tasks(dispatch_dir, nick, since, now, chans)
    return Digest(
        nick=nick,
        since=since,
        now=now,
        window_exact=exact,
        mail=_collect_mail(dispatch_dir, nick),
        tasks=events,
        open_tasks_for_me=open_mine,
        active_while_away=_collect_teammates(dispatch_dir, nick, since, now),
        live_now=sorted(dispatch_fs.live_nicks(dispatch_dir) - {nick}),
    )


def channel_gap_note() -> str:
    return (
        "Channel posts reach live subscribers only, so anything posted to a room "
        "while you were offline left no local record — it is absent, not unread. "
        "The git transport's lanes do retain it; this digest does not read them yet."
    )


def render(d: Digest) -> str:
    """Human- and model-readable. One screen, most actionable first."""
    lines: list[str] = []
    window = f"since {d.since}" if d.since else "since first contact"
    if not d.window_exact:
        window += " (approximate — no clean end-of-session mark to anchor to)"
    lines.append(f"While {d.nick} was away — {window}")

    if d.empty:
        lines.append("  Nothing waiting and nothing moved.")
    if d.mail:
        lines.append(f"\n  Unread mail ({d.mail_total}):")
        for m in d.mail:
            flags = ""
            if m.must_read:
                flags += f" {m.must_read}🔒"
            if m.urgent:
                flags += f" {m.urgent}‼"
            lines.append(f"    {m.sender}: {m.count}{flags}")

    if d.open_tasks_for_me:
        lines.append("\n  Open tasks addressed to you:")
        for t in d.open_tasks_for_me:
            lines.append(f"    {t.task_id}  {t.title}  (from {t.who}, to {t.target})")

    if d.tasks:
        lines.append("\n  Task activity:")
        for t in d.tasks:
            mark = " ←you" if t.mine else ""
            lines.append(f"    {t.at}  {t.kind:<7} {t.task_id}  {t.title}  by {t.who}{mark}")

    if d.active_while_away:
        lines.append(f"\n  Active while you were gone: {', '.join(d.active_while_away)}")
    if d.live_now:
        lines.append(f"  Live right now: {', '.join(d.live_now)}")

    if d.channel_gap:
        lines.append(f"\n  Note: {channel_gap_note()}")
    return "\n".join(lines)
