# Harness

The harness sends tickets to the agent service, plays the manager who approves large refunds, kills containers at
random, and then checks what actually happened. It reads the store backend's ledger (refunds and emails) and the LLM
provider's ledger (calls, status codes, cost). Everything it reports comes from those ledgers and from
`GET /runs?ticket_id=…`. It never relies on what the service says it did.

## Running it

From the assignment folder (the one with `docker-compose.yml`), with the stack running (`make up`):

```bash
make smoke           # 30 tickets at 1/s, no chaos
make bench           # the standard profile, chaos on
make bench-nochaos   # the standard profile, chaos off
make spike           # a heavier spike, chaos on
```

Or call it directly:

```bash
uv run --project harness python -m harness.bench --profile standard [--no-chaos] [--seed 42] \
    [--sut http://localhost:8000] [--llm http://localhost:8100] [--commerce http://localhost:8200] \
    [--drain-timeout 180] [--out results]
```

| Flag | Default | Meaning |
|---|---|---|
| `--profile` | `standard` | `smoke`, `standard` or `spike` (below) |
| `--chaos` / `--no-chaos` | on for `standard` and `spike`, off for `smoke` | kill and restart containers during the run |
| `--seed` | `42` | seeds arrival jitter, which tickets the spike uses, and chaos timing |
| `--sut`, `--llm`, `--commerce` | `localhost:8000`, `:8100`, `:8200` | where the three services listen |
| `--drain-timeout` | `180` | seconds to wait after the last submit for every ticket to finish |
| `--out` | `results` | where the JSON result and `latest.md` go |

The harness resets both ledgers at the start of a bench. It does **not** reset your service. If the service remembers
tickets from an earlier bench (a durable one should), those tickets will look wrong in the next bench, because their
refunds and emails were cleared from the ledgers. The bench warns when this happens. Restart the stack between benches
(`make restart`) to get clean numbers.

## What a bench does

1. Resets the LLM provider's and the store backend's ledgers, then waits up to 60 s for the service's `/healthz`.
2. Runs three things at once:
   - **Load.** Each ticket is submitted with `POST /runs` at its scheduled time. This is open-loop: a ticket is sent
     on time even if earlier ones are still waiting. Every submit carries `Idempotency-Key: <ticket_id>` and has a
     30 s timeout. Connection errors, timeouts, `429` and `5xx` are retried up to 5 attempts in total, with 1, 2, 4
     and 8 s between attempts. Every attempt is recorded. The time of a ticket's first attempt is its start time.
     Partner integrations deliver at least once, so 5% of tickets (a seeded sample; `--duplicate-rate`) are delivered
     a second time, 0.5–5 s after the first delivery finishes, with the same `Idempotency-Key`. The second delivery
     is retried the same way, and its attempts are recorded too.
   - **Approver.** Every second it calls `GET /runs?status=awaiting_approval`. When it first sees a run waiting, it
     waits that ticket's approval delay (3–15 s, from the workload) and then calls `POST /runs/{run_id}/approve` with
     `{"approved": true|false, "approver": "harness"}`. About 80% of these are approvals. Errors are retried on the
     next tick.
   - **Chaos** (when on). Every 10–20 s it picks one running container from the target services, kills it with
     `docker kill`, and starts it again 3 s later with `docker start`.
3. After the last submit, the approver and chaos keep running. The harness polls every 2 s until every ticket has a
   finished run or `--drain-timeout` passes.
4. Chaos stops. Every container it killed is started again, even on Ctrl-C or an error. The harness then waits for
   `/healthz`, reads the final state, prints the scoreboard and writes the results.

### Profiles

Arrival rates are in tickets per second. Tickets come from the workload pool in order, `T-00001` first. During a
spike, 80% of arrivals are tickets from the large account `cust-01`.

| Profile | Shape | Tickets |
|---|---|---|
| `smoke` | 1/s for 30 s | 30 |
| `standard` | ramp 0→3/s over 30 s · 3/s for 45 s · **spike 12/s for 15 s** · 1/s for 30 s | 390 |
| `spike` | like `standard`, but the spike is **25/s for 20 s** | 710 |

### Chaos targets

Chaos kills containers of the compose services named in the `CHAOS_TARGETS` environment variable. If that is not set,
it reads `.chaos` in the assignment folder, a comma-separated list such as `api,worker`. If neither exists, the target is `api`.
It uses `docker compose ps -q <service>` from the assignment folder to find containers. If Docker or Compose isn't available,
the bench prints a warning and runs without chaos. The scoreboard shows `chaos=on, but docker unavailable`.

## The scoreboard

Every number is computed from the tickets this bench submitted. `verify.py` holds the exact definitions. A **finished
run** is one with status `completed` or `failed`.

### Hard targets (each must be 0)

