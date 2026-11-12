"""TrueMenu HTTP API.

Run:  uvicorn truemenu.server:app --host 127.0.0.1 --port 8088
Discovery: GET /.well-known/agent-card.json
"""
import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config, db, query

app = FastAPI(title="TrueMenu", version=config.VERSION)
db.init()

# Request log: one JSON line per API call (agent name, never the key), so a client's use of the
# service can be reviewed afterwards. Bodies are capped; responses are summarised, not stored whole.
REQUEST_LOG = Path(os.environ.get("TRUEMENU_REQUEST_LOG", config.ROOT / "logs" / "requests.jsonl"))
RATE_LIMIT_PER_MIN = int(os.environ.get("TRUEMENU_RATE_LIMIT_PER_MIN", "60"))
_hits = defaultdict(deque)
_log_lock = threading.Lock()


def _summary(path, data):
    if not isinstance(data, dict):
        return None
    if "detail" in data:
        return {"detail": data["detail"]}
    if path.endswith("/find_items"):
        return {"count": data.get("count"), "dropped": data.get("dropped"),
                "items": [{"item_id": r.get("item_id"), "price": (r.get("price") or {}).get("amount"),
                           "freshness": (r.get("freshness") or {}).get("label"),
                           "quoted": bool(r.get("quote_id")),
                           "refused": r.get("quote_refused_reason")} for r in data.get("results", [])]}
    if path.endswith("/verify_quote"):
        return {k: data.get(k) for k in ("verdict", "valid_signature", "item_id", "quoted_price",
                                          "current_price", "reason") if k in data}
    return {k: data[k] for k in list(data)[:6] if not isinstance(data[k], (list, dict))}


@app.middleware("http")
async def log_and_limit(request: Request, call_next):
    key = request.headers.get("x-agent-key") or ""
    info = config.agent_keys().get(key)
    who = info["agent"] if info else ("anonymous" if not key else "unknown-key")
    client = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    bucket = _hits[who if info else f"ip:{client}"]
    now = time.time()
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    body = await request.body()
    if len(bucket) >= RATE_LIMIT_PER_MIN:
        response, data = JSONResponse({"detail": "rate limit exceeded"}, status_code=429), None
    else:
        bucket.append(now)
        response = await call_next(request)
        data = None
        if response.headers.get("content-type", "").startswith("application/json"):
            raw = b"".join([chunk async for chunk in response.body_iterator])
            try:
                data = json.loads(raw or b"null")
            except ValueError:
                data = None
            response = JSONResponse(content=data, status_code=response.status_code,
                                    headers={k: v for k, v in response.headers.items()
                                             if k.lower() not in ("content-length", "content-type")})
    if request.url.path.startswith(("/v1/", "/.well-known/")):
        try:
            req_json = json.loads(body) if body else None
        except ValueError:
            req_json = body[:500].decode("utf-8", "replace")
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "agent": who,
                "method": request.method, "path": request.url.path, "status": response.status_code,
                "request": req_json, "response": _summary(request.url.path, data),
                "user_agent": request.headers.get("user-agent", "")[:120]}
        with _log_lock:
            REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(REQUEST_LOG, "a") as f:
                f.write(json.dumps(line) + "\n")
    return response


def agent_for(scope):
    def dep(x_agent_key: Optional[str] = Header(default=None)):
        keys = config.agent_keys()
        info = keys.get(x_agent_key or "")
        if not info:
            raise HTTPException(401, "missing or unknown X-Agent-Key")
        if scope not in info["scopes"]:
            raise HTTPException(403, f"key lacks scope '{scope}'")
        return info["agent"]
    return dep


class FindItems(BaseModel):
    near: Optional[str] = Field(None, description="Area name, e.g. 'Kendall Square'")
    budget_usd_max: Optional[float] = Field(None, ge=0)
    exclude: List[str] = Field(default_factory=list, description="Words like pork, avocado, gluten, shellfish, nuts, salad, carbs")
    diet: List[str] = Field(default_factory=list, description="low_carb | vegetarian | gluten_free | shellfish_free | nut_free")
    include: List[str] = Field(default_factory=list, description="Things the user specifically wants the item to be or contain, e.g. seafood, fish, chicken, soup. Every word must match.")
    available_at: Optional[str] = Field("now", description="'now' or ISO time; local time assumed if no offset")
    limit: Optional[int] = Field(8, ge=1, le=25)
    include_synthetic: bool = False


class VerifyQuote(BaseModel):
    quote_id: str


class Feedback(BaseModel):
    item_id: str
    tag: str
    observed_value: bool
    note: Optional[str] = None


@app.get("/.well-known/agent-card.json")
def agent_card():
    return {
        "name": "TrueMenu",
        "version": config.VERSION,
        "description": ("Verified merchant data for ordering agents: menu items, prices and open-now status with "
                        "provenance and freshness on every field, and a signed quote any third party can verify."),
        "url": config.SERVICE_URL,
        "auth": {"type": "header", "header": "X-Agent-Key", "scopes": ["read", "feedback"],
                 "note": "verify_quote is public so that any party can check a quote"},
        "tools": [
            {"name": "find_items", "method": "POST", "path": "/v1/find_items",
             "input": FindItems.model_json_schema(),
             "returns": "ranked items; each has price{amount,currency,source,verified}, availability, tags{value,confidence,source}, "
                        "freshness{label,observed_at,age_s}, constraints{satisfied,uncertain}, quote_id or quote_refused_reason"},
            {"name": "get_menu", "method": "GET", "path": "/v1/menu/{merchant_id}"},
            {"name": "verify_quote", "method": "POST", "path": "/v1/verify_quote", "input": VerifyQuote.model_json_schema(),
             "returns": "valid_signature, expired, quoted_price, current_price, price_unchanged, verdict"},
            {"name": "record_feedback", "method": "POST", "path": "/v1/feedback", "input": Feedback.model_json_schema()},
        ],
        "guarantees": [
            "Filtering is deterministic; no model runs at query time.",
            f"Tags are used for filtering only at confidence >= {config.SERVING_THRESHOLD}; below that the constraint is reported as uncertain.",
            "Inferred tags never overwrite merchant-stated tags.",
            "Quotes are issued only for verified prices from a fresh source at an open branch.",
            "Merchant text is sanitized; instruction-like text is removed and flagged.",
        ],
        "limitations": [
            "Availability means the branch is open and the item is listed; there is no item-level stock signal.",
            "Demo data: a hand-verified snapshot of real Kendall Square menus fed through a simulated POS sync.",
        ],
    }


@app.get("/health")
def health():
    return {"ok": True, "version": config.VERSION}


@app.post("/v1/find_items")
def find_items(body: FindItems, agent: str = Depends(agent_for("read"))):
    return query.find_items(body.model_dump(), agent)


@app.get("/v1/menu/{merchant_id}")
def get_menu(merchant_id: str, agent: str = Depends(agent_for("read"))):
    out = query.get_menu(merchant_id, agent)
    if out is None:
        raise HTTPException(404, "unknown merchant")
    return out


@app.post("/v1/verify_quote")
def verify_quote(body: VerifyQuote):
    return query.verify_quote(body.quote_id)


@app.post("/v1/feedback")
def feedback(body: Feedback, agent: str = Depends(agent_for("feedback"))):
    return query.record_feedback(body.item_id, body.tag, body.observed_value, body.note, agent)
