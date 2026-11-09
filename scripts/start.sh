#!/usr/bin/env bash
# Start the simulated POS feed and the TrueMenu API in the background.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
PY=${PY:-./venv/bin/python}
UVICORN=${UVICORN:-./venv/bin/uvicorn}
mkdir -p state logs

scripts/stop.sh >/dev/null 2>&1 || true
$PY -m truemenu.feed once >/dev/null
nohup $PY -m truemenu.feed loop >> logs/feed.log 2>&1 &
nohup $UVICORN truemenu.server:app --host 127.0.0.1 --port "${TRUEMENU_PORT:-8088}" >> logs/api.log 2>&1 &
echo $! > state/api.pid
sleep 2
curl -sf "http://127.0.0.1:${TRUEMENU_PORT:-8088}/health" >/dev/null && echo "TrueMenu API up on 127.0.0.1:${TRUEMENU_PORT:-8088}; feed running (logs/feed.log)" || { echo "API failed to start, see logs/api.log"; exit 1; }
