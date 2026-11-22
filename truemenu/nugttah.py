"""Nugttah merchant feed: real menus, prices, hours and ordering status from Nugttah's ordering platform.

Read-only. TrueMenu's server is the only caller: Nugttah requires both its allowlisted IP and a key.
Agents never see these endpoints. Format: "Nugttah TrueMenu Feed API, Integration Guide" (Nov 2026).

How the sync works:
  first sync   GET /changes (no since) for a starting cursor, GET /branches, then each branch's full menu.
               The cursor is taken BEFORE the menus, so anything that changes during the sync is replayed.
               Menus are fetched a few per tick, so the rest of the feed loop keeps its rhythm.
  every tick   one /changes pass (all pages). The cursor is saved only after the whole pass succeeds.
               A successful pass proves the feed is alive, so every loaded branch counts as current.
  hourly       GET /branches. A branch missing from the list left the pilot: its items are delisted.
  nightly      each branch's full menu again, a few per tick (Nugttah's backstop for missed changes).
  any 400 on /changes: the cursor is too old or unreadable, so start a full resync.

Rules from Nugttah's guide: upsert by id; apply a record only if its updated_at is the same or newer;
repeats are normal; a /changes item is one record per brand with a branches[] list of per-branch
price, availability and VAT; deleted or hidden items arrive with deleted:true or available:false.

Settings (.env, never committed):
  NUGTTAH_API_BASE          https://<api host>/ai/truemenu/v1  (unset: use NUGTTAH_FIXTURE)
  NUGTTAH_API_KEY           read-only key, sent as Authorization: Bearer <key>
  NUGTTAH_FIXTURE           folder of sample responses, for building and tests
  NUGTTAH_SYNC_INTERVAL_S   how often to run a /changes pass (default 20)
  NUGTTAH_BRANCH_IDS        optional comma list to limit which branches are loaded
  NUGTTAH_EXCLUDE_PATTERN   regex on branch names to skip test, warehouse and lab branches
  NUGTTAH_MENUS_PER_TICK    menus fetched per tick during a sync (default 8)

  python -m truemenu.nugttah once      run one tick now (starts the first sync if needed)
  python -m truemenu.nugttah sync      run the whole first sync now, in the foreground
  python -m truemenu.nugttah show      print what was mapped
  python -m truemenu.nugttah state     print the cursor and sync progress
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import config, db, enrich, sanitize

PREFIX = "ng-"
STATUS = {"accepting_orders": "online", "paused": "paused"}
DAY_KEYS = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
            "saturday": "sat", "sunday": "sun", "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
            "fri": "fri", "sat": "sat", "sun": "sun"}
# Pork is not sold in Saudi Arabia; the tag is set from the branch's country, not guessed from the name.
JURISDICTION_TAGS = {"SA": [("contains_pork", 0, 0.99)]}
DEFAULT_EXCLUDE = r"\b(test|testing|warehouse|lab)\b|تجرب|تجارب|اختبار|مستودع|معمل"
BRANCHES_EVERY_S = 3600
RECONCILE_EVERY_S = 24 * 3600
FEED_GAP_S, MENU_GAP_S = 0.6, 1.1          # stay under 120/min (branches + changes) and 60/min (menus)
MAX_PAGES = 30


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


def _en(v):
    return (v.get("en") or v.get("ar") or "") if isinstance(v, dict) else (v or "")


def _ar(v):
    return v.get("ar") if isinstance(v, dict) else None


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
    tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
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
        if since == "expired":
            raise CursorRejected("fixture: cursor too old")
        if not (fx / name).exists():
            return {"statusCode": 200, "server_time": _now(), "has_more": False, "next_cursor": since,
                    "branches": [], "categories": [], "items": []}
    else:
        name = f"menu_{path.split('/')[2]}.json"
        if not (fx / name).exists():
            return None                                   # like a 404: not in the pilot
    return _unwrap(json.loads((fx / name).read_text(encoding="utf-8")))


def _unwrap(body):
    """The live API wraps every payload: {"statusCode", "message", "data": {server_time, ...}}.
    The menu endpoint names the wrapper "date" instead of "data" (seen 2026-11-20), so accept both."""
    if isinstance(body, dict) and "statusCode" in body:
        for key in ("data", "date"):
            if isinstance(body.get(key), dict):
                return body[key]
    return body


def _get(path, params=None):
    """JSON payload, or None for a menu that answers 404 (branch unknown or not in the pilot)."""
    base = os.environ.get("NUGTTAH_API_BASE")
    if not base:
        return _fixture(path, params)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {os.environ.get('NUGTTAH_API_KEY', '')}"}
    menu = "/menu" in path
    for attempt in range(4):
        _pace("menu" if menu else "feed", MENU_GAP_S if menu else FEED_GAP_S)
        r = requests.get(base.rstrip("/") + path, params=params or {}, timeout=20, headers=headers)
        if r.status_code == 401:
            raise Forbidden("Nugttah answered 401: the key is missing, wrong or revoked (NUGTTAH_API_KEY).")
        if r.status_code == 403:
            raise Forbidden("Nugttah answered 403: this server's IP is not on Nugttah's allowlist.")
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After") or 10 * (attempt + 1)))
            continue
        if r.status_code == 400 and path == "/changes":
            raise CursorRejected("Nugttah answered 400 on /changes; starting a full resync.")
        if r.status_code == 404 and menu:
            return None
        r.raise_for_status()
        return _unwrap(r.json())
    raise RuntimeError(f"Nugttah kept answering 429 on {path}")


# ---------------------------------------------------------------- mapping (Nugttah field names live here)

def _hours(raw):
    """[{"day":"sunday","shifts":[{"open","close","closes_next_day"}]}] -> {"sun": [["06:00","12:00"], ...]}.
    A shift with closes_next_day has close < open, which the opening-hours check reads as 'after midnight'."""
    out = {}
    if isinstance(raw, dict):                                  # older sample format: {"sun": [...]}
        raw = [{"day": d, "shifts": s} for d, s in raw.items()]
    for day in raw or []:
        key = DAY_KEYS.get((day.get("day") or "").lower())
        if key:
            out[key] = [[s["open"], s["close"]] for s in (day.get("shifts") or [])] or None
    return out


def _branch_row(b):
    city, district = _en(b.get("city")), _en(b.get("district"))
    brand, branch = _en(b.get("brand_name")), _en(b.get("branch_name"))
    name = branch if brand and brand.lower() in branch.lower() else " - ".join(x for x in (brand, branch) if x)
    return {
        "merchant_id": PREFIX + str(b["branch_id"]),
        "name": name or brand,
        "name_ar": _ar(b.get("branch_name")) or _ar(b.get("brand_name")),
        "area": ", ".join(x for x in (district, city) if x),
        "address": ", ".join(x for x in (district, city) if x),
        "tz": b.get("time_zone") or "Asia/Riyadh",
        "hours": _hours(b.get("opening_hours")),
        "status": STATUS.get((b.get("ordering_status") or "").lower(), "paused"),
        "country": "SA",
        "updated_at": b.get("updated_at"),
    }


def _excluded(b):
    pat = os.environ.get("NUGTTAH_EXCLUDE_PATTERN", DEFAULT_EXCLUDE)
    names = " ".join(filter(None, [_en(b.get("branch_name")), _ar(b.get("branch_name")) or ""]))
    return bool(pat and re.search(pat, names, re.IGNORECASE))


def _required_extra(groups, branch_id=None):
    """Halalas a customer must add for required choices (cheapest available options, after free ones).
    None when a required group has no available option at all."""
    extra = 0
    for g in groups or []:
        if not g.get("required"):
            continue
        prices = sorted(int(o.get("price_change") or 0) for o in g.get("options") or []
                        if o.get("available", True) and branch_id not in (o.get("unavailable_branch_ids") or []))
        need = max(int(g.get("min") or 1), 1)
        if len(prices) < need:
            return None
        extra += sum(prices[int(g.get("free_options") or 0):need])
    return extra


def _item_row(i, merchant_id, branch, categories):
    """i = item record; branch = that branch's {price, available, vat_inclusive} (a /changes entry or the menu item)."""
    vat = branch.get("vat_inclusive")
    cents = int(branch.get("price") or 0)                   # halalas, like cents
    if vat is False:
        cents, vat_status = int(round(cents * 1.15)), "added"   # Saudi VAT 15%: show what the customer pays
    elif vat is True:
        vat_status = "included"
    else:
        vat_status = "unknown"           # Nugttah: the price is what the customer pays, VAT inside or exempt unknown
    cat = categories.get(i.get("category_id")) or {}
    extra = _required_extra(i.get("option_groups"), branch.get("branch_id"))
    return {
        "item_id": f"{merchant_id}-{i['item_id']}",
        "name": _en(i.get("name")),
        "name_ar": _ar(i.get("name")),
        "category": (cat.get("name") or "").lower(),
        "description": _en(i.get("description")),
        "price_cents": cents, "currency": i.get("currency") or "SAR", "vat_status": vat_status,
        "options_extra": extra,
        "available": (bool(branch.get("available")) and not i.get("deleted") and cents > 0
                      and cat.get("available", True) is not False and extra is not None),
        "calories": i.get("calories"),
        "updated_at": i.get("updated_at"),
    }


