"""Demo Day booth: a visitor page and a presenter page on top of the same TrueMenu calls agents use.

  GET  /demo                       visitor page (phone): ask for lunch, see a verified dish, pay (simulated)
  GET  /demo/presenter?t=TOKEN     presenter page: raise a price, pause a restaurant's feed, reset
  POST /demo/api/lunch             find_items + pick, as an ordering agent would
  POST /demo/api/pay               verify_quote, then a simulated payment only if the quote is still valid
  GET  /demo/api/status            each restaurant's freshness, for the live data-age counters
  POST /demo/api/presenter         presenter actions (token required)

The booth never takes real orders or payments. Presenter token: TRUEMENU_PRESENTER_TOKEN in .env.
"""
import hmac
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config, db, feed, query

router = APIRouter()
STATIC = Path(__file__).resolve().parent / "static"
AREA = "Kendall Square"
TZ = ZoneInfo("America/New_York")
MAINS = {"bowl", "salad", "plate", "main", "pita", "pizza"}
BOOTH_AGENT = "booth-demo"
_hits = defaultdict(deque)
_events = deque(maxlen=30)
_lock = threading.Lock()


VISITOR_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
SHARED_IP_FACTOR = int(os.environ.get("TRUEMENU_DEMO_SHARED_IP_FACTOR", "25"))   # phones behind one Wi-Fi address


def _limit(request: Request, per_min=30):
    """Per-visitor limit, plus a much higher per-address ceiling.

    At a venue, every phone on the Wi-Fi can share one public address, so limiting by address alone would
    lock out the whole room. Each page sends a random visitor id (X-Visitor); a client without one is
    limited by its address at the per-visitor rate, as before.
    """
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    vid = request.headers.get("x-visitor") or ""
    keys = [(f"ip:{ip}", per_min * SHARED_IP_FACTOR), (f"v:{vid}", per_min)] if VISITOR_RE.match(vid) else [(f"ip:{ip}", per_min)]
    now = time.time()
    with _lock:
        for key, cap in keys:
            q = _hits[key]
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= cap:
                raise HTTPException(429, "too many requests, wait a minute")
        for key, _ in keys:
            _hits[key].append(now)


def _event(kind, text):
    _status_cache["body"] = None              # the next status read shows this event
    _events.appendleft({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "text": text})


def _meal_time():
    """Now, unless no restaurant is open now: then today's 12:30 (or tomorrow's, after lunch hours)."""
    pinned = os.environ.get("TRUEMENU_DEMO_AT")
    if pinned:
        at = datetime.fromisoformat(pinned)
        return (at if at.tzinfo else at.replace(tzinfo=TZ)), "pinned"
    now = datetime.now(TZ)
    with db.connect() as con:
        ms = [m for m in con.execute("SELECT * FROM merchants WHERE synthetic=0 AND area LIKE ?", (f"%{AREA}%",))]
    if any(query.branch_open(m, now)["within_hours"] for m in ms):
        return now, "now"
    lunch = now.replace(hour=12, minute=30, second=0, microsecond=0)
    if lunch < now:
        lunch += timedelta(days=1)
    return lunch, "next_lunch"


class LunchAsk(BaseModel):
    exclude: List[str] = Field(default_factory=list)
    diet: List[str] = Field(default_factory=list)
    budget_usd_max: Optional[float] = Field(20, ge=5, le=100)
    skip: List[str] = Field(default_factory=list, description="item_ids already shown, for 'try another'")


class Pay(BaseModel):
    quote_id: str


class PresenterAction(BaseModel):
    token: str
    action: str                      # raise_price | pause | resume | pause_all | reset
    item_id: Optional[str] = None
    merchant_id: Optional[str] = None
    delta: float = 2.0


def _pick(results, skip):
    usable = [r for r in results if r["item_id"] not in skip]
    quoted = [r for r in usable if r["quote_id"] and r["constraints"]["satisfied"]]
    mains = [r for r in quoted if r["category"] in MAINS]
    return (mains or quoted or [None])[0], usable


@router.get("/demo", include_in_schema=False)
def visitor_page():
    return FileResponse(STATIC / "booth.html", headers={"Cache-Control": "no-store"})


@router.get("/demo/presenter", include_in_schema=False)
def presenter_page(t: str = ""):
    _check_token(t)
    return FileResponse(STATIC / "presenter.html", headers={"Cache-Control": "no-store"})


@router.post("/demo/api/lunch")
def lunch(ask: LunchAsk, request: Request):
    _limit(request)
    at, basis = _meal_time()
    params = {"near": AREA, "budget_usd_max": ask.budget_usd_max, "exclude": ask.exclude[:8], "diet": ask.diet[:5],
              "available_at": at.isoformat(timespec="minutes"), "limit": 25}
    res = query.find_items(params, BOOTH_AGENT)
    pick, usable = _pick(res["results"], set(ask.skip))
    refused = [{"name": r["name"], "merchant": r["merchant"]["name"], "reason": r["quote_refused_reason"]}
               for r in usable if not r["quote_id"]][:4]
    rules = [f"no {x}" for x in ask.exclude] + [x.replace("_", " ") for x in ask.diet]
    _event("ask", f"Visitor asked for lunch: {', '.join(rules) or 'no rules'}, under ${ask.budget_usd_max:g}"
           + (f" -> {pick['name']} ${pick['price']['amount']:.2f}" if pick else " -> nothing verifiable"))
    return {"meal_time": at.isoformat(timespec="minutes"), "meal_time_basis": basis, "pick": pick,
            "considered": res["count"] + sum(res["dropped"].values()), "matching": res["count"],
            "dropped": res["dropped"], "refused": refused,
            "alternatives": [r["item_id"] for r in usable if r["quote_id"]][:10]}


