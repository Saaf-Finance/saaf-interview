"""Store backend: orders, refunds and outbound customer email.

An in-memory stand-in for a small online shop's backend and the email provider
it uses. State lives in process memory, so run it as a single process.

    GET  /orders/{order_id}   look up an order
    POST /refunds             issue a refund (accepts an Idempotency-Key header)
    POST /emails              send an email to a customer (no idempotency support)
    GET  /ledger              every refund and email issued since the last reset
    POST /admin/reset         clear refunds, emails and stored idempotency keys
    GET  /healthz

Like the services it stands in for, it is not perfectly reliable: a small share
of refund and email calls fail with 503, and a small share of refunds are slow
to respond. Rates are configured through environment variables (see Settings).

All handlers are `async def` and run on one event loop, so a block of code with
no `await` in it runs without interruption; the idempotency bookkeeping relies
on that.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


@dataclass
class Settings:
    """Service configuration. `from_env` reads the environment variables."""

    workload_path: str = "/data/workload.json"
    seed: int = 11
    refund_error_rate: float = 0.01
    refund_slow_rate: float = 0.03
    refund_slow_s: float = 3.0
    email_error_rate: float = 0.01
    order_latency_min_ms: float = 20.0
    order_latency_max_ms: float = 80.0

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            workload_path=env.get("WORKLOAD_PATH", cls.workload_path),
            seed=int(env.get("COMMERCE_SEED", cls.seed)),
            refund_error_rate=float(env.get("REFUND_ERROR_RATE", cls.refund_error_rate)),
            refund_slow_rate=float(env.get("REFUND_SLOW_RATE", cls.refund_slow_rate)),
            refund_slow_s=float(env.get("REFUND_SLOW_S", cls.refund_slow_s)),
            email_error_rate=float(env.get("EMAIL_ERROR_RATE", cls.email_error_rate)),
            order_latency_min_ms=float(env.get("ORDER_LATENCY_MIN_MS", cls.order_latency_min_ms)),
            order_latency_max_ms=float(env.get("ORDER_LATENCY_MAX_MS", cls.order_latency_max_ms)),
        )


@dataclass
class Ledger:
    """Everything the store has done since the last reset."""

    refunds: list[dict] = field(default_factory=list)
    refund_replays: int = 0
    emails: list[dict] = field(default_factory=list)
    # Idempotency-Key -> (status_code, body) of the request that finished with that key.
    idempotency_results: dict[str, tuple[int, dict]] = field(default_factory=dict)
    # Idempotency-Keys whose first request is still being processed.
    idempotency_in_flight: set[str] = field(default_factory=set)


class RefundRequest(BaseModel):
    ticket_id: str
    order_id: str
    amount: float


class EmailRequest(BaseModel):
    ticket_id: str
    to: str
    subject: str
    body: str


def normalize_order(order_id: str, raw: dict) -> dict:
    """Shape a workload order into the public order representation."""
    return {
        "order_id": order_id,
        "customer_id": raw.get("customer_id"),
        "amount": round(float(raw["amount"]), 2),
        "currency": raw.get("currency", "USD"),
        "days_since_delivery": raw.get("days_since_delivery"),
        "final_sale": bool(raw.get("final_sale", False)),
        "status": raw.get("status", "delivered"),
    }


def load_orders(path: str) -> dict[str, dict]:
    """Read `workload["orders"]` (order_id -> order) from the workload file."""
    try:
        with open(path) as f:
            workload = json.load(f)
        raw_orders = workload["orders"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(
            f"could not load orders from {path!r} ({exc!r}); "
            "set WORKLOAD_PATH to a workload JSON file with an 'orders' object"
        ) from exc
    if isinstance(raw_orders, list):
        raw_orders = {o["order_id"]: o for o in raw_orders}
    return {oid: normalize_order(oid, o) for oid, o in raw_orders.items()}


def error(status_code: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status_code)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    rng = random.Random(settings.seed)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.orders = load_orders(settings.workload_path)
        yield

    app = FastAPI(title="Store backend", lifespan=lifespan)
    app.state.settings = settings
    app.state.orders = {}
    app.state.ledger = Ledger()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return error(422, "invalid request body", detail=jsonable_encoder(exc.errors()))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str):
        delay_ms = rng.uniform(settings.order_latency_min_ms, settings.order_latency_max_ms)
        await asyncio.sleep(delay_ms / 1000)
        order = app.state.orders.get(order_id)
        if order is None:
            return error(404, "order not found")
        return order

    def record_refund(ledger: Ledger, req: RefundRequest, key: str | None) -> tuple[int, dict]:
        """Validate the refund against the order and record it. Returns (status, body)."""
        order = app.state.orders.get(req.order_id)
        if order is None:
            return 404, {"error": "order not found"}
        if round(req.amount, 2) != order["amount"]:
            return 422, {"error": "amount must equal the order amount", "order_amount": order["amount"]}
        refund_id = f"rf_{uuid.uuid4().hex}"
        ledger.refunds.append(
            {
                "refund_id": refund_id,
                "ts": time.time(),
                "ticket_id": req.ticket_id,
                "order_id": req.order_id,
                "amount": order["amount"],
                "idempotency_key": key,
            }
        )
        return 201, {"refund_id": refund_id, "status": "issued"}

    @app.post("/refunds", status_code=201)
    async def create_refund(
        req: RefundRequest,
        idempotency_key: Annotated[str | None, Header()] = None,
    ):
        """Issue a full refund for an order.

        With an Idempotency-Key header, the first request that gets past the
        availability check is processed and its response stored; later requests
        with the same key get the stored response back (Idempotent-Replayed: true)
        without refunding again. While the first request is still running, other
        requests with that key get 409.
        """
        ledger = app.state.ledger  # a reset during this request swaps in a new ledger
        if rng.random() < settings.refund_error_rate:
            return error(503, "temporarily unavailable")

        key = (idempotency_key or "").strip() or None
        if key is not None:
            if key in ledger.idempotency_results:
                status, body = ledger.idempotency_results[key]
                ledger.refund_replays += 1
                return JSONResponse(body, status_code=status, headers={"Idempotent-Replayed": "true"})
            if key in ledger.idempotency_in_flight:
                return error(409, "a request with this idempotency key is in progress")
            # No await between the checks above and this line, so one request claims the key.
            ledger.idempotency_in_flight.add(key)

        result = None
        try:
            result = record_refund(ledger, req, key)
            if result[0] == 201 and rng.random() < settings.refund_slow_rate:
                # The refund is already recorded; only the response is late.
                await asyncio.sleep(settings.refund_slow_s)
        finally:
            if key is not None:
                ledger.idempotency_in_flight.discard(key)
                if result is not None:
                    ledger.idempotency_results[key] = result
        status, body = result
        return JSONResponse(body, status_code=status)

    @app.post("/emails", status_code=201)
    async def send_email(req: EmailRequest):
        """Send an email to a customer. Every accepted call sends a new email."""
        ledger = app.state.ledger
        if rng.random() < settings.email_error_rate:
            return error(503, "temporarily unavailable")
        email_id = f"em_{uuid.uuid4().hex}"
        ledger.emails.append(
            {
                "email_id": email_id,
                "ts": time.time(),
                "ticket_id": req.ticket_id,
                "to": req.to,
                "subject": req.subject,
            }
        )
        return JSONResponse({"email_id": email_id}, status_code=201)

    @app.get("/ledger")
    async def get_ledger():
        ledger = app.state.ledger
        return {
            "refunds": ledger.refunds,
            "refund_replays": ledger.refund_replays,
            "emails": ledger.emails,
            "summary": {
                "refunds": len(ledger.refunds),
                "tickets_refunded": len({r["ticket_id"] for r in ledger.refunds}),
                "emails": len(ledger.emails),
            },
        }

    @app.post("/admin/reset")
    async def reset():
        """Clear refunds, replays, emails and idempotency keys; restart the random sequence."""
        app.state.ledger = Ledger()
        rng.seed(settings.seed)
        return {"ok": True}

    return app


app = create_app()
