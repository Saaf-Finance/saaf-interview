"""POST /v1/chat/completions: JSON and SSE responses, errors, latency and billing."""

import json
import math
import random
import statistics
import time

import pytest

from helpers import chat, parse_sse, prompt
from mockllm.app import sample_latency, split_text
from mockllm.settings import Settings

TICKET = {"ticket_id": "T-00001", "customer_id": "cust-02", "order_id": "O-00001",
          "email": "cust-02@example.com", "message": "My kettle arrived broken, refund please"}
ORDER = {"order_id": "O-00001", "customer_id": "cust-02", "amount": 49.99, "currency": "USD",
         "days_since_delivery": 4, "final_sale": False, "status": "delivered"}


def content_of(response):
    return response.json()["choices"][0]["message"]["content"]


# --- non-streaming ------------------------------------------------------------

def test_completion_shape_and_usage(client):
    messages = prompt("classify", TICKET)
    r = chat(client, messages)
    assert r.status_code == 200
    body = r.json()
    assert body["id"].startswith("chatcmpl-")
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int)
    assert body["model"] == "mock-large"
    choice = body["choices"][0]
    assert choice["index"] == 0 and choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    chars = sum(len(m["content"]) for m in messages)
    usage = body["usage"]
    assert usage["prompt_tokens"] == math.ceil(chars / 4)
    assert usage["completion_tokens"] == math.ceil(len(choice["message"]["content"]) / 4)
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_classify_over_http(client):
    out = json.loads(content_of(chat(client, prompt("classify", TICKET))))
    assert out["intent"] == "refund_request"
    other = {**TICKET, "message": "Where is my parcel?"}
    assert json.loads(content_of(chat(client, prompt("classify", other))))["intent"] == "not_refund"


@pytest.mark.parametrize("changes, expected", [
    ({}, "eligible"),
    ({"days_since_delivery": 31}, "ineligible"),
    ({"final_sale": True}, "ineligible"),
    ({"status": "partially_shipped", "days_since_delivery": None}, "need_more_info"),
])
def test_assess_over_http(client, changes, expected):
    out = json.loads(content_of(chat(client, prompt("assess", TICKET, {**ORDER, **changes}))))
    assert out["decision"] == expected


def test_draft_reply_over_http(client):
    text = content_of(chat(client, prompt("draft_reply", TICKET, ORDER, {"outcome": "refunded"})))
    assert 300 <= len(text) <= 900 and "O-00001" in text


def test_task_is_read_from_last_user_message(client):
    messages = [
        {"role": "system", "content": "TASK: assess"},
        {"role": "user", "content": "TASK: assess\nORDER: {}"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": f"TASK: classify\nTICKET: {json.dumps(TICKET)}"},
    ]
    assert json.loads(content_of(chat(client, messages)))["intent"] == "refund_request"
    entry = client.get("/ledger/raw").json()["entries"][-1]
    assert entry["task"] == "classify"


def test_content_parts_are_accepted(client):
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "TASK: classify\n"},
        {"type": "text", "text": f"TICKET: {json.dumps(TICKET)}"},
    ]}]
    assert json.loads(content_of(chat(client, messages)))["intent"] == "refund_request"


def test_no_task_gets_generic_answer(client):
    r = chat(client, [{"role": "user", "content": "Hello!"}])
    assert content_of(r) == "I can help with that. Could you share more details?"
    assert client.get("/ledger/raw").json()["entries"][-1]["task"] is None


