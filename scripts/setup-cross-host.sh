#!/usr/bin/env bash
# Set up cross-host agent messaging on THIS host, end to end.
#
#   ./scripts/setup-cross-host.sh git@github.com:you/agent-bus.git
#
# Run it once per machine. It is idempotent — re-run it after pulling an update,
# after moving the repo, or any time you're not sure of the state.
#
# What it does, and why each step exists:
#   1. Preflight: can *you* reach the bus repo at all? (fails early with the real
#      git error rather than a mystery dead daemon later)
#   2. Credentials: a systemd user service inherits almost NOTHING from your login
#      shell. An SSH remote that works when you type it can still fail for the
#      service, because the service has no SSH_AUTH_SOCK. This is the single most
#      common way this setup silently half-works.
#   3. Lingering: a systemd *user* manager does not run at boot unless lingering is
#      enabled, and on many distros it is torn down when your last session ends. On
#      a headless box the bridge would only run while you happen to be logged in.
#   4. Install + start the bridge, then verify it is actually syncing.
set -euo pipefail

BUS_URL="${1:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GITSYNC="$REPO_ROOT/bin/dispatch-gitsync"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31merror\033[0m %s\n\n' "$*" >&2; exit 1; }

if [ -z "$BUS_URL" ]; then
  die "usage: $0 <bus-repo-url>
       e.g. $0 git@github.com:you/agent-bus.git
            $0 https://github.com/you/agent-bus.git

       The bus is a PRIVATE git repo shared by every host you want to connect.
       Create it once (gh repo create you/agent-bus --private), then run this on
       each machine with the same URL."
fi

# ── 1. preflight ────────────────────────────────────────────────────────────
say "Checking prerequisites"
command -v git     >/dev/null || die "git is not installed."
command -v python3 >/dev/null || die "python3 is not installed."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python3 is older than 3.11, which mcp-dispatch requires."
[ -x "$GITSYNC" ] || chmod +x "$GITSYNC" 2>/dev/null || true
[ -f "$GITSYNC" ] || die "can't find $GITSYNC — run this from inside the mcp-dispatch clone."
ok "git, python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])'), dispatch-gitsync"

say "Checking you can reach the bus repo"
if ! git ls-remote "$BUS_URL" >/dev/null 2>&1; then
  printf '\n'
  git ls-remote "$BUS_URL" || true   # show the real error, unswallowed
  die "cannot reach $BUS_URL as you, right now.

       Fix access first — nothing below can work until this succeeds:
         SSH remote   : ssh -T git@github.com   (should greet you by name)
                        ssh-add -l              (should list a key)
         HTTPS remote : gh auth login && gh auth setup-git
       Then re-run this script."
fi
ok "bus repo reachable"

