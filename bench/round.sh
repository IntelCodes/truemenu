#!/usr/bin/env bash
# Run one benchmark round in the background (survives SSH disconnects).
#   RESET=1 bench/round.sh 1 tune     round 1: fresh state, re-tag with the model, tune cases
#   bench/round.sh 2 all              round 2: all cases, keeps current state
cd "$(dirname "$0")/.."
ROUND=${1:-1}; SPLIT=${2:-tune}; REPS=${3:-3}
set -a; source .env; set +a
mkdir -p bench/results
if [ "${RESET:-0}" = 1 ]; then ENRICH_FLAGS=--llm scripts/reset.sh; else scripts/start.sh; fi
./venv/bin/python bench/run.py --check-cases --split "$SPLIT" > "bench/results/round${ROUND}_cases.txt"
nohup ./venv/bin/python -u bench/run.py --round "$ROUND" --split "$SPLIT" --reps "$REPS" > "bench/results/round${ROUND}.log" 2>&1 &
echo "Round $ROUND started in the background (pid $!)."
echo "Watch progress:   tail -f bench/results/round${ROUND}.log      (Ctrl-C stops watching, not the run)"
