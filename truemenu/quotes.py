"""Signed price quotes.

A quote_id is   tq1.<base64url payload>.<hex HMAC-SHA256>
The payload binds item, merchant, price, currency, the time the price was observed upstream, and
an expiry. Anyone holding the service's verify endpoint can check it; nobody can forge one without
the secret. Verification is stateless, but issued quotes are also logged for audit.
"""
import base64
import hashlib
import hmac
import json
import time
from . import config


def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret():
    if not config.QUOTE_SECRET:
        raise RuntimeError("TRUEMENU_QUOTE_SECRET is not set; run setup.sh")
    return config.QUOTE_SECRET.encode()


def sign(item_id, merchant_id, price_cents, currency, observed_at, agent):
    now = int(time.time())
    payload = {
        "v": 1, "item_id": item_id, "merchant_id": merchant_id, "price_cents": price_cents,
        "currency": currency, "observed_at": observed_at, "iat": now, "exp": now + config.QUOTE_TTL_S,
        "aud": agent,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:40]
    return f"tq1.{body}.{mac}", payload


def verify(quote_id):
    """Return (ok_signature, payload_or_None, reason)."""
    try:
        prefix, body, mac = quote_id.split(".")
        if prefix != "tq1":
            return False, None, "unknown quote format"
        good = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:40]
        if not hmac.compare_digest(good, mac):
            return False, None, "signature does not match"
        return True, json.loads(_b64d(body)), "ok"
    except Exception as e:  # malformed input
        return False, None, f"malformed quote: {type(e).__name__}"