# ── 2. credentials for the SERVICE (not just for you) ───────────────────────
say "Working out how the background service will authenticate"
ENV_ARGS=()
case "$BUS_URL" in
  https://*)
    HELPER="$(git config --get credential.helper || true)"
    if [ -n "$HELPER" ]; then
      ok "HTTPS + credential helper '$HELPER' — the service reads the same store. Nothing to pass through."
    else
      warn "HTTPS remote with NO credential helper configured."
      warn "You reached the repo just now, but the service may not be able to."
      warn "Recommended:  gh auth login && gh auth setup-git"
    fi
    ;;
  *)
    # SSH. The agent socket is the fragile part: it is per-login, and a user
    # service started at boot has no SSH_AUTH_SOCK at all.
    if [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1; then
      ENV_ARGS=(--env "SSH_AUTH_SOCK=$SSH_AUTH_SOCK")
      ok "passing your ssh-agent through to the service ($SSH_AUTH_SOCK)"
      warn "Agent sockets are per-login: if this path changes after a reboot, the"
      warn "service loses access until you re-run this script. For a machine that"
      warn "should just work forever, prefer a passphrase-less deploy key instead:"
      warn "  ssh-keygen -t ed25519 -f ~/.ssh/agent_bus -N '' -C agent-bus"
      warn "  # add ~/.ssh/agent_bus.pub as a deploy key (write access) on the bus repo"
      warn "  # then in ~/.ssh/config:"
      warn "  #   Host github.com"
      warn "  #     IdentityFile ~/.ssh/agent_bus"
      warn "  #     IdentitiesOnly yes"
    else
      warn "SSH remote, but no usable ssh-agent in this shell."
      warn "If your key is passphrase-less and named in ~/.ssh/config, this is fine."
      warn "Otherwise the service will not be able to push or fetch. See the deploy-key"
      warn "recipe in the README's cross-host section."
    fi
    ;;
esac

# ── 3. lingering (headless boxes) ───────────────────────────────────────────
say "Checking the service will survive logout / start at boot"
if command -v loginctl >/dev/null 2>&1; then
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" = "yes" ]; then
    ok "lingering already enabled"
  else
    warn "lingering is OFF: a systemd *user* service does not start at boot, and on"
    warn "many distros is killed when your last session ends. On a headless host the"
    warn "bridge would only run while you happen to be logged in. Enabling it:"
    if sudo -n true 2>/dev/null && sudo -n loginctl enable-linger "$USER" 2>/dev/null; then
      ok "lingering enabled"
    else
      warn "  sudo loginctl enable-linger $USER      <-- run this yourself (needs sudo)"
    fi
  fi
else
  warn "no loginctl here — if this is not a systemd host, see the fallback at the end."
fi

# ── 4. install and verify ───────────────────────────────────────────────────
say "Installing the bridge (clone/seed the bus, write config, start the service)"
python3 "$GITSYNC" init "$BUS_URL" --service "${ENV_ARGS[@]+"${ENV_ARGS[@]}"}"

say "Verifying"
# Give the just-restarted daemon a moment to take the host lock BEFORE reporting,
# or `status` truthfully-but-confusingly says "daemon running: False".
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  sleep 3
fi
python3 "$GITSYNC" status || true

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  STATE="$(systemctl --user is-active mcp-dispatch-gitsync 2>/dev/null || true)"
  if [ "$STATE" = "active" ]; then
    ok "service is running"
    # The service authenticates differently from your shell, so prove IT can sync,
    # not just that you can. Auth failures show up here and nowhere else.
    if journalctl --user -u mcp-dispatch-gitsync --since -1min --no-pager 2>/dev/null \
        | grep -qiE 'permission denied|could not read from remote|authentication failed|host key'; then
      warn "the service started but its git access is FAILING — this is the credentials"
      warn "problem described above. Full detail:"
      warn "  journalctl --user -u mcp-dispatch-gitsync -n 50 --no-pager"
    else
      ok "no authentication errors in the log"
    fi
  else
    warn "service state is '$STATE' — inspect with:"
    warn "  journalctl --user -u mcp-dispatch-gitsync -n 50 --no-pager"
  fi
else
  warn "No systemd user session on this host, so no service was installed."
  warn "Run the daemon under whatever supervisor you do have (launchd, supervisord,"
  warn "tmux, docker). The flag that matters is --no-presence-gate:"
  warn "  $GITSYNC --no-presence-gate"
fi

cat <<EOF

$(printf '\033[1mDone.\033[0m') This host is on the bus. Agents keep calling dispatch() exactly as
before; messages to agents on other machines now go out over git automatically.

  watch it      journalctl --user -u mcp-dispatch-gitsync -f
  check it      $GITSYNC status
  re-run this   after moving this repo, rebuilding a venv, or pulling an update
  remove it     $GITSYNC service uninstall

Note on speed: sends always go out within ~2s. RECEIVING the first message after a
quiet period can take up to 30s, because the bridge backs off its polling when
nothing is moving (that is what keeps it near 1% CPU instead of 6.5% running 24/7).
Set max_fetch_interval = 0 under [git] in ~/.config/mcp-dispatch/config.toml if you
want a constant 2s in both directions.
EOF
