"""HTTP API behaviour, using FastAPI's TestClient and the in-process fakes."""

import pytest
from fastapi.testclient import TestClient

from app import server
from app.config import LLM_MAX_ATTEMPTS, RECURSION_LIMIT

from .fakes import TICKETS

RECORD_KEYS = {"run_id", "ticket_id", "customer_id", "status", "created_at", "finished_at", "error", "result", "steps"}


@pytest.fixture
def client(fake_llm, fake_commerce, monkeypatch):
    monkeypatch.setattr(server, "RUNS", {})
    with TestClient(server.app) as c:
        yield c


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_small_refund_run_completes(client, fake_commerce):
    resp = client.post("/runs", json=TICKETS["small"])

    assert resp.status_code == 200
    run = resp.json()
    assert set(run) == RECORD_KEYS
    assert run["status"] == "completed"
    assert run["ticket_id"] == "T-O-SMALL"
    assert run["customer_id"] == "cust-02"
    assert run["error"] is None
    assert run["finished_at"] >= run["created_at"]
    # classify, lookup_order, assess, issue_refund, draft_reply, send_email
    assert run["steps"] == 6
    assert run["result"]["outcome"] == "refunded"
    assert run["result"]["refund_id"] == fake_commerce.refunds[0]["refund_id"]
    assert run["result"]["email_id"] == fake_commerce.emails[0]["email_id"]

    assert client.get(f"/runs/{run['run_id']}").json() == run


def test_not_refund_run_completes(client, fake_commerce):
    run = client.post("/runs", json=TICKETS["question"]).json()

    assert run["status"] == "completed"
    assert run["steps"] == 3
    assert run["result"]["outcome"] == "not_refund"
    assert run["result"]["amount"] is None
    assert fake_commerce.refunds == []
    assert len(fake_commerce.emails) == 1


def test_large_refund_waits_for_approval_then_completes(client, fake_commerce):
    run = client.post("/runs", json=TICKETS["large"]).json()

    assert run["status"] == "awaiting_approval"
    assert run["finished_at"] is None
    assert run["result"] == {"approval_request": {
        "type": "approval", "ticket_id": "T-O-LARGE", "order_id": "O-LARGE", "amount": 1250.0}}
    waiting = client.get("/runs", params={"status": "awaiting_approval"}).json()["runs"]
    assert [r["run_id"] for r in waiting] == [run["run_id"]]
    assert fake_commerce.refunds == []

    resp = client.post(f"/runs/{run['run_id']}/approve", json={"approved": True, "approver": "alice"})

    assert resp.status_code == 200
    done = resp.json()
    assert done["status"] == "completed"
    assert done["result"]["outcome"] == "refunded"
    assert done["result"]["approval"] == {"approved": True, "approver": "alice"}
    assert done["finished_at"] is not None
    assert len(fake_commerce.refunds) == 1
    assert client.get("/runs", params={"status": "awaiting_approval"}).json() == {"runs": []}

    # A second decision does not change anything.
    again = client.post(f"/runs/{run['run_id']}/approve", json={"approved": False, "approver": "bob"})
    assert again.status_code == 200
    assert again.json()["result"]["approval"] == {"approved": True, "approver": "alice"}
    assert len(fake_commerce.refunds) == 1


def test_large_refund_rejected(client, fake_commerce):
    run = client.post("/runs", json=TICKETS["large"]).json()
    done = client.post(f"/runs/{run['run_id']}/approve", json={"approved": False, "approver": "bob"}).json()

    assert done["status"] == "completed"
    assert done["result"]["outcome"] == "declined"
    assert fake_commerce.refunds == []
    assert len(fake_commerce.emails) == 1


def test_partially_shipped_run_fails_at_recursion_limit(client, fake_llm, fake_commerce):
    run = client.post("/runs", json=TICKETS["partial"]).json()

    assert run["status"] == "failed"
    assert "Recursion limit of 200" in run["error"]
    assert run["steps"] == RECURSION_LIMIT
    assert run["finished_at"] is not None
    assert fake_llm.tasks().count("assess") > 90
    assert fake_commerce.refunds == []
    assert fake_commerce.emails == []


def test_llm_outage_fails_the_run(client, fake_llm):
    fake_llm.fail_next = [503] * LLM_MAX_ATTEMPTS
    run = client.post("/runs", json=TICKETS["small"]).json()

    assert run["status"] == "failed"
    assert "failed after 5 attempts" in run["error"]
    assert fake_llm.tasks() == ["classify"] * LLM_MAX_ATTEMPTS


def test_list_runs_filters(client):
    small = client.post("/runs", json=TICKETS["small"]).json()
    large = client.post("/runs", json=TICKETS["large"]).json()
    second_small = client.post("/runs", json=TICKETS["small"]).json()

    by_ticket = client.get("/runs", params={"ticket_id": "T-O-SMALL"}).json()["runs"]
    assert [r["run_id"] for r in by_ticket] == [small["run_id"], second_small["run_id"]]

    completed = client.get("/runs", params={"status": "completed"}).json()["runs"]
    assert {r["run_id"] for r in completed} == {small["run_id"], second_small["run_id"]}

    both = client.get("/runs", params={"ticket_id": "T-O-LARGE", "status": "awaiting_approval"}).json()["runs"]
    assert [r["run_id"] for r in both] == [large["run_id"]]

    assert len(client.get("/runs").json()["runs"]) == 3
    assert client.get("/runs", params={"ticket_id": "T-nope"}).json() == {"runs": []}


def test_unknown_run_is_404(client):
    assert client.get("/runs/does-not-exist").status_code == 404
    resp = client.post("/runs/does-not-exist/approve", json={"approved": True, "approver": "alice"})
    assert resp.status_code == 404


def test_approve_while_run_is_in_progress_is_409(client):
    run = client.post("/runs", json=TICKETS["large"]).json()
    server.RUNS[run["run_id"]]["status"] = "running"

    resp = client.post(f"/runs/{run['run_id']}/approve", json={"approved": True, "approver": "alice"})
    assert resp.status_code == 409


def test_bad_request_body_is_422(client):
    resp = client.post("/runs", json={"ticket_id": "T-1"})
    assert resp.status_code == 422
