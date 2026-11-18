"""Nugttah merchant feed: real menus, prices, hours and ordering status from Nugttah's ordering platform.

Read-only. TrueMenu's server is the only caller (Nugttah allows one IP); agents never see these endpoints.

How the sync works (Nugttah's TrueMenu API, 2026-11-18):
  first sync   GET /changes (no since) for a starting cursor, GET /branches, then each branch's full menu.
               The cursor is taken BEFORE the menus, so anything that changes during the sync is replayed.
               Menus are fetched a few per tick, so the rest of the feed loop keeps running.
  every tick   GET /changes?since=<cursor> (paged). A successful call proves the feed is alive, so it
               refreshes the freshness of every Nugttah branch, even when nothing changed.
  hourly       GET /branches (names, hours, ordering status).
  nightly      each branch's full menu again (reconcile), a few per tick.

Limits: /branches and /changes share 120 calls a minute; menus have their own 60 a minute.
Prices are integers in halalas, currency SAR, times ISO-8601 UTC.

Settings (.env, never committed):
  NUGTTAH_API_BASE          https://api.nugttah.com/ai/truemenu/v1  (unset: use NUGTTAH_FIXTURE)
  NUGTTAH_API_KEY           only if Nugttah issues one; sent as a bearer token
  NUGTTAH_FIXTURE           folder of sample responses, for building and tests
  NUGTTAH_SYNC_INTERVAL_S   how often to call /changes (default 20)
  NUGTTAH_BRANCH_IDS        optional comma list to limit which branches are loaded
  NUGTTAH_MENUS_PER_TICK    menus fetched per tick during a sync (default 8)

  python -m truemenu.nugttah once      run one tick now (starts the first sync if needed)
  python -m truemenu.nugttah sync      run the whole first sync now, in the foreground
  python -m truemenu.nugttah show      print what was mapped
  python -m truemenu.nugttah state     print the cursor and sync progress
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import config, db, enrich, sanitize

PREFIX = "ng-"
STATUS = {"accepting_orders": "online", "online": "online", "paused": "paused", "busy": "busy", "closed": "paused"}
DAY_KEYS = {"mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun",
            "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
            "saturday": "sat", "sunday": "sun"}
# Pork is not sold in Saudi Arabia; the tag is set from the branch's country, not guessed from the name.
JURISDICTION_TAGS = {"SA": [("contains_pork", 0, 0.99)]}
BRANCHES_EVERY_S = 3600
RECONCILE_EVERY_S = 24 * 3600
FEED_GAP_S, MENU_GAP_S = 0.6, 1.1          # stay under 120/min and 60/min


class Forbidden(RuntimeError):
    pass


class CursorRejected(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_s(iso):
    if not iso:
        return 10 ** 9
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()


def interval():
    return int(os.environ.get("NUGTTAH_SYNC_INTERVAL_S", "20"))


def configured():
    return bool(os.environ.get("NUGTTAH_API_BASE") or os.environ.get("NUGTTAH_FIXTURE"))


def _allowed():
    ids = os.environ.get("NUGTTAH_BRANCH_IDS", "").strip()
    return {x.strip() for x in ids.split(",") if x.strip()} or None


# ---------------------------------------------------------------- state

def _state_path():
    return config.STATE_DIR / "nugttah_state.json"


def load_state():
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st):
    tmp = _state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    tmp.replace(_state_path())


# ---------------------------------------------------------------- HTTP

_last_call = {}


def _pace(kind, gap):
    wait = _last_call.get(kind, 0) + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call[kind] = time.time()


def _fixture(path, params):
    fx = Path(os.environ["NUGTTAH_FIXTURE"])
    if path == "/branches":
        name = "branches.json"
    elif path == "/changes":
        since = (params or {}).get("since")
        name = f"changes_{since}.json" if since else "changes_start.json"
        if not (fx / name).exists():
            return {"server_time": _now(), "branches": [], "items": [], "next_cursor": since, "has_more": False}
    else:
        bid = path.split("/")[2]
        name = f"menu_{bid}_delta.json" if (params or {}).get("updated_since") else f"menu_{bid}.json"
    return json.loads((fx / name).read_text(encoding="utf-8"))


def _get(path, params=None):
    base = os.environ.get("NUGTTAH_API_BASE")
    if not base:
        return _fixture(path, params)
    headers = {"Accept": "application/json"}
    if os.environ.get("NUGTTAH_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['NUGTTAH_API_KEY']}"
    menu = "/menu" in path
    for attempt in range(4):
        _pace("menu" if menu else "feed", MENU_GAP_S if menu else FEED_GAP_S)
        r = requests.get(base.rstrip("/") + path, params=params or {}, timeout=20, headers=headers)
        if r.status_code == 403:
            raise Forbidden("Nugttah answered 403: this server's IP is not on Nugttah's allowlist.")
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After") or 5 * (attempt + 1)))
            continue
        if r.status_code in (400, 404, 410) and path == "/changes" and (params or {}).get("since"):
            raise CursorRejected(f"Nugttah rejected the cursor ({r.status_code}); starting a full sync.")
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Nugttah kept answering 429 on {path}")


# ---------------------------------------------------------------- mapping (field names in one place)

def _hours(raw):
    out = {}
    for day, shifts in (raw or {}).items():
        key = DAY_KEYS.get(day.lower())
        if key:
            out[key] = [[s["open"], s["close"]] if isinstance(s, dict) else list(s) for s in (shifts or [])] or None
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
    vat = i.get("vat_inclusive")
    cents = int(i["price"])                       # halalas, like cents
    if vat is False:
        cents, vat_status = int(round(cents * 1.15)), "added"   # Saudi VAT 15%: show what the customer pays
    elif vat is True:
        vat_status = "included"
    else:
        vat_status = "unknown"                    # Nugttah sends null when it does not know
    return {
        "item_id": f"{merchant_id}-{i['item_id']}",
        "name": i.get("name_en") or i.get("name_ar"),
        "name_ar": i.get("name_ar"),
        "category": (i.get("category") or "").lower(),
        "description": i.get("description_en") or i.get("description_ar") or "",
        "price_cents": cents, "currency": i.get("currency") or "SAR", "vat_status": vat_status,
        "available": bool(i.get("available", True)) and int(i.get("price") or 0) > 0,
        "calories": i.get("calories"),
    }


def _item_branch(raw):
    return str(raw.get("branch_id"))


# ---------------------------------------------------------------- writes

def _upsert_branch(con, b, seen_at):
    """seen_at: Nugttah's server_time of the response, used as the status time (status_updated_at is always null)."""
    m = _branch_row(b)
    old = con.execute("SELECT pos_status, pos_status_at FROM merchants WHERE merchant_id=?", (m["merchant_id"],)).fetchone()
    status_at = b.get("status_updated_at") or (old["pos_status_at"] if old and old["pos_status"] == m["status"] else seen_at)
    con.execute(
        "INSERT INTO merchants(merchant_id,name,name_ar,area,address,tz,hours_json,hours_verified,synthetic,pos_status,pos_status_at,country) "
        "VALUES(?,?,?,?,?,?,?,1,0,?,?,?) ON CONFLICT(merchant_id) DO UPDATE SET name=excluded.name, name_ar=excluded.name_ar, "
        "area=excluded.area, address=excluded.address, tz=excluded.tz, hours_json=excluded.hours_json, hours_verified=1, "
        "pos_status=excluded.pos_status, pos_status_at=excluded.pos_status_at, country=excluded.country",
        (m["merchant_id"], m["name"], m["name_ar"], m["area"], m["address"], m["tz"], json.dumps(m["hours"]),
         m["status"], status_at, m["country"]))
    if old and old["pos_status"] != m["status"]:
        return {"merchant_id": m["merchant_id"], "event": "status", "from": old["pos_status"], "to": m["status"]}
    return None


