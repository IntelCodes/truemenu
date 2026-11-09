"""Simulated POS feed.

Stands in for the merchant POS sync. It "pulls" each source on that source's own interval from the
ground-truth file, applies demo-time overrides (price changes, branch paused), and records:
  observed_at      = time of the last successful pull for that source (what freshness is based on)
  price_changed_at = time a pull saw a different price than the previous pull (change detection by diff)

Commands:
  python -m truemenu.feed once                    pull every source now
  python -m truemenu.feed loop                    keep pulling on each source's interval (Ctrl-C or `stop`)
  python -m truemenu.feed stop                    stop the background loop
  python -m truemenu.feed set-price ITEM USD      change a price; applied on the next pull
  python -m truemenu.feed set-status MERCHANT online|paused|busy
  python -m truemenu.feed pause-source MERCHANT   that source stops syncing (its data goes stale)
  python -m truemenu.feed resume-source MERCHANT
  python -m truemenu.feed clear                   remove all overrides
  python -m truemenu.feed enrich [--llm]          (re)infer tags for all items
  python -m truemenu.feed status                  show each source's freshness
"""
import csv
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

from . import config, db, enrich, sanitize


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_overrides():
    if config.OVERRIDES.exists():
        return json.loads(config.OVERRIDES.read_text())
    return {"prices": {}, "status": {}, "paused_sources": []}


def _save_overrides(o):
    tmp = config.OVERRIDES.with_suffix(".tmp")
    tmp.write_text(json.dumps(o, indent=2))
    tmp.replace(config.OVERRIDES)


def _interval(m):
    if config.SYNC_INTERVAL_OVERRIDE:
        return int(config.SYNC_INTERVAL_OVERRIDE)
    return int(m.get("source", {}).get("sync_interval_s", 86400))


def load_sources():
    merchants = json.loads(config.MERCHANTS.read_text())["merchants"]
    rows = list(csv.DictReader(open(config.GROUND_TRUTH, newline="", encoding="utf-8")))
    return merchants, rows


def pull(merchant_ids=None):
    """Pull the given sources (default: all not paused). Returns list of change events."""
    db.init()
    merchants, rows = load_sources()
    ov = _load_overrides()
    t = now_iso()
    events = []
    with db.connect() as con:
        for m in merchants:
            mid = m["merchant_id"]
            if merchant_ids is not None and mid not in merchant_ids:
                continue
            if mid in ov.get("paused_sources", []):
                continue
            status = ov.get("status", {}).get(mid, "online")
            prev = con.execute("SELECT pos_status FROM merchants WHERE merchant_id=?", (mid,)).fetchone()
            con.execute(
                "INSERT INTO merchants(merchant_id,name,area,address,tz,hours_json,hours_verified,synthetic,pos_status,pos_status_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(merchant_id) DO UPDATE SET name=excluded.name, area=excluded.area, "
                "address=excluded.address, tz=excluded.tz, hours_json=excluded.hours_json, hours_verified=excluded.hours_verified, "
                "synthetic=excluded.synthetic, pos_status=excluded.pos_status, pos_status_at=excluded.pos_status_at",
                (mid, m["name"], m["area"], m["address"], m["tz"], json.dumps(m["hours"]),
                 int(bool(m.get("hours_verified"))), int(bool(m.get("synthetic"))), status, t))
            if prev and prev["pos_status"] != status:
                events.append({"merchant_id": mid, "event": "status", "from": prev["pos_status"], "to": status})
            changed_any = False
            seen = set()
            for r in (r for r in rows if r["merchant_id"] == mid):
                iid = r["item_id"]
                seen.add(iid)
                price = ov.get("prices", {}).get(iid, r["price_usd"])
                cents = int(round(float(price) * 100))
                desc, flags = sanitize.clean(r.get("description", ""))
                name, nflags = sanitize.clean(r["name"])
                verified = r.get("verified", "").strip().lower() in ("yes", "synthetic")
                old = con.execute("SELECT price_cents, price_changed_at FROM items WHERE item_id=?", (iid,)).fetchone()
                changed_at = old["price_changed_at"] if old else t
                if old and old["price_cents"] != cents:
                    changed_at = t
                    changed_any = True
                    events.append({"item_id": iid, "event": "price", "from": old["price_cents"] / 100, "to": cents / 100})
                con.execute(
                    "INSERT INTO items(item_id,merchant_id,name,category,description,description_flags,price_cents,currency,"
                    "price_verified,verified_on,verified_how,listed,observed_at,price_changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?) "
                    "ON CONFLICT(item_id) DO UPDATE SET merchant_id=excluded.merchant_id, name=excluded.name, category=excluded.category, "
                    "description=excluded.description, description_flags=excluded.description_flags, price_cents=excluded.price_cents, "
                    "price_verified=excluded.price_verified, verified_on=excluded.verified_on, verified_how=excluded.verified_how, "
                    "listed=1, observed_at=excluded.observed_at, price_changed_at=excluded.price_changed_at",
                    (iid, mid, name, r.get("category", ""), desc, json.dumps(flags + nflags), cents, "USD",
                     int(verified), r.get("verified_on", ""), r.get("verified_how", ""), t, changed_at))
                for tag in filter(None, (x.strip() for x in (r.get("merchant_tags") or "").split("|"))):
                    tagname = tag
                    con.execute("INSERT INTO tags(item_id,tag,value,confidence,source,updated_at) VALUES(?,?,1,1.0,'merchant_stated',?) "
                                "ON CONFLICT(item_id,tag) DO UPDATE SET value=1, confidence=1.0, source='merchant_stated', updated_at=excluded.updated_at",
                                (iid, tagname, t))
            # Items no longer in the feed are unlisted, not deleted.
            con.execute(f"UPDATE items SET listed=0 WHERE merchant_id=? AND item_id NOT IN ({','.join('?' * len(seen)) or 'NULL'})",
                        (mid, *seen))
            src = con.execute("SELECT last_change_at FROM sources WHERE merchant_id=?", (mid,)).fetchone()
            con.execute(
                "INSERT INTO sources(merchant_id,kind,expected_interval_s,last_pull_at,last_change_at,pulls) VALUES(?,?,?,?,?,1) "
                "ON CONFLICT(merchant_id) DO UPDATE SET kind=excluded.kind, expected_interval_s=excluded.expected_interval_s, "
                "last_pull_at=excluded.last_pull_at, last_change_at=excluded.last_change_at, pulls=sources.pulls+1",
                (mid, m["source"]["kind"], _interval(m), t, t if (changed_any or not src) else src["last_change_at"]))
        missing = [r[0] for r in con.execute("SELECT item_id FROM items WHERE item_id NOT IN (SELECT DISTINCT item_id FROM tags)")]
    if missing:
        enrich.run(use_llm=False, only_items=set(missing))
    return events


