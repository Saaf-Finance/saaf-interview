"""Small in-process fakes of the three services, for exercising the harness without Docker.

- FakeLLM:      the LLM provider's ledger endpoints (/ledger, /ledger/raw, /admin/reset).
- FakeCommerce: the store backend's ledger endpoints (/ledger, /admin/reset) plus refund()/email() helpers.
- FakeSUT:      an agent service that follows the return policy, runs tickets on background threads and writes to
                the two fakes above. `faults` switches on specific misbehaviours per ticket.

Run all three from a shell to try the bench by hand:
    uv run --project harness python harness/tests/fakes.py
"""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

REFUND_WORDS = ("refund", "return", "money back", "damaged", "broken", "wrong item", "defective", "doesn't fit",
                "does not fit")


class _HTTPServer(ThreadingHTTPServer):
    request_queue_size = 256  # the default backlog of 5 refuses connections under concurrent load
    daemon_threads = True


class Server:
    """Serves `app.handle(method, path, query, headers, body) -> (status, payload)` on 127.0.0.1."""

    def __init__(self, app, port: int = 0):
        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                status, payload = app.handle(self.command, parsed.path, query, self.headers, body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.httpd = _HTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "Server":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeLLM:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries: list[dict] = []
        self.mode = "ok"  # "ok" | "malformed" | "no_summary" | "error"

    def record(self, ticket_id: str | None, status: int = 200, cost: float = 0.001) -> None:
        with self.lock:
            self.entries.append({"ts": time.time(), "ticket_id": ticket_id, "run_id": None, "task": "x",
                                 "status_code": status, "prompt_tokens": 100, "completion_tokens": 20,
                                 "cost_usd": cost if status == 200 else 0.0, "latency_s": 0.01, "stream": False,
                                 "dropped": False})

    def ledger(self) -> dict:
        with self.lock:
            entries = list(self.entries)
        by_status: dict[str, int] = {}
        per_ticket: dict[str, dict] = {}
        for e in entries:
            by_status[str(e["status_code"])] = by_status.get(str(e["status_code"]), 0) + 1
            t = per_ticket.setdefault(str(e["ticket_id"]), {"calls": 0, "ok_calls": 0, "cost_usd": 0.0, "tokens": 0})
            t["calls"] += 1
            t["ok_calls"] += e["status_code"] == 200
            t["cost_usd"] += e["cost_usd"]
            t["tokens"] += e["prompt_tokens"] + e["completion_tokens"]
        return {"calls": len(entries), "by_status": by_status, "prompt_tokens": 0, "completion_tokens": 0,
                "cost_usd": sum(e["cost_usd"] for e in entries), "dropped_streams": 0, "per_ticket": per_ticket}

    def handle(self, method, path, query, headers, body):
        if path == "/healthz":
            return 200, {"ok": True}
        if method == "POST" and path == "/admin/reset":
            with self.lock:
                self.entries.clear()
            return 200, {"ok": True}
        if self.mode == "error":
            return 500, {"error": "boom"}
        if self.mode == "malformed" or (self.mode == "no_summary" and path == "/ledger"):
            return 200, b"{not json"
        if path == "/ledger":
            return 200, self.ledger()
        if path == "/ledger/raw":
            with self.lock:
                return 200, {"entries": list(self.entries)}
        return 404, {"error": "not found"}


class FakeCommerce:
    def __init__(self):
        self.lock = threading.Lock()
        self.refunds: list[dict] = []
        self.emails: list[dict] = []
        self.replays = 0
        self.keys: dict[str, dict] = {}
        self.mode = "ok"  # "ok" | "malformed"

    def refund(self, ticket_id: str, order_id: str, amount: float, key: str | None = None) -> dict:
        with self.lock:
            if key and key in self.keys:
                self.replays += 1
                return self.keys[key]
            refund = {"refund_id": f"rf_{uuid.uuid4().hex}", "ts": time.time(), "ticket_id": ticket_id,
                      "order_id": order_id, "amount": amount, "idempotency_key": key}
            self.refunds.append(refund)
            if key:
                self.keys[key] = refund
            return refund

    def email(self, ticket_id: str, to: str, subject: str = "About your order") -> None:
        with self.lock:
            self.emails.append({"email_id": f"em_{uuid.uuid4().hex}", "ts": time.time(), "ticket_id": ticket_id,
                                "to": to, "subject": subject})

    def handle(self, method, path, query, headers, body):
        if path == "/healthz":
            return 200, {"ok": True}
        if method == "POST" and path == "/admin/reset":
            with self.lock:
                self.refunds.clear()
                self.emails.clear()
                self.keys.clear()
                self.replays = 0
            return 200, {"ok": True}
        if path == "/ledger":
            if self.mode == "malformed":
                return 200, {"refunds": "nope"}
            with self.lock:
                return 200, {"refunds": list(self.refunds), "refund_replays": self.replays,
                             "emails": list(self.emails),
                             "summary": {"refunds": len(self.refunds), "emails": len(self.emails)}}
        return 404, {"error": "not found"}


class FakeSUT:
    """An agent service that behaves correctly unless a fault is switched on.

    faults (all optional):
      duplicate_refund: set of ticket ids refunded twice
      duplicate_email:  set of ticket ids emailed twice
      lose:             set of ticket ids whose runs never finish
      skip_approval:    True to refund large amounts without asking for approval
      malformed_runs:   set of ticket ids for which GET /runs?ticket_id= returns invalid JSON
      duplicate_run:    set of ticket ids that get two runs (Idempotency-Key ignored)
    """

    def __init__(self, workload: dict, llm: FakeLLM, commerce: FakeCommerce, *, faults: dict | None = None,
                 work_s: tuple[float, float] = (0.02, 0.1), seed: int = 1):
        self.orders = workload["orders"]
        self.llm, self.commerce = llm, commerce
        self.faults = faults or {}
        self.work_s = work_s
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.runs: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.tickets: dict[str, dict] = {}
        self.down = False

    # -- helpers ---------------------------------------------------------------------------------------------------
    def _sleep(self) -> None:
        with self.lock:
            delay = self.rng.uniform(*self.work_s)
        time.sleep(delay)

    def _finish(self, run: dict, status: str = "completed", **result) -> None:
        with self.lock:
            run.update(status=status, finished_at=time.time(), result=result or None, steps=5)

    def _email(self, ticket: dict) -> None:
        self.commerce.email(ticket["ticket_id"], ticket["email"])
        if ticket["ticket_id"] in self.faults.get("duplicate_email", set()):
            self.commerce.email(ticket["ticket_id"], ticket["email"])

    def _refund(self, ticket: dict, order: dict) -> None:
        tid = ticket["ticket_id"]
        self.commerce.refund(tid, order["order_id"], order["amount"], key=f"refund-{tid}")
        if tid in self.faults.get("duplicate_refund", set()):
            self.commerce.refund(tid, order["order_id"], order["amount"], key=None)

    def _process(self, run: dict) -> None:
        ticket = self.tickets[run["run_id"]]
        tid = ticket["ticket_id"]
        with self.lock:
            run["status"] = "running"
        self._sleep()
        self.llm.record(tid)
        if tid in self.faults.get("lose", set()):
            return
        if not any(w in ticket["message"].lower() for w in REFUND_WORDS):
            self.llm.record(tid)
            self._email(ticket)
            return self._finish(run, outcome="not_refund")
        order = self.orders[ticket["order_id"]]
        self.llm.record(tid)
        if order["status"] == "partially_shipped" or order["days_since_delivery"] is None:
            return self._finish(run, outcome="escalated")
        if order["final_sale"] or order["days_since_delivery"] > 30:
            self.llm.record(tid)
            self._email(ticket)
            return self._finish(run, outcome="declined")
        if order["amount"] > 500 and not self.faults.get("skip_approval"):
            with self.lock:
                run["status"] = "awaiting_approval"
            return
        self._refund(ticket, order)
        self.llm.record(tid)
        self._email(ticket)
        self._finish(run, outcome="refunded")

    def _resume(self, run: dict, approved: bool) -> None:
        ticket = self.tickets[run["run_id"]]
        order = self.orders[ticket["order_id"]]
        self._sleep()
        if approved:
            self._refund(ticket, order)
        self.llm.record(ticket["ticket_id"])
        self._email(ticket)
        self._finish(run, outcome="refunded" if approved else "declined")

    # -- HTTP ------------------------------------------------------------------------------------------------------
    def handle(self, method, path, query, headers, body):
        if self.down:
            return 503, {"error": "down"}
        if path == "/healthz":
            return 200, {"ok": True}
        parts = [p for p in path.split("/") if p]
        if method == "POST" and parts == ["runs"]:
            return self._create(headers, body)
        if method == "GET" and parts == ["runs"]:
            tid = query.get("ticket_id")
            if tid and tid in self.faults.get("malformed_runs", set()):
                return 200, b"<html>oops</html>"
            with self.lock:
                runs = [dict(r) for r in self.runs.values()
                        if (not tid or r["ticket_id"] == tid)
                        and (not query.get("status") or r["status"] == query["status"])]
            return 200, {"runs": runs}
        if method == "GET" and len(parts) == 2 and parts[0] == "runs":
            with self.lock:
                run = self.runs.get(parts[1])
                return (200, dict(run)) if run else (404, {"error": "not found"})
        if method == "POST" and len(parts) == 3 and parts[0] == "runs" and parts[2] == "approve":
            return self._approve(parts[1], body or {})
        return 404, {"error": "not found"}

    def _create(self, headers, body):
        if not isinstance(body, dict) or "ticket_id" not in body:
            return 422, {"error": "bad body"}
        key = headers.get("Idempotency-Key")
        tid = body["ticket_id"]
        with self.lock:
            if key and key in self.by_key and tid not in self.faults.get("duplicate_run", set()):
                return 200, dict(self.runs[self.by_key[key]])
            run_id = uuid.uuid4().hex
            run = {"run_id": run_id, "ticket_id": tid, "customer_id": body.get("customer_id"), "status": "queued",
                   "created_at": time.time(), "finished_at": None, "error": None, "result": None, "steps": None}
            self.runs[run_id] = run
            self.tickets[run_id] = body
            if key:
                self.by_key.setdefault(key, run_id)
            snapshot = dict(run)
        threading.Thread(target=self._process, args=(run,), daemon=True).start()
        return 202, snapshot

    def _approve(self, run_id: str, body: dict):
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                return 404, {"error": "not found"}
            if run["status"] != "awaiting_approval" or run.get("decision") is not None:
                return 409, {"error": "already decided"}
            run["decision"] = bool(body.get("approved"))
            run["status"] = "running"
        threading.Thread(target=self._resume, args=(run, run["decision"]), daemon=True).start()
        return 200, {"run_id": run_id, "status": "running"}


def start_all(workload: dict, **sut_kwargs) -> tuple[FakeSUT, FakeLLM, FakeCommerce, list[Server]]:
    llm, commerce = FakeLLM(), FakeCommerce()
    sut = FakeSUT(workload, llm, commerce, **sut_kwargs)
    servers = [Server(sut).start(), Server(llm).start(), Server(commerce).start()]
    return sut, llm, commerce, servers


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from harness import workload as workload_mod

    wl = workload_mod.load(workload_mod.DEFAULT_WORKLOAD_PATH)
    llm_, commerce_ = FakeLLM(), FakeCommerce()
    sut_ = FakeSUT(wl, llm_, commerce_, work_s=(0.2, 1.5))
    servers_ = [Server(sut_, 18000).start(), Server(llm_, 18100).start(), Server(commerce_, 18200).start()]
    print("fake services: --sut http://127.0.0.1:18000 --llm http://127.0.0.1:18100 "
          "--commerce http://127.0.0.1:18200  (Ctrl-C to stop)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
