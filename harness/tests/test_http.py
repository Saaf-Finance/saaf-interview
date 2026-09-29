"""Load, approver and observe() against small HTTP fakes: retries, errors and malformed responses."""

import asyncio
import socket
import threading
import time

import fakes
from harness.approver import Approver
from harness.load import LoadRunner
from harness.profiles import Arrival
from harness.verify import compute, observe


def closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class Scripted:
    """Answers POST /runs with a scripted list of statuses, then 202."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []
        self.lock = threading.Lock()

    def handle(self, method, path, query, headers, body):
        with self.lock:
            self.calls.append((time.time(), headers.get("Idempotency-Key"), body))
            status = self.statuses.pop(0) if self.statuses else 202
        if status == "sleep":
            time.sleep(1.0)
            status = 202
        return status, {"run_id": "run-1", "status": "queued"} if status < 300 else {"error": "x"}


def ticket(tid="T-00001"):
    return {"ticket_id": tid, "customer_id": "cust-01", "order_id": "O-00001", "email": "cust-01@example.com",
            "message": "refund please"}


def test_load_retries_then_accepts():
    app = Scripted([503, 429, 202])
    server = fakes.Server(app).start()
    try:
        runner = LoadRunner(server.url, backoff_s=(0.01, 0.02, 0.04, 0.08))
        results = asyncio.run(runner.run([Arrival(0.0, "steady", ticket())]))
    finally:
        server.stop()
    result = results["T-00001"]
    assert result.accepted and result.run_id == "run-1"
    assert [a["status"] for a in result.attempts] == [503, 429, 202]
    assert result.first_submit_ts == result.attempts[0]["ts"]
    assert all(key == "T-00001" for _, key, _ in app.calls)
    assert app.calls[0][2] == ticket()


def test_load_gives_up_after_five_attempts_and_does_not_retry_4xx():
    app = Scripted([500] * 10)
    server = fakes.Server(app).start()
    try:
        runner = LoadRunner(server.url, backoff_s=(0.01,))
        results = asyncio.run(runner.run([Arrival(0.0, "steady", ticket("T-00001"))]))
        app.statuses = [422]
        results.update(asyncio.run(LoadRunner(server.url, backoff_s=(0.01,)).run(
            [Arrival(0.0, "steady", ticket("T-00002"))])))
    finally:
        server.stop()
    assert len(results["T-00001"].attempts) == 5 and not results["T-00001"].accepted
    assert len(results["T-00002"].attempts) == 1 and not results["T-00002"].accepted


def test_load_records_connection_errors_and_timeouts():
    runner = LoadRunner(closed_port_url(), backoff_s=(0.01,))
    results = asyncio.run(runner.run([Arrival(0.0, "steady", ticket())]))
    attempts = results["T-00001"].attempts
    assert len(attempts) == 5 and all(a["status"] is None and "Connect" in a["error"] for a in attempts)

    app = Scripted(["sleep", 202])
    server = fakes.Server(app).start()
    try:
        runner = LoadRunner(server.url, timeout_s=0.2, backoff_s=(0.01,))
        results = asyncio.run(runner.run([Arrival(0.0, "steady", ticket())]))
    finally:
        server.stop()
    attempts = results["T-00001"].attempts
    assert "Timeout" in attempts[0]["error"] and attempts[1]["status"] == 202
    assert results["T-00001"].accepted


def test_load_is_open_loop():
    app = Scripted(["sleep", "sleep", "sleep"])
    server = fakes.Server(app).start()
    try:
        schedule = [Arrival(0.1 * i, "steady", ticket(f"T-0000{i + 1}")) for i in range(3)]
        start = time.time()
        asyncio.run(LoadRunner(server.url).run(schedule))
    finally:
        server.stop()
    sent = sorted(ts for ts, _, _ in app.calls)
    assert sent[-1] - start < 0.8  # the third submit did not wait for the slow first two


def test_approver_decides_after_delay(workload, services):
    expected = {
        "T-A": {"approval": "approve", "approval_delay_s": 0.3},
        "T-R": {"approval": "reject", "approval_delay_s": 0.3},
    }

    class Waiting:
        def __init__(self):
            self.decisions = {}
            self.lock = threading.Lock()

        def handle(self, method, path, query, headers, body):
            with self.lock:
                if method == "GET":
                    runs = [{"run_id": f"r-{t}", "ticket_id": t, "status": "awaiting_approval"}
                            for t in ("T-A", "T-R") if t not in self.decisions]
                    return 200, {"runs": runs}
                run_id = path.split("/")[2]
                if run_id in self.decisions:
                    return 409, {"error": "already decided"}
                self.decisions[run_id] = (time.time(), body)
                return 200, {"ok": True}

    app = Waiting()
    server = fakes.Server(app).start()

    async def go():
        approver = Approver(server.url, expected, poll_interval_s=0.05)
        stop = asyncio.Event()
        task = asyncio.create_task(approver.run(stop))
        await asyncio.sleep(1.0)
        stop.set()
        await task
        return approver

    try:
        approver = asyncio.run(go())
    finally:
        server.stop()
    per_ticket = approver.per_ticket()
    assert app.decisions["r-T-A"][1] == {"approved": True, "approver": "harness"}
    assert app.decisions["r-T-R"][1] == {"approved": False, "approver": "harness"}
    a = per_ticket["T-A"]
    assert a["decision"] is True and a["confirmed"]
    assert a["approved_ts"] - a["first_seen_ts"] >= 0.3
    assert per_ticket["T-R"]["approved_ts"] is None and per_ticket["T-R"]["decision"] is False
    assert len(app.decisions) == 2  # each run decided exactly once


def test_approver_tolerates_errors():
    class Flaky:
        def __init__(self):
            self.gets = 0
            self.posts = 0

        def handle(self, method, path, query, headers, body):
            if method == "GET":
                self.gets += 1
                if self.gets % 2:
                    return 200, b"not json"
                return 200, {"runs": [{"run_id": "r1", "ticket_id": "T1", "status": "awaiting_approval"}]}
            self.posts += 1
            return (503, {"error": "busy"}) if self.posts < 3 else (200, {"ok": True})

    app = Flaky()
    server = fakes.Server(app).start()

    async def go():
        approver = Approver(server.url, {"T1": {"approval": "approve", "approval_delay_s": 0.0}},
                            poll_interval_s=0.05)
        stop = asyncio.Event()
        task = asyncio.create_task(approver.run(stop))
        await asyncio.sleep(0.8)
        stop.set()
        await task
        return approver

    try:
        approver = asyncio.run(go())
    finally:
        server.stop()
    entry = approver.per_ticket()["T1"]
    assert entry["confirmed"] and app.posts == 3
    attempts = entry["runs"][0]["attempts"]
    assert [a["status"] for a in attempts] == [503, 503, 200]
    assert entry["approved_ts"] == attempts[0]["ts"]  # a 5xx may already have been acted on
    assert approver.poll_errors >= 1


def test_approver_with_service_down():
    async def go():
        approver = Approver(closed_port_url(), {}, poll_interval_s=0.05)
        stop = asyncio.Event()
        task = asyncio.create_task(approver.run(stop))
        await asyncio.sleep(0.3)
        stop.set()
        await task
        return approver

    approver = asyncio.run(go())
    assert approver.poll_errors >= 2 and approver.per_ticket() == {}


def test_observe_with_malformed_and_missing_data(workload, services):
    sut, llm, commerce, servers = services(faults={"malformed_runs": {"T-00002"}})
    sut_url, llm_url, commerce_url = (s.url for s in servers)
    tickets = ["T-00001", "T-00002", "T-00003"]
    for tid in tickets:
        sut.handle("POST", "/runs", {}, {"Idempotency-Key": tid}, workload["tickets"][int(tid[2:]) - 1])
    time.sleep(0.5)

    obs = asyncio.run(observe(sut_url, llm_url, commerce_url, tickets))
    assert obs["run_errors"] == {"T-00002": "response is not valid JSON"}
    assert len(obs["runs"]["T-00001"]) == 1 and obs["runs"]["T-00002"] == []
    assert obs["commerce"] is not None and obs["llm"] is not None

    commerce.mode = "malformed"
    llm.mode = "no_summary"
    obs = asyncio.run(observe(sut_url, llm_url, commerce_url, tickets))
    assert obs["commerce"] is None and "refunds" in obs["commerce_error"]
    assert obs["llm"] is not None and obs["llm_error"] is None  # fell back to the raw ledger
    assert obs["llm"]["per_ticket"]["T-00001"]["calls"] >= 1

    llm.mode = "error"
    obs = asyncio.run(observe(sut_url, llm_url, commerce_url, tickets))
    assert obs["llm"] is None and "HTTP 500" in obs["llm_error"]

    subs = [{"ticket_id": t, "customer_id": "cust-01", "first_submit_ts": 0.0, "accepted": True, "attempts": []}
            for t in tickets]
    metrics = compute(subs, workload, obs)
    assert metrics["hard"]["lost_runs"] == 1
    assert metrics["hard"]["duplicate_refunds"] is None and not metrics["passed"]


def test_observe_with_everything_down(workload):
    down = closed_port_url()
    obs = asyncio.run(observe(down, down, down, ["T-00001", "T-00002"]))
    assert set(obs["run_errors"]) == {"T-00001", "T-00002"}
    assert obs["commerce"] is None and obs["llm"] is None
    subs = [{"ticket_id": t, "customer_id": "cust-01", "first_submit_ts": 0.0, "accepted": False, "attempts": []}
            for t in ("T-00001", "T-00002")]
    metrics = compute(subs, workload, obs)
    assert metrics["hard"]["lost_runs"] == 2
    assert metrics["passed"] is False


def test_duplicate_delivery_is_seeded_and_uses_the_same_key():
    from harness.load import LoadRunner
    a = LoadRunner("http://x", duplicate_rate=0.05, seed=42)
    b = LoadRunner("http://x", duplicate_rate=0.05, seed=42)
    ids = [f"T-{i:05d}" for i in range(1, 1201)]
    picked_a = [t for t in ids if a._duplicate_delay(t) is not None]
    assert picked_a == [t for t in ids if b._duplicate_delay(t) is not None]
    assert 30 <= len(picked_a) <= 90  # ~5% of 1200
    assert all(0.5 <= a._duplicate_delay(t) <= 5.0 for t in picked_a)
    assert LoadRunner("http://x")._duplicate_delay("T-00001") is None  # off by default
