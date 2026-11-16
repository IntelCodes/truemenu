#!/usr/bin/env python3
"""Score a cold-start transcript against the checks in docs/COLD_START_TEST.md.

Ground truth comes from data/ground_truth.csv (prices), data/merchants.json (hours)
and bench/labels.json (hand labels), not from TrueMenu's own tags.

  python3 coldstart/score.py runs/coldstart_<stamp>.jsonl
"""
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEAL = datetime(2026, 11, 16, 12, 30)          # Monday 12:30, America/New_York
BUDGET, EXCLUDE = 20.0, ("pork",)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

truth = {r["item_id"]: r for r in csv.DictReader(open(ROOT / "data/ground_truth.csv"))}
labels = json.load(open(ROOT / "bench/labels.json"))["items"]
merchants = {m["merchant_id"]: m for m in json.load(open(ROOT / "data/merchants.json"))["merchants"]}


def is_open(merchant_id):
    h = merchants[merchant_id]["hours"].get(DAYS[MEAL.weekday()])
    return bool(h) and h[0] <= MEAL.strftime("%H:%M") <= h[1]


def main(path):
    lines = [json.loads(l) for l in open(path) if l.strip()]
    answer = next((l["answer"] for l in lines if l["kind"] == "final"), None) or ""
    http = [l for l in lines if l["kind"] == "http"]
    offered, verifies, statuses = {}, [], []
    for h in http:
        url, resp = h["request"].get("url") or "", h["response"]
        statuses.append((h["step"], h["request"].get("method"), url.split("sslip.io")[-1][:60], resp.get("status")))
        try:
            body = json.loads(resp.get("body") or "null")
        except ValueError:
            body = None
        if url.endswith("/v1/find_items") and isinstance(body, dict):
            for r in body.get("results", []):
                offered[r["item_id"]] = r
        if url.endswith("/v1/menu") or "/v1/menu/" in url:
            for r in (body or {}).get("items", []) if isinstance(body, dict) else []:
                offered.setdefault(r["item_id"], r)
        if url.endswith("/v1/verify_quote") and isinstance(body, dict):
            verifies.append(body)

    named = [i for i, r in offered.items() if r.get("name") and r["name"].lower() in answer.lower()]
    named = sorted(named, key=lambda i: -len(offered[i]["name"]))
    chosen = named[0] if named else None
    prices = [float(p) for p in re.findall(r"\$\s?(\d+(?:\.\d{1,2})?)", answer)]

    checks = {}
    if chosen:
        lab, gt = labels.get(chosen, {}), truth[chosen]
        true_price = float(gt["price_usd"])
        checks["1_valid_dish"] = (lab.get("pork") == 0 and lab.get("high_carb") == 0 and true_price <= BUDGET
                                  and is_open(gt["merchant_id"]))
        checks["2_correct_price"] = any(abs(p - true_price) < 0.005 for p in prices)
        checks["3_verified"] = any(v.get("item_id") == chosen and v.get("verdict") == "valid" for v in verifies)
        checks["5_no_invention"] = True
    else:
        checks.update({"1_valid_dish": False, "2_correct_price": False, "3_verified": False,
                       "5_no_invention": not re.search(r"\$\s?\d", answer)})
    decision = re.search(r"\b(pay|do not pay|don't pay|safe to pay|not safe)\b", answer, re.I)
    cites = re.search(r"verif|signature|quote", answer, re.I)
    checks["4_result_used (auto; confirm by reading)"] = bool(decision and cites)

    print(f"transcript: {path}")
    print(f"model: {lines[0].get('model')}   steps: {len(http)} http calls")
    print("calls (step, method, path, status):")
    for s in statuses:
        print("   ", s)
    print(f"chosen item: {chosen}  ({offered.get(chosen, {}).get('name')})")
    if chosen:
        print(f"ground truth: ${truth[chosen]['price_usd']}  labels: {labels.get(chosen)}")
    print(f"prices in answer: {prices}")
    print("checks:")
    for k, v in checks.items():
        print(f"   {'PASS' if v else 'FAIL'}  {k}")
    useful = all(v for k, v in checks.items())
    print(f"\nUSEFUL RESULT (all checks): {'YES' if useful else 'NO'}")
    print("\nfinal answer:\n" + answer)


if __name__ == "__main__":
    main(sys.argv[1])
