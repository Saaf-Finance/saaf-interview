"""POST /refunds: validation, Idempotency-Key handling, failures and slow responses."""

import asyncio

import pytest

from helpers import running_app

pytestmark = pytest.mark.anyio

REFUND = {"ticket_id": "T-00001", "order_id": "O-00001", "amount": 42.5}


async def ledger(client):
    return (await client.get("/ledger")).json()


async def test_refund_without_key_is_issued_and_recorded():
    async with running_app() as (_, client):
        resp = await client.post("/refunds", json=REFUND)
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "issued"
        assert body["refund_id"].startswith("rf_")
        assert "idempotent-replayed" not in resp.headers

        data = await ledger(client)
        assert len(data["refunds"]) == 1
        refund = data["refunds"][0]
        assert set(refund) == {"refund_id", "ts", "ticket_id", "order_id", "amount", "idempotency_key"}
        assert refund["refund_id"] == body["refund_id"]
        assert refund["ticket_id"] == "T-00001"
        assert refund["order_id"] == "O-00001"
        assert refund["amount"] == 42.5
        assert refund["idempotency_key"] is None
        assert isinstance(refund["ts"], float)


async def test_requests_without_key_are_never_deduplicated():
    async with running_app() as (_, client):
        first = await client.post("/refunds", json=REFUND)
        second = await client.post("/refunds", json=REFUND)
        assert first.status_code == second.status_code == 201
        assert first.json()["refund_id"] != second.json()["refund_id"]
        data = await ledger(client)
        assert data["summary"]["refunds"] == 2
        assert data["summary"]["tickets_refunded"] == 1


async def test_same_key_replays_stored_response():
    async with running_app() as (_, client):
        headers = {"Idempotency-Key": "T-00001"}
        first = await client.post("/refunds", json=REFUND, headers=headers)
        second = await client.post("/refunds", json=REFUND, headers=headers)
        third = await client.post("/refunds", json=REFUND, headers=headers)

        assert first.status_code == 201
        assert "idempotent-replayed" not in first.headers
        for replay in (second, third):
            assert replay.status_code == 201
            assert replay.headers["idempotent-replayed"] == "true"
            assert replay.json() == first.json()

        data = await ledger(client)
        assert len(data["refunds"]) == 1
        assert data["refunds"][0]["idempotency_key"] == "T-00001"
        assert data["refund_replays"] == 2


async def test_replay_ignores_the_body_of_later_requests():
    async with running_app() as (_, client):
        headers = {"Idempotency-Key": "k-1"}
        first = await client.post("/refunds", json=REFUND, headers=headers)
        other = {"ticket_id": "T-00002", "order_id": "O-00002", "amount": 1250.0}
        replay = await client.post("/refunds", json=other, headers=headers)
        assert replay.headers["idempotent-replayed"] == "true"
        assert replay.json() == first.json()
        assert len((await ledger(client))["refunds"]) == 1


async def test_different_keys_issue_separate_refunds():
    async with running_app() as (_, client):
        a = await client.post("/refunds", json=REFUND, headers={"Idempotency-Key": "a"})
        b = await client.post("/refunds", json=REFUND, headers={"Idempotency-Key": "b"})
        assert a.status_code == b.status_code == 201
        assert a.json()["refund_id"] != b.json()["refund_id"]
        assert len((await ledger(client))["refunds"]) == 2


async def test_blank_key_is_treated_as_no_key():
    async with running_app() as (_, client):
        await client.post("/refunds", json=REFUND, headers={"Idempotency-Key": "  "})
        resp = await client.post("/refunds", json=REFUND, headers={"Idempotency-Key": "  "})
        assert "idempotent-replayed" not in resp.headers
        assert len((await ledger(client))["refunds"]) == 2


async def test_wrong_amount_is_rejected_with_422_and_not_recorded():
    async with running_app() as (_, client):
        resp = await client.post("/refunds", json={**REFUND, "amount": 40.0})
        assert resp.status_code == 422
        assert resp.json() == {"error": "amount must equal the order amount", "order_amount": 42.5}
        data = await ledger(client)
        assert data["refunds"] == []
        assert data["summary"]["refunds"] == 0


async def test_amount_is_compared_to_the_cent():
    async with running_app() as (_, client):
        assert (await client.post("/refunds", json={**REFUND, "amount": 42.50})).status_code == 201
        assert (await client.post("/refunds", json={**REFUND, "amount": 42.51})).status_code == 422
        assert (await client.post("/refunds", json={**REFUND, "amount": "42.5"})).status_code == 201


async def test_unknown_order_is_404_and_not_recorded():
    async with running_app() as (_, client):
        resp = await client.post("/refunds", json={**REFUND, "order_id": "O-99999"})
        assert resp.status_code == 404
        assert resp.json() == {"error": "order not found"}
        assert (await ledger(client))["refunds"] == []


