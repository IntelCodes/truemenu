# TrueMenu

MIT AI Studio, Fall 2026. Shahaleel Al Yousef.

Verified merchant data for ordering agents. A lunch agent discovers TrueMenu through its agent card,
asks for items that fit its user's constraints, gets merchant-stated prices with freshness, inferred
dietary tags with confidence, and a signed quote, then chooses one. A separate checker verifies the
quote and compares the result with a hand-verified ground-truth table.

```
lunch agent ──GET /.well-known/agent-card.json──▶ TrueMenu API ◀── simulated POS feed (per-source sync, diff)
            ──POST /v1/find_items (X-Agent-Key)─▶  (FastAPI)   ◀── enrichment at ingest (rules + LLM, cautious merge)
checker     ──POST /v1/verify_quote (public)───▶   SQLite      ◀── data/ground_truth.csv, data/merchants.json
```

## 1. Install (about 10 minutes)

On a Linux machine with Python 3.11 or newer:

```bash
git clone https://github.com/IntelCodes/truemenu ~/truemenu && cd ~/truemenu
./setup.sh
./tests/smoke.sh          # takes ~40 s; expect ALL SMOKE TESTS PASSED
./ask "Find me lunch near Kendall Square today"
./check
```

`setup.sh` creates a virtualenv, writes `.env` (mode 600) with a fresh quote secret and agent key, and reads
the model key from `~/hw4/.env` (not copied). Nothing listens on a public port: the API binds to 127.0.0.1:8088.


## 2. Where the data comes from

`data/ground_truth.csv` holds 27 real menu items from three Kendall Square restaurants, each name, description
and price taken from the restaurant's **own** page on 2026-10-25 (never from Uber Eats, DoorDash or price blogs):

| Restaurant | Source | Items |
|---|---|---|
| CAVA, 82 Ames St | cava.com menu with the Kendall Square pickup store selected | 9 |
| Area Four, 500 Technology Sq | areafour.com Technology Square menu page | 9 |
| Row 34, 314 Main St | row34.com Cambridge lunch menu PDF dated 2026-10-23 | 9 |

Three prices also match checks made by hand in September (CAVA Greek Salad $12.25, Area Four wings $10,
Row 34 fried shrimp $18). Opening hours in `data/merchants.json` come from the same sites.
Left out on purpose: Naya (its menu page lists no prices and its ordering site would not load) and two Row 34
items whose prices sit between rows in the PDF. Nothing is guessed.

If you want to re-check or add items, edit the CSV (`nano data/ground_truth.csv`); changes are picked up on
the next feed sync, no restart needed. A row with `verified=no` is shown but never quoted.

## 3. Commands

| Command | What it does |
|---|---|
| `./ask "Find me lunch near Kendall Square today"` | agent: discover, plan (model), call, decide (model), answer |
| `./ask --at 2026-10-26T12:30 "lunch on Monday"` | same, for a meal time other than now (the model also understands "on Monday at 12:30") |
| `./check` | independent check of the last answer (quote, ground truth, constraints) |
| `./ask feedback a4-wings high_carb true "breaded here"` | agent reports a wrong inferred tag; next query respects it |
| `./feed status` | freshness of each source |
| `./feed set-price ITEM 13.50` then `./feed once` | simulate the merchant changing a price |
| `./feed pause-source cava-kendall` / `resume-source` | stop one source syncing so it goes stale |
| `./feed clear` | remove all simulated changes |
| `scripts/reset.sh` | wipe demo state (feedback, quotes) and restart |
| `scripts/stop.sh` | stop the API and feed |

## 4. Benchmark (MIT AI Studio HW7)

`bench/` holds the benchmark from the HW7 report: 18 cases (12 tune, 6 held out), hand labels independent of
TrueMenu's own tags, the harness that runs TrueMenu against two baselines with the same model, and the logged runs
in `bench/results/`. `python bench/report_data.py` recomputes every reported number from those logs.

## 5. Nanda Town integration (MIT AI Studio HW8)

TrueMenu can play the seller in a [Nanda Town](https://github.com/projnanda/nandatown) Track run. The adapters, the
compatibility check and its instructions live in Town under `examples/truemenu/`; TrueMenu itself runs here,
unchanged, and Town reaches it over HTTP at `TRUEMENU_URL`.

## 6. Code map

- `truemenu/server.py` API and agent card
- `truemenu/query.py` constraint filtering, freshness, open-now, quotes, feedback (no model at query time)
- `truemenu/feed.py` simulated POS sync with per-source intervals and change detection
- `truemenu/enrich.py` tag inference: keyword rules + LLM, merged cautiously (a missed allergen is worse than a false alarm)
- `truemenu/sanitize.py` strips instruction-like text from merchant strings
- `truemenu/quotes.py` HMAC-signed quotes
- `agent/lunch_agent.py` the client agent; `agent/check.py` the independent checker
- `tests/smoke.sh` end-to-end scenarios without the model

Never commit or submit `.env`, `state/`, `logs/` or `runs/`.
