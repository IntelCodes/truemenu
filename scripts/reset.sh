#!/usr/bin/env bash
# Wipe demo state (tags from feedback, quotes, overrides) and start fresh.
cd "$(dirname "$0")/.."
scripts/stop.sh >/dev/null 2>&1
rm -rf state runs/last_run.json
scripts/start.sh
set -a; source .env; set +a
${PY:-./venv/bin/python} -m truemenu.feed enrich ${ENRICH_FLAGS:-}
