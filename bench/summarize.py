"""Aggregate one or more result files into the tables for the report.

  python bench/summarize.py bench/results/round1_*.jsonl
"""
import json
import sys
from collections import defaultdict

rows = [json.loads(l) for p in sys.argv[1:] for l in open(p) if l.strip()]
if not rows:
    sys.exit("no rows")
SYS = ["truemenu", "raw_menu", "raw_menu_aligned", "no_data"]


def pct(a, b):
    return f"{100 * a / b:.0f}% ({a}/{b})" if b else "-"


def table(title, groups):
    print(f"\n## {title}\n")
    print("| group | " + " | ".join(s for s in SYS if any(r["system"] == s for r in rows)) + " |")
    print("|---|" + "---|" * len([s for s in SYS if any(r["system"] == s for r in rows)]))
    for g, sel in groups:
        cells = []
        for s in SYS:
            rs = [r for r in rows if r["system"] == s and sel(r)]
            if not any(r["system"] == s for r in rows):
                continue
            cells.append(pct(sum(r["score"]["success"] for r in rs), len(rs)))
        print(f"| {g} | " + " | ".join(cells) + " |")


rounds = sorted({r["round"] for r in rows})
for rd in rounds:
    R = [r for r in rows if r["round"] == rd]
    globals()["rows"], allrows = R, rows
    print(f"\n# Round {rd}: {len({r['case'] for r in R})} cases, {len(R)} runs")
    table("Task success by case group", [
        ("all cases", lambda r: True),
        ("tune split", lambda r: r["split"] == "tune"),
        ("fresh split (held out)", lambda r: r["split"] == "fresh"),
        ("ordinary", lambda r: r["kind"] == "ordinary"),
        ("hard (hidden pork / allergens)", lambda r: r["kind"].startswith("hard")),
        ("failure (closed / no answer / stale)", lambda r: r["kind"].startswith("failure")),
        ("price changed and synced", lambda r: r["kind"] == "price_changed_synced"),
    ])
    print("\n## Error types (share of runs)\n")
    print("| system | constraint violation | wrong price | invented item | unnecessary decline | should have declined | malformed reply | errors | model calls/run | model s/run | tokens/run |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for s in SYS:
        rs = [r for r in R if r["system"] == s]
        if not rs:
            continue
        def cnt(f):
            return sum(1 for r in rs if f(r))
        reasons = lambda r: " ".join(r["score"].get("reasons", []))
        n = len(rs)
        print(f"| {s} | {pct(cnt(lambda r: r['score'].get('constraint_violation')), n)} | "
              f"{pct(cnt(lambda r: 'price wrong' in reasons(r)), n)} | {pct(cnt(lambda r: 'not on the menu' in reasons(r)), n)} | "
              f"{pct(cnt(lambda r: 'unnecessary decline' in reasons(r)), n)} | {pct(cnt(lambda r: 'should have declined' in reasons(r)), n)} | "
              f"{pct(cnt(lambda r: 'malformed' in reasons(r)), n)} | "
              f"{pct(cnt(lambda r: r.get('error')), n)} | {sum(r['model_calls'] for r in rs) / n:.1f} | "
              f"{sum(r['model_ms'] for r in rs) / n / 1000:.1f} | {sum(r['prompt_tokens'] + r['completion_tokens'] for r in rs) / n:.0f} |")
    print("\n## Per case (passes out of reps)\n")
    print("| case | kind | expect | " + " | ".join(s for s in SYS if any(r["system"] == s for r in R)) + " |")
    print("|---|---|---|" + "---|" * len([s for s in SYS if any(r["system"] == s for r in R)]))
    for c in sorted({r["case"] for r in R}):
        cr = [r for r in R if r["case"] == c]
        cells = []
        for s in SYS:
            rs = [r for r in cr if r["system"] == s]
            if not any(r["system"] == s for r in R):
                continue
            fails = sorted({"; ".join(r["score"].get("reasons", []))[:60] for r in rs if not r["score"]["success"]})
            cells.append(f"{sum(r['score']['success'] for r in rs)}/{len(rs)}" + (f" ({' | '.join(fails)})" if fails else ""))
        print(f"| {c} | {cr[0]['kind']} | {cr[0]['expect']} | " + " | ".join(cells) + " |")
    rows = allrows