async def test_rejected_request_with_key_is_replayed_too():
    async with running_app() as (_, client):
        headers = {"Idempotency-Key": "bad-amount"}
        first = await client.post("/refunds", json={**REFUND, "amount": 1.0}, headers=headers)
        again = await client.post("/refunds", json=REFUND, headers=headers)
        assert first.status_code == again.status_code == 422
        assert again.headers["idempotent-replayed"] == "true"
        assert (await ledger(client))["refunds"] == []


async def test_invalid_body_is_422_with_error_field():
    async with running_app() as (_, client):
        resp = await client.post("/refunds", json={"order_id": "O-00001", "amount": 42.5})
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "invalid request body"
        assert any(err["loc"][-1] == "ticket_id" for err in body["detail"])


async def test_503_records_nothing_and_does_not_consume_the_key():
    async with running_app(refund_error_rate=1.0) as (app, client):
        headers = {"Idempotency-Key": "T-00001"}
        for _ in range(3):
            resp = await client.post("/refunds", json=REFUND, headers=headers)
            assert resp.status_code == 503
            assert resp.json() == {"error": "temporarily unavailable"}
        data = await ledger(client)
        assert data["refunds"] == []
        assert data["refund_replays"] == 0

        app.state.settings.refund_error_rate = 0.0
        resp = await client.post("/refunds", json=REFUND, headers=headers)
        assert resp.status_code == 201
        assert "idempotent-replayed" not in resp.headers
        assert len((await ledger(client))["refunds"]) == 1


async def test_503_can_hit_a_retry_of_a_completed_key():
    async with running_app() as (app, client):
        headers = {"Idempotency-Key": "T-00001"}
        assert (await client.post("/refunds", json=REFUND, headers=headers)).status_code == 201
        app.state.settings.refund_error_rate = 1.0
        assert (await client.post("/refunds", json=REFUND, headers=headers)).status_code == 503
        data = await ledger(client)
        assert len(data["refunds"]) == 1
        assert data["refund_replays"] == 0


async def test_concurrent_requests_with_same_key_get_409():
    async with running_app(refund_slow_rate=1.0, refund_slow_s=0.5) as (_, client):
        headers = {"Idempotency-Key": "T-00001"}
        responses = await asyncio.gather(
            client.post("/refunds", json=REFUND, headers=headers),
            client.post("/refunds", json=REFUND, headers=headers),
            client.post("/refunds", json=REFUND, headers=headers),
        )
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [201, 409, 409]
        conflict = next(r for r in responses if r.status_code == 409)
        assert conflict.json() == {"error": "a request with this idempotency key is in progress"}
        winner = next(r for r in responses if r.status_code == 201)

        # Once the first request has finished, the key replays its response.
        replay = await client.post("/refunds", json=REFUND, headers=headers)
        assert replay.status_code == 201
        assert replay.headers["idempotent-replayed"] == "true"
        assert replay.json() == winner.json()

        data = await ledger(client)
        assert len(data["refunds"]) == 1
        assert data["refund_replays"] == 1


async def test_concurrent_requests_with_same_key_and_fast_path_refund_once():
    async with running_app() as (_, client):
        headers = {"Idempotency-Key": "T-00001"}
        responses = await asyncio.gather(
            *(client.post("/refunds", json=REFUND, headers=headers) for _ in range(10))
        )
        assert all(r.status_code in (201, 409) for r in responses)
        assert len({r.json()["refund_id"] for r in responses if r.status_code == 201}) == 1
        assert len((await ledger(client))["refunds"]) == 1


async def test_slow_refund_is_recorded_before_the_response_is_sent():
    async with running_app(refund_slow_rate=1.0, refund_slow_s=0.5) as (_, client):
        pending = asyncio.create_task(client.post("/refunds", json=REFUND))
        await asyncio.sleep(0.15)
        assert not pending.done()
        data = await ledger(client)
        assert len(data["refunds"]) == 1

        resp = await pending
        assert resp.status_code == 201
        assert resp.json()["refund_id"] == data["refunds"][0]["refund_id"]


async def test_short_timeout_retries_without_key_issue_two_refunds():
    async with running_app(refund_slow_rate=1.0, refund_slow_s=0.5) as (_, client):
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(client.post("/refunds", json=REFUND), timeout=0.1)
        data = await ledger(client)
        assert data["summary"]["refunds"] == 2
        assert data["summary"]["tickets_refunded"] == 1
        assert len({r["refund_id"] for r in data["refunds"]}) == 2


async def test_abandoned_slow_request_still_stores_its_result_for_the_key():
    async with running_app(refund_slow_rate=1.0, refund_slow_s=0.5) as (app, client):
        headers = {"Idempotency-Key": "T-00001"}
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.post("/refunds", json=REFUND, headers=headers), timeout=0.1)
        issued = (await ledger(client))["refunds"][0]["refund_id"]

        app.state.settings.refund_slow_rate = 0.0
        replay = await client.post("/refunds", json=REFUND, headers=headers)
        assert replay.status_code == 201
        assert replay.headers["idempotent-replayed"] == "true"
        assert replay.json()["refund_id"] == issued
        assert len((await ledger(client))["refunds"]) == 1
