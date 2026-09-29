"""Token buckets and the 429 responses they produce."""

import asyncio
import math

from helpers import FakeClock, chat, prompt
from mockllm.limiter import RateLimiter, TokenBucket

RL_HEADERS = ("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
              "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens")


def run(coro):
    return asyncio.run(coro)


# --- buckets ------------------------------------------------------------------

def test_bucket_refills_up_to_capacity():
    bucket = TokenBucket(capacity=10, refill_per_s=2, now=0.0)
    bucket.level = 0
    bucket.refill(1.5)
    assert bucket.level == 3
    bucket.refill(100)
    assert bucket.level == 10


def test_bucket_seconds_until():
    bucket = TokenBucket(capacity=10, refill_per_s=2, now=0.0)
    bucket.level = 1
    assert bucket.seconds_until(1) == 0
    assert bucket.seconds_until(5) == 2.0
    assert bucket.seconds_until(11) == math.inf


def test_limiter_capacity_and_refill():
    clock = FakeClock()
    limiter = RateLimiter(rpm=60, tpm=1_000_000, burst_seconds=5, clock=clock)
    results = [run(limiter.acquire(10)).allowed for _ in range(7)]
    assert results == [True] * 5 + [False] * 2  # capacity = 60/60 * 5
    clock.advance(1.0)  # refills 1 request
    assert run(limiter.acquire(10)).allowed
    assert not run(limiter.acquire(10)).allowed


def test_limiter_retry_after_for_requests():
    clock = FakeClock()
    limiter = RateLimiter(rpm=30, tpm=1_000_000, burst_seconds=2, clock=clock)  # cap 1, 0.5/s
    assert run(limiter.acquire(1)).allowed
    d = run(limiter.acquire(1))
    assert not d.allowed and d.limited_by == ("requests",)
    assert d.retry_after_s == 2  # needs 1 request at 0.5/s
    assert d.headers()["Retry-After"] == "2"


def test_limiter_retry_after_for_tokens_and_cap():
    clock = FakeClock()
    limiter = RateLimiter(rpm=6000, tpm=600, burst_seconds=5, clock=clock)  # 50 tokens, 10/s
    assert run(limiter.acquire(45)).allowed  # 5 left
    d = run(limiter.acquire(40))
    assert d.limited_by == ("tokens",)
    assert d.retry_after_s == 4  # (40 - 5) / 10 = 3.5 -> 4
    assert d.remaining_tokens == 5
    # A charge that can never fit is capped at 10 seconds.
    assert run(limiter.acquire(10_000)).retry_after_s == 10
    # A long wait is capped too.
    limiter2 = RateLimiter(rpm=6000, tpm=60, burst_seconds=50, clock=clock)  # 50 tokens, 1/s
    assert run(limiter2.acquire(50)).allowed
    assert run(limiter2.acquire(40)).retry_after_s == 10


def test_limiter_retry_after_is_at_least_one_second():
    clock = FakeClock()
    limiter = RateLimiter(rpm=6000, tpm=1_000_000, burst_seconds=1, clock=clock)  # 100, 100/s
    for _ in range(100):
        assert run(limiter.acquire(1)).allowed
    d = run(limiter.acquire(1))
    assert not d.allowed and d.retry_after_s == 1  # real wait is 0.01 s


def test_rejected_request_charges_nothing():
    clock = FakeClock()
    limiter = RateLimiter(rpm=60, tpm=600, burst_seconds=5, clock=clock)  # 5 req, 50 tokens
    assert not run(limiter.acquire(60)).allowed
    assert limiter.requests.level == 5 and limiter.tokens.level == 50


def test_limiter_reset_and_reconfigure():
    clock = FakeClock()
    limiter = RateLimiter(rpm=60, tpm=600, burst_seconds=5, clock=clock)
    for _ in range(5):
        run(limiter.acquire(1))
    assert not run(limiter.acquire(1)).allowed
    run(limiter.reset())
    assert run(limiter.acquire(1)).allowed
    run(limiter.reconfigure(rpm=12, tpm=600))  # capacity 1 request
    assert limiter.requests.capacity == 1 and limiter.requests.level <= 1
    assert limiter.rpm == 12


# --- over HTTP ----------------------------------------------------------------

def test_burst_above_request_limit_gets_429s(make_client, clock):
    client = make_client(rpm=60)  # 5 requests of burst, then 1/s
    responses = [chat(client, prompt("classify", {"message": "refund"}), ticket_id=f"T-{i}")
                 for i in range(12)]
    codes = [r.status_code for r in responses]
    assert codes == [200] * 5 + [429] * 7

    for r in responses[5:]:
        assert r.headers["Retry-After"] == "1"
        assert r.headers["x-ratelimit-limit-requests"] == "60"
        assert r.headers["x-ratelimit-remaining-requests"] == "0"
        assert r.headers["x-ratelimit-limit-tokens"] == "600000"
        assert int(r.headers["x-ratelimit-remaining-tokens"]) > 0
        err = r.json()["error"]
        assert err["type"] == "rate_limit_error"
        assert err["message"] == ("Rate limit reached for requests per minute. "
                                  "Retry after 1 s.")
    for r in responses[:5]:
        assert all(h in r.headers for h in RL_HEADERS)
        assert "Retry-After" not in r.headers

    ledger = client.get("/ledger").json()
    assert ledger["calls"] == 12
    assert ledger["by_status"] == {"200": 5, "429": 7}
    assert ledger["cost_usd"] > 0
    rejected = ledger["per_ticket"]["T-7"]
    assert (rejected["calls"], rejected["ok_calls"], rejected["cost_usd"]) == (1, 0, 0.0)
    accepted = ledger["per_ticket"]["T-0"]
    assert (accepted["calls"], accepted["ok_calls"]) == (1, 1) and accepted["cost_usd"] > 0
    raw = client.get("/ledger/raw").json()["entries"]
    assert [e["status_code"] for e in raw] == codes
    assert all(e["cost_usd"] == 0 and e["completion_tokens"] == 0
               for e in raw if e["status_code"] == 429)

    clock.advance(1.0)
    assert chat(client, prompt("classify", {"message": "x"})).status_code == 200
    assert chat(client, prompt("classify", {"message": "x"})).status_code == 429


def test_token_limit_uses_prompt_plus_max_tokens(make_client, clock):
    client = make_client(tpm=6000)  # 500 tokens of burst, 100 tokens/s
    messages = [{"role": "user", "content": "x" * 400}]  # 100 prompt tokens
    ok = [chat(client, messages, max_tokens=150).status_code for _ in range(3)]
    assert ok == [200, 200, 429]  # 250 + 250 = 500 fits, the third does not
    r = chat(client, messages, max_tokens=150)
    assert r.status_code == 429
    assert r.headers["x-ratelimit-remaining-tokens"] == "0"
    assert r.headers["Retry-After"] == "3"  # 250 tokens at 100/s = 2.5 s -> 3
    assert "tokens per minute" in r.json()["error"]["message"]
    # Without max_tokens the charge is prompt + 200.
    clock.advance(5)
    assert chat(client, messages).status_code == 200  # 300
    assert chat(client, messages).status_code == 429  # 200 left < 300


def test_both_limits_reported_when_both_short(make_client):
    client = make_client(rpm=12, tpm=600)  # 1 request of burst, 50 tokens
    messages = [{"role": "user", "content": "hi"}]
    assert chat(client, messages, max_tokens=40).status_code == 200
    r = chat(client, messages, max_tokens=40)
    assert r.status_code == 429
    assert "requests and tokens per minute" in r.json()["error"]["message"]
