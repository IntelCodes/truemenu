"""Independent check of the agent's last recommendation.

Runs as a separate process and trusts neither the agent nor the response it received:
  1 verify the quote with TrueMenu's public verify endpoint (signature, expiry, price unchanged)
  2 compare the item and price against the ground-truth table on disk (plus any simulated POS change)
  3 re-check the user's constraints from the raw data: budget, excluded words, posted hours

  python agent/check.py               check runs/last_run.json
"""
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("TRUEMENU_URL", "http://127.0.0.1:8088")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Words that make an item violate each dislike/diet, checked on the raw name (independent of TrueMenu's tags).
BANNED = {
    "pork": ["pepperoni", "bacon", "ham", "prosciutto", "sausage", "pork", "chorizo", "salami"],
    "avocado": ["avocado", "guac"],
    "no carbs": ["rice", "pita", "wrap", "burger", "pizza", "bread", "pasta", "roll", "sandwich", "fries", "taco", "fried", "crispy", "chowder"],
}

results = []


def check(name, ok, detail):
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<34} {detail}")


def main():
    run = json.loads((ROOT / "runs" / "last_run.json").read_text())
    pick = run.get("pick")
    print(f"Checking run from {run['started']}: request '{run['request']}'")
    if run.get("status") != "recommended" or not pick:
        print("  agent declined to recommend; nothing to verify (this is the correct behaviour when no verified, fresh option exists)")
        return 0
    print(f"  pick: {pick['merchant']['name']} / {pick['name']} at ${pick['price']['amount']:.2f}\n")

    # 1 quote
    v = requests.post(f"{URL}/v1/verify_quote", json={"quote_id": pick["quote_id"]}, timeout=10).json()
    check("quote signature valid", v.get("valid_signature") is True, v.get("reason", "HMAC matches"))
    check("quote issued by this service", v.get("issued_by_this_service") is True, "found in the service's quote log")
    check("quote not expired", v.get("expired") is False, f"issued {v.get('issued_at')}")
    check("quoted price equals agent's price", abs(v.get("quoted_price", -1) - pick["price"]["amount"]) < 0.005,
          f"quote ${v.get('quoted_price')} vs agent ${pick['price']['amount']}")
    check("price unchanged since quote", v.get("price_unchanged") is True,
          f"current ${v.get('current_price')} ({v.get('verdict')})")

    # 2 ground truth
    rows = {r["item_id"]: r for r in csv.DictReader(open(ROOT / "data" / "ground_truth.csv", newline="", encoding="utf-8"))}
    ov_path = ROOT / "state" / "overrides.json"
    ov = json.loads(ov_path.read_text()) if ov_path.exists() else {}
    gt = rows.get(pick["item_id"])
    check("item exists in ground truth", gt is not None, pick["item_id"])
    if gt:
        expected = float(ov.get("prices", {}).get(pick["item_id"], gt["price_usd"]))
        src = "simulated POS change" if pick["item_id"] in ov.get("prices", {}) else f"merchant's own page, checked {gt['verified_on']}"
        check("price matches ground truth", abs(expected - pick["price"]["amount"]) < 0.005, f"${expected:.2f} ({src})")
        check("ground-truth price is verified", gt["verified"].strip().lower() == "yes", gt.get("verified_how") or "not verified")

    # 3 constraints, from raw data
    prefs = run["prefs"]
    check("within budget", pick["price"]["amount"] <= prefs["budget_usd_max"], f"<= ${prefs['budget_usd_max']}")
    name = pick["name"].lower()
    for rule in list(prefs.get("dislikes", [])) + [prefs.get("diet", "")]:
        words = [w for w in BANNED.get(rule, []) if w in name]
        if rule in BANNED:
            check(f"respects '{rule}'", not words, "no banned words in name" if not words else f"found {words}")
    merchants = {m["merchant_id"]: m for m in json.loads((ROOT / "data" / "merchants.json").read_text())["merchants"]}
    m = merchants[pick["merchant"]["id"]]
    asked = run["args"].get("available_at", "now")
    if asked in (None, "", "now"):
        local, label = datetime.now(timezone.utc).astimezone(ZoneInfo(m["tz"])), "open now per posted hours"
    else:
        t = datetime.fromisoformat(asked)
        local = (t if t.tzinfo else t.replace(tzinfo=ZoneInfo(m["tz"]))).astimezone(ZoneInfo(m["tz"]))
        label = f"open at {local.strftime('%a %H:%M')} per posted hours"
    o, c = m["hours"][DAYS[local.weekday()]]
    check(label, o <= local.strftime("%H:%M") <= c, f"{o}-{c} local; hours_verified={m['hours_verified']}")
    print(f"\n{'ALL CHECKS PASSED' if all(results) else 'SOME CHECKS FAILED'} ({sum(results)}/{len(results)})")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
