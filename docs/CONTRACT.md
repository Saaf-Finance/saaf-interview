# Service contract

The harness drives your service only through the endpoints below, and reads the fake LLM provider's and the store
backend's ledgers to check what actually happened. Rework anything behind this contract — split it into services, add
queues and databases, rewrite it in another framework — but keep these endpoints and behaviours.

Base URL: `http://localhost:8000`.

## Endpoints

### `POST /runs`
Starts handling a support ticket.

Headers: `Idempotency-Key: <ticket_id>`. Clients, including the harness, retry this request on timeouts, connection
errors, `429` and `5xx`, always with the same key. The partner integration also delivers at least once, so the same
ticket can arrive again after it was accepted, with the same key. A repeat must not start a second run: return the
existing one.

Body:
```json
{"ticket_id": "T-00042", "customer_id": "cust-07", "order_id": "O-00042", "email": "cust-07@example.com",
 "message": "The blender arrived broken, I'd like a refund."}
```
Response: any `2xx` means accepted. The JSON body must include at least `run_id` and `status`. You may return as soon as
the run is accepted; you don't have to wait for it to finish.

### `GET /runs/{run_id}`
Returns a run record:
```json
{"run_id": "…", "ticket_id": "T-00042", "customer_id": "cust-07",
 "status": "queued | running | awaiting_approval | completed | failed",
 "created_at": 1790000000.12, "finished_at": 1790000004.87, "error": null, "result": {}, "steps": 7}
```
`created_at` and `finished_at` are Unix timestamps in seconds. `finished_at` is `null` until the run reaches
`completed` or `failed`.

Every run must eventually reach `completed` or `failed`. A run that hands its ticket to a human still finishes: use
either status and say what happened in `result` or `error`.

### `GET /runs?ticket_id=T-00042` and `GET /runs?status=awaiting_approval`
Both return `{"runs": [<run record>, …]}`.

### `POST /runs/{run_id}/approve`
Body: `{"approved": true, "approver": "manager-name"}`. Resumes a run that is waiting for a manager. Calling it again
after the decision has been made must not change the decision; return `2xx` or `409`.

### `GET /healthz`
Returns `200` when the service is ready to accept runs.

### Optional: `GET /runs/{run_id}/events`
Server-sent events describing progress. The harness doesn't call it; if you build it, we'll try it by hand.

## Calls your service makes

- **LLM:** `POST http://mock-llm:8100/v1/chat/completions`, OpenAI-compatible. Send the header `X-Ticket-Id: <ticket_id>`
  on every call so cost can be attributed to tickets. Keep the `TASK: …` prompt lines used in `agent/app/llm.py`: the fake
  provider answers based on them.
- **Store backend:** `http://commerce:8200`. Refund and email requests must include `ticket_id` in the body. Refunds
  accept an `Idempotency-Key` header; emails don't.

## Rules

- Don't modify `mock_llm/`, `commerce/` or `harness/`. We run our own copies of them against your service.
- List every compose service that runs your code in `.chaos`: the API and anything that executes runs (comma-separated,
  e.g. `api,worker`). Chaos kills a random container from those services every 10–20 seconds and starts it again
  3 seconds later. Off-the-shelf infrastructure you add (a database, a queue, a load balancer) isn't killed.
- Your service must not read the workload file or its expected outcomes. It learns about tickets only from `POST /runs`
  and about orders only from the store backend.
- Your service may call only `POST /v1/chat/completions` on the LLM provider and `/orders`, `/refunds` and `/emails` on
  the store backend. `/ledger` and `/admin` on both belong to the harness.
- `make up` must start everything on a clean machine with Docker and uv installed.
