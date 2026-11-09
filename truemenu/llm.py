"""Minimal OpenAI-compatible chat client for the Nous Portal (same setup as the HW3/HW4 agent).

Lessons carried over from HW3/HW4: reasoning models can spend the whole budget thinking and return
no content, so we give a large max_tokens, ask for low reasoning effort, force JSON output, and tell
the model to begin with "{" immediately.
"""
import json
import os
import re
import time

import requests


def load_env_file(path):
    """Load KEY=VALUE lines into os.environ without overriding what is already set."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return False
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return True


# Every successful call is recorded here (the benchmark reads it to report time and token usage).
USAGE = []


def available():
    return bool(os.environ.get("NOUS_API_KEY"))


def chat_json(system, user, max_tokens=4000, retries=3):
    base = os.environ.get("NOUS_BASE", "https://inference-api.nousresearch.com/v1").rstrip("/")
    model = os.environ.get("NOUS_MODEL", "stepfun/step-3.7-flash")
    headers = {"Authorization": f"Bearer {os.environ['NOUS_API_KEY']}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system + "\n\nOutput rule: reply with one JSON object only. Begin your reply with { immediately."},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "reasoning": {"effort": "low"},
        "response_format": {"type": "json_object"},
    }
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=90)
            if r.status_code == 400 and ("reasoning" in body or "response_format" in body):
                # Some providers reject optional fields; retry once with a plain request.
                body.pop("reasoning", None); body.pop("response_format", None)
                r = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=90)
            if r.status_code >= 400 and r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"model API {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            data = r.json()
            u = data.get("usage") or {}
            USAGE.append({"model": model, "ms": int(r.elapsed.total_seconds() * 1000),
                          "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens")})
            choice = data["choices"][0]
            msg = choice["message"]
            # Same fallback as the HW4 agent: reasoning models sometimes leave content empty.
            content = (msg.get("content") or msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                raise ValueError(f"no JSON in model reply (finish_reason={choice.get('finish_reason')}, "
                                 f"content_chars={len(msg.get('content') or '')})")
            return json.loads(m.group(0)), model
        except Exception as e:
            last = e
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"model call failed after {retries} attempts: {last}")
