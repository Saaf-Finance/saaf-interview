# Mock LLM provider

A stand-in for a hosted LLM API. It speaks a subset of the OpenAI Chat Completions API,
enforces account-wide rate limits, has realistic latency and occasional server errors,
and keeps a usage ledger with per-call cost.

Base URL: `http://mock-llm:8100/v1` inside docker compose, `http://localhost:8100/v1` from the host.

| Method | Path                    | Purpose                                        |
|--------|-------------------------|------------------------------------------------|
| POST   | `/v1/chat/completions`  | Chat completion (JSON or SSE stream)           |
| GET    | `/ledger`               | Usage and cost summary                         |
| GET    | `/ledger/raw`           | Every recorded call                            |
| GET    | `/admin/config`         | Current settings                               |
| POST   | `/admin/config`         | Change settings at runtime                     |
| POST   | `/admin/reset`          | Clear the ledger and refill rate-limit buckets |
| GET    | `/healthz`              | `{"ok": true}`                                 |

## Chat completions

```bash
curl -s localhost:8100/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Ticket-Id: T-00001' -H 'X-Run-Id: 3f1c...' \
  -d '{"model": "mock-large", "messages": [
        {"role": "system", "content": "You are a friendly support agent."},
        {"role": "user", "content": "TASK: classify\nTICKET: {\"ticket_id\": \"T-00001\", \"message\": \"My kettle arrived broken\"}"}]}'
```

Request body: `model`, `messages` (`[{role, content}]`), optional `stream` (default `false`),
`max_tokens`, `temperature`. Other OpenAI fields are accepted and ignored. Any model name works.

Headers: `X-Ticket-Id` and `X-Run-Id` are optional and are recorded in the ledger so usage can
be attributed. `Authorization` is accepted and ignored.

Response (non-streaming):

```json
{"id": "chatcmpl-...", "object": "chat.completion", "created": 1767225600, "model": "mock-large",
 "choices": [{"index": 0, "message": {"role": "assistant", "content": "{\"intent\": \"refund_request\", \"confidence\": 0.91}"},
              "finish_reason": "stop"}],
 "usage": {"prompt_tokens": 38, "completion_tokens": 13, "total_tokens": 51}}
```

### Streaming

With `"stream": true` the response is `text/event-stream`. Each event is one line
`data: <json>` followed by a blank line:

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":...,"model":"mock-large","choices":[{"index":0,"delta":{"role":"assistant","content":"{\"inte"},"finish_reason":null}]}
data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...,"choices":[{"index":0,"delta":{"content":"nt\": \"re"},"finish_reason":null}]}
...
data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...,"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{...}}
data: [DONE]
```

A complete stream always ends with a chunk whose `finish_reason` is `"stop"` (it carries
`usage`) followed by `data: [DONE]`. Streams are occasionally cut off by the provider part-way
through (see `MOCK_LLM_STREAM_DROP_RATE`); such a stream simply ends without the final chunk
and without `[DONE]`, and the content received so far is incomplete.

## Prompt format

The model behind this API is not a general-purpose LLM. It reads these lines from the **last
user message** (each on its own line, JSON on a single line) and ignores everything else:

```
TASK: classify | assess | draft_reply
TICKET: {"ticket_id": "...", "order_id": "...", "message": "..."}
ORDER: {"order_id": "...", "amount": 42.5, "days_since_delivery": 12, "final_sale": false, "status": "delivered"}
DECISION: {"outcome": "refunded", "reason": "..."}
```

| TASK          | Reply content                                                                                     |
|---------------|---------------------------------------------------------------------------------------------------|
| `classify`    | JSON `{"intent": "refund_request" \| "not_refund", "confidence": 0.80–0.99}`, from `TICKET.message` |
| `assess`      | JSON `{"decision": "eligible" \| "ineligible" \| "need_more_info", "reason": "..."}`, from `ORDER`: 30-day return window, no refunds on final-sale items, `need_more_info` when the order has no usable delivery date |
| `draft_reply` | Plain-text email to the customer (300–900 characters) mentioning the order id; wording follows `DECISION.outcome` (`refunded`, `declined`, `not_refund`, `escalated`) |
| anything else | `I can help with that. Could you share more details?`                                            |

Replies are deterministic: the same prompt always gets the same answer.

## Rate limits

Limits are per account, i.e. shared by every caller of this server. There are two token buckets
and a request must fit in both:

| Bucket   | Refill           | Burst capacity                    | Defaults                 |
|----------|------------------|-----------------------------------|--------------------------|
| requests | `RPM / 60` per s | `RPM / 60 × BURST_SECONDS`        | 20/s, burst 100          |
| tokens   | `TPM / 60` per s | `TPM / 60 × BURST_SECONDS`        | 10,000/s, burst 50,000   |

Tokens are estimated as `ceil(characters / 4)` over the content of all messages. An admitted
request is charged 1 request and `prompt_tokens + max_tokens` tokens (`max_tokens` defaults to
200 when not set). Rejected requests are not charged. A request whose token charge is larger
than the token burst capacity can never be admitted.

When a limit is hit the server answers immediately with **429**:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 1
x-ratelimit-limit-requests: 1200
x-ratelimit-remaining-requests: 0
x-ratelimit-limit-tokens: 600000
x-ratelimit-remaining-tokens: 41250

{"error": {"type": "rate_limit_error", "message": "Rate limit reached for requests per minute. Retry after 1 s."}}
```

