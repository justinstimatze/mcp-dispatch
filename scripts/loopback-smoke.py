#!/usr/bin/env python3
"""Single-machine cross-host smoke test for the git transport.

You don't need two machines to prove cross-host dispatch works end to end. This
script simulates two hosts on one box — two independent DISPATCH_DIRs and two
clones of the SAME git bus repo — and round-trips a message between them. It
exercises the whole path the daemon uses: local inbox -> git lane -> push ->
fetch -> materialize into the other host's inbox, including the repo-local
git-identity that CI runners need.

    scripts/loopback-smoke.py                    # self-contained: a local bare repo, no network
    scripts/loopback-smoke.py --repo owner/name  # against a REAL remote (writes test lanes to it!)
    scripts/loopback-smoke.py --keep             # leave the scratch dir for inspection

By default it stands up a throwaway *local* bare repo, so it's fully offline and
safe to run repeatedly — it never touches your live config, your real
DISPATCH_DIR, or any shared bus. Pass --repo only when you specifically want to
prove the real network + auth path; note that leaves test lanes in that repo.

Exit 0 = both directions delivered.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import dispatch_fs  # noqa: E402
from git_bridge import GitBridge  # noqa: E402


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _clone(repo: str, dest: Path) -> None:
    # A local path (our default throwaway bare repo) clones directly; a slug goes
    # through gh so it carries keyring auth for private repos, with a git fallback.
    if Path(repo).exists():
        cp = _run(["git", "clone", "-q", repo, str(dest)])
        if cp.returncode != 0:
            sys.exit(f"clone failed for {repo}: {cp.stderr.strip()}")
        return
    if _run(["gh", "repo", "clone", repo, str(dest)]).returncode != 0:
        cp = _run(["git", "clone", repo, str(dest)])
        if cp.returncode != 0:
            sys.exit(f"clone failed for {repo}: {cp.stderr.strip()}")


def _has_head(clone: Path) -> bool:
    return _run(["git", "-C", str(clone), "rev-parse", "HEAD"]).returncode == 0


def _seed(clone: Path) -> None:
    """Give an empty bus repo its first commit on main so lanes have a branch."""
    (clone / "README.md").write_text("# mcp-dispatch git bus\n\nSeeded by loopback-smoke.\n")
    for args in (
        ["checkout", "-q", "-B", "main"],
        ["add", "README.md"],
        [
            "-c",
            "user.name=loopback",
            "-c",
            "user.email=loopback@mcp-dispatch",
            "commit",
            "-q",
            "-m",
            "seed bus",
        ],
        ["push", "-q", "origin", "HEAD:main"],
    ):
        cp = _run(["git", "-C", str(clone), *args])
        if cp.returncode != 0:
            sys.exit(f"seed step {args[:1]} failed: {cp.stderr.strip()}")


def _sync_to_main(clone: Path) -> None:
    """Point a fresh clone at origin/main (it may have cloned an empty repo)."""
    _run(["git", "-C", str(clone), "fetch", "-q", "origin"])
    _run(["git", "-C", str(clone), "checkout", "-q", "-B", "main", "origin/main"])


def _presence(dispatch_dir: Path, agent_id: str):
    """Create + hold a live presence flock (caller keeps the handle alive)."""
    import fcntl

    pdir = dispatch_dir / ".presence"
    pdir.mkdir(parents=True, exist_ok=True)
    pf = pdir / f"{agent_id}.json"
    pf.write_text(json.dumps({"agent_id": agent_id, "channels": []}))
    fh = open(pf, "a+")  # noqa: SIM115 - held for the test's lifetime
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def _local_dm(dispatch_dir: Path, frm: str, to: str, content: str) -> str:
    """Write a DM into the recipient's inbox exactly as server._send does."""
    mid = f"msg-{uuid.uuid4().hex[:8]}"
    msg = {
        "id": mid,
        "from": frm,
        "to": to,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "priority": "normal",
        "content": content,
        "payload": None,
        "thread_id": None,
        "reply_to": None,
        "ttl": None,
        "must_read": False,
        "state": "pending",
    }
    inbox = dispatch_dir / to
    inbox.mkdir(parents=True, exist_ok=True)
    dispatch_fs.atomic_write(inbox / dispatch_fs.message_filename(frm), msg)
    return mid


def _delivered(dispatch_dir: Path, agent_id: str, mid: str) -> dict | None:
    inbox = dispatch_dir / agent_id
    for f in inbox.glob("*.json") if inbox.is_dir() else []:
        m = json.loads(f.read_text())
        if m.get("id") == mid:
            return m
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Single-machine cross-host git dispatch smoke test.")
    ap.add_argument(
        "--repo",
        default=None,
        help="REAL bus repo slug/URL to test against (writes test lanes to it). "
        "Omit for a self-contained local bare repo (default, no network).",
    )
    ap.add_argument("--keep", action="store_true", help="keep the scratch dir for inspection")
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="dispatch-loopback-"))
    holds: list = []
    a_id, b_id = "hosta-1111", "hostb-2222"
    try:
        dir_a, dir_b = root / "hostA/messages", root / "hostB/messages"
        clone_a, clone_b = root / "hostA/bus", root / "hostB/bus"
        for d in (dir_a, dir_b):
            (d / a_id).mkdir(parents=True, exist_ok=True)

        if args.repo:
            repo = args.repo
            print(f"• cloning REAL remote {repo} twice (two simulated hosts)…")
        else:
            bare = root / "bus.git"
            _run(["git", "init", "-q", "--bare", "-b", "main", str(bare)])
            repo = str(bare)
            print("• standing up a local bare bus repo (offline) + two clones…")
        _clone(repo, clone_a)
        _clone(repo, clone_b)
        if not _has_head(clone_a):
            print("• bus repo is empty — seeding an initial commit on main…")
            _seed(clone_a)
        _sync_to_main(clone_a)
        _sync_to_main(clone_b)

        holds.append(_presence(dir_a, a_id))
        holds.append(_presence(dir_b, b_id))
        bridge_a = GitBridge(dir_a, clone_a, remote="origin")
        bridge_b = GitBridge(dir_b, clone_b, remote="origin")

        print(f"• host A: {a_id} sends a DM to {b_id} (a remote agent)…")
        mid = _local_dm(dir_a, a_id, b_id, "hello from host A over real git")
        bridge_a.tick()  # publish to A's lane + push to GitHub
        bridge_b.tick()  # fetch from GitHub + materialize into host B's inbox
        got = _delivered(dir_b, b_id, mid)
        assert got, "DM did NOT reach host B's inbox"
        assert got.get("_via") == "git", f"expected _via=git, got {got.get('_via')!r}"
        print(f"  ✓ delivered to host B as {mid} (via={got.get('_via')})")

        print(f"• host B: {b_id} replies to {a_id}…")
        rid = _local_dm(dir_b, b_id, a_id, "reply from host B")
        bridge_b.tick()
        bridge_a.tick()
        rep = _delivered(dir_a, a_id, rid)
        assert rep and rep.get("_via") == "git", "reply did NOT reach host A"
        print(f"  ✓ reply delivered to host A as {rid} (via={rep.get('_via')})")

        where = f"real remote {args.repo}" if args.repo else "a local bare repo"
        print(f"\nPASS — bidirectional cross-host dispatch works over {where}.")
        return 0
    except AssertionError as e:
        print(f"\nFAIL — {e}")
        return 1
    finally:
        for fh in holds:
            fh.close()
        if args.keep:
            print(f"(scratch kept at {root})")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
