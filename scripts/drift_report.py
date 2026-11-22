#!/usr/bin/env python3
"""How often real menus change: counts from the live Nugttah feed log. Read-only.

  venv/bin/python scripts/drift_report.py [logs/feed.log]

Reads the change events the feed loop printed ("pulled nugttah changes [...]") and the current database,
and prints totals only: no keys, no names, no prices of single items. Notes on what is counted:
  - price: an item's price at a branch changed (Nugttah sends one record per brand item, one entry per branch)
  - unavailable: an item that was on sale was switched off (sold out or removed). Switching back on is not
    logged as an event, so this counts "went off", not "came back".
  - status: a branch switched between accepting orders and paused
  - the first full sync is not counted: loading a menu for the first time is not a change
"""
import ast
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from truemenu import config  # noqa: E402

LINE = re.compile(r"^\[feed\] (\S+) pulled nugttah(?: changes (\[.*\]))?\s*$")


def parse(path):
    pulls, events = [], []
    for raw in open(path, encoding="utf-8", errors="replace"):
        m = LINE.match(raw.strip())
        if not m:
            continue
        t = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        pulls.append(t)
        if m.group(2):
            try:
                for ev in ast.literal_eval(m.group(2)):
                    if isinstance(ev, dict):
                        events.append((t, ev))
            except (ValueError, SyntaxError):
                pass
    return pulls, events


def branch_of(item_id):
    return item_id.rsplit("-", 1)[0]


def main():
    log = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "logs" / "feed.log"
    pulls, events = parse(log)
    if not pulls:
        sys.exit(f"no Nugttah pulls found in {log}")
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    total_items = con.execute("SELECT COUNT(*) FROM items WHERE merchant_id LIKE 'ng-%'").fetchone()[0]
    listed = con.execute("SELECT COUNT(*) FROM items WHERE merchant_id LIKE 'ng-%' AND listed=1").fetchone()[0]
    branches = con.execute("SELECT COUNT(*) FROM merchants WHERE merchant_id LIKE 'ng-%'").fetchone()[0]
    paused_now = con.execute("SELECT COUNT(*) FROM merchants WHERE merchant_id LIKE 'ng-%' AND pos_status!='online'").fetchone()[0]

    start, end = min(pulls), max(pulls)
    hours = (end - start).total_seconds() / 3600
    gaps = sorted((b - a).total_seconds() for a, b in zip(pulls, pulls[1:]))
    by = defaultdict(list)
    for t, ev in events:
        by[ev.get("event")].append((t, ev))

    price = by["price"]
    moves = [(ev["to"] - ev["from"]) / ev["from"] * 100 for _, ev in price if ev.get("from")]
    changed = {ev["item_id"] for _, ev in price} | {ev["item_id"] for _, ev in by["unavailable"]}
    per_day = Counter(t.date().isoformat() for t, ev in events if ev.get("event") in ("price", "unavailable", "status"))

    print(f"Window: {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC ({hours:.1f} h), {len(pulls)} pulls, "
          f"median gap {statistics.median(gaps) if gaps else 0:.0f} s, longest gap {max(gaps) if gaps else 0:.0f} s")
    print(f"Now: {branches} branches ({paused_now} not accepting orders), {total_items} branch-items, {listed} on sale")
    print()
    print(f"Price changes:        {len(price)} events, {len({e['item_id'] for _, e in price})} branch-items, "
          f"{len({branch_of(e['item_id']) for _, e in price})} branches")
    if moves:
        ups = sum(1 for x in moves if x > 0)
        print(f"  direction: {ups} up, {len(moves) - ups} down; median size {statistics.median(abs(x) for x in moves):.1f}%")
    un = by["unavailable"]
    print(f"Went unavailable:     {len(un)} events, {len({e['item_id'] for _, e in un})} branch-items, "
          f"{len({branch_of(e['item_id']) for _, e in un})} branches")
    st = by["status"]
    print(f"Branch open/pause:    {len(st)} switches at {len({e['merchant_id'] for _, e in st})} branches")
    for k in sorted(set(by) - {"price", "unavailable", "status"}):
        print(f"Other ({k}): {len(by[k])} events")
    print()
    if total_items:
        print(f"Branch-items whose price or availability changed at least once: {len(changed)} of {total_items} "
              f"({len(changed) / total_items * 100:.1f}%)")
    if hours:
        n = len(price) + len(un) + len(st)
        print(f"Changes per hour: {n / hours:.1f}  (price {len(price) / hours:.1f}, unavailable {len(un) / hours:.1f}, "
              f"open/pause {len(st) / hours:.1f})")
    for d in sorted(per_day):
        print(f"  {d}: {per_day[d]} changes")


if __name__ == "__main__":
    main()
