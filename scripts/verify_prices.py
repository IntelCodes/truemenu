#!/usr/bin/env python3
"""Check TrueMenu's stored Nugttah menus against a fresh read of Nugttah's API. Read-only.

  set -a; source .env; set +a; venv/bin/python scripts/verify_prices.py [--sample N] [--gap 1.5]

For each branch it fetches the menu straight from Nugttah and compares every item with what TrueMenu
would quote right now: price (after the same VAT handling) and whether it is on sale. Prints totals only:
no keys, no names. A price mismatch means TrueMenu missed a change; the line says whether Nugttah's
updated_at moved (a change the feed should have caught) or not (a change Nugttah made without one).
Takes about 1.5 s per branch, to stay well under Nugttah's menu rate limit while the feed keeps running.
"""
import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from truemenu import config, nugttah  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="check only N random branches (default: all)")
    ap.add_argument("--gap", type=float, default=1.5, help="seconds between menu calls")
    a = ap.parse_args()
    nugttah.MENU_GAP_S = max(a.gap, nugttah.MENU_GAP_S)
    cats = nugttah.load_state().get("categories", {})
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    mids = [r[0] for r in con.execute("SELECT merchant_id FROM merchants WHERE merchant_id LIKE 'ng-%' ORDER BY merchant_id")]
    if a.sample:
        mids = random.sample(mids, min(a.sample, len(mids)))
    t0 = time.time()
    n = {"branches": 0, "no_menu": 0, "items": 0, "match": 0, "price_diff": 0, "price_diff_newer": 0,
         "on_sale_missing": 0, "off_sale_but_listed": 0, "listed_not_in_api": 0}
    diffs = []
    for mid in mids:
        bid = mid[len(nugttah.PREFIX):]
        menu = nugttah._get(f"/branches/{bid}/menu")
        if menu is None:
            n["no_menu"] += 1
            continue
        n["branches"] += 1
        db = {r["item_id"]: r for r in con.execute(
            "SELECT item_id, price_cents, listed, src_updated_at FROM items WHERE merchant_id=?", (mid,))}
        seen = set()
        for raw in menu.get("items", []):
            it = nugttah._item_row(raw, mid, {**raw, "branch_id": bid}, cats)
            seen.add(it["item_id"])
            n["items"] += 1
            row = db.get(it["item_id"])
            if not it["available"]:
                if row and row["listed"]:
                    n["off_sale_but_listed"] += 1
                else:
                    n["match"] += 1
                continue
            if not row or not row["listed"]:
                n["on_sale_missing"] += 1
            elif row["price_cents"] != it["price_cents"]:
                n["price_diff"] += 1
                try:
                    newer = nugttah._ts(it["updated_at"]) > nugttah._ts(row["src_updated_at"])
                except (TypeError, ValueError):
                    newer = False
                n["price_diff_newer"] += bool(newer)
                diffs.append(abs(it["price_cents"] - row["price_cents"]) / max(row["price_cents"], 1) * 100)
            else:
                n["match"] += 1
        n["listed_not_in_api"] += sum(1 for k, r in db.items() if r["listed"] and k not in seen)
    mins = (time.time() - t0) / 60
    print(f"Checked {n['branches']} branches ({n['no_menu']} without a menu), {n['items']} items, in {mins:.1f} min")
    print(f"Same as Nugttah right now:            {n['match']} ({n['match'] / max(n['items'], 1) * 100:.2f}%)")
    print(f"Price differs:                        {n['price_diff']} (Nugttah's updated_at is newer on {n['price_diff_newer']})"
          + (f", median gap {sorted(diffs)[len(diffs) // 2]:.1f}%" if diffs else ""))
    print(f"On sale at Nugttah, not in TrueMenu:  {n['on_sale_missing']}")
    print(f"Off sale at Nugttah, still offered:   {n['off_sale_but_listed']}")
    print(f"Offered by TrueMenu, gone at Nugttah: {n['listed_not_in_api']}")
    print("Differences can be changes made in the last 20 s that the next sync will pick up; run it twice to tell.")


if __name__ == "__main__":
    main()
