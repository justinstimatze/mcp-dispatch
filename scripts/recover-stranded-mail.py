#!/usr/bin/env python3
"""Re-home mail stranded in spools that inheritance can never adopt.

Scope, deliberately narrow — this moves real mail:

  - the donor spool has no live presence (nobody is reading it);
  - the donor is listed in .remote/, i.e. it is one of the spools the
    presence-vs-registry bug reclassified as another host's. Spools NOT listed
    there are already adoptable by _inherit_orphan_inbox and are left alone;
  - the donor name has a pid suffix, so the nick behind it is well defined;
  - the donor is owned by us (the same uid check _inherit_orphan_inbox makes);
  - the message state is `pending` — read, acked and expired records stay put;
  - the destination is the nick's own inbox, which _inherit_orphan_inbox already
    treats as a donor, so the nick's next session collects it normally;
  - the nick itself is not in .remote/, which would make the destination just as
    unreachable as the source.

Run with --apply to move. Default is a dry run.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

RELAY = Path(os.environ.get("MCP_DISPATCH_DIR", "/var/tmp/mcp-dispatch"))
PID_SUFFIX = re.compile(r"^(?P<nick>.+)-\d+$")
APPLY = "--apply" in sys.argv


def live(name: str) -> bool:
    """A presence file with no holdable flock is a corpse, not a reader."""
    import fcntl

    pf = RELAY / ".presence" / f"{name}.json"
    if not pf.exists():
        return False
    try:
        fh = open(pf)
    except OSError:
        return True  # can't tell; assume someone is home
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def main() -> int:
    remote = {p.stem for p in (RELAY / ".remote").glob("*.json")}
    moves: list[tuple[Path, Path]] = []
    skipped: list[str] = []

    for d in sorted(RELAY.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name not in remote:
            continue  # already adoptable — not this script's business
        m = PID_SUFFIX.match(d.name)
        if not m:
            skipped.append(f"{d.name}: no pid suffix, nick undefined")
            continue
        nick = m.group("nick")
        if nick in remote:
            skipped.append(f"{d.name}: nick '{nick}' is itself remote")
            continue
        if live(d.name):
            skipped.append(f"{d.name}: live, leave it alone")
            continue
        try:
            if d.stat().st_uid != os.getuid():
                skipped.append(f"{d.name}: another account's mail")
                continue
        except OSError:
            continue
        for f in sorted(d.glob("*.json")):
            try:
                msg = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if msg.get("state", "pending") != "pending":
                continue
            moves.append((f, RELAY / nick / f.name))

    for src, dst in moves:
        rel = f"{src.parent.name}/{src.name}"
        try:
            msg = json.loads(src.read_text())
        except (json.JSONDecodeError, OSError):
            msg = {}
        who = msg.get("from", "?")
        when = msg.get("timestamp", "?")
        print(f"  {rel}\n    from {who} at {when}  ->  {dst.parent.name}/")
        if not APPLY:
            continue
        if dst.exists():
            print("    SKIP: destination exists")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        msg["_recovered_from"] = src.parent.name
        tmp = dst.with_suffix(".tmp")
        tmp.write_text(json.dumps(msg, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
        src.unlink()

    for s in skipped:
        print(f"  (skipped) {s}")
    verb = "moved" if APPLY else "would move"
    print(f"\n{verb} {len(moves)} pending message(s) out of unreachable spools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
