#!/usr/bin/env python3
"""Check the live booth end to end: one valid quote is paid, and each kind of change after the quote blocks payment.

  set -a; source .env; set +a; venv/bin/python scripts/verify_live.py [--url http://127.0.0.1:8088]

Runs on the server, against the running API, using only the Kendall Square demo menus (never Nugttah).
For each case it asks /demo/api/lunch for a quote, makes one change at the demo restaurant, syncs that
restaurant, and calls /demo/api/pay exactly as the phone page does:

  valid                     nothing changed                 -> paid
  price_changed_since_quote price raised by $2              -> blocked
  unavailable_since_quote   dish switched off sale          -> blocked
  branch_not_taking_orders  restaurant paused ordering      -> blocked
  data_stale                restaurant stops syncing        -> blocked (waits about 2 sync intervals)
  valid                     everything put back             -> paid

The overrides that were in place before the run are put back at the end, even if a case fails.
Takes about a minute. Exit code 0 only if every case passes. Shows up in the presenter feed as asks,
payments and blocks, so run it before the doors open, not during a visit.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from truemenu import config, feed  # noqa: E402

CASES = ["valid", "price_changed_since_quote", "unavailable_since_quote", "branch_not_taking_orders", "data_stale", "valid"]


def http_post(base):
    visitor = "verify-" + uuid.uuid4().hex[:12]

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "x-visitor": visitor})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    return post


def wait_until_stale(mid):
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    interval = con.execute("SELECT expected_interval_s FROM sources WHERE merchant_id=?", (mid,)).fetchone()[0]
    con.close()
    wait = int(interval) * 2 + 3
    print(f"      waiting {wait} s for {mid} to go stale (sync interval {interval} s)", flush=True)
    time.sleep(wait)


def run(post, make_stale=wait_until_stale, say=print):
    """Returns the list of (case, ok, detail). `post(path, body)` returns the decoded JSON reply."""
    before = feed._load_overrides()
    results = []
    try:
        for case in CASES:
            feed._save_overrides({"prices": {}, "status": {}, "paused_sources": [], "sold_out": []})
            feed.pull()                                            # the demo menus only; Nugttah syncs in its own loop
            pick = post("/demo/api/lunch", {"budget_usd_max": 20})["pick"]
            if not pick:
                results.append((case, False, "no quote offered"))
                continue
            iid, mid, price = pick["item_id"], pick["merchant"]["id"], pick["price"]["amount"]
            ov = feed._load_overrides()
            if case == "price_changed_since_quote":
                ov["prices"][iid] = round(price + 2, 2)
            elif case == "unavailable_since_quote":
                ov["sold_out"].append(iid)
            elif case == "branch_not_taking_orders":
                ov["status"][mid] = "paused"
            elif case == "data_stale":
                ov["paused_sources"].append(mid)
            feed._save_overrides(ov)
            feed.pull([mid])
            if case == "data_stale":
                make_stale(mid)
            r = post("/demo/api/pay", {"quote_id": pick["quote_id"]})
            verdict = r["verification"].get("verdict")
            ok = (r["paid"] is True) if case == "valid" else (r["paid"] is False and verdict == case)
            detail = f"{pick['name']} ${price:.2f}: paid={r['paid']} verdict={verdict}"
            results.append((case, ok, detail))
            say(f"{'PASS' if ok else 'FAIL'}  {case:<26} {detail}")
    finally:
        feed._save_overrides(before)
        feed.pull()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.environ.get('TRUEMENU_PORT', '8088')}")
    a = ap.parse_args()
    res = run(http_post(a.url.rstrip("/")))
    bad = [c for c, ok, _ in res if not ok]
    print(f"\n{len(res) - len(bad)} of {len(res)} cases passed" + (f"; failed: {', '.join(bad)}" if bad else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