| Metric | Definition |
|---|---|
| lost runs | Tickets with no finished run at the end of the drain. This includes tickets never accepted, runs stuck in `queued`, `running` or `awaiting_approval`, runs the service forgot, and tickets whose `GET /runs?ticket_id=` failed during verification. Run records without a `run_id` are ignored. |
| duplicate runs | Tickets for which `GET /runs?ticket_id=` returns more than one run. Retries and duplicate deliveries of the same ticket carry the same `Idempotency-Key` and must not start a second run. |
| duplicate refunds | Summed over tickets: `max(0, refunds − 1)`. A ticket refunded three times counts 2. |
| unexpected refunds | Tickets that got at least one refund but should not have: final sale, outside the window, not a refund request, partially shipped, or rejected by the manager. |
| missing refunds | Tickets that should have been refunded and have a finished run, but have no refund. A failed run doesn't excuse a missing refund. |
| wrong-amount refunds | Tickets that should have been refunded and have a refund whose amount differs from the order amount by more than $0.005. |
| refunds before approval | Tickets that needed approval and were refunded, where either the harness never approved them (it rejected them, or never saw them waiting), or the first refund came before the approval. The approval time is when the harness sent its first approve request that may have reached the service. A refused connection or a `4xx` other than `409` doesn't count. The comparison allows 1 s for clock differences between containers. |

If the store ledger can't be read, the refund metrics show `n/a` and fail.

### Soft targets

| Metric | Target | Definition |
|---|---|---|
| p95 time to finish, automated tickets | < 30 s | For tickets that don't need approval: the finish time minus the harness's **first** submit attempt for that ticket. The finish time is the `finished_at` of the earliest `completed` run, or of the earliest `failed` run if none completed. Lost tickets are not included; they are counted as lost runs. |
| LLM 429 rate | < 5% | `429` responses divided by all calls in the LLM ledger during the bench. |

### Other metrics (lower is better, no fixed target)

| Metric | Definition |
|---|---|
| duplicate emails | Summed over tickets: `max(0, emails − 1)`. |
| missing emails | Tickets that should get an email (all except partially shipped orders) and have a finished run, but got no email. |
| p50 / p99 time to finish | Same definition as the p95 above. |
| worst small-customer p95 | The p95 time to finish for each customer other than the large account; the scoreboard shows the worst one. It shows how the other customers fare while `cust-01`'s burst goes through. Orders that are only partially shipped are left out here, since they can't be resolved automatically; they still count in the overall percentiles. Each smaller customer has only a handful of tickets per bench, so this number is noisy: compare it across a few runs rather than reading one. |
| large-account p95 | The same p95 for `cust-01`, with the same exclusion. |
| LLM calls / 429s / 5xx | From the LLM ledger. Calls per ticket is total calls divided by tickets submitted. |
| LLM calls without X-Ticket-Id | Calls that can't be attributed to a ticket. The contract requires the header. |
| dropped streams | Streaming responses the provider cut off before `[DONE]`. |
| LLM cost | Total cost of the bench, and cost per ticket (p50, p95, max). Cost is attributed to tickets by the `X-Ticket-Id` header. A ticket with no calls costs $0. |
| partially shipped vs other tickets | Mean LLM cost per ticket for partially shipped orders compared with all other tickets. |
| submit attempts / tickets never accepted | All `POST /runs` attempts, including retries and duplicate deliveries, and tickets for which no attempt got a `2xx`. |
| approval tickets seen waiting | Tickets that needed approval and that the harness saw in `awaiting_approval`, out of all such tickets submitted. |

Percentiles use the nearest-rank method: p95 is the smallest value with at least 95% of the values at or below it.

## Results

- `results/<UTC timestamp>.json` holds everything:
  - `meta`: profile, timings and drain status.
  - `chaos`: targets and the full kill and start log.
  - `warnings`.
  - `metrics`: the scoreboard values, per-customer latency, flagged ticket ids and one row per ticket. Each row has
    every submit attempt (a second delivery's under `duplicate_delivery`), the runs, refunds, email count, approval
    attempts, LLM calls and cost, and the metrics it failed.
  - `ledgers`: the ledger summaries, plus every refund and email.
- `results/latest.md` holds the scoreboard, the flagged tickets and the chaos log.

## Workload

`harness/data/workload.json` is generated and committed. The store backend serves its orders. It holds 20 customers,
1,200 tickets (`T-00001`…`T-01200`) with one order each, and the expected outcome of each ticket:

| Category | Share | Expected |
|---|---|---|
| eligible, ≤ $500 | 45% | refund the order amount |
| eligible, > $500 | 12% | ask for approval; refund if approved (about 80%), otherwise no refund |
| final sale | 6% | no refund |
| outside the 30-day window | 7% | no refund |
| not a refund request | 28% | no refund |
| partially shipped | 2% | no refund, no email: delivery is unknown for part of the order |

Every category except partially shipped expects exactly one email to the customer. About 35% of tickets belong to
`cust-01`.

Regenerate it with `make workload`, or
`uv run --project harness python -m harness.workload --seed 42 --out harness/data/workload.json`. The same seed always
gives a byte-identical file.

## Layout

```
harness/
  data/workload.json      generated workload (seed 42)
  src/harness/
    workload.py           workload generator
    profiles.py           arrival schedules
    load.py               open-loop submitter with retries
    approver.py           plays the manager
    chaos.py              kills and restarts containers
    verify.py             reads ledgers and runs, computes every metric
    report.py             scoreboard, latest.md, JSON
    bench.py              the CLI that ties it together
  tests/                  pytest suite, with in-process fakes of the three services (tests/fakes.py)
```

Run the harness's own tests with `make test`, or `uv run --project harness pytest -q harness/tests`. They take about a
minute and don't need Docker.
