#!/usr/bin/env bash
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && source .env; set +a
PY=${PY:-./venv/bin/python}
[ -f state/api.pid ] && { kill "$(cat state/api.pid)" 2>/dev/null && echo "API stopped"; rm -f state/api.pid; }
$PY -m truemenu.feed stop 2>/dev/null || true