def _upsert_item(con, raw, mid, country, t, events, new_items):
    it = _item_row(raw, mid)
    if not it["available"]:
        cur = con.execute("UPDATE items SET listed=0 WHERE item_id=? AND listed=1", (it["item_id"],))
        if cur.rowcount:
            events.append({"item_id": it["item_id"], "event": "unavailable"})
        return None
    name, f1 = sanitize.clean(it["name"])
    desc, f2 = sanitize.clean(it["description"])
    old = con.execute("SELECT price_cents, price_changed_at FROM items WHERE item_id=?", (it["item_id"],)).fetchone()
    changed_at = old["price_changed_at"] if old else t
    if old and old["price_cents"] != it["price_cents"]:
        changed_at = t
        events.append({"item_id": it["item_id"], "event": "price", "from": old["price_cents"] / 100, "to": it["price_cents"] / 100})
    if not old:
        new_items.add(it["item_id"])
    con.execute(
        "INSERT INTO items(item_id,merchant_id,name,name_ar,category,description,description_flags,price_cents,currency,"
        "price_verified,verified_on,verified_how,listed,observed_at,price_changed_at,calories,vat_status) "
        "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,1,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET name=excluded.name, name_ar=excluded.name_ar, "
        "category=excluded.category, description=excluded.description, description_flags=excluded.description_flags, "
        "price_cents=excluded.price_cents, currency=excluded.currency, price_verified=1, verified_on=excluded.verified_on, "
        "verified_how=excluded.verified_how, listed=1, observed_at=excluded.observed_at, "
        "price_changed_at=excluded.price_changed_at, calories=excluded.calories, vat_status=excluded.vat_status",
        (it["item_id"], mid, name, it["name_ar"], it["category"], desc, json.dumps(f1 + f2), it["price_cents"],
         it["currency"], t[:10], "merchant's own ordering system (Nugttah)", t, changed_at, it["calories"], it["vat_status"]))
    for tag, val, conf in JURISDICTION_TAGS.get(country, []):
        con.execute("INSERT INTO tags(item_id,tag,value,confidence,source,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(item_id,tag) DO UPDATE SET value=excluded.value, confidence=excluded.confidence, "
                    "source=excluded.source, updated_at=excluded.updated_at",
                    (it["item_id"], tag, val, conf, f"jurisdiction:{country}", t))
    return it["item_id"]