def test_invalid_request_is_400_and_recorded(client):
    r = client.post("/v1/chat/completions", json={"model": "m"}, headers={"X-Ticket-Id": "T-9"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"
    entry = client.get("/ledger/raw").json()["entries"][-1]
    assert entry["status_code"] == 400 and entry["ticket_id"] == "T-9" and entry["cost_usd"] == 0


def test_headers_are_recorded(client):
    chat(client, prompt("classify", TICKET), ticket_id="T-00001", run_id="run-abc")
    chat(client, prompt("classify", TICKET), ticket_id=None)
    first, second = client.get("/ledger/raw").json()["entries"]
    assert (first["ticket_id"], first["run_id"]) == ("T-00001", "run-abc")
    assert (second["ticket_id"], second["run_id"]) == (None, None)
    summary = client.get("/ledger").json()
    assert summary["calls"] == 2 and list(summary["per_ticket"]) == ["T-00001"]


# --- streaming ----------------------------------------------------------------

def test_stream_chunks_then_usage_then_done(client):
    expected = content_of(chat(client, prompt("assess", TICKET, ORDER)))
    r = chat(client, prompt("assess", TICKET, ORDER), stream=True)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    for c in chunks:
        assert c["object"] == "chat.completion.chunk"
        assert c["id"] == chunks[0]["id"] and c["model"] == "mock-large"
        assert c["choices"][0]["index"] == 0
    content_chunks, final = chunks[:-1], chunks[-1]
    assert 9 <= len(content_chunks) <= 21  # first chunk + 8..20 more
    assert content_chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert all(c["choices"][0]["finish_reason"] is None for c in content_chunks)
    text = "".join(c["choices"][0]["delta"]["content"] for c in content_chunks)
    assert text == expected
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["choices"][0]["delta"] == {}
    assert final["usage"]["completion_tokens"] == math.ceil(len(text) / 4)

    entry = client.get("/ledger/raw").json()["entries"][-1]
    assert entry["stream"] is True and entry["dropped"] is False
    assert entry["completion_tokens"] == final["usage"]["completion_tokens"]
    assert entry["cost_usd"] > 0


def test_stream_drop_stops_half_way_without_done(make_client):
    client = make_client(stream_drop_rate=1.0)
    full = content_of(chat(client, prompt("draft_reply", TICKET, ORDER, {"outcome": "refunded"})))
    r = chat(client, prompt("draft_reply", TICKET, ORDER, {"outcome": "refunded"}), stream=True)
    assert r.status_code == 200
    events = parse_sse(r.text)
    assert "[DONE]" not in events
    chunks = [json.loads(e) for e in events]
    assert all(c["choices"][0]["finish_reason"] is None for c in chunks)
    assert all("usage" not in c for c in chunks)
    text = "".join(c["choices"][0]["delta"]["content"] for c in chunks)
    assert 0 < len(text) < len(full) and full.startswith(text)

    ledger = client.get("/ledger").json()
    assert ledger["dropped_streams"] == 1
    entry = client.get("/ledger/raw").json()["entries"][-1]
    assert entry["dropped"] is True and entry["status_code"] == 200
    assert entry["completion_tokens"] == math.ceil(len(text) / 4)


def test_stream_is_spread_over_latency(make_client):
    client = make_client(latency_median_s=0.4, latency_sigma=0.0)
    started = time.perf_counter()
    r = chat(client, prompt("classify", TICKET), stream=True)
    elapsed = time.perf_counter() - started
    assert parse_sse(r.text)[-1] == "[DONE]"
    assert 0.35 <= elapsed < 1.5
    assert client.get("/ledger/raw").json()["entries"][-1]["latency_s"] >= 0.35


def test_split_text():
    assert split_text("abcdefghij", 3) == ["abcd", "efg", "hij"]
    assert split_text("ab", 10) == ["a", "b"]
    assert split_text("", 5) == [""]


# --- server errors, latency, billing -------------------------------------------

def test_injected_server_errors(make_client):
    client = make_client(error_rate=1.0)
    codes = set()
    for _ in range(12):
        r = chat(client, prompt("classify", TICKET))
        assert r.status_code in (500, 503)
        assert r.json()["error"]["type"] == "server_error"
        codes.add(r.status_code)
    assert codes == {500, 503}
    ledger = client.get("/ledger").json()
    assert ledger["calls"] == 12 and ledger["cost_usd"] == 0
    assert ledger["completion_tokens"] == 0


def test_latency_is_applied(make_client):
    client = make_client(latency_median_s=0.3, latency_sigma=0.0)
    started = time.perf_counter()
    chat(client, prompt("classify", TICKET))
    assert time.perf_counter() - started >= 0.3
    assert client.get("/ledger/raw").json()["entries"][-1]["latency_s"] >= 0.3


def test_latency_distribution():
    rng = random.Random(7)
    s = Settings(latency_median_s=0.8, latency_sigma=0.6, slow_tail_rate=0.0)
    samples = [sample_latency(rng, s) for _ in range(20_000)]
    assert 0.76 <= statistics.median(samples) <= 0.84
    assert max(samples) > 3 * 0.8  # long right tail

    slow = Settings(slow_tail_rate=1.0, slow_tail_s=8.0)
    assert all(6.4 <= sample_latency(rng, slow) <= 10.0 for _ in range(1000))
    assert sample_latency(rng, Settings(latency_median_s=0.0, slow_tail_rate=0.0)) == 0.0


def test_same_seed_same_random_sequence():
    a, b = random.Random(7), random.Random(7)
    s = Settings()
    assert [sample_latency(a, s) for _ in range(50)] == [sample_latency(b, s) for _ in range(50)]


def test_cost_is_charged_only_for_200(make_client, clock):
    client = make_client(rpm=24)  # 2 requests of burst
    ok1 = chat(client, prompt("classify", TICKET), ticket_id="T-A")
    ok2 = chat(client, prompt("draft_reply", TICKET, ORDER, {"outcome": "refunded"}),
               ticket_id="T-A", stream=True)
    limited = chat(client, prompt("classify", TICKET), ticket_id="T-A")
    assert (ok1.status_code, ok2.status_code, limited.status_code) == (200, 200, 429)

    client.post("/admin/config", json={"error_rate": 1.0})
    clock.advance(10)
    failed = chat(client, prompt("classify", TICKET), ticket_id="T-A")
    assert failed.status_code in (500, 503)

    entries = client.get("/ledger/raw").json()["entries"]
    assert [e["status_code"] for e in entries] == [200, 200, 429, failed.status_code]
    for e in entries:
        expected = (e["prompt_tokens"] * 3.0 + e["completion_tokens"] * 15.0) / 1e6
        if e["status_code"] == 200:
            assert e["cost_usd"] == pytest.approx(expected) and e["cost_usd"] > 0
        else:
            assert e["cost_usd"] == 0 and e["completion_tokens"] == 0
            assert e["prompt_tokens"] > 0

    ledger = client.get("/ledger").json()
    billed = sum(e["cost_usd"] for e in entries if e["status_code"] == 200)
    assert ledger["cost_usd"] == pytest.approx(billed, abs=1e-6)
    assert ledger["by_status"] == {"200": 2, "429": 1, str(failed.status_code): 1}
    t = ledger["per_ticket"]["T-A"]
    assert t["calls"] == 4 and t["ok_calls"] == 2
    assert t["cost_usd"] == pytest.approx(billed, abs=1e-6)
    assert t["tokens"] == sum(e["prompt_tokens"] + e["completion_tokens"] for e in entries)
    assert ledger["prompt_tokens"] == sum(e["prompt_tokens"] for e in entries)
    assert ledger["completion_tokens"] == sum(e["completion_tokens"] for e in entries)
