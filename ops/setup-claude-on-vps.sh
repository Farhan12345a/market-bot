#!/usr/bin/env bash
#
# Install Claude Code on the trading VPS.
#
# Run ON THE DROPLET, once:
#     ssh root@YOUR_DROPLET_IP
#     cd /root/market-bot && git pull
#     bash ops/setup-claude-on-vps.sh
#
# Safe to re-run. Touches nothing belonging to the bot: no config, no logs, no
# systemd unit, no restart. It installs Node and Claude Code and stops.

set -euo pipefail

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }

say "1/5  Checking the box"
echo "    host:   $(hostname)"
echo "    distro: $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo unknown)"
echo "    disk:   $(df -h / | awk 'NR==2 {print $4" free of "$2}')"

FREE_MB=$(df -m / | awk 'NR==2 {print $4}')
if [ "$FREE_MB" -lt 1500 ]; then
  warn "Only ${FREE_MB}MB free. Node + Claude Code want ~1GB. Free some space first."
  exit 1
fi

say "2/5  Node.js 18+"
NEED_NODE=1
if command -v node >/dev/null 2>&1; then
  MAJOR=$(node -v | sed 's/v\([0-9]*\).*/\1/')
  echo "    found node $(node -v)"
  [ "$MAJOR" -ge 18 ] && NEED_NODE=0 || warn "node $MAJOR is too old, upgrading"
fi

if [ "$NEED_NODE" -eq 1 ]; then
  echo "    installing Node 20 from NodeSource..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
  echo "    now on $(node -v)"
fi

say "3/5  Claude Code"
npm install -g @anthropic-ai/claude-code
echo "    installed: $(claude --version 2>/dev/null || echo '(version check failed)')"

say "4/5  tmux"
# Without this, closing your laptop kills whatever Claude was mid-way through.
command -v tmux >/dev/null 2>&1 || apt-get install -y tmux
echo "    $(tmux -V)"

say "5/5  Sanity check on the bot"
systemctl is-active --quiet market-bot \
  && echo "    market-bot.service: ACTIVE (untouched by this script)" \
  || warn "market-bot.service is not active - unrelated to this install, but worth a look"

cat <<'NEXT'

────────────────────────────────────────────────────────────────────
Done. To start working:

    tmux new -s claude          # so an SSH drop doesn't kill the session
    cd /root/market-bot
    claude

First run asks you to authenticate. On a headless box it prints a URL —
open it on your laptop, approve, paste the code back.

Detach with  ctrl-b  then  d .  Come back with  tmux attach -t claude .

Then, before restarting the live bot after ANY change:

    bash ops/deploy.sh

────────────────────────────────────────────────────────────────────
NEXT
