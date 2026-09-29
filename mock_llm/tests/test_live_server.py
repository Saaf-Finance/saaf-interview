"""End-to-end checks against a real uvicorn process over HTTP."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
TICKET = {"ticket_id": "T-00077", "order_id": "O-00077", "message": "It arrived damaged"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = free_port()
    env = {**os.environ, "MOCK_LLM_RPM": "120", "MOCK_LLM_ERROR_RATE": "0",
           "MOCK_LLM_LATENCY_MEDIAN_S": "0.05", "MOCK_LLM_LATENCY_SIGMA": "0.2",
           "MOCK_LLM_SLOW_TAIL_RATE": "0"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mockllm.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--workers", "1", "--no-access-log"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while True:
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline or proc.poll() is not None:
            proc.kill()
            pytest.fail("server did not start:\n" + proc.stdout.read().decode())
        time.sleep(0.1)
    yield base
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(autouse=True)
def reset(server):
    httpx.post(f"{server}/admin/reset")


def body(task="classify", stream=False):
    return {"model": "mock-large", "stream": stream, "messages": [
        {"role": "system", "content": "You are a helpful store support agent."},
        {"role": "user", "content": f"TASK: {task}\nTICKET: {json.dumps(TICKET)}"}]}


def test_concurrent_burst_gets_429s(server):
    """120 rpm = 10 requests of burst, refilling at 2/s. 40 concurrent requests."""
    async def burst():
        async with httpx.AsyncClient(base_url=server, timeout=10) as client:
            return await asyncio.gather(*[
                client.post("/v1/chat/completions", json=body(),
                            headers={"X-Ticket-Id": f"T-{i:05d}", "X-Run-Id": "burst"})
                for i in range(40)])

    responses = asyncio.run(burst())
    ok = [r for r in responses if r.status_code == 200]
    limited = [r for r in responses if r.status_code == 429]
    assert len(ok) + len(limited) == 40
    assert 10 <= len(ok) <= 13
    for r in limited:
        assert 1 <= int(r.headers["Retry-After"]) <= 10
        assert r.headers["x-ratelimit-limit-requests"] == "120"
        assert r.json()["error"]["type"] == "rate_limit_error"

    ledger = httpx.get(f"{server}/ledger").json()
    assert ledger["calls"] == 40
    assert ledger["by_status"] == {"200": len(ok), "429": len(limited)}
    assert len(ledger["per_ticket"]) == 40
    assert sum(t["ok_calls"] for t in ledger["per_ticket"].values()) == len(ok)


def test_stream_over_http(server):
    events = []
    with httpx.stream("POST", f"{server}/v1/chat/completions", json=body(stream=True),
                      headers={"X-Ticket-Id": "T-00077"}, timeout=10) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(line[len("data: "):])
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert json.loads(text)["intent"] == "refund_request"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop" and "usage" in chunks[-1]
    entry = httpx.get(f"{server}/ledger/raw").json()["entries"][-1]
    assert entry["stream"] and not entry["dropped"] and entry["cost_usd"] > 0


def test_admin_config_over_http(server):
    try:
        r = httpx.post(f"{server}/admin/config", json={"error_rate": 1.0})
        assert r.status_code == 200 and r.json()["error_rate"] == 1.0
        r = httpx.post(f"{server}/v1/chat/completions", json=body(), timeout=10)
        assert r.status_code in (500, 503)
    finally:
        httpx.post(f"{server}/admin/config", json={"error_rate": 0.0})
    r = httpx.post(f"{server}/v1/chat/completions", json=body(), timeout=10)
    assert r.status_code == 200
    ledger = httpx.get(f"{server}/ledger").json()
    assert ledger["calls"] == 2 and ledger["by_status"]["200"] == 1
