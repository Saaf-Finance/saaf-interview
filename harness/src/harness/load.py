"""Open-loop load generator: submits each ticket to POST /runs at its scheduled time.

Every submit uses `Idempotency-Key: <ticket_id>`. Connection errors, timeouts, 429 and 5xx are retried up to 5 attempts
with 1, 2, 4, 8 s backoff. Every attempt is recorded; the first attempt's timestamp is the ticket's start time.

Partner integrations deliver at least once, so a seeded sample of tickets (`duplicate_rate`) is delivered a second
time, 0.5-5 s after the first delivery finishes, with the same Idempotency-Key. The second delivery is retried the same
way, and its attempts are recorded separately in `duplicate_delivery`.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field

import httpx

from harness.profiles import Arrival

MAX_ATTEMPTS = 5
BACKOFF_S = (1, 2, 4, 8)
SUBMIT_TIMEOUT_S = 30.0


@dataclass
class SubmitResult:
    ticket_id: str
    customer_id: str
    phase: str
    scheduled_at: float  # seconds after load start
    attempts: list[dict] = field(default_factory=list)
    accepted: bool = False
    run_id: str | None = None
    duplicate_delivery: list[dict] | None = None  # attempts of the second delivery; None if delivered once

    @property
    def first_submit_ts(self) -> float | None:
        return self.attempts[0]["ts"] if self.attempts else None

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "customer_id": self.customer_id,
            "phase": self.phase,
            "scheduled_at": self.scheduled_at,
            "first_submit_ts": self.first_submit_ts,
            "accepted": self.accepted,
            "run_id": self.run_id,
            "attempts": self.attempts,
            "duplicate_delivery": self.duplicate_delivery,
        }


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


class LoadRunner:
    """Runs a schedule against the service; `results` fills in as submits finish."""

    def __init__(self, sut_url: str, *, timeout_s: float = SUBMIT_TIMEOUT_S, max_attempts: int = MAX_ATTEMPTS,
                 backoff_s: tuple[float, ...] = BACKOFF_S, duplicate_rate: float = 0.0, seed: int = 0):
        self.sut_url = sut_url.rstrip("/")
        self.duplicate_rate = duplicate_rate
        self.seed = seed
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.results: dict[str, SubmitResult] = {}
        self.started = 0
        self.finished = 0

    async def run(self, schedule: list[Arrival]) -> dict[str, SubmitResult]:
        limits = httpx.Limits(max_connections=2000, max_keepalive_connections=200)
        async with httpx.AsyncClient(timeout=self.timeout_s, limits=limits, trust_env=False) as client:
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            tasks = [asyncio.create_task(self._arrive(client, loop, t0, a)) for a in schedule]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
        return self.results

    async def _arrive(self, client: httpx.AsyncClient, loop: asyncio.AbstractEventLoop, t0: float,
                      arrival: Arrival) -> None:
        await asyncio.sleep(max(0.0, t0 + arrival.at_s - loop.time()))
        ticket = arrival.ticket
        result = SubmitResult(ticket["ticket_id"], ticket["customer_id"], arrival.phase, arrival.at_s)
        self.results[result.ticket_id] = result
        self.started += 1
        try:
            await self._submit(client, ticket, result)
            delay = self._duplicate_delay(result.ticket_id)
            if delay is not None:
                await asyncio.sleep(delay)
                await self._deliver_again(client, ticket, result)
        finally:
            self.finished += 1

    def _duplicate_delay(self, ticket_id: str) -> float | None:
        """Seconds to wait before delivering this ticket again, or None if it's delivered once. Deterministic per seed."""
        if self.duplicate_rate <= 0:
            return None
        digest = hashlib.sha256(f"{self.seed}:{ticket_id}:dup".encode()).digest()
        if int.from_bytes(digest[:4], "big") / 2**32 >= self.duplicate_rate:
            return None
        return 0.5 + 4.5 * int.from_bytes(digest[4:8], "big") / 2**32

    async def _deliver_again(self, client: httpx.AsyncClient, ticket: dict, result: SubmitResult) -> None:
        """One more delivery with the same Idempotency-Key and the same retry policy as the first."""
        result.duplicate_delivery = []
        await self._post(client, ticket, result, result.duplicate_delivery)

    async def _submit(self, client: httpx.AsyncClient, ticket: dict, result: SubmitResult) -> None:
        await self._post(client, ticket, result, result.attempts)

    async def _post(self, client: httpx.AsyncClient, ticket: dict, result: SubmitResult, attempts: list[dict]) -> None:
        """POST /runs until accepted or out of attempts, appending every attempt to `attempts`."""
        body = {k: ticket[k] for k in ("ticket_id", "customer_id", "order_id", "email", "message")}
        headers = {"Idempotency-Key": ticket["ticket_id"]}
        for attempt in range(1, self.max_attempts + 1):
            record: dict = {"attempt": attempt, "ts": time.time(), "status": None, "error": None}
            start = time.monotonic()
            retry = False
            try:
                resp = await client.post(f"{self.sut_url}/runs", json=body, headers=headers)
                record["status"] = resp.status_code
                if 200 <= resp.status_code < 300:
                    if not result.accepted:
                        result.accepted = True
                        result.run_id = _run_id(resp)
                else:
                    retry = _retryable(resp.status_code)
            except httpx.TransportError as exc:  # connect errors, timeouts, dropped connections
                record["error"] = f"{type(exc).__name__}: {exc}"[:200]
                retry = True
            except httpx.HTTPError as exc:  # e.g. an undecodable response body: not worth retrying
                record["error"] = f"{type(exc).__name__}: {exc}"[:200]
            record["latency_s"] = round(time.monotonic() - start, 4)
            attempts.append(record)
            if not retry or attempt == self.max_attempts:
                return
            await asyncio.sleep(self.backoff_s[min(attempt - 1, len(self.backoff_s) - 1)])


def _run_id(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    run_id = body.get("run_id") if isinstance(body, dict) else None
    return str(run_id) if run_id is not None else None
