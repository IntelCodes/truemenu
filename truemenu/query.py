"""Constraint queries over the menu store. Deterministic: no model runs at query time."""
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config, db, quotes

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Exclusion words an agent may send, mapped to tags. Unknown words fall back to a name match.
EXCLUDE_MAP = {
    "pork": "contains_pork", "bacon": "contains_pork", "ham": "contains_pork",
    "avocado": "contains_avocado",
    "gluten": "contains_gluten", "wheat": "contains_gluten",
    "shellfish": "contains_shellfish", "shrimp": "contains_shellfish",
    "nuts": "contains_nuts", "nut": "contains_nuts", "peanut": "contains_nuts",
    "salad": "is_salad",
    "carbs": "high_carb", "bread": "high_carb", "rice": "high_carb", "pasta": "high_carb",
}
# HW7 change: words an agent may send in `include` (something the user specifically wants). Known words
# map to a pattern over the item's name, category and description; unknown words are matched as text.
# Applied before ranking and the limit, so a wanted item is never cut by cheaper unrelated items.
INCLUDE_MAP = {
    "seafood": r"seafood|fish|salmon|tuna|\bcod\b|shrimp|prawn|lobster|crab|\bclams?\b|oyster|scallop|mussel|calamari|squid|octopus|poke",
    "fish": r"fish|salmon|tuna|\bcod\b|poke",
    "shellfish": r"shrimp|prawn|lobster|crab|\bclams?\b|oyster|scallop|mussel",
    "chicken": r"chicken|wings?\b",
    "salad": r"salad",
    "soup": r"soup|bisque|chowder",
}


def _included(it, include):
    """True if the item matches every `include` word. Empty list = no requirement."""
    text = " ".join(filter(None, [it["name"], it["category"], it["description"]])).lower()
    for w in include:
        w = w.lower().strip()
        if not w:
            continue
        if not re.search(INCLUDE_MAP.get(w, re.escape(w)), text):
            return False
    return True


DIET_MAP = {  # diet -> (tag, required value)
    "low_carb": ("high_carb", 0), "no_carbs": ("high_carb", 0),
    "vegetarian": ("vegetarian", 1),
    "gluten_free": ("contains_gluten", 0),
    "shellfish_free": ("contains_shellfish", 0),
    "nut_free": ("contains_nuts", 0),
}


def _parse(ts):
    return datetime.fromisoformat(ts) if ts else None


def freshness(source_row, at=None):
    at = at or datetime.now(timezone.utc)
    last = _parse(source_row["last_pull_at"])
    interval = int(source_row["expected_interval_s"] or 86400)
    age = int((at - last).total_seconds()) if last else 10 ** 9
    if age <= interval * 1.25:
        label = "fresh"
    elif age <= interval * 2:
        label = "aging"
    else:
        label = "stale"
    return {"label": label, "observed_at": source_row["last_pull_at"], "age_s": age,
            "expected_interval_s": interval, "source": source_row["kind"],
            "last_change_at": source_row["last_change_at"]}


def branch_open(m, at):
    """Open = within posted hours AND the POS says the branch is taking orders."""
    local = at.astimezone(ZoneInfo(m["tz"]))
    hours = json.loads(m["hours_json"]).get(DAYS[local.weekday()])
    within = False
    if hours:
        o, c = hours
        hm = local.strftime("%H:%M")
        within = o <= hm <= c
    taking_orders = m["pos_status"] == "online"
    return {
        "open": bool(within and taking_orders),
        "within_hours": within, "pos_status": m["pos_status"],
        "hours_today": hours, "hours_verified": bool(m["hours_verified"]),
        "basis": "posted hours + POS status; no item-level stock signal",
    }


def _tags(con, item_id):
    return {r["tag"]: {"value": bool(r["value"]), "confidence": round(r["confidence"], 2), "source": r["source"]}
            for r in con.execute("SELECT tag,value,confidence,source FROM tags WHERE item_id=?", (item_id,))}


