#!/usr/bin/env python3
"""How wrong is a menu copy? Replays the real open/pause switches from the live Nugttah feed. Read-only.

  venv/bin/python scripts/staleness_report.py [logs/feed.log] [--since 2026-11-21T02:28] [--until 2026-11-22T19:46]

--since / --until (UTC) replay one fixed window, so a figure on a slide can be reproduced later.

For every branch it rebuilds when the branch was taking orders and when it was paused, from the switches
the feed logged. Then it asks: an agent that works from a copy of the branch list refreshed every hour,
6 hours or day (the way scraped or cached menus work), how much of the time is that copy wrong? It also
reports TrueMenu's own lag, which is the gap between syncs. Totals only.

  wrong, sends order  the copy says the branch takes orders, but it is paused (the order fails)
  wrong, misses sale  the copy says paused, but the branch takes orders
Each copy policy is averaged over 12 refresh start times, so the result does not depend on when the
copy happened to be taken. Branches with no switches count as always right.
"""
import argparse
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from truemenu import config  # noqa: E402
from drift_report import parse  # noqa: E402

STEP = 30            # seconds between samples
PHASES = 12
POLICIES = [("hourly copy", 3600), ("every 6 hours", 6 * 3600), ("daily copy", 24 * 3600)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default=str(ROOT / "logs" / "feed.log"))
    ap.add_argument("--since")
    ap.add_argument("--until")
    a = ap.parse_args()
    log = Path(a.log)
    pulls, events = parse(log)
    utc = lambda x: datetime.fromisoformat(x).replace(tzinfo=timezone.utc)
    lo = utc(a.since) if a.since else None
    hi = utc(a.until) if a.until else None
    keep = lambda t: (lo is None or t >= lo) and (hi is None or t <= hi)
    pulls = [p for p in pulls if keep(p)]
    events = [(t, ev) for t, ev in events if keep(t)]
    if not pulls:
        sys.exit(f"no Nugttah pulls found in {log}")
    start, end = min(pulls), max(pulls)
    span = (end - start).total_seconds()
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    n_branches = con.execute("SELECT COUNT(*) FROM merchants WHERE merchant_id LIKE 'ng-%'").fetchone()[0]

    # per branch: [(seconds since start, status after)], plus the status before the first switch
    switches = {}
    for t, ev in events:
        if ev.get("event") == "status":
            switches.setdefault(ev["merchant_id"], []).append(((t - start).total_seconds(), ev["from"], ev["to"]))
    pull_s = sorted((p - start).total_seconds() for p in pulls)

    def status_at(sw, s):
        cur = sw[0][1]
        for at, _, to in sw:
            if at <= s:
                cur = to
            else:
                break
        return cur

    samples = int(span // STEP)
    total = n_branches * samples
    print(f"Window: {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC ({span / 3600:.1f} h), "
          f"{n_branches} branches, {sum(len(v) for v in switches.values())} open/pause switches at {len(switches)} branches")
    print()
    print(f"{'Agent works from':<18} {'wrong':>8} {'sends order':>12} {'misses sale':>12}   wrong per 1,000 orders")
    for name, period in POLICIES:
        bad_send = bad_miss = 0
        for ph in range(PHASES):
            offset = period * ph / PHASES
            for sw in switches.values():
                sw.sort()
                for i in range(samples):
                    s = i * STEP
                    taken = max(0.0, ((s + offset) // period) * period - offset)   # when the copy was last refreshed
                    truth, copy = status_at(sw, s), status_at(sw, taken)
                    if truth != copy:
                        if copy == "online":
                            bad_send += 1
                        else:
                            bad_miss += 1
        bad_send /= PHASES
        bad_miss /= PHASES
        wrong = (bad_send + bad_miss) / total
        print(f"{name:<18} {wrong * 100:>7.2f}% {bad_send / total * 100:>11.2f}% {bad_miss / total * 100:>11.2f}%   {wrong * 1000:.1f}")
    # TrueMenu: a switch is seen at the next sync, so each switch is wrong for at most the gap before it was seen
    lags = []
    for sw in switches.values():
        for at, _, _ in sw:
            i = next((k for k, p in enumerate(pull_s) if p >= at), None)
            if i:
                lags.append(pull_s[i] - pull_s[i - 1])
    tm_wrong = sum(lags) / (n_branches * span) if span else 0
    print(f"{'TrueMenu':<18} {tm_wrong * 100:>7.3f}%   (at most one sync behind: median {statistics.median(lags) if lags else 0:.0f} s, "
          f"longest {max(lags) if lags else 0:.0f} s)   {tm_wrong * 1000:.2f}")
    print()
    print("'Per 1,000 orders' assumes orders spread evenly across branches and hours; real orders cluster at meal times.")


if __name__ == "__main__":
    main()
