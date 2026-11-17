# Cold-start evidence (HW9, 2026-11-16)

Plan, checks and results: [docs/COLD_START_TEST.md](../../docs/COLD_START_TEST.md).
All runs are self-run with the generic harness in `coldstart/`; model `stepfun/step-3.7-flash`; no manual help.

| File | Task | Result (rescore with `python3 coldstart/score.py <file>`) |
|---|---|---|
| `coldstart_20261116_153318.jsonl` | Main | 5/5 checks |
| `coldstart_main_20261116_160310.jsonl` | Main | 5/5 |
| `coldstart_main_20261116_160336.jsonl` | Main | 5/5 |
| `coldstart_boundary_20261116_160409.jsonl` | Harvard Square | Fail: searched Kendall Square, decided to pay |
| `coldstart_boundary_after_20261116_162613.jsonl` | Harvard Square, after fix | B1, B3, B4 pass; B2 (explicit don't-pay) fail |
| `coldstart_boundary_after_20261116_162626.jsonl` | Harvard Square, after fix | B1, B3, B4 pass; B2 fail |
| `coldstart_main_after_20261116_162644.jsonl` | Main, after fix | 5/5 |
| `server_request_log_atl-marlon.jsonl` | TrueMenu's own log for the key given to another team (independent run, 2026-11-17) | 1 `find_items` line at 16:20:35 UTC, matching their report: same request, 6 results. The only call ever made with that key. Their report is in the HW9 submission. |
| `server_request_log_coldstart-fresh.jsonl` | TrueMenu's own log for the test key | 8 `find_items` lines: one per run plus one manual `curl` check of the deployed change (16:26:12). `verify_quote` is public and logged without a key. |

Each transcript line is one event: `start` (task, docs URL, base URL), `model` (the model's output), `http`
(request and full response) or `final` (the answer). The key is replaced by `<KEY>`. The three `after` runs
read the docs pinned to commit `c026efc` to avoid a stale cached copy; the content is the same as `main` at that time.
