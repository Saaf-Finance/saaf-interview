# commerce: store backend

The shop's backend API: order lookup, refunds and outbound customer email.
It listens on port **8200** (`http://commerce:8200` inside the compose network).
Everything is kept in memory and cleared on restart, so it runs as a single process.

Orders are loaded at startup from the workload file (`WORKLOAD_PATH`, its `orders` object).

## Endpoints

### `GET /orders/{order_id}`

```json
{"order_id": "O-00042", "customer_id": "cust-07", "amount": 129.0, "currency": "USD",
 "days_since_delivery": 12, "final_sale": false, "status": "delivered"}
```

`status` is `delivered` or `partially_shipped`; `days_since_delivery` is `null` when the
delivery date is not known yet. Unknown orders return `404 {"error": "order not found"}`.

### `POST /refunds`

Refunds an order in full. Body: `{"ticket_id", "order_id", "amount"}`; `amount` must equal the
order amount.

| status | body | meaning |
|---|---|---|
| 201 | `{"refund_id": "rf_...", "status": "issued"}` | refund issued |
| 404 | `{"error": "order not found"}` | |
| 409 | `{"error": "a request with this idempotency key is in progress"}` | see below |
| 422 | `{"error": "amount must equal the order amount", "order_amount": ...}` | also returned for a malformed body |
| 503 | `{"error": "temporarily unavailable"}` | nothing was refunded |

**Idempotency.** Refunds accept an optional `Idempotency-Key` header. Use one key per logical
refund (for example the ticket id):

- The first request with a key is processed and its response (201, 404 or 422) is stored.
- Later requests with the same key get the stored response back, with the header
  `Idempotent-Replayed: true`, and no new refund is issued. The body of the later request is
  not compared with the original.
- While the first request is still being processed, other requests with that key get `409`.
  Retry after a short wait to get the stored response.
- A `503` is returned before the key is looked at, so it never stores anything under the key.
- Keys are kept until `POST /admin/reset` or a restart.

Without the header, every accepted request issues a new refund.

### `POST /emails`

Sends an email to a customer. Body: `{"ticket_id", "to", "subject", "body"}` → `201 {"email_id": "em_..."}`.

The email provider has **no idempotency support**: every accepted call sends a new email, and an
`Idempotency-Key` header is ignored. A `503` means nothing was sent.

### `GET /ledger`

Everything issued since the last reset:

```json
{"refunds": [{"refund_id", "ts", "ticket_id", "order_id", "amount", "idempotency_key"}],
 "refund_replays": 0,
 "emails": [{"email_id", "ts", "ticket_id", "to", "subject"}],
 "summary": {"refunds": 0, "tickets_refunded": 0, "emails": 0}}
```

`refund_replays` counts responses served from the idempotency store; `tickets_refunded` counts
distinct `ticket_id`s with at least one refund.

### `POST /admin/reset` and `GET /healthz`

`/admin/reset` clears refunds, replays, emails and stored idempotency keys, and restarts the
seeded random sequence. `/healthz` returns `{"ok": true}`.

## Reliability

Like the payment and email providers it stands in for, the service is not perfectly reliable.
With the default settings, about 1% of refund calls and 1% of email calls return `503`, and about
3% of refunds take 3 s to respond. A slow refund response starts after the refund has been
recorded. Order lookups take 20–80 ms. All random behaviour is seeded (`COMMERCE_SEED`).

## Configuration

| variable | default | |
|---|---|---|
| `WORKLOAD_PATH` | `/data/workload.json` | file with an `orders` object (order_id → order) |
| `COMMERCE_SEED` | `11` | seed for all random behaviour |
| `REFUND_ERROR_RATE` | `0.01` | share of refund calls answered with 503 |
| `REFUND_SLOW_RATE` | `0.03` | share of issued refunds that respond slowly |
| `REFUND_SLOW_S` | `3.0` | delay for slow refund responses, seconds |
| `EMAIL_ERROR_RATE` | `0.01` | share of email calls answered with 503 |
| `ORDER_LATENCY_MIN_MS` / `ORDER_LATENCY_MAX_MS` | `20` / `80` | order lookup latency range |

## Running locally

```bash
cd commerce
WORKLOAD_PATH=../harness/data/workload.json \
  uv run --no-project --python 3.12 --with-requirements requirements.txt \
  uvicorn app:app --port 8200
```

Tests:

```bash
cd commerce
uv run --no-project --python 3.12 --with-requirements requirements-dev.txt pytest -q
```