`Retry-After` is whole seconds (1–10) until the bucket has refilled enough for that request.
Every chat completion response carries the `x-ratelimit-*` headers.

## Errors

| Status | `error.type`            | When                                                       |
|--------|-------------------------|------------------------------------------------------------|
| 400    | `invalid_request_error` | Body fails validation (e.g. missing `model` or `messages`) |
| 429    | `rate_limit_error`      | Rate limit reached (see above)                             |
| 500    | `server_error`          | Random internal error, `MOCK_LLM_ERROR_RATE` of requests (half 500, half 503), after 50–300 ms |
| 503    | `server_error`          | Random overload, same as above                             |

All errors use the body `{"error": {"type": "...", "message": "..."}}`.

## Latency

Response time is lognormal around `MOCK_LLM_LATENCY_MEDIAN_S` (spread `MOCK_LLM_LATENCY_SIGMA`).
A small share of requests (`MOCK_LLM_SLOW_TAIL_RATE`) takes about `MOCK_LLM_SLOW_TAIL_S`
seconds (±25%). For streams the first chunk arrives after about a quarter of that time and the
rest of the content is spread over the remainder.

## Billing and the ledger

`cost_usd = prompt_tokens × PRICE_INPUT_PER_MTOK / 1e6 + completion_tokens × PRICE_OUTPUT_PER_MTOK / 1e6`

Only successful (200) calls are billed; 400, 429 and 5xx responses cost nothing. A
non-streaming call is billed once the provider has finished it, even if the client stopped
waiting. Streams are billed for the content actually delivered.

Every call to `/v1/chat/completions` is recorded, whatever the outcome.
`GET /ledger/raw` returns `{"entries": [...]}` with one entry per call:

```json
{"ts": 1767225600.12, "ticket_id": "T-00001", "run_id": "3f1c...", "task": "classify",
 "status_code": 200, "prompt_tokens": 38, "completion_tokens": 13, "cost_usd": 0.000309,
 "latency_s": 0.7421, "stream": false, "dropped": false}
```

`GET /ledger` summarises them:

```json
{"calls": 1520, "by_status": {"200": 1431, "429": 61, "500": 14, "503": 14},
 "prompt_tokens": 512340, "completion_tokens": 98812, "cost_usd": 3.019, "dropped_streams": 0,
 "per_ticket": {"T-00001": {"calls": 4, "ok_calls": 4, "cost_usd": 0.0021, "tokens": 1470}}}
```

Token totals include every call (rejected and failed calls count their prompt tokens and zero
completion tokens); `cost_usd` only counts billed calls. Calls without an `X-Ticket-Id` header
are included in the totals but not in `per_ticket`.

## Admin

`POST /admin/config` takes a partial JSON object with any of `rpm`, `tpm`, `error_rate`,
`latency_median_s`, `latency_sigma`, `slow_tail_rate`, `slow_tail_s`, `stream_drop_rate` and
returns the full new config. Changes apply to requests that arrive afterwards. Unknown keys or
out-of-range values are rejected with 422. When `rpm`/`tpm` change, the buckets keep their
current level (capped at the new capacity) and refill at the new rate.

```bash
curl -s localhost:8100/admin/config -H 'Content-Type: application/json' -d '{"rpm": 300, "error_rate": 0.1}'
```

`POST /admin/reset` clears the ledger, refills both buckets and restarts the random sequence
from the seed. Settings are kept.

## Configuration

| Variable                          | Default  | Meaning                                         |
|-----------------------------------|----------|-------------------------------------------------|
| `MOCK_LLM_RPM`                    | 1200     | Requests per minute                             |
| `MOCK_LLM_TPM`                    | 600000   | Tokens per minute                               |
| `MOCK_LLM_BURST_SECONDS`          | 5        | Bucket capacity, in seconds of refill           |
| `MOCK_LLM_ERROR_RATE`             | 0.02     | Share of admitted requests answered with 500/503 |
| `MOCK_LLM_LATENCY_MEDIAN_S`       | 0.8      | Median response time (0 disables latency)      |
| `MOCK_LLM_LATENCY_SIGMA`          | 0.6      | Lognormal sigma                                 |
| `MOCK_LLM_SLOW_TAIL_RATE`         | 0.01     | Share of very slow responses                    |
| `MOCK_LLM_SLOW_TAIL_S`            | 8.0      | Duration of a very slow response                |
| `MOCK_LLM_STREAM_DROP_RATE`       | 0.0      | Share of streams cut off half-way               |
| `MOCK_LLM_SEED`                   | 7        | Random seed                                     |
| `MOCK_LLM_PRICE_INPUT_PER_MTOK`   | 3.0      | USD per million prompt tokens                   |
| `MOCK_LLM_PRICE_OUTPUT_PER_MTOK`  | 15.0     | USD per million completion tokens               |

## Running

The server must run as a single process with one worker: limits and the ledger live in memory.

```bash
# Docker (listens on 8100)
docker build -t mock-llm . && docker run --rm -p 8100:8100 mock-llm

# Locally, from this directory
uv run --python 3.12 --with-requirements requirements.txt uvicorn mockllm.app:app --port 8100

# Tests
uv run --python 3.12 --with-requirements requirements-dev.txt pytest
```
