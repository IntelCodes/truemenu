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
