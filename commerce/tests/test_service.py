"""Orders, emails, ledger, admin reset, configuration and startup."""

import time

import pytest

from app import Settings, load_orders
from helpers import FIXTURE_WORKLOAD, running_app

pytestmark = pytest.mark.anyio

REFUND = {"ticket_id": "T-00001", "order_id": "O-00001", "amount": 42.5}
EMAIL = {"ticket_id": "T-00001", "to": "cust-01@example.com", "subject": "Your refund", "body": "Hi there..."}


async def test_healthz():
    async with running_app() as (_, client):
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


async def test_get_order():
    async with running_app() as (_, client):
        resp = await client.get("/orders/O-00001")
        assert resp.status_code == 200
        assert resp.json() == {
            "order_id": "O-00001",
            "customer_id": "cust-01",
            "amount": 42.5,
            "currency": "USD",
            "days_since_delivery": 5,
            "final_sale": False,
            "status": "delivered",
        }


async def test_partially_shipped_order_has_null_delivery_days():
    async with running_app() as (_, client):
        order = (await client.get("/orders/O-00004")).json()
        assert order["status"] == "partially_shipped"
        assert order["days_since_delivery"] is None


async def test_order_fields_get_defaults_and_amount_is_rounded():
    async with running_app() as (_, client):
        order = (await client.get("/orders/O-00005")).json()
        assert order == {
            "order_id": "O-00005",
            "customer_id": "cust-02",
            "amount": 20.0,
            "currency": "USD",
            "days_since_delivery": 45,
            "final_sale": False,
            "status": "delivered",
        }


async def test_unknown_order_is_404():
    async with running_app() as (_, client):
        resp = await client.get("/orders/O-99999")
        assert resp.status_code == 404
        assert resp.json() == {"error": "order not found"}


async def test_order_lookup_latency_is_within_configured_range():
    async with running_app(order_latency_min_ms=40, order_latency_max_ms=60) as (_, client):
        for _ in range(3):
            start = time.perf_counter()
            await client.get("/orders/O-00001")
            elapsed = time.perf_counter() - start
            assert 0.035 <= elapsed < 0.5


async def test_email_is_sent_and_recorded_without_body():
    async with running_app() as (_, client):
        resp = await client.post("/emails", json=EMAIL)
        assert resp.status_code == 201
        email_id = resp.json()["email_id"]
        assert email_id.startswith("em_")

        emails = (await client.get("/ledger")).json()["emails"]
        assert len(emails) == 1
        assert set(emails[0]) == {"email_id", "ts", "ticket_id", "to", "subject"}
        assert emails[0]["email_id"] == email_id
        assert emails[0]["ticket_id"] == "T-00001"
        assert emails[0]["to"] == "cust-01@example.com"
        assert emails[0]["subject"] == "Your refund"


async def test_every_email_call_sends_a_new_email_even_with_a_key():
    async with running_app() as (_, client):
        headers = {"Idempotency-Key": "T-00001"}
        first = await client.post("/emails", json=EMAIL, headers=headers)
        second = await client.post("/emails", json=EMAIL, headers=headers)
        assert first.status_code == second.status_code == 201
        assert "idempotent-replayed" not in second.headers
        assert first.json()["email_id"] != second.json()["email_id"]
        assert (await client.get("/ledger")).json()["summary"]["emails"] == 2


async def test_email_503_records_nothing():
    async with running_app(email_error_rate=1.0) as (_, client):
        resp = await client.post("/emails", json=EMAIL)
        assert resp.status_code == 503
        assert resp.json() == {"error": "temporarily unavailable"}
        assert (await client.get("/ledger")).json()["emails"] == []