def _check_constraints(name, tags, exclude, diet):
    """Return (violations, uncertain). A tag counts only at or above the serving threshold."""
    thr = config.SERVING_THRESHOLD
    violations, uncertain = [], []
    for term in exclude:
        term_l = term.lower().strip()
        tag = EXCLUDE_MAP.get(term_l)
        if not tag:
            if term_l in name.lower():
                violations.append(f"exclude:{term_l} (name)")
            continue
        t = tags.get(tag)
        if t and t["confidence"] >= thr:
            if t["value"]:
                violations.append(f"exclude:{term_l} ({tag}, {t['source']}, {t['confidence']})")
        else:
            uncertain.append(f"exclude:{term_l} ({tag} unknown or below threshold)")
    for d in diet:
        spec = DIET_MAP.get(d.lower().strip())
        if not spec:
            uncertain.append(f"diet:{d} (unsupported)")
            continue
        tag, need = spec
        t = tags.get(tag)
        if t and t["confidence"] >= thr:
            if int(t["value"]) != need:
                violations.append(f"diet:{d} ({tag}={int(t['value'])}, {t['source']}, {t['confidence']})")
        else:
            uncertain.append(f"diet:{d} ({tag} unknown or below threshold)")
    return violations, uncertain


def _serialize(con, it, m, src, at, agent, issue_quote):
    tags = _tags(con, it["item_id"])
    fr = freshness(src)            # freshness is always about now, not the requested meal time
    avail = branch_open(m, at)
    quote_id, refusal = None, None
    if issue_quote:
        if not it["price_verified"]:
            refusal = "price not verified against the merchant's own source"
        elif fr["label"] == "stale":
            refusal = f"source stale (last sync {fr['age_s']}s ago, expected every {fr['expected_interval_s']}s)"
        elif not avail["open"]:
            refusal = "branch not open or not taking orders"
        else:
            quote_id, payload = quotes.sign(it["item_id"], it["merchant_id"], it["price_cents"], it["currency"],
                                            fr["observed_at"], agent)
            con.execute("INSERT OR IGNORE INTO quotes(quote_id,item_id,price_cents,observed_at,issued_at,agent) VALUES(?,?,?,?,?,?)",
                        (quote_id, it["item_id"], it["price_cents"], fr["observed_at"],
                         datetime.now(timezone.utc).isoformat(timespec="seconds"), agent))
    return {
        "item_id": it["item_id"], "name": it["name"], "category": it["category"],
        "description": it["description"] or None,
        "flags": json.loads(it["description_flags"] or "[]"),
        "merchant": {"id": m["merchant_id"], "name": m["name"], "area": m["area"], "address": m["address"], "synthetic": bool(m["synthetic"])},
        "price": {"amount": it["price_cents"] / 100, "currency": it["currency"], "source": "merchant_stated",
                  "verified": bool(it["price_verified"]), "verified_on": it["verified_on"] or None,
                  "changed_at": it["price_changed_at"]},
        "availability": avail,
        "tags": tags,
        "freshness": fr,
        "quote_id": quote_id,
        "quote_refused_reason": refusal,
    }


