#!/usr/bin/env bash
# Run one cold-start session against a TrueMenu deployment.
#   COLDSTART_KEY=<a read key> coldstart/run.sh [model]
# The agent gets only the task, the docs URL, the base URL and the key.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${COLDSTART_KEY:?set COLDSTART_KEY to a TrueMenu read key}"
BASE=${COLDSTART_BASE:-https://64-227-18-144.sslip.io}
DOCS=${COLDSTART_DOCS:-https://raw.githubusercontent.com/IntelCodes/truemenu/main/docs/AGENT_API.md}
MODEL=${1:-stepfun/step-3.7-flash}
STAMP=$(date -u +%Y%m%d_%H%M%S)
OUT=runs/coldstart_${STAMP}.jsonl
"${PYTHON:-python3}" coldstart/agent.py --task-file coldstart/task.txt --docs "$DOCS" --base "$BASE" \
  --key "$COLDSTART_KEY" --model "$MODEL" --out "$OUT"
echo; "${PYTHON:-python3}" coldstart/score.py "$OUT"
