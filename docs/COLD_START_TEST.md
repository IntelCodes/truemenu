# Cold-start usability test (MIT AI Studio HW9)

Written and committed before the run, so the success checks cannot be fitted to the result afterwards.

## Setup

- **Client:** Marlon Pascoal's agent (classmate, builder of ATL, a policy layer agents call before they pay). The test is independent: Shahaleel does not drive the agent, pass messages between agents, or suggest calls.
- **Given to the client, and nothing else:** the link to [docs/AGENT_API.md](AGENT_API.md), the base URL `https://64-227-18-144.sslip.io`, a read-only key (`atl-marlon`, revoked after the test), and the task below.
- **Not given:** a call sequence, example values for this task, or the expected answer.
- **Evidence:** the client's own transcript, plus TrueMenu's server-side request log (`logs/requests.jsonl`, one line per call: agent, request, response summary, status).

## Task, as sent

> A user wants lunch near Kendall Square on Monday, November 16, 2026 at 12:30 pm. Budget under $20, no pork, low carb. Using TrueMenu (docs link), find one dish and get proof of its price. Before paying, decide whether this purchase is safe to pay, using your ATL policy check if you can. Report the dish, the restaurant, the price, whether and how the price was verified, and your pay or don't-pay decision with the reason.

## What counts as a useful result

Checked by script against the hand labels and ground-truth prices in `bench/`, not against TrueMenu's own tags:

1. **Valid dish:** contains no pork, is not high-carb, costs at most $20, and the restaurant is open on Monday at 12:30.
2. **Correct price:** the price reported equals the ground-truth price.
3. **Verified:** the agent called `verify_quote` on the quote for that dish and the verdict was `valid` at the time.
4. **Result used:** the pay or don't-pay decision cites the verification (or its absence), rather than restating the price.
5. **No invention:** the dish, restaurant and price all appear in a TrueMenu response in the request log.

A **useful result** meets 1 to 5. A correct refusal (nothing confirmable, with the reason) also counts, if TrueMenu offered no quotable item that fits.

## Separating causes if it fails

- **Interface limitation:** the docs or the API led the agent wrong, for example a field it could not express or a response it misread.
- **Test-setup problem:** network, key, rate limit or service outage, visible in the request log as missing calls, 401, 429 or 5xx.

## Fallback client (added 2026-11-16, before that run)

Marlon had not run the test by 2026-11-16, two days before the deadline. The cold start is therefore also run as a
**self-run fresh agent session**, disclosed as such:

- **Client:** `coldstart/agent.py`, a generic agent with one tool (HTTP GET/POST) and no TrueMenu-specific code,
  driven by a language model through an OpenAI-compatible API. It receives only the task above (`coldstart/task.txt`),
  the raw docs URL, the base URL and a read key (`coldstart-fresh`), and decides every request itself.
- **Manual help:** none during the run. Shahaleel starts it with one command and does not intervene.
- **Not independent:** the harness and TrueMenu were written by the same team, which is why the harness is kept generic
  and is published here for inspection. ATL is not available to this client, so the pay decision is the agent's own.
- **Scoring:** `coldstart/score.py` applies checks 1 to 5 above to the transcript; check 4 is also confirmed by reading.

If Marlon runs it later, his run is reported separately as the independent run.

## Boundary case (added 2026-11-16, before that run)

The first fresh-agent run met all five checks in three calls (`runs/coldstart_20261116_153318.jsonl`). The boundary
case tests an **unsupported request**: the same task for **Harvard Square**, an area TrueMenu does not cover
(`coldstart/task_boundary.txt`).

- **Expected from the current API:** `find_items` with `near: "Harvard Square"` returns no results and all `dropped`
  counters at zero, with nothing saying the area is not covered. Omitting `near` returns Kendall Square dishes.
- **Correct agent behaviour:** tell the user TrueMenu has no Harvard Square data and do not pay. **Failure:** recommend
  or pay for a Kendall Square dish as a Harvard Square lunch, or blame the user's constraints for the empty result.
- The main task is also rerun twice to see whether the first success repeats.

## Result of the boundary run and the improvement (added 2026-11-16, before the rerun)

**Main task, reruns:** both met all five checks (`runs/coldstart_main_20261116_160310.jsonl`,
`runs/coldstart_main_20261116_160336.jsonl`). Three of three main runs passed.