def find_items(params, agent, at=None):
    at = at or datetime.now(timezone.utc)
    if params.get("available_at") not in (None, "", "now"):
        at = datetime.fromisoformat(params["available_at"])
        if at.tzinfo is None:
            at = at.replace(tzinfo=ZoneInfo("America/New_York"))
    area = (params.get("near") or "").lower()
    budget = params.get("budget_usd_max")
    exclude = params.get("exclude") or []
    diet = params.get("diet") or []
    include = params.get("include") or []
    limit = max(1, min(int(params.get("limit") or 8), 25))
    include_synth = bool(params.get("include_synthetic"))
    results, dropped = [], {"over_budget": 0, "closed": 0, "violates_constraints": 0, "not_included": 0}
    with db.connect() as con:
        merchants = {r["merchant_id"]: r for r in con.execute("SELECT * FROM merchants")}
        sources = {r["merchant_id"]: r for r in con.execute("SELECT * FROM sources")}
        for it in con.execute("SELECT * FROM items WHERE listed=1"):
            m = merchants.get(it["merchant_id"])
            if not m or (m["synthetic"] and not include_synth):
                continue
            if area and area not in (m["area"] or "").lower():
                continue
            if include and not _included(it, include):
                dropped["not_included"] += 1
                continue
            if budget is not None and it["price_cents"] > float(budget) * 100:
                dropped["over_budget"] += 1
                continue
            avail = branch_open(m, at)
            if not avail["open"]:
                dropped["closed"] += 1
                continue
            tags = _tags(con, it["item_id"])
            viol, unc = _check_constraints(it["name"], tags, exclude, diet)
            if viol:
                dropped["violates_constraints"] += 1
                continue
            row = _serialize(con, it, m, sources[it["merchant_id"]], at, agent, issue_quote=True)
            row["constraints"] = {"satisfied": not unc, "uncertain": unc}
            results.append(row)
        # Rank: certain + quotable first, then cheaper.
        results.sort(key=lambda r: (not r["constraints"]["satisfied"], r["quote_id"] is None, r["price"]["amount"]))
        con.execute("INSERT INTO calls(at,agent,endpoint,request,result_count) VALUES(?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), agent, "find_items", json.dumps(params), len(results)))
    return {"results": results[:limit], "count": len(results), "dropped": dropped,
            "evaluated_at": at.isoformat(timespec="seconds"), "serving_threshold": config.SERVING_THRESHOLD}


def get_menu(merchant_id, agent):
    at = datetime.now(timezone.utc)
    with db.connect() as con:
        m = con.execute("SELECT * FROM merchants WHERE merchant_id=?", (merchant_id,)).fetchone()
        if not m:
            return None
        src = con.execute("SELECT * FROM sources WHERE merchant_id=?", (merchant_id,)).fetchone()
        items = [_serialize(con, it, m, src, at, agent, issue_quote=False)
                 for it in con.execute("SELECT * FROM items WHERE merchant_id=? AND listed=1 ORDER BY category, name", (merchant_id,))]
    return {"merchant": dict(id=m["merchant_id"], name=m["name"], address=m["address"]),
            "availability": branch_open(m, at), "freshness": freshness(src), "items": items}


def verify_quote(quote_id):
    ok, payload, reason = quotes.verify(quote_id)
    if not ok:
        return {"valid_signature": False, "reason": reason}
    now = datetime.now(timezone.utc)
    out = {"valid_signature": True, "expired": now.timestamp() > payload["exp"],
           "item_id": payload["item_id"], "merchant_id": payload["merchant_id"],
           "quoted_price": payload["price_cents"] / 100, "currency": payload["currency"],
           "price_observed_at": payload["observed_at"],
           "issued_at": datetime.fromtimestamp(payload["iat"], timezone.utc).isoformat(timespec="seconds"),
           "issued_to": payload.get("aud")}
    with db.connect() as con:
        it = con.execute("SELECT * FROM items WHERE item_id=?", (payload["item_id"],)).fetchone()
        src = con.execute("SELECT * FROM sources WHERE merchant_id=?", (payload["merchant_id"],)).fetchone()
        logged = con.execute("SELECT 1 FROM quotes WHERE quote_id=?", (quote_id,)).fetchone() is not None
    out["issued_by_this_service"] = logged
    if it:
        out["current_price"] = it["price_cents"] / 100
        out["price_unchanged"] = it["price_cents"] == payload["price_cents"]
        out["current_freshness"] = freshness(src, now)["label"] if src else None
    out["verdict"] = ("valid" if not out["expired"] and out.get("price_unchanged") else
                      "expired" if out["expired"] else "price_changed_since_quote")
    return out


def record_feedback(item_id, tag, observed_value, note, agent):
    """Agent reports a tag is wrong. Inferred tags are replaced; merchant-stated tags are not touched."""
    t = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.connect() as con:
        cur = con.execute("SELECT * FROM tags WHERE item_id=? AND tag=?", (item_id, tag)).fetchone()
        con.execute("INSERT INTO feedback(item_id,tag,observed_value,note,agent,at) VALUES(?,?,?,?,?,?)",
                    (item_id, tag, int(bool(observed_value)), note, agent, t))
        if cur and cur["source"] == "merchant_stated":
            return {"applied": False, "reason": "tag is merchant-stated; feedback logged for the merchant to review"}
        if cur:
            con.execute("INSERT INTO tag_history VALUES(?,?,?,?,?,?)",
                        (item_id, tag, cur["value"], cur["confidence"], cur["source"], t))
        con.execute("INSERT INTO tags(item_id,tag,value,confidence,source,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(item_id,tag) DO UPDATE SET value=excluded.value, confidence=excluded.confidence, "
                    "source=excluded.source, updated_at=excluded.updated_at",
                    (item_id, tag, int(bool(observed_value)), 0.75, "agent_feedback", t))
    return {"applied": True, "item_id": item_id, "tag": tag, "new_value": bool(observed_value),
            "previous": dict(cur) if cur else None}