# ---------------------------------------------------------------- writes

def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _newer(incoming, held):
    """True when the incoming record is the same age or newer (compared as times, not strings)."""
    if not held or not incoming:
        return True
    try:
        return _ts(incoming) >= _ts(held)
    except ValueError:
        return incoming >= held


def _upsert_branch(con, b, seen_at):
    """seen_at: Nugttah's server_time, used as the status time (status_updated_at is always null)."""
    m = _branch_row(b)
    old = con.execute("SELECT pos_status, pos_status_at, src_updated_at FROM merchants WHERE merchant_id=?",
                      (m["merchant_id"],)).fetchone()
    if old and not _newer(m["updated_at"], old["src_updated_at"]):
        return None
    status_at = old["pos_status_at"] if old and old["pos_status"] == m["status"] else seen_at
    con.execute(
        "INSERT INTO merchants(merchant_id,name,name_ar,area,address,tz,hours_json,hours_verified,synthetic,pos_status,"
        "pos_status_at,country,src_updated_at) VALUES(?,?,?,?,?,?,?,1,0,?,?,?,?) ON CONFLICT(merchant_id) DO UPDATE SET "
        "name=excluded.name, name_ar=excluded.name_ar, area=excluded.area, address=excluded.address, tz=excluded.tz, "
        "hours_json=excluded.hours_json, hours_verified=1, pos_status=excluded.pos_status, "
        "pos_status_at=excluded.pos_status_at, country=excluded.country, src_updated_at=excluded.src_updated_at",
        (m["merchant_id"], m["name"], m["name_ar"], m["area"], m["address"], m["tz"], json.dumps(m["hours"]),
         m["status"], status_at, m["country"], m["updated_at"]))
    if old and old["pos_status"] != m["status"]:
        return {"merchant_id": m["merchant_id"], "event": "status", "from": old["pos_status"], "to": m["status"]}
    return None