def _country(con, mid):
    row = con.execute("SELECT country FROM merchants WHERE merchant_id=?", (mid,)).fetchone()
    return (row["country"] if row else None) or "SA"


def _touch_source(con, mid, t, changed, data_as_of=None):
    src = con.execute("SELECT last_change_at FROM sources WHERE merchant_id=?", (mid,)).fetchone()
    con.execute(
        "INSERT INTO sources(merchant_id,kind,expected_interval_s,last_pull_at,last_change_at,pulls,merchant_data_as_of) "
        "VALUES(?,?,?,?,?,1,?) ON CONFLICT(merchant_id) DO UPDATE SET kind=excluded.kind, "
        "expected_interval_s=excluded.expected_interval_s, last_pull_at=excluded.last_pull_at, "
        "last_change_at=excluded.last_change_at, pulls=sources.pulls+1, "
        "merchant_data_as_of=COALESCE(excluded.merchant_data_as_of, sources.merchant_data_as_of)",
        (mid, "nugttah_api", interval(), t, t if (changed or not src) else src["last_change_at"], data_as_of))


def _apply_menu(con, menu, mid, t, events, new_items):
    full = menu.get("is_full_snapshot", True)
    country = _country(con, mid)
    n_before = len(events)
    seen = {x for x in (_upsert_item(con, raw, mid, country, t, events, new_items) for raw in menu.get("items", [])) if x}
    if full:   # only a full snapshot may delist what it does not contain; a delta lists only changes
        cur = con.execute(f"UPDATE items SET listed=0 WHERE merchant_id=? AND listed=1 AND item_id NOT IN "
                          f"({','.join('?' * len(seen)) or 'NULL'})", (mid, *seen))
        if cur.rowcount:
            events.append({"merchant_id": mid, "event": "delisted", "count": cur.rowcount})
    _touch_source(con, mid, t, len(events) > n_before, menu.get("data_as_of"))


def _branch_list(resp):
    return resp.get("branches", resp if isinstance(resp, list) else [])


def _keep(branch_id):
    allowed = _allowed()
    return allowed is None or str(branch_id) in allowed


# ---------------------------------------------------------------- sync steps

def _refresh_branches(st, events):
    resp = _get("/branches")
    seen_at = resp.get("server_time") or _now() if isinstance(resp, dict) else _now()
    ids = []
    with db.connect() as con:
        for b in _branch_list(resp):
            if not _keep(b["branch_id"]):
                continue
            ids.append(str(b["branch_id"]))
            ev = _upsert_branch(con, b, seen_at)
            if ev:
                events.append(ev)
    st["branches_at"] = _now()
    return ids


def _fetch_menus(st, events, new_items, budget):
    queue = st.get("menu_queue", [])
    done = 0
    while queue and done < budget:
        bid = queue[0]
        menu = _get(f"/branches/{bid}/menu")
        t = _now()
        with db.connect() as con:
            if con.execute("SELECT 1 FROM merchants WHERE merchant_id=?", (PREFIX + bid,)).fetchone():
                _apply_menu(con, menu, PREFIX + bid, t, events, new_items)
        queue.pop(0)
        done += 1
        st["menu_queue"] = queue
        save_state(st)
    if not queue and st.get("menu_queue_kind"):
        st[st.pop("menu_queue_kind") + "_done_at"] = _now()
    return done


def _start_full_sync(st, events, kind="first_sync"):
    st["cursor"] = _get("/changes").get("next_cursor")     # cursor first: changes during the sync are replayed
    ids = _refresh_branches(st, events)
    st["menu_queue"], st["menu_queue_kind"] = ids, kind
    st["reconcile_started_at"] = _now()
    save_state(st)


