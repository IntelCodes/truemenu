"""Nugttah merchant feed: real menus, prices, hours and ordering status from Nugttah's ordering platform.

Read-only. TrueMenu's server is the only caller; agents never see these endpoints or the key.

Settings (.env, never committed):
  NUGTTAH_API_BASE          e.g. https://api.example/ai/truemenu/v1  (unset: use NUGTTAH_FIXTURE)
  NUGTTAH_API_KEY           read-only key issued to TrueMenu
  NUGTTAH_FIXTURE           folder with branches.json and menu_<branch_id>.json, for building and tests
  NUGTTAH_SYNC_INTERVAL_S   how often to pull (default 60)

  python -m truemenu.nugttah once      pull every allowlisted branch now
  python -m truemenu.nugttah show      print what was mapped

The mapping below follows the format requested from Nugttah on 2026-11-16. When the real endpoints
arrive, only the field names in _branch_row and _item_row should need to change.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import db, enrich, sanitize

PREFIX = "ng-"
STATUS = {"accepting_orders": "online", "online": "online", "paused": "paused", "busy": "busy", "closed": "paused"}
DAY_KEYS = {"mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun",
            "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
            "saturday": "sat", "sunday": "sun"}
# Pork is not sold in Saudi Arabia; the tag is set from the branch's country, not guessed from the name.
JURISDICTION_TAGS = {"SA": [("contains_pork", 0, 0.99)]}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def interval():
    return int(os.environ.get("NUGTTAH_SYNC_INTERVAL_S", "60"))


def configured():
    return bool(os.environ.get("NUGTTAH_API_BASE") or os.environ.get("NUGTTAH_FIXTURE"))


def _get(path, params=None):
    base = os.environ.get("NUGTTAH_API_BASE")
    if base:
        r = requests.get(base.rstrip("/") + path, params=params or {}, timeout=15,
                         headers={"Authorization": f"Bearer {os.environ.get('NUGTTAH_API_KEY', '')}",
                                  "Accept": "application/json"})
        r.raise_for_status()
        return r.json()
    fx = Path(os.environ["NUGTTAH_FIXTURE"])
    name = "branches.json" if path == "/branches" else f"menu_{path.split('/')[2]}.json"
    return json.loads((fx / name).read_text(encoding="utf-8"))


def _hours(raw):
    out = {}
    for day, shifts in (raw or {}).items():
        key = DAY_KEYS.get(day.lower())
        if key:
            out[key] = [[s["open"], s["close"]] if isinstance(s, dict) else list(s) for s in shifts] or None
    return out


def _branch_row(b):
    city, district = b.get("city", ""), b.get("district", "")
    return {
        "merchant_id": PREFIX + str(b["branch_id"]),
        "name": f"{b.get('brand_name_en') or b.get('brand_name_ar')} ({b.get('branch_name_en') or district})",
        "name_ar": b.get("brand_name_ar"),
        "area": ", ".join(x for x in (district, city) if x),
        "address": ", ".join(x for x in (b.get("address_en"), district, city) if x),
        "tz": b.get("timezone") or "Asia/Riyadh",
        "hours": _hours(b.get("opening_hours")),
        "status": STATUS.get((b.get("ordering_status") or "").lower(), "paused"),
        "country": (b.get("country") or "SA").upper(),
    }


def _item_row(i, merchant_id):
    vat = i.get("vat_inclusive", True)
    cents = int(i["price"])                       # halalas, like cents
    if not vat:
        cents = int(round(cents * 1.15))         # Saudi VAT 15%: show what the customer pays
    return {
        "item_id": f"{merchant_id}-{i['item_id']}",
        "name": i.get("name_en") or i.get("name_ar"),
        "name_ar": i.get("name_ar"),
        "category": (i.get("category") or "").lower(),
        "description": i.get("description_en") or i.get("description_ar") or "",
        "price_cents": cents, "currency": i.get("currency", "SAR"),
        "available": bool(i.get("available", True)),
        "calories": i.get("calories"),
    }


def pull(branch_ids=None):
    """Pull allowlisted branches from Nugttah. Returns change events."""
    db.init()
    t = _now()
    events, new_items = [], set()
    branches = _get("/branches")
    rows = branches.get("branches", branches if isinstance(branches, list) else [])
    with db.connect() as con:
        for b in rows:
            m = _branch_row(b)
            mid = m["merchant_id"]
            if branch_ids is not None and mid not in branch_ids:
                continue
            menu = _get(f"/branches/{b['branch_id']}/menu")
            con.execute(
                "INSERT INTO merchants(merchant_id,name,name_ar,area,address,tz,hours_json,hours_verified,synthetic,pos_status,pos_status_at,country) "
                "VALUES(?,?,?,?,?,?,?,1,0,?,?,?) ON CONFLICT(merchant_id) DO UPDATE SET name=excluded.name, name_ar=excluded.name_ar, "
                "area=excluded.area, address=excluded.address, tz=excluded.tz, hours_json=excluded.hours_json, hours_verified=1, "
                "pos_status=excluded.pos_status, pos_status_at=excluded.pos_status_at, country=excluded.country",
                (mid, m["name"], m["name_ar"], m["area"], m["address"], m["tz"], json.dumps(m["hours"]),
                 m["status"], b.get("status_updated_at") or t, m["country"]))
            seen, changed = set(), False
            for raw in menu.get("items", []):
                it = _item_row(raw, mid)
                if not it["available"]:
                    continue                                   # hidden or out of stock: not offered
                seen.add(it["item_id"])
                name, f1 = sanitize.clean(it["name"])
                desc, f2 = sanitize.clean(it["description"])
                old = con.execute("SELECT price_cents, price_changed_at FROM items WHERE item_id=?", (it["item_id"],)).fetchone()
                changed_at = old["price_changed_at"] if old else t
                if old and old["price_cents"] != it["price_cents"]:
                    changed_at, changed = t, True
                    events.append({"item_id": it["item_id"], "event": "price",
                                   "from": old["price_cents"] / 100, "to": it["price_cents"] / 100})
                if not old:
                    new_items.add(it["item_id"])
                con.execute(
                    "INSERT INTO items(item_id,merchant_id,name,name_ar,category,description,description_flags,price_cents,currency,"
                    "price_verified,verified_on,verified_how,listed,observed_at,price_changed_at,calories) "
                    "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,1,?,?,?) ON CONFLICT(item_id) DO UPDATE SET name=excluded.name, name_ar=excluded.name_ar, "
                    "category=excluded.category, description=excluded.description, description_flags=excluded.description_flags, "
                    "price_cents=excluded.price_cents, currency=excluded.currency, price_verified=1, verified_on=excluded.verified_on, "
                    "verified_how=excluded.verified_how, listed=1, observed_at=excluded.observed_at, "
                    "price_changed_at=excluded.price_changed_at, calories=excluded.calories",
                    (it["item_id"], mid, name, it["name_ar"], it["category"], desc, json.dumps(f1 + f2), it["price_cents"],
                     it["currency"], t[:10], "merchant's own ordering system (Nugttah)", t, changed_at, it["calories"]))
                for tag, val, conf in JURISDICTION_TAGS.get(m["country"], []):
                    con.execute("INSERT INTO tags(item_id,tag,value,confidence,source,updated_at) VALUES(?,?,?,?,?,?) "
                                "ON CONFLICT(item_id,tag) DO UPDATE SET value=excluded.value, confidence=excluded.confidence, "
                                "source=excluded.source, updated_at=excluded.updated_at",
                                (it["item_id"], tag, val, conf, f"jurisdiction:{m['country']}", t))
            con.execute(f"UPDATE items SET listed=0 WHERE merchant_id=? AND item_id NOT IN ({','.join('?' * len(seen)) or 'NULL'})",
                        (mid, *seen))
            src = con.execute("SELECT last_change_at FROM sources WHERE merchant_id=?", (mid,)).fetchone()
            con.execute(
                "INSERT INTO sources(merchant_id,kind,expected_interval_s,last_pull_at,last_change_at,pulls,merchant_data_as_of) "
                "VALUES(?,?,?,?,?,1,?) ON CONFLICT(merchant_id) DO UPDATE SET kind=excluded.kind, "
                "expected_interval_s=excluded.expected_interval_s, last_pull_at=excluded.last_pull_at, "
                "last_change_at=excluded.last_change_at, pulls=sources.pulls+1, merchant_data_as_of=excluded.merchant_data_as_of",
                (mid, "nugttah_api", interval(), t, t if (changed or not src) else src["last_change_at"], menu.get("data_as_of")))
    if new_items:
        enrich.run(use_llm=False, only_items=new_items)
    return events


def show():
    from .query import freshness
    with db.connect() as con:
        for m in con.execute("SELECT * FROM merchants WHERE merchant_id LIKE ?", (PREFIX + "%",)):
            n = con.execute("SELECT COUNT(*) FROM items WHERE merchant_id=? AND listed=1", (m["merchant_id"],)).fetchone()[0]
            src = con.execute("SELECT * FROM sources WHERE merchant_id=?", (m["merchant_id"],)).fetchone()
            print(f"{m['merchant_id']:<16} {m['name']:<40} {m['area']:<24} items {n:>3}  status {m['pos_status']:<7} "
                  f"freshness {freshness(src)['label']}")


if __name__ == "__main__":
    cmd = (sys.argv[1:] or ["once"])[0]
    if cmd == "once":
        print(pull() or "pulled, no changes")
    elif cmd == "show":
        show()
    else:
        print(__doc__)
