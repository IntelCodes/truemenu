#!/usr/bin/env bash
# Round 2 (after the one change), in the background.
#   part A: TrueMenu only, the 12 tune cases again (retest T05, check nothing else broke)
#   part B: all three systems, the 6 held-out fresh cases (never run before)
# Keeps round 1's state and tags (no reset), so the only difference is the code change.
cd "$(dirname "$0")/.."
set -a; source .env; set +a
scripts/start.sh
./venv/bin/python bench/run.py --check-cases --split all > bench/results/round2_cases.txt
nohup bash -c "./venv/bin/python -u bench/run.py --round 2 --split tune --reps 3 --systems truemenu && \
               ./venv/bin/python -u bench/run.py --round 2 --split fresh --reps 3" > bench/results/round2.log 2>&1 &
echo "Round 2 started in the background (pid $!). 90 runs, about 55 minutes."
echo "Check progress:   tail -3 bench/results/round2.log"
