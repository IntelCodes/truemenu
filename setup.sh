#!/usr/bin/env bash
# One-time setup on the droplet. Safe to re-run; never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
  KEY=$(python3 -c "import secrets;print('tm_'+secrets.token_urlsafe(24))")
  cat > .env <<ENV
TRUEMENU_QUOTE_SECRET=$SECRET
TRUEMENU_AGENT_KEYS=lunch-agent:$KEY:read+feedback
TRUEMENU_LUNCH_AGENT_KEY=$KEY
TRUEMENU_URL=http://127.0.0.1:8088
TRUEMENU_PORT=8088
# Model credentials are read from the HW4 agent's .env, not copied here.
AGENT_MODEL_ENV=$HOME/hw4/.env
# Short interval for recording the demo; remove for normal use (sources then use merchants.json).
TRUEMENU_SYNC_INTERVAL_S=15
ENV
  chmod 600 .env
  echo "created .env (mode 600) with a new quote secret and agent key"
fi
chmod +x scripts/*.sh ask check feed tests/smoke.sh
[ -f "$HOME/hw4/.env" ] && echo "model credentials: using $HOME/hw4/.env" || echo "WARNING: $HOME/hw4/.env not found; the agent will run in offline mode"
ENRICH_FLAGS=--llm scripts/reset.sh
echo "setup done. Try:  ./ask \"Find me lunch near Kendall Square today\"   then   ./check"
