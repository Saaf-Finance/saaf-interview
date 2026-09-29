"""End-to-end checks against a real uvicorn process over HTTP.

These exercise behaviour that depends on a real network client: a request that
times out on the client side keeps running on the server.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from helpers import FIXTURE_WORKLOAD

SERVICE_DIR = Path(__file__).resolve().parents[1]
SLOW_S = 1.5
REFUND = {"ticket_id": "T-00001", "order_id": "O-00001", "amount": 42.5}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    port = free_port()
    env = {
        **os.environ,
        "WORKLOAD_PATH": str(FIXTURE_WORKLOAD),
        "COMMERCE_SEED": "11",
        "REFUND_ERROR_RATE": "0",
        "REFUND_SLOW_RATE": "1",
        "REFUND_SLOW_S": str(SLOW_S),
        "EMAIL_ERROR_RATE": "0",
    }
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=SERVICE_DIR,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 15
        while True:
            try:
                if httpx.get(f"{url}/healthz", timeout=0.5).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if proc.poll() is not None or time.time() > deadline:
                log.seek(0)
                raise RuntimeError(f"server did not start:\n{log.read().decode()}")
            time.sleep(0.1)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()


@pytest.fixture
def client(base_url):
    with httpx.Client(base_url=base_url, timeout=10) as c:
        c.post("/admin/reset").raise_for_status()
        yield c


def test_endpoints_over_http(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/orders/O-00002").json()["amount"] == 1250.0
    assert client.get("/orders/nope").status_code == 404
    email = client.post("/emails", json={"ticket_id": "T-1", "to": "a@example.com", "subject": "s", "body": "b"})
    assert email.status_code == 201
    assert client.get("/ledger").json()["summary"]["emails"] == 1


def test_client_timeout_then_retry_without_key_refunds_twice(client):
    for _ in range(2):
        with pytest.raises(httpx.ReadTimeout):
            client.post("/refunds", json=REFUND, timeout=0.3)
    data = client.get("/ledger").json()
    assert data["summary"]["refunds"] == 2
    assert data["summary"]["tickets_refunded"] == 1


def test_client_timeout_then_retry_with_key_refunds_once(client):
    headers = {"Idempotency-Key": "T-00001"}
    with pytest.raises(httpx.ReadTimeout):
        client.post("/refunds", json=REFUND, headers=headers, timeout=0.3)

    # The first request is still sleeping on the server: the key is in flight.
    busy = client.post("/refunds", json=REFUND, headers=headers)
    assert busy.status_code == 409
    assert busy.json() == {"error": "a request with this idempotency key is in progress"}

    time.sleep(SLOW_S)
    replay = client.post("/refunds", json=REFUND, headers=headers)
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replayed"] == "true"

    data = client.get("/ledger").json()
    assert data["summary"]["refunds"] == 1
    assert data["refunds"][0]["refund_id"] == replay.json()["refund_id"]
    assert data["refund_replays"] == 1


def test_concurrent_same_key_over_http(base_url, client):
    headers = {"Idempotency-Key": "T-00001"}

    def post(_):
        with httpx.Client(base_url=base_url, timeout=10) as c:
            return c.post("/refunds", json=REFUND, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = sorted(pool.map(post, range(4)))
    assert statuses == [201, 409, 409, 409]
    assert client.get("/ledger").json()["summary"]["refunds"] == 1