def _upsert_item(con, it, mid, t, events, new_items):
    old = con.execute("SELECT price_cents, price_changed_at, listed, src_updated_at FROM items WHERE item_id=?",
                      (it["item_id"],)).fetchone()
    if old and not _newer(it["updated_at"], old["src_updated_at"]):
        return it["item_id"] if old["listed"] else None              # an older copy: ignore it
    if not it["available"]:
        cur = con.execute("UPDATE items SET listed=0, src_updated_at=? WHERE item_id=? AND listed=1",
                          (it["updated_at"], it["item_id"]))
        if cur.rowcount:
            events.append({"item_id": it["item_id"], "event": "unavailable"})
        return None
    name, f1 = sanitize.clean(it["name"])
    desc, f2 = sanitize.clean(it["description"])
    changed_at = old["price_changed_at"] if old else t
    if old and old["price_cents"] != it["price_cents"]:
        changed_at = t
        events.append({"item_id": it["item_id"], "event": "price", "from": old["price_cents"] / 100, "to": it["price_cents"] / 100})
    if not old:
        new_items.add(it["item_id"])
    elif not old["listed"]:
        events.append({"item_id": it["item_id"], "event": "available"})     # back on sale
    con.execute(
        "INSERT INTO items(item_id,merchant_id,name,name_ar,category,description,description_flags,price_cents,currency,"
        "price_verified,verified_on,verified_how,listed,observed_at,price_changed_at,calories,vat_status,options_extra_cents,"
        "src_updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,1,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET name=excluded.name, "
        "name_ar=excluded.name_ar, category=excluded.category, description=excluded.description, "
        "description_flags=excluded.description_flags, price_cents=excluded.price_cents, currency=excluded.currency, "
        "price_verified=1, verified_on=excluded.verified_on, verified_how=excluded.verified_how, listed=1, "
        "observed_at=excluded.observed_at, price_changed_at=excluded.price_changed_at, calories=excluded.calories, "
        "vat_status=excluded.vat_status, options_extra_cents=excluded.options_extra_cents, src_updated_at=excluded.src_updated_at",
        (it["item_id"], mid, name, it["name_ar"], it["category"], desc, json.dumps(f1 + f2), it["price_cents"],
         it["currency"], t[:10], "merchant's own ordering system (Nugttah)", t, changed_at, it["calories"],
         it["vat_status"], it["options_extra"], it["updated_at"]))
    for tag, val, conf in JURISDICTION_TAGS.get("SA", []):
        con.execute("INSERT INTO tags(item_id,tag,value,confidence,source,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(item_id,tag) DO UPDATE SET value=excluded.value, confidence=excluded.confidence, "
                    "source=excluded.source, updated_at=excluded.updated_at",
                    (it["item_id"], tag, val, conf, "jurisdiction:SA", t))
    return it["item_id"]


