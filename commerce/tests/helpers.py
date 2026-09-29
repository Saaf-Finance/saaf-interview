"""Shared helpers for the test modules."""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from app import Settings, create_app

FIXTURE_WORKLOAD = Path(__file__).parent / "fixtures" / "workload.json"


def make_settings(**overrides) -> Settings:
    """Settings for tests: fixture workload, no failures, no slow refunds, no order latency."""
    values = dict(
        workload_path=str(FIXTURE_WORKLOAD),
        seed=11,
        refund_error_rate=0.0,
        refund_slow_rate=0.0,
        refund_slow_s=0.5,
        email_error_rate=0.0,
        order_latency_min_ms=0.0,
        order_latency_max_ms=0.0,
    )
    values.update(overrides)
    return Settings(**values)


@asynccontextmanager
async def running_app(**overrides):
    """Start the app in-process (including startup) and yield (app, client)."""
    app = create_app(make_settings(**overrides))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://commerce") as client:
            yield app, client
