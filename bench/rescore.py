"""Re-apply the current score() to saved runs (no model calls). Writes <file>.rescored.jsonl next to each input.

  python bench/rescore.py bench/results/round1_*.jsonl
"""
import json
import sys
from pathlib import Path

sys.argv, files = [sys.argv[0]], sys.argv[1:]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run  # noqa: E402

CASES = {c["id"]: c for c in run.CASES["cases"]}
for f in files:
    out = Path(f).with_suffix(".rescored.jsonl")
    changed = 0
    with open(f) as src, open(out, "w") as dst:
        for line in src:
            r = json.loads(line)
            if not r.get("error"):
                new = run.score(CASES[r["case"]], r["system"], {"raw": r["output"]})
                if new.get("reasons") != r["score"].get("reasons") or new["success"] != r["score"]["success"]:
                    changed += 1
                    print(f"{r['case']} {r['system']} rep{r['rep']}: {r['score'].get('reasons')} -> {new.get('reasons')}"
                          f"{'  (SUCCESS CHANGED)' if new['success'] != r['score']['success'] else ''}")
                r["score"] = new
            dst.write(json.dumps(r) + "\n")
    print(f"{f}: {changed} runs relabelled -> {out}")