@router.post("/demo/api/pay")
def pay(body: Pay, request: Request):
    _limit(request)
    v = query.verify_quote(body.quote_id)
    with db.connect() as con:
        row = con.execute("SELECT name FROM items WHERE item_id=?", (v.get("item_id"),)).fetchone()
    dish = row["name"] if row else "unknown dish"
    ok = v.get("valid_signature") and v.get("verdict") == "valid" and v.get("current_freshness") != "stale"
    if ok:
        _event("paid", f"Paid ${v['quoted_price']:.2f} for {dish} (simulated): quote valid")
    else:
        why = {"price_changed_since_quote": f"price changed to ${v.get('current_price', 0):.2f} after the quote",
               "expired": "quote expired"}.get(v.get("verdict"), v.get("reason") or "quote not valid")
        if v.get("valid_signature") and v.get("verdict") == "valid":
            why = "restaurant data went stale"
        _event("blocked", f"Payment blocked for {dish}: {why}")
    return {"paid": bool(ok), "verification": v}


_status_cache = {"at": 0.0, "body": None}
STATUS_CACHE_S = 1.5


@router.get("/demo/api/status")
def status(request: Request):
    _limit(request, per_min=60)
    with _lock:
        if _status_cache["body"] is not None and time.time() - _status_cache["at"] < STATUS_CACHE_S:
            return _status_cache["body"]
    now = datetime.now(timezone.utc)
    ov = feed._load_overrides()
    with db.connect() as con:
        ms = {m["merchant_id"]: m for m in con.execute("SELECT * FROM merchants WHERE synthetic=0 AND area LIKE ?", (f"%{AREA}%",))}
        srcs = {s["merchant_id"]: s for s in con.execute("SELECT * FROM sources")}
        marks = ",".join("?" * len(ms)) or "''"
        items = [dict(item_id=i["item_id"], name=i["name"], merchant_id=i["merchant_id"], price=i["price_cents"] / 100)
                 for i in con.execute(f"SELECT item_id, name, merchant_id, price_cents FROM items WHERE listed=1 "
                                      f"AND merchant_id IN ({marks}) ORDER BY merchant_id, name", list(ms))]
    merchants = [{"merchant_id": mid, "name": m["name"], "freshness": query.freshness(srcs[mid], now),
                  "open_now": query.branch_open(m, now)["open"], "paused": mid in ov.get("paused_sources", [])}
                 for mid, m in ms.items() if mid in srcs]
    body = {"at": now.isoformat(timespec="seconds"), "merchants": merchants, "items": items,
            "price_overrides": ov.get("prices", {}), "events": list(_events)[:12]}
    with _lock:
        _status_cache.update(at=time.time(), body=body)
    return body


def _check_token(t):
    want = os.environ.get("TRUEMENU_PRESENTER_TOKEN", "")
    if not want or not hmac.compare_digest(t.encode(), want.encode()):
        raise HTTPException(403, "presenter token required")


@router.post("/demo/api/presenter")
def presenter(body: PresenterAction):
    _check_token(body.token)
    ov = feed._load_overrides()
    if body.action == "raise_price":
        with db.connect() as con:
            it = con.execute("SELECT * FROM items WHERE item_id=?", (body.item_id,)).fetchone()
        if not it:
            raise HTTPException(404, "unknown item")
        new = round(it["price_cents"] / 100 + body.delta, 2)
        ov.setdefault("prices", {})[body.item_id] = new
        feed._save_overrides(ov)
        feed.pull([it["merchant_id"]])          # the restaurant's system reports the new price now
        _event("price", f"Restaurant changed {it['name']} to ${new:.2f}")
    elif body.action in ("pause", "resume"):
        ps = [p for p in ov.get("paused_sources", []) if p != body.merchant_id]
        if body.action == "pause":
            ps.append(body.merchant_id)
        ov["paused_sources"] = ps
        feed._save_overrides(ov)
        _event("feed", f"{body.merchant_id} feed {'paused: data will go stale in 30 s' if body.action == 'pause' else 'resumed'}")
        if body.action == "resume":
            feed.pull([body.merchant_id])
    elif body.action == "pause_all":
        with db.connect() as con:
            ov["paused_sources"] = [m["merchant_id"] for m in con.execute(
                "SELECT merchant_id FROM merchants WHERE synthetic=0 AND area LIKE ?", (f"%{AREA}%",))]
        feed._save_overrides(ov)
        _event("feed", "All restaurant feeds paused: data will go stale in 30 s")
    elif body.action == "reset":
        feed._save_overrides({"prices": {}, "status": {}, "paused_sources": []})
        feed.pull()
        _event("reset", "Demo reset: original prices, all feeds live")
    else:
        raise HTTPException(400, "unknown action")
    return {"ok": True}