def _touch_source(con, mid, t, changed, edited_at=None):
    """edited_at: the newest updated_at seen for this branch, i.e. when the merchant last edited it.
    Not Nugttah's data_as_of, which is the oldest record across every merchant on the key."""
    src = con.execute("SELECT last_change_at, merchant_data_as_of FROM sources WHERE merchant_id=?", (mid,)).fetchone()
    if src and src["merchant_data_as_of"] and not _newer(edited_at, src["merchant_data_as_of"]):
        edited_at = src["merchant_data_as_of"]
    con.execute(
        "INSERT INTO sources(merchant_id,kind,expected_interval_s,last_pull_at,last_change_at,pulls,merchant_data_as_of) "
        "VALUES(?,?,?,?,?,1,?) ON CONFLICT(merchant_id) DO UPDATE SET kind=excluded.kind, "
        "expected_interval_s=excluded.expected_interval_s, last_pull_at=excluded.last_pull_at, "
        "last_change_at=excluded.last_change_at, pulls=sources.pulls+1, "
        "merchant_data_as_of=COALESCE(excluded.merchant_data_as_of, sources.merchant_data_as_of)",
        (mid, "nugttah_api", interval(), t, t if (changed or not src) else src["last_change_at"], edited_at))


def _remember_categories(st, cats):
    store = st.setdefault("categories", {})
    for c in cats or []:
        held = store.get(c["category_id"])
        if not held or _newer(c.get("updated_at"), held.get("updated_at")):
            store[c["category_id"]] = {"name": _en(c.get("name")), "available": c.get("available", True),
                                       "updated_at": c.get("updated_at")}


