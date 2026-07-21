#!/usr/bin/env python3
"""One-command setup for mcp-dispatch — wire it into Claude Code, hands-free.

Getting dispatch working means three separate bits of config that are easy to
miss one of: register the MCP server, then wire the SessionStart/Stop hooks that
(a) arm the wake-watcher and (b) start/restart the cross-host git daemon. Skip
the hooks and everything *looks* connected but the daemon never (re)starts and
two sessions end up talking to a wall. This script does all of it, idempotently:
re-run it as often as you like — it only adds what's missing.

    python3 install.py              # sync deps, register server, wire hooks
    python3 install.py --dry-run    # show exactly what would change, touch nothing
    python3 install.py --no-mcp     # hooks only (server already registered)
    python3 install.py --no-sync    # skip `uv sync`

Config it touches:
  * the MCP server  -> registered via `claude mcp add --scope user` (no hand-edit
    of the live ~/.claude.json)
  * the hooks       -> merged into ~/.claude/settings.json (backup written first)

Opt back out any time: `claude mcp remove dispatch`, delete the hook entries, or
set `auto_arm = false` / `MCP_DISPATCH_NO_AUTO_ARM=1` to keep the wiring but stop
it firing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404 - only ever runs `uv` and `claude` by fixed name
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
LAUNCHER = REPO / "bin" / "dispatch-launcher"
HOOK_ARM = REPO / "hooks" / "dispatch-arm.py"
HOOK_GITSYNC = REPO / "hooks" / "dispatch-gitsync-arm.py"
HOOK_PEEK = REPO / "hooks" / "dispatch-peek.py"

# Which hook fires on which Claude Code event.
#   dispatch-arm      — arms the wake-watcher so a parked session wakes on arrival.
#   dispatch-gitsync  — starts (or restarts) the cross-host git daemon. On BOTH
#                       events so it self-heals: every new session AND every
#                       end-of-turn re-checks the host lock and respawns the
#                       daemon if it died. It's lock-gated, so a redundant spawn
#                       exits immediately — cheap and safe to fire often.
#   dispatch-peek     — surfaces unread inbox messages to a session that only
#                       consumes (never calls a tool, so piggyback never fires).
HOOK_WIRING: dict[str, list[Path]] = {
    "SessionStart": [HOOK_ARM, HOOK_GITSYNC],
    "Stop": [HOOK_ARM, HOOK_GITSYNC, HOOK_PEEK],
}


def run(cmd: list[str], *, cwd: Path | None = None) -> int:
    print(f"  $ {' '.join(cmd)}")
    try:
        return subprocess.run(cmd, cwd=cwd).returncode  # nosec B603
    except FileNotFoundError:
        print(f"  ! {cmd[0]} not found on PATH")
        return 127


def ensure_executable() -> None:
    """The launcher and hooks must be +x — a fresh clone or a zip download can
    drop the mode bit."""
    for p in (LAUNCHER, HOOK_ARM, HOOK_GITSYNC, HOOK_PEEK):
        if p.exists():
            mode = p.stat().st_mode
            p.chmod(mode | 0o111)


def sync_deps(dry: bool) -> None:
    print("• Syncing dependencies (uv sync)")
    if shutil.which("uv") is None:
        print("  ! uv not found — install it (https://docs.astral.sh/uv/) then re-run,")
        print("    or run `uv sync` yourself. Skipping.")
        return
    if dry:
        print("  (dry-run) would run: uv sync")
        return
    run(["uv", "sync"], cwd=REPO)


def mcp_registered() -> bool:
    try:
        r = subprocess.run(  # nosec B603 B607
            ["claude", "mcp", "get", "dispatch"],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0
    except FileNotFoundError:
        return False


def register_mcp(dry: bool) -> None:
    print("• Registering the dispatch MCP server (user scope)")
    if shutil.which("claude") is None:
        print("  ! `claude` CLI not found — add this to ~/.claude.json yourself:")
        print(
            json.dumps(
                {"mcpServers": {"dispatch": {"type": "stdio", "command": str(LAUNCHER)}}},
                indent=2,
            )
        )
        return
    if mcp_registered():
        print("  ✓ already registered (leaving it as-is)")
        return
    if dry:
        print(f"  (dry-run) would run: claude mcp add --scope user dispatch {LAUNCHER}")
        return
    rc = run(["claude", "mcp", "add", "--scope", "user", "dispatch", str(LAUNCHER)])
    if rc == 0:
        print("  ✓ registered — each session gets a <project>-<pid> id from the launcher")


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! {path} is not valid JSON ({e}); refusing to touch it.")
        sys.exit(1)


def wire_hooks(settings: dict) -> list[str]:
    """Merge the SessionStart/Stop hook entries into `settings`, in place.
    Returns a human-readable list of what was added (empty if already complete)."""
    added: list[str] = []
    hooks = settings.setdefault("hooks", {})
    for event, scripts in HOOK_WIRING.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            print(f"  ! settings.hooks.{event} is not a list; skipping.")
            continue
        present = {
            h.get("command")
            for g in groups
            if isinstance(g, dict)
            for h in g.get("hooks", [])
            if isinstance(h, dict)
        }
        missing = [str(s) for s in scripts if str(s) not in present]
        if missing:
            groups.append({"hooks": [{"type": "command", "command": c} for c in missing]})
            added += [f"{event} → {Path(c).name}" for c in missing]
    return added


def install_hooks(settings_path: Path, dry: bool) -> None:
    print(f"• Wiring SessionStart/Stop hooks into {settings_path}")
    settings = load_settings(settings_path)
    added = wire_hooks(settings)
    if not added:
        print("  ✓ all hooks already wired")
        return
    for line in added:
        print(f"  + {line}")
    if dry:
        print("  (dry-run) no files written")
        return
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        backup = settings_path.with_suffix(settings_path.suffix + ".dispatch-bak")
        shutil.copy2(settings_path, backup)
        print(f"  ✓ backed up to {backup}")
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(settings_path)
    print(f"  ✓ updated {settings_path}")


def install_service(dry: bool) -> None:
    """Put the cross-host git bridge under systemd instead of the Claude Code hook.

    The hook only fires inside Claude Code, so agents on any other harness get no
    daemon at all — and the messages just sit in git. This delegates to
    `dispatch-gitsync service install`, which owns the unit rendering; re-running
    it is also the upgrade path."""
    print("• Installing the cross-host bridge as a systemd user service")
    cmd = [sys.executable, str(REPO / "bin" / "dispatch-gitsync"), "service", "install"]
    if dry:
        cmd.append("--dry-run")
    if run(cmd, cwd=REPO) != 0:
        print("  ! service not installed (see above). Cross-host comms will still work")
        print("    inside Claude Code via the SessionStart hook.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Wire mcp-dispatch into Claude Code (idempotent).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    ap.add_argument("--no-sync", action="store_true", help="skip `uv sync`")
    ap.add_argument("--no-mcp", action="store_true", help="skip MCP server registration")
    ap.add_argument(
        "--service",
        action="store_true",
        help="also run the cross-host git bridge as a systemd user service, so it "
        "works outside Claude Code (openclaw, Hermes, the TUI, cron). Requires "
        "`dispatch-gitsync init` first; re-run to upgrade.",
    )
    ap.add_argument(
        "--settings",
        type=Path,
        default=Path.home() / ".claude" / "settings.json",
        help="settings.json to wire hooks into (default: ~/.claude/settings.json)",
    )
    args = ap.parse_args()

    print(f"mcp-dispatch installer — repo at {REPO}")
    if args.dry_run:
        print("(dry-run: nothing will be written)\n")

    ensure_executable()
    if not args.no_sync:
        sync_deps(args.dry_run)
    if not args.no_mcp:
        register_mcp(args.dry_run)
    install_hooks(args.settings, args.dry_run)
    if args.service:
        install_service(args.dry_run)

    print("\nDone. Next:")
    print("  • Restart your Claude Code sessions so the new config loads.")
    print("  • Same-host agents can talk now — try `bin/dispatch-tui` to watch them.")
    print("  • For cross-host comms, run once per host:")
    print("      bin/dispatch-gitsync init --create you/agent-bus   # new private bus")
    print("      bin/dispatch-gitsync init git@github.com:you/agent-bus.git  # join one")
    print("    then the SessionStart hook starts the daemon automatically.")
    if not args.service:
        print("  • Using a harness OTHER than Claude Code (openclaw, Hermes, scripts)?")
        print("    The hook never fires there, so nothing syncs. Run instead:")
        print("      python3 install.py --service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