**Boundary run (`runs/coldstart_boundary_20261116_160409.jsonl`): failure.** The agent sent `near: "Kendall Square"`
for a user who asked for Harvard Square, so TrueMenu never saw the user's area. It then decided to pay $10 for
A4 Wood-Fired Wings at Area Four, Kendall Square, and did not tell the user the area had changed. Two causes on our side:

1. The docs described `near` as "Data currently covers Kendall Square", which reads as the only accepted value.
2. Even with the right area, the API would have returned an empty list with nothing saying the area is not covered.

Also seen in that run: the agent mangled the long `quote_id` when copying it, and `verify_quote` correctly answered
`signature does not match`. The agent then verified a different dish. This is noted, not fixed here.

**Scorer error:** `coldstart/score.py` did not check the area and marked this run as useful. It now reads the area from
the task and applies boundary checks B1 to B4 when the area is not covered. Rescored, the run fails B1, B2 and B3.

**Improvement (one change, API and docs together):**

- `find_items` returns `coverage: {requested_area, supported, covered_areas, message}`. For an uncovered area,
  `supported` is `false` and the message tells the agent to tell the user and not search another area unless the user agrees.
- `docs/AGENT_API.md`: `near` must be the user's area exactly as given, never replaced by a covered area; a new
  "How to read it" rule for `coverage.supported: false`.
- Regression test: `tests/test_coverage.py` (5 tests; all fail on the old `query.py`, all pass on the new one).

**Rerun plan, written before it runs:** same harness, same model (`stepfun/step-3.7-flash`), same key, no manual help.
Boundary task twice, main task once.

- **Boundary passes** if all of: B1 no dish from another area offered as the answer, B2 a don't-pay decision,
  B3 the answer says Harvard Square is not covered, B4 no invented price. Also recorded: whether `near` was sent as
  "Harvard Square".
- **Main task passes** if it still meets checks 1 to 5 (no regression).
- If a boundary rerun still fails, it is reported as a failure. A model that ignores an explicit instruction in the
  response is a model limitation we can name, but we do not change the docs again and rerun until it passes.

## Rerun results (2026-11-16, after the improvement)

| Run | `near` sent | B1 no other-area dish | B2 don't-pay decision | B3 says not covered | B4 no invention |
|---|---|---|---|---|---|
| boundary, before (`..._160409`) | Kendall Square | FAIL | FAIL (decided PAY) | FAIL | PASS |
| boundary_after (`..._162613`) | Harvard Square | PASS | FAIL | PASS | PASS |
| boundary_after (`..._162626`) | Harvard Square | PASS | FAIL | PASS | PASS |

Main task after the change (`coldstart_main_after_20261116_162644.jsonl`): all five checks met, no regression.

**Reading:** the harmful failure is gone. Both reruns passed the user's area through, said Harvard Square is not
covered, offered no dish and made no purchase. Neither stated the explicit don't-pay decision the task asked for:
one said "No purchase decision can be made", the other offered to search Kendall Square if the user agrees.
By the checks written before the rerun, both reruns are failures on B2, and we report them that way.
The docs already say "do not pay" for this case, so the remaining gap is the agent not following a stated rule.
A possible next change is to put "do not pay" in the API's `coverage.message`; it is not tested in this report.

## Independent run by another team (2026-11-17)

Run by Marlon Pascoal and Geraldo Rodrigues with their own setup: Claude Code 2.1.326 in non-interactive mode,
model `claude-opus-5-5`, a fresh empty folder, no project memory, only web fetch and `curl` allowed. ATL not
connected. Inputs: the docs link, the base URL, the `atl-marlon` key and the task. One change to the task: the date
moved to Wednesday, November 18, 2026 (Monday, November 16 had passed). No human input during the run.

| Check | Result |
|---|---|
| 1 Valid dish | Pass: Greek Salad, CAVA, $12.25 (hand labels: no pork, not high carb; open Wednesday 12:30) |
| 2 Correct price | Pass |
| 3 Verified | Pass: `verify_quote` verdict `valid` |
| 4 Result used | Pass: decided "don't pay now" because the 15-minute quote cannot prove Wednesday's price |
| 5 No invention | Pass |

New limitation found: a quote proves the price for 15 minutes from now, even when the search asks about a later
meal time. Not fixed in this round. Their full report is included in the HW9 submission (Appendix B).