def _apply_menu(con, st, menu, bid, t, events, new_items):
    """A branch menu: flat items with that branch's price, available and VAT."""
    mid = PREFIX + bid
    _remember_categories(st, menu.get("categories"))
    n_before = len(events)
    seen = set()
    row = con.execute("SELECT src_updated_at FROM merchants WHERE merchant_id=?", (mid,)).fetchone()
    stamps = [row["src_updated_at"]] if row and row["src_updated_at"] else []
    for raw in menu.get("items", []):
        if raw.get("updated_at"):
            stamps.append(raw["updated_at"])
        it = _item_row(raw, mid, {**raw, "branch_id": bid}, st.get("categories", {}))
        x = _upsert_item(con, it, mid, t, events, new_items)
        if x:
            seen.add(x)
    if menu.get("is_full_snapshot", True):   # only a full menu may delist what it does not contain
        cur = con.execute(f"UPDATE items SET listed=0 WHERE merchant_id=? AND listed=1 AND item_id NOT IN "
                          f"({','.join('?' * len(seen)) or 'NULL'})", (mid, *seen))
        if cur.rowcount:
            events.append({"merchant_id": mid, "event": "delisted", "count": cur.rowcount})
    _touch_source(con, mid, t, len(events) > n_before, max(stamps, key=_ts) if stamps else None)


def _keep(b):
    ids = {x.strip() for x in os.environ.get("NUGTTAH_BRANCH_IDS", "").split(",") if x.strip()}
    return (not ids or str(b["branch_id"]) in ids) and not _excluded(b)


# ---------------------------------------------------------------- sync steps

def _refresh_branches(st, events):
    resp = _get("/branches")
    seen_at = resp.get("server_time") or _now()
    rows = resp.get("data", resp.get("branches", []))
    kept = [b for b in rows if _keep(b)]
    st["excluded_branches"] = sorted(_en(b.get("branch_name")) for b in rows if not _keep(b))
    ids = {PREFIX + str(b["branch_id"]) for b in kept}
    with db.connect() as con:
        for b in kept:
            ev = _upsert_branch(con, b, seen_at)
            if ev:
                events.append(ev)
        for r in con.execute("SELECT merchant_id FROM merchants WHERE merchant_id LIKE ?", (PREFIX + "%",)).fetchall():
            if r["merchant_id"] not in ids:                  # left the pilot: Nugttah sends no deletion records
                cur = con.execute("UPDATE items SET listed=0 WHERE merchant_id=? AND listed=1", (r["merchant_id"],))
                con.execute("UPDATE merchants SET pos_status='paused' WHERE merchant_id=?", (r["merchant_id"],))
                if cur.rowcount:
                    events.append({"merchant_id": r["merchant_id"], "event": "left_pilot", "count": cur.rowcount})
    st["branches_at"] = _now()
    return [b["branch_id"] for b in kept]


def _fetch_menus(st, events, new_items, budget):
    queue = st.get("menu_queue", [])
    done = 0
    while queue and done < budget:
        bid = str(queue[0])
        menu = _get(f"/branches/{bid}/menu")
        t = _now()
        if menu is not None:
            with db.connect() as con:
                _apply_menu(con, st, menu, bid, t, events, new_items)
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


def _apply_changes_page(st, resp, events, new_items):
    seen_at = resp.get("server_time") or _now()
    t = _now()
    _remember_categories(st, resp.get("categories"))
    touched = {}
    with db.connect() as con:
        for b in resp.get("branches") or []:
            if _keep(b):
                ev = _upsert_branch(con, b, seen_at)
                if ev:
                    events.append(ev)
        loaded = {r["merchant_id"] for r in con.execute("SELECT merchant_id FROM sources WHERE merchant_id LIKE ?", (PREFIX + "%",))}
        for raw in resp.get("items") or []:
            for br in raw.get("branches") or []:
                mid = PREFIX + str(br["branch_id"])
                if mid not in loaded:
                    continue                   # branch not loaded yet: its full menu will bring this item
                n = len(events)
                _upsert_item(con, _item_row(raw, mid, br, st.get("categories", {})), mid, t, events, new_items)
                if len(events) > n:
                    touched[mid] = max(filter(None, [touched.get(mid), raw.get("updated_at")]), key=_ts, default=None)
        for mid, edited in touched.items():
            _touch_source(con, mid, t, True, edited)


