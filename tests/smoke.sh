#!/usr/bin/env bash
# End-to-end smoke test without the model. Prints PASS/FAIL per scenario.
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export AGENT_MODEL_ENV=/nonexistent NOUS_API_KEY=
PY=${PY:-./venv/bin/python}
ok(){ echo "PASS  $1"; }; bad(){ echo "FAIL  $1"; FAILED=1; }
AT=$(date -d "next monday 12:30" +%Y-%m-%dT12:30 2>/dev/null || date -v+mon +%Y-%m-%dT12:30)
scripts/reset.sh >/dev/null
$PY -m truemenu.feed clear >/dev/null
$PY agent/lunch_agent.py --offline --at "$AT" "lunch" > /tmp/tm_a.txt
grep -q "Lunch:" /tmp/tm_a.txt && ok "agent recommends a verified item" || bad "agent recommends a verified item"
$PY agent/check.py > /tmp/tm_c.txt && ok "independent check passes" || bad "independent check passes"
ITEM=$($PY -c "import json;print(json.load(open('runs/last_run.json'))['pick']['item_id'])")
$PY -m truemenu.feed set-price "$ITEM" 99.99 >/dev/null; $PY -m truemenu.feed once >/dev/null
$PY agent/check.py > /tmp/tm_c2.txt; grep -q "FAIL  price unchanged since quote" /tmp/tm_c2.txt && ok "price change after quote is caught" || bad "price change after quote is caught"
$PY -m truemenu.feed clear >/dev/null; $PY -m truemenu.feed once >/dev/null
OUT=$(curl -s -X POST "$TRUEMENU_URL/v1/find_items" -H "X-Agent-Key: $TRUEMENU_LUNCH_AGENT_KEY" -H 'content-type: application/json' -d "{\"include_synthetic\":true,\"budget_usd_max\":9,\"available_at\":\"$AT\"}")
echo "$OUT" | grep -q "instruction-like text removed" && ! echo "$OUT" | grep -qi "ignore previous" && ok "injected menu text is stripped and flagged" || bad "injected menu text is stripped and flagged"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$TRUEMENU_URL/v1/find_items" -H 'content-type: application/json' -d '{}')
[ "$CODE" = "401" ] && ok "request without agent key refused" || bad "request without agent key refused ($CODE)"
for m in cava-kendall area-four-kendall row34-kendall; do $PY -m truemenu.feed pause-source $m >/dev/null; done
sleep $(( ${TRUEMENU_SYNC_INTERVAL_S:-15} * 2 + 4 ))
$PY agent/lunch_agent.py --offline --at "$AT" "lunch" > /tmp/tm_s.txt
grep -q "No option I can stand behind" /tmp/tm_s.txt && grep -q "source stale" /tmp/tm_s.txt && ok "stale sources make the agent decline" || bad "stale sources make the agent decline"
$PY -m truemenu.feed clear >/dev/null
[ -z "${FAILED:-}" ] && echo "ALL SMOKE TESTS PASSED" || echo "SOME SMOKE TESTS FAILED"
