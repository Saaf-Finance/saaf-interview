# Take-home: Make this agent survive production

**Role:** MLOps Engineer — AI Platform & Infrastructure
**Time box:** 3–4 hours. Please stop at 4 and write down what you'd do next.
**Tools:** Use anything you like, including AI coding assistants. We expect you to.

---

## The situation

An online store runs an AI agent that handles return and refund requests. It reads the ticket, looks up the order,
decides whether the request is eligible under the return policy, asks a manager to approve refunds over $500, issues
the refund and emails the customer.

It works, one customer at a time. It runs each ticket inside the HTTP request, keeps everything in memory, and has never
met real traffic. Next week it will: a partner integration starts forwarding tickets in bursts, and the platform
restarts machines without warning.

Your job is to make it survive production.

## What's in this folder

| Path | What it is | Yours to change? |
|---|---|---|
| `agent/` | The agent service: LangGraph + FastAPI | Yes. Change, rewrite or replace it |
| `mock_llm/` | A fake LLM provider, OpenAI-compatible | No |
| `commerce/` | The store's backend: orders, refunds, emails | No |
| `harness/` | Load generator, chaos, and the scoreboard | No |
| `docs/CONTRACT.md` | The API your service must keep | Read it first |

Facts about the environment, as you'd find them on a provider's dashboard or in a vendor's docs:

- **LLM provider:** your account allows **1,200 requests/minute** and **600,000 tokens/minute**, shared by everything you
  run. Latency has a long tail, and about 2% of requests fail with a `5xx`. When you're over the limit you get a `429`
  with a `Retry-After` header.
- **Refunds API:** accepts an `Idempotency-Key` header. Occasionally slow, occasionally `503`.
- **Email API:** no idempotency support.
- **Approvals:** refunds over $500 need a manager. During a benchmark the harness plays the manager: it approves or
  rejects waiting runs after a few seconds.
- **Delivery:** the partner integration delivers at least once. The same ticket can arrive more than once, always
  with the same `Idempotency-Key`.
- **Hand-offs:** if the agent can't reach a decision on a ticket, a human takes it over. The agent issues no refund
  and sends no email.

## Quick start

You need Docker and [uv](https://docs.astral.sh/uv/). Run every command from this folder (`mlops-engineer/`).

```bash
make up      # build and start the fake provider, the store backend and the agent
make smoke   # 30 tickets, no chaos: a sanity check (a few minutes against the starting code)
make bench   # the scored run: a few hundred tickets with a spike, containers killed at random (about 5 minutes)
```

Every `make smoke` and `make bench` starts from a fresh stack (`docker compose down -v`, then `up`), so runs
don't leak state into each other.

**Before you change anything**, run `make bench` and save the scoreboard. That's your "before". It takes about six
minutes, longer than later runs, because some runs never finish and the harness waits out its drain timeout.

## The scoreboard

`make bench` prints a scoreboard and writes it to `results/`. See `harness/README.md` for exactly how each number is
computed.

**Hard targets — each must be 0:**
lost runs · duplicate runs · duplicate refunds · unexpected refunds · missing refunds · wrong-amount refunds ·
refunds before approval

**Soft targets — we look at these and at how you reason about them:**
p95 completion time for runs that don't need approval · 429 rate · cost per ticket · duplicate emails · how evenly
customers are served during the spike

## What to build, in priority order

Work down the list. If you run out of time, stop and say in `RESULTS.md` what you'd do next.

1. **Run agents in the background.** `POST /runs` returns quickly. Runs execute on workers you can scale horizontally.
2. **Durable runs.** Kill any worker at any moment and every run still finishes. That includes runs waiting for a
   manager, which can wait minutes and shouldn't tie up a worker while they do.
3. **No duplicate side effects.** A retried or resumed run must never refund twice. Avoid duplicate emails as far as
   you can, and tell us what guarantee you can actually give for them.
4. **Respect the provider.** Stay within the account limits across all your workers combined, back off when told to,
   and when a spike arrives, queue it rather than fail it.
5. **`RESULTS.md`**, at most two pages:
   - your before and after scoreboards;
   - where your system breaks next, and why;
   - what you'd change to handle ten times this traffic;
   - what you cut.

**Optional, only if you have time.** None of these is needed for a strong result.
- Progress streaming via `GET /runs/{run_id}/events`.
- Per-run cost and tracing.
- Shipping a new version of the graph while runs from the old version are still in flight.
- Infrastructure-as-code for a production deployment on a cloud of your choice. It only has to validate or plan.

## Rules

- Keep the API in `docs/CONTRACT.md`. The harness depends on it.
- Don't modify `mock_llm/`, `commerce/` or `harness/`. We run our own copies against your service.
- List every service that runs your code in `.chaos`: the API and anything that executes runs, e.g. `api,worker`.
  During `make bench`, chaos kills a random container from those services every 10–20 seconds and starts it again
  3 seconds later. Off-the-shelf infrastructure you add, such as a database, a queue or a load balancer, isn't killed
  by the bench. We may restart it in the follow-up.
- Add whatever you need to `docker-compose.yml`: a database, a queue, more services. `make up` must start everything
  on a clean machine.
- Your service must not read the harness's workload file or its expected outcomes. It may call only the LLM's
  `/v1/chat/completions` and the store's `/orders`, `/refunds` and `/emails`. The `/ledger` and `/admin` endpoints
  belong to the harness.
- A real LLM provider is optional. We score with the fake one.

## What to send back

- This folder with your changes, in a **private** repository shared with us, or as a zip. Please don't fork this
  repository publicly.
- `RESULTS.md` with your before and after scoreboards.
- Optionally, a screen recording of five minutes or less.

## How we'll look at it

- We run `make up && make bench` ourselves, on a clean machine.
- Hard targets first, then soft targets and your explanations of them.
- Could a teammate operate this at 3am from what you've written?
- Does `RESULTS.md` hold up when we push on it?

We don't score how many optional items you did, or your choice of queue, database or framework.

## The follow-up

A 60-minute technical interview with a senior engineer covering the review of the take-home task, production
infrastructure fundamentals, containers and CI/CD, Python, and debugging scenarios.

Your code remains yours. This is an exercise, and we won't use your submission in our products.