def _apply_changes(resp, events, new_items):
    seen_at = resp.get("server_time") or _now()
    t = _now()
    touched = set()
    with db.connect() as con:
        for b in resp.get("branches", []) or []:
            if _keep(b["branch_id"]):
                ev = _upsert_branch(con, b, seen_at)
                if ev:
                    events.append(ev)
        for raw in resp.get("items", []) or []:
            bid = _item_branch(raw)
            mid = PREFIX + bid
            if not _keep(bid) or not con.execute("SELECT 1 FROM sources WHERE merchant_id=?", (mid,)).fetchone():
                continue                       # branch not loaded yet: its full menu will bring this item
            n = len(events)
            _upsert_item(con, raw, mid, _country(con, mid), t, events, new_items)
            if len(events) > n:
                touched.add(mid)
        for mid in touched:
            _touch_source(con, mid, t, True)
    return len(resp.get("branches", []) or []) + len(resp.get("items", []) or [])


def _heartbeat(data_as_of=None):
    """A successful /changes call means every loaded branch is current as of now."""
    t = _now()
    with db.connect() as con:
        con.execute("UPDATE sources SET last_pull_at=?, pulls=pulls+1, expected_interval_s=?, "
                    "merchant_data_as_of=COALESCE(?, merchant_data_as_of) WHERE merchant_id LIKE ?",
                    (t, interval(), data_as_of, PREFIX + "%"))


def tick():
    """One step of the feed. Returns change events (price, unavailable, status, delisted)."""
    db.init()
    st = load_state()
    events, new_items = [], set()
    if not st.get("cursor"):
        _start_full_sync(st, events)
    # 1. changes since the cursor, every tick
    try:
        for _ in range(10):                                     # at most 10 pages per tick
            resp = _get("/changes", {"since": st["cursor"], "limit": 500})
            n = _apply_changes(resp, events, new_items)
            st["cursor"] = resp.get("next_cursor") or st["cursor"]
            st["changes_ok_at"] = _now()
            save_state(st)
            if not resp.get("has_more") and n < 500:
                break
        _heartbeat(resp.get("data_as_of"))
    except CursorRejected:
        st.pop("cursor", None)
        _start_full_sync(st, events, kind="resync")
    # 2. branch list hourly
    if _age_s(st.get("branches_at")) > BRANCHES_EVERY_S:
        _refresh_branches(st, events)
        save_state(st)
    # 3. nightly reconcile of full menus
    if not st.get("menu_queue") and _age_s(st.get("reconcile_started_at")) > RECONCILE_EVERY_S:
        st["menu_queue"] = [r["merchant_id"][len(PREFIX):] for r in _loaded_branches()]
        st["menu_queue_kind"], st["reconcile_started_at"] = "reconcile", _now()
        save_state(st)
    # 4. a few menus per tick, so the rest of the feed keeps its rhythm
    _fetch_menus(st, events, new_items, int(os.environ.get("NUGTTAH_MENUS_PER_TICK", "8")))
    save_state(st)
    if new_items:
        enrich.run(use_llm=False, only_items=new_items)
    return events


def _loaded_branches():
    with db.connect() as con:
        return list(con.execute("SELECT merchant_id FROM merchants WHERE merchant_id LIKE ?", (PREFIX + "%",)))


def sync_all():
    """The whole first sync in the foreground (about 4 minutes for 203 branches)."""
    events = tick()
    while load_state().get("menu_queue"):
        events += tick()
    return events


def pull(branch_ids=None):
    """What the feed loop calls: one tick. The first sync spreads over several ticks, so the loop never blocks."""
    return tick()


def show():
    from .query import freshness
    with db.connect() as con:
        for m in con.execute("SELECT * FROM merchants WHERE merchant_id LIKE ?", (PREFIX + "%",)):
            n = con.execute("SELECT COUNT(*) FROM items WHERE merchant_id=? AND listed=1", (m["merchant_id"],)).fetchone()[0]
            src = con.execute("SELECT * FROM sources WHERE merchant_id=?", (m["merchant_id"],)).fetchone()
            label = freshness(src)["label"] if src else "no menu yet"
            print(f"{m['merchant_id']:<16} {m['name']:<40} {m['area']:<24} items {n:>3}  status {m['pos_status']:<7} "
                  f"freshness {label}")


if __name__ == "__main__":
    cmd = (sys.argv[1:] or ["once"])[0]
    if cmd == "once":
        print(tick() or "no changes")
    elif cmd == "sync":
        print(f"{len(sync_all())} events")
        show()
    elif cmd == "show":
        show()
    elif cmd == "state":
        st = load_state()
        print({k: (len(v) if isinstance(v, list) else v) for k, v in st.items()})
    else:
        print(__doc__)