async def test_email_requires_ticket_id():
    async with running_app() as (_, client):
        body = {k: v for k, v in EMAIL.items() if k != "ticket_id"}
        resp = await client.post("/emails", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"] == "invalid request body"


async def test_ledger_contents_and_summary():
    async with running_app() as (_, client):
        await client.post("/refunds", json=REFUND, headers={"Idempotency-Key": "T-00001"})
        await client.post("/refunds", json=REFUND, headers={"Idempotency-Key": "T-00001"})
        await client.post("/refunds", json=REFUND)
        await client.post("/refunds", json={"ticket_id": "T-00002", "order_id": "O-00002", "amount": 1250.0})
        await client.post("/refunds", json={**REFUND, "amount": 1.0})
        await client.post("/emails", json=EMAIL)
        await client.post("/emails", json={**EMAIL, "ticket_id": "T-00002"})

        data = (await client.get("/ledger")).json()
        assert set(data) == {"refunds", "refund_replays", "emails", "summary"}
        assert [(r["ticket_id"], r["amount"], r["idempotency_key"]) for r in data["refunds"]] == [
            ("T-00001", 42.5, "T-00001"),
            ("T-00001", 42.5, None),
            ("T-00002", 1250.0, None),
        ]
        assert data["refund_replays"] == 1
        assert [e["ticket_id"] for e in data["emails"]] == ["T-00001", "T-00002"]
        assert data["summary"] == {"refunds": 3, "tickets_refunded": 2, "emails": 2}
        timestamps = [r["ts"] for r in data["refunds"]]
        assert timestamps == sorted(timestamps)


async def test_reset_clears_everything():
    async with running_app() as (_, client):
        headers = {"Idempotency-Key": "T-00001"}
        first = await client.post("/refunds", json=REFUND, headers=headers)
        await client.post("/refunds", json=REFUND, headers=headers)
        await client.post("/emails", json=EMAIL)

        resp = await client.post("/admin/reset")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        data = (await client.get("/ledger")).json()
        assert data == {
            "refunds": [],
            "refund_replays": 0,
            "emails": [],
            "summary": {"refunds": 0, "tickets_refunded": 0, "emails": 0},
        }

        # The idempotency store was cleared too: the same key refunds again.
        again = await client.post("/refunds", json=REFUND, headers=headers)
        assert again.status_code == 201
        assert "idempotent-replayed" not in again.headers
        assert again.json()["refund_id"] != first.json()["refund_id"]


async def status_sequence(client, n=40):
    return [(await client.post("/refunds", json=REFUND)).status_code for _ in range(n)]


async def test_failures_are_deterministic_for_a_seed_and_restart_on_reset():
    async with running_app(refund_error_rate=0.5, seed=5) as (_, client):
        first = await status_sequence(client)
        await client.post("/admin/reset")
        after_reset = await status_sequence(client)
    async with running_app(refund_error_rate=0.5, seed=5) as (_, client):
        fresh = await status_sequence(client)
    async with running_app(refund_error_rate=0.5, seed=6) as (_, client):
        other_seed = await status_sequence(client)

    assert set(first) == {201, 503}
    assert first == after_reset == fresh
    assert first != other_seed


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("WORKLOAD_PATH", "/tmp/w.json")
    monkeypatch.setenv("COMMERCE_SEED", "3")
    monkeypatch.setenv("REFUND_ERROR_RATE", "0.2")
    monkeypatch.setenv("REFUND_SLOW_RATE", "0.4")
    monkeypatch.setenv("REFUND_SLOW_S", "1.5")
    monkeypatch.setenv("EMAIL_ERROR_RATE", "0.1")
    monkeypatch.setenv("ORDER_LATENCY_MIN_MS", "5")
    monkeypatch.setenv("ORDER_LATENCY_MAX_MS", "9")
    assert Settings.from_env() == Settings(
        workload_path="/tmp/w.json",
        seed=3,
        refund_error_rate=0.2,
        refund_slow_rate=0.4,
        refund_slow_s=1.5,
        email_error_rate=0.1,
        order_latency_min_ms=5.0,
        order_latency_max_ms=9.0,
    )


def test_settings_defaults(monkeypatch):
    for name in (
        "WORKLOAD_PATH", "COMMERCE_SEED", "REFUND_ERROR_RATE", "REFUND_SLOW_RATE", "REFUND_SLOW_S",
        "EMAIL_ERROR_RATE", "ORDER_LATENCY_MIN_MS", "ORDER_LATENCY_MAX_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    s = Settings.from_env()
    assert (s.workload_path, s.seed) == ("/data/workload.json", 11)
    assert (s.refund_error_rate, s.refund_slow_rate, s.refund_slow_s) == (0.01, 0.03, 3.0)
    assert (s.email_error_rate, s.order_latency_min_ms, s.order_latency_max_ms) == (0.01, 20.0, 80.0)


def test_load_orders_reads_the_orders_object():
    orders = load_orders(str(FIXTURE_WORKLOAD))
    assert sorted(orders) == ["O-00001", "O-00002", "O-00003", "O-00004", "O-00005"]


def test_load_orders_fails_clearly_when_file_is_missing(tmp_path):
    with pytest.raises(RuntimeError, match="WORKLOAD_PATH"):
        load_orders(str(tmp_path / "missing.json"))


def test_load_orders_fails_clearly_without_orders(tmp_path):
    path = tmp_path / "workload.json"
    path.write_text('{"tickets": []}')
    with pytest.raises(RuntimeError, match="orders"):
        load_orders(str(path))
