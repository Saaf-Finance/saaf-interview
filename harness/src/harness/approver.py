"""The harness plays the manager: it approves or rejects runs that wait for approval.

Every second it lists GET /runs?status=awaiting_approval. A run first seen at time t gets its decision at
t + approval_delay_s (from the workload): POST /runs/{run_id}/approve {"approved": ..., "approver": "harness"}.
Errors are tolerated and retried on the next tick, since the service may be restarting. Records without a non-empty
`run_id` (a string or an integer) and a non-empty string `ticket_id` are skipped.

For each ticket the approver records `approved_ts`: the send time of the first approve request (with approved=true)
that may have reached the service, i.e. any attempt except a refused connection or a 4xx other than 409. Refunds are
checked against this time.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import httpx

POLL_INTERVAL_S = 1.0
DEFAULT_DELAY_S = 3.0
APPROVER_NAME = "harness"
_NOT_DELIVERED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class Approver:
    def __init__(self, sut_url: str, expected: dict, *, poll_interval_s: float = POLL_INTERVAL_S,
                 request_timeout_s: float = 30.0):
        self.sut_url = sut_url.rstrip("/")
        self.expected = expected
        self.poll_interval_s = poll_interval_s
        self.request_timeout_s = request_timeout_s
        self.runs: dict[str, dict] = {}  # run_id -> state
        self.poll_errors = 0
        self.polls = 0
        self._in_flight: dict[str, asyncio.Task] = {}

    @property
    def pending(self) -> int:
        return sum(1 for r in self.runs.values() if not r["done"])

    async def run(self, stop: asyncio.Event) -> None:
        async with httpx.AsyncClient(timeout=self.request_timeout_s, trust_env=False) as client:
            try:
                while not stop.is_set():
                    await self._poll(client)
                    self._send_due(client)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_s)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if self._in_flight:
                    await asyncio.wait(list(self._in_flight.values()), timeout=5.0)
                for task in self._in_flight.values():
                    task.cancel()

    async def _poll(self, client: httpx.AsyncClient) -> None:
        self.polls += 1
        try:
            resp = await client.get(f"{self.sut_url}/runs", params={"status": "awaiting_approval"}, timeout=5.0)
            body = resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            body = None
        records = body.get("runs") if isinstance(body, dict) else None
        if not isinstance(records, list):
            self.poll_errors += 1
            return
        now = time.time()
        for record in records:
            if not isinstance(record, dict):
                continue
            run_id, ticket_id = record.get("run_id"), record.get("ticket_id")
            if isinstance(run_id, bool) or not isinstance(run_id, (str, int)):
                continue  # malformed record: skip it, the rest of the list is still usable
            run_id = str(run_id)
            if not (run_id.strip() and isinstance(ticket_id, str) and ticket_id):
                continue
            if isinstance(record.get("status"), str) and record["status"] != "awaiting_approval":
                continue
            if run_id in self.runs:
                continue
            exp = self.expected.get(ticket_id) or {}
            delay = exp.get("approval_delay_s")
            delay = float(delay) if isinstance(delay, (int, float)) else DEFAULT_DELAY_S
            self.runs[run_id] = {
                "run_id": run_id,
                "ticket_id": ticket_id,
                "decision": exp.get("approval") == "approve",
                "first_seen_ts": now,
                "due_ts": now + delay,
                "done": False,
                "confirmed": False,
                "delivered_ts": None,
                "attempts": [],
            }

    def _send_due(self, client: httpx.AsyncClient) -> None:
        now = time.time()
        for run_id, state in self.runs.items():
            if state["done"] or now < state["due_ts"] or run_id in self._in_flight:
                continue
            task = asyncio.create_task(self._approve(client, state))
            self._in_flight[run_id] = task
            task.add_done_callback(lambda _t, rid=run_id: self._in_flight.pop(rid, None))

    async def _approve(self, client: httpx.AsyncClient, state: dict) -> None:
        attempt = {"ts": time.time(), "approved": state["decision"], "status": None, "error": None}
        state["attempts"].append(attempt)
        maybe_delivered = True
        try:
            resp = await client.post(f"{self.sut_url}/runs/{quote(state['run_id'], safe='')}/approve",
                                     json={"approved": state["decision"], "approver": APPROVER_NAME})
            attempt["status"] = resp.status_code
            if 200 <= resp.status_code < 300 or resp.status_code == 409:
                state["done"] = state["confirmed"] = True
            elif 400 <= resp.status_code < 500:
                maybe_delivered = False
                if resp.status_code != 429:
                    state["done"] = True  # unknown run or rejected request: retrying won't help
        except asyncio.CancelledError:
            attempt["error"] = "cancelled when the bench stopped"
            raise
        except httpx.HTTPError as exc:
            attempt["error"] = f"{type(exc).__name__}: {exc}"[:200]
            maybe_delivered = not isinstance(exc, _NOT_DELIVERED)
        finally:
            if maybe_delivered and state["delivered_ts"] is None:
                state["delivered_ts"] = attempt["ts"]

    def per_ticket(self) -> dict[str, dict]:
        """ticket_id -> decision, approved_ts and the runs the harness saw waiting."""
        out: dict[str, dict] = {}
        for state in sorted(self.runs.values(), key=lambda s: s["first_seen_ts"]):
            ticket_id = state["ticket_id"]
            if ticket_id is None:
                continue
            entry = out.setdefault(ticket_id, {
                "decision": state["decision"],
                "approved_ts": None,
                "first_seen_ts": state["first_seen_ts"],
                "confirmed": False,
                "runs": [],
            })
            entry["runs"].append({k: state[k] for k in ("run_id", "first_seen_ts", "due_ts", "confirmed",
                                                          "delivered_ts", "attempts")})
            entry["confirmed"] = entry["confirmed"] or state["confirmed"]
            if state["decision"] and state["delivered_ts"] is not None:
                if entry["approved_ts"] is None or state["delivered_ts"] < entry["approved_ts"]:
                    entry["approved_ts"] = state["delivered_ts"]
        return out