def loop():
    config.FEED_PID.write_text(str(os.getpid()))
    merchants, _ = load_sources()
    last = {}
    print(f"[feed] running, pid {os.getpid()}; intervals: " + ", ".join(f"{m['merchant_id']}={_interval(m)}s" for m in merchants), flush=True)
    try:
        while True:
            due = [m["merchant_id"] for m in merchants if time.time() - last.get(m["merchant_id"], 0) >= _interval(m)]
            if due:
                ev = pull(set(due))
                for mid in due:
                    last[mid] = time.time()
                paused = _load_overrides().get("paused_sources", [])
                live = [d for d in due if d not in paused]
                print(f"[feed] {now_iso()} pulled {live}" + (f" skipped(paused) {[d for d in due if d in paused]}" if len(live) < len(due) else "")
                      + (f" changes {ev}" if ev else ""), flush=True)
            time.sleep(2)
    finally:
        if config.FEED_PID.exists():
            config.FEED_PID.unlink()


def stop():
    if not config.FEED_PID.exists():
        print("feed not running")
        return
    pid = int(config.FEED_PID.read_text())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"stopped feed pid {pid}")
    except ProcessLookupError:
        print("feed pid not alive")
    config.FEED_PID.unlink(missing_ok=True)


def status():
    from .query import freshness
    db.init()
    with db.connect() as con:
        for s in con.execute("SELECT * FROM sources ORDER BY merchant_id"):
            f = freshness(s)
            print(f"{s['merchant_id']:<24} {f['label']:<6} age {f['age_s']:>6}s  interval {s['expected_interval_s']}s  "
                  f"pulls {s['pulls']}  last_change {s['last_change_at']}")


def main(argv):
    if not argv:
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    ov = _load_overrides()
    if cmd == "once":
        print(pull() or "pulled, no changes")
    elif cmd == "loop":
        loop()
    elif cmd == "stop":
        stop()
    elif cmd == "set-price":
        ov.setdefault("prices", {})[args[0]] = float(args[1]); _save_overrides(ov)
        print(f"override: {args[0]} -> ${float(args[1]):.2f} (applies on next pull)")
    elif cmd == "set-status":
        ov.setdefault("status", {})[args[0]] = args[1]; _save_overrides(ov)
        print(f"override: {args[0]} status -> {args[1]} (applies on next pull)")
    elif cmd == "pause-source":
        ps = ov.setdefault("paused_sources", [])
        if args[0] not in ps:
            ps.append(args[0])
        _save_overrides(ov); print(f"source {args[0]} paused: it will stop syncing and go stale")
    elif cmd == "resume-source":
        ov["paused_sources"] = [p for p in ov.get("paused_sources", []) if p != args[0]]
        _save_overrides(ov); print(f"source {args[0]} resumed")
    elif cmd == "clear":
        _save_overrides({"prices": {}, "status": {}, "paused_sources": []}); print("overrides cleared")
    elif cmd == "enrich":
        from . import llm
        llm.load_env_file(os.environ.get("AGENT_MODEL_ENV", "~/hw4/.env"))
        db.init(); print(enrich.run(use_llm="--llm" in args))
    elif cmd == "status":
        status()
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