def _heartbeat():
    """A successful /changes pass means every loaded branch is current as of now."""
    with db.connect() as con:
        con.execute("UPDATE sources SET last_pull_at=?, pulls=pulls+1, expected_interval_s=? WHERE merchant_id LIKE ?",
                    (_now(), interval(), PREFIX + "%"))


def _changes_pass(st, events, new_items):
    """All pages from the saved cursor; the cursor is saved only when the whole pass succeeded."""
    cursor = st["cursor"]
    for _ in range(MAX_PAGES):
        resp = _get("/changes", {"since": cursor, "limit": 500})
        _apply_changes_page(st, resp, events, new_items)
        cursor = resp.get("next_cursor") or cursor
        if not resp.get("has_more"):
            st["cursor"], st["changes_ok_at"] = cursor, _now()
            save_state(st)
            _heartbeat()
            return True
    return False                                   # pass not finished: retry from the saved cursor next tick


def tick():
    """One step of the feed. Returns change events (price, unavailable, status, delisted, left_pilot)."""
    db.init()
    st = load_state()
    events, new_items = [], set()
    if not st.get("cursor"):
        _start_full_sync(st, events)
    try:
        _changes_pass(st, events, new_items)
    except CursorRejected:
        st.pop("cursor", None)
        _start_full_sync(st, events, kind="resync")
    if _age_s(st.get("branches_at")) > BRANCHES_EVERY_S:
        _refresh_branches(st, events)
        save_state(st)
    if not st.get("menu_queue") and _age_s(st.get("reconcile_started_at")) > RECONCILE_EVERY_S:
        with db.connect() as con:
            st["menu_queue"] = [r["merchant_id"][len(PREFIX):] for r in
                                con.execute("SELECT merchant_id FROM merchants WHERE merchant_id LIKE ?", (PREFIX + "%",))]
        st["menu_queue_kind"], st["reconcile_started_at"] = "reconcile", _now()
        save_state(st)
    _fetch_menus(st, events, new_items, int(os.environ.get("NUGTTAH_MENUS_PER_TICK", "8")))
    save_state(st)
    if new_items:
        enrich.run(use_llm=False, only_items=new_items)
    return events


def sync_all():
    """The whole first sync in the foreground (about 4 minutes for 203 branches)."""
    events = tick()
    while load_state().get("menu_queue"):
        events += tick()
    return events


def pull(branch_ids=None):
    """What the feed loop calls: one tick. A first sync spreads over several ticks, so the loop never blocks."""
    return tick()


def show():
    from .query import freshness
    with db.connect() as con:
        for m in con.execute("SELECT * FROM merchants WHERE merchant_id LIKE ? ORDER BY name", (PREFIX + "%",)):
            n = con.execute("SELECT COUNT(*) FROM items WHERE merchant_id=? AND listed=1", (m["merchant_id"],)).fetchone()[0]
            src = con.execute("SELECT * FROM sources WHERE merchant_id=?", (m["merchant_id"],)).fetchone()
            label = freshness(src)["label"] if src else "no menu yet"
            print(f"{m['merchant_id']:<28} {m['name'][:38]:<38} {m['area'][:26]:<26} items {n:>3}  "
                  f"{m['pos_status']:<7} {label}")
        tot = con.execute("SELECT COUNT(*) n, SUM(vat_status='unknown') v, SUM(options_extra_cents>0) o "
                          "FROM items WHERE merchant_id LIKE ? AND listed=1", (PREFIX + "%",)).fetchone()
        print(f"listed items {tot['n']}, VAT unknown {tot['v'] or 0}, need a paid required choice {tot['o'] or 0}")
    excl = load_state().get("excluded_branches") or []
    if excl:
        print(f"skipped by name ({len(excl)}): " + "; ".join(excl))


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
        print({k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in st.items()})
    else:
        print(__doc__)
