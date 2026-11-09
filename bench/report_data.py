"""Numbers for the HW7 report, computed from the result files (nothing typed by hand).

  python bench/report_data.py      prints every figure the report uses
"""
import json
from pathlib import Path

RES = Path(__file__).resolve().parent / "results"
R1 = RES / "round1_20261101_1826.rescored.jsonl"      # round 1, rescored (one label changed, no score changed)
R2A = RES / "round2_20261101_1954.jsonl"             # round 2 part A: TrueMenu, tune cases, after the change
R2B = sorted(RES.glob("round2_20261101_2050*.jsonl"))  # round 2 part B: all systems, held-out cases
R3 = RES / "round3_20261103_0009.jsonl"            # round 3: B1 with aligned main-dish wording, all 18 cases
SYS = ["truemenu", "raw_menu", "raw_menu_aligned", "no_data"]
NAME = {"truemenu": "TrueMenu agent", "raw_menu": "B1 raw menu", "raw_menu_aligned": "B1a raw menu, aligned",
        "no_data": "B0 no data"}
GROUPS = [
    ("All cases", lambda r: True),
    ("Ordinary", lambda r: r["kind"] == "ordinary"),
    ("Hard (hidden pork, allergens, seafood)", lambda r: r["kind"].startswith("hard")),
    ("Closed, nothing fits, or stale data", lambda r: r["kind"].startswith("failure")),
    ("Price changed and synced", lambda r: r["kind"] == "price_changed_synced"),
]


def load(p):
    return [json.loads(l) for l in open(p) if l.strip()] if p and Path(p).exists() else []


r1, r2a, r3 = load(R1), load(R2A), load(R3)
r2b = [x for p in R2B if not p.name.endswith(".rescored.jsonl") for x in load(p)]


def rate(rows, sys=None, sel=lambda r: True):
    rs = [r for r in rows if (sys is None or r["system"] == sys) and sel(r)]
    k = sum(r["score"]["success"] for r in rs)
    return k, len(rs)


def fmt(k, n):
    return f"{100 * k / n:.0f}% ({k}/{n})" if n else "n/a"


def reasons(r):
    return " ".join(r["score"].get("reasons", []))


ERRS = [
    ("Constraint violated (ingredient or diet)", lambda r: r["score"].get("constraint_violation")),
    ("Restaurant closed or over budget", lambda r: "restaurant closed" in reasons(r) or "over budget" in reasons(r)),
    ("Wrong price", lambda r: "price wrong" in reasons(r)),
    ("Item not on menu", lambda r: "not on the menu" in reasons(r)),
    ("Declined when an answer existed", lambda r: "unnecessary decline" in reasons(r)),
    ("Answered when it should decline", lambda r: "should have declined" in reasons(r)),
    ("Malformed reply", lambda r: "malformed" in reasons(r)),
    ("Model call failed (out of tokens)", lambda r: r.get("error")),
]


def err_table(rows):
    out = []
    for s in SYS:
        rs = [r for r in rows if r["system"] == s]
        if not rs:
            continue
        n = len(rs)
        cells = [fmt(sum(1 for r in rs if f(r)), n) for _, f in ERRS]
        cost = (f"{sum(r['model_calls'] for r in rs) / n:.1f}", f"{sum(r['model_ms'] for r in rs) / n / 1000:.0f}",
                f"{sum(r['prompt_tokens'] + r['completion_tokens'] for r in rs) / n:,.0f}")
        out.append((s, cells, cost))
    return out


def per_case():
    """case -> {col: 'k/n' or '-'}"""
    cases = json.loads((RES.parent / "cases.json").read_text())["cases"]
    rows = []
    for c in cases:
        cid = c["id"]
        def kn(rs, s):
            x = [r for r in rs if r["case"] == cid and r["system"] == s]
            return f"{sum(r['score']['success'] for r in x)}/{len(x)}" if x else "-"
        if c["split"] == "tune":
            rows.append((cid, c["split"], c["kind"], c["expect"], kn(r1, "truemenu"), kn(r2a, "truemenu"),
                         kn(r1, "raw_menu"), kn(r3, "raw_menu_aligned"), kn(r1, "no_data")))
        else:
            rows.append((cid, c["split"], c["kind"], c["expect"], "-", kn(r2b, "truemenu"),
                         kn(r2b, "raw_menu"), kn(r3, "raw_menu_aligned"), kn(r2b, "no_data")))
    return rows, {c["id"]: c for c in cases}


if __name__ == "__main__":
    print(f"files: R1={len(r1)} R2A={len(r2a)} R2B={len(r2b)} R3={len(r3)}")
    for sp in ("tune", "fresh"):
        for g, sel in GROUPS:
            print(f"  B1a {sp:<5} {g:<45} {fmt(*rate(r3, 'raw_menu_aligned', lambda r, sel=sel: r['split'] == sp and sel(r)))}")
    print("  B1a all", fmt(*rate(r3)))
    for s_, cells, cost in err_table(r3):
        print(f"  {s_} " + " | ".join(cells) + f" | calls {cost[0]} s {cost[1]} tok {cost[2]}")
    print("\nTUNE (round 1 before change; TrueMenu after = round 2 part A)")
    for g, sel in GROUPS:
        print(f"  {g:<48} TM before {fmt(*rate(r1, 'truemenu', sel)):<14} TM after {fmt(*rate(r2a, 'truemenu', sel)):<14} "
              f"B1 {fmt(*rate(r1, 'raw_menu', sel)):<14} B0 {fmt(*rate(r1, 'no_data', sel))}")
    print("\nFRESH (held out, round 2 part B)")
    for g, sel in GROUPS:
        print(f"  {g:<48} TM {fmt(*rate(r2b, 'truemenu', sel)):<14} B1 {fmt(*rate(r2b, 'raw_menu', sel)):<14} "
              f"B0 {fmt(*rate(r2b, 'no_data', sel))}")
    for title, rows in [("errors round 1 tune", r1), ("errors round 2 fresh", r2b), ("errors TM round 2 tune", r2a)]:
        print(f"\n{title}")
        for s, cells, cost in err_table(rows):
            print(f"  {s:<9} " + " | ".join(cells) + f" | calls {cost[0]} s {cost[1]} tok {cost[2]}")
    print("\nper case")
    for row in per_case()[0]:
        print("  ", row)
